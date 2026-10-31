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

from litestar import Controller, Request, Response, get
from litestar.exceptions import NotFoundException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lightfall_logbook.bundle_store import BundleStore
from lightfall_logbook.scoped_api import _caller, _chain, _parse_scope, _tree, _unprocessable
from lightfall_logbook.scoped_models import ContentItemRow, ContentRevisionRow
from lightfall_logbook.scoped_store import _iso

CONTENT_TYPES = frozenset({"plan", "plugin", "agent", "skill"})
CONTENT_SUBJECT = "lightfall.content.changed"
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def _store(request: Request) -> BundleStore:
    return request.app.state.bundle_store


async def _target(request: Request, db_session: AsyncSession, scope_id: str):
    """The content scope for ``scope_id``: 404 if not in the tree, 422 for user scopes."""
    scope = _parse_scope(scope_id)
    if scope.kind == "user":
        raise _unprocessable("content can't be stored at user scope")
    _chain(await _tree(request, db_session), scope)  # 404 for unknown scopes
    return scope


def _check_item(type_: str, name: str) -> None:
    if type_ not in CONTENT_TYPES:
        raise _unprocessable(f"unknown content type {type_!r}")
    if not _NAME.match(name) or ".." in name:
        raise _unprocessable(f"invalid content name {name!r}")


def _revision_record(rev: ContentRevisionRow) -> dict[str, Any]:
    return {"revision": rev.id, "sha256": rev.sha256, "size": rev.size,
            "published_by": rev.published_by, "published_at": _iso(rev.published_at),
            "author": rev.author}


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
        scope = str(await _target(request, db_session, scope_id))
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
        scope = str(await _target(request, db_session, scope_id))
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
        scope = str(await _target(request, db_session, scope_id))
        _check_item(type_, name)
        item = await _item(db_session, scope, type_, name)
        q = (select(ContentRevisionRow)
             .where(ContentRevisionRow.scope == scope, ContentRevisionRow.type == type_,
                    ContentRevisionRow.name == name)
             .order_by(ContentRevisionRow.id.desc()))
        return [{**_revision_record(rev), "message": rev.message, "agent": rev.agent,
                 "current": rev.id == item.current_revision}
                for rev in (await db_session.execute(q)).scalars()]
