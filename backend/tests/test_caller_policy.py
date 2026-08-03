import asyncio
import base64
import json
from collections.abc import Iterator

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.database import create_database_runtime
from backend.app.integrations.bifrost.binding_service import persist_created_binding
from backend.app.integrations.bifrost.models import VirtualKeyCreationResult, VirtualKeyState
from backend.app.models import Base
from backend.app.models.bindings import ApiClientBinding
from backend.app.models.clients import ApiClient
from backend.app.security.credentials import create_protected_credential
from backend.app.security.dependencies import AuthenticatedCaller
from backend.app.security.keyring import CredentialKeyring
from backend.app.services.callers import CallerPolicyError, DatabaseCredentialResolver, load_caller_policy

VIRTUAL_KEY_VALUE = "vk-policy-redaction-sentinel"


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


def seed_caller(engine: Engine, *, binding_status: str = "active") -> CredentialKeyring:
    ring = keyring()
    client = ApiClient(
        id="client-1",
        name="Caller One",
        description=None,
        status="active",
        allowed_endpoints=["chat.completions"],
        allowed_models=["model-1"],
        group_ids=[],
        qps_limit=3,
        concurrency_limit=2,
        daily_request_limit=100,
        daily_token_limit=10_000,
        total_request_limit=1_000,
        total_token_limit=100_000,
        max_output_tokens_per_request=2_048,
        version=1,
        created_by="admin-1",
        updated_by="admin-1",
        created_at=1_785_420_000,
        updated_at=1_785_420_000,
    )
    credential = create_protected_credential(
        ring,
        api_client_id=client.id,
        created_by="admin-1",
        credential_id="credential-1",
        key_id="zpk_policy_0123456789",
        now=1_785_420_000,
        expires_at=1_785_430_000,
    ).credential
    binding = ApiClientBinding(
        id="binding-1",
        api_client_id=client.id,
        zangpu_service_user_id="10000000-0000-4000-8000-000000000001",
        bifrost_virtual_key_id=None,
        bifrost_value_ciphertext=None,
        bifrost_value_key_version=None,
        bifrost_config_hash="desired-config-hash",
        sync_status="pending",
        version=1,
        created_at=1_785_420_000,
        updated_at=1_785_420_000,
    )
    persist_created_binding(
        binding,
        VirtualKeyCreationResult(
            state=VirtualKeyState(
                id="vk-1",
                name="zangpu-client-1",
                description="managed by Zangpu",
                is_active=True,
                provider="provider-1",
                model="model-1",
                config_hash="remote-config-hash",
            ),
            value=SecretStr(VIRTUAL_KEY_VALUE),
        ),
        ring,
        now=1_785_420_000,
    )
    binding.sync_status = binding_status
    with Session(engine) as session:
        session.add_all((client, credential, binding))
        session.commit()
    return ring


def test_database_runtime_is_lazy_and_secret_safe() -> None:
    runtime = create_database_runtime("postgresql+psycopg://control:private-value@unresolved.invalid:5432/control")
    try:
        assert "private-value" not in repr(runtime)
        assert runtime.sessions.kw["expire_on_commit"] is False
    finally:
        runtime.close()


def test_database_resolver_and_caller_policy_keep_protected_material_private(engine: Engine) -> None:
    ring = seed_caller(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    resolver = DatabaseCredentialResolver(factory)

    resolved = asyncio.run(resolver("zpk_policy_0123456789"))
    assert resolved is not None
    assert (resolved.api_client_id, resolved.credential_id, resolved.client_status) == (
        "client-1",
        "credential-1",
        "active",
    )
    assert "ciphertext" not in repr(resolved).lower()
    assert asyncio.run(resolver("zpk_unknown_0123456789")) is None

    caller = AuthenticatedCaller(
        api_client_id="client-1",
        credential_id="credential-1",
        key_id="zpk_policy_0123456789",
        request_id="req_policy_0123456789",
        nonce="nonce_policy_0123456789",
        timestamp=1_785_420_100,
    )
    with factory() as session:
        policy = load_caller_policy(session, caller=caller, keyring=ring, now=1_785_420_100)

    assert policy.allowed_endpoints == ("chat.completions",)
    assert policy.allowed_models == ("model-1",)
    assert policy.qps_limit == 3
    assert policy.concurrency_limit == 2
    assert policy.service_user_id == "10000000-0000-4000-8000-000000000001"
    assert policy.bifrost_virtual_key.get_secret_value() == VIRTUAL_KEY_VALUE
    assert VIRTUAL_KEY_VALUE not in repr(policy)
    assert "ciphertext" not in repr(policy).lower()


def test_caller_policy_rejects_non_active_binding(engine: Engine) -> None:
    ring = seed_caller(engine, binding_status="error")
    caller = AuthenticatedCaller(
        api_client_id="client-1",
        credential_id="credential-1",
        key_id="zpk_policy_0123456789",
        request_id="req_policy_0123456789",
        nonce="nonce_policy_0123456789",
        timestamp=1_785_420_100,
    )
    with Session(engine) as session, pytest.raises(CallerPolicyError) as captured:
        load_caller_policy(session, caller=caller, keyring=ring, now=1_785_420_100)
    assert captured.value.code == "CALLER_BINDING_UNAVAILABLE"
