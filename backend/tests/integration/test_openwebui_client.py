import asyncio
import hashlib
import hmac
import json
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr

from backend.app.integrations.openwebui.client import MAX_RESPONSE_BYTES, OpenWebUIClient
from backend.app.integrations.openwebui.models import (
    OpenWebUIProtocolError,
    OpenWebUIUpstreamError,
)

SERVICE_ID = "zangpu-api-control-plane"
SERVICE_SECRET = "openwebui-internal-secret-redaction-sentinel"  # noqa: S105 - non-secret test sentinel
EXTERNAL_CLIENT_ID = UUID("10000000-0000-4000-8000-000000000001")
SERVICE_USER_ID = UUID("20000000-0000-4000-8000-000000000002")
OPERATION_ID = UUID("30000000-0000-4000-8000-000000000003")
SETTLEMENT_ID = UUID("40000000-0000-4000-8000-000000000004")
ACCOUNT_ID = UUID("50000000-0000-4000-8000-000000000005")
REFUND_LEDGER_ID = UUID("60000000-0000-4000-8000-000000000006")
FIXED_TIMESTAMP = 2_000_000_000


def account_payload(*, balance_micro: str = "10000000", version: int = 1) -> dict[str, object]:
    return {
        "account_id": str(ACCOUNT_ID),
        "service_user_id": str(SERVICE_USER_ID),
        "balance_micro": balance_micro,
        "status": "active",
        "version": version,
        "updated_at": FIXED_TIMESTAMP,
    }


def operation_payload(*, status: str, charged_micro: str, balance_after_micro: str | None) -> dict[str, object]:
    return {
        "operation_id": str(OPERATION_ID),
        "settlement_id": str(SETTLEMENT_ID),
        "service_user_id": str(SERVICE_USER_ID),
        "model_id": "tibetan-med",
        "provider": "bifrost",
        "status": status,
        "prompt_tokens": 10 if status != "pending" else 0,
        "completion_tokens": 5 if status != "pending" else 0,
        "total_tokens": 15 if status != "pending" else 0,
        "charged_micro": charged_micro,
        "balance_after_micro": balance_after_micro,
        "account_version_after": 2 if balance_after_micro is not None else None,
        "started_at": FIXED_TIMESTAMP,
        "completed_at": FIXED_TIMESTAMP if status != "pending" else None,
        "updated_at": FIXED_TIMESTAMP,
    }


def expected_signature(request: httpx.Request) -> str:
    timestamp = request.headers["x-zangpu-timestamp"]
    canonical = "\n".join(
        (
            "zangpu-internal-v1",
            SERVICE_ID,
            timestamp,
            request.method,
            request.url.raw_path.decode("ascii"),
            hashlib.sha256(request.content).hexdigest(),
        )
    )
    return hmac.new(
        SERVICE_SECRET.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def test_typed_credit_lifecycle_uses_only_internal_signed_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.append((request.url.path, payload))
        assert request.headers["x-zangpu-service-id"] == SERVICE_ID
        assert request.headers["x-zangpu-timestamp"] == str(FIXED_TIMESTAMP)
        assert hmac.compare_digest(request.headers["x-zangpu-signature"], expected_signature(request))
        assert "authorization" not in request.headers
        assert "x-bf-vk" not in request.headers
        assert "x-zangpu-nonce" not in request.headers
        assert SERVICE_SECRET.encode("utf-8") not in request.content

        if request.url.path.endswith("/service-users/resolve"):
            assert payload == {"external_client_id": str(EXTERNAL_CLIENT_ID)}
            return httpx.Response(
                200,
                json={
                    "external_client_id": str(EXTERNAL_CLIENT_ID),
                    "service_user_id": str(SERVICE_USER_ID),
                    "created": True,
                    "account": account_payload(balance_micro="0", version=0),
                },
            )
        if request.url.path.endswith("/operations/reserve"):
            assert payload == {
                "model_id": "tibetan-med",
                "operation_id": str(OPERATION_ID),
                "provider": "bifrost",
                "service_user_id": str(SERVICE_USER_ID),
            }
            return httpx.Response(
                200,
                json={
                    **operation_payload(
                        status="pending",
                        charged_micro="0",
                        balance_after_micro=None,
                    ),
                    "usage_operation_id": f"{OPERATION_ID}:usage",
                    "account": account_payload(),
                },
            )
        if request.url.path.endswith("/operations/settle"):
            assert payload["prompt_tokens"] == 10
            assert payload["completion_tokens"] == 5
            return httpx.Response(
                200,
                json={
                    **operation_payload(
                        status="succeeded_charged",
                        charged_micro="20",
                        balance_after_micro="9999980",
                    ),
                    "usage_operation_id": f"{OPERATION_ID}:usage",
                    "account": account_payload(balance_micro="9999980", version=2),
                },
            )
        if request.url.path.endswith("/operations/cancel"):
            return httpx.Response(
                200,
                json={
                    **operation_payload(
                        status="cancelled_charged",
                        charged_micro="0",
                        balance_after_micro="10000000",
                    ),
                    "account": account_payload(),
                },
            )
        if request.url.path.endswith("/operations/refund"):
            return httpx.Response(
                200,
                json={
                    "operation_id": str(OPERATION_ID),
                    "settlement_id": str(SETTLEMENT_ID),
                    "service_user_id": str(SERVICE_USER_ID),
                    "refund_ledger_id": str(REFUND_LEDGER_ID),
                    "refunded_micro": "20",
                    "refunded_at": FIXED_TIMESTAMP,
                    "account": account_payload(),
                },
            )
        assert request.url.path.endswith("/operations/status")
        return httpx.Response(
            200,
            json={
                **operation_payload(
                    status="succeeded_charged",
                    charged_micro="20",
                    balance_after_micro="9999980",
                ),
                "usage_operation_id": f"{OPERATION_ID}:usage",
                "refunded": True,
                "refund_ledger_id": str(REFUND_LEDGER_ID),
                "refunded_micro": "20",
                "account": account_payload(),
            },
        )

    async def scenario():
        client = OpenWebUIClient(
            base_url="http://openwebui:8080",
            service_id=SERVICE_ID,
            service_secret=SecretStr(SERVICE_SECRET),
            transport=httpx.MockTransport(handler),
        )
        resolved = await client.resolve_service_user(EXTERNAL_CLIENT_ID)
        reserved = await client.reserve_operation(
            operation_id=OPERATION_ID,
            service_user_id=SERVICE_USER_ID,
            model_id="tibetan-med",
            provider="bifrost",
        )
        settled = await client.settle_operation(
            operation_id=OPERATION_ID,
            service_user_id=SERVICE_USER_ID,
            prompt_tokens=10,
            completion_tokens=5,
        )
        cancelled = await client.cancel_operation(
            operation_id=OPERATION_ID,
            service_user_id=SERVICE_USER_ID,
        )
        refunded = await client.refund_operation(
            operation_id=OPERATION_ID,
            service_user_id=SERVICE_USER_ID,
        )
        snapshot = await client.get_operation_status(
            operation_id=OPERATION_ID,
            service_user_id=SERVICE_USER_ID,
        )
        rendered = repr(client)
        await client.aclose()
        return resolved, reserved, settled, cancelled, refunded, snapshot, rendered

    monkeypatch.setattr("backend.app.integrations.openwebui.client.time.time", lambda: FIXED_TIMESTAMP)
    resolved, reserved, settled, cancelled, refunded, snapshot, rendered = asyncio.run(scenario())

    assert resolved.account.balance_micro == 0 and resolved.created is True
    assert reserved.status == "pending" and settled.charged_micro == 20
    assert cancelled.status == "cancelled_charged" and cancelled.charged_micro == 0
    assert refunded.refund_ledger_id == REFUND_LEDGER_ID
    assert snapshot.refunded is True and snapshot.refunded_micro == 20
    assert SERVICE_SECRET not in rendered
    assert [path.rsplit("/", 1)[-1] for path, _payload in seen] == [
        "resolve",
        "reserve",
        "settle",
        "cancel",
        "refund",
        "status",
    ]


def test_error_and_protocol_failures_are_bounded_without_raw_upstream_text() -> None:
    raw_marker = "raw-openwebui-error-must-not-escape"

    def rejected(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            402,
            json={
                "detail": {
                    "code": "CREDIT_BALANCE_EXHAUSTED",
                    "message": raw_marker,
                }
            },
        )

    async def scenario():
        client = OpenWebUIClient(
            base_url="http://openwebui:8080",
            service_id=SERVICE_ID,
            service_secret=SecretStr(SERVICE_SECRET),
            transport=httpx.MockTransport(rejected),
        )
        with pytest.raises(OpenWebUIUpstreamError) as captured:
            await client.reserve_operation(
                operation_id=OPERATION_ID,
                service_user_id=SERVICE_USER_ID,
                model_id="tibetan-med",
                provider="bifrost",
            )
        await client.aclose()
        return captured.value

    failure = asyncio.run(scenario())
    assert (failure.code, failure.status_code, failure.retryable) == (
        "CREDIT_BALANCE_EXHAUSTED",
        402,
        False,
    )
    assert raw_marker not in str(failure)


def test_request_contract_rejects_non_integer_token_evidence_before_transport() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500, json={"unused": True})

    async def scenario() -> None:
        client = OpenWebUIClient(
            base_url="http://openwebui:8080",
            service_id=SERVICE_ID,
            service_secret=SecretStr(SERVICE_SECRET),
            transport=httpx.MockTransport(handler),
        )
        with pytest.raises(ValueError):
            await client.settle_operation(
                operation_id=OPERATION_ID,
                service_user_id=SERVICE_USER_ID,
                prompt_tokens=True,
                completion_tokens=0,
            )
        await client.aclose()

    asyncio.run(scenario())
    assert requests == []


def test_response_body_is_rejected_at_the_streaming_size_limit() -> None:
    async def scenario() -> OpenWebUIProtocolError:
        client = OpenWebUIClient(
            base_url="http://openwebui:8080",
            service_id=SERVICE_ID,
            service_secret=SecretStr(SERVICE_SECRET),
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200,
                    headers={"content-type": "application/json"},
                    content=b"[" + b" " * MAX_RESPONSE_BYTES,
                )
            ),
        )
        with pytest.raises(OpenWebUIProtocolError) as captured:
            await client.get_operation_status(
                operation_id=OPERATION_ID,
                service_user_id=SERVICE_USER_ID,
            )
        await client.aclose()
        return captured.value

    failure = asyncio.run(scenario())
    assert failure.code == "OPENWEBUI_PROTOCOL_ERROR"


@pytest.mark.parametrize(
    "base_url",
    [
        "http://user:password@openwebui:8080",
        "http://openwebui:8080/api",
        "http://openwebui:8080?secret=value",
    ],
)
def test_client_rejects_non_origin_base_urls_and_redacts_secret(base_url: str) -> None:
    with pytest.raises(ValueError, match="origin"):
        OpenWebUIClient(
            base_url=base_url,
            service_id=SERVICE_ID,
            service_secret=SecretStr(SERVICE_SECRET),
        )


def test_spa_html_and_sensitive_network_errors_become_safe_protocol_failures() -> None:
    async def html_scenario():
        client = OpenWebUIClient(
            base_url="http://openwebui:8080",
            service_id=SERVICE_ID,
            service_secret=SecretStr(SERVICE_SECRET),
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200,
                    headers={"content-type": "text/html"},
                    text="<html>SPA</html>",
                )
            ),
        )
        with pytest.raises(OpenWebUIProtocolError) as captured:
            await client.get_operation_status(
                operation_id=OPERATION_ID,
                service_user_id=SERVICE_USER_ID,
            )
        await client.aclose()
        return captured.value

    html_failure = asyncio.run(html_scenario())
    assert "SPA" not in str(html_failure)

    def network_failure(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"wire contained {SERVICE_SECRET}", request=request)

    async def network_scenario():
        client = OpenWebUIClient(
            base_url="http://openwebui:8080",
            service_id=SERVICE_ID,
            service_secret=SecretStr(SERVICE_SECRET),
            transport=httpx.MockTransport(network_failure),
        )
        with pytest.raises(OpenWebUIUpstreamError) as captured:
            await client.get_operation_status(
                operation_id=OPERATION_ID,
                service_user_id=SERVICE_USER_ID,
            )
        await client.aclose()
        return captured.value

    network_error = asyncio.run(network_scenario())
    assert network_error.code == "OPENWEBUI_UNAVAILABLE"
    assert network_error.__cause__ is None
    assert SERVICE_SECRET not in repr(network_error)
