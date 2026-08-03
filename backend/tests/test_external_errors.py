import pytest

from backend.app.api.errors import ERROR_SPECS, ExternalApiError, external_error_response


@pytest.mark.parametrize(("code", "status"), [(code, spec.status_code) for code, spec in ERROR_SPECS.items()])
def test_every_external_error_uses_one_bounded_envelope(code: str, status: int) -> None:
    response = external_error_response(
        code,
        server_request_id="req_server_0123456789",
        operation_id="operation-1" if code.startswith("REQUEST_") else None,
    )
    payload = bytes(response.body).decode("utf-8")

    assert response.status_code == status
    assert response.headers["x-zangpu-request-id"] == "req_server_0123456789"
    assert response.headers["cache-control"] == "no-store"
    assert '"error"' in payload and f'"code":"{code}"' in payload
    assert "traceback" not in payload.lower()


def test_external_api_error_rejects_unregistered_codes_and_preserves_rate_headers() -> None:
    with pytest.raises(ValueError, match="unknown"):
        ExternalApiError("UPSTREAM_RAW_DETAIL")

    response = ExternalApiError(
        "QPS_LIMITED",
        headers={"X-RateLimit-Limit": "2", "X-RateLimit-Remaining": "0"},
    ).to_response("req_server_0123456789")
    assert response.status_code == 429
    assert response.headers["x-ratelimit-limit"] == "2"
    assert response.headers["x-ratelimit-remaining"] == "0"
