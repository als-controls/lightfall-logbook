"""Scoped settings HTTP API (spec §3): /logbook/scopes and /logbook/setting-keys."""
from __future__ import annotations

from typing import Any

from lightfall_settings import (
    ScopeChain,
    ScopeId,
    WriteDenied,
    role_on,
)
from litestar import Controller, Request, Response, get
from litestar.exceptions import HTTPException, NotFoundException, PermissionDeniedException
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lightfall_logbook.auth import request_groups, request_identity
from lightfall_logbook.scoped_config import ScopedConfig, UnknownScope
from lightfall_logbook.scoped_models import ScopedSettingRow, SettingLockRow
from lightfall_logbook.scoped_store import _iso, snapshot_etag

SETTINGS_SUBJECT = "lightfall.settings.changed"
KEYS_SUBJECT = "lightfall.settings.keys"


def _parse_scope(text: str) -> ScopeId:
    try:
        return ScopeId.parse(text)
    except ValueError:
        raise NotFoundException(f"Unknown scope {text!r}") from None


def _cfg(request: Request) -> ScopedConfig:
    return request.app.state.scoped_config


def _chain(cfg: ScopedConfig, scope: ScopeId) -> ScopeChain:
    try:
        return cfg.tree.chain_for(scope)
    except UnknownScope:
        raise NotFoundException(f"Unknown scope {scope}") from None


def _deny(e: WriteDenied) -> HTTPException:
    return HTTPException(status_code=e.status, detail=e.reason)


def _caller(request: Request) -> tuple[str, list[str]]:
    return request_identity(request), request_groups(request)


def _is_agent(request: Request) -> bool:
    return request.headers.get("X-Lightfall-Agent", "").lower() in ("1", "true", "yes")


async def _publish(request: Request, subject: str, payload: dict[str, Any]) -> None:
    pub = request.app.state.logbook_events
    fn = getattr(pub, "publish", None)
    if fn is None:
        return
    try:
        await fn(subject, payload)
    except Exception as e:  # best-effort; never fail the request
        logger.warning("Settings event publish failed: {}", e)


def _role_label(scope: ScopeId, chain: ScopeChain, actor: str, groups: list[str],
                cfg: ScopedConfig) -> str | None:
    if scope.kind == "user":
        return "owner" if scope.name == actor else None
    return role_on(scope, chain, groups, cfg.facility_grants)


class ScopesController(Controller):
    path = "/logbook/scopes"

    @get("/chain")
    async def chain(self, request: Request, bind: str = "facility") -> dict[str, Any]:
        cfg = _cfg(request)
        actor, groups = _caller(request)
        scope = _parse_scope(bind)
        if scope.kind == "user":
            raise NotFoundException("A session binds to facility, a beamline or a branch")
        chain = _chain(cfg, scope).with_user(actor)
        entitled = [
            str(s) for s in cfg.tree.scopes()
            if s.kind in ("beamline", "branch")
            and role_on(s, cfg.tree.chain_for(s), groups, cfg.facility_grants) is not None
        ]
        return {
            "chain": [str(s) for s in chain],
            "roles": {str(s): _role_label(s, chain, actor, groups, cfg) for s in chain},
            "entitled": entitled,
            "facility_label": cfg.tree.label,
        }

    @get("/{scope_id:str}/settings")
    async def snapshot(
        self, scope_id: str, request: Request, db_session: AsyncSession
    ) -> Response[Any]:
        cfg = _cfg(request)
        actor, _ = _caller(request)
        target = _parse_scope(scope_id)
        _chain(cfg, target)
        if target.kind == "user" and target.name != actor:
            raise PermissionDeniedException("Only the user may read their own scope")
        rows = (await db_session.execute(
            select(ScopedSettingRow).where(ScopedSettingRow.scope == str(target))
        )).scalars().all()
        locks = (await db_session.execute(
            select(SettingLockRow).where(SettingLockRow.scope == str(target))
        )).scalars().all()
        etag = snapshot_etag(
            {r.key: r.version for r in rows}, {lk.key: _iso(lk.locked_at) or "" for lk in locks}
        )
        if request.headers.get("If-None-Match") == etag:
            return Response(content=b"", status_code=304, headers={"ETag": etag})
        body = {
            "scope": str(target),
            "settings": {r.key: r.value for r in rows},
            "versions": {r.key: r.version for r in rows},
            "locks": {
                lk.key: {"locked_by": lk.locked_by, "reason": lk.reason,
                         "locked_at": _iso(lk.locked_at)}
                for lk in locks
            },
            "etag": etag,
        }
        return Response(content=body, headers={"ETag": etag})
