from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

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
