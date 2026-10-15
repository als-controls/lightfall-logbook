"""Regression tests for the phase-3 final review findings."""
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm.exc import StaleDataError

from lightfall_logbook.models import Base
from lightfall_logbook.scoped_models import ScopedSettingRow, SettingKeyRow, SettingLockRow
from lightfall_logbook.scoped_store import spec_for
from tests.scoped_helpers import ADMIN, ALICE, MAINT, OPER, as_, make_client

WAKE = {"key": "voice.wake_word", "type": "str", "scopes": ["branch", "user"]}
PROPOSE = "/logbook/setting-keys/voice.wake_word/propose"


@pytest.fixture
async def env(tmp_path, monkeypatch):
    async with make_client(tmp_path, monkeypatch) as pair:
        yield pair


@pytest.fixture
async def factory(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'r.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _skip_get(monkeypatch, entity):
    """Make session.get miss rows of ``entity`` (simulates a concurrent insert)."""
    original = AsyncSession.get

    async def get(self, cls, ident, *args, **kwargs):
        if cls is entity:
            return None
        return await original(self, cls, ident, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "get", get)


# 1. string "false" must not make a key operator-writable
async def test_string_false_is_rejected(env):
    tc, _ = env
    r = await tc.post(PROPOSE, json={"spec": {**WAKE, "operator_writable": "false"},
                                     "scope": "branch:7.0.1.1"}, headers=MAINT)
    assert r.status_code == 422


# 2. corrupt approved spec rows are treated as unapproved, never raise
@pytest.mark.parametrize(
    "spec", [5, ["x"], {"key": "bad", "type": ["x"]}, {"key": "bad", "scopes": 5},
             {"key": "bad", "write_role": {"beamline": ["a"]}}, {"key": "other"}],
)
async def test_corrupt_approved_spec_rows(factory, spec):
    async with factory() as s:
        s.add(SettingKeyRow(key="bad", status="approved", spec=spec))
        await s.commit()
    async with factory() as s:
        got, known = await spec_for(s, "bad")
    assert not known and got.key == "bad" and got.scopes == frozenset({"user"})


# 3. malformed specs on propose/approve are 422, not 500
@pytest.mark.parametrize("bad", [{"type": ["str"]}, {"scopes": 5}, {"policy": []},
                                 {"write_role": ["admin"]}])
async def test_propose_malformed_is_422(env, bad):
    tc, _ = env
    r = await tc.post(PROPOSE, json={"spec": {**WAKE, **bad}, "scope": "branch:7.0.1.1"},
                      headers=MAINT)
    assert r.status_code == 422


async def test_approve_malformed_edit_is_422(env):
    tc, _ = env
    await tc.post(PROPOSE, json={"spec": WAKE, "scope": "branch:7.0.1.1"}, headers=MAINT)
    r = await tc.post("/logbook/setting-keys/voice.wake_word/approve",
                      json={"spec": {"write_role": ["admin"]}}, headers=ADMIN)
    assert r.status_code == 422


# 4. a stale version must not overwrite a newer one
async def test_stale_version_update_raises(factory):
    async with factory() as s:
        s.add(ScopedSettingRow(scope="facility", key="k", value="a", updated_by="x"))
        await s.commit()
    async with factory() as s1, factory() as s2:
        r1 = await s1.get(ScopedSettingRow, ("facility", "k"))
        r2 = await s2.get(ScopedSettingRow, ("facility", "k"))
        r1.value = "b"
        await s1.commit()
        r2.value = "c"
        with pytest.raises(StaleDataError):
            await s2.commit()
    async with factory() as s:
        row = await s.get(ScopedSettingRow, ("facility", "k"))
    assert row.value == "b" and row.version == 2


async def test_versions_still_count_through_the_api(env):
    tc, _ = env
    u = "/logbook/scopes/branch:7.0.1.1/settings/tiled.url"
    assert (await tc.put(u, json={"value": "a"}, headers=MAINT)).json()["version"] == 1
    r = await tc.put(u, json={"value": "b"}, headers={**MAINT, "If-Match": "1"})
    assert r.json()["version"] == 2


# 5. insert races give 4xx, not 500
async def test_concurrent_propose_returns_existing(env, monkeypatch):
    tc, _ = env
    assert (await tc.post(PROPOSE, json={"spec": WAKE, "scope": "branch:7.0.1.1"},
                          headers=MAINT)).status_code == 201
    _skip_get(monkeypatch, SettingKeyRow)
    r = await tc.post(PROPOSE, json={"spec": WAKE, "scope": "branch:7.0.1.1"}, headers=MAINT)
    assert r.status_code == 201 and r.json()["status"] == "pending"


async def test_concurrent_key_pending_proposal(env, monkeypatch):
    tc, _ = env
    await tc.post(PROPOSE, json={"spec": WAKE, "scope": "branch:7.0.1.1"}, headers=MAINT)
    _skip_get(monkeypatch, SettingKeyRow)
    import json
    r = await tc.put("/logbook/scopes/branch:7.0.1.1/settings/voice.wake_word",
                     json={"value": "hey"}, headers={**MAINT, "X-Proposed-Spec": json.dumps(WAKE)})
    assert r.status_code == 409 and r.json()["extra"]["status"] == "pending"


async def test_concurrent_lock_is_409(env, monkeypatch):
    tc, _ = env
    lock = "/logbook/scopes/beamline:7.0.1/locks/tiled.url"
    assert (await tc.put(lock, json={}, headers=ADMIN)).status_code == 200
    _skip_get(monkeypatch, SettingLockRow)
    assert (await tc.put(lock, json={}, headers=ADMIN)).status_code == 409


# 6. ETag changes when a key is reset and re-created at the same version
async def test_etag_changes_after_delete_and_recreate(env):
    tc, _ = env
    snap = "/logbook/scopes/branch:7.0.1.1/settings"
    u = snap + "/tiled.url"
    await tc.put(u, json={"value": "a"}, headers=MAINT)
    e1 = (await tc.get(snap, headers=ALICE)).json()["etag"]
    await tc.delete(u, headers=MAINT)
    await tc.put(u, json={"value": "b"}, headers=MAINT)
    r = await tc.get(snap, headers={**ALICE, "If-None-Match": e1})
    assert r.status_code == 200 and r.json()["settings"]["tiled.url"] == "b"


# 7. an identity that isn't a valid scope name is refused cleanly
async def test_chain_with_unusable_identity_is_403(env):
    tc, _ = env
    r = await tc.get("/logbook/scopes/chain", headers=as_("a b"))
    assert r.status_code == 403


async def test_operator_still_cannot_write_after_fixes(env):
    tc, _ = env
    r = await tc.put("/logbook/scopes/beamline:7.0.1/settings/tiled.url", json={"value": "x"},
                     headers=OPER)
    assert r.status_code == 403
