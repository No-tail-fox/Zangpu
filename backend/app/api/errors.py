from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from fastapi.responses import JSONResponse


@dataclass(frozen=True, slots=True)
class ExternalErrorSpec:
    status_code: int
    message: str
    retryable: bool


ERROR_SPECS = {
    "INVALID_REQUEST": ExternalErrorSpec(400, "Invalid request.", False),
    "UNSUPPORTED_FEATURE": ExternalErrorSpec(400, "Unsupported feature.", False),
    "AUTH_FAILED": ExternalErrorSpec(401, "Authentication failed.", False),
    "CREDIT_BALANCE_EXHAUSTED": ExternalErrorSpec(402, "Credit balance is exhausted.", False),
    "CLIENT_DISABLED": ExternalErrorSpec(403, "Client is disabled.", False),
    "ENDPOINT_FORBIDDEN": ExternalErrorSpec(403, "Endpoint is not allowed.", False),
    "MODEL_FORBIDDEN": ExternalErrorSpec(403, "Model is not allowed.", False),
    "CREDIT_ACCOUNT_FROZEN": ExternalErrorSpec(403, "Credit account is frozen.", False),
    "MODEL_NOT_FOUND": ExternalErrorSpec(404, "Model was not found.", False),
    "REQUEST_IN_PROGRESS": ExternalErrorSpec(409, "Request is already in progress.", True),
    "REQUEST_ALREADY_COMPLETED": ExternalErrorSpec(409, "Request was already completed.", False),
    "REQUEST_ID_CONFLICT": ExternalErrorSpec(409, "Request ID conflicts with another request.", False),
    "PAYLOAD_TOO_LARGE": ExternalErrorSpec(413, "Request payload is too large.", False),
    "CLIENT_DISCONNECTED": ExternalErrorSpec(499, "Client disconnected.", False),
    "QPS_LIMITED": ExternalErrorSpec(429, "Request rate limit exceeded.", True),
    "CONCURRENCY_LIMITED": ExternalErrorSpec(429, "Concurrency limit exceeded.", True),
    "DAILY_REQUEST_QUOTA_EXCEEDED": ExternalErrorSpec(429, "Daily request quota exceeded.", False),
    "DAILY_TOKEN_QUOTA_EXCEEDED": ExternalErrorSpec(429, "Daily Token quota exceeded.", False),
    "TOTAL_REQUEST_QUOTA_EXCEEDED": ExternalErrorSpec(429, "Lifetime request quota exceeded.", False),
    "TOTAL_TOKEN_QUOTA_EXCEEDED": ExternalErrorSpec(429, "Lifetime Token quota exceeded.", False),
    "CONTROL_PLANE_UNAVAILABLE": ExternalErrorSpec(503, "Control plane is temporarily unavailable.", True),
    "MODEL_UNAVAILABLE": ExternalErrorSpec(503, "Model is temporarily unavailable.", True),
    "MODEL_TIMEOUT": ExternalErrorSpec(504, "Model request timed out.", True),
    "INTERNAL_ERROR": ExternalErrorSpec(500, "Internal server error.", False),
}


class ExternalApiError(RuntimeError):
    __slots__ = ("code", "headers", "operation_id")

    def __init__(
        self,
        code: str,
        *,
        operation_id: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        if code not in ERROR_SPECS:
            raise ValueError("unknown external API error code")
        self.code = code
        self.operation_id = operation_id
        self.headers = dict(headers or {})
        super().__init__("external API request failed")

    def to_response(self, server_request_id: str) -> JSONResponse:
        return external_error_response(
            self.code,
            server_request_id=server_request_id,
            operation_id=self.operation_id,
            headers=self.headers,
        )


def external_error_response(
    code: str,
    *,
    server_request_id: str,
    operation_id: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    spec = ERROR_SPECS.get(code)
    if spec is None:
        raise ValueError("unknown external API error code")
    error: dict[str, str | bool] = {
        "code": code,
        "message": spec.message,
        "request_id": server_request_id,
        "retryable": spec.retryable,
    }
    if operation_id is not None:
        error["operation_id"] = operation_id
    response_headers = {
        "Cache-Control": "no-store",
        "X-Zangpu-Request-Id": server_request_id,
        **dict(headers or {}),
    }
    return JSONResponse(
        status_code=spec.status_code,
        content={"error": error},
        headers=response_headers,
    )
