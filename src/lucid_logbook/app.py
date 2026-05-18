"""Litestar application factory."""

from __future__ import annotations

import os
from pathlib import Path

from litestar import Litestar, get
from litestar.di import Provide
from litestar.middleware.base import DefineMiddleware
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from lucid_logbook.api import (
    ImageController,
    LogbookController,
    SearchController,
    SettingsController,
)
from lucid_logbook.apikeys import AuthController
from lucid_logbook.auth import CombinedAuthMiddleware
from lucid_logbook.image_store import ImageStore
from lucid_logbook.models import Base

_DEFAULT_DB_URL = "sqlite+aiosqlite:///logbook.db"


@get("/health")
async def health_check() -> dict[str, str]:
    """Simple health check endpoint."""
    return {"status": "ok"}


def create_app(db_url: str | None = None) -> Litestar:
    """Create and configure the Litestar application.

    Args:
        db_url: SQLAlchemy async database URL.  Falls back to
                ``DATABASE_URL`` env var, then SQLite default.
    """
    if db_url is None:
        db_url = os.environ.get("DATABASE_URL", _DEFAULT_DB_URL)

    engine = create_async_engine(db_url, echo=False)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def on_startup() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

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
            LogbookController,
            SearchController,
            ImageController,
            SettingsController,
            AuthController,
        ],
        on_startup=[on_startup],
        dependencies={"db_session": Provide(provide_db_session)},
        middleware=middleware,
    )
    app.state.image_store = image_store
    return app


app = create_app()
