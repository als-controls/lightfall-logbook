from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from lightfall_logbook.models import Base, UserSettingRow
from lightfall_logbook.scoped_models import ScopedSettingRow, SettingAuditRow
from lightfall_logbook.scoped_store import migrate_user_settings


@pytest.fixture
async def factory(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'm.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def test_migrates_once_and_never_overwrites(factory):
    async with factory() as s:
        s.add_all([
            UserSettingRow(user_id="sub-1", beamline="", key="profile_image_id", value="img"),
            UserSettingRow(user_id="sub-1", beamline="", key="theme", value="dark"),
            UserSettingRow(user_id="sub-1", beamline="7.0.1", key="device_favorites", value=[]),
            UserSettingRow(user_id="bad id!", beamline="", key="theme", value="x"),
        ])
        s.add(ScopedSettingRow(scope="user:sub-1", key="theme", value="light",
                               updated_by="sub-1"))
        await s.commit()
    async with factory() as s:
        existing = await s.get(ScopedSettingRow, ("user:sub-1", "theme"))
        before = (existing.value, existing.version)

    assert await migrate_user_settings(factory) == 1
    assert await migrate_user_settings(factory) == 0

    async with factory() as s:
        rows = {(r.scope, r.key): (r.value, r.version)
                for r in (await s.execute(select(ScopedSettingRow))).scalars()}
        audits = (await s.execute(select(SettingAuditRow))).scalars().all()
    assert rows[("user:sub-1", "profile_image_id")] == ("img", 1)
    assert rows[("user:sub-1", "theme")] == before == ("light", 1)
    assert ("user:sub-1", "device_favorites") not in rows
    assert [a.action for a in audits] == ["migrate"]
