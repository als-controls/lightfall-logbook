"""NATS publisher for logbook change notifications (notify-and-pull).

Publishes a tiny event after each write so connected clients know to pull.
Best-effort: if NATS is unconfigured or unreachable, every method is a no-op
and never raises into the request path.
"""
from __future__ import annotations

import contextlib
import json
import ssl
from typing import Any

from loguru import logger


def subject_for_user(user_id: str) -> str:
    """Well-known, subject-safe topic for a user's logbook changes."""
    token = user_id.encode("utf-8").hex()
    return f"_lightfall.logbook.changed.{token}"


class LogbookEventPublisher:
    """Best-effort NATS publisher. No-op when ``nats_url`` is falsy."""

    def __init__(self, nats_url: str | None) -> None:
        self._nats_url = nats_url
        self._nc: Any = None

    async def connect(self) -> None:
        if not self._nats_url:
            return
        try:
            import nats

            kwargs: dict[str, Any] = {}
            if self._nats_url.startswith("tls://"):
                kwargs["tls"] = ssl.create_default_context()
            self._nc = await nats.connect(self._nats_url, **kwargs)
            logger.info("Logbook event publisher connected to NATS at {}", self._nats_url)
        except Exception as exc:
            logger.warning("Logbook event publisher could not connect: {}", exc)
            self._nc = None

    async def close(self) -> None:
        if self._nc is not None:
            with contextlib.suppress(Exception):
                await self._nc.drain()
            self._nc = None

    async def publish_change(
        self,
        *,
        user_id: str,
        op: str,
        kind: str,
        entity_id: str,
        origin: str | None = None,
    ) -> None:
        if self._nc is None:
            return
        payload = json.dumps(
            {"user_id": user_id, "op": op, "kind": kind, "id": entity_id, "origin": origin}
        ).encode()
        await self._nc.publish(subject_for_user(user_id), payload)
