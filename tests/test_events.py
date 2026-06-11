from __future__ import annotations

import pytest

from lightfall_logbook.events import LogbookEventPublisher, subject_for_user


def test_subject_is_deterministic_and_subject_safe():
    s1 = subject_for_user("alice")
    s2 = subject_for_user("alice")
    assert s1 == s2
    assert s1 == "_lightfall.logbook.changed." + b"alice".hex()
    token = s1.rsplit(".", 1)[1]
    for bad in (" ", "*", ">"):
        assert bad not in token


def test_subject_handles_keycloak_sub_with_dashes():
    s = subject_for_user("a1b2-c3d4")
    assert "." not in s.rsplit(".", 1)[1]


@pytest.mark.asyncio
async def test_publish_change_is_noop_without_connection():
    pub = LogbookEventPublisher(nats_url=None)
    await pub.publish_change(user_id="alice", op="create", kind="entry", entity_id="e1")


class _FakeNC:
    def __init__(self):
        self.published: list[tuple[str, bytes]] = []
    async def publish(self, subject, payload):
        self.published.append((subject, payload))
    async def drain(self):
        pass


@pytest.mark.asyncio
async def test_publish_change_sends_subject_and_payload():
    import json
    pub = LogbookEventPublisher(nats_url="nats://x")
    fake = _FakeNC()
    pub._nc = fake  # simulate a live connection

    await pub.publish_change(user_id="alice", op="delete", kind="entry", entity_id="e9")

    assert len(fake.published) == 1
    subject, payload = fake.published[0]
    assert subject == subject_for_user("alice")
    body = json.loads(payload.decode())
    assert body == {"user_id": "alice", "op": "delete", "kind": "entry", "id": "e9", "origin": None}
