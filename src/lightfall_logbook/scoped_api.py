"""Scoped settings HTTP API (spec §3): /logbook/scopes and /logbook/setting-keys."""
from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime
from typing import Any

from lightfall_settings import (
    FACILITY,
    OVERRIDABLE_FIELDS,
    ScopeChain,
    ScopeId,
    WriteDenied,
    check_write,
    resolve,
    role_at_least,
    role_on,
    spec_from_dict,
    spec_to_dict,
)
from litestar import Controller, Request, Response, delete, get, post, put
from litestar.exceptions import HTTPException, NotFoundException, PermissionDeniedException
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from lightfall_logbook.auth import request_groups, request_identity
from lightfall_logbook.scoped_config import ScopedConfig, UnknownScope
from lightfall_logbook.scoped_models import (
    ScopedSettingRow,
    SettingAuditRow,
    SettingKeyRow,
    SettingLockRow,
)
from lightfall_logbook.scoped_store import (
    _iso,
    add_audit,
    declared_specs,
    is_core_key,
    key_record,
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


class ProposeBody(BaseModel):
    spec: dict[str, Any]
    scope: str


class ApproveBody(BaseModel):
    spec: dict[str, Any] | None = None
    note: str = ""


class RejectBody(BaseModel):
    note: str = ""


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=422, detail=detail)


def _require_facility_admin(cfg: ScopedConfig, groups: list[str]) -> None:
    role = role_on(FACILITY, ScopeChain((FACILITY,)), groups, cfg.facility_grants)
    if not role_at_least(role, "admin"):
        raise PermissionDeniedException("facility admin role required")


def _proposal_spec(key: str, raw: Any) -> dict[str, Any]:
    """Validate a proposed spec dict for ``key``; return its canonical dict."""
    if not isinstance(raw, dict):
        raise ValueError("spec must be an object")
    if raw.get("key", key) != key:
        raise ValueError("spec key does not match the URL")
    return spec_to_dict(spec_from_dict({**raw, "key": key}))


async def _reload_key(session: AsyncSession, key: str) -> SettingKeyRow | None:
    """Fresh read of a key record (bypasses the identity map after a rollback)."""
    q = select(SettingKeyRow).where(SettingKeyRow.key == key).execution_options(
        populate_existing=True
    )
    return (await session.execute(q)).scalar_one_or_none()


async def _record_proposal(
    session: AsyncSession, *, key: str, spec_dict: dict[str, Any], scope: ScopeId, actor: str
) -> SettingKeyRow:
    row = await session.get(SettingKeyRow, key)
    now = datetime.now(UTC)
    if row is None:
        row = SettingKeyRow(key=key, status="pending", proposed_spec=spec_dict,
                            proposed_by=actor, proposed_scope=str(scope), proposed_at=now,
                            note="")
        session.add(row)
    elif row.status == "rejected":
        row.status = "pending"
        row.proposed_spec, row.proposed_by = spec_dict, actor
        row.proposed_scope, row.proposed_at = str(scope), now
        row.reviewed_by = row.reviewed_at = None
    return row


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
        base = _chain(cfg, scope)  # 404 for unknown scopes (litestar's 404 is a ValueError)
        try:
            chain = base.with_user(actor)
        except ValueError:
            raise PermissionDeniedException("Caller identity cannot name a user scope") from None
        tree = [
            {
                "scope": str(s),
                "parent": (f"beamline:{cfg.tree.branch_parent[s.name]}"
                           if s.kind == "branch" else None),
                "role": role_on(s, cfg.tree.chain_for(s), groups, cfg.facility_grants),
            }
            for s in cfg.tree.scopes() if s.kind in ("beamline", "branch")
        ]
        entitled = [e["scope"] for e in tree if e["role"] is not None]
        return {
            "chain": [str(s) for s in chain],
            "roles": {str(s): _role_label(s, chain, actor, groups, cfg) for s in chain},
            "entitled": entitled,
            "tree": tree,  # every beamline/branch with the caller's role (console binding)
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
        # Omit values written before their spec forbade this scope (e.g. a user-scope
        # tiled_beamline): clients resolve with their own registry and would let them win.
        specs = await declared_specs(db_session, (r.key for r in rows))
        rows = [r for r in rows if r.key not in specs or specs[r.key].allows(target.kind)]
        locks = (await db_session.execute(
            select(SettingLockRow).where(SettingLockRow.scope == str(target))
        )).scalars().all()
        etag = snapshot_etag(
            {r.key: [r.version, _iso(r.updated_at), r.value] for r in rows},
            {lk.key: _iso(lk.locked_at) or "" for lk in locks},
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
        spec, known = await spec_for(db_session, key)
        if not known and target.kind != "user":
            await self._key_pending(request, db_session, cfg, key, target, chain, actor, groups)
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
            row.value = value  # version bumps via version_id_col on flush
            row.updated_by = actor
        add_audit(db_session, scope=str(target), key=key, action="set", old=old, new=value,
                  actor=actor, agent=_is_agent(request))
        try:
            await db_session.commit()
        except (IntegrityError, StaleDataError):
            await db_session.rollback()
            raise _precondition_failed(f"{key} at {target} changed concurrently") from None
        await _publish(request, SETTINGS_SUBJECT,
                       {"scope": str(target), "key": key, "version": row.version})
        return {"scope": str(target), "key": key, "value": row.value, "version": row.version}

    async def _key_pending(
        self, request: Request, db_session: AsyncSession, cfg: ScopedConfig, key: str,
        target: ScopeId, chain: ScopeChain, actor: str, groups: list[str],
    ) -> None:
        """Refuse a write of an unapproved key above user scope (spec §3 proposal flow)."""
        if not role_at_least(role_on(target, chain, groups, cfg.facility_grants), "maintainer"):
            raise PermissionDeniedException(f"maintainer role required on {target}")
        row = await db_session.get(SettingKeyRow, key)
        header = request.headers.get("X-Proposed-Spec")
        if row is None and header:
            try:
                spec_dict = _proposal_spec(key, json.loads(header))
            except (ValueError, TypeError) as e:
                logger.info("Ignoring invalid X-Proposed-Spec for {!r}: {}", key, e)
            else:
                row = await _record_proposal(db_session, key=key, spec_dict=spec_dict,
                                             scope=target, actor=actor)
                try:
                    await db_session.commit()
                except IntegrityError:  # another proposal for this key landed first
                    await db_session.rollback()
                    row = await _reload_key(db_session, key)
                else:
                    await _publish(request, KEYS_SUBJECT, {"key": key, "status": "pending"})
        status = row.status if row is not None else "unproposed"
        raise HTTPException(
            status_code=409,
            detail=f"{key} is not an approved setting yet",
            extra={"code": "key_pending", "status": status},
        )

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
        if not spec.allows(target.kind):
            # A value stranded by a stricter spec: removable by whoever may write the
            # scope; locks don't apply since the value is ignored anyway.
            spec = dataclasses.replace(spec, scopes=spec.scopes | {target.kind})
            snaps = []
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
        try:
            await db_session.commit()
        except (IntegrityError, StaleDataError):
            await db_session.rollback()
            raise HTTPException(
                status_code=409, detail=f"{key} at {target} changed concurrently; retry"
            ) from None
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


class SettingKeysController(Controller):
    path = "/logbook/setting-keys"

    @get("/")
    async def list_keys(
        self, request: Request, db_session: AsyncSession, status: str | None = None
    ) -> list[dict[str, Any]]:
        cfg = _cfg(request)
        _actor, groups = _caller(request)
        if not any(
            role_at_least(role_on(s, cfg.tree.chain_for(s), groups, cfg.facility_grants),
                          "maintainer")
            for s in cfg.tree.scopes()
        ):
            raise PermissionDeniedException("maintainer role required")
        q = select(SettingKeyRow).order_by(SettingKeyRow.key)
        if status is not None:
            q = q.where(SettingKeyRow.status == status)
        return [key_record(r) for r in (await db_session.execute(q)).scalars()]

    @post("/{key:str}/propose")
    async def propose(
        self, key: str, data: ProposeBody, request: Request, db_session: AsyncSession
    ) -> dict[str, Any]:
        cfg = _cfg(request)
        actor, groups = _caller(request)
        target = _parse_scope(data.scope)
        chain = _chain(cfg, target)
        if target.kind == "user" or not role_at_least(
            role_on(target, chain, groups, cfg.facility_grants), "maintainer"
        ):
            raise PermissionDeniedException(f"maintainer role required on {target}")
        if is_core_key(key):
            raise HTTPException(status_code=409, detail=f"{key} is a core setting")
        try:
            spec_dict = _proposal_spec(key, data.spec)
        except ValueError as e:
            raise _unprocessable(str(e)) from None
        row = await _record_proposal(db_session, key=key, spec_dict=spec_dict, scope=target,
                                     actor=actor)
        if row.status == "approved":
            raise HTTPException(status_code=409, detail=f"{key} is already approved")
        try:
            await db_session.commit()
        except IntegrityError:  # a concurrent proposal for this key landed first
            await db_session.rollback()
            row = await _reload_key(db_session, key)
            if row is None or row.status == "approved":
                raise HTTPException(status_code=409, detail=f"{key} changed concurrently") from None
            return key_record(row)
        await _publish(request, KEYS_SUBJECT, {"key": key, "status": row.status})
        return key_record(row)

    @post("/{key:str}/approve")
    async def approve(
        self, key: str, data: ApproveBody, request: Request, db_session: AsyncSession
    ) -> dict[str, Any]:
        cfg = _cfg(request)
        actor, groups = _caller(request)
        _require_facility_admin(cfg, groups)
        row = await db_session.get(SettingKeyRow, key)
        if row is None:
            raise NotFoundException(f"No proposal for {key}")
        edits = data.spec or {}
        bad = set(edits) - OVERRIDABLE_FIELDS
        if bad:
            raise _unprocessable(f"fields {sorted(bad)} cannot be edited at approval")
        base = (row.spec if row.status == "approved" else row.proposed_spec) or {"key": key}
        try:
            approved = spec_from_dict({**base, **edits, "key": key})
        except ValueError as e:
            raise _unprocessable(str(e)) from None
        row.spec = spec_to_dict(approved)
        row.status = "approved"
        row.reviewed_by, row.reviewed_at, row.note = actor, datetime.now(UTC), data.note
        await db_session.commit()
        await _publish(request, KEYS_SUBJECT, {"key": key, "status": "approved"})
        return key_record(row)

    @post("/{key:str}/reject")
    async def reject(
        self, key: str, data: RejectBody, request: Request, db_session: AsyncSession
    ) -> dict[str, Any]:
        cfg = _cfg(request)
        actor, groups = _caller(request)
        _require_facility_admin(cfg, groups)
        row = await db_session.get(SettingKeyRow, key)
        if row is None:
            raise NotFoundException(f"No proposal for {key}")
        row.status, row.spec = "rejected", None
        row.reviewed_by, row.reviewed_at, row.note = actor, datetime.now(UTC), data.note
        await db_session.commit()
        await _publish(request, KEYS_SUBJECT, {"key": key, "status": "rejected"})
        return key_record(row)

    @delete("/{key:str}", status_code=204)
    async def revoke(self, key: str, request: Request, db_session: AsyncSession) -> None:
        cfg = _cfg(request)
        _actor, groups = _caller(request)
        _require_facility_admin(cfg, groups)
        row = await db_session.get(SettingKeyRow, key)
        if row is None:
            raise NotFoundException(f"No record for {key}")
        await db_session.delete(row)
        await db_session.commit()
        await _publish(request, KEYS_SUBJECT, {"key": key, "status": "revoked"})
