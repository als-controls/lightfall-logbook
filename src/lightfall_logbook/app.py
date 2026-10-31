"""Litestar application factory."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from litestar import Litestar, Request, get
from litestar.di import Provide
from litestar.middleware.base import DefineMiddleware
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from lightfall_logbook import scoped_models  # noqa: F401  (registers tables)
from lightfall_logbook.api import (
    ImageController,
    LogbookController,
    SearchController,
    SettingsController,
)
from lightfall_logbook.apikeys import AuthController, ensure_api_key_groups_column
from lightfall_logbook.auth import (
    CombinedAuthMiddleware,
    dev_mode_enabled,
    keycloak_auth_enabled,
)
from lightfall_logbook.bundle_store import BundleStore
from lightfall_logbook.content_api import ContentController
from lightfall_logbook.events import LogbookEventPublisher
from lightfall_logbook.image_store import ImageStore
from lightfall_logbook.models import Base
from lightfall_logbook.scoped_api import ScopesController, SettingKeysController
from lightfall_logbook.scoped_config import ScopedConfig, load_scoped_config
from lightfall_logbook.scoped_store import migrate_user_settings, seed_keys, seed_tree
from lightfall_logbook.well_known import build_bootstrap

_DEFAULT_DB_URL = "sqlite+aiosqlite:///logbook.db"


@get("/health")
async def health_check() -> dict[str, str]:
    """Simple health check endpoint."""
    return {"status": "ok"}


@get("/.well-known/lightfall")
async def well_known_lightfall(request: Request, db_session: AsyncSession) -> dict[str, Any]:
    """Facility bootstrap for fresh clients: public facility settings, no auth."""
    return await build_bootstrap(db_session, request.app.state.scoped_config.tree.label)


def create_app(
    db_url: str | None = None,
    event_publisher: LogbookEventPublisher | None = None,
    scoped_config: ScopedConfig | None = None,
) -> Litestar:
    """Create and configure the Litestar application.

    Args:
        db_url: SQLAlchemy async database URL.  Falls back to
                ``DATABASE_URL`` env var, then SQLite default.
        scoped_config: Scope tree / grants / seeds; defaults to ``load_scoped_config()``.
    """
    if db_url is None:
        db_url = os.environ.get("DATABASE_URL", _DEFAULT_DB_URL)

    engine = create_async_engine(db_url, echo=False)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    if event_publisher is None:
        event_publisher = LogbookEventPublisher(os.environ.get("NATS_URL"))

    if scoped_config is None:
        scoped_config = load_scoped_config()

    async def on_startup() -> None:
        if keycloak_auth_enabled():
            if os.environ.get("LOGBOOK_DEV_MODE"):
                logger.warning("LOGBOOK_DEV_MODE is ignored because Keycloak is configured")
        elif dev_mode_enabled():
            logger.warning("DEV MODE: trusting X-User-Id / X-User-Groups headers. Never in prod.")
        else:
            logger.error(
                "No auth configured: set KEYCLOAK_URL and KEYCLOAK_REALM, or LOGBOOK_DEV_MODE=1 "
                "for local development. All authenticated requests will be refused."
            )
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(ensure_api_key_groups_column)
        await seed_tree(session_factory, scoped_config.tree)
        await seed_keys(session_factory, scoped_config.seed)
        moved = await migrate_user_settings(session_factory)
        if moved:
            logger.info("Moved {} legacy user setting(s) into user scope", moved)
        await event_publisher.connect()

    async def on_shutdown() -> None:
        await event_publisher.close()

    async def provide_db_session() -> AsyncSession:
        async with session_factory() as session:
            yield session  # type: ignore[misc]

    # Combined middleware always registers; the dev-mode fallthrough
    # (pass-through when no Authorization header AND Keycloak is unset) is
    # handled inside the middleware itself.
    middleware = [
        DefineMiddleware(CombinedAuthMiddleware, session_factory=session_factory),
    ]

    image_dir = Path(os.environ.get("IMAGE_STORAGE_DIR", "./logbook_images"))
    image_store = ImageStore(storage_dir=image_dir)

    app = Litestar(
        route_handlers=[
            health_check,
            well_known_lightfall,
            LogbookController,
            SearchController,
            ImageController,
            SettingsController,
            AuthController,
            ScopesController,
            ContentController,
            SettingKeysController,
        ],
        on_startup=[on_startup],
        on_shutdown=[on_shutdown],
        dependencies={"db_session": Provide(provide_db_session)},
        middleware=middleware,
    )
    app.state.image_store = image_store
    app.state.bundle_store = BundleStore(
        Path(os.environ.get("CONTENT_STORAGE_DIR", "./content_bundles")))
    app.state.logbook_events = event_publisher
    app.state.scoped_config = scoped_config
    return app


app = create_app()
