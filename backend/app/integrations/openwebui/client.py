from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from typing import Any
from uuid import UUID

import httpx
from pydantic import BaseModel, SecretStr, ValidationError

from backend.app.integrations.openwebui.models import (
    CreditOperationSnapshot,
    CreditOperationStatus,
    CreditRefundSnapshot,
    OpenWebUIErrorEnvelope,
    OpenWebUIProtocolError,
    OpenWebUIUpstreamError,
    OperationActionRequest,
    OperationReserveRequest,
    OperationSettleRequest,
    ServiceUserResolution,
    ServiceUserResolveRequest,
)

MAX_RESPONSE_BYTES = 1024 * 1024
SIGNATURE_VERSION = "zangpu-internal-v1"
SERVICE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
KNOWN_ERROR_CODES = frozenset(
    {
        "CREDIT_ACCOUNTING_ERROR",
        "CREDIT_ACCOUNT_FROZEN",
        "CREDIT_ACCOUNT_NOT_FOUND",
        "CREDIT_ACCOUNT_STATE_CONFLICT",
        "CREDIT_BALANCE_EXHAUSTED",
        "CREDIT_BALANCE_OVERFLOW",
        "CREDIT_IDEMPOTENCY_CONFLICT",
        "CREDIT_PRICING_UNAVAILABLE",
        "CREDIT_SETTLEMENT_NOT_FOUND",
        "CREDIT_SETTLEMENT_STATE_CONFLICT",
        "CREDIT_TERMINAL_EVIDENCE_INVALID",
        "CREDIT_USAGE_EVIDENCE_INVALID",
        "CREDIT_USAGE_OPERATION_CONFLICT",
        "INTERNAL_AUTH_FAILED",
        "INTERNAL_NETWORK_FORBIDDEN",
        "INTERNAL_SERVICE_UNAVAILABLE",
        "INTERNAL_SERVICE_USER_CONFLICT",
        "INTERNAL_SERVICE_USER_FORBIDDEN",
    }
)


def _fallback_error(status_code: int) -> tuple[str, bool]:
    if status_code in {401, 403}:
        return "OPENWEBUI_ACCESS_REJECTED", False
    if status_code == 404:
        return "OPENWEBUI_CREDIT_NOT_FOUND", False
    if status_code == 409:
        return "OPENWEBUI_CREDIT_CONFLICT", False
    if status_code == 422:
        return "OPENWEBUI_REQUEST_REJECTED", False
    if status_code == 429:
        return "OPENWEBUI_RATE_LIMITED", True
    if status_code >= 500:
        return "OPENWEBUI_UNAVAILABLE", True
    return "OPENWEBUI_REQUEST_REJECTED", False


class OpenWebUIClient:
    __slots__ = ("_client", "_service_id", "_service_secret")

    def __init__(
        self,
        *,
        base_url: str,
        service_id: str,
        service_secret: SecretStr,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        parsed_base_url = httpx.URL(base_url)
        if (
            parsed_base_url.scheme not in {"http", "https"}
            or not parsed_base_url.host
            or parsed_base_url.username
            or parsed_base_url.password
            or parsed_base_url.query
            or parsed_base_url.fragment
            or parsed_base_url.path not in {"", "/"}
        ):
            raise ValueError("Open WebUI base URL must be an origin without credentials")
        if not SERVICE_ID_RE.fullmatch(service_id):
            raise ValueError("Open WebUI internal service ID is invalid")
        secret_size = len(service_secret.get_secret_value().encode("utf-8"))
        if not 32 <= secret_size <= 4096:
            raise ValueError("Open WebUI internal service Secret length is invalid")
        if isinstance(timeout_seconds, bool) or not 1.0 <= timeout_seconds <= 60.0:
            raise ValueError("Open WebUI timeout is out of bounds")

        self._service_id = service_id
        self._service_secret = service_secret
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, 5.0)),
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=16),
            follow_redirects=False,
            transport=transport,
        )

    def __repr__(self) -> str:
        return (
            f"OpenWebUIClient(base_url={str(self._client.base_url)!r}, "
            f"service_id={self._service_id!r}, service_secret=<redacted>)"
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _signed_headers(self, *, method: str, path: str, body: bytes) -> dict[str, str]:
        timestamp = str(int(time.time()))
        canonical = "\n".join(
            (
                SIGNATURE_VERSION,
                self._service_id,
                timestamp,
                method.upper(),
                path,
                hashlib.sha256(body).hexdigest(),
            )
        )
        signature = hmac.new(
            self._service_secret.get_secret_value().encode("utf-8"),
            canonical.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return {
            "content-type": "application/json",
            "x-zangpu-service-id": self._service_id,
            "x-zangpu-timestamp": timestamp,
            "x-zangpu-signature": signature,
        }

    async def _request(
        self,
        path: str,
        request_model: BaseModel,
        response_model: type[BaseModel],
    ) -> Any:
        body = json.dumps(
            request_model.model_dump(mode="json"),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        try:
            async with self._client.stream(
                "POST",
                path,
                content=body,
                headers=self._signed_headers(method="POST", path=path, body=body),
            ) as response:
                content_type = response.headers.get("content-type", "").partition(";")[0].strip().lower()
                if content_type != "application/json":
                    raise OpenWebUIProtocolError

                content = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(content) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise OpenWebUIProtocolError
                    content.extend(chunk)
                status_code = response.status_code
        except httpx.RequestError:
            raise OpenWebUIUpstreamError(
                code="OPENWEBUI_UNAVAILABLE",
                status_code=503,
                retryable=True,
            ) from None

        try:
            payload = json.loads(content)
        except ValueError:
            raise OpenWebUIProtocolError from None

        if status_code >= 400:
            fallback_code, retryable = _fallback_error(status_code)
            try:
                parsed_error = OpenWebUIErrorEnvelope.model_validate(payload)
                code = parsed_error.detail.code if parsed_error.detail.code in KNOWN_ERROR_CODES else fallback_code
            except ValidationError:
                code = fallback_code
            raise OpenWebUIUpstreamError(
                code=code,
                status_code=status_code,
                retryable=retryable,
            )

        try:
            return response_model.model_validate(payload)
        except ValidationError:
            raise OpenWebUIProtocolError from None

    async def resolve_service_user(self, external_client_id: UUID | str) -> ServiceUserResolution:
        return await self._request(
            "/api/v1/internal/external-api/service-users/resolve",
            ServiceUserResolveRequest(external_client_id=external_client_id),
            ServiceUserResolution,
        )

    async def reserve_operation(
        self,
        *,
        operation_id: UUID | str,
        service_user_id: UUID | str,
        model_id: str,
        provider: str | None,
    ) -> CreditOperationSnapshot:
        return await self._request(
            "/api/v1/internal/external-api/operations/reserve",
            OperationReserveRequest(
                operation_id=operation_id,
                service_user_id=service_user_id,
                model_id=model_id,
                provider=provider,
            ),
            CreditOperationSnapshot,
        )

    async def settle_operation(
        self,
        *,
        operation_id: UUID | str,
        service_user_id: UUID | str,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> CreditOperationSnapshot:
        return await self._request(
            "/api/v1/internal/external-api/operations/settle",
            OperationSettleRequest(
                operation_id=operation_id,
                service_user_id=service_user_id,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            ),
            CreditOperationSnapshot,
        )

    async def cancel_operation(
        self,
        *,
        operation_id: UUID | str,
        service_user_id: UUID | str,
    ) -> CreditOperationSnapshot:
        return await self._request(
            "/api/v1/internal/external-api/operations/cancel",
            OperationActionRequest(
                operation_id=operation_id,
                service_user_id=service_user_id,
            ),
            CreditOperationSnapshot,
        )

    async def refund_operation(
        self,
        *,
        operation_id: UUID | str,
        service_user_id: UUID | str,
    ) -> CreditRefundSnapshot:
        return await self._request(
            "/api/v1/internal/external-api/operations/refund",
            OperationActionRequest(
                operation_id=operation_id,
                service_user_id=service_user_id,
            ),
            CreditRefundSnapshot,
        )

    async def get_operation_status(
        self,
        *,
        operation_id: UUID | str,
        service_user_id: UUID | str,
    ) -> CreditOperationStatus:
        return await self._request(
            "/api/v1/internal/external-api/operations/status",
            OperationActionRequest(
                operation_id=operation_id,
                service_user_id=service_user_id,
            ),
            CreditOperationStatus,
        )
