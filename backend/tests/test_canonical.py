import pytest

from backend.app.security.canonical import (
    CanonicalizationError,
    body_sha256_hex,
    canonicalize_path,
    canonicalize_query,
    create_canonical_request,
    sign_canonical_request,
    verify_signature,
)

FROZEN_BODY = (
    b'{"model":"zangpu-test","messages":[{"role":"user","content":"hello"}],'
    b'"stream":false,"max_tokens":64}'
)
FROZEN_BODY_HASH = "7bca5683972121e152dcab367d58a69c5fe17b7585f621eb702e1b17209bc77a"
FROZEN_SIGNATURE = "121118be99e3276c168066f7a10b12cd4d395a13ecbb843dbb545363595decfe"


def test_frozen_hmac_v1_vector() -> None:
    body_hash = body_sha256_hex(FROZEN_BODY)
    canonical = create_canonical_request(
        method="post",
        raw_path="/api/v1/external/chat/completions",
        raw_query="",
        body_hash=body_hash,
        key_id="zpk_test_0123456789",
        timestamp="1785420000",
        nonce="nonce_0123456789abcdef",
        request_id="req_0123456789abcdef",
    )

    assert body_hash == FROZEN_BODY_HASH
    assert len(canonical.splitlines()) == 10
    assert not canonical.endswith("\n")
    assert sign_canonical_request("zps_test_secret_0123456789", canonical) == FROZEN_SIGNATURE
    assert verify_signature("zps_test_secret_0123456789", canonical, FROZEN_SIGNATURE)


def test_path_and_repeated_query_are_normalized_with_rfc3986_rules() -> None:
    assert canonicalize_path("/v1/%e8%97%8f/%2f") == "/v1/%E8%97%8F/%2F"
    assert canonicalize_query("b=2&a=z&a=b&plus=hello+world") == "a=b&a=z&b=2&plus=hello%2Bworld"


@pytest.mark.parametrize(
    "raw_path",
    ["relative/path", "/a/./b", "/a/%2e%2e/b", "/a/%252F/b", "/bad/%ZZ", "/bad/%FF", "/bad/\ud800"],
)
def test_malformed_or_ambiguous_paths_are_rejected(raw_path: str) -> None:
    with pytest.raises(CanonicalizationError):
        canonicalize_path(raw_path)


@pytest.mark.parametrize("raw_query", ["key=%ZZ", "key=%FF"])
def test_malformed_queries_are_rejected(raw_query: str) -> None:
    with pytest.raises(CanonicalizationError):
        canonicalize_query(raw_query)


def test_malformed_content_hash_and_signing_fields_are_rejected() -> None:
    values = {
        "method": "POST",
        "raw_path": "/api/v1/external/models",
        "raw_query": "",
        "body_hash": "A" * 64,
        "key_id": "zpk_test_0123456789",
        "timestamp": "1785420000",
        "nonce": "nonce_0123456789abcdef",
        "request_id": "req_0123456789abcdef",
    }
    with pytest.raises(CanonicalizationError):
        create_canonical_request(**values)

    values["body_hash"] = body_sha256_hex(b"")
    values["request_id"] = "bad\nrequest"
    with pytest.raises(CanonicalizationError):
        create_canonical_request(**values)
