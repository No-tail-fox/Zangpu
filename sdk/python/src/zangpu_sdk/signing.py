from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from hmac import new as new_hmac
from time import time
from urllib.parse import quote, unquote_to_bytes
from uuid import uuid4

HMAC_ALGORITHM = "ZANGPU-HMAC-SHA256"
SIGNATURE_VERSION = "1"
LOWER_HEX_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
KEY_ID_RE = re.compile(r"^zpk_[A-Za-z0-9_-]{4,76}$")
TIMESTAMP_RE = re.compile(r"^(?:0|[1-9][0-9]{0,19})$")
NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
METHOD_RE = re.compile(r"^[A-Za-z]+$")
INVALID_PERCENT_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")
PERCENT_ESCAPE_RE = re.compile(r"%[0-9A-Fa-f]{2}")


class ZangpuSigningError(ValueError):
    pass


def body_sha256_hex(body: bytes) -> str:
    if not isinstance(body, bytes):
        raise ZangpuSigningError("body must be raw bytes")
    return sha256(body).hexdigest()


def _decode_component(value: str, *, reject_double_encoding: bool) -> str:
    if INVALID_PERCENT_RE.search(value):
        raise ZangpuSigningError("malformed percent encoding")
    try:
        decoded = unquote_to_bytes(value).decode("utf-8", errors="strict")
    except (UnicodeDecodeError, UnicodeEncodeError) as exc:
        raise ZangpuSigningError("component is not valid UTF-8") from exc
    if any(ord(character) < 32 or ord(character) == 127 for character in decoded):
        raise ZangpuSigningError("component contains a control character")
    if reject_double_encoding and PERCENT_ESCAPE_RE.search(decoded):
        raise ZangpuSigningError("double encoding is not allowed")
    return decoded


def _rfc3986_encode(value: str) -> str:
    return quote(value, safe="-._~", encoding="utf-8", errors="strict")


def canonicalize_path(raw_path: str) -> str:
    if not isinstance(raw_path, str) or not raw_path.startswith("/") or "?" in raw_path or "#" in raw_path:
        raise ZangpuSigningError("path must be absolute and exclude query or fragment")
    encoded_segments: list[str] = []
    for raw_segment in raw_path.split("/"):
        segment = _decode_component(raw_segment, reject_double_encoding=True)
        if segment in {".", ".."} or "\\" in segment:
            raise ZangpuSigningError("ambiguous path segment")
        encoded_segments.append(_rfc3986_encode(segment))
    return "/".join(encoded_segments)


def canonicalize_query(raw_query: str) -> str:
    if not isinstance(raw_query, str):
        raise ZangpuSigningError("query must be text")
    if not raw_query:
        return ""
    pairs: list[tuple[str, str]] = []
    for raw_pair in raw_query.split("&"):
        raw_key, separator, raw_value = raw_pair.partition("=")
        if not separator:
            raw_value = ""
        key = _rfc3986_encode(_decode_component(raw_key, reject_double_encoding=False))
        value = _rfc3986_encode(_decode_component(raw_value, reject_double_encoding=False))
        pairs.append((key, value))
    pairs.sort()
    return "&".join(f"{key}={value}" for key, value in pairs)


def _validate_fields(*, key_id: str, timestamp: str, nonce: str, request_id: str) -> None:
    if not KEY_ID_RE.fullmatch(key_id):
        raise ZangpuSigningError("invalid key ID")
    if not TIMESTAMP_RE.fullmatch(timestamp):
        raise ZangpuSigningError("invalid timestamp")
    if not NONCE_RE.fullmatch(nonce):
        raise ZangpuSigningError("invalid nonce")
    if not REQUEST_ID_RE.fullmatch(request_id):
        raise ZangpuSigningError("invalid request ID")


def create_canonical_request(
    *,
    method: str,
    raw_path: str,
    raw_query: str,
    body_hash: str,
    key_id: str,
    timestamp: str,
    nonce: str,
    request_id: str,
) -> str:
    if not METHOD_RE.fullmatch(method):
        raise ZangpuSigningError("invalid HTTP method")
    if not LOWER_HEX_SHA256_RE.fullmatch(body_hash):
        raise ZangpuSigningError("invalid body hash")
    _validate_fields(key_id=key_id, timestamp=timestamp, nonce=nonce, request_id=request_id)
    return "\n".join(
        (
            HMAC_ALGORITHM,
            SIGNATURE_VERSION,
            method.upper(),
            canonicalize_path(raw_path),
            canonicalize_query(raw_query),
            body_hash,
            key_id,
            timestamp,
            nonce,
            request_id,
        )
    )


@dataclass(frozen=True, slots=True, repr=False)
class SignedHeaders:
    key_id: str
    timestamp: str
    nonce: str
    request_id: str
    signature: str

    def as_headers(self) -> dict[str, str]:
        return {
            "x-zangpu-key": self.key_id,
            "x-zangpu-timestamp": self.timestamp,
            "x-zangpu-nonce": self.nonce,
            "x-zangpu-request-id": self.request_id,
            "x-zangpu-signature-version": SIGNATURE_VERSION,
            "x-zangpu-signature": self.signature,
        }

    def __repr__(self) -> str:
        return (
            f"SignedHeaders(key_id={self.key_id!r}, request_id={self.request_id!r}, "
            "nonce=<redacted>, signature=<redacted>)"
        )


def _new_nonce() -> str:
    return f"nonce_{uuid4().hex}"


def _new_request_id() -> str:
    return f"req_{uuid4().hex}"


class ZangpuSigner:
    __slots__ = ("_clock", "_key_id", "_nonce_factory", "_request_id_factory", "_secret")

    def __init__(
        self,
        *,
        key_id: str,
        secret: str,
        clock: Callable[[], float] = time,
        nonce_factory: Callable[[], str] = _new_nonce,
        request_id_factory: Callable[[], str] = _new_request_id,
    ) -> None:
        if not KEY_ID_RE.fullmatch(key_id):
            raise ZangpuSigningError("invalid key ID")
        if not isinstance(secret, str) or not secret or len(secret) > 4096:
            raise ZangpuSigningError("invalid signing material")
        if not callable(clock) or not callable(nonce_factory) or not callable(request_id_factory):
            raise ZangpuSigningError("invalid signer dependency")
        self._key_id = key_id
        self._secret = secret
        self._clock = clock
        self._nonce_factory = nonce_factory
        self._request_id_factory = request_id_factory

    def __repr__(self) -> str:
        return f"ZangpuSigner(key_id={self._key_id!r}, secret=<redacted>)"

    def sign(
        self,
        *,
        method: str,
        raw_path: str,
        body: bytes,
        raw_query: str = "",
        request_id: str | None = None,
        nonce: str | None = None,
        timestamp: int | None = None,
    ) -> SignedHeaders:
        resolved_timestamp = int(self._clock()) if timestamp is None else timestamp
        if isinstance(resolved_timestamp, bool) or resolved_timestamp < 0:
            raise ZangpuSigningError("invalid timestamp")
        timestamp_text = str(resolved_timestamp)
        resolved_nonce = self._nonce_factory() if nonce is None else nonce
        resolved_request_id = self._request_id_factory() if request_id is None else request_id
        body_hash = body_sha256_hex(body)
        canonical = create_canonical_request(
            method=method,
            raw_path=raw_path,
            raw_query=raw_query,
            body_hash=body_hash,
            key_id=self._key_id,
            timestamp=timestamp_text,
            nonce=resolved_nonce,
            request_id=resolved_request_id,
        )
        signature = new_hmac(
            self._secret.encode("utf-8"),
            canonical.encode("utf-8"),
            "sha256",
        ).hexdigest()
        return SignedHeaders(
            key_id=self._key_id,
            timestamp=timestamp_text,
            nonce=resolved_nonce,
            request_id=resolved_request_id,
            signature=signature,
        )
