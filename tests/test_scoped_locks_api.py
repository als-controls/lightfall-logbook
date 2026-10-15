import pytest

from tests.scoped_helpers import ADMIN, ALICE, MAINT, STAFF, as_, make_client

S = "/logbook/scopes/{scope}/settings/tiled.url"
L = "/logbook/scopes/{scope}/locks/tiled.url"


@pytest.fixture
async def env(tmp_path, monkeypatch):
    async with make_client(tmp_path, monkeypatch) as pair:
        yield pair


async def test_lock_without_row_freezes_inherited_value(env):
    tc, pub = env
    assert (await tc.put(S.format(scope="facility"), json={"value": "a"},
                         headers=STAFF)).status_code == 200
    r = await tc.put(L.format(scope="beamline:7.0.1"), json={"reason": "safety"}, headers=ADMIN)
    assert r.status_code == 200
    assert r.json()["locked_by"] == "ada" and r.json()["version"] == 1
    snap = (await tc.get("/logbook/scopes/beamline:7.0.1/settings", headers=ALICE)).json()
    assert snap["settings"] == {"tiled.url": "a"}
    assert snap["locks"]["tiled.url"]["reason"] == "safety"
    assert (await tc.put(S.format(scope="branch:7.0.1.1"), json={"value": "x"},
                         headers=MAINT)).status_code == 423
    assert (await tc.put(S.format(scope="beamline:7.0.1"), json={"value": "x"},
                         headers={**ADMIN, "If-Match": "1"})).status_code == 423
    # user scope is checked against facility locks only (planning decision 3)
    assert (await tc.put(S.format(scope="user:alice"), json={"value": "x"},
                         headers=ALICE)).status_code == 200
    assert ("lightfall.settings.changed",
            {"scope": "beamline:7.0.1", "key": "tiled.url", "version": 1}) in pub.events


async def test_lock_with_no_effective_value_writes_no_row(env):
    tc, _ = env
    r = await tc.put(L.format(scope="branch:7.0.1.1"), json={}, headers=ADMIN)
    assert r.status_code == 200 and r.json()["version"] == 0
    snap = (await tc.get("/logbook/scopes/branch:7.0.1.1/settings", headers=ALICE)).json()
    assert snap["settings"] == {} and "tiled.url" in snap["locks"]


async def test_facility_lock_blocks_user_scope(env):
    tc, _ = env
    await tc.put(S.format(scope="facility"), json={"value": "a"}, headers=STAFF)
    assert (await tc.put(L.format(scope="facility"), json={},
                         headers=STAFF)).status_code == 403  # BCG_staff can't lock
    assert (await tc.put(L.format(scope="facility"), json={}, headers=ADMIN)).status_code == 200
    assert (await tc.put(S.format(scope="user:alice"), json={"value": "x"},
                         headers=ALICE)).status_code == 423


async def test_lock_permissions_and_unlock(env):
    tc, _ = env
    assert (await tc.put(L.format(scope="beamline:7.0.1"), json={},
                         headers=MAINT)).status_code == 403
    assert (await tc.put(L.format(scope="user:alice"), json={},
                         headers=ALICE)).status_code == 409
    assert (await tc.put(L.format(scope="beamline:7.0.1"), json={},
                         headers=ADMIN)).status_code == 200
    assert (await tc.delete(L.format(scope="beamline:7.0.1"), headers=MAINT)).status_code == 403
    assert (await tc.delete(L.format(scope="beamline:7.0.1"), headers=ADMIN)).status_code == 204
    assert (await tc.delete(L.format(scope="beamline:7.0.1"), headers=ADMIN)).status_code == 404


async def test_audit_trail(env):
    tc, _ = env
    await tc.put(S.format(scope="branch:7.0.1.1"), json={"value": "a"},
                 headers={**MAINT, "X-Lightfall-Agent": "1"})
    await tc.put(S.format(scope="branch:7.0.1.1"), json={"value": "b"},
                 headers={**MAINT, "If-Match": "1"})
    await tc.put(L.format(scope="branch:7.0.1.1"), json={"reason": "r"}, headers=ADMIN)
    r = await tc.get("/logbook/scopes/branch:7.0.1.1/audit", headers=MAINT)
    assert r.status_code == 200
    rows = r.json()
    assert [x["action"] for x in rows] == ["lock", "set", "set"]
    assert rows[1]["old"] == "a" and rows[1]["new"] == "b" and rows[1]["agent"] is False
    assert rows[2]["agent"] is True and rows[2]["actor"] == "mia"
    assert len((await tc.get("/logbook/scopes/branch:7.0.1.1/audit",
                             params={"limit": 1}, headers=MAINT)).json()) == 1
    assert (await tc.get("/logbook/scopes/branch:7.0.1.1/audit",
                         headers=ALICE)).status_code == 403
    assert (await tc.get("/logbook/scopes/user:alice/audit", headers=ALICE)).status_code == 200
    assert (await tc.get("/logbook/scopes/user:alice/audit",
                         headers=as_("bob"))).status_code == 403
