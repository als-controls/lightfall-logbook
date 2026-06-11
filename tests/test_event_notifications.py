from __future__ import annotations
import pytest
from litestar.testing import AsyncTestClient
from lightfall_logbook.app import create_app

H = {"X-User-Id": "alice"}


class _RecordingPublisher:
    def __init__(self):
        self.calls = []
    async def connect(self):
        pass
    async def close(self):
        pass
    async def publish_change(self, **kwargs):
        self.calls.append(kwargs)


@pytest.fixture
async def client_and_pub(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/t.db")
    monkeypatch.setenv("IMAGE_STORAGE_DIR", str(tmp_path / "img"))
    pub = _RecordingPublisher()
    app = create_app(event_publisher=pub)
    async with AsyncTestClient(app=app) as tc:
        yield tc, pub


@pytest.mark.asyncio
async def test_entry_create_update_delete_notify(client_and_pub):
    tc, pub = client_and_pub
    created = await tc.post("/logbook/entries", json={"title": "x"}, headers=H)
    eid = created.json()["id"]
    await tc.put(f"/logbook/entries/{eid}", json={"title": "y"}, headers=H)
    await tc.delete(f"/logbook/entries/{eid}", headers=H)

    ops = [(c["op"], c["kind"], c["entity_id"]) for c in pub.calls]
    assert ("create", "entry", eid) in ops
    assert ("update", "entry", eid) in ops
    assert ("delete", "entry", eid) in ops
    assert all(c["user_id"] == "alice" for c in pub.calls)
