"""Litestar REST API for the logbook Entry→Fragment system.

Routes are mounted under ``/logbook``. Authentication is handled by
extracting ``user_id`` from the request state (to be wired to Keycloak
JWT middleware later).
"""

from __future__ import annotations

import uuid
from typing import Any

from litestar import Controller, delete, get, post, put
from litestar.di import Provide
from litestar.exceptions import NotAuthorizedException, NotFoundException, ValidationException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lucid_logbook.models import (
    EntryCreate,
    EntryRow,
    EntrySchema,
    EntryUpdate,
    FragmentCreate,
    FragmentRow,
    FragmentSchema,
    FragmentUpdate,
    LogbookRow,
    LogbookSchema,
)
from loguru import logger


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_user_id(request: Any) -> str:
    """Extract user_id from request.  Placeholder until Keycloak middleware."""
    user_id: str | None = getattr(request, "user", None) or getattr(
        getattr(request, "state", None), "user_id", None
    )
    if not user_id:
        raise NotAuthorizedException("Missing user identity")
    return user_id


async def _get_or_create_logbook(session: AsyncSession, user_id: str) -> LogbookRow:
    result = await session.execute(
        select(LogbookRow).where(LogbookRow.user_id == user_id)
    )
    logbook = result.scalar_one_or_none()
    if logbook is None:
        logbook = LogbookRow(user_id=user_id)
        session.add(logbook)
        await session.flush()
        logger.info("Created new logbook for user {}", user_id)
    return logbook


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------


class LogbookController(Controller):
    """REST endpoints for logbook, entries, and fragments."""

    path = "/logbook"

    @get("/")
    async def get_logbook(self, request: Any, db_session: AsyncSession) -> LogbookSchema:
        user_id = _get_user_id(request)
        logbook = await _get_or_create_logbook(db_session, user_id)
        await db_session.commit()
        return LogbookSchema.model_validate(logbook)

    # -- Entries ------------------------------------------------------------

    @post("/entries")
    async def create_entry(
        self, data: EntryCreate, request: Any, db_session: AsyncSession
    ) -> EntrySchema:
        user_id = _get_user_id(request)
        logbook = await _get_or_create_logbook(db_session, user_id)
        entry = EntryRow(logbook_id=logbook.id, title=data.title, tags=data.tags)
        session_add = db_session.add
        session_add(entry)
        await db_session.flush()
        logger.info("Created entry {} in logbook {}", entry.id, logbook.id)
        await db_session.commit()
        await db_session.refresh(entry)
        return EntrySchema.model_validate(entry)

    @get("/entries")
    async def list_entries(
        self,
        request: Any,
        db_session: AsyncSession,
        sort: str = "created_at",
    ) -> list[EntrySchema]:
        user_id = _get_user_id(request)
        logbook = await _get_or_create_logbook(db_session, user_id)
        order_col = (
            EntryRow.updated_at if sort == "updated_at" else EntryRow.created_at
        )
        result = await db_session.execute(
            select(EntryRow)
            .where(EntryRow.logbook_id == logbook.id)
            .order_by(order_col.desc())
        )
        entries = result.scalars().all()
        await db_session.commit()
        return [EntrySchema.model_validate(e) for e in entries]

    @get("/entries/{entry_id:uuid}")
    async def get_entry(
        self, entry_id: uuid.UUID, request: Any, db_session: AsyncSession
    ) -> EntrySchema:
        _get_user_id(request)
        result = await db_session.execute(
            select(EntryRow).where(EntryRow.id == entry_id)
        )
        entry = result.scalar_one_or_none()
        if entry is None:
            raise NotFoundException(f"Entry {entry_id} not found")
        return EntrySchema.model_validate(entry)

    @put("/entries/{entry_id:uuid}")
    async def update_entry(
        self,
        entry_id: uuid.UUID,
        data: EntryUpdate,
        request: Any,
        db_session: AsyncSession,
    ) -> EntrySchema:
        _get_user_id(request)
        result = await db_session.execute(
            select(EntryRow).where(EntryRow.id == entry_id)
        )
        entry = result.scalar_one_or_none()
        if entry is None:
            raise NotFoundException(f"Entry {entry_id} not found")
        if data.title is not None:
            entry.title = data.title
        if data.tags is not None:
            entry.tags = data.tags
        await db_session.commit()
        await db_session.refresh(entry)
        return EntrySchema.model_validate(entry)

    # -- Fragments ----------------------------------------------------------

    @post("/entries/{entry_id:uuid}/fragments")
    async def create_fragment(
        self,
        entry_id: uuid.UUID,
        data: FragmentCreate,
        request: Any,
        db_session: AsyncSession,
    ) -> FragmentSchema:
        _get_user_id(request)
        result = await db_session.execute(
            select(EntryRow).where(EntryRow.id == entry_id)
        )
        entry = result.scalar_one_or_none()
        if entry is None:
            raise NotFoundException(f"Entry {entry_id} not found")

        # Auto-assign position if not provided
        position = data.position
        if position is None:
            max_pos = max((f.position for f in entry.fragments), default=-1)
            position = max_pos + 1

        fragment = FragmentRow(
            entry_id=entry_id,
            position=position,
            kind=data.kind,
            subtype=data.subtype,
            content=data.content,
            data=data.data,
        )
        db_session.add(fragment)
        await db_session.commit()
        await db_session.refresh(fragment)
        logger.debug("Created fragment {} at position {}", fragment.id, position)
        return FragmentSchema.model_validate(fragment)

    @put("/fragments/{fragment_id:uuid}")
    async def update_fragment(
        self,
        fragment_id: uuid.UUID,
        data: FragmentUpdate,
        request: Any,
        db_session: AsyncSession,
    ) -> FragmentSchema:
        _get_user_id(request)
        result = await db_session.execute(
            select(FragmentRow).where(FragmentRow.id == fragment_id)
        )
        fragment = result.scalar_one_or_none()
        if fragment is None:
            raise NotFoundException(f"Fragment {fragment_id} not found")
        if fragment.kind != "text":
            raise ValidationException("Only text fragments can be edited")
        if data.content is not None:
            fragment.content = data.content
        if data.position is not None:
            fragment.position = data.position
        await db_session.commit()
        await db_session.refresh(fragment)
        return FragmentSchema.model_validate(fragment)

    @delete("/fragments/{fragment_id:uuid}", status_code=204)
    async def delete_fragment(
        self,
        fragment_id: uuid.UUID,
        request: Any,
        db_session: AsyncSession,
    ) -> None:
        _get_user_id(request)
        result = await db_session.execute(
            select(FragmentRow).where(FragmentRow.id == fragment_id)
        )
        fragment = result.scalar_one_or_none()
        if fragment is None:
            raise NotFoundException(f"Fragment {fragment_id} not found")
        if fragment.kind != "text":
            raise ValidationException("Only text fragments can be deleted")
        await db_session.delete(fragment)
        await db_session.commit()
