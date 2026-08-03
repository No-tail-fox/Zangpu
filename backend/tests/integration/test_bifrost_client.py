import asyncio
import json

import httpx
import pytest
from pydantic import SecretStr

from backend.app.integrations.bifrost.client import BifrostClient
from backend.app.integrations.bifrost.models import (
    BifrostProtocolError,
    BifrostUpstreamError,
    VirtualKeySpec,
)

MANAGEMENT_TOKEN = "management-token-redaction-sentinel-value"  # noqa: S105 - non-secret redaction sentinel
VIRTUAL_KEY_VALUE = "sk-bf-virtual-key-redaction-sentinel"


def virtual_key_payload(*, active: bool = True, name: str = "zangpu-client-1") -> dict[str, object]:
    return {
        "id": "vk-1",
        "name": name,
        "value": VIRTUAL_KEY_VALUE,
        "description": "managed by Zangpu",
        "is_active": active,
        "provider_configs": [
            {
                "provider": "provider-1",
                "allowed_models": ["model-1"],
                "blacklisted_models": [],
                "key_ids": [],
                "weight": 1,
            }
        ],
        "mcp_configs": [],
        "calendar_aligned": False,
        "config_hash": "vendor-config-hash",
        "created_at": "2026-07-31T00:00:00Z",
        "updated_at": "2026-07-31T00:00:00Z",
    }


def test_virtual_key_lifecycle_is_typed_and_secret_safe() -> None:
    seen: list[tuple[str, str, dict[str, str], object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        seen.append((request.method, request.url.path, dict(request.headers), body))
        assert request.headers.get("authorization") == f"Bearer {MANAGEMENT_TOKEN}"
        assert "x-bf-vk" not in request.headers
        if request.method == "POST":
            assert body == {
                "name": "zangpu-client-1",
                "description": "managed by Zangpu",
                "provider_configs": [
                    {
                        "provider": "provider-1",
                        "weight": 1.0,
                        "allowed_models": ["model-1"],
                        "blacklisted_models": [],
                        "key_ids": [],
                    }
                ],
                "mcp_configs": [],
                "budgets": [],
                "is_active": True,
                "calendar_aligned": False,
            }
            return httpx.Response(200, json={"message": "created", "virtual_key": virtual_key_payload()})
        if request.method == "GET":
            return httpx.Response(200, json={"virtual_key": virtual_key_payload()})
        assert request.method == "PUT"
        active = bool(body.get("is_active", True))
        return httpx.Response(
            200,
            json={"message": "updated", "virtual_key": virtual_key_payload(active=active)},
        )

    async def scenario() -> tuple[object, object, object, object]:
        client = BifrostClient(
            base_url="http://bifrost:8080",
            management_token=SecretStr(MANAGEMENT_TOKEN),
            transport=httpx.MockTransport(handler),
        )
        spec = VirtualKeySpec(
            name="zangpu-client-1",
            description="managed by Zangpu",
            provider="provider-1",
            model="model-1",
        )
        created = await client.create_virtual_key(spec)
        read = await client.get_virtual_key("vk-1")
        updated = await client.update_virtual_key("vk-1", spec)
        disabled = await client.disable_virtual_key("vk-1")
        await client.aclose()
        return created, read, updated, disabled

    created, read, updated, disabled = asyncio.run(scenario())
    rendered = repr((created, read, updated, disabled, seen))

    assert created.state.id == "vk-1"
    assert created.take_value().get_secret_value() == VIRTUAL_KEY_VALUE
    assert read.model == "model-1" and updated.provider == "provider-1"
    assert disabled.is_active is False
    assert VIRTUAL_KEY_VALUE not in repr((created, read, updated, disabled))
    assert MANAGEMENT_TOKEN not in repr(created)
    assert [item[:2] for item in seen] == [
        ("POST", "/api/governance/virtual-keys"),
        ("GET", "/api/governance/virtual-keys/vk-1"),
        ("PUT", "/api/governance/virtual-keys/vk-1"),
        ("PUT", "/api/governance/virtual-keys/vk-1"),
    ]
    assert VIRTUAL_KEY_VALUE not in rendered


def test_spa_html_200_is_rejected_as_a_protocol_failure() -> None:
    async def scenario() -> None:
        client = BifrostClient(
            base_url="http://bifrost:8080",
            management_token=SecretStr(MANAGEMENT_TOKEN),
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, headers={"content-type": "text/html"}, text="<html>SPA</html>")
            ),
        )
        with pytest.raises(BifrostProtocolError, match="invalid response contract") as captured:
            await client.get_version()
        await client.aclose()
        assert "SPA" not in str(captured.value)

    asyncio.run(scenario())


def test_virtual_key_ids_cannot_escape_the_management_route() -> None:
    async def scenario() -> None:
        client = BifrostClient(
            base_url="http://bifrost:8080",
            management_token=SecretStr(MANAGEMENT_TOKEN),
            transport=httpx.MockTransport(lambda _request: pytest.fail("unsafe ID reached transport")),
        )
        with pytest.raises(ValueError, match="invalid virtual-key ID"):
            await client.get_virtual_key("../api/config")
        await client.aclose()

    asyncio.run(scenario())


def test_normal_and_sse_forwarding_keep_management_auth_out_and_map_errors() -> None:
    inference_headers: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        inference_headers.append(dict(request.headers))
        assert request.headers.get("x-bf-vk") == VIRTUAL_KEY_VALUE
        assert "authorization" not in request.headers
        payload = json.loads(request.content)
        if payload.get("model") == "rate-limited":
            return httpx.Response(429, json={"error": {"type": "request_limited", "message": "raw vendor text"}})
        if payload.get("stream"):
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n',
            )
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "object": "chat.completion",
                "created": 1,
                "model": "provider-1/model-1",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    async def scenario() -> tuple[object, bytes, BifrostUpstreamError]:
        client = BifrostClient(
            base_url="http://bifrost:8080",
            management_token=SecretStr(MANAGEMENT_TOKEN),
            transport=httpx.MockTransport(handler),
        )
        result = await client.forward_chat_completion(
            {"model": "provider-1/model-1", "messages": [{"role": "user", "content": "not logged"}]},
            virtual_key=SecretStr(VIRTUAL_KEY_VALUE),
        )
        chunks = b"".join(
            [
                chunk
                async for chunk in client.stream_chat_completion(
                    {"model": "provider-1/model-1", "messages": [{"role": "user", "content": "not logged"}]},
                    virtual_key=SecretStr(VIRTUAL_KEY_VALUE),
                )
            ]
        )
        with pytest.raises(BifrostUpstreamError) as captured:
            await client.forward_chat_completion(
                {"model": "rate-limited", "messages": []},
                virtual_key=SecretStr(VIRTUAL_KEY_VALUE),
            )
        await client.aclose()
        return result, chunks, captured.value

    result, chunks, failure = asyncio.run(scenario())

    assert result.usage.total_tokens == 2
    assert b"[DONE]" in chunks
    assert (failure.code, failure.status_code, failure.retryable) == ("BIFROST_RATE_LIMITED", 429, True)
    assert "raw vendor text" not in str(failure)
    assert all(MANAGEMENT_TOKEN not in repr(headers) for headers in inference_headers)


def test_network_failure_drops_the_sensitive_http_exception_chain() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"wire contained {VIRTUAL_KEY_VALUE}", request=request)

    async def scenario() -> BifrostUpstreamError:
        client = BifrostClient(
            base_url="http://bifrost:8080",
            management_token=SecretStr(MANAGEMENT_TOKEN),
            transport=httpx.MockTransport(handler),
        )
        with pytest.raises(BifrostUpstreamError) as captured:
            await client.forward_chat_completion(
                {"model": "provider-1/model-1", "messages": []},
                virtual_key=SecretStr(VIRTUAL_KEY_VALUE),
            )
        await client.aclose()
        return captured.value

    failure = asyncio.run(scenario())
    assert failure.code == "BIFROST_UNAVAILABLE" and failure.__cause__ is None
    assert VIRTUAL_KEY_VALUE not in repr(failure)


def test_inference_timeout_has_a_distinct_sanitized_contract() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("raw timeout detail", request=request)

    async def scenario() -> BifrostUpstreamError:
        client = BifrostClient(
            base_url="http://bifrost:8080",
            management_token=SecretStr(MANAGEMENT_TOKEN),
            transport=httpx.MockTransport(handler),
        )
        with pytest.raises(BifrostUpstreamError) as captured:
            await client.forward_chat_completion(
                {"model": "model-1", "messages": []},
                virtual_key=SecretStr(VIRTUAL_KEY_VALUE),
            )
        await client.aclose()
        return captured.value

    failure = asyncio.run(scenario())

    assert (failure.code, failure.status_code, failure.retryable) == ("BIFROST_TIMEOUT", 504, True)
    assert failure.__cause__ is None
    assert "raw timeout detail" not in str(failure)
    assert VIRTUAL_KEY_VALUE not in repr(failure)
