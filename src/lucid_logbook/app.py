"""Litestar application factory."""

from __future__ import annotations

import os

from litestar import Litestar, get
from litestar.di import Provide
from litestar.middleware.base import DefineMiddleware
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from lucid_logbook.api import LogbookController, SearchController
from lucid_logbook.auth import KeycloakAuthMiddleware, keycloak_auth_enabled
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

    middleware = []
    if keycloak_auth_enabled():
        middleware.append(DefineMiddleware(KeycloakAuthMiddleware))

    return Litestar(
        route_handlers=[health_check, LogbookController, SearchController],
        on_startup=[on_startup],
        dependencies={"db_session": Provide(provide_db_session)},
        middleware=middleware,
    )


app = create_app()
