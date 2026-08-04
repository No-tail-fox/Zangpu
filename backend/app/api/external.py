from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import ValidationError

from backend.app.api.errors import ExternalApiError, external_error_response
from backend.app.api.external_models import ChatCompletionRequest
from backend.app.limits.redis import ControlPlaneUnavailable
from backend.app.security.canonical import body_sha256_hex
from backend.app.security.dependencies import (
    ExternalAuthFailure,
    ExternalClientDisabled,
    authenticate_http_request,
)
from backend.app.services.chat import ExternalChatService
from backend.app.services.metadata import ExternalMetadataService

MAX_EXTERNAL_BODY_BYTES = 1024 * 1024

router = APIRouter(prefix="/api/v1/external", tags=["external"])


def new_server_request_id() -> str:
    return f"req_{uuid4().hex}"


async def _bounded_json_body(request: Request) -> bytes:
    content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if content_type != "application/json":
        raise ExternalApiError("INVALID_REQUEST")
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError as exc:
            raise ExternalApiError("INVALID_REQUEST") from exc
        if declared_length < 0:
            raise ExternalApiError("INVALID_REQUEST")
        if declared_length > MAX_EXTERNAL_BODY_BYTES:
            raise ExternalApiError("PAYLOAD_TOO_LARGE")
    body = await request.body()
    if len(body) > MAX_EXTERNAL_BODY_BYTES:
        raise ExternalApiError("PAYLOAD_TOO_LARGE")
    if not body:
        raise ExternalApiError("INVALID_REQUEST")
    return body


async def _external_metadata_response(request: Request, *, resource: str) -> Response:
    server_request_id = new_server_request_id()
    try:
        caller = await authenticate_http_request(
            request,
            request.app.state.external_authenticator,
        )
        if request.scope.get("query_string", b"") or await request.body():
            raise ExternalApiError("INVALID_REQUEST")
        service: ExternalMetadataService = request.app.state.external_metadata_service
        if resource == "models":
            result = await service.list_models(caller=caller, server_request_id=server_request_id)
        elif resource == "usage":
            result = await service.get_usage(caller=caller, server_request_id=server_request_id)
        else:
            raise RuntimeError("unknown external metadata resource")
        return JSONResponse(
            content=result.response.model_dump(mode="json"),
            headers={
                "Cache-Control": "no-store",
                "X-Zangpu-Request-Id": server_request_id,
                **result.rate_limit_headers,
            },
        )
    except ExternalApiError as exc:
        return exc.to_response(server_request_id)
    except ExternalAuthFailure:
        return external_error_response("AUTH_FAILED", server_request_id=server_request_id)
    except ExternalClientDisabled:
        return external_error_response("CLIENT_DISABLED", server_request_id=server_request_id)
    except ControlPlaneUnavailable:
        return external_error_response("CONTROL_PLANE_UNAVAILABLE", server_request_id=server_request_id)
    except Exception:
        return external_error_response("INTERNAL_ERROR", server_request_id=server_request_id)


@router.get("/models")
async def external_models(request: Request) -> Response:
    return await _external_metadata_response(request, resource="models")


@router.get("/usage")
async def external_usage(request: Request) -> Response:
    return await _external_metadata_response(request, resource="usage")


@router.post("/chat/completions")
async def external_chat_completion(request: Request) -> Response:
    server_request_id = new_server_request_id()
    try:
        body = await _bounded_json_body(request)
        try:
            chat_request = ChatCompletionRequest.model_validate_json(body)
        except ValidationError as exc:
            raise ExternalApiError("INVALID_REQUEST") from exc

        caller = await authenticate_http_request(
            request,
            request.app.state.external_authenticator,
        )
        service: ExternalChatService = request.app.state.external_chat_service
        if chat_request.stream:
            result = await service.prepare_stream(
                request=chat_request,
                caller=caller,
                server_request_id=server_request_id,
                request_fingerprint=body_sha256_hex(body),
            )
            return StreamingResponse(
                result.stream,
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-store, no-transform",
                    "X-Accel-Buffering": "no",
                    "X-Zangpu-Request-Id": server_request_id,
                    **result.rate_limit_headers,
                },
            )
        result = await service.execute(
            request=chat_request,
            caller=caller,
            server_request_id=server_request_id,
            request_fingerprint=body_sha256_hex(body),
        )
        return JSONResponse(
            content=result.response.model_dump(mode="json", exclude_none=True),
            headers={
                "Cache-Control": "no-store",
                "X-Zangpu-Request-Id": server_request_id,
                **result.rate_limit_headers,
            },
        )
    except ExternalApiError as exc:
        return exc.to_response(server_request_id)
    except ExternalAuthFailure:
        return external_error_response("AUTH_FAILED", server_request_id=server_request_id)
    except ExternalClientDisabled:
        return external_error_response("CLIENT_DISABLED", server_request_id=server_request_id)
    except ControlPlaneUnavailable:
        return external_error_response("CONTROL_PLANE_UNAVAILABLE", server_request_id=server_request_id)
    except Exception:
        return external_error_response("INTERNAL_ERROR", server_request_id=server_request_id)
