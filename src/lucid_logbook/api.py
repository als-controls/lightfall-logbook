"""Litestar REST API for the logbook Entry→Fragment system.

Routes are mounted under ``/logbook``. Authentication is handled by
extracting ``user_id`` from the request state (to be wired to Keycloak
JWT middleware later).
"""

from __future__ import annotations

import uuid
from typing import Any

from litestar import Controller, Request, delete, get, post, put
from litestar.datastructures import UploadFile
from litestar.di import Provide
from litestar.enums import RequestEncodingType
from litestar.exceptions import NotAuthorizedException, NotFoundException, ValidationException
from litestar.params import Body
from litestar.response import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lucid_logbook.image_store import ImageStore, ImageStoreError

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
    UserSettingRow,
    UserSettingSchema,
    UserSettingWrite,
)
from loguru import logger
from lucid_logbook.auth import keycloak_auth_enabled


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_user_id(request: Any) -> str:
    """Extract user_id from request state (set by Keycloak middleware).

    Falls back to ``X-User-Id`` header for dev/testing when auth is disabled.
    """
    # From Keycloak middleware
    user_id: str | None = getattr(
        getattr(request, "state", None), "user_id", None
    )
    if user_id:
        return user_id

    # Dev fallback: allow header-based identity
    user_id = request.headers.get("X-User-Id")
    if user_id:
        return user_id

    # When Keycloak is disabled, fall back to a default dev user
    if not keycloak_auth_enabled():
        return "dev-user"

    raise NotAuthorizedException("Missing user identity")


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
        kwargs: dict[str, Any] = {"logbook_id": logbook.id, "title": data.title, "tags": data.tags}
        if data.id is not None:
            kwargs["id"] = data.id
        entry = EntryRow(**kwargs)
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

        frag_kwargs: dict[str, Any] = {
            "entry_id": entry_id,
            "position": position,
            "kind": data.kind,
            "subtype": data.subtype,
            "content": data.content,
            "data": data.data,
        }
        if data.id is not None:
            frag_kwargs["id"] = data.id
        fragment = FragmentRow(**frag_kwargs)
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
        if fragment.kind not in ("text", "image"):
            raise ValidationException("Only text and image fragments can be deleted")

        # Clean up image file if this is an image fragment
        if fragment.kind == "image" and fragment.data and "image_id" in fragment.data:
            image_store: ImageStore = request.app.state.image_store
            image_store.delete(fragment.data["image_id"])

        await db_session.delete(fragment)
        await db_session.commit()

    @delete("/entries/{entry_id:uuid}", status_code=204)
    async def delete_entry(
        self,
        entry_id: uuid.UUID,
        request: Any,
        db_session: AsyncSession,
    ) -> None:
        _get_user_id(request)
        result = await db_session.execute(
            select(EntryRow).where(EntryRow.id == entry_id)
        )
        entry = result.scalar_one_or_none()
        if entry is None:
            raise NotFoundException(f"Entry {entry_id} not found")
        await db_session.delete(entry)
        await db_session.commit()


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


class SearchController(Controller):
    """Full-text search across fragment content."""

    path = "/logbook/search"

    @get("/")
    async def search_fragments(
        self,
        q: str,
        request: Any,
        db_session: AsyncSession,
        limit: int = 50,
    ) -> list[FragmentSchema]:
        """Search fragment content (case-insensitive LIKE).

        For Postgres, upgrade to ``to_tsvector``/``to_tsquery`` for proper
        full-text search. SQLite falls back to LIKE.
        """
        user_id = _get_user_id(request)
        logbook = await _get_or_create_logbook(db_session, user_id)

        # Join fragments through entries to scope to this user's logbook
        from sqlalchemy import func

        pattern = f"%{q}%"
        result = await db_session.execute(
            select(FragmentRow)
            .join(EntryRow, FragmentRow.entry_id == EntryRow.id)
            .where(EntryRow.logbook_id == logbook.id)
            .where(FragmentRow.content.ilike(pattern))
            .order_by(FragmentRow.updated_at.desc())
            .limit(limit)
        )
        fragments = result.scalars().all()
        await db_session.commit()
        return [FragmentSchema.model_validate(f) for f in fragments]


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------


class ImageController(Controller):
    """Image upload/download/delete endpoints."""

    path = "/logbook/images"

    @post("/", status_code=201)
    async def upload_image(
        self,
        request: Request,
        data: UploadFile = Body(media_type=RequestEncodingType.MULTI_PART),
    ) -> dict:
        image_store: ImageStore = request.app.state.image_store
        content = await data.read()
        mime_type = data.content_type or "application/octet-stream"

        try:
            image_id = image_store.save(content, mime_type)
        except ImageStoreError as e:
            raise ValidationException(str(e))

        return {
            "image_id": image_id,
            "mime_type": mime_type,
            "size_bytes": len(content),
        }

    @get("/{image_id:str}")
    async def download_image(self, request: Request, image_id: str) -> Response:
        image_store: ImageStore = request.app.state.image_store

        try:
            data, mime_type = image_store.load(image_id)
        except ImageStoreError:
            raise NotFoundException(f"Image not found: {image_id}")

        return Response(
            content=data,
            media_type=mime_type,
            headers={"Cache-Control": "max-age=86400"},
        )

    @delete("/{image_id:str}", status_code=204)
    async def delete_image(self, request: Request, image_id: str) -> None:
        image_store: ImageStore = request.app.state.image_store
        deleted = image_store.delete(image_id)
        if not deleted:
            raise NotFoundException(f"Image not found: {image_id}")


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


async def _run_settings_post_write_hook(
    *,
    request: Any,
    user_id: str,
    beamline: str,
    key: str,
    old_value: Any,
    new_value: Any,
) -> None:
    """Dispatch to any registered post-write hook for a setting key.

    Task 4 plugs the profile_image_id hook in here.
    """
    return None


class SettingsController(Controller):
    """Per-user key/value settings, optionally scoped to a beamline."""

    path = "/logbook/settings"

    @get("/")
    async def list_settings(
        self,
        request: Any,
        db_session: AsyncSession,
        beamline: str = "",
    ) -> dict[str, Any]:
        """Return {key: value, ...} for the requesting user in this scope."""
        user_id = _get_user_id(request)
        result = await db_session.execute(
            select(UserSettingRow)
            .where(UserSettingRow.user_id == user_id)
            .where(UserSettingRow.beamline == beamline)
        )
        rows = result.scalars().all()
        await db_session.commit()
        return {row.key: row.value for row in rows}

    @get("/{key:str}")
    async def get_setting(
        self,
        key: str,
        request: Any,
        db_session: AsyncSession,
        beamline: str = "",
    ) -> UserSettingSchema:
        user_id = _get_user_id(request)
        result = await db_session.execute(
            select(UserSettingRow).where(
                UserSettingRow.user_id == user_id,
                UserSettingRow.beamline == beamline,
                UserSettingRow.key == key,
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise NotFoundException(f"Setting {key!r} not found")
        await db_session.commit()
        return UserSettingSchema.model_validate(row)

    @put("/{key:str}")
    async def put_setting(
        self,
        key: str,
        data: UserSettingWrite,
        request: Any,
        db_session: AsyncSession,
    ) -> UserSettingSchema:
        user_id = _get_user_id(request)
        # TODO: SELECT-then-INSERT/UPDATE is not atomic under Postgres;
        # rewrite as ON CONFLICT DO UPDATE before Postgres promotion.
        # SQLite serializes writes so this is safe in current deployments.
        result = await db_session.execute(
            select(UserSettingRow).where(
                UserSettingRow.user_id == user_id,
                UserSettingRow.beamline == data.beamline,
                UserSettingRow.key == key,
            )
        )
        row = result.scalar_one_or_none()
        old_value = row.value if row is not None else None

        if row is None:
            row = UserSettingRow(
                user_id=user_id,
                beamline=data.beamline,
                key=key,
                value=data.value,
            )
            db_session.add(row)
        else:
            row.value = data.value
            # updated_at refreshes via the column's onupdate hook on commit

        await db_session.commit()
        await db_session.refresh(row)

        # Run any post-write hook registered for this key. Hook failures
        # MUST NOT fail the response — the write is already durable, and
        # hooks handle side-effects (e.g., orphan-blob cleanup) that are
        # safer to log-and-move-on than to surface as a 500.
        try:
            await _run_settings_post_write_hook(
                request=request,
                user_id=user_id,
                beamline=data.beamline,
                key=key,
                old_value=old_value,
                new_value=data.value,
            )
        except Exception:
            logger.exception(
                "Post-write hook failed for key={!r} user={!r}; "
                "write already committed",
                key,
                user_id,
            )
        return UserSettingSchema.model_validate(row)
