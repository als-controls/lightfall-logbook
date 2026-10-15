"""User-scoped API key minting, lookup, revocation, and HTTP controller.

The wire shape mirrors the Tiled apikey contract:

- Mint:    POST   /api/v1/auth/apikey  (requires Bearer in prod)
- Revoke:  DELETE /api/v1/auth/apikey?first_eight=<8 hex>

Subsequent requests authenticate with ``Authorization: Apikey <secret>``.
The secret is a 64-char hex string (32 random bytes). Only its SHA-256 hash
is persisted; ``first_eight`` is a non-secret handle used for revocation and
display.

Mint requires a Keycloak Bearer in production. In dev mode (no Keycloak env
vars), the dev-only ``X-User-Id`` fallback in
:func:`lightfall_logbook.auth.request_identity` is
honored so local development without a Keycloak server still works.
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from litestar import Controller, Request, delete, get, post
from litestar.exceptions import (
    NotFoundException,
    PermissionDeniedException,
    ValidationException,
)
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Integer,
    String,
    UniqueConstraint,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from lightfall_logbook.auth import (
    dev_mode_enabled,
    keycloak_auth_enabled,
    request_groups,
    request_identity,
)
from lightfall_logbook.models import Base

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# 7 days, plus 60s slack so clients can request exactly 604800 without
# tripping the upper-bound clamp on rounding/clock skew.
MAX_EXPIRES_IN_SECONDS = 7 * 86400 + 60
DEFAULT_EXPIRES_IN_SECONDS = 7 * 86400

# Secret cleartext is hex (token_hex(32) -> 64 chars). first_eight is the
# first 8 chars of the cleartext -- matches the Lightfall-side MintedKey
# semantics on feature/notebook-pipelines-impl.
_SECRET_BYTES = 32
_FIRST_EIGHT_LEN = 8


# ---------------------------------------------------------------------------
# ORM model
# ---------------------------------------------------------------------------


class ApiKeyRow(Base):
    """Persisted apikey row. Only the hash of the secret is stored."""

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    secret_hash: Mapped[str] = mapped_column(
        String(128), unique=True, index=True, nullable=False
    )
    first_eight: Mapped[str] = mapped_column(
        String(16), index=True, nullable=False
    )
    sub: Mapped[str] = mapped_column(String(256), index=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    scopes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    # Keycloak ``groups`` claim captured at mint time. Column is named
    # ``kc_groups`` because GROUPS is an SQL keyword (SQLite window frames).
    # Nullable: rows created before this column existed read as no groups.
    groups: Mapped[list[str] | None] = mapped_column(
        "kc_groups", JSON, nullable=True, default=list
    )
    # How the key was minted: "bearer" (Keycloak) or "dev" (X-User-Id fallback).
    # Dev-minted keys carry unverified identity/groups and are refused while
    # Keycloak auth is enabled. NULL = minted before this column existed.
    mint_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    note: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )

    __table_args__ = (
        UniqueConstraint("sub", "first_eight", name="uq_api_keys_sub_first_eight"),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hash_secret(secret: str) -> str:
    """Return the canonical SHA-256 hex digest of ``secret``."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _ensure_aware_utc(dt: datetime) -> datetime:
    """SQLite via aiosqlite returns naive datetimes; treat them as UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


async def mint_key(
    session: AsyncSession,
    *,
    sub: str,
    expires_in_seconds: int,
    scopes: list[str],
    note: str,
    groups: list[str] | None = None,
    mint_mode: str = "dev",
) -> tuple[str, ApiKeyRow]:
    """Mint a new key for ``sub``.

    Returns a tuple of ``(cleartext_secret, row)``. The cleartext secret is
    returned exactly once -- the database only stores the hash.
    """
    secret = secrets.token_hex(_SECRET_BYTES)
    row = ApiKeyRow(
        secret_hash=_hash_secret(secret),
        first_eight=secret[:_FIRST_EIGHT_LEN],
        sub=sub,
        expires_at=datetime.now(UTC) + timedelta(seconds=expires_in_seconds),
        scopes=list(scopes),
        groups=list(groups or []),
        mint_mode=mint_mode,
        note=note,
    )
    session.add(row)
    await session.flush()
    logger.info(
        "Minted apikey first_eight={} for sub={} expires_at={}",
        row.first_eight,
        sub,
        row.expires_at.isoformat(),
    )
    return secret, row


@dataclass(frozen=True)
class KeyIdentity:
    """Who an apikey belongs to, and the groups snapshotted at mint."""

    sub: str
    groups: list[str]


async def lookup_key_by_secret(
    session: AsyncSession, secret: str
) -> KeyIdentity | None:
    """Return identity for a valid, un-revoked, un-expired key, else ``None``."""
    if not secret:
        return None
    digest = _hash_secret(secret)
    result = await session.execute(
        select(ApiKeyRow).where(ApiKeyRow.secret_hash == digest)
    )
    row = result.scalar_one_or_none()
    if row is None or row.revoked:
        return None
    if _ensure_aware_utc(row.expires_at) <= datetime.now(UTC):
        return None
    if row.mint_mode == "dev" and not dev_mode_enabled():
        logger.warning(
            "Refusing dev-minted apikey first_eight={} sub={} outside dev mode",
            row.first_eight,
            row.sub,
        )
        return None
    return KeyIdentity(sub=row.sub, groups=list(row.groups or []))


async def lookup_user_by_secret(
    session: AsyncSession, secret: str
) -> str | None:
    """Return the ``sub`` for a valid key, else ``None``."""
    ident = await lookup_key_by_secret(session, secret)
    return ident.sub if ident else None


# Columns added to ``api_keys`` after it first shipped: name -> SQL type.
_ADDED_API_KEY_COLUMNS = {"kc_groups": "JSON", "mint_mode": "VARCHAR(16)"}


def ensure_api_key_groups_column(sync_conn: Any) -> None:
    """Add post-launch ``api_keys`` columns to databases created before them.

    ``Base.metadata.create_all`` creates missing tables but never adds
    columns to existing ones. Run via ``conn.run_sync`` after ``create_all``.
    Safe when several workers start at once: Postgres uses ``IF NOT EXISTS``;
    on SQLite a worker that loses the race gets "duplicate column" and moves on.
    """
    import sqlalchemy
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError

    if sync_conn.dialect.name == "postgresql":
        for name, sql_type in _ADDED_API_KEY_COLUMNS.items():
            sync_conn.execute(text(
                f"ALTER TABLE IF EXISTS api_keys ADD COLUMN IF NOT EXISTS {name} {sql_type}"
            ))
        return

    insp = sqlalchemy.inspect(sync_conn)
    if "api_keys" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("api_keys")}
    for name, sql_type in _ADDED_API_KEY_COLUMNS.items():
        if name in cols:
            continue
        try:
            sync_conn.execute(text(f"ALTER TABLE api_keys ADD COLUMN {name} {sql_type}"))
            logger.info("Added api_keys.{} column", name)
        except OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise


async def revoke_key(
    session: AsyncSession, *, sub: str, first_eight: str
) -> bool:
    """Revoke a key owned by ``sub`` matching ``first_eight``.

    Returns True if a matching un-revoked row was found and revoked, else
    False (e.g. unknown handle, wrong owner, or already revoked). The
    cross-user case deliberately returns False so callers surface a 404
    rather than leaking the existence of another user's key.
    """
    result = await session.execute(
        select(ApiKeyRow).where(
            ApiKeyRow.first_eight == first_eight,
            ApiKeyRow.sub == sub,
            ApiKeyRow.revoked.is_(False),
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        return False
    row.revoked = True
    await session.flush()
    logger.info("Revoked apikey first_eight={} sub={}", first_eight, sub)
    return True


# ---------------------------------------------------------------------------
# Pydantic wire models
# ---------------------------------------------------------------------------


class ApiKeyMintRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    expires_in: int = DEFAULT_EXPIRES_IN_SECONDS
    scopes: list[str] = Field(default_factory=list)
    note: str = ""


class ApiKeyMintResponse(BaseModel):
    secret: str
    first_eight: str
    expiration_time: str
    scopes: list[str]
    note: str


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------


def _get_caller_sub(request: Request) -> str:
    """Caller identity; see :func:`lightfall_logbook.auth.request_identity`."""
    return request_identity(request)


class AuthController(Controller):
    """Mint/revoke user-scoped API keys."""

    path = "/api/v1/auth"

    @post("/apikey", status_code=201)
    async def mint(
        self,
        data: ApiKeyMintRequest,
        request: Request,
        db_session: AsyncSession,
    ) -> ApiKeyMintResponse:
        if not (0 < data.expires_in <= MAX_EXPIRES_IN_SECONDS):
            raise ValidationException(
                f"expires_in must be in (0, {MAX_EXPIRES_IN_SECONDS}]"
            )

        # Mint requires Bearer in prod. In dev mode (no Keycloak configured)
        # we allow the dev-only X-User-Id fallback (auth.request_identity) so the
        # local dev workflow keeps working without a Keycloak server.
        auth_mode = getattr(
            getattr(request, "state", None), "auth_mode", None
        )
        if keycloak_auth_enabled() and auth_mode != "bearer":
            raise PermissionDeniedException(
                "Minting an apikey requires Bearer auth"
            )

        sub = _get_caller_sub(request)
        claims = getattr(request.state, "user_claims", None) or {}
        if keycloak_auth_enabled() and "groups" not in claims:
            logger.warning(
                "Token for sub={} has no 'groups' claim; key gets no scope roles. "
                "Check the Keycloak groups mapper (full group path, in access token).",
                sub,
            )
        secret, row = await mint_key(
            db_session,
            sub=sub,
            expires_in_seconds=data.expires_in,
            scopes=data.scopes,
            note=data.note,
            groups=request_groups(request),
            mint_mode="bearer" if auth_mode == "bearer" else "dev",
        )
        await db_session.commit()
        return ApiKeyMintResponse(
            secret=secret,
            first_eight=row.first_eight,
            expiration_time=_ensure_aware_utc(row.expires_at).isoformat(),
            scopes=list(row.scopes),
            note=row.note,
        )

    @get("/whoami")
    async def whoami(self, request: Request) -> dict[str, Any]:
        """Echo the caller's identity as the server sees it (diagnostics)."""
        return {
            "sub": request_identity(request),
            "groups": request_groups(request),
            "auth_mode": getattr(request.state, "auth_mode", None) or "dev",
        }

    @delete("/apikey", status_code=204)
    async def revoke(
        self,
        request: Request,
        db_session: AsyncSession,
        first_eight: str,
    ) -> None:
        if len(first_eight) != _FIRST_EIGHT_LEN:
            raise ValidationException(
                f"first_eight must be {_FIRST_EIGHT_LEN} chars"
            )
        sub = _get_caller_sub(request)
        ok = await revoke_key(db_session, sub=sub, first_eight=first_eight)
        if not ok:
            raise NotFoundException("No matching apikey")
        await db_session.commit()
