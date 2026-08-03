import pytest
from pydantic import ValidationError

from backend.app.api.external_models import (
    ChatCompletionRequest,
    ExternalPayloadError,
    estimate_prompt_tokens,
)


def valid_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "model": "tibetan-med",
        "messages": [
            {"role": "system", "content": "Answer directly."},
            {"role": "user", "content": "hello"},
        ],
        "stream": False,
        "max_tokens": 64,
    }
    payload.update(overrides)
    return payload


def test_chat_payload_is_a_bounded_openai_compatible_subset() -> None:
    request = ChatCompletionRequest.model_validate(valid_payload(temperature=0.2, top_p=0.9, stop=["END"]))

    assert request.admitted_output_tokens(client_limit=128, global_limit=256) == 64
    assert request.bifrost_payload() == valid_payload(temperature=0.2, top_p=0.9, stop=["END"])
    assert estimate_prompt_tokens(request) >= sum(len(message.content.encode("utf-8")) for message in request.messages)


@pytest.mark.parametrize(
    "payload",
    [
        valid_payload(tools=[]),
        valid_payload(messages=[{"role": "tool", "content": "unsafe"}]),
        valid_payload(messages=[{"role": "user", "content": "x"}] * 101),
        valid_payload(max_tokens=64, max_completion_tokens=64),
        valid_payload(stop=["a", "b", "c", "d", "e"]),
    ],
)
def test_chat_payload_rejects_unsupported_or_unbounded_fields(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ChatCompletionRequest.model_validate(payload)


def test_streaming_and_output_limit_fail_before_upstream_forwarding() -> None:
    streaming = ChatCompletionRequest.model_validate(valid_payload(stream=True))
    with pytest.raises(ExternalPayloadError) as unsupported:
        streaming.require_non_streaming()
    assert unsupported.value.code == "UNSUPPORTED_FEATURE"

    oversized = ChatCompletionRequest.model_validate(valid_payload(max_tokens=129))
    with pytest.raises(ExternalPayloadError) as limited:
        oversized.admitted_output_tokens(client_limit=128, global_limit=256)
    assert limited.value.code == "INVALID_REQUEST"

    defaulted = ChatCompletionRequest.model_validate(valid_payload(max_tokens=None))
    assert defaulted.admitted_output_tokens(client_limit=128, global_limit=64) == 64
