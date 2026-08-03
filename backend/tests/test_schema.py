from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, delete, event, inspect, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.models import Base
from backend.app.models.audits import ApiClientAdminAudit, ImmutableAuditError
from backend.app.models.bindings import ApiClientBinding, BindingSummary
from backend.app.models.clients import ApiClient
from backend.app.models.credentials import ApiClientCredential, CredentialSummary
from backend.app.models.events import ApiCallEvent, ImmutableEventError
from backend.app.models.operations import ApiCallOperation
from backend.app.models.outbox import ControlOutbox, queue_binding_sync
from backend.app.models.quotas import ApiClientQuotaUsage


def build_client(*, client_id: str = "client-1", name: str = "Caller One", **overrides: object) -> ApiClient:
    values: dict[str, object] = {
        "id": client_id,
        "name": name,
        "description": None,
        "status": "active",
        "allowed_endpoints": ["chat.completions", "models.read"],
        "allowed_models": ["zangpu-model-1"],
        "group_ids": [],
        "qps_limit": 2,
        "concurrency_limit": 1,
        "daily_request_limit": 100,
        "daily_token_limit": 10_000,
        "total_request_limit": None,
        "total_token_limit": None,
        "max_output_tokens_per_request": 2_048,
        "version": 1,
        "created_by": "admin-1",
        "updated_by": "admin-1",
        "created_at": 1_700_000_000,
        "updated_at": 1_700_000_000,
    }
    values.update(overrides)
    return ApiClient(**values)


def build_credential(
    *, credential_id: str = "credential-1", client_id: str = "client-1", key_id: str = "zpk_public_1"
) -> ApiClientCredential:
    return ApiClientCredential(
        id=credential_id,
        api_client_id=client_id,
        key_id=key_id,
        secret_ciphertext="SENSITIVE_CREDENTIAL_CIPHERTEXT",  # noqa: S106 - non-secret redaction sentinel
        secret_nonce="SENSITIVE_CREDENTIAL_NONCE",  # noqa: S106 - non-secret redaction sentinel
        master_key_id="key-version-1",
        secret_fingerprint="SENSITIVE_CREDENTIAL_FINGERPRINT",  # noqa: S106 - non-secret redaction sentinel
        status="active",
        created_by="admin-1",
        created_at=1_700_000_000,
    )


def build_operation(
    *, operation_id: str = "operation-1", client_id: str = "client-1", credential_id: str = "credential-1"
) -> ApiCallOperation:
    return ApiCallOperation(
        id=operation_id,
        api_client_id=client_id,
        credential_id=credential_id,
        client_request_id="request-1",
        request_fingerprint="fingerprint-1",
        endpoint="chat.completions",
        method="POST",
        model_id="zangpu-model-1",
        status="completed",
        reserved_tokens=128,
        prompt_tokens=10,
        completion_tokens=20,
        total_tokens=30,
        terminal_http_status=200,
        terminal_code="OK",
        started_at=1_700_000_000,
        completed_at=1_700_000_001,
        updated_at=1_700_000_001,
    )


def build_event(**overrides: object) -> ApiCallEvent:
    values: dict[str, object] = {
        "id": "event-1",
        "server_request_id": "server-request-1",
        "client_request_id": "request-1",
        "operation_id": "operation-1",
        "api_client_id": "client-1",
        "credential_id": "credential-1",
        "endpoint": "chat.completions",
        "method": "POST",
        "model_id": "zangpu-model-1",
        "stream": False,
        "outcome": "success",
        "stage": "response",
        "http_status": 200,
        "business_code": "OK",
        "retryable": False,
        "duration_ms": 1_000,
        "quota_overrun": False,
        "prompt_tokens": 10,
        "completion_tokens": 20,
        "total_tokens": 30,
        "charged_micro": 0,
        "qps_observed": 1,
        "concurrency_observed": 1,
        "daily_requests_after": 1,
        "daily_tokens_after": 30,
        "total_requests_after": 1,
        "total_tokens_after": 30,
        "remote_ip_hash": "remote-ip-hash",
        "user_agent_family": "test-client",
        "started_at": 1_700_000_000,
        "completed_at": 1_700_000_001,
        "created_at": 1_700_000_001,
    }
    values.update(overrides)
    return ApiCallEvent(**values)


def build_audit(**overrides: object) -> ApiClientAdminAudit:
    values: dict[str, object] = {
        "id": "audit-1",
        "actor_user_id": "admin-1",
        "api_client_id": "client-1",
        "target_type": "client",
        "target_id": "client-1",
        "action": "update",
        "changed_fields": ["allowed_models", "status"],
        "before_summary": {"allowed_models": ["model-1"], "status": "active"},
        "after_summary": {"allowed_models": ["model-1", "model-2"], "status": "disabled"},
        "created_at": 1_700_000_000,
    }
    values.update(overrides)
    return ApiClientAdminAudit(**values)


@pytest.fixture
def engine() -> Iterator[Engine]:
    database = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(database, "connect")
    def enable_foreign_keys(dbapi_connection: object, _connection_record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(database)
    yield database
    database.dispose()


def seed_client_and_credential(engine: Engine) -> None:
    with Session(engine) as session:
        session.add(build_client())
        session.add(build_credential())
        session.commit()


def test_contract_identifiers_and_bindings_are_unique(engine: Engine) -> None:
    seed_client_and_credential(engine)

    with Session(engine) as session:
        session.add(build_client(client_id="client-2", name="Caller One"))
        with pytest.raises(IntegrityError):
            session.commit()

    with Session(engine) as session:
        session.add(build_client(client_id="client-2", name="Caller Two"))
        session.commit()
        session.add(build_credential(credential_id="credential-2", client_id="client-2", key_id="zpk_public_1"))
        with pytest.raises(IntegrityError):
            session.commit()

    with Session(engine) as session:
        session.add(
            ApiClientBinding(
                id="binding-1",
                api_client_id="client-1",
                zangpu_service_user_id="service-user-1",
                bifrost_virtual_key_id="bifrost-key-1",
                bifrost_value_ciphertext="SENSITIVE_BIFROST_CIPHERTEXT",
                bifrost_value_key_version="key-version-1",
                bifrost_config_hash="config-hash-1",
                sync_status="active",
                version=1,
                created_at=1_700_000_000,
                updated_at=1_700_000_000,
            )
        )
        session.commit()

    with Session(engine) as session:
        session.add(
            ApiClientBinding(
                id="binding-client-duplicate",
                api_client_id="client-1",
                zangpu_service_user_id="service-user-2",
                bifrost_virtual_key_id="bifrost-key-2",
                bifrost_value_ciphertext="SENSITIVE_BIFROST_CIPHERTEXT_2",
                bifrost_value_key_version="key-version-1",
                bifrost_config_hash="config-hash-2",
                sync_status="active",
                version=1,
                created_at=1_700_000_000,
                updated_at=1_700_000_000,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()

    with Session(engine) as session:
        session.add(
            ApiClientBinding(
                id="binding-service-duplicate",
                api_client_id="client-2",
                zangpu_service_user_id="service-user-1",
                bifrost_virtual_key_id="bifrost-key-2",
                bifrost_value_ciphertext="SENSITIVE_BIFROST_CIPHERTEXT_2",
                bifrost_value_key_version="key-version-1",
                bifrost_config_hash="config-hash-2",
                sync_status="active",
                version=1,
                created_at=1_700_000_000,
                updated_at=1_700_000_000,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()

    with Session(engine) as session:
        session.add(
            ApiClientBinding(
                id="binding-bifrost-duplicate",
                api_client_id="client-2",
                zangpu_service_user_id="service-user-3",
                bifrost_virtual_key_id="bifrost-key-1",
                bifrost_value_ciphertext="SENSITIVE_BIFROST_CIPHERTEXT_3",
                bifrost_value_key_version="key-version-1",
                bifrost_config_hash="config-hash-3",
                sync_status="active",
                version=1,
                created_at=1_700_000_000,
                updated_at=1_700_000_000,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()

    with Session(engine) as session:
        session.add(build_operation())
        session.commit()
        duplicate = build_operation(operation_id="operation-2")
        duplicate.client_request_id = "request-1"
        session.add(duplicate)
        with pytest.raises(IntegrityError):
            session.commit()


def test_status_permission_and_quota_values_are_bounded(engine: Engine) -> None:
    with pytest.raises(ValueError, match="allowed_endpoints"):
        build_client(allowed_endpoints=["unknown.endpoint"])

    with Session(engine) as session:
        session.add(build_client(status="unknown"))
        with pytest.raises(IntegrityError):
            session.commit()

    with Session(engine) as session:
        session.add(build_client(qps_limit=0))
        with pytest.raises(IntegrityError):
            session.commit()

    seed_client_and_credential(engine)
    invalid_rows = [
        build_credential(credential_id="credential-invalid", key_id="zpk_invalid"),
        build_operation(operation_id="operation-invalid"),
        ApiClientBinding(
            id="binding-invalid",
            api_client_id="client-1",
            zangpu_service_user_id="service-user-invalid",
            bifrost_virtual_key_id="bifrost-key-invalid",
            bifrost_value_ciphertext="SENSITIVE_BIFROST_CIPHERTEXT_INVALID",
            bifrost_value_key_version="key-version-1",
            bifrost_config_hash="config-hash-invalid",
            sync_status="invalid",
            version=1,
            created_at=1_700_000_000,
            updated_at=1_700_000_000,
        ),
        ControlOutbox(
            id="outbox-invalid",
            aggregate_type="api_client_binding",
            aggregate_id="binding-invalid",
            target="bifrost",
            action="update",
            idempotency_key="outbox-invalid",
            payload={},
            status="invalid",
            attempt_count=0,
            available_at=1_700_000_000,
            created_at=1_700_000_000,
            updated_at=1_700_000_000,
        ),
    ]
    invalid_rows[0].status = "invalid"
    invalid_rows[1].status = "invalid"

    for row in invalid_rows:
        with Session(engine) as session:
            session.add(row)
            with pytest.raises(IntegrityError):
                session.commit()

    with Session(engine) as session:
        session.add(build_operation())
        session.commit()
        session.add(build_event(outcome="invalid"))
        with pytest.raises(IntegrityError):
            session.commit()

    with Session(engine) as session:
        malformed_key = build_credential(credential_id="credential-malformed", key_id="invalid-public-id")
        session.add(malformed_key)
        with pytest.raises(IntegrityError):
            session.commit()

    with Session(engine) as session:
        session.add(
            ApiClientQuotaUsage(
                id="quota-1",
                api_client_id="client-1",
                scope="daily",
                period_start=1_699_920_000,
                request_count=-1,
                token_reserved=0,
                token_consumed=0,
                version=1,
                updated_at=1_700_000_000,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_terminal_events_are_append_only(engine: Engine) -> None:
    seed_client_and_credential(engine)
    with Session(engine) as session:
        session.add(build_operation())
        session.add(build_event())
        session.commit()

        terminal_event = session.get(ApiCallEvent, "event-1")
        assert terminal_event is not None
        terminal_event.outcome = "system_error"
        with pytest.raises(ImmutableEventError):
            session.commit()
        session.rollback()

        terminal_event = session.get(ApiCallEvent, "event-1")
        assert terminal_event is not None
        session.delete(terminal_event)
        with pytest.raises(ImmutableEventError):
            session.commit()

        session.rollback()
        with pytest.raises(ImmutableEventError):
            session.execute(update(ApiCallEvent).where(ApiCallEvent.id == "event-1").values(outcome="system_error"))
        with pytest.raises(ImmutableEventError):
            session.execute(delete(ApiCallEvent).where(ApiCallEvent.id == "event-1"))


def test_admin_audits_are_sanitized_append_only_history(engine: Engine) -> None:
    with Session(engine) as session:
        session.add(build_client())
        session.add(build_audit())
        session.commit()

        audit = session.get(ApiClientAdminAudit, "audit-1")
        assert audit is not None
        assert audit.changed_fields == ["allowed_models", "status"]
        audit.action = "archive"
        with pytest.raises(ImmutableAuditError):
            session.commit()
        session.rollback()

        audit = session.get(ApiClientAdminAudit, "audit-1")
        assert audit is not None
        session.delete(audit)
        with pytest.raises(ImmutableAuditError):
            session.commit()
        session.rollback()

        with pytest.raises(ImmutableAuditError):
            session.execute(
                update(ApiClientAdminAudit)
                .where(ApiClientAdminAudit.id == "audit-1")
                .values(action="archive")
            )
        with pytest.raises(ImmutableAuditError):
            session.execute(delete(ApiClientAdminAudit).where(ApiClientAdminAudit.id == "audit-1"))

        with pytest.raises(IntegrityError):
            session.execute(delete(ApiClient).where(ApiClient.id == "client-1"))
        session.rollback()


def test_admin_audit_rejects_sensitive_or_unstructured_summaries(engine: Engine) -> None:
    with pytest.raises(ValueError, match="sensitive"):
        build_audit(changed_fields=["secret_ciphertext"])
    with pytest.raises(ValueError, match="sensitive"):
        build_audit(before_summary={"Authorization": "redacted"})
    with pytest.raises(ValueError, match="sensitive"):
        build_audit(after_summary={"secretCiphertext": "redacted"})
    with pytest.raises(ValueError, match="flat"):
        build_audit(before_summary={"status": {"from": "active"}})
    with pytest.raises(ValueError, match="flat"):
        build_audit(after_summary={"allowed_models": [["model-1"]]})

    with Session(engine) as session:
        session.add(build_client())
        session.add(build_audit(after_summary={"description": "redacted"}))
        with pytest.raises(ValueError, match="changed_fields"):
            session.commit()


def test_pending_binding_can_exist_before_remote_identifiers(engine: Engine) -> None:
    with Session(engine) as session:
        session.add(build_client())
        session.add(
            ApiClientBinding(
                id="binding-pending",
                api_client_id="client-1",
                zangpu_service_user_id=None,
                bifrost_virtual_key_id=None,
                bifrost_value_ciphertext=None,
                bifrost_value_key_version=None,
                bifrost_config_hash="pending-config-hash",
                sync_status="pending",
                version=1,
                created_at=1_700_000_000,
                updated_at=1_700_000_000,
            )
        )
        session.add(
            ControlOutbox(
                id="outbox-create",
                aggregate_type="api_client_binding",
                aggregate_id="binding-pending",
                target="bifrost",
                action="create",
                idempotency_key="binding-pending-create",
                payload={"config_hash": "pending-config-hash"},
                status="pending",
                attempt_count=0,
                available_at=1_700_000_000,
                created_at=1_700_000_000,
                updated_at=1_700_000_000,
            )
        )
        session.commit()

        binding = session.get(ApiClientBinding, "binding-pending")
        assert binding is not None
        assert binding.sync_status == "pending"
        assert binding.bifrost_virtual_key_id is None


def test_default_response_models_exclude_secret_material() -> None:
    credential = build_credential()
    credential_payload = CredentialSummary.model_validate(credential).model_dump()
    binding = ApiClientBinding(
        id="binding-1",
        api_client_id="client-1",
        zangpu_service_user_id="service-user-1",
        bifrost_virtual_key_id="bifrost-key-1",
        bifrost_value_ciphertext="SENSITIVE_BIFROST_CIPHERTEXT",
        bifrost_value_key_version="key-version-1",
        bifrost_config_hash="config-hash-1",
        sync_status="active",
        version=1,
        created_at=1_700_000_000,
        updated_at=1_700_000_000,
    )
    binding_payload = BindingSummary.model_validate(binding).model_dump()

    rendered = repr((credential_payload, binding_payload))
    assert "SENSITIVE_CREDENTIAL_CIPHERTEXT" not in rendered
    assert "SENSITIVE_CREDENTIAL_FINGERPRINT" not in rendered
    assert "SENSITIVE_BIFROST_CIPHERTEXT" not in rendered
    assert {"secret_ciphertext", "secret_nonce", "secret_fingerprint", "master_key_id"}.isdisjoint(credential_payload)
    assert {"bifrost_value_ciphertext", "bifrost_value_key_version"}.isdisjoint(binding_payload)


def test_binding_desired_state_and_outbox_are_atomic(engine: Engine) -> None:
    with Session(engine) as session:
        session.add(build_client())
        session.add(
            ApiClientBinding(
                id="binding-1",
                api_client_id="client-1",
                zangpu_service_user_id="service-user-1",
                bifrost_virtual_key_id="bifrost-key-1",
                bifrost_value_ciphertext="SENSITIVE_BIFROST_CIPHERTEXT",
                bifrost_value_key_version="key-version-1",
                bifrost_config_hash="old-config-hash",
                sync_status="active",
                version=1,
                created_at=1_700_000_000,
                updated_at=1_700_000_000,
            )
        )
        session.add(
            ControlOutbox(
                id="outbox-existing",
                aggregate_type="api_client_binding",
                aggregate_id="binding-1",
                target="bifrost",
                action="update",
                idempotency_key="duplicate-key",
                payload={"config_hash": "old-config-hash"},
                status="pending",
                attempt_count=0,
                available_at=1_700_000_000,
                created_at=1_700_000_000,
                updated_at=1_700_000_000,
            )
        )
        session.commit()

    with Session(engine) as session:
        binding = session.get(ApiClientBinding, "binding-1")
        assert binding is not None
        queue_binding_sync(
            session,
            binding=binding,
            desired_config_hash="new-config-hash",
            target="bifrost",
            action="update",
            idempotency_key="duplicate-key",
            payload={"config_hash": "new-config-hash"},
            now=1_700_000_100,
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

    with Session(engine) as session:
        binding = session.get(ApiClientBinding, "binding-1")
        assert binding is not None
        assert (binding.bifrost_config_hash, binding.sync_status, binding.version) == ("old-config-hash", "active", 1)
        assert len(session.scalars(select(ControlOutbox)).all()) == 1

        with pytest.raises(ValueError, match="sensitive"):
            queue_binding_sync(
                session,
                binding=binding,
                desired_config_hash="new-config-hash",
                target="bifrost",
                action="update",
                idempotency_key="safe-key",
                payload={"secret": "must-not-enter-outbox"},
                now=1_700_000_100,
            )
        with pytest.raises(ValueError, match="sensitive"):
            queue_binding_sync(
                session,
                binding=binding,
                desired_config_hash="new-config-hash",
                target="bifrost",
                action="update",
                idempotency_key="safe-key-2",
                payload={"desired": {"secretCiphertext": "must-not-enter-outbox"}},
                now=1_700_000_100,
            )


def test_every_foreign_key_has_a_covering_index(engine: Engine) -> None:
    schema = inspect(engine)
    for table_name in Base.metadata.tables:
        indexes = [tuple(item["column_names"]) for item in schema.get_indexes(table_name)]
        indexes.extend(tuple(item["column_names"]) for item in schema.get_unique_constraints(table_name))
        indexes.extend((column,) for column in schema.get_pk_constraint(table_name).get("constrained_columns", []))
        for foreign_key in schema.get_foreign_keys(table_name):
            columns = tuple(foreign_key["constrained_columns"])
            assert any(index[: len(columns)] == columns for index in indexes), (table_name, columns)
