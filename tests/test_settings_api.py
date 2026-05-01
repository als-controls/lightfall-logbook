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
async def test_get_single_key_scopes_to_user(client):
    """alice's individual setting is not visible to bob via the {key} endpoint."""
    await client.put(
        "/logbook/settings/theme",
        json={"value": "dark"},
        headers=ALICE,
    )
    resp = await client.get("/logbook/settings/theme", headers=BOB)
    assert resp.status_code == 404


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_put_creates_then_updates(client):
    """Second PUT for the same (user, beamline, key) updates rather than inserts."""
    r1 = await client.put(
        "/logbook/settings/theme",
        json={"value": "dark"},
        headers=ALICE,
    )
    assert r1.status_code == 200
    assert r1.json()["value"] == "dark"

    r2 = await client.put(
        "/logbook/settings/theme",
        json={"value": "light"},
        headers=ALICE,
    )
    assert r2.status_code == 200
    assert r2.json()["value"] == "light"

    # And read confirms only one row exists
    r3 = await client.get("/logbook/settings", headers=ALICE)
    assert r3.json() == {"theme": "light"}


@pytest.mark.asyncio
async def test_put_arbitrary_json_value(client):
    body = {"value": {"nested": [1, 2, {"k": "v"}]}}
    r = await client.put("/logbook/settings/blob", json=body, headers=ALICE)
    assert r.status_code == 200
    assert r.json()["value"] == body["value"]


@pytest.mark.asyncio
async def test_put_does_not_leak_across_users(client):
    await client.put(
        "/logbook/settings/theme", json={"value": "alice-dark"}, headers=ALICE
    )
    await client.put(
        "/logbook/settings/theme", json={"value": "bob-light"}, headers=BOB
    )

    a = await client.get("/logbook/settings/theme", headers=ALICE)
    b = await client.get("/logbook/settings/theme", headers=BOB)
    assert a.json()["value"] == "alice-dark"
    assert b.json()["value"] == "bob-light"
