"""Mounts the built React app as static files, with an index.html fallback
for client-side routes (so /hardware survives a refresh). One process
serves everything on the Pi -- no nginx. In dev, Vite's own server handles
this instead and proxies /api to us; this mount is a no-op until
`frontend/dist` exists.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from starlette.staticfiles import StaticFiles

log = logging.getLogger("krauken.api.static")

FRONTEND_DIST = Path(__file__).parent / "_static"


def mount_frontend(app: FastAPI) -> None:
    if not FRONTEND_DIST.exists():
        log.warning(
            "%s does not exist -- frontend not built yet. API-only mode "
            "(fine for backend dev; run `npm run build` in frontend/ for a full app).",
            FRONTEND_DIST,
        )

        @app.get("/{full_path:path}")
        async def _no_frontend(full_path: str) -> JSONResponse:
            return JSONResponse(
                status_code=404,
                content={"error": {"code": "frontend_not_built", "message": "frontend/dist not built"}},
            )

        return

    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

    dist_root = FRONTEND_DIST.resolve()

    @app.get("/{full_path:path}")
    async def _spa_fallback(full_path: str) -> FileResponse:
        # Serve any other real build output (favicon.svg, icons.svg, etc.)
        # directly; anything that isn't a real file is a client-side route
        # (e.g. /hardware) and falls back to index.html so a refresh works.
        # full_path comes straight from the URL -- resolve() + a containment
        # check before is_file() so a request like /../../etc/passwd can't
        # escape dist_root (naive `FRONTEND_DIST / full_path` would not
        # sanitize a `..` component, which FileResponse would then happily serve).
        candidate = (dist_root / full_path).resolve()
        if full_path and candidate.is_relative_to(dist_root) and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(dist_root / "index.html")
