from functools import lru_cache
from typing import Annotated, Literal, Self

from pydantic import AnyHttpUrl, Field, PostgresDsn, RedisDsn, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from backend.app.limits.model_pool import ModelPoolPolicy

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
    bifrost_management_token: BoundedSecret
    bifrost_expected_version: str = Field(pattern=r"^v[0-9]+\.[0-9]+\.[0-9]+$")
    bifrost_timeout_seconds: float = Field(default=10.0, ge=1.0, le=30.0)
    openwebui_internal_base_url: AnyHttpUrl
    openwebui_internal_service_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9._-]+$",
    )
    openwebui_internal_service_secret: BoundedSecret
    openwebui_internal_timeout_seconds: float = Field(default=10.0, ge=1.0, le=30.0)
    admin_session_secret: BoundedSecret
    admin_login_token: BoundedSecret | None = None
    admin_session_ttl_seconds: int = Field(default=3600, ge=300, le=86_400)
    event_retention_days: int = Field(default=180, ge=30, le=3_650)
    admin_audit_retention_days: int = Field(default=730, ge=365, le=3_650)
    retention_batch_size: int = Field(default=1_000, ge=1, le=10_000)
    api_credential_keys: CredentialKeys
    api_credential_active_key_id: str = Field(pattern=r"^[A-Za-z0-9._-]{1,64}$")
    contract_api_timestamp_tolerance_seconds: int = Field(default=300, ge=30, le=900)
    contract_api_nonce_ttl_seconds: int = Field(default=600, ge=60, le=86_400)
    contract_api_concurrency_lease_seconds: int = Field(default=60, ge=3, le=900)
    contract_api_concurrency_heartbeat_seconds: int = Field(default=15, ge=1, le=300)
    contract_api_max_output_tokens: int = Field(default=4096, ge=1, le=1_000_000)
    model_pool_policies: dict[str, ModelPoolPolicy] = Field(default_factory=dict)
    contract_api_global_queue_limit: int = Field(default=200, ge=0, le=10_000)
    contract_api_caller_queue_limit: int = Field(default=8, ge=0, le=1_000)
    contract_api_queue_wait_seconds: int = Field(default=30, ge=1, le=300)
    contract_api_queue_poll_milliseconds: int = Field(default=250, ge=50, le=2_000)
    outbox_max_attempts: int = Field(default=8, ge=1, le=100)
    outbox_base_retry_seconds: int = Field(default=5, ge=1, le=300)
    outbox_max_retry_seconds: int = Field(default=300, ge=1, le=3600)
    outbox_claim_timeout_seconds: int = Field(default=120, ge=10, le=3600)
    outbox_batch_size: int = Field(default=25, ge=1, le=100)

    @field_validator("model_pool_policies")
    @classmethod
    def validate_model_pool_policies(
        cls, value: dict[str, ModelPoolPolicy]
    ) -> dict[str, ModelPoolPolicy]:
        if len(value) > 256:
            raise ValueError("model-pool policy count is out of bounds")
        pool_limits: dict[str, int] = {}
        for model_id, policy in value.items():
            if (
                not model_id
                or len(model_id) > 255
                or model_id != model_id.strip()
                or any(ord(character) < 32 or ord(character) == 127 for character in model_id)
            ):
                raise ValueError("model-pool policy model ID is invalid")
            previous_limit = pool_limits.setdefault(policy.pool_id, policy.active_limit)
            if previous_limit != policy.active_limit:
                raise ValueError("models sharing a pool must use the same active limit")
        return value

    @model_validator(mode="after")
    def validate_distributed_control_ttls(self) -> Self:
        if self.contract_api_nonce_ttl_seconds < self.contract_api_timestamp_tolerance_seconds * 2:
            raise ValueError("nonce TTL must be at least twice the timestamp tolerance")
        if self.contract_api_concurrency_heartbeat_seconds * 2 >= self.contract_api_concurrency_lease_seconds:
            raise ValueError("concurrency heartbeat must be less than half the lease")
        if self.contract_api_global_queue_limit > 0 and self.contract_api_caller_queue_limit == 0:
            raise ValueError("caller queue limit must be positive when queueing is enabled")
        if self.outbox_max_retry_seconds < self.outbox_base_retry_seconds:
            raise ValueError("outbox maximum retry delay must not be below its base delay")
        if self.admin_audit_retention_days < self.event_retention_days:
            raise ValueError("administrator audit retention must not be shorter than event retention")
        if self.environment == "production" and self.admin_login_token is None:
            raise ValueError("administrator login token is required in production")
        if self.environment == "production" and not self.model_pool_policies:
            raise ValueError("at least one model-pool policy is required in production")
        if (
            self.admin_login_token is not None
            and self.admin_login_token.get_secret_value() == self.admin_session_secret.get_secret_value()
        ):
            raise ValueError("administrator login and session Secrets must be different")
        return self


@lru_cache(maxsize=1)
def load_settings() -> Settings:
    return Settings()
