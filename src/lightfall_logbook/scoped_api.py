"""Scoped settings HTTP API (spec §3): /logbook/scopes and /logbook/setting-keys."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from lightfall_settings import (
    ScopeChain,
    ScopeId,
    WriteDenied,
    check_write,
    resolve,
    role_at_least,
    role_on,
)
from litestar import Controller, Request, Response, delete, get, put
from litestar.exceptions import HTTPException, NotFoundException, PermissionDeniedException
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lightfall_logbook.auth import request_groups, request_identity
from lightfall_logbook.scoped_config import ScopedConfig, UnknownScope
from lightfall_logbook.scoped_models import ScopedSettingRow, SettingAuditRow, SettingLockRow
from lightfall_logbook.scoped_store import (
    _iso,
    add_audit,
    load_snapshots,
    snapshot_etag,
    spec_for,
)

SETTINGS_SUBJECT = "lightfall.settings.changed"
KEYS_SUBJECT = "lightfall.settings.keys"


class ScopedSettingWrite(BaseModel):
    value: Any = None


class LockWrite(BaseModel):
    reason: str = ""


def _precondition_failed(detail: str) -> HTTPException:
    return HTTPException(status_code=412, detail=detail)


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

    @put("/{scope_id:str}/settings/{key:str}")
    async def put_setting(
        self, scope_id: str, key: str, data: ScopedSettingWrite, request: Request,
        db_session: AsyncSession,
    ) -> dict[str, Any]:
        cfg = _cfg(request)
        actor, groups = _caller(request)
        target = _parse_scope(scope_id)
        chain = _chain(cfg, target)
        spec, _known = await spec_for(db_session, key)
        snaps = await load_snapshots(db_session, chain, key)
        try:
            value = check_write(
                spec=spec, target=target, chain=chain, actor=actor, groups=groups,
                snapshots=snaps, action="set", value=data.value,
                facility_grants=cfg.facility_grants,
            )
        except WriteDenied as e:
            raise _deny(e) from None

        if_match = request.headers.get("If-Match")
        row = await db_session.get(ScopedSettingRow, (str(target), key))
        old = None
        if row is None:
            if if_match not in (None, "0"):
                raise _precondition_failed(f"{key} does not exist at {target}")
            row = ScopedSettingRow(scope=str(target), key=key, value=value, version=1,
                                   updated_by=actor)
            db_session.add(row)
        else:
            if if_match != str(row.version):
                raise _precondition_failed(
                    f"{key} at {target} is at version {row.version}; send If-Match"
                )
            old = row.value
            row.value = value
            row.version += 1
            row.updated_by = actor
        add_audit(db_session, scope=str(target), key=key, action="set", old=old, new=value,
                  actor=actor, agent=_is_agent(request))
        try:
            await db_session.commit()
        except IntegrityError:
            await db_session.rollback()
            raise _precondition_failed(f"{key} at {target} was created concurrently") from None
        await _publish(request, SETTINGS_SUBJECT,
                       {"scope": str(target), "key": key, "version": row.version})
        return {"scope": str(target), "key": key, "value": row.value, "version": row.version}

    @delete("/{scope_id:str}/settings/{key:str}", status_code=204)
    async def delete_setting(
        self, scope_id: str, key: str, request: Request, db_session: AsyncSession
    ) -> None:
        cfg = _cfg(request)
        actor, groups = _caller(request)
        target = _parse_scope(scope_id)
        chain = _chain(cfg, target)
        spec, _known = await spec_for(db_session, key)
        snaps = await load_snapshots(db_session, chain, key)
        try:
            check_write(spec=spec, target=target, chain=chain, actor=actor, groups=groups,
                        snapshots=snaps, action="delete", facility_grants=cfg.facility_grants)
        except WriteDenied as e:
            raise _deny(e) from None
        row = await db_session.get(ScopedSettingRow, (str(target), key))
        if row is None:
            raise NotFoundException(f"{key} is not set at {target}")
        add_audit(db_session, scope=str(target), key=key, action="delete", old=row.value,
                  new=None, actor=actor, agent=_is_agent(request))
        await db_session.delete(row)
        await db_session.commit()
        await _publish(request, SETTINGS_SUBJECT, {"scope": str(target), "key": key, "version": 0})

    @put("/{scope_id:str}/locks/{key:str}")
    async def put_lock(
        self, scope_id: str, key: str, data: LockWrite, request: Request,
        db_session: AsyncSession,
    ) -> dict[str, Any]:
        cfg = _cfg(request)
        actor, groups = _caller(request)
        target = _parse_scope(scope_id)
        chain = _chain(cfg, target)
        spec, _known = await spec_for(db_session, key)
        snaps = await load_snapshots(db_session, chain, key)
        try:
            check_write(spec=spec, target=target, chain=chain, actor=actor, groups=groups,
                        snapshots=snaps, action="lock", facility_grants=cfg.facility_grants)
        except WriteDenied as e:
            raise _deny(e) from None

        row = await db_session.get(ScopedSettingRow, (str(target), key))
        if row is None:
            effective = resolve(spec, snaps).value
            if effective is not None:
                row = ScopedSettingRow(scope=str(target), key=key, value=effective, version=1,
                                       updated_by=actor)
                db_session.add(row)
                add_audit(db_session, scope=str(target), key=key, action="set", old=None,
                          new=effective, actor=actor, agent=_is_agent(request))
        now = datetime.now(UTC)
        lock = await db_session.get(SettingLockRow, (str(target), key))
        if lock is None:
            lock = SettingLockRow(scope=str(target), key=key, locked_by=actor,
                                  reason=data.reason, locked_at=now)
            db_session.add(lock)
        else:
            lock.locked_by, lock.reason, lock.locked_at = actor, data.reason, now
        add_audit(db_session, scope=str(target), key=key, action="lock", old=None,
                  new={"reason": data.reason}, actor=actor, agent=_is_agent(request))
        await db_session.commit()
        version = row.version if row is not None else 0
        await _publish(request, SETTINGS_SUBJECT,
                       {"scope": str(target), "key": key, "version": version})
        return {"scope": str(target), "key": key, "locked_by": actor, "reason": data.reason,
                "locked_at": _iso(now), "version": version}

    @delete("/{scope_id:str}/locks/{key:str}", status_code=204)
    async def delete_lock(
        self, scope_id: str, key: str, request: Request, db_session: AsyncSession
    ) -> None:
        cfg = _cfg(request)
        actor, groups = _caller(request)
        target = _parse_scope(scope_id)
        chain = _chain(cfg, target)
        spec, _known = await spec_for(db_session, key)
        snaps = await load_snapshots(db_session, chain, key)
        try:
            check_write(spec=spec, target=target, chain=chain, actor=actor, groups=groups,
                        snapshots=snaps, action="unlock", facility_grants=cfg.facility_grants)
        except WriteDenied as e:
            raise _deny(e) from None
        lock = await db_session.get(SettingLockRow, (str(target), key))
        add_audit(db_session, scope=str(target), key=key, action="unlock",
                  old={"reason": lock.reason}, new=None, actor=actor, agent=_is_agent(request))
        await db_session.delete(lock)
        await db_session.commit()
        row = await db_session.get(ScopedSettingRow, (str(target), key))
        await _publish(request, SETTINGS_SUBJECT, {
            "scope": str(target), "key": key, "version": row.version if row else 0,
        })

    @get("/{scope_id:str}/audit")
    async def audit(
        self, scope_id: str, request: Request, db_session: AsyncSession,
        key: str | None = None, limit: int = 100,
    ) -> list[dict[str, Any]]:
        cfg = _cfg(request)
        actor, groups = _caller(request)
        target = _parse_scope(scope_id)
        chain = _chain(cfg, target)
        if target.kind == "user":
            if target.name != actor:
                raise PermissionDeniedException("Only the user may read their own audit trail")
        elif not role_at_least(role_on(target, chain, groups, cfg.facility_grants),
                               "maintainer"):
            raise PermissionDeniedException(f"maintainer role required on {target}")
        q = select(SettingAuditRow).where(SettingAuditRow.scope == str(target))
        if key is not None:
            q = q.where(SettingAuditRow.key == key)
        q = q.order_by(SettingAuditRow.id.desc()).limit(max(1, min(limit, 1000)))
        return [
            {"id": a.id, "scope": a.scope, "key": a.key, "action": a.action, "old": a.old,
             "new": a.new, "actor": a.actor, "agent": a.agent, "at": _iso(a.at)}
            for a in (await db_session.execute(q)).scalars()
        ]
