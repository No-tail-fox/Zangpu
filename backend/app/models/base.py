from collections.abc import Collection
from time import time
from uuid import uuid4

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def new_uuid() -> str:
    return str(uuid4())


def utc_epoch_seconds() -> int:
    return int(time())


def validate_string_set(
    field_name: str,
    values: object,
    *,
    allowed: Collection[str] | None = None,
    max_items: int = 256,
    max_item_length: int = 255,
) -> list[str]:
    if not isinstance(values, list) or len(values) > max_items:
        raise ValueError(f"{field_name} must be a bounded list")
    if any(not isinstance(value, str) or not value or len(value) > max_item_length for value in values):
        raise ValueError(f"{field_name} contains an invalid value")
    if len(set(values)) != len(values):
        raise ValueError(f"{field_name} must not contain duplicates")
    if allowed is not None and not set(values).issubset(allowed):
        raise ValueError(f"{field_name} contains an unsupported value")
    return sorted(values)
