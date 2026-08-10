import base64
import json

import pytest
from pydantic import SecretStr
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.models import Base
from backend.app.models.audits import ApiClientAdminAudit
from backend.app.models.clients import ApiClient
from backend.app.models.credentials import ApiClientCredential
from backend.app.models.outbox import ControlOutbox
from backend.app.security.admin import AdminSessionError, AdminSessionManager
from backend.app.security.credentials import OneTimeSecretAlreadyRead
from backend.app.security.keyring import CredentialKeyring
from backend.app.services.admin import (
    AdminCallerCreateRequest,
    AdminCallerService,
    AdminCallerStateError,
)


def keyring() -> CredentialKeyring:
    value = json.dumps({"v1": base64.b64encode(bytes(range(32))).decode("ascii")})
    return CredentialKeyring.from_json(SecretStr(value), active_key_id="v1")


def sessions() -> sessionmaker:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def request(**overrides: object) -> AdminCallerCreateRequest:
    values: dict[str, object] = {
        "name": "解剖学调用方",
        "description": "合同 API caller",
        "service_user_id": "10000000-0000-4000-8000-000000000001",
        "provider": "provider-1",
        "model": "model-1",
        "allowed_endpoints": ["chat.completions", "models.read", "usage.read"],
        "allowed_models": ["model-1"],
        "qps_limit": 10,
        "concurrency_limit": 2,
        "daily_request_limit": 100,
        "daily_token_limit": 10_000,
        "total_request_limit": 1_000,
        "total_token_limit": 100_000,
        "max_output_tokens_per_request": 128,
    }
    values.update(overrides)
    return AdminCallerCreateRequest.model_validate(values)


def test_admin_session_is_signed_and_bootstrap_secret_is_not_a_session_token() -> None:
    manager = AdminSessionManager(
        session_secret="admin-session-secret-that-is-at-least-32-bytes",  # noqa: S106
        bootstrap_token="admin-login-token-that-is-at-least-32-bytes",  # noqa: S106
        ttl_seconds=600,
    )

    issued = manager.issue("admin-login-token-that-is-at-least-32-bytes", now=1_800_000_000)
    claims = manager.verify(issued.token, now=1_800_000_100)

    assert claims.actor_id == "admin"
    assert claims.expires_at == 1_800_000_600
    assert claims.csrf_token == issued.csrf_token
    assert issued.token != "admin-login-token-that-is-at-least-32-bytes"  # noqa: S105
    with pytest.raises(AdminSessionError):
        manager.issue("wrong-token-that-is-at-least-32-bytes", now=1_800_000_000)
    with pytest.raises(AdminSessionError):
        manager.verify(issued.token, now=1_800_000_601)


def test_admin_caller_creation_is_one_time_secret_and_audited() -> None:
    service = AdminCallerService(sessions(), keyring())
    created = service.create_caller(request(), actor_id="admin", idempotency_key="create-1", now=1_800_000_000)
    secret = created.take_secret()

    assert secret.startswith("zps_")
    with pytest.raises(OneTimeSecretAlreadyRead):
        created.take_secret()
    with service._sessions() as session:  # noqa: SLF001
        client = session.get(ApiClient, created.client.id)
        credential = session.scalar(select(ApiClientCredential).where(ApiClientCredential.api_client_id == client.id))
        outbox = session.scalar(select(ControlOutbox))
        audit = session.scalar(select(ApiClientAdminAudit).where(ApiClientAdminAudit.api_client_id == client.id))

    assert client is not None and credential is not None and outbox is not None and audit is not None
    assert secret not in repr((client, credential, outbox, audit))
    assert audit.action == "caller.created"
    assert outbox.action == "create"


def test_admin_disable_revokes_credentials_and_queues_bifrost_disable() -> None:
    service = AdminCallerService(sessions(), keyring())
    created = service.create_caller(request(), actor_id="admin", idempotency_key="create-2", now=1_800_000_000)
    created.take_secret()

    disabled = service.disable_caller(
        created.client.id,
        actor_id="admin",
        idempotency_key="disable-1",
        now=1_800_000_010,
    )

    assert disabled.client.status == "disabled"
    with service._sessions() as session:  # noqa: SLF001
        credential = session.scalar(
            select(ApiClientCredential).where(ApiClientCredential.api_client_id == created.client.id)
        )
        outboxes = session.scalars(select(ControlOutbox)).all()
    assert credential is not None and credential.status == "revoked"
    assert {item.action for item in outboxes} == {"create", "disable"}


def test_admin_update_rejects_stale_caller_version() -> None:
    service = AdminCallerService(sessions(), keyring())
    created = service.create_caller(request(), actor_id="admin", idempotency_key="create-3", now=1_800_000_000)
    created.take_secret()

    with pytest.raises(AdminCallerStateError, match="version"):
        service.update_caller(
            created.client.id,
            expected_version=99,
            patch={"qps_limit": 20},
            actor_id="admin",
            now=1_800_000_010,
        )
