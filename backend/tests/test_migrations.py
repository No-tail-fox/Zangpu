from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from backend.app.models import Base

ROOT = Path(__file__).resolve().parents[2]
CONTRACT_TABLES = {
    "api_client",
    "api_client_admin_audit",
    "api_client_binding",
    "api_client_credential",
    "api_client_quota_usage",
    "api_call_event",
    "api_call_operation",
    "control_outbox",
}


def migration_config(database_url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "backend" / "migrations").replace("\\", "/"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def test_migration_has_one_head_and_round_trips_cleanly(tmp_path: Path) -> None:
    database_path = tmp_path / "migration.sqlite3"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = migration_config(database_url)
    script = ScriptDirectory.from_config(config)

    assert len(script.get_heads()) == 1

    command.upgrade(config, "head")
    command.check(config)
    engine = create_engine(database_url)
    schema = inspect(engine)
    assert set(schema.get_table_names()) == CONTRACT_TABLES | {"alembic_version"}

    for table_name, table in Base.metadata.tables.items():
        migrated_columns = {column["name"] for column in schema.get_columns(table_name)}
        assert migrated_columns == set(table.columns.keys())
        migrated_indexes = {index["name"] for index in schema.get_indexes(table_name)}
        assert {index.name for index in table.indexes}.issubset(migrated_indexes)

    engine.dispose()
    command.downgrade(config, "base")
    engine = create_engine(database_url)
    assert not CONTRACT_TABLES.intersection(inspect(engine).get_table_names())
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(database_url)
    assert set(inspect(engine).get_table_names()) == CONTRACT_TABLES | {"alembic_version"}
    engine.dispose()


def test_quota_overrun_migration_preserves_existing_events(tmp_path: Path) -> None:
    database_path = tmp_path / "migration-existing-event.sqlite3"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = migration_config(database_url)
    command.upgrade(config, "0001")

    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO api_call_event (
                    id, server_request_id, client_request_id, endpoint, method,
                    stream, outcome, stage, http_status, business_code, retryable,
                    duration_ms, prompt_tokens, completion_tokens, total_tokens,
                    charged_micro, qps_observed, concurrency_observed,
                    daily_requests_after, daily_tokens_after, total_requests_after,
                    total_tokens_after, started_at, completed_at, created_at
                ) VALUES (
                    'event-before-0002', 'server-before-0002', 'client-before-0002',
                    'chat.completions', 'POST', false, 'success', 'response', 200,
                    'OK', false, 7, 2, 3, 5, 0, 1, 1, 1, 5, 1, 5,
                    1785420000, 1785420001, 1785420001
                )
                """
            )
        )
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(database_url)
    with engine.connect() as connection:
        quota_overrun = connection.scalar(
            text("SELECT quota_overrun FROM api_call_event WHERE id = 'event-before-0002'")
        )
    column = next(item for item in inspect(engine).get_columns("api_call_event") if item["name"] == "quota_overrun")
    assert quota_overrun in (False, 0)
    assert column["nullable"] is False
    assert column["default"] is None
    engine.dispose()


def test_stream_evidence_migration_preserves_existing_operations(tmp_path: Path) -> None:
    database_path = tmp_path / "migration-existing-operation.sqlite3"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = migration_config(database_url)
    command.upgrade(config, "0002")

    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO api_call_operation (
                    id, api_client_id, credential_id, client_request_id,
                    request_fingerprint, endpoint, method, model_id, status,
                    reserved_tokens, prompt_tokens, completion_tokens, total_tokens,
                    started_at, updated_at
                ) VALUES (
                    'operation-before-0003', 'client-before-0003', 'credential-before-0003',
                    'request_before_0003',
                    'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                    'chat.completions', 'POST', 'model-1', 'pending',
                    32, 0, 0, 0, 1785420000, 1785420000
                )
                """
            )
        )
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(database_url)
    with engine.connect() as connection:
        evidence = connection.execute(
            text(
                "SELECT stream, provider_usage_recorded FROM api_call_operation "
                "WHERE id = 'operation-before-0003'"
            )
        ).one()
    columns = {item["name"]: item for item in inspect(engine).get_columns("api_call_operation")}
    assert tuple(evidence) in ((False, False), (0, 0))
    for name in ("stream", "provider_usage_recorded"):
        assert columns[name]["nullable"] is False
        assert columns[name]["default"] is None
    engine.dispose()


def test_retention_indexes_are_revision_0004_and_round_trip(tmp_path: Path) -> None:
    database_path = tmp_path / "migration-retention-indexes.sqlite3"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = migration_config(database_url)
    script = ScriptDirectory.from_config(config)

    assert script.get_current_head() == "0004"
    command.upgrade(config, "head")
    engine = create_engine(database_url)
    schema = inspect(engine)
    assert "ix_api_call_event_created" in {item["name"] for item in schema.get_indexes("api_call_event")}
    assert "ix_api_client_admin_audit_created" in {
        item["name"] for item in schema.get_indexes("api_client_admin_audit")
    }
    engine.dispose()

    command.downgrade(config, "0003")
    engine = create_engine(database_url)
    schema = inspect(engine)
    assert "ix_api_call_event_created" not in {item["name"] for item in schema.get_indexes("api_call_event")}
    assert "ix_api_client_admin_audit_created" not in {
        item["name"] for item in schema.get_indexes("api_client_admin_audit")
    }
    engine.dispose()
