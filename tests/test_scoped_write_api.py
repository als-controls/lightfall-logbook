import pytest

from tests.scoped_helpers import ALICE, MAINT, OPER, as_, make_client

URL = "/logbook/scopes/{scope}/settings/{key}"


@pytest.fixture
async def env(tmp_path, monkeypatch):
    async with make_client(tmp_path, monkeypatch) as pair:
        yield pair


def u(scope, key="tiled.url"):
    return URL.format(scope=scope, key=key)


async def test_maintainer_sets_branch_value(env):
    tc, pub = env
    r = await tc.put(u("branch:7.0.1.1"), json={"value": "https://t"}, headers=MAINT)
    assert r.status_code == 200
    assert r.json() == {"scope": "branch:7.0.1.1", "key": "tiled.url",
                        "value": "https://t", "version": 1}
    snap = (await tc.get("/logbook/scopes/branch:7.0.1.1/settings", headers=ALICE)).json()
    assert snap["settings"] == {"tiled.url": "https://t"} and snap["versions"] == {"tiled.url": 1}
    assert ("lightfall.settings.changed",
            {"scope": "branch:7.0.1.1", "key": "tiled.url", "version": 1}) in pub.events


async def test_update_needs_matching_if_match(env):
    tc, _ = env
    await tc.put(u("beamline:7.0.1"), json={"value": "a"}, headers=MAINT)
    assert (await tc.put(u("beamline:7.0.1"), json={"value": "b"},
                         headers=MAINT)).status_code == 412
    assert (await tc.put(u("beamline:7.0.1"), json={"value": "b"},
                         headers={**MAINT, "If-Match": "7"})).status_code == 412
    r = await tc.put(u("beamline:7.0.1"), json={"value": "b"}, headers={**MAINT, "If-Match": "1"})
    assert r.status_code == 200 and r.json()["version"] == 2


async def test_create_with_stale_if_match_is_412(env):
    tc, _ = env
    r = await tc.put(u("beamline:7.0.1"), json={"value": "a"}, headers={**MAINT, "If-Match": "3"})
    assert r.status_code == 412


async def test_role_checks(env):
    tc, _ = env
    assert (await tc.put(u("beamline:7.0.1"), json={"value": "a"},
                         headers=OPER)).status_code == 403
    assert (await tc.put(u("beamline:7.0.1"), json={"value": "a"},
                         headers=ALICE)).status_code == 403
    assert (await tc.put(u("beamline:8.3.2"), json={"value": "a"},
                         headers=MAINT)).status_code == 403
    assert (await tc.put(u("user:alice"), json={"value": "a"}, headers=ALICE)).status_code == 200
    assert (await tc.put(u("user:bob"), json={"value": "a"}, headers=ALICE)).status_code == 403


async def test_unknown_scope_never_written(env):
    tc, _ = env
    r = await tc.put(u("beamline:9.9.9"), json={"value": "a"},
                     headers=as_("ada", "/beamlines/_all/admin"))
    assert r.status_code == 404


async def test_bad_value_is_422(env):
    tc, _ = env
    assert (await tc.put(u("branch:7.0.1.1"), json={"value": 5},
                         headers=MAINT)).status_code == 422


async def test_core_union_key(env):
    tc, _ = env
    r = await tc.put(u("branch:7.0.1.1", "plugins.enabled"),
                     json={"value": {"deck": True, "voice": None}}, headers=MAINT)
    assert r.status_code == 200 and r.json()["value"] == {"deck": True, "voice": None}


async def test_delete_resets_to_inherited(env):
    tc, pub = env
    await tc.put(u("branch:7.0.1.1"), json={"value": "a"}, headers=MAINT)
    assert (await tc.delete(u("branch:7.0.1.1"), headers=MAINT)).status_code == 204
    snap = (await tc.get("/logbook/scopes/branch:7.0.1.1/settings", headers=ALICE)).json()
    assert snap["settings"] == {}
    assert ("lightfall.settings.changed",
            {"scope": "branch:7.0.1.1", "key": "tiled.url", "version": 0}) in pub.events
    assert (await tc.delete(u("branch:7.0.1.1"), headers=MAINT)).status_code == 404
    assert (await tc.delete(u("beamline:7.0.1"), headers=OPER)).status_code == 403
