from zangpu_sdk.signing import ZangpuSigner, canonicalize_path, canonicalize_query

FROZEN_BODY = (
    b'{"model":"zangpu-test","messages":[{"role":"user","content":"hello"}],'
    b'"stream":false,"max_tokens":64}'
)
FROZEN_SIGNATURE = "121118be99e3276c168066f7a10b12cd4d395a13ecbb843dbb545363595decfe"


def test_sdk_signer_matches_backend_frozen_vector_and_redacts_sensitive_fields() -> None:
    signer = ZangpuSigner(
        key_id="zpk_test_0123456789",
        secret="zps_test_secret_0123456789",  # noqa: S106
        clock=lambda: 1_785_420_000,
        nonce_factory=lambda: "nonce_0123456789abcdef",
        request_id_factory=lambda: "req_0123456789abcdef",
    )

    signed = signer.sign(
        method="POST",
        raw_path="/api/v1/external/chat/completions",
        body=FROZEN_BODY,
    )

    assert signed.signature == FROZEN_SIGNATURE
    assert signed.as_headers()["x-zangpu-signature-version"] == "1"
    rendered = repr((signer, signed))
    assert "zps_test_secret_0123456789" not in rendered
    assert FROZEN_SIGNATURE not in rendered
    assert "nonce_0123456789abcdef" not in rendered


def test_sdk_signer_uses_backend_rfc3986_normalization() -> None:
    assert canonicalize_path("/v1/%e8%97%8f/%2f") == "/v1/%E8%97%8F/%2F"
    assert canonicalize_query("b=2&a=z&a=b&plus=hello+world") == "a=b&a=z&b=2&plus=hello%2Bworld"
