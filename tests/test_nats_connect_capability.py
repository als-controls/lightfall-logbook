"""Connect passes the per-connection capability (token + inbox_prefix) to nats.connect."""

import asyncio

import nats

from lightfall_logbook.nats_capability import derive_prefix


def _capture(monkeypatch):
    calls = []

    async def fake_connect(*args, **kwargs):
        calls.append(kwargs)
        raise RuntimeError("stop after connect")

    monkeypatch.setattr(nats, "connect", fake_connect)
    return calls


def _check(kw):
    assert kw["inbox_prefix"] == "_INBOX." + derive_prefix(kw["token"])


def test_connect_passes_capability(monkeypatch):
    from lightfall_logbook.events import LogbookEventPublisher

    calls = _capture(monkeypatch)
    pub = LogbookEventPublisher("nats://x:4222")
    asyncio.run(pub.connect())
    asyncio.run(pub.connect())
    assert len(calls) == 2
    for kw in calls:
        _check(kw)
    assert calls[0]["token"] == calls[1]["token"]


def test_connect_keeps_tls(monkeypatch):
    from lightfall_logbook.events import LogbookEventPublisher

    calls = _capture(monkeypatch)
    asyncio.run(LogbookEventPublisher("tls://x:4222").connect())
    assert "tls" in calls[0]
    _check(calls[0])
