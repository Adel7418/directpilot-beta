from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import log_api_error


def is_api_v1_path(path: str) -> bool:
    return path == "/api/v1" or path.startswith("/api/v1/")


def _request_id(request: Request) -> str:
    context = getattr(request.state, "request_context", None)
    return getattr(context, "request_id", "unknown")


def safe_error_response(
    *, request: Request, status_code: int, code: str
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": "Request failed",
                "request_id": _request_id(request),
            }
        },
    )


async def api_http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> Response:
    if not is_api_v1_path(request.url.path):
        return await http_exception_handler(request, exc)
    log_api_error(request_id=_request_id(request), status_code=exc.status_code)
    return safe_error_response(
        request=request,
        status_code=exc.status_code,
        code="http_error",
    )


async def api_validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> Response:
    if not is_api_v1_path(request.url.path):
        return await request_validation_exception_handler(request, exc)
    log_api_error(request_id=_request_id(request), status_code=422)
    return safe_error_response(
        request=request,
        status_code=422,
        code="validation_error",
    )


def install_error_handlers(app: Any) -> None:
    app.add_exception_handler(StarletteHTTPException, api_http_exception_handler)
    app.add_exception_handler(RequestValidationError, api_validation_exception_handler)
