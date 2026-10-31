"""Shared content API: plans, plugins, agents and skills published to a scope.

Bundles are zips the client makes; the server stores and serves their bytes and never
opens them. Items point at their current immutable revision; retract clears the pointer.
Read: any signed-in caller, for any facility/beamline/branch scope in the tree.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from lightfall_settings import role_at_least, role_on
from litestar import Controller, Request, Response, get, post
from litestar.datastructures import UploadFile
from litestar.enums import RequestEncodingType
from litestar.exceptions import HTTPException, NotFoundException, PermissionDeniedException
from litestar.params import Body
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lightfall_logbook.bundle_store import BundleStore
from lightfall_logbook.scoped_api import (
    _caller,
    _cfg,
    _chain,
    _is_agent,
    _parse_scope,
    _publish,
    _tree,
    _unprocessable,
)
from lightfall_logbook.scoped_config import ContentPolicy
from lightfall_logbook.scoped_models import ContentItemRow, ContentRevisionRow
from lightfall_logbook.scoped_store import _iso, add_audit

CONTENT_TYPES = frozenset({"plan", "plugin", "agent", "skill"})
CONTENT_SUBJECT = "lightfall.content.changed"
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_SOURCE_MAX = 2048  # bytes of JSON


class Manifest(BaseModel):
    """What the client declares about a bundle; the server can't check what's inside it."""

    model_config = ConfigDict(extra="forbid")

    type: str
    name: str
    file_count: int = Field(ge=1)
    uncompressed_size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    message: str = Field(default="", max_length=1000)
    author: str | None = Field(default=None, max_length=256)
    agent: bool = False
    source: dict[str, str] = Field(default_factory=dict)

    @field_validator("source")
    @classmethod
    def _small_source(cls, value: dict[str, str]) -> dict[str, str]:
        if len(json.dumps(value)) > _SOURCE_MAX:
            raise ValueError(f"source is larger than {_SOURCE_MAX} bytes")
        return value


class RestoreBody(BaseModel):
    revision: int


def _too_large(detail: str) -> HTTPException:
    return HTTPException(status_code=413, detail=detail)


def _conflict() -> HTTPException:
    return HTTPException(status_code=409, detail="changed concurrently; retry")


def _store(request: Request) -> BundleStore:
    return request.app.state.bundle_store


async def _target(request: Request, db_session: AsyncSession, scope_id: str):
    """``(scope, chain)`` for ``scope_id``: 404 if not in the tree, 422 for user scopes."""
    scope = _parse_scope(scope_id)
    if scope.kind == "user":
        raise _unprocessable("content can't be stored at user scope")
    return scope, _chain(await _tree(request, db_session), scope)  # 404 for unknown scopes


def _check_policy(policy: ContentPolicy, size: int, manifest: Manifest) -> None:
    for value, limit, what in (
        (size, policy.max_bundle_bytes, "max_bundle_bytes"),
        (manifest.uncompressed_size, policy.max_uncompressed_bytes, "max_uncompressed_bytes"),
        (manifest.file_count, policy.max_files, "max_files"),
    ):
        if value > limit:
            raise _too_large(f"{what} is {limit}; this bundle has {value}")


def _check_item(type_: str, name: str) -> None:
    if type_ not in CONTENT_TYPES:
        raise _unprocessable(f"unknown content type {type_!r}")
    if not _NAME.match(name) or ".." in name:
        raise _unprocessable(f"invalid content name {name!r}")


def _revision_record(rev: ContentRevisionRow) -> dict[str, Any]:
    declared = rev.manifest or {}  # the publisher's word; clients enforce it when unpacking
    return {"revision": rev.id, "sha256": rev.sha256, "size": rev.size,
            "published_by": rev.published_by, "published_at": _iso(rev.published_at),
            "author": rev.author, "file_count": declared.get("file_count"),
            "uncompressed_size": declared.get("uncompressed_size")}


async def _item(db_session: AsyncSession, scope: str, type_: str,
                name: str) -> ContentItemRow:
    item = await db_session.get(ContentItemRow, (scope, type_, name))
    if item is None:
        raise NotFoundException(f"No {type_} {name!r} at {scope}")
    return item


class ContentController(Controller):
    path = "/logbook/scopes/{scope_id:str}/content"

    @get("/")
    async def list_content(self, scope_id: str, request: Request,
                           db_session: AsyncSession) -> Response[Any]:
        _caller(request)  # signed in
        scope = str((await _target(request, db_session, scope_id))[0])
        q = (select(ContentItemRow, ContentRevisionRow)
             .join(ContentRevisionRow, ContentRevisionRow.id == ContentItemRow.current_revision)
             .where(ContentItemRow.scope == scope)
             .order_by(ContentItemRow.type, ContentItemRow.name))
        items = [{"type": item.type, "name": item.name, **_revision_record(rev)}
                 for item, rev in (await db_session.execute(q)).all()]
        etag = hashlib.sha256(json.dumps(
            [(i["type"], i["name"], i["revision"], i["sha256"]) for i in items]
        ).encode()).hexdigest()[:16]
        if request.headers.get("If-None-Match") == etag:
            return Response(content=b"", status_code=304, headers={"ETag": etag})
        return Response(content={"scope": scope, "items": items, "etag": etag},
                        headers={"ETag": etag})

    @get("/{type_:str}/{name:str}/bundle")
    async def download(self, scope_id: str, type_: str, name: str, request: Request,
                       db_session: AsyncSession, revision: int | None = None) -> Response[bytes]:
        _caller(request)
        scope = str((await _target(request, db_session, scope_id))[0])
        _check_item(type_, name)
        item = await _item(db_session, scope, type_, name)
        rev_id = revision if revision is not None else item.current_revision
        if rev_id is None:
            raise NotFoundException(f"{type_} {name!r} at {scope} is retracted")
        rev = await db_session.get(ContentRevisionRow, rev_id)
        if rev is None or (rev.scope, rev.type, rev.name) != (scope, type_, name):
            raise NotFoundException(f"No revision {rev_id} of {type_} {name!r} at {scope}")
        try:
            data = _store(request).get(rev.sha256)
        except FileNotFoundError:
            raise NotFoundException(
                f"Bundle for revision {rev_id} is missing from the server's store") from None
        return Response(content=data, media_type="application/zip",
                        headers={"X-Content-SHA256": rev.sha256})

    @get("/{type_:str}/{name:str}/history")
    async def history(self, scope_id: str, type_: str, name: str, request: Request,
                      db_session: AsyncSession) -> list[dict[str, Any]]:
        _caller(request)
        scope = str((await _target(request, db_session, scope_id))[0])
        _check_item(type_, name)
        item = await _item(db_session, scope, type_, name)
        q = (select(ContentRevisionRow)
             .where(ContentRevisionRow.scope == scope, ContentRevisionRow.type == type_,
                    ContentRevisionRow.name == name)
             .order_by(ContentRevisionRow.id.desc()))
        return [{**_revision_record(rev), "message": rev.message, "agent": rev.agent,
                 "current": rev.id == item.current_revision}
                for rev in (await db_session.execute(q)).scalars()]

    @post("/{type_:str}/{name:str}", status_code=201)
    async def publish(
        self, scope_id: str, type_: str, name: str, request: Request,
        db_session: AsyncSession,
        data: dict[str, Any] = Body(media_type=RequestEncodingType.MULTI_PART),
    ) -> Response[dict[str, Any]]:
        """Publish a new revision. Maintainer+ on the scope; policy limits unless admin."""
        actor, groups = _caller(request)
        cfg = _cfg(request)
        target, chain = await _target(request, db_session, scope_id)
        scope = str(target)
        _check_item(type_, name)
        role = role_on(target, chain, groups, cfg.facility_grants)
        if not role_at_least(role, "maintainer"):
            raise PermissionDeniedException(f"maintainer role required on {scope}")

        try:
            manifest = Manifest.model_validate(json.loads(str(data.get("manifest") or "")))
        except (ValueError, ValidationError) as e:
            raise _unprocessable(f"invalid manifest: {e}") from None
        if (manifest.type, manifest.name) != (type_, name):
            raise _unprocessable("manifest type/name don't match the URL")
        upload = data.get("bundle")
        if not isinstance(upload, UploadFile):
            raise _unprocessable("missing bundle file")
        content = await upload.read()
        size = len(content)
        if size > cfg.content.hard_max_bundle_bytes:
            raise _too_large(f"hard_max_bundle_bytes is {cfg.content.hard_max_bundle_bytes}; "
                             f"this bundle has {size}")
        sha256 = hashlib.sha256(content).hexdigest()
        if sha256 != manifest.sha256:
            raise _unprocessable("bundle sha256 doesn't match the manifest")
        if not role_at_least(role, "admin"):
            _check_policy(cfg.content, size, manifest)

        item = await db_session.get(ContentItemRow, (scope, type_, name))
        base = item.current_revision if item is not None else None
        if base is not None:
            current = await db_session.get(ContentRevisionRow, base)
            if current is not None and current.sha256 == sha256:
                # A retry after a lost response: nothing new to record. Still store the
                # bytes (a no-op when present), so re-publishing repairs a lost file.
                _store(request).put(content)
                return Response(content=_publish_record(scope, type_, name, current),
                                status_code=200)

        _store(request).put(content)
        rev = ContentRevisionRow(
            scope=scope, type=type_, name=name, sha256=sha256, size=size,
            manifest=manifest.model_dump(), published_by=actor, author=manifest.author,
            message=manifest.message, agent=manifest.agent or _is_agent(request),
        )
        db_session.add(rev)
        await db_session.flush()
        if item is None:
            db_session.add(ContentItemRow(scope=scope, type=type_, name=name,
                                          current_revision=rev.id))
        elif not await _move_pointer(db_session, scope, type_, name, base, rev.id):
            await db_session.rollback()
            raise _conflict()
        add_audit(db_session, scope=scope, key=f"content:{type_}/{name}", action="publish",
                  old=base, new={"revision": rev.id, "sha256": sha256}, actor=actor,
                  agent=rev.agent)
        try:
            await db_session.commit()
        except IntegrityError:  # another worker created the item first
            await db_session.rollback()
            raise _conflict() from None
        await _publish(request, CONTENT_SUBJECT,
                       {"scope": scope, "type": type_, "name": name, "revision": rev.id})
        return Response(content=_publish_record(scope, type_, name, rev), status_code=201)

    async def _maintainer_item(self, request: Request, db_session: AsyncSession,
                               scope_id: str, type_: str, name: str):
        actor, groups = _caller(request)
        target, chain = await _target(request, db_session, scope_id)
        _check_item(type_, name)
        if not role_at_least(role_on(target, chain, groups, _cfg(request).facility_grants),
                             "maintainer"):
            raise PermissionDeniedException(f"maintainer role required on {target}")
        item = await _item(db_session, str(target), type_, name)
        return actor, str(target), item

    async def _point(self, request: Request, db_session: AsyncSession, actor: str,
                     scope: str, type_: str, name: str, item: ContentItemRow,
                     new: int | None, action: str) -> dict[str, Any]:
        """Move the item's pointer to ``new`` (None = retracted), audited and announced."""
        old = item.current_revision
        record = {"scope": scope, "type": type_, "name": name, "revision": new}
        if old == new:
            return record  # nothing changed: no audit, no event
        if not await _move_pointer(db_session, scope, type_, name, old, new):
            await db_session.rollback()
            raise _conflict()
        add_audit(db_session, scope=scope, key=f"content:{type_}/{name}", action=action,
                  old=old, new=new, actor=actor, agent=_is_agent(request))
        await db_session.commit()
        await _publish(request, CONTENT_SUBJECT, record)
        return record

    @post("/{type_:str}/{name:str}/retract", status_code=200)
    async def retract(self, scope_id: str, type_: str, name: str, request: Request,
                      db_session: AsyncSession) -> dict[str, Any]:
        """Take an item down; its revisions stay in history."""
        actor, scope, item = await self._maintainer_item(request, db_session, scope_id,
                                                         type_, name)
        return await self._point(request, db_session, actor, scope, type_, name, item,
                                 None, "retract")

    @post("/{type_:str}/{name:str}/restore", status_code=200)
    async def restore(self, scope_id: str, type_: str, name: str, data: RestoreBody,
                      request: Request, db_session: AsyncSession) -> dict[str, Any]:
        """Make one of this item's earlier revisions current again."""
        actor, scope, item = await self._maintainer_item(request, db_session, scope_id,
                                                         type_, name)
        rev = await db_session.get(ContentRevisionRow, data.revision)
        if rev is None or (rev.scope, rev.type, rev.name) != (scope, type_, name):
            raise _unprocessable(f"revision {data.revision} is not a revision of this item")
        if not _store(request).exists(rev.sha256):
            raise _unprocessable(f"revision {data.revision}'s bundle is missing from the store")
        return await self._point(request, db_session, actor, scope, type_, name, item,
                                 rev.id, "restore")


def _publish_record(scope: str, type_: str, name: str,
                    rev: ContentRevisionRow) -> dict[str, Any]:
    return {"scope": scope, "type": type_, "name": name, "revision": rev.id,
            "sha256": rev.sha256, "size": rev.size}


async def _move_pointer(db_session: AsyncSession, scope: str, type_: str, name: str,
                        expected: int | None, new: int | None) -> bool:
    """Compare-and-set the item's current revision; False if another writer moved it."""
    current = ContentItemRow.current_revision
    result = await db_session.execute(
        update(ContentItemRow)
        .where(ContentItemRow.scope == scope, ContentItemRow.type == type_,
               ContentItemRow.name == name,
               current.is_(None) if expected is None else current == expected)
        .values(current_revision=new)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


def content_controller(policy: ContentPolicy) -> type[ContentController]:
    """The controller with a body limit from this deployment's policy (admins included)."""
    return type("ContentController", (ContentController,),
                {"request_max_body_size": policy.hard_max_bundle_bytes + 64 * 1024})
