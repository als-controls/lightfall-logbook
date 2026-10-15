"""Identity resolution: X-User-Id is a dev-only fallback."""
from __future__ import annotations

from pathlib import Path

import pytest
from litestar.testing import AsyncTestClient

from lightfall_logbook.app import create_app


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"


@pytest.fixture
async def kc_client(db_url: str, tmp_path: Path, monkeypatch):
    """App with Keycloak 'enabled' and decode_token faked from a dict."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("IMAGE_STORAGE_DIR", str(tmp_path / "images"))
    monkeypatch.setenv("KEYCLOAK_URL", "https://kc.example")
    monkeypatch.setenv("KEYCLOAK_REALM", "als")
    tokens: dict[str, dict] = {}

    def fake_decode(token: str) -> dict:
        if token not in tokens:
            raise ValueError("unknown token")
        return tokens[token]

    monkeypatch.setattr("lightfall_logbook.auth.decode_token", fake_decode)
    app = create_app()
    async with AsyncTestClient(app=app) as tc:
        yield tc, tokens


@pytest.fixture
async def dev_client(db_url: str, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("IMAGE_STORAGE_DIR", str(tmp_path / "images"))
    monkeypatch.delenv("KEYCLOAK_URL", raising=False)
    monkeypatch.delenv("KEYCLOAK_REALM", raising=False)
    app = create_app()
    async with AsyncTestClient(app=app) as tc:
        yield tc


async def test_bearer_without_sub_is_rejected_even_with_x_user_id(kc_client):
    tc, tokens = kc_client
    tokens["nosub"] = {"groups": []}
    r = await tc.get(
        "/api/v1/auth/whoami",
        headers={"Authorization": "Bearer nosub", "X-User-Id": "admin"},
    )
    assert r.status_code == 401


async def test_x_user_id_ignored_when_bearer_present(kc_client):
    tc, tokens = kc_client
    tokens["alice"] = {"sub": "alice"}
    r = await tc.get(
        "/api/v1/auth/whoami",
        headers={"Authorization": "Bearer alice", "X-User-Id": "admin"},
    )
    assert r.status_code == 200
    assert r.json()["sub"] == "alice"


async def test_x_user_id_alone_rejected_under_keycloak(kc_client):
    tc, _ = kc_client
    r = await tc.get("/api/v1/auth/whoami", headers={"X-User-Id": "admin"})
    assert r.status_code == 401


async def test_x_user_id_still_works_in_dev_mode(dev_client):
    r = await dev_client.get("/api/v1/auth/whoami", headers={"X-User-Id": "alice"})
    assert r.status_code == 200
    assert r.json()["sub"] == "alice"


async def test_dev_mode_default_identity(dev_client):
    r = await dev_client.get("/api/v1/auth/whoami")
    assert r.status_code == 200
    assert r.json()["sub"] == "dev-user"
