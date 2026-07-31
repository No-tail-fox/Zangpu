import re
from hashlib import sha256
from hmac import compare_digest
from hmac import new as new_hmac
from urllib.parse import quote, unquote_to_bytes

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


class CanonicalizationError(ValueError):
    pass


def body_sha256_hex(body: bytes) -> str:
    if not isinstance(body, bytes):
        raise CanonicalizationError("body must be raw bytes")
    return sha256(body).hexdigest()


def _decode_component(value: str, *, reject_double_encoding: bool) -> str:
    if INVALID_PERCENT_RE.search(value):
        raise CanonicalizationError("malformed percent encoding")
    try:
        decoded = unquote_to_bytes(value).decode("utf-8", errors="strict")
    except (UnicodeDecodeError, UnicodeEncodeError) as exc:
        raise CanonicalizationError("component is not valid UTF-8") from exc
    if any(ord(character) < 32 or ord(character) == 127 for character in decoded):
        raise CanonicalizationError("component contains a control character")
    if reject_double_encoding and PERCENT_ESCAPE_RE.search(decoded):
        raise CanonicalizationError("double encoding is not allowed")
    return decoded


def _rfc3986_encode(value: str) -> str:
    return quote(value, safe="-._~", encoding="utf-8", errors="strict")


def canonicalize_path(raw_path: str) -> str:
    if not isinstance(raw_path, str) or not raw_path.startswith("/") or "?" in raw_path or "#" in raw_path:
        raise CanonicalizationError("path must be an absolute path without query or fragment")

    encoded_segments: list[str] = []
    for raw_segment in raw_path.split("/"):
        segment = _decode_component(raw_segment, reject_double_encoding=True)
        if segment in {".", ".."} or "\\" in segment:
            raise CanonicalizationError("ambiguous path segment")
        encoded_segments.append(_rfc3986_encode(segment))
    return "/".join(encoded_segments)


def canonicalize_query(raw_query: str) -> str:
    if not isinstance(raw_query, str):
        raise CanonicalizationError("query must be text")
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


def validate_signing_fields(*, key_id: str, timestamp: str, nonce: str, request_id: str) -> None:
    if not KEY_ID_RE.fullmatch(key_id):
        raise CanonicalizationError("invalid key ID")
    if not TIMESTAMP_RE.fullmatch(timestamp):
        raise CanonicalizationError("invalid timestamp")
    if not NONCE_RE.fullmatch(nonce):
        raise CanonicalizationError("invalid nonce")
    if not REQUEST_ID_RE.fullmatch(request_id):
        raise CanonicalizationError("invalid request ID")


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
        raise CanonicalizationError("invalid HTTP method")
    if not LOWER_HEX_SHA256_RE.fullmatch(body_hash):
        raise CanonicalizationError("invalid body hash")
    validate_signing_fields(key_id=key_id, timestamp=timestamp, nonce=nonce, request_id=request_id)
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


def sign_canonical_request(secret: str, canonical_request: str) -> str:
    if not isinstance(secret, str) or not secret or not isinstance(canonical_request, str):
        raise CanonicalizationError("secret and canonical request must be non-empty text")
    return new_hmac(secret.encode("utf-8"), canonical_request.encode("utf-8"), "sha256").hexdigest()


def verify_signature(secret: str, canonical_request: str, presented_signature: str) -> bool:
    if not LOWER_HEX_SHA256_RE.fullmatch(presented_signature):
        return False
    expected_signature = sign_canonical_request(secret, canonical_request)
    return compare_digest(expected_signature, presented_signature)
