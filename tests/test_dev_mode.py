from pathlib import Path

import pytest
from litestar.testing import AsyncTestClient

from lightfall_logbook.app import create_app


@pytest.fixture
async def no_auth_client(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'd.db'}")
    monkeypatch.setenv("IMAGE_STORAGE_DIR", str(tmp_path / "images"))
    monkeypatch.delenv("KEYCLOAK_URL", raising=False)
    monkeypatch.delenv("KEYCLOAK_REALM", raising=False)
    monkeypatch.delenv("LOGBOOK_DEV_MODE", raising=False)
    async with AsyncTestClient(app=create_app()) as tc:
        yield tc


async def test_no_auth_configured_refuses_header_identity(no_auth_client):
    r = await no_auth_client.get("/api/v1/auth/whoami", headers={"X-User-Id": "admin"})
    assert r.status_code == 401
    r = await no_auth_client.get("/logbook/settings", headers={"X-User-Id": "admin"})
    assert r.status_code == 401
    assert (await no_auth_client.get("/health")).status_code == 200


async def test_no_auth_configured_refuses_mint(no_auth_client):
    r = await no_auth_client.post("/api/v1/auth/apikey", json={"expires_in": 60},
                                  headers={"X-User-Id": "admin"})
    assert r.status_code == 401


@pytest.mark.parametrize("value", ["1", "true", "YES"])
def test_dev_mode_flag_values(monkeypatch, value):
    from lightfall_logbook.auth import dev_mode_enabled
    monkeypatch.delenv("KEYCLOAK_URL", raising=False)
    monkeypatch.delenv("KEYCLOAK_REALM", raising=False)
    monkeypatch.setenv("LOGBOOK_DEV_MODE", value)
    assert dev_mode_enabled()


def test_keycloak_overrides_dev_flag(monkeypatch):
    from lightfall_logbook.auth import dev_mode_enabled
    monkeypatch.setenv("KEYCLOAK_URL", "https://kc.example")
    monkeypatch.setenv("KEYCLOAK_REALM", "als")
    monkeypatch.setenv("LOGBOOK_DEV_MODE", "1")
    assert not dev_mode_enabled()


async def test_dev_key_refused_after_dev_mode_turned_off(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'k.db'}")
    monkeypatch.setenv("IMAGE_STORAGE_DIR", str(tmp_path / "images"))
    monkeypatch.delenv("KEYCLOAK_URL", raising=False)
    monkeypatch.delenv("KEYCLOAK_REALM", raising=False)
    async with AsyncTestClient(app=create_app()) as tc:
        secret = (await tc.post("/api/v1/auth/apikey", json={"expires_in": 60},
                                headers={"X-User-Id": "admin"})).json()["secret"]
        monkeypatch.delenv("LOGBOOK_DEV_MODE")
        r = await tc.get("/api/v1/auth/whoami", headers={"Authorization": f"Apikey {secret}"})
        assert r.status_code == 401
