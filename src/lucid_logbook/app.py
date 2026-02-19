"""Litestar application factory."""

from __future__ import annotations

from litestar import Litestar, get
from litestar.di import Provide
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from lucid_logbook.api import LogbookController
from lucid_logbook.models import Base

_DEFAULT_DB_URL = "sqlite+aiosqlite:///logbook.db"


@get("/health")
async def health_check() -> dict[str, str]:
    """Simple health check endpoint."""
    return {"status": "ok"}


def create_app(db_url: str = _DEFAULT_DB_URL) -> Litestar:
    """Create and configure the Litestar application."""

    engine = create_async_engine(db_url, echo=False)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def on_startup() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def provide_db_session() -> AsyncSession:
        async with session_factory() as session:
            yield session  # type: ignore[misc]

    return Litestar(
        route_handlers=[health_check, LogbookController],
        on_startup=[on_startup],
        dependencies={"db_session": Provide(provide_db_session)},
    )


app = create_app()
