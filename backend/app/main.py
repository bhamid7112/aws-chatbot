"""ASGI entrypoint — the Main component, outermost ring.

It exists to assemble the application and then get out of the way: it depends on
everything, and nothing depends on it.

Run locally with ``uvicorn app.main:app --reload``.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.infrastructure.config import Settings
from app.interfaces.dependencies import get_settings
from app.interfaces.routes import router


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application.

    A factory rather than a module-level singleton so tests can construct an app
    with explicit settings instead of reaching into the environment.
    """
    resolved = settings or get_settings()

    app = FastAPI(
        title="AWS Chatbot API",
        version="0.1.0",
        summary="Streams a chat reply over Server-Sent Events.",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    # In production Caddy serves the bundle and the API from one origin, so the
    # allow-list is empty and the middleware is not installed at all.
    if resolved.cors_allow_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(resolved.cors_allow_origins),
            allow_methods=["GET", "POST"],
            allow_headers=["Content-Type"],
        )

    app.include_router(router)
    return app


app = create_app()
