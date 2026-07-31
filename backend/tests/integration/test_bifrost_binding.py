import asyncio
import base64
import json
from collections.abc import Iterator

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.integrations.bifrost.binding_service import (
    decrypt_binding_value,
    persist_created_binding,
    stage_bifrost_binding_sync,
    stage_local_client_disable,
)
from backend.app.integrations.bifrost.models import (
    BifrostUpstreamError,
    VirtualKeyCreationResult,
    VirtualKeySpec,
    VirtualKeyState,
)
from backend.app.models import Base
from backend.app.models.bindings import ApiClientBinding
from backend.app.models.clients import ApiClient
from backend.app.models.credentials import ApiClientCredential
from backend.app.models.outbox import ControlOutbox
from backend.app.security.keyring import CredentialKeyring
from backend.app.workers.outbox import BifrostOutboxWorker

VIRTUAL_KEY_VALUE = "sk-bf-binding-secret-redaction-sentinel"


@pytest.fixture
def engine() -> Iterator[Engine]:
    value = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(value)
    yield value
    value.dispose()


def keyring() -> CredentialKeyring:
    encoded = base64.b64encode(bytes(range(32))).decode("ascii")
    return CredentialKeyring.from_json(SecretStr(json.dumps({"v1": encoded})), active_key_id="v1")


def build_client() -> ApiClient:
    return ApiClient(
        id="client-1",
        name="Caller One",
        description=None,
        status="active",
        allowed_endpoints=["chat.completions"],
        allowed_models=["model-1"],
        group_ids=[],
        qps_limit=2,
        concurrency_limit=1,
        daily_request_limit=100,
        daily_token_limit=10_000,
        total_request_limit=None,
        total_token_limit=None,
        max_output_tokens_per_request=2_048,
        version=1,
        created_by="admin-1",
        updated_by="admin-1",
        created_at=1_700_000_000,
        updated_at=1_700_000_000,
    )


def build_binding(*, active: bool = False) -> ApiClientBinding:
    return ApiClientBinding(
        id="binding-1",
        api_client_id="client-1",
        zangpu_service_user_id="service-user-1" if active else None,
        bifrost_virtual_key_id=None,
        bifrost_value_ciphertext=None,
        bifrost_value_key_version=None,
        bifrost_config_hash="desired-config-hash",
        sync_status="pending",
        version=1,
        created_at=1_700_000_000,
        updated_at=1_700_000_000,
    )


def creation_result() -> VirtualKeyCreationResult:
    return VirtualKeyCreationResult(
        state=VirtualKeyState(
            id="vk-1",
            name="zangpu-client-1",
            description="managed by Zangpu",
            is_active=True,
            provider="provider-1",
            model="model-1",
            config_hash="vendor-config-hash",
        ),
        value=SecretStr(VIRTUAL_KEY_VALUE),
    )


def test_created_virtual_key_is_persisted_only_as_authenticated_ciphertext() -> None:
    binding = build_binding()
    result = creation_result()

    persist_created_binding(binding, result, keyring(), now=1_700_000_100)

    assert binding.bifrost_virtual_key_id == "vk-1"
    assert binding.sync_status == "pending"
    assert binding.bifrost_value_key_version == "v1"
    assert VIRTUAL_KEY_VALUE not in (binding.bifrost_value_ciphertext or "")
    assert VIRTUAL_KEY_VALUE not in repr(binding)
    assert decrypt_binding_value(binding, keyring()).get_secret_value() == VIRTUAL_KEY_VALUE
    with pytest.raises(RuntimeError, match="already been read"):
        result.take_value()


def test_local_disable_and_credential_revoke_precede_remote_sync(engine: Engine) -> None:
    with Session(engine) as session:
        client = build_client()
        binding = build_binding()
        credential = ApiClientCredential(
            id="credential-1",
            api_client_id="client-1",
            key_id="zpk_public_1",
            secret_ciphertext="encrypted",  # noqa: S106 - non-secret fixture
            secret_nonce="nonce",  # noqa: S106 - non-secret fixture
            master_key_id="v1",
            secret_fingerprint="fingerprint",  # noqa: S106 - non-secret fixture
            status="active",
            created_by="admin-1",
            created_at=1_700_000_000,
        )
        session.add_all((client, binding, credential))
        session.commit()

        item = stage_local_client_disable(
            session,
            client=client,
            binding=binding,
            actor_id="admin-2",
            idempotency_key="disable-client-1-v2",
            now=1_700_000_100,
        )
        session.commit()

        assert client.status == "disabled" and client.disabled_at == 1_700_000_100
        assert credential.status == "revoked" and credential.revoked_at == 1_700_000_100
        assert binding.sync_status == "pending"
        assert item.action == "disable" and item.status == "pending"
        assert VIRTUAL_KEY_VALUE not in repr(item.payload)


class FlakyBifrostClient:
    def __init__(self) -> None:
        self.disable_calls = 0

    async def disable_virtual_key(self, virtual_key_id: str) -> VirtualKeyState:
        assert virtual_key_id == "vk-1"
        self.disable_calls += 1
        if self.disable_calls == 1:
            raise BifrostUpstreamError(code="BIFROST_UNAVAILABLE", status_code=503, retryable=True)
        return VirtualKeyState(
            id="vk-1",
            name="zangpu-client-1",
            description="managed by Zangpu",
            is_active=False,
            provider="provider-1",
            model="model-1",
            config_hash="vendor-config-hash",
        )


class RecoveringBifrostClient:
    def __init__(self) -> None:
        self.find_calls = 0
        self.create_calls = 0

    async def find_virtual_key_by_name(self, name: str) -> VirtualKeyCreationResult:
        assert name == "zangpu-client-1"
        self.find_calls += 1
        return creation_result()

    async def create_virtual_key(self, _spec: VirtualKeySpec) -> VirtualKeyCreationResult:
        self.create_calls += 1
        raise AssertionError("an existing remote create must be reconciled")


def test_outbox_recovers_an_existing_remote_create_by_stable_name(engine: Engine) -> None:
    ring = keyring()
    factory = sessionmaker(engine, expire_on_commit=False)
    remote = RecoveringBifrostClient()
    spec = VirtualKeySpec(
        name="zangpu-client-1",
        description="managed by Zangpu",
        provider="provider-1",
        model="model-1",
    )

    with factory() as session:
        client = build_client()
        binding = build_binding(active=True)
        session.add_all((client, binding))
        session.commit()
        item = stage_bifrost_binding_sync(
            session,
            binding=binding,
            spec=spec,
            action="create",
            idempotency_key="create-client-1-v2",
            now=1_700_000_100,
        )
        session.commit()
        assert VIRTUAL_KEY_VALUE not in repr(item.payload)

    worker = BifrostOutboxWorker(factory, remote, ring)
    assert asyncio.run(worker.run_once(now=1_700_000_100)) == 1

    with factory() as session:
        binding = session.get(ApiClientBinding, "binding-1")
        item = session.scalar(select(ControlOutbox))
        assert binding is not None and binding.sync_status == "active"
        assert decrypt_binding_value(binding, ring).get_secret_value() == VIRTUAL_KEY_VALUE
        assert item is not None and (item.status, item.attempt_count) == ("completed", 1)

    assert (remote.find_calls, remote.create_calls) == (1, 0)


def test_outbox_retries_remote_disable_idempotently_without_reenabling_caller(engine: Engine) -> None:
    ring = keyring()
    factory = sessionmaker(engine, expire_on_commit=False)
    remote = FlakyBifrostClient()

    with factory() as session:
        client = build_client()
        binding = build_binding(active=True)
        persist_created_binding(binding, creation_result(), ring, now=1_700_000_010)
        session.add_all((client, binding))
        session.commit()
        stage_local_client_disable(
            session,
            client=client,
            binding=binding,
            actor_id="admin-2",
            idempotency_key="disable-client-1-v2",
            now=1_700_000_100,
        )
        session.commit()

    worker = BifrostOutboxWorker(factory, remote, ring, max_attempts=3, base_retry_seconds=5)
    first = asyncio.run(worker.run_once(now=1_700_000_100))

    with factory() as session:
        client = session.get(ApiClient, "client-1")
        binding = session.get(ApiClientBinding, "binding-1")
        item = session.scalar(select(ControlOutbox))
        assert client is not None and client.status == "disabled"
        assert binding is not None and binding.sync_status == "error"
        assert item is not None
        assert (item.status, item.attempt_count, item.last_error_code, item.available_at) == (
            "failed",
            1,
            "BIFROST_UNAVAILABLE",
            1_700_000_105,
        )

    second = asyncio.run(worker.run_once(now=1_700_000_105))

    with factory() as session:
        client = session.get(ApiClient, "client-1")
        binding = session.get(ApiClientBinding, "binding-1")
        item = session.scalar(select(ControlOutbox))
        assert client is not None and client.status == "disabled"
        assert binding is not None and binding.sync_status == "disabled"
        assert item is not None
        assert (item.status, item.attempt_count, item.completed_at, item.last_error_code) == (
            "completed",
            2,
            1_700_000_105,
            None,
        )

    assert (first, second, remote.disable_calls) == (1, 1, 2)
