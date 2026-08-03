from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.api.errors import ExternalApiError
from backend.app.api.external import MAX_EXTERNAL_BODY_BYTES, router
from backend.app.integrations.bifrost.models import (
    ChatChoice,
    ChatCompletionResponse,
    ChatMessage,
    ChatUsage,
)
from backend.app.security.dependencies import AuthenticatedCaller
from backend.app.services.chat import ExternalChatResult


class FakeAuthenticator:
    async def authenticate(self, **_kwargs: object) -> AuthenticatedCaller:
        return AuthenticatedCaller(
            api_client_id="client-1",
            credential_id="credential-1",
            key_id="zpk_route_0123456789",
            request_id="req_route_0123456789",
            nonce="nonce_route_0123456789",
            timestamp=1_785_420_100,
        )


class FakeChatService:
    def __init__(self, *, error_code: str | None = None) -> None:
        self.error_code = error_code
        self.calls = 0

    async def execute(self, **_kwargs: object) -> ExternalChatResult:
        self.calls += 1
        if self.error_code is not None:
            raise ExternalApiError(self.error_code, operation_id="operation-1")
        return ExternalChatResult(
            response=ChatCompletionResponse(
                id="chatcmpl-route-1",
                model="model-1",
                choices=[
                    ChatChoice(
                        index=0,
                        message=ChatMessage(role="assistant", content="answer"),
                        finish_reason="stop",
                    )
                ],
                usage=ChatUsage(prompt_tokens=5, completion_tokens=3, total_tokens=8),
            ),
            rate_limit_headers={
                "X-RateLimit-Limit": "10",
                "X-RateLimit-Remaining": "9",
                "X-RateLimit-Reset": "1785420101",
            },
        )


def build_app(service: FakeChatService) -> FastAPI:
    app = FastAPI()
    app.state.external_authenticator = FakeAuthenticator()
    app.state.external_chat_service = service
    app.include_router(router)
    return app


def valid_payload() -> dict[str, object]:
    return {
        "model": "model-1",
        "messages": [{"role": "user", "content": "hello"}],
        "stream": False,
        "max_tokens": 32,
    }


def test_external_chat_route_returns_openai_body_and_contract_headers() -> None:
    service = FakeChatService()
    with TestClient(build_app(service)) as client:
        response = client.post("/api/v1/external/chat/completions", json=valid_payload())

    assert response.status_code == 200
    assert response.json()["usage"] == {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}
    assert response.headers["x-zangpu-request-id"].startswith("req_")
    assert response.headers["x-ratelimit-remaining"] == "9"
    assert response.headers["cache-control"] == "no-store"
    assert service.calls == 1


def test_external_chat_route_rejects_invalid_or_oversized_body_before_service() -> None:
    service = FakeChatService()
    with TestClient(build_app(service)) as client:
        invalid = client.post(
            "/api/v1/external/chat/completions",
            content=b'{"model":',
            headers={"content-type": "application/json"},
        )
        oversized = client.post(
            "/api/v1/external/chat/completions",
            content=b"x" * (MAX_EXTERNAL_BODY_BYTES + 1),
            headers={"content-type": "application/json"},
        )

    assert (invalid.status_code, invalid.json()["error"]["code"]) == (400, "INVALID_REQUEST")
    assert (oversized.status_code, oversized.json()["error"]["code"]) == (413, "PAYLOAD_TOO_LARGE")
    assert service.calls == 0


def test_external_chat_route_normalizes_service_failures() -> None:
    service = FakeChatService(error_code="REQUEST_ALREADY_COMPLETED")
    with TestClient(build_app(service)) as client:
        response = client.post("/api/v1/external/chat/completions", json=valid_payload())

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "REQUEST_ALREADY_COMPLETED"
    assert response.json()["error"]["operation_id"] == "operation-1"
    assert response.headers["x-zangpu-request-id"].startswith("req_")
