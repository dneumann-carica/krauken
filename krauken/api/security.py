"""Light anti-CSRF measure for a zero-auth LAN appliance. There's no login
(a stated non-goal), but a no-auth device on the LAN is otherwise fetchable
by any hostile webpage a browser on that network visits -- a malicious page
could `fetch('http://krauken.local/api/v1/fermentations/1/terminate',
{method:'POST'})` and de-energize a running batch with no user action.

Mitigation: mutating requests (anything but GET/HEAD/OPTIONS) must carry a
custom header. A cross-origin form submission or a simple/no-preflight
fetch cannot set a custom header at all -- attempting to forces the browser
into a CORS preflight, which our CORS policy (frontend/dev-server origins
only) then rejects before the request ever reaches this check. This is
enforcement, not just a client-side header nobody checks -- that gap
existed for a while before being caught and fixed here.
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
REQUIRED_HEADER = "x-krauken-client"


class RequireClientHeaderMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method not in SAFE_METHODS and REQUIRED_HEADER not in request.headers:
            return JSONResponse(
                status_code=403,
                content={
                    "error": {
                        "code": "missing_client_header",
                        "message": f"mutating requests must include the {REQUIRED_HEADER} header",
                        "details": {},
                    }
                },
            )
        return await call_next(request)


def register_security_middleware(app: FastAPI) -> None:
    app.add_middleware(RequireClientHeaderMiddleware)
