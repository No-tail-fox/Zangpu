from pydantic import BaseModel, ConfigDict

from backend.app.integrations.bifrost.client import BifrostClient


class BifrostPreflightError(RuntimeError):
    pass


class BifrostPreflightReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str
    database_connected: bool
    provider_count: int
    model_count: int
    virtual_key_route_compatible: bool


async def verify_bifrost_preflight(client: BifrostClient, *, expected_version: str) -> BifrostPreflightReport:
    health = await client.get_health()
    version = await client.get_version()
    config = await client.get_config()
    providers = await client.get_providers()
    models = await client.get_models()
    virtual_key_route_compatible = await client.probe_virtual_key_route()

    if health.status != "ok" or not config.is_db_connected:
        raise BifrostPreflightError("BIFROST_PREFLIGHT_FAILED")
    if version != expected_version:
        raise BifrostPreflightError("BIFROST_VERSION_MISMATCH")
    client_config = config.client_config
    if (
        not client_config.enforce_auth_on_inference
        or client_config.allow_direct_keys
        or client_config.allow_per_request_raw_override
    ):
        raise BifrostPreflightError("BIFROST_PREFLIGHT_FAILED")
    if not providers.providers or not models.models or any(
        provider.store_raw_request_response
        or provider.send_back_raw_request
        or provider.send_back_raw_response
        for provider in providers.providers
    ):
        raise BifrostPreflightError("BIFROST_PREFLIGHT_FAILED")
    return BifrostPreflightReport(
        version=version,
        database_connected=config.is_db_connected,
        provider_count=len(providers.providers),
        model_count=len(models.models),
        virtual_key_route_compatible=virtual_key_route_compatible,
    )
