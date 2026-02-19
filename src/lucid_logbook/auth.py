"""Keycloak JWT authentication middleware for Litestar.

When enabled (via env vars), validates Bearer tokens against the Keycloak
JWKS endpoint and injects ``request.state.user_id`` from the ``sub`` claim.

When disabled, falls through without authentication (dev mode).
"""

from __future__ import annotations

import os
from typing import Any

from loguru import logger
from litestar.connection import Request
from litestar.middleware.base import AbstractMiddleware
from litestar.types import ASGIApp, Receive, Scope, Send


def keycloak_auth_enabled() -> bool:
    """Check if Keycloak env vars are set."""
    return bool(
        os.environ.get("KEYCLOAK_URL")
        and os.environ.get("KEYCLOAK_REALM")
    )


def _get_keycloak_config() -> dict[str, str]:
    return {
        "url": os.environ["KEYCLOAK_URL"],
        "realm": os.environ["KEYCLOAK_REALM"],
        "client_id": os.environ.get("KEYCLOAK_CLIENT_ID", "lucid-logbook"),
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


class KeycloakAuthMiddleware(AbstractMiddleware):
    """Litestar middleware that validates Keycloak JWT Bearer tokens.

    Sets ``scope["state"]["user_id"]`` from the token ``sub`` claim.
    Skips the ``/health`` endpoint.
    """

    scopes = {"/health"}  # excluded paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Skip excluded paths
        path = scope.get("path", "")
        if path in self.scopes:
            await self.app(scope, receive, send)
            return

        # Extract Bearer token
        headers = dict(scope.get("headers", []))
        auth_header = headers.get(b"authorization", b"").decode()

        if not auth_header.startswith("Bearer "):
            await _send_401(send, "Missing Bearer token")
            return

        token = auth_header[7:]

        try:
            claims = decode_token(token)
        except Exception as e:
            logger.debug("JWT validation failed: {}", e)
            await _send_401(send, "Invalid token")
            return

        # Inject user_id into scope state
        if "state" not in scope:
            scope["state"] = {}
        scope["state"]["user_id"] = claims.get("sub", "")
        scope["state"]["user_claims"] = claims

        await self.app(scope, receive, send)


async def _send_401(send: Send, detail: str) -> None:
    """Send a 401 Unauthorized ASGI response."""
    import json

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
    })
