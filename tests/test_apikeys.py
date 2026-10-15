"""Tests for /api/v1/auth/apikey mint + revoke and Apikey-scheme auth."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from litestar.testing import AsyncTestClient
from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from lightfall_logbook.apikeys import (
    ApiKeyRow,
    _hash_secret,
    lookup_key_by_secret,
    lookup_user_by_secret,
    mint_key,
    revoke_key,
)
from lightfall_logbook.app import create_app
from lightfall_logbook.models import Base

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"


@pytest.fixture
async def client(db_url: str, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("IMAGE_STORAGE_DIR", str(tmp_path / "images"))
    # Ensure no Keycloak env is set, so we're in dev mode.
    monkeypatch.delenv("KEYCLOAK_URL", raising=False)
    monkeypatch.delenv("KEYCLOAK_REALM", raising=False)
    app = create_app()
    async with AsyncTestClient(app=app) as tc:
        yield tc


@pytest.fixture
async def session_factory(db_url: str):
    engine = create_async_engine(db_url, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


ALICE = {"X-User-Id": "alice"}
BOB = {"X-User-Id": "bob"}


# ---------------------------------------------------------------------------
# Task 1: helpers exercised directly
# ---------------------------------------------------------------------------


async def test_mint_key_persists_hash_not_cleartext(session_factory):
    async with session_factory() as session:
        secret, row = await mint_key(
            session,
            sub="alice",
            expires_in_seconds=3600,
            scopes=[],
            note="t",
        )
        await session.commit()

    assert len(secret) == 64
    assert all(c in "0123456789abcdef" for c in secret)
    assert row.first_eight == secret[:8]
    assert row.secret_hash == _hash_secret(secret)
    assert row.secret_hash != secret  # the cleartext is not stored

    # The cleartext does not appear anywhere in the row.
    async with session_factory() as session:
        result = await session.execute(select(ApiKeyRow))
        persisted = result.scalar_one()
        assert secret not in persisted.secret_hash


async def test_lookup_user_by_secret_returns_sub(session_factory):
    async with session_factory() as session:
        secret, _ = await mint_key(
            session, sub="alice", expires_in_seconds=3600, scopes=[], note=""
        )
        await session.commit()

        sub = await lookup_user_by_secret(session, secret)
        assert sub == "alice"


async def test_lookup_user_by_secret_unknown_returns_none(session_factory):
    async with session_factory() as session:
        assert await lookup_user_by_secret(session, "0" * 64) is None


async def test_lookup_user_by_secret_revoked_returns_none(session_factory):
    async with session_factory() as session:
        secret, row = await mint_key(
            session, sub="alice", expires_in_seconds=3600, scopes=[], note=""
        )
        row.revoked = True
        await session.commit()
        assert await lookup_user_by_secret(session, secret) is None


async def test_lookup_user_by_secret_expired_returns_none(session_factory):
    async with session_factory() as session:
        secret, row = await mint_key(
            session, sub="alice", expires_in_seconds=3600, scopes=[], note=""
        )
        # Force the row's expiration into the past.
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
        assert await lookup_user_by_secret(session, secret) is None


async def test_revoke_key_marks_row_revoked(session_factory):
    async with session_factory() as session:
        secret, row = await mint_key(
            session, sub="alice", expires_in_seconds=3600, scopes=[], note=""
        )
        await session.commit()
        first_eight = row.first_eight

        ok = await revoke_key(session, sub="alice", first_eight=first_eight)
        assert ok is True
        await session.commit()

        # Row still exists (audit), but revoked=True.
        result = await session.execute(
            select(ApiKeyRow).where(ApiKeyRow.first_eight == first_eight)
        )
        persisted = result.scalar_one()
        assert persisted.revoked is True

        # Lookup now misses.
        assert await lookup_user_by_secret(session, secret) is None


async def test_revoke_key_wrong_owner_returns_false(session_factory):
    async with session_factory() as session:
        _, row = await mint_key(
            session, sub="alice", expires_in_seconds=3600, scopes=[], note=""
        )
        await session.commit()

        ok = await revoke_key(session, sub="bob", first_eight=row.first_eight)
        assert ok is False


# ---------------------------------------------------------------------------
# Task 3 + 4: HTTP round-trip
# ---------------------------------------------------------------------------


async def test_mint_endpoint_round_trip(client):
    """Mint via X-User-Id (dev mode) returns a well-shaped response."""
    resp = await client.post(
        "/api/v1/auth/apikey",
        json={"expires_in": 3600, "scopes": [], "note": "smoke"},
        headers=ALICE,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert len(body["secret"]) == 64
    assert body["first_eight"] == body["secret"][:8]
    assert body["note"] == "smoke"
    assert body["scopes"] == []
    # expiration_time is ISO 8601 and parseable as a tz-aware datetime.
    parsed = datetime.fromisoformat(body["expiration_time"])
    assert parsed.tzinfo is not None


async def test_mint_then_use_apikey_on_logbook(client):
    """The Apikey secret authenticates as the minting user on other routes."""
    mint = await client.post(
        "/api/v1/auth/apikey",
        json={"expires_in": 3600, "scopes": [], "note": ""},
        headers=ALICE,
    )
    assert mint.status_code == 201
    secret = mint.json()["secret"]

    # Use the key (no X-User-Id) -- it should resolve to alice and 200.
    resp = await client.get(
        "/logbook/",
        headers={"Authorization": f"Apikey {secret}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["user_id"] == "alice"


async def test_revoke_endpoint_revokes_own_key(client):
    """Revoke succeeds and subsequent Apikey use returns 401."""
    mint = await client.post(
        "/api/v1/auth/apikey",
        json={"expires_in": 3600, "scopes": [], "note": ""},
        headers=ALICE,
    )
    body = mint.json()
    secret = body["secret"]
    first_eight = body["first_eight"]

    rev = await client.delete(
        f"/api/v1/auth/apikey?first_eight={first_eight}",
        headers=ALICE,
    )
    assert rev.status_code == 204, rev.text

    use = await client.get(
        "/logbook/",
        headers={"Authorization": f"Apikey {secret}"},
    )
    assert use.status_code == 401


async def test_revoke_by_other_user_returns_404(client):
    """Cross-user revoke returns 404 (preserves owner isolation)."""
    mint = await client.post(
        "/api/v1/auth/apikey",
        json={"expires_in": 3600, "scopes": [], "note": ""},
        headers=ALICE,
    )
    first_eight = mint.json()["first_eight"]

    rev = await client.delete(
        f"/api/v1/auth/apikey?first_eight={first_eight}",
        headers=BOB,
    )
    assert rev.status_code == 404


async def test_expired_apikey_returns_401(client, db_url):
    """An expired key returns 401 on use, not silent acceptance."""
    mint = await client.post(
        "/api/v1/auth/apikey",
        json={"expires_in": 3600, "scopes": [], "note": ""},
        headers=ALICE,
    )
    body = mint.json()
    secret = body["secret"]
    first_eight = body["first_eight"]

    # Forcibly expire the row by editing the DB directly.
    engine = create_async_engine(db_url, echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        result = await session.execute(
            select(ApiKeyRow).where(ApiKeyRow.first_eight == first_eight)
        )
        row = result.scalar_one()
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    await engine.dispose()

    resp = await client.get(
        "/logbook/",
        headers={"Authorization": f"Apikey {secret}"},
    )
    assert resp.status_code == 401


async def test_invalid_apikey_returns_401(client):
    """A garbage Apikey value returns 401, not 500."""
    resp = await client.get(
        "/logbook/",
        headers={"Authorization": "Apikey deadbeef" * 8},
    )
    assert resp.status_code == 401


async def test_mint_rejects_out_of_range_expires_in(client):
    # Zero / negative.
    r1 = await client.post(
        "/api/v1/auth/apikey",
        json={"expires_in": 0, "scopes": [], "note": ""},
        headers=ALICE,
    )
    assert r1.status_code in (400, 422)

    # Beyond one week + 60s slack.
    r2 = await client.post(
        "/api/v1/auth/apikey",
        json={"expires_in": 7 * 86400 + 61, "scopes": [], "note": ""},
        headers=ALICE,
    )
    assert r2.status_code in (400, 422)


async def test_mint_one_week_slack_accepted(client):
    """A request for exactly 604800 (one week, no slack) is accepted."""
    r = await client.post(
        "/api/v1/auth/apikey",
        json={"expires_in": 7 * 86400, "scopes": [], "note": ""},
        headers=ALICE,
    )
    assert r.status_code == 201


async def test_dev_mode_mint_via_x_user_id(client):
    """Per plan Task 4: in dev mode (no Keycloak), mint succeeds via
    X-User-Id alone. The Bearer-only restriction applies only when
    Keycloak is configured.
    """
    resp = await client.post(
        "/api/v1/auth/apikey",
        json={"expires_in": 3600, "scopes": [], "note": ""},
        headers=ALICE,
    )
    assert resp.status_code == 201


async def test_health_endpoint_unauthenticated(client):
    """The /health exclusion still works after the middleware switch."""
    resp = await client.get("/health")
    assert resp.status_code == 200


async def test_duplicate_first_eight_per_user_rejected(session_factory):
    """Composite UNIQUE(sub, first_eight) prevents same-user collision."""
    from sqlalchemy.exc import IntegrityError

    from lightfall_logbook.apikeys import ApiKeyRow

    async with session_factory() as session:
        row1 = ApiKeyRow(
            secret_hash="hash1",
            first_eight="abcdef12",
            sub="alice",
            expires_at=datetime.now(UTC) + timedelta(days=1),
            scopes=[],
            note="",
            revoked=False,
            created_at=datetime.now(UTC),
        )
        row2 = ApiKeyRow(
            secret_hash="hash2",
            first_eight="abcdef12",  # same first_eight, same user -- collision
            sub="alice",
            expires_at=datetime.now(UTC) + timedelta(days=1),
            scopes=[],
            note="",
            revoked=False,
            created_at=datetime.now(UTC),
        )
        session.add(row1)
        await session.commit()
        session.add(row2)
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_lookup_key_by_secret_returns_groups(session_factory):
    async with session_factory() as session:
        secret, _ = await mint_key(
            session, sub="alice", expires_in_seconds=3600, scopes=[], note="",
            groups=["/beamlines/7.0.1/viewer"],
        )
        await session.commit()
    async with session_factory() as session:
        ident = await lookup_key_by_secret(session, secret)
    assert ident is not None
    assert ident.sub == "alice"
    assert ident.groups == ["/beamlines/7.0.1/viewer"]


_OLD_API_KEYS_DDL = (
    "CREATE TABLE api_keys ("
    " id INTEGER PRIMARY KEY, secret_hash VARCHAR(128) NOT NULL UNIQUE,"
    " first_eight VARCHAR(16) NOT NULL, sub VARCHAR(256) NOT NULL,"
    " expires_at DATETIME NOT NULL, scopes JSON NOT NULL, note VARCHAR(512) NOT NULL,"
    " revoked BOOLEAN NOT NULL, created_at DATETIME NOT NULL)"
)


async def test_startup_migrates_old_api_keys_table(db_url, tmp_path, monkeypatch):
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.execute(text(_OLD_API_KEYS_DDL))
        await conn.execute(text(
            "INSERT INTO api_keys (secret_hash, first_eight, sub, expires_at, scopes,"
            " note, revoked, created_at) VALUES (:h, 'abcdabcd', 'olduser',"
            " '2999-01-01 00:00:00', '[]', '', 0, '2026-01-01 00:00:00')"
        ), {"h": _hash_secret("a" * 64)})
    await engine.dispose()

    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("IMAGE_STORAGE_DIR", str(tmp_path / "images"))
    monkeypatch.delenv("KEYCLOAK_URL", raising=False)
    monkeypatch.delenv("KEYCLOAK_REALM", raising=False)
    async with AsyncTestClient(app=create_app()) as tc:
        w = await tc.get("/api/v1/auth/whoami", headers={"Authorization": "Apikey " + "a" * 64})
        assert w.status_code == 200
        assert w.json() == {"sub": "olduser", "groups": [], "auth_mode": "apikey"}

    engine = create_async_engine(db_url)
    async with engine.connect() as conn:
        cols = await conn.run_sync(
            lambda c: {col["name"] for col in inspect(c).get_columns("api_keys")}
        )
    await engine.dispose()
    assert {"kc_groups", "mint_mode"} <= cols


def test_groups_migration_tolerates_concurrent_worker(tmp_path, monkeypatch):
    """Two workers both see the column missing; the loser must not crash."""
    import sqlalchemy

    from lightfall_logbook.apikeys import ensure_api_key_groups_column

    engine = sqlalchemy.create_engine(f"sqlite:///{tmp_path / 'race.db'}")
    with engine.begin() as conn:
        conn.execute(text(_OLD_API_KEYS_DDL))
        ensure_api_key_groups_column(conn)  # first worker adds the columns

    class StaleInspector:
        """What the second worker saw before the first worker's ALTER landed."""

        def __init__(self, conn):
            pass

        def get_table_names(self):
            return ["api_keys"]

        def get_columns(self, table):
            return [{"name": "id"}]

    monkeypatch.setattr(sqlalchemy, "inspect", StaleInspector)
    with engine.begin() as conn:
        ensure_api_key_groups_column(conn)  # must not raise "duplicate column"
    engine.dispose()


def test_groups_migration_uses_if_not_exists_on_postgres():
    from lightfall_logbook.apikeys import ensure_api_key_groups_column

    class FakeConn:
        class dialect:  # noqa: N801
            name = "postgresql"

        def __init__(self):
            self.sql = []

        def execute(self, stmt):
            self.sql.append(str(stmt))

    conn = FakeConn()
    ensure_api_key_groups_column(conn)
    assert conn.sql
    assert all("ADD COLUMN IF NOT EXISTS" in s for s in conn.sql)
