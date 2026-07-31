import asyncio
import base64
import json
from collections.abc import Awaitable, Callable
from dataclasses import replace

import pytest
from fastapi import Request
from pydantic import SecretStr

from backend.app.security import canonical as canonical_security
from backend.app.security.canonical import body_sha256_hex, create_canonical_request, sign_canonical_request
from backend.app.security.credentials import create_protected_credential
from backend.app.security.dependencies import (
    ExternalAuthenticator,
    ExternalAuthFailure,
    ExternalClientDisabled,
    ResolvedCallerCredential,
    authenticate_http_request,
    parse_external_auth_headers,
)
from backend.app.security.keyring import CredentialKeyring

FROZEN_BODY = (
    b'{"model":"zangpu-test","messages":[{"role":"user","content":"hello"}],'
    b'"stream":false,"max_tokens":64}'
)


def build_auth_fixture() -> tuple[CredentialKeyring, ResolvedCallerCredential, str]:
    serialized = SecretStr(
        json.dumps({"v1": base64.b64encode(bytes([7]) * 32).decode("ascii")}, separators=(",", ":"))
    )
    keyring = CredentialKeyring.from_json(serialized, active_key_id="v1")
    result = create_protected_credential(
        keyring,
        api_client_id="client-1",
        created_by="admin-1",
        credential_id="credential-1",
        key_id="zpk_test_0123456789",
        now=1_700_000_000,
    )
    secret = result.take_secret()
    credential = result.credential
    resolved = ResolvedCallerCredential(
        credential_id=credential.id,
        api_client_id=credential.api_client_id,
        key_id=credential.key_id,
        secret_ciphertext=credential.secret_ciphertext,
        secret_nonce=credential.secret_nonce,
        master_key_id=credential.master_key_id,
        credential_status=credential.status,
        credential_expires_at=credential.expires_at,
        client_status="active",
    )
    return keyring, resolved, secret


def signed_headers(
    secret: str,
    *,
    signature: str | None = None,
    method: str = "POST",
    raw_path: str = "/api/v1/external/chat/completions",
    raw_query: str = "",
    body: bytes = FROZEN_BODY,
) -> list[tuple[bytes, bytes]]:
    canonical = create_canonical_request(
        method=method,
        raw_path=raw_path,
        raw_query=raw_query,
        body_hash=body_sha256_hex(body),
        key_id="zpk_test_0123456789",
        timestamp="1785420000",
        nonce="nonce_0123456789abcdef",
        request_id="req_0123456789abcdef",
    )
    resolved_signature = signature or sign_canonical_request(secret, canonical)
    return [
        (b"x-zangpu-key", b"zpk_test_0123456789"),
        (b"x-zangpu-timestamp", b"1785420000"),
        (b"x-zangpu-nonce", b"nonce_0123456789abcdef"),
        (b"x-zangpu-request-id", b"req_0123456789abcdef"),
        (b"x-zangpu-signature-version", b"1"),
        (b"x-zangpu-signature", resolved_signature.encode("ascii")),
    ]


def resolver_for(
    resolved: ResolvedCallerCredential | None,
) -> Callable[[str], Awaitable[ResolvedCallerCredential | None]]:
    async def resolve(_key_id: str) -> ResolvedCallerCredential | None:
        return resolved

    return resolve


def test_external_auth_uses_constant_time_comparison(monkeypatch: pytest.MonkeyPatch) -> None:
    keyring, resolved, secret = build_auth_fixture()
    headers = signed_headers(secret)
    comparisons: list[tuple[str, str]] = []
    original_compare = canonical_security.compare_digest

    def recording_compare(left: str, right: str) -> bool:
        comparisons.append((left, right))
        return original_compare(left, right)

    monkeypatch.setattr(canonical_security, "compare_digest", recording_compare)
    authenticator = ExternalAuthenticator(keyring=keyring, resolver=resolver_for(resolved))
    context = asyncio.run(
        authenticator.authenticate(
            method="POST",
            raw_path="/api/v1/external/chat/completions",
            raw_query="",
            body=FROZEN_BODY,
            raw_headers=headers,
            now=1_785_420_000,
        )
    )

    assert context.api_client_id == "client-1"
    assert context.credential_id == "credential-1"
    assert context.request_id == "req_0123456789abcdef"
    assert len(comparisons) == 1
    rendered = repr((parse_external_auth_headers(headers), resolved, context))
    assert headers[-1][1].decode("ascii") not in rendered
    assert "nonce_0123456789abcdef" not in rendered
    assert resolved.secret_ciphertext not in rendered
    assert resolved.secret_nonce not in rendered


def test_duplicate_or_malformed_signed_headers_are_rejected() -> None:
    _keyring, _resolved, secret = build_auth_fixture()
    headers = signed_headers(secret)
    with pytest.raises(ExternalAuthFailure):
        parse_external_auth_headers(headers + [(b"X-Zangpu-Signature", headers[-1][1])])

    malformed = [(name, value) for name, value in headers if name != b"x-zangpu-nonce"]
    malformed.append((b"x-zangpu-nonce", b"short"))
    with pytest.raises(ExternalAuthFailure):
        parse_external_auth_headers(malformed)

    with pytest.raises(ExternalAuthFailure):
        parse_external_auth_headers(headers + [(b"x-extra", b"x" * 8_193)])
    with pytest.raises(ExternalAuthFailure):
        parse_external_auth_headers(headers + [(b"x" * 65, b"value")])


def test_auth_failures_have_one_outward_response() -> None:
    keyring, resolved, secret = build_auth_fixture()
    cases: list[tuple[ExternalAuthenticator, list[tuple[bytes, bytes]], int]] = [
        (ExternalAuthenticator(keyring=keyring, resolver=resolver_for(None)), signed_headers(secret), 1_785_420_000),
        (
            ExternalAuthenticator(keyring=keyring, resolver=resolver_for(resolved)),
            signed_headers(secret, signature="0" * 64),
            1_785_420_000,
        ),
        (
            ExternalAuthenticator(keyring=keyring, resolver=resolver_for(resolved)),
            signed_headers(secret),
            1_785_420_301,
        ),
    ]
    rendered: list[tuple[int, bytes, str]] = []
    for authenticator, headers, now in cases:
        with pytest.raises(ExternalAuthFailure) as captured:
            asyncio.run(
                authenticator.authenticate(
                    method="POST",
                    raw_path="/api/v1/external/chat/completions",
                    raw_query="",
                    body=FROZEN_BODY,
                    raw_headers=headers,
                    now=now,
                )
            )
        response = captured.value.to_response("req_server_0123456789")
        rendered.append((response.status_code, response.body, response.headers["x-zangpu-request-id"]))

    assert len(set(rendered)) == 1
    assert rendered[0][0] == 401
    assert json.loads(rendered[0][1]) == {
        "error": {
            "code": "AUTH_FAILED",
            "message": "Authentication failed.",
            "request_id": "req_server_0123456789",
            "retryable": False,
        }
    }


def test_unicode_asgi_path_uses_raw_request_target() -> None:
    keyring, resolved, secret = build_auth_fixture()
    raw_path = "/%E8%97%8F"
    headers = signed_headers(secret, method="GET", raw_path=raw_path, body=b"")

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/藏",
            "raw_path": raw_path.encode("ascii"),
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        },
        receive,
    )
    authenticator = ExternalAuthenticator(keyring=keyring, resolver=resolver_for(resolved))

    context = asyncio.run(authenticate_http_request(request, authenticator, now=1_785_420_000))
    assert context.api_client_id == "client-1"


def test_disabled_client_is_reported_only_after_valid_authentication() -> None:
    keyring, resolved, secret = build_auth_fixture()
    disabled = replace(resolved, client_status="disabled")
    authenticator = ExternalAuthenticator(keyring=keyring, resolver=resolver_for(disabled))

    with pytest.raises(ExternalClientDisabled) as captured:
        asyncio.run(
            authenticator.authenticate(
                method="POST",
                raw_path="/api/v1/external/chat/completions",
                raw_query="",
                body=FROZEN_BODY,
                raw_headers=signed_headers(secret),
                now=1_785_420_000,
            )
        )
    response = captured.value.to_response("req_server_0123456789")
    assert response.status_code == 403
    assert json.loads(response.body)["error"]["code"] == "CLIENT_DISABLED"
