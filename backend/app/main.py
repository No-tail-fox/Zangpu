from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from pydantic import BaseModel, ConfigDict

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


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        active_settings = settings or load_settings()
        app.state.settings = active_settings
        app.state.credential_keyring = CredentialKeyring.from_json(
            active_settings.api_credential_keys,
            active_key_id=active_settings.api_credential_active_key_id,
        )
        yield

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
