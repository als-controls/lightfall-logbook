from __future__ import annotations
import pytest
from litestar.testing import AsyncTestClient
from lightfall_logbook.app import create_app


class _RecordingPublisher:
    def __init__(self):
        self.calls = []
        self.connected = False
        self.closed = False
    async def connect(self):
        self.connected = True
    async def close(self):
        self.closed = True
    async def publish_change(self, **kwargs):
        self.calls.append(kwargs)


@pytest.mark.asyncio
async def test_app_uses_injected_publisher_and_connects(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/t.db")
    monkeypatch.setenv("IMAGE_STORAGE_DIR", str(tmp_path / "img"))
    pub = _RecordingPublisher()
    app = create_app(event_publisher=pub)
    async with AsyncTestClient(app=app) as _tc:
        assert app.state.logbook_events is pub
        assert pub.connected is True
    assert pub.closed is True
