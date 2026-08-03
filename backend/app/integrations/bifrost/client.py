from __future__ import annotations

import re
from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx
from pydantic import SecretStr, TypeAdapter, ValidationError

from backend.app.integrations.bifrost.models import (
    BifrostConfigState,
    BifrostHealth,
    BifrostModelList,
    BifrostProtocolError,
    BifrostProviderList,
    BifrostUpstreamError,
    ChatCompletionResponse,
    VirtualKeyCreationResult,
    VirtualKeyEnvelope,
    VirtualKeyListEnvelope,
    VirtualKeySpec,
    VirtualKeyState,
    VirtualKeyWire,
)

MAX_MANAGEMENT_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_INFERENCE_RESPONSE_BYTES = 16 * 1024 * 1024
VIRTUAL_KEY_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def _map_status(status_code: int) -> tuple[str, bool]:
    if status_code in {401, 403}:
        return "BIFROST_AUTH_REJECTED", False
    if status_code == 404:
        return "BIFROST_NOT_FOUND", False
    if status_code == 409:
        return "BIFROST_CONFLICT", True
    if status_code == 429:
        return "BIFROST_RATE_LIMITED", True
    if status_code >= 500:
        return "BIFROST_UNAVAILABLE", True
    return "BIFROST_REQUEST_REJECTED", False


class BifrostClient:
    __slots__ = ("_client", "_management_token")

    def __init__(
        self,
        *,
        base_url: str,
        management_token: SecretStr,
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
            raise ValueError("Bifrost base URL must be an origin without credentials")
        self._management_token = management_token
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, 5.0)),
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=16),
            follow_redirects=False,
            transport=transport,
        )

    def __repr__(self) -> str:
        return f"BifrostClient(base_url={str(self._client.base_url)!r}, management_token=<redacted>)"

    async def aclose(self) -> None:
        await self._client.aclose()

    def _management_headers(self) -> dict[str, str]:
        return {"authorization": f"Bearer {self._management_token.get_secret_value()}"}

    async def _json_request(
        self,
        method: str,
        path: str,
        *,
        management: bool,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        max_bytes: int = MAX_MANAGEMENT_RESPONSE_BYTES,
    ) -> Any:
        try:
            response = await self._client.request(
                method,
                path,
                headers=self._management_headers() if management else None,
                json=json,
                params=params,
            )
        except httpx.TimeoutException:
            raise BifrostUpstreamError(
                code="BIFROST_TIMEOUT", status_code=504, retryable=True
            ) from None
        except httpx.RequestError:
            raise BifrostUpstreamError(
                code="BIFROST_UNAVAILABLE", status_code=503, retryable=True
            ) from None
        content_type = response.headers.get("content-type", "").partition(";")[0].strip().lower()
        if content_type != "application/json" or len(response.content) > max_bytes:
            raise BifrostProtocolError
        if response.status_code >= 400:
            code, retryable = _map_status(response.status_code)
            raise BifrostUpstreamError(code=code, status_code=response.status_code, retryable=retryable)
        try:
            return response.json()
        except ValueError:
            raise BifrostProtocolError from None

    @staticmethod
    def _validate(model: type[Any], payload: Any) -> Any:
        try:
            return model.model_validate(payload)
        except (AttributeError, ValidationError, ValueError):
            raise BifrostProtocolError from None

    @staticmethod
    def _state(wire: VirtualKeyWire) -> VirtualKeyState:
        try:
            return wire.state()
        except (ValidationError, ValueError):
            raise BifrostProtocolError from None

    async def get_health(self) -> BifrostHealth:
        payload = await self._json_request("GET", "/health", management=False)
        return self._validate(BifrostHealth, payload)

    async def get_version(self) -> str:
        payload = await self._json_request("GET", "/api/version", management=True)
        try:
            return TypeAdapter(str).validate_python(payload)
        except ValidationError:
            raise BifrostProtocolError from None

    async def get_config(self) -> BifrostConfigState:
        payload = await self._json_request("GET", "/api/config", management=True)
        return self._validate(BifrostConfigState, payload)

    async def get_providers(self) -> BifrostProviderList:
        payload = await self._json_request("GET", "/api/providers", management=True)
        return self._validate(BifrostProviderList, payload)

    async def get_models(self) -> BifrostModelList:
        payload = await self._json_request("GET", "/api/models", management=True)
        return self._validate(BifrostModelList, payload)

    async def list_virtual_keys(self, *, limit: int = 100, offset: int = 0) -> list[VirtualKeyState]:
        if not 0 <= limit <= 1000 or offset < 0:
            raise ValueError("virtual-key pagination is out of bounds")
        payload = await self._json_request(
            "GET",
            "/api/governance/virtual-keys",
            management=True,
            params={"limit": limit, "offset": offset},
        )
        envelope = self._validate(VirtualKeyListEnvelope, payload)
        return [self._state(item) for item in envelope.virtual_keys]

    async def probe_virtual_key_route(self) -> bool:
        payload = await self._json_request(
            "GET",
            "/api/governance/virtual-keys",
            management=True,
            params={"limit": 0, "offset": 0},
        )
        self._validate(VirtualKeyListEnvelope, payload)
        return True

    async def _list_virtual_key_material(self, *, name: str) -> list[VirtualKeyCreationResult]:
        payload = await self._json_request(
            "GET",
            "/api/governance/virtual-keys",
            management=True,
            params={"limit": 100, "offset": 0, "search": name},
        )
        envelope = self._validate(VirtualKeyListEnvelope, payload)
        return [
            VirtualKeyCreationResult(state=self._state(item), value=item.value)
            for item in envelope.virtual_keys
            if item.name == name
        ]

    async def find_virtual_key_by_name(self, name: str) -> VirtualKeyCreationResult | None:
        matches = await self._list_virtual_key_material(name=name)
        if len(matches) > 1:
            raise BifrostProtocolError
        return matches[0] if matches else None

    async def create_virtual_key(self, spec: VirtualKeySpec) -> VirtualKeyCreationResult:
        payload = await self._json_request(
            "POST",
            "/api/governance/virtual-keys",
            management=True,
            json=spec.create_payload(),
        )
        envelope = self._validate(VirtualKeyEnvelope, payload)
        return VirtualKeyCreationResult(state=self._state(envelope.virtual_key), value=envelope.virtual_key.value)

    async def get_virtual_key(self, virtual_key_id: str) -> VirtualKeyState:
        envelope = await self._get_virtual_key_envelope(virtual_key_id)
        return self._state(envelope.virtual_key)

    async def get_virtual_key_material(self, virtual_key_id: str) -> VirtualKeyCreationResult:
        envelope = await self._get_virtual_key_envelope(virtual_key_id)
        return VirtualKeyCreationResult(state=self._state(envelope.virtual_key), value=envelope.virtual_key.value)

    async def _get_virtual_key_envelope(self, virtual_key_id: str) -> VirtualKeyEnvelope:
        self._validate_virtual_key_id(virtual_key_id)
        payload = await self._json_request(
            "GET", f"/api/governance/virtual-keys/{virtual_key_id}", management=True
        )
        return self._validate(VirtualKeyEnvelope, payload)

    async def update_virtual_key(self, virtual_key_id: str, spec: VirtualKeySpec) -> VirtualKeyState:
        payload = spec.create_payload()
        payload.pop("is_active")
        envelope = await self._update_virtual_key(virtual_key_id, payload)
        return self._state(envelope.virtual_key)

    async def disable_virtual_key(self, virtual_key_id: str) -> VirtualKeyState:
        envelope = await self._update_virtual_key(virtual_key_id, {"is_active": False})
        return self._state(envelope.virtual_key)

    async def rotate_virtual_key(self, virtual_key_id: str) -> VirtualKeyCreationResult:
        self._validate_virtual_key_id(virtual_key_id)
        response = await self._json_request(
            "POST",
            f"/api/governance/virtual-keys/{virtual_key_id}/rotate",
            management=True,
        )
        envelope = self._validate(VirtualKeyEnvelope, response)
        return VirtualKeyCreationResult(state=self._state(envelope.virtual_key), value=envelope.virtual_key.value)

    async def _update_virtual_key(self, virtual_key_id: str, payload: Mapping[str, Any]) -> VirtualKeyEnvelope:
        self._validate_virtual_key_id(virtual_key_id)
        response = await self._json_request(
            "PUT",
            f"/api/governance/virtual-keys/{virtual_key_id}",
            management=True,
            json=payload,
        )
        return self._validate(VirtualKeyEnvelope, response)

    async def delete_virtual_key(self, virtual_key_id: str) -> None:
        self._validate_virtual_key_id(virtual_key_id)
        try:
            await self._json_request(
                "DELETE", f"/api/governance/virtual-keys/{virtual_key_id}", management=True
            )
        except BifrostUpstreamError as exc:
            if exc.code != "BIFROST_NOT_FOUND":
                raise

    @staticmethod
    def _validate_virtual_key_id(virtual_key_id: str) -> None:
        if not VIRTUAL_KEY_ID_RE.fullmatch(virtual_key_id):
            raise ValueError("invalid virtual-key ID")

    async def forward_chat_completion(
        self, payload: Mapping[str, Any], *, virtual_key: SecretStr
    ) -> ChatCompletionResponse:
        response = await self._inference_json(payload, virtual_key=virtual_key)
        return self._validate(ChatCompletionResponse, response)

    async def _inference_json(self, payload: Mapping[str, Any], *, virtual_key: SecretStr) -> Any:
        try:
            response = await self._client.post(
                "/v1/chat/completions",
                headers={"x-bf-vk": virtual_key.get_secret_value()},
                json=dict(payload),
            )
        except httpx.TimeoutException:
            raise BifrostUpstreamError(
                code="BIFROST_TIMEOUT", status_code=504, retryable=True
            ) from None
        except httpx.RequestError:
            raise BifrostUpstreamError(
                code="BIFROST_UNAVAILABLE", status_code=503, retryable=True
            ) from None
        content_type = response.headers.get("content-type", "").partition(";")[0].strip().lower()
        if content_type != "application/json" or len(response.content) > MAX_INFERENCE_RESPONSE_BYTES:
            raise BifrostProtocolError
        if response.status_code >= 400:
            code, retryable = _map_status(response.status_code)
            raise BifrostUpstreamError(code=code, status_code=response.status_code, retryable=retryable)
        try:
            return response.json()
        except ValueError:
            raise BifrostProtocolError from None

    async def stream_chat_completion(
        self, payload: Mapping[str, Any], *, virtual_key: SecretStr
    ) -> AsyncIterator[bytes]:
        request_payload = dict(payload)
        request_payload["stream"] = True
        try:
            async with self._client.stream(
                "POST",
                "/v1/chat/completions",
                headers={"x-bf-vk": virtual_key.get_secret_value()},
                json=request_payload,
            ) as response:
                content_type = response.headers.get("content-type", "").partition(";")[0].strip().lower()
                if response.status_code >= 400:
                    code, retryable = _map_status(response.status_code)
                    raise BifrostUpstreamError(code=code, status_code=response.status_code, retryable=retryable)
                if content_type != "text/event-stream":
                    raise BifrostProtocolError
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_INFERENCE_RESPONSE_BYTES:
                        raise BifrostProtocolError
                    yield chunk
        except httpx.TimeoutException:
            raise BifrostUpstreamError(
                code="BIFROST_TIMEOUT", status_code=504, retryable=True
            ) from None
        except httpx.RequestError:
            raise BifrostUpstreamError(
                code="BIFROST_UNAVAILABLE", status_code=503, retryable=True
            ) from None
