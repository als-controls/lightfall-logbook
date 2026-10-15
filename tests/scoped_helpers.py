"""Shared fixtures for scoped-settings API tests (dev-mode identity headers)."""
from __future__ import annotations

from contextlib import asynccontextmanager

from litestar.testing import AsyncTestClient

from lightfall_logbook.app import create_app
from lightfall_logbook.scoped_config import parse_scoped_config

CONFIG = """
[facility]
label = "ALS"

[[beamline]]
name = "7.0.1"
branches = ["7.0.1.1"]

[[beamline]]
name = "8.3.2"

[seed."tiled.url"]
type = "str"
scopes = ["facility", "beamline", "branch", "user"]
"""


class RecordingPublisher:
    def __init__(self):
        self.calls = []
        self.events = []

    async def connect(self):
        pass

    async def close(self):
        pass

    async def publish_change(self, **kwargs):
        self.calls.append(kwargs)

    async def publish(self, subject, payload):
        self.events.append((subject, payload))


def as_(user: str, *groups: str) -> dict[str, str]:
    headers = {"X-User-Id": user}
    if groups:
        headers["X-User-Groups"] = ",".join(groups)
    return headers


MAINT = as_("mia", "/beamlines/7.0.1/maintainer")
ADMIN = as_("ada", "/beamlines/_all/admin")
OPER = as_("otto", "/beamlines/7.0.1/operator")
STAFF = as_("sam", "/BCG_staff")
ALICE = as_("alice")


@asynccontextmanager
async def make_client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'scoped.db'}")
    monkeypatch.setenv("IMAGE_STORAGE_DIR", str(tmp_path / "images"))
    monkeypatch.delenv("KEYCLOAK_URL", raising=False)
    monkeypatch.delenv("KEYCLOAK_REALM", raising=False)
    pub = RecordingPublisher()
    app = create_app(event_publisher=pub, scoped_config=parse_scoped_config(CONFIG))
    async with AsyncTestClient(app=app) as tc:
        yield tc, pub
