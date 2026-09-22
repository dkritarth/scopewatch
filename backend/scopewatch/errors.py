"""Error definitions and sanitized exception handlers for the Scopewatch API."""

import logging
from typing import Any, Optional
import uuid
from fastapi import Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger("scopewatch.errors")


class ScopewatchAPIError(Exception):
    """Standardized API exception returning a structured JSON error envelope."""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = status.HTTP_400_BAD_REQUEST,
        request_id: Optional[str] = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.request_id = request_id or str(uuid.uuid4())

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "request_id": self.request_id,
            }
        }


async def scopewatch_api_error_handler(request: Request, exc: ScopewatchAPIError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=exc.to_dict(),
    )


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    request_id = str(uuid.uuid4())
    first_error = exc.errors()[0] if exc.errors() else {"msg": "Validation failed"}
    msg = f"{first_error.get('loc', ['body'])[-1]}: {first_error.get('msg', 'Invalid input')}"
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "error": {
                "code": "SCHEMA_VALIDATION_ERROR",
                "message": msg,
                "request_id": request_id,
            }
        },
    )


async def generic_error_handler(request: Request, exc: Exception) -> JSONResponse:
    request_id = str(uuid.uuid4())
    logger.exception("Sanitized unexpected error [%s]: %s", request_id, type(exc).__name__)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": {
                "code": "INTERNAL_SERVER_ERROR",
                "message": "An unexpected error occurred. Raw exception details are withheld.",
                "request_id": request_id,
            }
        },
    )
