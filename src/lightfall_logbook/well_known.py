"""``GET /.well-known/lightfall``: the facility bootstrap a fresh client reads before login.

Built from facility-scope settings whose spec (core or approved) is ``public``.
Unset keys are omitted. Clients ignore fields they don't know.
"""
from __future__ import annotations

import re
from typing import Any

from lightfall_settings import FACILITY, SECRET_MARKERS
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lightfall_logbook.scoped_models import ScopedSettingRow
from lightfall_logbook.scoped_store import spec_for

SCHEMA = 1

# Public facility key -> (response section, field).
SECTIONS: dict[str, tuple[str, str]] = {
    "auth.provider": ("auth", "provider"),
    "auth.server_url": ("auth", "server_url"),
    "auth.realm": ("auth", "realm"),
    "auth.client_id": ("auth", "client_id"),
    "tiled_url": ("services", "tiled_url"),
    "logbook_url": ("services", "logbook_url"),
    "ipc_nats_url": ("services", "ipc_nats_url"),
    "facility.proxy_domains": ("network", "proxy_domains"),
    "agent.backend": ("agent", "backend"),
    "agent.agy_gcp_project": ("agent", "agy_gcp_project"),
    "agent.agy_gcp_location": ("agent", "agy_gcp_location"),
}


def facility_id(label: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
    return slug or "facility"


async def build_bootstrap(session: AsyncSession, label: str) -> dict[str, Any]:
    out: dict[str, Any] = {
        "schema": SCHEMA,
        "facility": {"id": facility_id(label), "name": label},
    }
    rows = (await session.execute(
        select(ScopedSettingRow).where(ScopedSettingRow.scope == str(FACILITY))
    )).scalars().all()
    for row in sorted(rows, key=lambda r: r.key):
        target = SECTIONS.get(row.key)
        if target is None or row.value is None:
            continue
        if any(marker in row.key.lower() for marker in SECRET_MARKERS):
            continue  # belt and braces: public specs already exclude these
        spec, known = await spec_for(session, row.key)
        if not (known and spec.public):
            continue
        section, name = target
        out.setdefault(section, {})[name] = row.value
    return out
