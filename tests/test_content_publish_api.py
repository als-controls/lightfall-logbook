"""Publishing shared content: maintainers+, integrity, policy (admins exempt), idempotency."""
import hashlib
import json
import os

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from lightfall_logbook.scoped_models import ContentItemRow, ContentRevisionRow
from tests.content_helpers import make_bundle
from tests.scoped_helpers import ADMIN, ALICE, CONFIG, MAINT, OPER, make_client

BR = "branch:7.0.1.1"
URL = "/logbook/scopes/{scope}/content/{type}/{name}"
SUBJECT = "lightfall.content.changed"


def manifest(data: bytes, type_="plan", name="xafs_scan", **over):
    m = {"type": type_, "name": name, "file_count": 1, "uncompressed_size": 10,
         "sha256": hashlib.sha256(data).hexdigest(), "message": "first version"}
    m.update(over)
    return m


async def publish(tc, headers, data, *, scope=BR, type_="plan", name="xafs_scan", **over):
    return await tc.post(URL.format(scope=scope, type=type_, name=name), headers=headers,
                         data={"manifest": json.dumps(manifest(data, type_, name, **over))},
                         files={"bundle": ("bundle.zip", data, "application/zip")})


@pytest.fixture
async def make_env(tmp_path, monkeypatch):
    engines = []

    async def _make(extra=""):
        monkeypatch.setenv("CONTENT_STORAGE_DIR", str(tmp_path / "bundles"))
        cm = make_client(tmp_path, monkeypatch, config=CONFIG + extra)
        tc, pub = await cm.__aenter__()
        await tc.get("/health")
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'scoped.db'}")
        engines.append((cm, engine))
        return tc, pub, async_sessionmaker(engine, expire_on_commit=False)
    yield _make
    for cm, engine in engines:
        await engine.dispose()
        await cm.__aexit__(None, None, None)


@pytest.fixture
async def env(make_env):
    return await make_env()


async def test_maintainer_publishes_and_everyone_sees_it(env):
    tc, pub, _ = env
    data = make_bundle({"xafs_scan.py": b"def plan(): ...\n"})
    r = await publish(tc, MAINT, data)
    assert r.status_code == 201, r.text
    body = r.json()
    assert (body["scope"], body["type"], body["name"], body["revision"]) == (
        BR, "plan", "xafs_scan", 1)
    assert body["sha256"] == hashlib.sha256(data).hexdigest() and body["size"] == len(data)
    items = (await tc.get(f"/logbook/scopes/{BR}/content", headers=ALICE)).json()["items"]
    assert [(i["name"], i["published_by"]) for i in items] == [("xafs_scan", "mia")]
    got = await tc.get(URL.format(scope=BR, type="plan", name="xafs_scan") + "/bundle",
                       headers=ALICE)
    assert got.content == data
    audit = (await tc.get(f"/logbook/scopes/{BR}/audit",
                          params={"key": "content:plan/xafs_scan"}, headers=MAINT)).json()
    assert [a["action"] for a in audit] == ["publish"]
    assert (SUBJECT, {"scope": BR, "type": "plan", "name": "xafs_scan", "revision": 1}) in \
        pub.events


@pytest.mark.parametrize("who", [OPER, ALICE])
async def test_non_maintainers_cannot_publish(env, who):
    tc, _, factory = env
    r = await publish(tc, who, make_bundle({"a.py": b"x"}))
    assert r.status_code == 403
    async with factory() as s:
        assert (await s.execute(select(ContentRevisionRow))).first() is None


async def test_user_scope_refused(env):
    tc, _, _ = env
    r = await publish(tc, ALICE, make_bundle({"a.py": b"x"}), scope="user:alice")
    assert r.status_code == 422


async def test_sha_mismatch_and_url_mismatch(env):
    tc, _, _ = env
    data = make_bundle({"a.py": b"x"})
    assert (await publish(tc, MAINT, data, sha256="0" * 64)).status_code == 422
    r = await tc.post(URL.format(scope=BR, type="plan", name="other"), headers=MAINT,
                      data={"manifest": json.dumps(manifest(data))},
                      files={"bundle": ("b.zip", data, "application/zip")})
    assert r.status_code == 422


@pytest.mark.parametrize("bad", [
    {"file_count": 0}, {"uncompressed_size": -1}, {"surprise": 1}, {"message": "x" * 1001},
    {"source": {"path": "p" * 3000}},
])
async def test_bad_manifest_is_422(env, bad):
    tc, _, _ = env
    assert (await publish(tc, MAINT, make_bundle({"a.py": b"x"}), **bad)).status_code == 422


@pytest.mark.parametrize("name", ["..", "_hidden", "has space", "a" * 65, "a/b"])
async def test_bad_names_are_refused(env, name):
    tc, _, _ = env
    r = await publish(tc, MAINT, make_bundle({"a.py": b"x"}), name=name)
    if "/" in name or name == "..":  # the URL layer handles these; they never reach the route
        assert r.status_code in (404, 405)
    else:
        assert r.status_code == 422


SMALL = "\n[content]\nmax_bundle_bytes = 400\nmax_uncompressed_bytes = 1000\nmax_files = 3\n"


@pytest.mark.parametrize("over,limit", [
    ({}, "max_bundle_bytes"),
    ({"uncompressed_size": 5000}, "max_uncompressed_bytes"),
    ({"file_count": 4}, "max_files"),
])
async def test_policy_limits_for_maintainers_not_admins(make_env, over, limit):
    tc, _, _ = await make_env(SMALL)
    big = os.urandom(600) if limit == "max_bundle_bytes" else make_bundle({"a.py": b"x"})
    r = await publish(tc, MAINT, big, **over)
    assert r.status_code == 413 and limit in r.json()["detail"]
    assert (await publish(tc, ADMIN, big, **over)).status_code == 201


async def test_over_hard_limit_is_413_for_everyone(make_env):
    tc, _, _ = await make_env(
        "\n[content]\nmax_bundle_bytes = 1000\nhard_max_bundle_bytes = 2000\n")
    mid = os.urandom(5000)                       # past the hard limit, inside the body cap
    assert (await publish(tc, ADMIN, mid)).status_code == 413
    huge = os.urandom(200_000)                   # past the route's body cap too
    assert (await publish(tc, ADMIN, huge)).status_code == 413


async def test_identical_retry_is_idempotent(env):
    tc, _, _ = env
    data = make_bundle({"a.py": b"x"})
    first = await publish(tc, MAINT, data)
    again = await publish(tc, MAINT, data)
    assert first.status_code == 201 and again.status_code == 200
    assert again.json()["revision"] == first.json()["revision"]
    hist = (await tc.get(URL.format(scope=BR, type="plan", name="xafs_scan") + "/history",
                         headers=MAINT)).json()
    assert len(hist) == 1
    audit = (await tc.get(f"/logbook/scopes/{BR}/audit",
                          params={"key": "content:plan/xafs_scan"}, headers=MAINT)).json()
    assert len(audit) == 1


async def test_second_publish_makes_revision_2(env):
    tc, _, _ = env
    await publish(tc, MAINT, make_bundle({"a.py": b"1"}))
    r = await publish(tc, MAINT, make_bundle({"a.py": b"2"}))
    assert r.status_code == 201 and r.json()["revision"] == 2


async def test_agent_flag_from_manifest_or_header(env):
    tc, _, _ = env
    await publish(tc, MAINT, make_bundle({"a.py": b"1"}), name="a1", agent=True)
    await publish(tc, {**MAINT, "X-Lightfall-Agent": "1"}, make_bundle({"a.py": b"2"}),
                  name="a2")
    await publish(tc, MAINT, make_bundle({"a.py": b"3"}), name="a3")
    for name, agent in (("a1", True), ("a2", True), ("a3", False)):
        hist = (await tc.get(URL.format(scope=BR, type="plan", name=name) + "/history",
                             headers=MAINT)).json()
        assert hist[0]["agent"] is agent


async def test_author_and_manifest_are_recorded(env):
    tc, _, factory = env
    await publish(tc, MAINT, make_bundle({"a.py": b"1"}), author="alice",
                  source={"machine": "tsuru", "path": "~/lightfall/plans/xafs_scan.py"})
    async with factory() as s:
        rev = (await s.execute(select(ContentRevisionRow))).scalar_one()
    assert rev.author == "alice" and rev.manifest["source"]["machine"] == "tsuru"


async def test_concurrent_publish_one_wins(env, monkeypatch):
    """Another worker moves the item's pointer between our read and our update: 409."""
    tc, _, factory = env
    await publish(tc, MAINT, make_bundle({"a.py": b"base"}))
    store = tc.app.state.bundle_store
    real_put = store.put

    def racing_put(data):
        sha = real_put(data)
        import sqlite3
        with sqlite3.connect(str(factory.kw["bind"].url.database)) as conn:
            conn.execute("UPDATE content_items SET current_revision = 99 "
                         "WHERE name = 'xafs_scan'")
        return sha

    monkeypatch.setattr(store, "put", racing_put)
    r = await publish(tc, MAINT, make_bundle({"a.py": b"mine"}))
    assert r.status_code == 409
    async with factory() as s:
        item = await s.get(ContentItemRow, (BR, "plan", "xafs_scan"))
        assert item.current_revision == 99          # the other writer's value stands
        revs = (await s.execute(select(ContentRevisionRow.id))).scalars().all()
        assert revs == [1]                          # no orphan revision of ours
