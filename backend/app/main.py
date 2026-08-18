from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from backend.app.api.admin import AdminApiError, admin_error_response
from backend.app.api.admin import router as admin_router
from backend.app.api.external import router as external_router
from backend.app.database import DatabaseRuntime, create_database_runtime
from backend.app.integrations.bifrost.client import BifrostClient
from backend.app.integrations.bifrost.preflight import BifrostPreflightReport, verify_bifrost_preflight
from backend.app.integrations.openwebui.client import OpenWebUIClient
from backend.app.limits.concurrency import ConcurrencyLimiter
from backend.app.limits.model_pool import ModelPoolLimiter
from backend.app.limits.nonce import NonceGuard
from backend.app.limits.qps import SlidingWindowQps
from backend.app.limits.redis import create_redis_client
from backend.app.security.admin import AdminSessionManager
from backend.app.security.dependencies import ExternalAuthenticator
from backend.app.security.keyring import CredentialKeyring
from backend.app.services.admin import AdminCallerService
from backend.app.services.callers import DatabaseCredentialResolver
from backend.app.services.chat import ExternalChatService
from backend.app.services.metadata import ExternalMetadataService
from backend.app.services.observability import AdminObservabilityService
from backend.app.services.retention import RetentionService
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


def create_openwebui_client(settings: Settings) -> OpenWebUIClient:
    return OpenWebUIClient(
        base_url=str(settings.openwebui_internal_base_url),
        service_id=settings.openwebui_internal_service_id,
        service_secret=settings.openwebui_internal_service_secret,
        timeout_seconds=settings.openwebui_internal_timeout_seconds,
    )


def create_database(settings: Settings) -> DatabaseRuntime:
    return create_database_runtime(str(settings.database_url))


def create_app(
    settings: Settings | None = None,
    *,
    bifrost_client_factory: Callable[[Settings], BifrostClient] = create_bifrost_client,
    bifrost_preflight: Callable[[BifrostClient, str], Awaitable[BifrostPreflightReport | None]] | None = None,
    openwebui_client_factory: Callable[[Settings], OpenWebUIClient] = create_openwebui_client,
    database_factory: Callable[[Settings], DatabaseRuntime] = create_database,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        active_settings = settings or load_settings()
        app.state.settings = active_settings
        app.state.credential_keyring = CredentialKeyring.from_json(
            active_settings.api_credential_keys,
            active_key_id=active_settings.api_credential_active_key_id,
        )
        database_runtime = database_factory(active_settings)
        app.state.database = database_runtime
        app.state.session_factory = database_runtime.sessions
        app.state.admin_sessions = AdminSessionManager(
            session_secret=active_settings.admin_session_secret,
            bootstrap_token=active_settings.admin_login_token,
            ttl_seconds=active_settings.admin_session_ttl_seconds,
        )
        app.state.admin_callers = AdminCallerService(database_runtime.sessions, app.state.credential_keyring)
        app.state.admin_observability = AdminObservabilityService(database_runtime.sessions)
        app.state.admin_retention = RetentionService(
            database_runtime.sessions,
            event_retention_days=active_settings.event_retention_days,
            admin_audit_retention_days=active_settings.admin_audit_retention_days,
            batch_size=active_settings.retention_batch_size,
        )
        redis_client = None
        bifrost_client: BifrostClient | None = None
        openwebui_client: OpenWebUIClient | None = None
        try:
            redis_client = create_redis_client(str(active_settings.redis_url))
            app.state.redis = redis_client
            bifrost_client = bifrost_client_factory(active_settings)
            app.state.bifrost = bifrost_client
            openwebui_client = openwebui_client_factory(active_settings)
            app.state.openwebui = openwebui_client
            app.state.external_authenticator = ExternalAuthenticator(
                keyring=app.state.credential_keyring,
                resolver=DatabaseCredentialResolver(database_runtime.sessions),
                timestamp_tolerance_seconds=active_settings.contract_api_timestamp_tolerance_seconds,
            )
            nonce_guard = NonceGuard(
                redis_client,
                ttl_seconds=active_settings.contract_api_nonce_ttl_seconds,
            )
            qps_limiter = SlidingWindowQps(redis_client)
            concurrency_limiter = ConcurrencyLimiter(
                redis_client,
                lease_seconds=active_settings.contract_api_concurrency_lease_seconds,
            )
            model_pool_limiter = ModelPoolLimiter(
                redis_client,
                lease_seconds=active_settings.contract_api_concurrency_lease_seconds,
            )
            app.state.concurrency_limiter = concurrency_limiter
            app.state.model_pool_limiter = model_pool_limiter
            app.state.external_chat_service = ExternalChatService(
                sessions=database_runtime.sessions,
                keyring=app.state.credential_keyring,
                nonce_guard=nonce_guard,
                qps_limiter=qps_limiter,
                concurrency_limiter=concurrency_limiter,
                model_pool_limiter=model_pool_limiter,
                model_pool_policies=active_settings.model_pool_policies,
                bifrost=bifrost_client,
                openwebui=openwebui_client,
                global_max_output_tokens=active_settings.contract_api_max_output_tokens,
                heartbeat_interval_seconds=active_settings.contract_api_concurrency_heartbeat_seconds,
                global_queue_limit=active_settings.contract_api_global_queue_limit,
                caller_queue_limit=active_settings.contract_api_caller_queue_limit,
                queue_wait_seconds=active_settings.contract_api_queue_wait_seconds,
                queue_poll_milliseconds=active_settings.contract_api_queue_poll_milliseconds,
            )
            app.state.external_metadata_service = ExternalMetadataService(
                sessions=database_runtime.sessions,
                nonce_guard=nonce_guard,
                qps_limiter=qps_limiter,
            )
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
                if openwebui_client is not None:
                    await openwebui_client.aclose()
            finally:
                try:
                    if bifrost_client is not None:
                        await bifrost_client.aclose()
                finally:
                    try:
                        if redis_client is not None:
                            await redis_client.aclose()
                    finally:
                        database_runtime.close()

    application = FastAPI(
        title="Zangpu API Control Plane",
        version=settings.service_version if settings else "unconfigured",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    application.include_router(external_router)
    application.include_router(admin_router)

    @application.exception_handler(AdminApiError)
    async def handle_admin_api_error(_request: Request, exc: AdminApiError) -> JSONResponse:
        return admin_error_response(exc)

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
