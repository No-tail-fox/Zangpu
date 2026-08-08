import json
from collections.abc import Iterator

import httpx
import pytest
from zangpu_sdk import (
    MAX_JSON_RESPONSE_BYTES,
    ZangpuAPIError,
    ZangpuClient,
    ZangpuProtocolError,
)

from backend.app.security.canonical import (
    body_sha256_hex,
    create_canonical_request,
    verify_signature,
)

SIGNING_VALUE = "zps_sdk_test_material_0123456789"
KEY_ID = "zpk_sdk_0123456789"


def assert_signed(request: httpx.Request, seen_nonces: list[str]) -> None:
    headers = request.headers
    raw_path = request.url.raw_path.decode("ascii")
    path, _, query = raw_path.partition("?")
    canonical = create_canonical_request(
        method=request.method,
        raw_path=path,
        raw_query=query,
        body_hash=body_sha256_hex(request.content),
        key_id=headers["x-zangpu-key"],
        timestamp=headers["x-zangpu-timestamp"],
        nonce=headers["x-zangpu-nonce"],
        request_id=headers["x-zangpu-request-id"],
    )
    assert verify_signature(SIGNING_VALUE, canonical, headers["x-zangpu-signature"])
    seen_nonces.append(headers["x-zangpu-nonce"])


def response_headers() -> dict[str, str]:
    return {
        "content-type": "application/json",
        "x-zangpu-request-id": "req_server_sdk_0123456789",
        "x-ratelimit-limit": "10",
        "x-ratelimit-remaining": "9",
        "x-ratelimit-reset": "1785420001",
    }


def build_client(handler: httpx.MockTransport) -> ZangpuClient:
    return ZangpuClient(
        base_url="http://127.0.0.1:9000",
        key_id=KEY_ID,
        secret=SIGNING_VALUE,  # noqa: S106
        transport=handler,
        clock=lambda: 1_785_420_000,
    )


def test_sdk_calls_models_usage_and_json_chat_with_fresh_nonces() -> None:
    requests: list[httpx.Request] = []
    nonces: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert_signed(request, nonces)
        if request.url.path.endswith("/models"):
            payload = {"object": "list", "data": [{"id": "model-1", "object": "model"}]}
        elif request.url.path.endswith("/usage"):
            payload = {"object": "usage", "as_of": 1_785_420_000, "daily": {}, "lifetime": {}}
        else:
            body = json.loads(request.content)
            assert body == {
                "max_tokens": 64,
                "messages": [{"content": "hello", "role": "user"}],
                "model": "model-1",
                "stream": False,
            }
            payload = {
                "id": "chatcmpl-sdk-1",
                "model": "model-1",
                "choices": [],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
            }
        return httpx.Response(200, headers=response_headers(), json=payload)

    with build_client(httpx.MockTransport(handler)) as client:
        models = client.list_models()
        usage = client.get_usage()
        chat = client.chat_completions(
            model="model-1",
            messages=[{"role": "user", "content": "hello"}],
            max_tokens=64,
            request_id="req_sdk_chat_0123456789",
        )

    assert models.data["data"][0]["id"] == "model-1"
    assert usage.data["object"] == "usage"
    assert chat.data["usage"]["total_tokens"] == 8
    assert chat.request_id == "req_server_sdk_0123456789"
    assert chat.rate_limit is not None and chat.rate_limit.remaining == 9
    assert [request.method for request in requests] == ["GET", "GET", "POST"]
    assert len(set(nonces)) == 3


def test_sdk_streams_heartbeat_json_events_and_requires_done() -> None:
    nonces: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert_signed(request, nonces)
        payload = json.loads(request.content)
        assert payload["stream"] is True
        return httpx.Response(
            200,
            headers={
                "content-type": "text/event-stream; charset=utf-8",
                "x-zangpu-request-id": "req_server_stream_0123456789",
            },
            content=(
                b": heartbeat\n\n"
                b'data: {"id":"chunk-1","choices":[{"delta":{"content":"answer"}}]}\n\n'
                b'data: {"id":"chunk-1","choices":[],"usage":{"total_tokens":8}}\n\n'
                b"data: [DONE]\n\n"
            ),
        )

    with build_client(httpx.MockTransport(handler)) as client:
        events = list(
            client.stream_chat_completions(
                model="model-1",
                messages=[{"role": "user", "content": "hello"}],
                max_tokens=64,
            )
        )

    assert [event["id"] for event in events] == ["chunk-1", "chunk-1"]
    assert events[-1]["usage"]["total_tokens"] == 8
    assert events[0].request_id == "req_server_stream_0123456789"
    assert len(nonces) == 1


@pytest.mark.parametrize(
    ("status", "headers", "content", "error_type"),
    [
        (
            503,
            {"content-type": "application/json", "x-zangpu-request-id": "req_server_error_0123456789"},
            json.dumps(
                {
                    "error": {
                        "code": "MODEL_UNAVAILABLE",
                        "message": "Model is temporarily unavailable.",
                        "request_id": "req_server_error_0123456789",
                        "retryable": True,
                        "raw_detail": "private upstream detail",
                    }
                }
            ).encode(),
            ZangpuAPIError,
        ),
        (
            200,
            {"content-type": "text/html", "x-zangpu-request-id": "req_server_error_0123456789"},
            b"<html>private upstream detail</html>",
            ZangpuProtocolError,
        ),
        (
            200,
            {"content-type": "application/json", "x-zangpu-request-id": "req_server_error_0123456789"},
            b"x" * (MAX_JSON_RESPONSE_BYTES + 1),
            ZangpuProtocolError,
        ),
    ],
    ids=("api-error", "wrong-content-type", "oversized-json"),
)
def test_sdk_errors_are_bounded_and_do_not_expose_raw_bodies(
    status: int,
    headers: dict[str, str],
    content: bytes,
    error_type: type[Exception],
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers=headers, content=content)

    with build_client(httpx.MockTransport(handler)) as client, pytest.raises(error_type) as captured:
        client.list_models()

    assert "private upstream detail" not in repr(captured.value)
    assert SIGNING_VALUE not in repr(captured.value)


def test_sdk_stream_rejects_sanitized_error_and_missing_done() -> None:
    responses: Iterator[bytes] = iter(
        [
            (
                b'data: {"error":{"code":"MODEL_UNAVAILABLE","message":"Model unavailable.",'
                b'"request_id":"req_server_stream_error_01","retryable":true}}\n\n'
            ),
            b'data: {"id":"chunk-without-done","choices":[]}\n\n',
        ]
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "content-type": "text/event-stream",
                "x-zangpu-request-id": "req_server_stream_error_01",
            },
            content=next(responses),
        )

    with build_client(httpx.MockTransport(handler)) as client:
        with pytest.raises(ZangpuAPIError):
            list(client.stream_chat_completions(model="model-1", messages=[{"role": "user", "content": "x"}]))
        with pytest.raises(ZangpuProtocolError):
            list(client.stream_chat_completions(model="model-1", messages=[{"role": "user", "content": "x"}]))


def test_sdk_rejects_credentialed_origins_and_public_plain_http() -> None:
    with pytest.raises(ValueError):
        ZangpuClient(base_url="https://user@example.com", key_id=KEY_ID, secret=SIGNING_VALUE)  # noqa: S106
    with pytest.raises(ValueError):
        ZangpuClient(base_url="http://api.example.com", key_id=KEY_ID, secret=SIGNING_VALUE)  # noqa: S106
