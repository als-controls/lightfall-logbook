"""Helpers for shared-content API tests: bundles in memory and rows seeded directly."""
from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import async_sessionmaker

from lightfall_logbook.bundle_store import BundleStore
from lightfall_logbook.scoped_models import ContentItemRow, ContentRevisionRow


def make_bundle(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


async def seed_revision(factory: async_sessionmaker, store: BundleStore, scope: str,
                        type_: str, name: str, data: bytes, *, current: bool = True,
                        published_by: str = "seed", message: str = "") -> int:
    """Insert a revision (and its item) as if it had been published; returns its id."""
    sha = store.put(data)
    async with factory() as s:
        rev = ContentRevisionRow(scope=scope, type=type_, name=name, sha256=sha,
                                 size=len(data), manifest={}, published_by=published_by,
                                 published_at=datetime.now(UTC), author=None,
                                 message=message, agent=False)
        s.add(rev)
        await s.flush()
        item = await s.get(ContentItemRow, (scope, type_, name))
        if item is None:
            item = ContentItemRow(scope=scope, type=type_, name=name)
            s.add(item)
        item.current_revision = rev.id if current else item.current_revision
        await s.commit()
        return rev.id
