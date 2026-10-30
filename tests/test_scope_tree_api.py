"""The scope tree lives in the database; facility admins add and remove beamlines/branches."""
import pytest

from tests.scoped_helpers import ADMIN, ALICE, MAINT, make_client

TREE = "/logbook/scopes/tree"


@pytest.fixture
async def env(tmp_path, monkeypatch):
    async with make_client(tmp_path, monkeypatch) as pair:
        yield pair


async def _tree(tc):
    body = (await tc.get("/logbook/scopes/chain", headers=ADMIN)).json()
    return [(e["scope"], e["parent"]) for e in body["tree"]]


async def test_config_tree_is_seeded_into_the_database(env):
    tc, _ = env
    assert await _tree(tc) == [("beamline:7.0.1", None), ("beamline:8.3.2", None),
                               ("branch:7.0.1.1", "beamline:7.0.1")]


async def test_admin_adds_a_beamline_and_a_branch_then_writes_there(env):
    tc, pub = env
    r = await tc.post(TREE, json={"scope": "beamline:12.0.2"}, headers=ADMIN)
    assert r.status_code == 201, r.text
    r = await tc.post(TREE, json={"scope": "branch:12.0.2.1", "parent": "beamline:12.0.2"},
                      headers=ADMIN)
    assert r.status_code == 201, r.text
    assert ("branch:12.0.2.1", "beamline:12.0.2") in await _tree(tc)
    chain = (await tc.get("/logbook/scopes/chain", params={"bind": "branch:12.0.2.1"},
                          headers=ADMIN)).json()
    assert chain["chain"][:3] == ["facility", "beamline:12.0.2", "branch:12.0.2.1"]
    r = await tc.put("/logbook/scopes/branch:12.0.2.1/settings/tiled.url",
                     json={"value": "https://t"}, headers=ADMIN)
    assert r.status_code == 200
    assert ("lightfall.settings.changed", {"scope": "facility", "key": "", "version": 0}) in \
        pub.events


@pytest.mark.parametrize("body,status", [
    ({"scope": "branch:9.9.9.1", "parent": "beamline:9.9.9"}, 422),   # parent unknown
    ({"scope": "branch:9.9.9.1"}, 422),                               # branch needs a parent
    ({"scope": "beamline:9.9.9", "parent": "beamline:7.0.1"}, 422),   # beamline has none
    ({"scope": "user:bob"}, 422),
    ({"scope": "facility"}, 422),
    ({"scope": "beamline:_all"}, 422),
    ({"scope": "beamline:7.0.1"}, 409),                               # exists
])
async def test_bad_additions_are_refused(env, body, status):
    tc, _ = env
    assert (await tc.post(TREE, json=body, headers=ADMIN)).status_code == status


async def test_only_facility_admins_change_the_tree(env):
    tc, _ = env
    for who in (MAINT, ALICE):
        assert (await tc.post(TREE, json={"scope": "beamline:9.9.9"},
                              headers=who)).status_code == 403
        assert (await tc.delete(f"{TREE}/beamline:8.3.2", headers=who)).status_code == 403


async def test_remove_only_when_empty(env):
    tc, _ = env
    # 7.0.1 has a branch; give 8.3.2 a value; a fresh beamline is empty.
    assert (await tc.delete(f"{TREE}/beamline:7.0.1", headers=ADMIN)).status_code == 409
    await tc.put("/logbook/scopes/beamline:8.3.2/settings/tiled.url", json={"value": "x"},
                 headers=ADMIN)
    assert (await tc.delete(f"{TREE}/beamline:8.3.2", headers=ADMIN)).status_code == 409
    await tc.post(TREE, json={"scope": "beamline:9.9.9"}, headers=ADMIN)
    assert (await tc.delete(f"{TREE}/beamline:9.9.9", headers=ADMIN)).status_code == 204
    assert ("beamline:9.9.9", None) not in await _tree(tc)
    assert (await tc.delete(f"{TREE}/beamline:9.9.9", headers=ADMIN)).status_code == 404


async def test_removed_scope_is_unknown_again(env):
    tc, _ = env
    await tc.post(TREE, json={"scope": "beamline:9.9.9"}, headers=ADMIN)
    await tc.delete(f"{TREE}/beamline:9.9.9", headers=ADMIN)
    r = await tc.get("/logbook/scopes/chain", params={"bind": "beamline:9.9.9"}, headers=ADMIN)
    assert r.status_code == 404


async def test_tree_changes_are_audited(env):
    tc, _ = env
    await tc.post(TREE, json={"scope": "beamline:9.9.9", "label": "COSMIC"}, headers=ADMIN)
    audit = (await tc.get("/logbook/scopes/beamline:9.9.9/audit", headers=ADMIN)).json()
    assert [(a["action"], a["actor"]) for a in audit] == [("scope_add", "ada")]
