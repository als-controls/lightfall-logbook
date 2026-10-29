"""lightfall_settings.DEPLOYMENT_SPECS enforced by the server (the 7.0.1.1 incident)."""
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from lightfall_logbook.models import UserSettingRow
from lightfall_logbook.scoped_models import ScopedSettingRow, SettingAuditRow
from lightfall_logbook.scoped_store import migrate_user_settings
from tests.scoped_helpers import ALICE, MAINT, make_client

URL = "/logbook/scopes/{scope}/settings/{key}"


def row(rid: str, path: str = "/x.json") -> dict:
    return {"id": rid, "type": "happi", "name": rid, "enabled": True,
            "config": {"path": path, "beamline": ""}}


@pytest.fixture
async def env(tmp_path, monkeypatch):
    async with make_client(tmp_path, monkeypatch) as (tc, _pub):
        await tc.get("/health")  # startup has created the tables
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'scoped.db'}")
        yield tc, async_sessionmaker(engine, expire_on_commit=False)
        await engine.dispose()


async def _plant(factory, scope: str, key: str, value) -> None:
    """A row written before the server enforced the spec."""
    async with factory() as s:
        s.add(ScopedSettingRow(scope=scope, key=key, value=value, updated_by="old-client"))
        await s.commit()


@pytest.mark.parametrize("key", ["tiled_beamline", "ipc_topic_prefix", "current_beamline"])
async def test_user_scope_write_of_identity_key_is_refused(env, key):
    tc, _ = env
    r = await tc.put(URL.format(scope="user:alice", key=key), json={"value": "12.0.2.1"},
                     headers=ALICE)
    assert r.status_code == 409
    assert "cannot be set at user scope" in r.json()["detail"]


async def test_maintainer_sets_identity_key_at_branch(env):
    tc, _ = env
    r = await tc.put(URL.format(scope="branch:7.0.1.1", key="tiled_beamline"),
                     json={"value": "7.0.1.1"}, headers=MAINT)
    assert r.status_code == 200


async def test_device_backends_union_rows_accepted_at_user_and_beamline(env):
    tc, _ = env
    r = await tc.put(URL.format(scope="beamline:7.0.1", key="device_backends"),
                     json={"value": {"a": row("a")}}, headers=MAINT)
    assert r.status_code == 200
    r = await tc.put(URL.format(scope="user:alice", key="device_backends"),
                     json={"value": {"b": row("b")}}, headers=ALICE)
    assert r.status_code == 200


async def test_list_shaped_device_backends_are_unprocessable(env):
    tc, _ = env
    r = await tc.put(URL.format(scope="user:alice", key="device_backends"),
                     json={"value": [row("a")]}, headers=ALICE)
    assert r.status_code == 422


async def test_user_cannot_remove_a_beamline_row(env):
    tc, _ = env
    r = await tc.put(URL.format(scope="user:alice", key="device_backends"),
                     json={"value": {"a": None}}, headers=ALICE)
    assert r.status_code == 422


async def test_deployment_keys_cannot_be_proposed(env):
    tc, _ = env
    r = await tc.post("/logbook/setting-keys/device_backends/propose",
                      json={"spec": {"scopes": ["user"]}, "scope": "beamline:7.0.1"},
                      headers=MAINT)
    assert r.status_code == 409


async def test_snapshot_hides_values_the_spec_forbids_at_that_scope(env):
    tc, factory = env
    await _plant(factory, "user:alice", "tiled_beamline", "12.0.2.1")
    await _plant(factory, "user:alice", "theme", "dark")
    snap = (await tc.get("/logbook/scopes/user:alice/settings", headers=ALICE)).json()
    assert snap["settings"] == {"theme": "dark"}
    assert "tiled_beamline" not in snap["versions"]


async def test_snapshot_keeps_unapproved_keys(env):
    """Unapproved keys fall back to user-only; their rows elsewhere are left visible."""
    tc, factory = env
    await _plant(factory, "beamline:7.0.1", "some.revoked_key", 1)
    snap = (await tc.get("/logbook/scopes/beamline:7.0.1/settings", headers=ALICE)).json()
    assert snap["settings"] == {"some.revoked_key": 1}


async def test_user_may_delete_their_stray_identity_row(env):
    tc, factory = env
    await _plant(factory, "user:alice", "tiled_beamline", "12.0.2.1")
    r = await tc.delete(URL.format(scope="user:alice", key="tiled_beamline"), headers=ALICE)
    assert r.status_code == 204
    async with factory() as s:
        assert await s.get(ScopedSettingRow, ("user:alice", "tiled_beamline")) is None
        audit = (await s.execute(select(SettingAuditRow))).scalars().all()
    assert [(a.action, a.old) for a in audit] == [("delete", "12.0.2.1")]


async def test_stray_delete_still_checks_ownership(env):
    tc, factory = env
    await _plant(factory, "user:bob", "tiled_beamline", "12.0.2.1")
    r = await tc.delete(URL.format(scope="user:bob", key="tiled_beamline"), headers=ALICE)
    assert r.status_code == 403


async def test_legacy_migration_skips_keys_user_scope_forbids(env):
    _tc, factory = env
    async with factory() as s:
        s.add_all([
            UserSettingRow(user_id="carol", beamline="", key="tiled_beamline", value="12.0.2.1"),
            UserSettingRow(user_id="carol", beamline="", key="theme", value="dark"),
        ])
        await s.commit()
    assert await migrate_user_settings(factory) == 1
    async with factory() as s:
        assert await s.get(ScopedSettingRow, ("user:carol", "theme")) is not None
        assert await s.get(ScopedSettingRow, ("user:carol", "tiled_beamline")) is None
