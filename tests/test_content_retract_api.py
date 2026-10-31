"""Retracting and restoring shared content: maintainers+, history kept, quiet repeats."""
# ruff: noqa: F811  (the make_env fixture is imported from test_content_publish_api)
from tests.content_helpers import make_bundle
from tests.scoped_helpers import ALICE, MAINT, OPER
from tests.test_content_publish_api import BR, SUBJECT, URL, make_env, publish  # noqa: F401

ITEM = URL.format(scope=BR, type="plan", name="xafs_scan")


async def _audit(tc):
    return (await tc.get(f"/logbook/scopes/{BR}/audit",
                         params={"key": "content:plan/xafs_scan"}, headers=MAINT)).json()


async def test_retract_hides_from_list_keeps_history(make_env):
    tc, pub, _ = await make_env()
    await publish(tc, MAINT, make_bundle({"a.py": b"1"}))
    r = await tc.post(f"{ITEM}/retract", headers=MAINT)
    assert r.status_code == 200
    assert r.json() == {"scope": BR, "type": "plan", "name": "xafs_scan", "revision": None}
    assert (await tc.get(f"/logbook/scopes/{BR}/content", headers=ALICE)).json()["items"] == []
    hist = (await tc.get(f"{ITEM}/history", headers=ALICE)).json()
    assert [(h["revision"], h["current"]) for h in hist] == [(1, False)]
    assert [a["action"] for a in await _audit(tc)] == ["retract", "publish"]
    assert (SUBJECT, {"scope": BR, "type": "plan", "name": "xafs_scan", "revision": None}) in \
        pub.events


async def test_restore_brings_back_an_older_revision(make_env):
    tc, pub, _ = await make_env()
    v1 = make_bundle({"a.py": b"1"})
    await publish(tc, MAINT, v1)
    await publish(tc, MAINT, make_bundle({"a.py": b"2"}))
    r = await tc.post(f"{ITEM}/restore", json={"revision": 1}, headers=MAINT)
    assert r.status_code == 200 and r.json()["revision"] == 1
    assert (await tc.get(f"{ITEM}/bundle", headers=ALICE)).content == v1
    assert [a["action"] for a in await _audit(tc)][0] == "restore"
    assert (SUBJECT, {"scope": BR, "type": "plan", "name": "xafs_scan", "revision": 1}) in \
        pub.events


async def test_restore_after_retract(make_env):
    tc, _, _ = await make_env()
    await publish(tc, MAINT, make_bundle({"a.py": b"1"}))
    await tc.post(f"{ITEM}/retract", headers=MAINT)
    r = await tc.post(f"{ITEM}/restore", json={"revision": 1}, headers=MAINT)
    assert r.status_code == 200
    items = (await tc.get(f"/logbook/scopes/{BR}/content", headers=ALICE)).json()["items"]
    assert [i["name"] for i in items] == ["xafs_scan"]


async def test_restore_rejects_another_items_revision(make_env):
    tc, _, _ = await make_env()
    await publish(tc, MAINT, make_bundle({"a.py": b"1"}))
    await publish(tc, MAINT, make_bundle({"b.py": b"2"}), name="other")
    r = await tc.post(f"{ITEM}/restore", json={"revision": 2}, headers=MAINT)
    assert r.status_code == 422
    r = await tc.post(f"{ITEM}/restore", json={"revision": 999}, headers=MAINT)
    assert r.status_code == 422


async def test_restore_rejects_a_revision_whose_bundle_is_missing(make_env):
    tc, _, _ = await make_env()
    data = make_bundle({"a.py": b"1"})
    await publish(tc, MAINT, data)
    await publish(tc, MAINT, make_bundle({"a.py": b"2"}))
    import hashlib
    sha = hashlib.sha256(data).hexdigest()
    (tc.app.state.bundle_store._root / sha[:2] / sha).unlink()
    r = await tc.post(f"{ITEM}/restore", json={"revision": 1}, headers=MAINT)
    assert r.status_code == 422 and "missing" in r.json()["detail"].lower()


async def test_retract_and_restore_need_maintainer(make_env):
    tc, _, _ = await make_env()
    await publish(tc, MAINT, make_bundle({"a.py": b"1"}))
    for who in (OPER, ALICE):
        assert (await tc.post(f"{ITEM}/retract", headers=who)).status_code == 403
        assert (await tc.post(f"{ITEM}/restore", json={"revision": 1},
                              headers=who)).status_code == 403


async def test_unknown_item_is_404(make_env):
    tc, _, _ = await make_env()
    missing = URL.format(scope=BR, type="plan", name="nope")
    for r in (await tc.post(f"{missing}/retract", headers=MAINT),
              await tc.post(f"{missing}/restore", json={"revision": 1}, headers=MAINT)):
        assert r.status_code == 404 and "nope" in r.json()["detail"]  # the handler's 404


async def test_repeat_retract_and_restore_to_current_are_quiet(make_env):
    tc, pub, _ = await make_env()
    await publish(tc, MAINT, make_bundle({"a.py": b"1"}))
    assert (await tc.post(f"{ITEM}/restore", json={"revision": 1},
                          headers=MAINT)).status_code == 200
    await tc.post(f"{ITEM}/retract", headers=MAINT)
    events_before = len(pub.events)
    assert (await tc.post(f"{ITEM}/retract", headers=MAINT)).status_code == 200
    assert len(pub.events) == events_before
    assert [a["action"] for a in await _audit(tc)] == ["retract", "publish"]
