"""Authentication middleware for Litestar.

Accepts two auth schemes:

- ``Authorization: Bearer <jwt>``  -- validated against Keycloak's JWKS endpoint
- ``Authorization: Apikey <hex>``  -- looked up in the ``api_keys`` table

In dev mode (no Keycloak env vars configured), unauthenticated requests are
passed through so the existing ``X-User-Id`` header fallback in
:func:`lightfall_logbook.api._get_user_id` keeps working.

The middleware always registers; the dev fallthrough is internal so we don't
need conditional middleware wiring in :mod:`lightfall_logbook.app`.
"""

from __future__ import annotations

import json
import os
from typing import Any

from litestar.middleware.base import AbstractMiddleware
from litestar.types import Receive, Scope, Send
from loguru import logger
from sqlalchemy.ext.asyncio import async_sessionmaker


def keycloak_auth_enabled() -> bool:
    """Check if Keycloak env vars are set."""
    return bool(
        os.environ.get("KEYCLOAK_URL")
        and os.environ.get("KEYCLOAK_REALM")
    )


def _get_keycloak_config() -> dict[str, str]:
    return {
        "url": os.environ["KEYCLOAK_URL"].rstrip("/"),
        "realm": os.environ["KEYCLOAK_REALM"],
        "client_id": os.environ.get("KEYCLOAK_CLIENT_ID", "lightfall-logbook"),
        "audience": os.environ.get("KEYCLOAK_AUDIENCE", ""),
    }


# Lazily loaded JWKS client
_jwks_client = None


def _get_jwks_client():
    global _jwks_client
    if _jwks_client is None:
        import jwt  # PyJWT

        config = _get_keycloak_config()
        jwks_url = f"{config['url']}/realms/{config['realm']}/protocol/openid-connect/certs"
        _jwks_client = jwt.PyJWKClient(jwks_url, cache_keys=True)
        logger.info("Keycloak JWKS client configured: {}", jwks_url)
    return _jwks_client


def decode_token(token: str) -> dict[str, Any]:
    """Decode and validate a Keycloak JWT token.

    Returns the decoded claims dict.

    Raises:
        jwt.InvalidTokenError: If the token is invalid or expired.
    """
    import jwt

    config = _get_keycloak_config()
    jwks_client = _get_jwks_client()
    signing_key = jwks_client.get_signing_key_from_jwt(token)

    decode_options: dict[str, Any] = {
        "algorithms": ["RS256"],
        "issuer": f"{config['url']}/realms/{config['realm']}",
    }
    if config["audience"]:
        decode_options["audience"] = config["audience"]
    else:
        decode_options["options"] = {"verify_aud": False}

    return jwt.decode(token, signing_key.key, **decode_options)


class CombinedAuthMiddleware(AbstractMiddleware):
    """Litestar middleware accepting both Bearer (Keycloak) and Apikey schemes.

    Always registers. Behavior per request:

    - Non-HTTP scope or excluded path: pass through.
    - ``Authorization: Apikey <secret>``: look up the key in ``api_keys``.
      Set ``state.user_id`` and ``state.auth_mode="apikey"`` on success;
      401 on miss/expired/revoked.
    - ``Authorization: Bearer <jwt>``: decode against Keycloak JWKS. Set
      ``state.user_id``, ``state.user_claims``, ``state.auth_mode="bearer"``.
      If Keycloak is not configured, 401 (don't silently accept).
    - No header: in prod (Keycloak configured) 401, in dev pass through so
      the X-User-Id fallback in the API layer still works.
    - Anything else: 401 (unsupported scheme).
    """

    exclude = ["/health"]

    def __init__(
        self,
        app: Any,
        session_factory: async_sessionmaker | None = None,
    ) -> None:
        super().__init__(app)
        # Resolved at first use to avoid an import cycle at module import time.
        self._session_factory = session_factory

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        auth_header = headers.get(b"authorization", b"").decode()

        if "state" not in scope:
            scope["state"] = {}

        if auth_header.startswith("Apikey "):
            secret = auth_header[len("Apikey "):].strip()
            sub = await self._lookup_apikey(secret)
            if not sub:
                await _send_401(send, "Invalid or expired apikey")
                return
            scope["state"]["user_id"] = sub
            scope["state"]["auth_mode"] = "apikey"
            await self.app(scope, receive, send)
            return

        if auth_header.startswith("Bearer "):
            if not keycloak_auth_enabled():
                await _send_401(send, "Bearer auth not configured")
                return
            token = auth_header[len("Bearer "):].strip()
            try:
                claims = decode_token(token)
            except Exception as e:
                logger.debug("JWT validation failed: {}", e)
                await _send_401(send, "Invalid token")
                return
            scope["state"]["user_id"] = claims.get("sub", "")
            scope["state"]["user_claims"] = claims
            scope["state"]["auth_mode"] = "bearer"
            await self.app(scope, receive, send)
            return

        if not auth_header:
            # Prod requires auth; dev mode falls through so the X-User-Id
            # header fallback in the API layer keeps working.
            if keycloak_auth_enabled():
                await _send_401(send, "Missing Authorization header")
                return
            await self.app(scope, receive, send)
            return

        await _send_401(send, "Unsupported Authorization scheme")

    async def _lookup_apikey(self, secret: str) -> str | None:
        if self._session_factory is None or not secret:
            return None
        # Import here to avoid circular imports at module load.
        from lightfall_logbook.apikeys import lookup_user_by_secret

        async with self._session_factory() as session:
            try:
                return await lookup_user_by_secret(session, secret)
            except Exception as e:
                logger.error("Apikey lookup failed: {}", e)
                return None


async def _send_401(send: Send, detail: str) -> None:
    """Send a 401 Unauthorized ASGI response."""
    body = json.dumps({"detail": detail}).encode()
    await send({
        "type": "http.response.start",
        "status": 401,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
    })
    await send({
        "type": "http.response.body",
        "body": body,
        "more_body": False,
    })
