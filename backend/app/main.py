"""ASGI entrypoint — the Main component, outermost ring.

It exists to assemble the application and then get out of the way: it depends on
everything, and nothing depends on it.

Run locally with ``uvicorn app.main:app --reload``.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.infrastructure.config import ProcessRole, Settings
from app.infrastructure.logging import configure_logging
from app.interfaces.dependencies import get_settings
from app.interfaces.jobs import router as jobs_router
from app.interfaces.routes import router
from app.interfaces.worker import router as worker_router


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application.

    A factory rather than a module-level singleton so tests can construct an app
    with explicit settings instead of reaching into the environment.
    """
    # First, so that anything logged during the rest of assembly is already
    # formatted. Belongs here for the same reason the rest of this file does:
    # installing a handler is a process-wide side effect, and the outermost ring
    # is the only place allowed to have one.
    configure_logging()

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
            # DELETE because cancelling a job is one. Without it a browser on a
            # different origin can start a reply and then not stop it, which is
            # the one case where failing to cancel costs money.
            allow_methods=["GET", "POST", "DELETE"],
            allow_headers=["Content-Type"],
        )

    if settings is not None:
        # Make the argument mean what it says. Assembly decides what to mount
        # from these settings, while the dependency graph would otherwise go on
        # reading the environment — so an app built with explicit settings could
        # mount the job routes and, in the same breath, tell a client over
        # /api/health that it has none.
        app.dependency_overrides[get_settings] = lambda: settings

    app.include_router(router)

    # Mounted only where there is something behind them. On the server target
    # there is no job store, so these paths genuinely do not exist and say so
    # with a 404 — the same answer /api/health gives when asked which
    # transports it has.
    if resolved.async_replies_enabled:
        app.include_router(jobs_router)

    # The worker's entrypoint, and only for the deployment that is one. This is
    # the whole reason the role is configured rather than inferred: nothing a
    # viewer can reach routes here on either target, but that is a property of
    # a CDN path pattern and a proxy matcher, and an application's surface
    # should not depend on those staying exactly as they are.
    if resolved.role is ProcessRole.WORKER and resolved.async_replies_enabled:
        app.include_router(worker_router)

    return app


app = create_app()
