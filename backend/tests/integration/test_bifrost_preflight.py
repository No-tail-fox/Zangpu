import asyncio

import httpx
import pytest
from pydantic import SecretStr

from backend.app.integrations.bifrost.client import BifrostClient
from backend.app.integrations.bifrost.preflight import BifrostPreflightError, verify_bifrost_preflight


def build_transport(*, version: str = "v1.6.3", insecure_flag: str | None = None) -> httpx.MockTransport:
    config = {
        "client_config": {
            "enforce_auth_on_inference": True,
            "allow_direct_keys": False,
            "allow_per_request_raw_override": False,
        },
        "is_db_connected": True,
    }
    providers = {
        "providers": [
            {
                "name": "provider-1",
                "store_raw_request_response": False,
                "send_back_raw_request": False,
                "send_back_raw_response": False,
            }
        ],
        "total": 1,
    }
    if insecure_flag in config["client_config"]:
        config["client_config"][insecure_flag] = not config["client_config"][insecure_flag]
    if insecure_flag in providers["providers"][0]:
        providers["providers"][0][insecure_flag] = True

    def handler(request: httpx.Request) -> httpx.Response:
        responses = {
            "/health": {"status": "ok", "components": {"db_pings": "ok"}},
            "/api/version": version,
            "/api/config": config,
            "/api/providers": providers,
            "/api/models": {"models": [{"name": "model-1", "provider": "provider-1"}], "total": 1},
            "/api/governance/virtual-keys": {
                "virtual_keys": [],
                "count": 0,
                "total_count": 0,
                "limit": 0,
                "offset": 0,
            },
        }
        return httpx.Response(200, json=responses[request.url.path])

    return httpx.MockTransport(handler)


def test_preflight_verifies_version_routes_and_security_privacy_invariants() -> None:
    async def scenario() -> object:
        client = BifrostClient(
            base_url="http://bifrost:8080",
            management_token=SecretStr("management-token-redaction-sentinel-value"),
            transport=build_transport(),
        )
        report = await verify_bifrost_preflight(client, expected_version="v1.6.3")
        await client.aclose()
        return report

    report = asyncio.run(scenario())
    assert report.model_dump() == {
        "version": "v1.6.3",
        "database_connected": True,
        "provider_count": 1,
        "model_count": 1,
        "virtual_key_route_compatible": True,
    }


@pytest.mark.parametrize(
    "insecure_flag",
    [
        "enforce_auth_on_inference",
        "allow_direct_keys",
        "allow_per_request_raw_override",
        "store_raw_request_response",
        "send_back_raw_request",
        "send_back_raw_response",
    ],
)
def test_preflight_fails_closed_for_every_security_or_privacy_flag(insecure_flag: str) -> None:
    async def scenario() -> None:
        client = BifrostClient(
            base_url="http://bifrost:8080",
            management_token=SecretStr("management-token-redaction-sentinel-value"),
            transport=build_transport(insecure_flag=insecure_flag),
        )
        with pytest.raises(BifrostPreflightError, match="BIFROST_PREFLIGHT_FAILED"):
            await verify_bifrost_preflight(client, expected_version="v1.6.3")
        await client.aclose()

    asyncio.run(scenario())


def test_preflight_rejects_an_unpinned_version() -> None:
    async def scenario() -> None:
        client = BifrostClient(
            base_url="http://bifrost:8080",
            management_token=SecretStr("management-token-redaction-sentinel-value"),
            transport=build_transport(version="v9.9.9"),
        )
        with pytest.raises(BifrostPreflightError, match="BIFROST_VERSION_MISMATCH"):
            await verify_bifrost_preflight(client, expected_version="v1.6.3")
        await client.aclose()

    asyncio.run(scenario())
