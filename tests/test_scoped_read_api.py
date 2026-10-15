import pytest

from tests.scoped_helpers import ALICE, MAINT, as_, make_client


@pytest.fixture
async def env(tmp_path, monkeypatch):
    async with make_client(tmp_path, monkeypatch) as pair:
        yield pair


async def test_chain_for_branch(env):
    tc, _ = env
    r = await tc.get("/logbook/scopes/chain", params={"bind": "branch:7.0.1.1"}, headers=MAINT)
    assert r.status_code == 200
    body = r.json()
    assert body["chain"] == ["facility", "beamline:7.0.1", "branch:7.0.1.1", "user:mia"]
    assert body["roles"] == {
        "facility": None, "beamline:7.0.1": "maintainer", "branch:7.0.1.1": "maintainer",
        "user:mia": "owner",
    }
    assert body["entitled"] == ["beamline:7.0.1", "branch:7.0.1.1"]
    assert body["facility_label"] == "ALS"


async def test_chain_defaults_to_facility(env):
    tc, _ = env
    r = await tc.get("/logbook/scopes/chain", headers=ALICE)
    assert r.json()["chain"] == ["facility", "user:alice"]
    assert r.json()["entitled"] == []


@pytest.mark.parametrize("bind", ["beamline:9.9.9", "branch:nope", "garbage", "user:bob"])
async def test_chain_rejects_bad_bind(env, bind):
    tc, _ = env
    r = await tc.get("/logbook/scopes/chain", params={"bind": bind}, headers=ALICE)
    assert r.status_code == 404


async def test_empty_snapshot_and_etag_304(env):
    tc, _ = env
    r = await tc.get("/logbook/scopes/branch:7.0.1.1/settings", headers=ALICE)
    assert r.status_code == 200
    body = r.json()
    assert body["scope"] == "branch:7.0.1.1"
    assert body["settings"] == {} and body["versions"] == {} and body["locks"] == {}
    assert r.headers["etag"] == body["etag"]
    r2 = await tc.get("/logbook/scopes/branch:7.0.1.1/settings",
                      headers={**ALICE, "If-None-Match": body["etag"]})
    assert r2.status_code == 304


async def test_user_snapshot_only_for_owner(env):
    tc, _ = env
    assert (await tc.get("/logbook/scopes/user:alice/settings", headers=ALICE)).status_code == 200
    r = await tc.get("/logbook/scopes/user:alice/settings", headers=as_("bob"))
    assert r.status_code == 403


@pytest.mark.parametrize("scope", ["beamline:9.9.9", "beamline:7.0.10", "bogus", "facility:x"])
async def test_snapshot_unknown_scope_404(env, scope):
    tc, _ = env
    assert (await tc.get(f"/logbook/scopes/{scope}/settings", headers=ALICE)).status_code == 404
