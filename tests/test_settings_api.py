"""Tests for /logbook/settings CRUD endpoints."""
from __future__ import annotations

from pathlib import Path

import pytest
from litestar.testing import AsyncTestClient

from lucid_logbook.app import create_app


@pytest.fixture
async def client(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("IMAGE_STORAGE_DIR", str(tmp_path / "images"))
    app = create_app()
    async with AsyncTestClient(app=app) as tc:
        yield tc


ALICE = {"X-User-Id": "alice"}
BOB = {"X-User-Id": "bob"}


@pytest.mark.asyncio
async def test_get_unknown_key_returns_404(client):
    resp = await client.get("/logbook/settings/missing", headers=ALICE)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_all_empty(client):
    resp = await client.get("/logbook/settings", headers=ALICE)
    assert resp.status_code == 200
    assert resp.json() == {}


@pytest.mark.asyncio
@pytest.mark.skip(reason="depends on PUT — added in Task 3")
async def test_get_all_scopes_to_user(client):
    """alice's settings are not visible to bob."""
    await client.put(
        "/logbook/settings/theme",
        json={"value": "dark"},
        headers=ALICE,
    )
    resp = await client.get("/logbook/settings", headers=BOB)
    assert resp.status_code == 200
    assert resp.json() == {}


@pytest.mark.asyncio
@pytest.mark.skip(reason="depends on PUT — added in Task 3")
async def test_get_all_with_beamline_filter(client):
    """Default scope is global (beamline=''); ?beamline=X returns only that scope."""
    await client.put("/logbook/settings/k", json={"value": "global"}, headers=ALICE)
    await client.put(
        "/logbook/settings/k",
        json={"value": "bl-specific", "beamline": "11.0.1"},
        headers=ALICE,
    )

    resp = await client.get("/logbook/settings", headers=ALICE)
    assert resp.json() == {"k": "global"}

    resp = await client.get(
        "/logbook/settings?beamline=11.0.1", headers=ALICE
    )
    assert resp.json() == {"k": "bl-specific"}
