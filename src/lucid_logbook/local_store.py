"""Local SQLite storage for offline logbook mode.

Uses aiosqlite via SQLAlchemy async to persist entries and fragments
locally. Tracks unsynced changes via a ``sync_status`` column on each
row (``synced`` | ``pending`` | ``deleted``). Provides push/pull sync
with last-write-wins conflict resolution per fragment.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from lucid_logbook.models import (
    Base,
    EntryRow,
    EntrySchema,
    FragmentRow,
    FragmentSchema,
    LogbookRow,
    LogbookSchema,
)
from loguru import logger

_DEFAULT_DB = Path.home() / ".lucid" / "logbook.db"


class LocalStore:
    """Async local SQLite store for logbook data."""

    def __init__(self, db_path: Path | str = _DEFAULT_DB) -> None:
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._engine = create_async_engine(
            f"sqlite+aiosqlite:///{db_path}", echo=False
        )
        self._session_factory = async_sessionmaker(
            self._engine, expire_on_commit=False
        )

    async def init_db(self) -> None:
        """Create tables if they don't exist."""
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Local logbook DB initialized")

    def session(self) -> AsyncSession:
        return self._session_factory()

    # -- Logbook ------------------------------------------------------------

    async def get_or_create_logbook(self, user_id: str) -> LogbookSchema:
        async with self.session() as s:
            result = await s.execute(
                select(LogbookRow).where(LogbookRow.user_id == user_id)
            )
            row = result.scalar_one_or_none()
            if row is None:
                row = LogbookRow(user_id=user_id)
                s.add(row)
                await s.commit()
                await s.refresh(row)
            return LogbookSchema.model_validate(row)

    # -- Entries ------------------------------------------------------------

    async def create_entry(
        self,
        logbook_id: uuid.UUID,
        title: str | None = None,
        tags: list[str] | None = None,
    ) -> EntrySchema:
        async with self.session() as s:
            entry = EntryRow(
                logbook_id=logbook_id,
                title=title,
                tags=tags or [],
                sync_status="pending",
            )
            s.add(entry)
            await s.commit()
            await s.refresh(entry)
            return EntrySchema.model_validate(entry)

    async def list_entries(
        self, logbook_id: uuid.UUID, sort: str = "created_at"
    ) -> list[EntrySchema]:
        async with self.session() as s:
            order = EntryRow.updated_at if sort == "updated_at" else EntryRow.created_at
            result = await s.execute(
                select(EntryRow)
                .where(EntryRow.logbook_id == logbook_id, EntryRow.sync_status != "deleted")
                .order_by(order.desc())
            )
            return [EntrySchema.model_validate(e) for e in result.scalars().all()]

    async def get_entry(self, entry_id: uuid.UUID) -> EntrySchema | None:
        async with self.session() as s:
            result = await s.execute(
                select(EntryRow).where(EntryRow.id == entry_id)
            )
            row = result.scalar_one_or_none()
            return EntrySchema.model_validate(row) if row else None

    async def update_entry(
        self,
        entry_id: uuid.UUID,
        title: str | None = None,
        tags: list[str] | None = None,
    ) -> EntrySchema | None:
        async with self.session() as s:
            result = await s.execute(
                select(EntryRow).where(EntryRow.id == entry_id)
            )
            row = result.scalar_one_or_none()
            if row is None:
                return None
            if title is not None:
                row.title = title
            if tags is not None:
                row.tags = tags
            row.sync_status = "pending"
            row.updated_at = datetime.now(UTC)
            await s.commit()
            await s.refresh(row)
            return EntrySchema.model_validate(row)

    # -- Fragments ----------------------------------------------------------

    async def add_fragment(
        self,
        entry_id: uuid.UUID,
        kind: str = "text",
        content: str = "",
        subtype: str | None = None,
        data: dict[str, Any] | None = None,
        position: int | None = None,
    ) -> FragmentSchema:
        async with self.session() as s:
            if position is None:
                result = await s.execute(
                    select(FragmentRow)
                    .where(FragmentRow.entry_id == entry_id)
                    .order_by(FragmentRow.position.desc())
                    .limit(1)
                )
                last = result.scalar_one_or_none()
                position = (last.position + 1) if last else 0

            frag = FragmentRow(
                entry_id=entry_id,
                position=position,
                kind=kind,
                subtype=subtype,
                content=content,
                data=data,
                sync_status="pending",
            )
            s.add(frag)
            await s.commit()
            await s.refresh(frag)
            return FragmentSchema.model_validate(frag)

    async def update_fragment(
        self,
        fragment_id: uuid.UUID,
        content: str | None = None,
        position: int | None = None,
    ) -> FragmentSchema | None:
        async with self.session() as s:
            result = await s.execute(
                select(FragmentRow).where(FragmentRow.id == fragment_id)
            )
            row = result.scalar_one_or_none()
            if row is None or row.kind != "text":
                return None
            if content is not None:
                row.content = content
            if position is not None:
                row.position = position
            row.sync_status = "pending"
            row.updated_at = datetime.now(UTC)
            await s.commit()
            await s.refresh(row)
            return FragmentSchema.model_validate(row)

    async def delete_fragment(self, fragment_id: uuid.UUID) -> bool:
        async with self.session() as s:
            result = await s.execute(
                select(FragmentRow).where(FragmentRow.id == fragment_id)
            )
            row = result.scalar_one_or_none()
            if row is None or row.kind != "text":
                return False
            row.sync_status = "deleted"
            await s.commit()
            return True

    # -- Sync ---------------------------------------------------------------

    async def sync_to_server(self, client: Any) -> int:
        """Push locally-pending changes to the remote server.

        ``client`` is expected to be an httpx.AsyncClient (or similar)
        pointing at the logbook API base URL.  Returns the number of
        items pushed.
        """
        pushed = 0
        async with self.session() as s:
            # Push pending entries
            result = await s.execute(
                select(EntryRow).where(EntryRow.sync_status == "pending")
            )
            for entry in result.scalars().all():
                try:
                    resp = await client.post(
                        "/logbook/entries",
                        json={
                            "title": entry.title,
                            "tags": entry.tags,
                        },
                    )
                    resp.raise_for_status()
                    entry.sync_status = "synced"
                    pushed += 1
                except Exception:
                    logger.warning("Failed to sync entry {}", entry.id)

            # Push pending fragments
            result = await s.execute(
                select(FragmentRow).where(FragmentRow.sync_status == "pending")
            )
            for frag in result.scalars().all():
                try:
                    resp = await client.post(
                        f"/logbook/entries/{frag.entry_id}/fragments",
                        json={
                            "kind": frag.kind,
                            "subtype": frag.subtype,
                            "content": frag.content,
                            "data": frag.data,
                            "position": frag.position,
                        },
                    )
                    resp.raise_for_status()
                    frag.sync_status = "synced"
                    pushed += 1
                except Exception:
                    logger.warning("Failed to sync fragment {}", frag.id)

            # Handle soft-deleted fragments
            result = await s.execute(
                select(FragmentRow).where(FragmentRow.sync_status == "deleted")
            )
            for frag in result.scalars().all():
                try:
                    resp = await client.delete(f"/logbook/fragments/{frag.id}")
                    resp.raise_for_status()
                    await s.delete(frag)
                    pushed += 1
                except Exception:
                    logger.warning("Failed to delete remote fragment {}", frag.id)

            await s.commit()
        logger.info("Synced {} items to server", pushed)
        return pushed

    async def sync_from_server(self, client: Any) -> int:
        """Pull latest data from remote server.  Last-write-wins per fragment.

        Returns the number of items updated locally.
        """
        updated = 0
        async with self.session() as s:
            # Fetch entries
            try:
                resp = await client.get("/logbook/entries")
                resp.raise_for_status()
                remote_entries: list[dict[str, Any]] = resp.json()
            except Exception:
                logger.warning("Failed to fetch remote entries")
                return 0

            for re in remote_entries:
                entry_id = uuid.UUID(re["id"])
                result = await s.execute(
                    select(EntryRow).where(EntryRow.id == entry_id)
                )
                local = result.scalar_one_or_none()
                remote_updated = datetime.fromisoformat(re["updated_at"])

                if local is None:
                    entry = EntryRow(
                        id=entry_id,
                        logbook_id=uuid.UUID(re["logbook_id"]),
                        title=re.get("title"),
                        tags=re.get("tags", []),
                        created_at=datetime.fromisoformat(re["created_at"]),
                        updated_at=remote_updated,
                        sync_status="synced",
                    )
                    s.add(entry)
                    updated += 1
                elif local.sync_status == "synced" and remote_updated > local.updated_at:
                    local.title = re.get("title")
                    local.tags = re.get("tags", [])
                    local.updated_at = remote_updated
                    updated += 1

                # Fetch fragments for this entry
                try:
                    fresp = await client.get(f"/logbook/entries/{entry_id}")
                    fresp.raise_for_status()
                    remote_frags: list[dict[str, Any]] = fresp.json().get("fragments", [])
                except Exception:
                    continue

                for rf in remote_frags:
                    frag_id = uuid.UUID(rf["id"])
                    fresult = await s.execute(
                        select(FragmentRow).where(FragmentRow.id == frag_id)
                    )
                    local_frag = fresult.scalar_one_or_none()
                    rf_updated = datetime.fromisoformat(rf["updated_at"])

                    if local_frag is None:
                        frag = FragmentRow(
                            id=frag_id,
                            entry_id=entry_id,
                            position=rf["position"],
                            kind=rf["kind"],
                            subtype=rf.get("subtype"),
                            content=rf.get("content", ""),
                            data=rf.get("data"),
                            created_at=datetime.fromisoformat(rf["created_at"]),
                            updated_at=rf_updated,
                            sync_status="synced",
                        )
                        s.add(frag)
                        updated += 1
                    elif local_frag.sync_status == "synced" and rf_updated > local_frag.updated_at:
                        local_frag.content = rf.get("content", "")
                        local_frag.data = rf.get("data")
                        local_frag.position = rf["position"]
                        local_frag.updated_at = rf_updated
                        updated += 1

            await s.commit()
        logger.info("Pulled {} updates from server", updated)
        return updated
