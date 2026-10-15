import json

import pytest

from tests.scoped_helpers import ADMIN, ALICE, MAINT, OPER, STAFF, make_client

PUT = "/logbook/scopes/branch:7.0.1.1/settings/voice.wake_word"
WAKE = {"key": "voice.wake_word", "type": "str", "scopes": ["branch", "user"]}


@pytest.fixture
async def env(tmp_path, monkeypatch):
    async with make_client(tmp_path, monkeypatch) as pair:
        yield pair


async def test_unknown_key_above_user_scope_is_pending(env):
    tc, pub = env
    r = await tc.put(PUT, json={"value": "hey"}, headers=MAINT)
    assert r.status_code == 409
    assert r.json()["extra"] == {"code": "key_pending", "status": "unproposed"}

    r = await tc.put(PUT, json={"value": "hey"},
                     headers={**MAINT, "X-Proposed-Spec": json.dumps(WAKE)})
    assert r.status_code == 409 and r.json()["extra"]["status"] == "pending"
    assert ("lightfall.settings.keys",
            {"key": "voice.wake_word", "status": "pending"}) in pub.events

    listed = (await tc.get("/logbook/setting-keys", params={"status": "pending"},
                           headers=MAINT)).json()
    assert [k["key"] for k in listed] == ["voice.wake_word"]
    assert listed[0]["proposed_by"] == "mia" and listed[0]["proposed_scope"] == "branch:7.0.1.1"
    assert (await tc.get("/logbook/setting-keys", headers=ALICE)).status_code == 403


async def test_unknown_key_at_user_scope_needs_no_approval(env):
    tc, _ = env
    r = await tc.put("/logbook/scopes/user:alice/settings/voice.wake_word",
                     json={"value": "hey"}, headers=ALICE)
    assert r.status_code == 200


async def test_operator_gets_403_not_pending(env):
    tc, _ = env
    assert (await tc.put(PUT, json={"value": "hey"}, headers=OPER)).status_code == 403


async def test_bad_proposed_spec_header_records_nothing(env):
    tc, _ = env
    r = await tc.put(PUT, json={"value": "hey"}, headers={**MAINT, "X-Proposed-Spec": "{oops"})
    assert r.status_code == 409 and r.json()["extra"]["status"] == "unproposed"


async def test_approve_with_edits_then_operator_can_write(env):
    tc, pub = env
    assert (await tc.post("/logbook/setting-keys/voice.wake_word/propose",
                          json={"spec": WAKE, "scope": "branch:7.0.1.1"},
                          headers=MAINT)).status_code == 201
    assert (await tc.post("/logbook/setting-keys/voice.wake_word/approve", json={},
                          headers=MAINT)).status_code == 403
    assert (await tc.post("/logbook/setting-keys/voice.wake_word/approve",
                          json={"spec": {"type": "int"}}, headers=ADMIN)).status_code == 422
    r = await tc.post("/logbook/setting-keys/voice.wake_word/approve",
                      json={"spec": {"operator_writable": True}, "note": "ok"}, headers=ADMIN)
    assert r.status_code == 201
    rec = r.json()
    assert rec["status"] == "approved" and rec["reviewed_by"] == "ada"
    assert rec["spec"]["operator_writable"] is True and rec["spec"]["type"] == "str"
    assert ("lightfall.settings.keys",
            {"key": "voice.wake_word", "status": "approved"}) in pub.events
    assert (await tc.put(PUT, json={"value": "hey"}, headers=OPER)).status_code == 200
    # approved spec doesn't allow beamline scope -> plain 409, not key_pending
    r = await tc.put("/logbook/scopes/beamline:7.0.1/settings/voice.wake_word",
                     json={"value": "x"}, headers=MAINT)
    assert r.status_code == 409 and "extra" not in r.json()


async def test_reject_reopen_and_revoke(env):
    tc, _ = env
    await tc.post("/logbook/setting-keys/voice.wake_word/propose",
                  json={"spec": WAKE, "scope": "branch:7.0.1.1"}, headers=MAINT)
    r = await tc.post("/logbook/setting-keys/voice.wake_word/reject", json={"note": "no"},
                      headers=ADMIN)
    assert r.status_code == 201 and r.json()["status"] == "rejected"
    r = await tc.put(PUT, json={"value": "hey"}, headers=MAINT)
    assert r.json()["extra"]["status"] == "rejected"
    r = await tc.post("/logbook/setting-keys/voice.wake_word/propose",
                      json={"spec": WAKE, "scope": "branch:7.0.1.1"}, headers=MAINT)
    assert r.json()["status"] == "pending"
    await tc.post("/logbook/setting-keys/voice.wake_word/approve", json={}, headers=ADMIN)
    assert (await tc.put(PUT, json={"value": "hey"}, headers=MAINT)).status_code == 200
    assert (await tc.delete("/logbook/setting-keys/voice.wake_word",
                            headers=STAFF)).status_code == 403
    assert (await tc.delete("/logbook/setting-keys/voice.wake_word",
                            headers=ADMIN)).status_code == 204
    r = await tc.put(PUT, json={"value": "again"}, headers={**MAINT, "If-Match": "1"})
    assert r.status_code == 409 and r.json()["extra"]["status"] == "unproposed"
    snap = (await tc.get("/logbook/scopes/branch:7.0.1.1/settings", headers=ALICE)).json()
    assert snap["settings"]["voice.wake_word"] == "hey"  # stored values stay


async def test_propose_rules(env):
    tc, _ = env
    assert (await tc.post("/logbook/setting-keys/plugins.enabled/propose",
                          json={"spec": {"key": "plugins.enabled"}, "scope": "branch:7.0.1.1"},
                          headers=MAINT)).status_code == 409
    assert (await tc.post("/logbook/setting-keys/x.y/propose",
                          json={"spec": {"key": "x.y", "type": "complex"},
                                "scope": "branch:7.0.1.1"},
                          headers=MAINT)).status_code == 422
    assert (await tc.post("/logbook/setting-keys/x.y/propose",
                          json={"spec": {"key": "x.y"}, "scope": "beamline:8.3.2"},
                          headers=MAINT)).status_code == 403
    assert (await tc.post("/logbook/setting-keys/x.y/propose",
                          json={"spec": {"key": "x.y"}, "scope": "user:mia"},
                          headers=MAINT)).status_code == 403
    assert (await tc.post("/logbook/setting-keys/tiled.url/propose",
                          json={"spec": {"key": "tiled.url"}, "scope": "branch:7.0.1.1"},
                          headers=MAINT)).status_code == 409  # seeded = approved
    assert (await tc.post("/logbook/setting-keys/none/approve", json={},
                          headers=ADMIN)).status_code == 404


async def test_seeded_key_is_listed_as_approved(env):
    tc, _ = env
    rows = (await tc.get("/logbook/setting-keys", params={"status": "approved"},
                         headers=MAINT)).json()
    assert [(r["key"], r["reviewed_by"]) for r in rows] == [("tiled.url", "seed")]
