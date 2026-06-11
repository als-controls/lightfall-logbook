from __future__ import annotations
import pytest
from lightfall_logbook.events import subject_for_user, LogbookEventPublisher


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
