from functools import lru_cache
from typing import Annotated, Literal, Self

from pydantic import AnyHttpUrl, Field, PostgresDsn, RedisDsn, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BoundedSecret = Annotated[SecretStr, Field(min_length=32, max_length=4096)]
CredentialKeys = Annotated[SecretStr, Field(min_length=2, max_length=16384)]


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
    api_credential_keys: CredentialKeys
    api_credential_active_key_id: str = Field(pattern=r"^[A-Za-z0-9._-]{1,64}$")
    contract_api_timestamp_tolerance_seconds: int = Field(default=300, ge=30, le=900)
    contract_api_nonce_ttl_seconds: int = Field(default=600, ge=60, le=86_400)
    contract_api_concurrency_lease_seconds: int = Field(default=60, ge=3, le=900)
    contract_api_concurrency_heartbeat_seconds: int = Field(default=15, ge=1, le=300)

    @model_validator(mode="after")
    def validate_distributed_control_ttls(self) -> Self:
        if self.contract_api_nonce_ttl_seconds < self.contract_api_timestamp_tolerance_seconds * 2:
            raise ValueError("nonce TTL must be at least twice the timestamp tolerance")
        if self.contract_api_concurrency_heartbeat_seconds * 2 >= self.contract_api_concurrency_lease_seconds:
            raise ValueError("concurrency heartbeat must be less than half the lease")
        return self


@lru_cache(maxsize=1)
def load_settings() -> Settings:
    return Settings()
