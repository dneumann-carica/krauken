"""Maps krauken.contracts.errors.KraukenError to HTTP responses. Uniform
shape: {"error": {"code", "message", "details"}}.

Mapped by the error's .code STRING, not by its Python type. An error raised
inside the daemon and returned to the API over IPC arrives back as a plain
KraukenError with .code set from the wire (see krauken.ipc.client) -- the
original subclass never crosses the process boundary, so a type-keyed
mapping would silently miss every daemon-raised error and fall through to
500. Errors raised directly inside the API process (no IPC hop) still carry
their real subclass, but .code is what's authoritative either way.
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from krauken.contracts.errors import KraukenError

_STATUS_BY_CODE: dict[str, int] = {
    "no_active_fermentation": 404,
    "fermentation_already_active": 409,
    "hardware_incomplete": 422,
    "unqualified_assignment": 422,
    "unknown_device": 404,
    "stage_not_running": 409,
    "stale_revision": 409,
    "daemon_unavailable": 503,
    "validation_error": 422,
    "test_already_running": 409,
    "unknown_test": 404,
    "unknown_op": 404,
    "dev_panel_disabled": 403,
}


def _status_for(exc: KraukenError) -> int:
    return _STATUS_BY_CODE.get(exc.code, 500)


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(KraukenError)
    async def _handle(request: Request, exc: KraukenError) -> JSONResponse:
        return JSONResponse(
            status_code=_status_for(exc),
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "details": exc.details,
                }
            },
        )
