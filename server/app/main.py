"""TMaster server entry point.

Run with:

    uvicorn app.main:app --host 127.0.0.1 --port 8000

or directly:

    python -m app.main
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from tmaster.common import configure_logging, get_logger

from app.api import agent_ws, dashboard_ws, rest
from app.core.config import Settings
from app.core.hub import make_hub
from app.core.store import Store

log = get_logger("server")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    store = Store(settings.db_path)
    await store.connect()
    app.state.store = store

    # Bootstrap user on first run
    if settings.bootstrap_password and await store.count_users() == 0:
        await store.create_user(settings.bootstrap_user, settings.bootstrap_password)
        log.info("bootstrap user created", username=settings.bootstrap_user)

    app.state.hub = make_hub()

    log.info("server started", host=settings.listen_host, port=settings.listen_port)
    try:
        yield
    finally:
        await store.close()


def create_app(settings: Settings | None = None) -> FastAPI:
    configure_logging("server")
    settings = settings or Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    app = FastAPI(title="TMaster Server", version="0.0.1", lifespan=_lifespan)
    app.state.settings = settings

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Routers are built lazily inside lifespan, but we need them at import
    # time for FastAPI route registration. We build them against lazy proxies
    # that read from app.state.
    class _LazyStore:
        def __getattr__(self, item):
            return getattr(app.state.store, item)

    class _LazyHub:
        def __getattr__(self, item):
            return getattr(app.state.hub, item)

    _store = _LazyStore()
    _hub = _LazyHub()

    app.include_router(rest.build_router(settings, _store, _hub))
    app.include_router(agent_ws.build_router(settings, _store, _hub))
    app.include_router(dashboard_ws.build_router(settings, _hub))

    # Static dashboard bundle (if present). Mount at root LAST so /api and
    # /ws take precedence.
    static_dir = Path(__file__).resolve().parent / "static"
    if static_dir.exists() and any(static_dir.iterdir()):
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="dashboard")

    return app


app = create_app()


def main() -> None:
    import uvicorn
    settings = Settings()
    uvicorn.run(
        "app.main:app",
        host=settings.listen_host,
        port=settings.listen_port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
