"""Shared content: list, download and history for any signed-in caller."""
import hashlib

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.content_helpers import make_bundle, seed_revision
from tests.scoped_helpers import ALICE, MAINT, make_client

BR = "branch:7.0.1.1"
BASE = "/logbook/scopes"


@pytest.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTENT_STORAGE_DIR", str(tmp_path / "bundles"))
    async with make_client(tmp_path, monkeypatch) as (tc, _pub):
        await tc.get("/health")
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'scoped.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        yield tc, factory, tc.app.state.bundle_store
        await engine.dispose()


async def test_list_shows_current_items_only_with_etag(env):
    tc, factory, store = env
    await seed_revision(factory, store, BR, "plan", "xafs_scan", make_bundle({"a.py": b"1"}))
    await seed_revision(factory, store, BR, "skill", "gone", make_bundle({"SKILL.md": b"x"}),
                        current=False)
    r = await tc.get(f"{BASE}/{BR}/content", headers=ALICE)
    assert r.status_code == 200
    body = r.json()
    assert [(i["type"], i["name"]) for i in body["items"]] == [("plan", "xafs_scan")]
    assert body["items"][0]["revision"] == 1 and body["items"][0]["published_by"] == "seed"
    again = await tc.get(f"{BASE}/{BR}/content",
                         headers={**ALICE, "If-None-Match": r.headers["ETag"]})
    assert again.status_code == 304


async def test_any_signed_in_user_reads_but_not_anonymous(env, monkeypatch):
    tc, _, _ = env
    assert (await tc.get(f"{BASE}/{BR}/content", headers=ALICE)).status_code == 200
    # Dev mode stands in a dev-user for header-less requests; production does not.
    monkeypatch.setattr("lightfall_logbook.auth.dev_mode_enabled", lambda: False)
    assert (await tc.get(f"{BASE}/{BR}/content")).status_code == 401


@pytest.mark.parametrize("scope,status", [("beamline:9.9.9", 404), ("user:alice", 422)])
async def test_unknown_and_user_scopes(env, scope, status):
    tc, _, _ = env
    assert (await tc.get(f"{BASE}/{scope}/content", headers=ALICE)).status_code == status


async def test_unknown_type_is_422(env):
    tc, _, _ = env
    r = await tc.get(f"{BASE}/{BR}/content/macro/x/history", headers=ALICE)
    assert r.status_code == 422


async def test_download_current_and_older_revision(env):
    tc, factory, store = env
    v1 = make_bundle({"a.py": b"one"})
    v2 = make_bundle({"a.py": b"two"})
    r1 = await seed_revision(factory, store, BR, "plan", "p", v1)
    await seed_revision(factory, store, BR, "plan", "p", v2)
    other = await seed_revision(factory, store, BR, "plan", "q", make_bundle({"b.py": b"q"}))
    cur = await tc.get(f"{BASE}/{BR}/content/plan/p/bundle", headers=ALICE)
    assert cur.status_code == 200 and cur.content == v2
    assert cur.headers["X-Content-SHA256"] == hashlib.sha256(v2).hexdigest()
    old = await tc.get(f"{BASE}/{BR}/content/plan/p/bundle", params={"revision": r1},
                       headers=ALICE)
    assert old.status_code == 200 and old.content == v1
    wrong = await tc.get(f"{BASE}/{BR}/content/plan/p/bundle", params={"revision": other},
                         headers=ALICE)
    assert wrong.status_code == 404


async def test_retracted_item_has_no_current_bundle(env):
    tc, factory, store = env
    await seed_revision(factory, store, BR, "plan", "p", make_bundle({"a.py": b"1"}),
                        current=False)
    assert (await tc.get(f"{BASE}/{BR}/content/plan/p/bundle",
                         headers=ALICE)).status_code == 404


async def test_download_missing_file_is_404_listing_still_works(env):
    tc, factory, store = env
    data = make_bundle({"a.py": b"lost"})
    await seed_revision(factory, store, BR, "plan", "p", data)
    sha = hashlib.sha256(data).hexdigest()
    (store._root / sha[:2] / sha).unlink()
    r = await tc.get(f"{BASE}/{BR}/content/plan/p/bundle", headers=ALICE)
    assert r.status_code == 404 and "missing" in r.json()["detail"].lower()
    assert (await tc.get(f"{BASE}/{BR}/content", headers=ALICE)).status_code == 200


async def test_history_newest_first_with_current_flag(env):
    tc, factory, store = env
    r1 = await seed_revision(factory, store, BR, "agent", "helper",
                             make_bundle({"helper.md": b"1"}), message="first")
    r2 = await seed_revision(factory, store, BR, "agent", "helper",
                             make_bundle({"helper.md": b"2"}), message="second")
    hist = (await tc.get(f"{BASE}/{BR}/content/agent/helper/history", headers=MAINT)).json()
    assert [(h["revision"], h["current"], h["message"]) for h in hist] == [
        (r2, True, "second"), (r1, False, "first")]
    missing = await tc.get(f"{BASE}/{BR}/content/agent/nope/history", headers=ALICE)
    assert missing.status_code == 404
