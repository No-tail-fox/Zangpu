from functools import lru_cache
from typing import Annotated, Literal

from pydantic import AnyHttpUrl, Field, PostgresDsn, RedisDsn, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

BoundedSecret = Annotated[SecretStr, Field(min_length=32, max_length=4096)]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ZANGPU_",
        case_sensitive=False,
        extra="ignore",
        str_strip_whitespace=True,
    )

    environment: Literal["development", "test", "staging", "production"]
    service_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
    database_url: PostgresDsn
    redis_url: RedisDsn
    bifrost_base_url: AnyHttpUrl
    openwebui_internal_base_url: AnyHttpUrl
    admin_session_secret: BoundedSecret
    credential_keyring: BoundedSecret


@lru_cache(maxsize=1)
def load_settings() -> Settings:
    return Settings()
