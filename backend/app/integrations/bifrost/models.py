from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class BifrostUpstreamError(RuntimeError):
    def __init__(self, *, code: str, status_code: int, retryable: bool) -> None:
        self.code = code
        self.status_code = status_code
        self.retryable = retryable
        super().__init__(f"{code}: upstream request failed")


class BifrostProtocolError(BifrostUpstreamError):
    def __init__(self) -> None:
        super().__init__(code="BIFROST_PROTOCOL_ERROR", status_code=502, retryable=True)
        self.args = ("Bifrost returned an invalid response contract",)


class VirtualKeySpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=1024)
    provider: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=255)
    provider_key_ids: list[str] = Field(default_factory=list, max_length=64)

    @field_validator("provider_key_ids")
    @classmethod
    def validate_provider_key_ids(cls, values: list[str]) -> list[str]:
        if any(not value or len(value) > 128 for value in values) or len(set(values)) != len(values):
            raise ValueError("provider key IDs must be a bounded unique list")
        return sorted(values)

    def create_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "provider_configs": [
                {
                    "provider": self.provider,
                    "weight": 1.0,
                    "allowed_models": [self.model],
                    "blacklisted_models": [],
                    "key_ids": self.provider_key_ids,
                }
            ],
            "mcp_configs": [],
            "budgets": [],
            "is_active": True,
            "calendar_aligned": False,
        }


class VirtualKeyState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=1024)
    is_active: bool
    provider: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=255)
    config_hash: str = Field(max_length=256)


class VirtualKeyCreationResult:
    __slots__ = ("_value", "state")

    def __init__(self, *, state: VirtualKeyState, value: SecretStr) -> None:
        self.state = state
        self._value: SecretStr | None = value

    def __repr__(self) -> str:
        return f"VirtualKeyCreationResult(state={self.state!r}, value=<redacted>)"

    def take_value(self) -> SecretStr:
        if self._value is None:
            raise RuntimeError("Bifrost virtual key value has already been read")
        value = self._value
        self._value = None
        return value


class _ProviderConfigWire(BaseModel):
    model_config = ConfigDict(extra="ignore")

    provider: str
    allowed_models: list[str]


class VirtualKeyWire(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    value: SecretStr
    description: str = ""
    is_active: bool
    provider_configs: list[_ProviderConfigWire]
    config_hash: str

    def state(self) -> VirtualKeyState:
        if len(self.provider_configs) != 1 or len(self.provider_configs[0].allowed_models) != 1:
            raise BifrostProtocolError
        provider = self.provider_configs[0]
        return VirtualKeyState(
            id=self.id,
            name=self.name,
            description=self.description,
            is_active=self.is_active,
            provider=provider.provider,
            model=provider.allowed_models[0],
            config_hash=self.config_hash,
        )


class VirtualKeyEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore")

    virtual_key: VirtualKeyWire


class VirtualKeyListEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore")

    virtual_keys: list[VirtualKeyWire]
    count: int = Field(ge=0)
    total_count: int = Field(ge=0)
    limit: int = Field(ge=0)
    offset: int = Field(ge=0)


class BifrostClientConfig(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    enforce_auth_on_inference: bool
    allow_direct_keys: bool
    allow_per_request_raw_override: bool


class BifrostConfigState(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    client_config: BifrostClientConfig
    is_db_connected: bool


class BifrostProviderState(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    name: str = Field(min_length=1, max_length=128)
    store_raw_request_response: bool
    send_back_raw_request: bool
    send_back_raw_response: bool


class BifrostProviderList(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    providers: list[BifrostProviderState]
    total: int = Field(ge=0)


class BifrostModelState(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    name: str = Field(min_length=1, max_length=255)
    provider: str = Field(min_length=1, max_length=128)


class BifrostModelList(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    models: list[BifrostModelState]
    total: int = Field(ge=0)


class BifrostHealth(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    status: str


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    role: str
    content: str | None = None


class ChatChoice(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    index: int = Field(ge=0)
    message: ChatMessage
    finish_reason: str | None = None


class ChatUsage(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)


class ChatCompletionResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    id: str
    model: str
    choices: list[ChatChoice]
    usage: ChatUsage


class BifrostOutboxPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    config_hash: str = Field(min_length=1, max_length=128)
    binding_version: int = Field(gt=0)
    desired: VirtualKeySpec | None = None
