from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from krauken.api.errors import register_error_handlers
from krauken.api.routers import dev_panel, fermentations, hardware, settings, system
from krauken.api.security import register_security_middleware
from krauken.api.static import mount_frontend


def create_app() -> FastAPI:
    app = FastAPI(title="Krauken API")

    register_error_handlers(app)

    app.include_router(system.router, prefix="/api/v1")
    app.include_router(hardware.router, prefix="/api/v1")
    app.include_router(fermentations.router, prefix="/api/v1")
    app.include_router(settings.router, prefix="/api/v1")
    app.include_router(dev_panel.router, prefix="/api/v1")

    register_security_middleware(app)

    # Dev convenience only -- in prod, one process serves both frontend and
    # API from the same origin, so CORS isn't exercised. Kept permissive for
    # Vite's dev server on a different port during local development.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    mount_frontend(app)
    return app
