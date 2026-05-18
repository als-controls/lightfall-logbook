"""User-scoped API key minting, lookup, revocation, and HTTP controller.

The wire shape mirrors the Tiled apikey contract:

- Mint:    POST   /api/v1/auth/apikey  (requires Bearer in prod)
- Revoke:  DELETE /api/v1/auth/apikey?first_eight=<8 hex>

Subsequent requests authenticate with ``Authorization: Apikey <secret>``.
The secret is a 64-char hex string (32 random bytes). Only its SHA-256 hash
is persisted; ``first_eight`` is a non-secret handle used for revocation and
display.

Mint requires a Keycloak Bearer in production. In dev mode (no Keycloak env
vars), the existing ``X-User-Id`` fallback in :func:`api._get_user_id` is
honored so local development without a Keycloak server still works.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from litestar import Controller, Request, delete, post
from litestar.exceptions import (
    NotAuthorizedException,
    NotFoundException,
    PermissionDeniedException,
    ValidationException,
)
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import JSON, Boolean, DateTime, Integer, String, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from lucid_logbook.auth import keycloak_auth_enabled
from lucid_logbook.models import Base

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# 7 days, plus 60s slack so clients can request exactly 604800 without
# tripping the upper-bound clamp on rounding/clock skew.
MAX_EXPIRES_IN_SECONDS = 7 * 86400 + 60
DEFAULT_EXPIRES_IN_SECONDS = 7 * 86400

# Secret cleartext is hex (token_hex(32) -> 64 chars). first_eight is the
# first 8 chars of the cleartext -- matches the LUCID-side MintedKey
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
    note: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
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


async def lookup_user_by_secret(
    session: AsyncSession, secret: str
) -> str | None:
    """Return the ``sub`` for a valid, un-revoked, un-expired key, else ``None``."""
    if not secret:
        return None
    digest = _hash_secret(secret)
    result = await session.execute(
        select(ApiKeyRow).where(ApiKeyRow.secret_hash == digest)
    )
    row = result.scalar_one_or_none()
    if row is None:
        return None
    if row.revoked:
        return None
    if _ensure_aware_utc(row.expires_at) <= datetime.now(UTC):
        return None
    return row.sub


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
    """Same identity logic as :func:`api._get_user_id`, kept inline to avoid
    a circular import between ``api`` and ``apikeys``.
    """
    user_id: str | None = getattr(
        getattr(request, "state", None), "user_id", None
    )
    if user_id:
        return user_id
    user_id = request.headers.get("X-User-Id")
    if user_id:
        return user_id
    if not keycloak_auth_enabled():
        return "dev-user"
    raise NotAuthorizedException("Missing user identity")


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
        # we allow the X-User-Id fallback so the local dev workflow keeps
        # working without a Keycloak server.
        auth_mode = getattr(
            getattr(request, "state", None), "auth_mode", None
        )
        if keycloak_auth_enabled() and auth_mode != "bearer":
            raise PermissionDeniedException(
                "Minting an apikey requires Bearer auth"
            )

        sub = _get_caller_sub(request)
        secret, row = await mint_key(
            db_session,
            sub=sub,
            expires_in_seconds=data.expires_in,
            scopes=data.scopes,
            note=data.note,
        )
        await db_session.commit()
        return ApiKeyMintResponse(
            secret=secret,
            first_eight=row.first_eight,
            expiration_time=_ensure_aware_utc(row.expires_at).isoformat(),
            scopes=list(row.scopes),
            note=row.note,
        )

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
            await db_session.commit()
            raise NotFoundException("No matching apikey")
        await db_session.commit()
