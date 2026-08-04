import pytest

from backend.app.integrations.bifrost.models import BifrostProtocolError, ChatUsage
from backend.app.services.streaming import OpenAIStreamDecoder


def test_incremental_sse_decoder_validates_frames_and_terminal_usage() -> None:
    decoder = OpenAIStreamDecoder()

    assert decoder.feed(b": upstream heartbeat\r\n\r\ndata: {\"id\":\"chatcmpl-1\",\r\n") == []
    frames = decoder.feed(
        b'data: "object":"chat.completion.chunk","model":"model-1","choices":'
        b'[{"index":0,"delta":{"content":"ok"}}]}\r\n\r\n'
        b'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","model":"model-1",'
        b'"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":3,"total_tokens":8}}\n\n'
        b"data: [DONE]\n\n"
    )

    assert len(frames) == 3
    assert frames[-1] == b"data: [DONE]\n\n"
    assert decoder.usage == ChatUsage(prompt_tokens=5, completion_tokens=3, total_tokens=8)
    decoder.finish()


@pytest.mark.parametrize(
    "payload",
    [
        b"data: [DONE]\n\n",
        (
            b'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","model":"model-1",'
            b'"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":3,"total_tokens":9}}\n\n'
        ),
        b'data: {"error":{"message":"raw upstream detail"}}\n\n',
    ],
)
def test_sse_decoder_fails_closed_without_exposing_raw_upstream_details(payload: bytes) -> None:
    decoder = OpenAIStreamDecoder()

    with pytest.raises(BifrostProtocolError) as captured:
        decoder.feed(payload)

    assert "raw upstream detail" not in str(captured.value)


def test_sse_decoder_requires_done_and_rejects_bytes_after_done() -> None:
    decoder = OpenAIStreamDecoder()
    decoder.feed(
        b'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","model":"model-1",'
        b'"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\n'
    )
    with pytest.raises(BifrostProtocolError):
        decoder.finish()

    complete = OpenAIStreamDecoder()
    complete.feed(
        b'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","model":"model-1",'
        b'"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\n'
        b"data: [DONE]\n\n"
    )
    with pytest.raises(BifrostProtocolError):
        complete.feed(b"data: unexpected\n\n")

    duplicate = OpenAIStreamDecoder()
    with pytest.raises(BifrostProtocolError):
        duplicate.feed(
            b'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","model":"model-1",'
            b'"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\n'
            b"data: [DONE]\n\n"
            b"data: [DONE]\n\n"
        )
