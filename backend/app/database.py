from dataclasses import dataclass

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker


@dataclass(slots=True, repr=False)
class DatabaseRuntime:
    engine: Engine
    sessions: sessionmaker[Session]

    def __repr__(self) -> str:
        return "DatabaseRuntime(engine=<redacted>, sessions=<configured>)"

    def close(self) -> None:
        self.engine.dispose()


def create_database_runtime(database_url: str) -> DatabaseRuntime:
    if not database_url or len(database_url) > 8_192:
        raise ValueError("database URL is invalid")
    engine = create_engine(
        database_url,
        pool_pre_ping=True,
        hide_parameters=True,
    )
    return DatabaseRuntime(
        engine=engine,
        sessions=sessionmaker(engine, expire_on_commit=False),
    )
