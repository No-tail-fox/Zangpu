from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from zangpu_sdk.errors import ZangpuAPIError, ZangpuProtocolError, ZangpuTransportError
from zangpu_sdk.signing import ZangpuSigner

MAX_REQUEST_BYTES = 1024 * 1024
MAX_JSON_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_SSE_EVENT_BYTES = 1024 * 1024
SAFE_RESPONSE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
SDK_VERSION = "0.1.0"


@dataclass(frozen=True, slots=True)
class RateLimit:
    limit: int
    remaining: int
    reset_at: int


@dataclass(frozen=True, slots=True)
class ZangpuResponse:
    data: dict[str, Any]
    request_id: str
    rate_limit: RateLimit


@dataclass(frozen=True, slots=True)
class ZangpuStreamEvent(Mapping[str, Any]):
    data: dict[str, Any]
    request_id: str

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.data)

    def __len__(self) -> int:
        return len(self.data)


def _read_bounded(response: httpx.Response, *, limit: int) -> bytes:
    content = bytearray()
    for chunk in response.iter_bytes():
        if len(content) + len(chunk) > limit:
            raise ZangpuProtocolError
        content.extend(chunk)
    return bytes(content)


def _content_type(response: httpx.Response) -> str:
    return response.headers.get("content-type", "").partition(";")[0].strip().lower()


def _server_request_id(response: httpx.Response) -> str:
    value = response.headers.get("x-zangpu-request-id", "")
    if not SAFE_RESPONSE_ID_RE.fullmatch(value):
        raise ZangpuProtocolError
    return value


def _rate_limit(response: httpx.Response) -> RateLimit:
    try:
        limit = int(response.headers["x-ratelimit-limit"])
        remaining = int(response.headers["x-ratelimit-remaining"])
        reset_at = int(response.headers["x-ratelimit-reset"])
    except (KeyError, TypeError, ValueError):
        raise ZangpuProtocolError from None
    if limit <= 0 or remaining < 0 or remaining > limit or reset_at < 0:
        raise ZangpuProtocolError
    return RateLimit(limit=limit, remaining=remaining, reset_at=reset_at)


def _safe_error_text(value: object, *, default: str, max_length: int) -> str:
    if not isinstance(value, str) or not value or len(value) > max_length:
        return default
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return default
    return value


def _api_error(response: httpx.Response, content: bytes) -> ZangpuAPIError:
    request_id = response.headers.get("x-zangpu-request-id")
    code = "HTTP_ERROR"
    message = "Request failed."
    retryable = response.status_code == 429 or response.status_code >= 500
    operation_id: str | None = None
    if _content_type(response) == "application/json":
        try:
            payload = json.loads(content)
            error = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(error, dict):
                code = _safe_error_text(error.get("code"), default=code, max_length=64)
                message = _safe_error_text(error.get("message"), default=message, max_length=512)
                envelope_request_id = error.get("request_id")
                if isinstance(envelope_request_id, str) and SAFE_RESPONSE_ID_RE.fullmatch(envelope_request_id):
                    request_id = envelope_request_id
                if isinstance(error.get("retryable"), bool):
                    retryable = error["retryable"]
                candidate_operation = error.get("operation_id")
                if isinstance(candidate_operation, str) and SAFE_RESPONSE_ID_RE.fullmatch(candidate_operation):
                    operation_id = candidate_operation
        except (UnicodeDecodeError, ValueError):
            pass
    if request_id is not None and not SAFE_RESPONSE_ID_RE.fullmatch(request_id):
        request_id = None
    return ZangpuAPIError(
        status_code=response.status_code,
        code=code,
        message=message,
        request_id=request_id,
        retryable=retryable,
        operation_id=operation_id,
    )


class ZangpuClient:
    __slots__ = ("_client", "_signer")

    def __init__(
        self,
        *,
        base_url: str,
        key_id: str,
        secret: str,
        timeout_seconds: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        parsed = httpx.URL(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.host
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or (parsed.scheme == "http" and parsed.host.lower() not in LOOPBACK_HOSTS)
        ):
            raise ValueError("base URL must be an HTTPS origin or a loopback HTTP origin")
        if isinstance(timeout_seconds, bool) or not 0.1 <= timeout_seconds <= 300:
            raise ValueError("timeout must be between 0.1 and 300 seconds")
        signer_options = {} if clock is None else {"clock": clock}
        self._signer = ZangpuSigner(key_id=key_id, secret=secret, **signer_options)
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, 5.0)),
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=8),
            follow_redirects=False,
            headers={"user-agent": f"zangpu-python/{SDK_VERSION}"},
            transport=transport,
        )

    def __repr__(self) -> str:
        return f"ZangpuClient(base_url={str(self._client.base_url)!r}, signer={self._signer!r})"

    def __enter__(self) -> ZangpuClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    @staticmethod
    def _json_body(payload: Mapping[str, Any]) -> bytes:
        try:
            body = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        except (TypeError, ValueError):
            raise ValueError("request payload is not JSON serializable") from None
        if len(body) > MAX_REQUEST_BYTES:
            raise ValueError("request payload exceeds 1 MiB")
        return body

    def _headers(
        self,
        *,
        method: str,
        path: str,
        body: bytes,
        request_id: str | None,
        stream: bool,
    ) -> dict[str, str]:
        signed = self._signer.sign(method=method, raw_path=path, body=body, request_id=request_id)
        headers = signed.as_headers()
        headers["accept"] = "text/event-stream" if stream else "application/json"
        if body:
            headers["content-type"] = "application/json"
        return headers

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        body: bytes = b"",
        request_id: str | None = None,
    ) -> ZangpuResponse:
        headers = self._headers(
            method=method,
            path=path,
            body=body,
            request_id=request_id,
            stream=False,
        )
        try:
            with self._client.stream(method, path, headers=headers, content=body) as response:
                content = _read_bounded(response, limit=MAX_JSON_RESPONSE_BYTES)
                if response.status_code >= 400:
                    raise _api_error(response, content)
                if _content_type(response) != "application/json":
                    raise ZangpuProtocolError
                try:
                    payload = json.loads(content)
                except (UnicodeDecodeError, ValueError):
                    raise ZangpuProtocolError from None
                if not isinstance(payload, dict):
                    raise ZangpuProtocolError
                return ZangpuResponse(
                    data=payload,
                    request_id=_server_request_id(response),
                    rate_limit=_rate_limit(response),
                )
        except (ZangpuAPIError, ZangpuProtocolError):
            raise
        except httpx.TimeoutException:
            raise ZangpuTransportError("REQUEST_TIMEOUT") from None
        except httpx.RequestError:
            raise ZangpuTransportError("REQUEST_UNAVAILABLE") from None

    @staticmethod
    def _chat_payload(
        *,
        model: str,
        messages: Sequence[Mapping[str, Any]],
        stream: bool,
        temperature: float | None,
        top_p: float | None,
        max_tokens: int | None,
        max_completion_tokens: int | None,
        stop: str | Sequence[str] | None,
    ) -> dict[str, Any]:
        if not isinstance(model, str) or not model or len(model) > 255:
            raise ValueError("model must be bounded non-empty text")
        if not isinstance(messages, Sequence) or isinstance(messages, str | bytes) or not 1 <= len(messages) <= 100:
            raise ValueError("messages must contain between 1 and 100 items")
        if any(not isinstance(message, Mapping) for message in messages):
            raise ValueError("each message must be a mapping")
        if max_tokens is not None and max_completion_tokens is not None:
            raise ValueError("max_tokens and max_completion_tokens are mutually exclusive")
        payload: dict[str, Any] = {
            "model": model,
            "messages": [dict(message) for message in messages],
            "stream": stream,
        }
        optional = {
            "temperature": temperature,
            "top_p": top_p,
            "max_tokens": max_tokens,
            "max_completion_tokens": max_completion_tokens,
            "stop": list(stop) if isinstance(stop, Sequence) and not isinstance(stop, str) else stop,
        }
        payload.update({key: value for key, value in optional.items() if value is not None})
        return payload

    def list_models(self, *, request_id: str | None = None) -> ZangpuResponse:
        return self._request_json("GET", "/api/v1/external/models", request_id=request_id)

    def get_usage(self, *, request_id: str | None = None) -> ZangpuResponse:
        return self._request_json("GET", "/api/v1/external/usage", request_id=request_id)

    def chat_completions(
        self,
        *,
        model: str,
        messages: Sequence[Mapping[str, Any]],
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
        max_completion_tokens: int | None = None,
        stop: str | Sequence[str] | None = None,
        request_id: str | None = None,
    ) -> ZangpuResponse:
        payload = self._chat_payload(
            model=model,
            messages=messages,
            stream=False,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            max_completion_tokens=max_completion_tokens,
            stop=stop,
        )
        return self._request_json(
            "POST",
            "/api/v1/external/chat/completions",
            body=self._json_body(payload),
            request_id=request_id,
        )

    def stream_chat_completions(
        self,
        *,
        model: str,
        messages: Sequence[Mapping[str, Any]],
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
        max_completion_tokens: int | None = None,
        stop: str | Sequence[str] | None = None,
        request_id: str | None = None,
    ) -> Iterator[ZangpuStreamEvent]:
        payload = self._chat_payload(
            model=model,
            messages=messages,
            stream=True,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            max_completion_tokens=max_completion_tokens,
            stop=stop,
        )
        body = self._json_body(payload)
        path = "/api/v1/external/chat/completions"
        headers = self._headers(method="POST", path=path, body=body, request_id=request_id, stream=True)
        try:
            with self._client.stream("POST", path, headers=headers, content=body) as response:
                if response.status_code >= 400:
                    content = _read_bounded(response, limit=MAX_JSON_RESPONSE_BYTES)
                    raise _api_error(response, content)
                if _content_type(response) != "text/event-stream":
                    raise ZangpuProtocolError
                server_request_id = _server_request_id(response)
                data_lines: list[str] = []
                event_bytes = 0
                done = False
                for line in response.iter_lines():
                    if line == "":
                        if not data_lines:
                            continue
                        event_text = "\n".join(data_lines)
                        data_lines.clear()
                        event_bytes = 0
                        if event_text == "[DONE]":
                            done = True
                            break
                        try:
                            event = json.loads(event_text)
                        except (UnicodeDecodeError, ValueError):
                            raise ZangpuProtocolError from None
                        if not isinstance(event, dict):
                            raise ZangpuProtocolError
                        if "error" in event:
                            synthetic = httpx.Response(
                                200,
                                headers={
                                    "content-type": "application/json",
                                    "x-zangpu-request-id": server_request_id,
                                },
                            )
                            raise _api_error(synthetic, json.dumps(event).encode("utf-8"))
                        yield ZangpuStreamEvent(data=event, request_id=server_request_id)
                        continue
                    if line.startswith(":"):
                        if data_lines:
                            raise ZangpuProtocolError
                        continue
                    if not line.startswith("data:"):
                        raise ZangpuProtocolError
                    value = line[5:]
                    if value.startswith(" "):
                        value = value[1:]
                    event_bytes += len(value.encode("utf-8"))
                    if event_bytes > MAX_SSE_EVENT_BYTES:
                        raise ZangpuProtocolError
                    data_lines.append(value)
                if not done or data_lines:
                    raise ZangpuProtocolError
        except (ZangpuAPIError, ZangpuProtocolError):
            raise
        except httpx.TimeoutException:
            raise ZangpuTransportError("REQUEST_TIMEOUT") from None
        except httpx.RequestError:
            raise ZangpuTransportError("REQUEST_UNAVAILABLE") from None
