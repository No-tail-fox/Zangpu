from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    field_validator,
    model_validator,
)

MAX_MESSAGES = 100
MAX_MESSAGE_CONTENT_LENGTH = 65_536
MAX_STOP_SEQUENCES = 4
MAX_STOP_SEQUENCE_LENGTH = 256

SafeModelId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=255,
        pattern=r"^[^\x00-\x1f\x7f]+$",
    ),
]


class ExternalPayloadError(ValueError):
    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("external chat payload is not admitted")


class ChatMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=MAX_MESSAGE_CONTENT_LENGTH)


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model: SafeModelId
    messages: list[ChatMessageRequest] = Field(min_length=1, max_length=MAX_MESSAGES)
    stream: StrictBool = False
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    top_p: float | None = Field(default=None, gt=0.0, le=1.0)
    max_tokens: StrictInt | None = Field(default=None, gt=0, le=2**31 - 1)
    max_completion_tokens: StrictInt | None = Field(default=None, gt=0, le=2**31 - 1)
    stop: str | list[str] | None = None

    @field_validator("temperature", "top_p", mode="before")
    @classmethod
    def reject_boolean_sampling_values(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("sampling values must be numeric")
        return value

    @field_validator("stop")
    @classmethod
    def validate_stop(cls, value: str | list[str] | None) -> str | list[str] | None:
        if value is None:
            return None
        values = [value] if isinstance(value, str) else value
        if (
            not values
            or len(values) > MAX_STOP_SEQUENCES
            or any(not item or len(item) > MAX_STOP_SEQUENCE_LENGTH for item in values)
            or len(set(values)) != len(values)
        ):
            raise ValueError("stop sequences are invalid")
        return value

    @model_validator(mode="after")
    def validate_output_token_field(self) -> Self:
        if self.max_tokens is not None and self.max_completion_tokens is not None:
            raise ValueError("max_tokens and max_completion_tokens are mutually exclusive")
        return self

    def require_non_streaming(self) -> None:
        if self.stream:
            raise ExternalPayloadError("UNSUPPORTED_FEATURE")

    def admitted_output_tokens(self, *, client_limit: int, global_limit: int) -> int:
        if client_limit <= 0 or global_limit <= 0:
            raise ValueError("output token limits must be positive")
        requested = self.max_tokens or self.max_completion_tokens
        admitted_limit = min(client_limit, global_limit)
        if requested is None:
            return admitted_limit
        if requested > admitted_limit:
            raise ExternalPayloadError("INVALID_REQUEST")
        return requested

    def bifrost_payload(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude_none=True)


def estimate_prompt_tokens(request: ChatCompletionRequest) -> int:
    return sum(
        len(message.role.encode("utf-8")) + len(message.content.encode("utf-8")) + 8 for message in request.messages
    )
