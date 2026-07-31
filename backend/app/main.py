from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from pydantic import BaseModel, ConfigDict

from backend.app.integrations.bifrost.client import BifrostClient
from backend.app.integrations.bifrost.preflight import BifrostPreflightReport, verify_bifrost_preflight
from backend.app.limits.redis import create_redis_client
from backend.app.security.keyring import CredentialKeyring
from backend.app.settings import Settings, load_settings

SERVICE_NAME = "zangpu-api-control-plane"
API_VERSION = "v1"


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: str
    service: str
    version: str
    api_version: str


def create_bifrost_client(settings: Settings) -> BifrostClient:
    return BifrostClient(
        base_url=str(settings.bifrost_base_url),
        management_token=settings.bifrost_management_token,
        timeout_seconds=settings.bifrost_timeout_seconds,
    )


def create_app(
    settings: Settings | None = None,
    *,
    bifrost_client_factory: Callable[[Settings], BifrostClient] = create_bifrost_client,
    bifrost_preflight: Callable[[BifrostClient, str], Awaitable[BifrostPreflightReport | None]] | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        active_settings = settings or load_settings()
        app.state.settings = active_settings
        app.state.credential_keyring = CredentialKeyring.from_json(
            active_settings.api_credential_keys,
            active_key_id=active_settings.api_credential_active_key_id,
        )
        redis_client = create_redis_client(str(active_settings.redis_url))
        app.state.redis = redis_client
        bifrost_client: BifrostClient | None = None
        try:
            bifrost_client = bifrost_client_factory(active_settings)
            app.state.bifrost = bifrost_client
            if bifrost_preflight is None:
                app.state.bifrost_preflight = await verify_bifrost_preflight(
                    bifrost_client, expected_version=active_settings.bifrost_expected_version
                )
            else:
                app.state.bifrost_preflight = await bifrost_preflight(
                    bifrost_client, active_settings.bifrost_expected_version
                )
            yield
        finally:
            try:
                if bifrost_client is not None:
                    await bifrost_client.aclose()
            finally:
                await redis_client.aclose()

    application = FastAPI(
        title="Zangpu API Control Plane",
        version=settings.service_version if settings else "unconfigured",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @application.get("/api/v1/external/health", response_model=HealthResponse)
    async def health(request: Request) -> HealthResponse:
        active_settings: Settings = request.app.state.settings
        return HealthResponse(
            status="ready",
            service=SERVICE_NAME,
            version=active_settings.service_version,
            api_version=API_VERSION,
        )

    return application


app = create_app()
