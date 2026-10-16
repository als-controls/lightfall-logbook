"""Persistence helpers for scoped settings: specs, snapshots, audit, seeding."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from lightfall_settings import (
    Lock,
    ScopeChain,
    ScopeId,
    ScopeSnapshot,
    SettingSpec,
    core_registry,
    spec_from_dict,
    spec_to_dict,
    user_only_spec,
)
from loguru import logger
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lightfall_logbook.models import UserSettingRow
from lightfall_logbook.scoped_models import (
    ScopedSettingRow,
    SettingAuditRow,
    SettingKeyRow,
    SettingLockRow,
)

_CORE = core_registry()


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:  # SQLite returns naive datetimes
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


async def spec_for(session: AsyncSession, key: str) -> tuple[SettingSpec, bool]:
    """Server-trusted spec for ``key`` and whether it is core/approved."""
    if _CORE.is_declared(key):
        return _CORE.get(key), True
    row = await session.get(SettingKeyRow, key)
    if row is not None and row.status == "approved" and row.spec:
        try:
            if not isinstance(row.spec, dict):
                raise ValueError("stored spec is not an object")
            spec = spec_from_dict(row.spec)
            if spec.key != key:
                raise ValueError(f"stored spec is for {spec.key!r}")
            return spec, True
        except (ValueError, TypeError) as e:
            logger.error("Approved spec for {!r} is invalid ({}); treating as unapproved", key, e)
    return user_only_spec(key), False


def is_core_key(key: str) -> bool:
    return _CORE.is_declared(key)


async def load_snapshots(
    session: AsyncSession, chain: ScopeChain, key: str | None = None
) -> list[ScopeSnapshot]:
    names = [str(s) for s in chain]
    q_set = select(ScopedSettingRow).where(ScopedSettingRow.scope.in_(names))
    q_lock = select(SettingLockRow).where(SettingLockRow.scope.in_(names))
    if key is not None:
        q_set = q_set.where(ScopedSettingRow.key == key)
        q_lock = q_lock.where(SettingLockRow.key == key)
    settings: dict[str, dict[str, Any]] = {n: {} for n in names}
    locks: dict[str, dict[str, Lock]] = {n: {} for n in names}
    for row in (await session.execute(q_set)).scalars():
        settings[row.scope][row.key] = row.value
    for row in (await session.execute(q_lock)).scalars():
        locks[row.scope][row.key] = Lock(
            locked_by=row.locked_by, reason=row.reason, locked_at=_iso(row.locked_at) or ""
        )
    return [ScopeSnapshot(s, settings[str(s)], locks[str(s)]) for s in chain]


def snapshot_etag(settings: Mapping[str, Any], locks: Mapping[str, str]) -> str:
    """ETag over per-key tokens (callers pass version + updated_at + value, so a
    reset-and-recreate at the same version still changes it) and lock times."""
    blob = json.dumps([sorted(settings.items()), sorted(locks.items())], default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def add_audit(
    session: AsyncSession,
    *,
    scope: str,
    key: str,
    action: str,
    old: Any,
    new: Any,
    actor: str,
    agent: bool = False,
) -> None:
    session.add(SettingAuditRow(
        scope=scope, key=key, action=action, old=old, new=new, actor=actor, agent=agent,
    ))


def key_record(row: SettingKeyRow) -> dict[str, Any]:
    return {
        "key": row.key,
        "status": row.status,
        "spec": row.spec,
        "proposed_spec": row.proposed_spec,
        "proposed_by": row.proposed_by,
        "proposed_scope": row.proposed_scope,
        "proposed_at": _iso(row.proposed_at),
        "reviewed_by": row.reviewed_by,
        "reviewed_at": _iso(row.reviewed_at),
        "note": row.note,
    }


async def seed_keys(session_factory: async_sessionmaker, specs: Iterable[SettingSpec]) -> int:
    """Insert approved records for seeded specs not yet in the DB. Returns rows inserted.

    Each key commits separately so a concurrent worker seeding the same key
    (multi-worker startup) only costs that one insert.
    """
    inserted = 0
    now = datetime.now(UTC)
    for spec in specs:
        if is_core_key(spec.key):
            logger.warning("Ignoring seed for core key {!r}", spec.key)
            continue
        async with session_factory() as session:
            if await session.get(SettingKeyRow, spec.key) is not None:
                continue
            session.add(SettingKeyRow(
                key=spec.key, status="approved", spec=spec_to_dict(spec),
                reviewed_by="seed", reviewed_at=now, note="seeded from SCOPES_CONFIG",
            ))
            try:
                await session.commit()
                inserted += 1
            except IntegrityError:
                await session.rollback()
    return inserted


async def migrate_user_settings(session_factory: async_sessionmaker) -> int:
    """Copy legacy ``user_settings`` rows into user scope (spec section 11, phase 4).

    Idempotent and non-destructive: a key already set at ``user:<id>`` is left alone and
    the legacy table is not modified. Rows with a beamline are skipped (user scope has no
    beamline dimension), as are user ids that cannot name a scope. Returns rows inserted.
    """
    async with session_factory() as session:
        legacy = (await session.execute(select(UserSettingRow))).scalars().all()
    inserted = skipped = 0
    for row in legacy:
        if row.beamline:
            skipped += 1
            continue
        try:
            scope = str(ScopeId("user", row.user_id))
        except ValueError:
            skipped += 1
            continue
        async with session_factory() as session:
            if await session.get(ScopedSettingRow, (scope, row.key)) is not None:
                continue
            session.add(ScopedSettingRow(scope=scope, key=row.key, value=row.value, version=1,
                                         updated_by="migration"))
            add_audit(session, scope=scope, key=row.key, action="migrate", old=None,
                      new=row.value, actor="migration", agent=False)
            try:
                await session.commit()
                inserted += 1
            except IntegrityError:
                await session.rollback()
    if skipped:
        logger.info("Legacy settings migration skipped {} row(s) (beamline-specific or "
                    "unusable user id)", skipped)
    return inserted
