from datetime import UTC, datetime
from pathlib import Path

import pytest
from lightfall_settings import FACILITY, Lock, ScopeChain, ScopeId, SettingSpec
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from lightfall_logbook.models import Base
from lightfall_logbook.scoped_models import ScopedSettingRow, SettingKeyRow, SettingLockRow
from lightfall_logbook.scoped_store import (
    load_snapshots,
    seed_keys,
    snapshot_etag,
    spec_for,
)

BL = ScopeId("beamline", "7.0.1")
BR = ScopeId("branch", "7.0.1.1")
CHAIN = ScopeChain((FACILITY, BL, BR))


@pytest.fixture
async def factory(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 's.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def test_spec_for_core_approved_and_unknown(factory):
    async with factory() as s:
        s.add(SettingKeyRow(key="tiled.url", status="approved",
                            spec={"key": "tiled.url", "type": "str", "scopes": ["branch"]}))
        s.add(SettingKeyRow(key="pending.key", status="pending",
                            proposed_spec={"key": "pending.key"}))
        await s.commit()
    async with factory() as s:
        core, known = await spec_for(s, "plugins.enabled")
        assert known and core.policy == "union"
        approved, known = await spec_for(s, "tiled.url")
        assert known and approved.type is str and approved.scopes == frozenset({"branch"})
        pending, known = await spec_for(s, "pending.key")
        assert not known and pending.scopes == frozenset({"user"})
        unknown, known = await spec_for(s, "nope")
        assert not known and unknown.scopes == frozenset({"user"})


async def test_corrupt_approved_spec_is_treated_as_unapproved(factory):
    async with factory() as s:
        s.add(SettingKeyRow(key="bad", status="approved", spec={"key": "bad", "type": "complex"}))
        await s.commit()
    async with factory() as s:
        spec, known = await spec_for(s, "bad")
    assert not known and spec.scopes == frozenset({"user"})


async def test_load_snapshots_in_chain_order(factory):
    async with factory() as s:
        s.add(ScopedSettingRow(scope="facility", key="k", value="a", version=1, updated_by="x"))
        s.add(ScopedSettingRow(scope="branch:7.0.1.1", key="k", value="c", version=3,
                               updated_by="x"))
        s.add(ScopedSettingRow(scope="branch:7.0.1.1", key="other", value=1, version=1,
                               updated_by="x"))
        s.add(ScopedSettingRow(scope="beamline:8.3.2", key="k", value="z", version=1,
                               updated_by="x"))
        s.add(SettingLockRow(scope="beamline:7.0.1", key="k", locked_by="ron", reason="r",
                             locked_at=datetime(2026, 10, 15, tzinfo=UTC)))
        await s.commit()
    async with factory() as s:
        snaps = await load_snapshots(s, CHAIN, "k")
    assert [sn.scope for sn in snaps] == [FACILITY, BL, BR]
    assert snaps[0].settings == {"k": "a"}
    assert snaps[1].settings == {} and snaps[1].locks["k"].locked_by == "ron"
    assert isinstance(snaps[1].locks["k"], Lock)
    assert snaps[2].settings == {"k": "c"}
    async with factory() as s:
        all_keys = await load_snapshots(s, CHAIN)
    assert all_keys[2].settings == {"k": "c", "other": 1}


def test_snapshot_etag_changes_with_versions_and_locks():
    a = snapshot_etag({"k": 1}, {})
    assert a == snapshot_etag({"k": 1}, {})
    assert a != snapshot_etag({"k": 2}, {})
    assert a != snapshot_etag({"k": 1}, {"k": "2026-10-15T00:00:00+00:00"})


async def test_seed_keys_inserts_once_and_tolerates_rerun(factory):
    spec = SettingSpec("voice.wake_word", type=str, scopes={"branch", "user"})
    assert await seed_keys(factory, [spec]) == 1
    assert await seed_keys(factory, [spec]) == 0  # second worker / restart
    async with factory() as s:
        row = await s.get(SettingKeyRow, "voice.wake_word")
    assert row.status == "approved" and row.reviewed_by == "seed"
    assert row.spec["type"] == "str"


async def test_seed_keys_skips_core_keys(factory):
    assert await seed_keys(factory, [SettingSpec("plugins.enabled", scopes={"user"})]) == 0
