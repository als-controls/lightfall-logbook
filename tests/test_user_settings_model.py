"""Round-trip tests for UserSettingRow ORM model and schemas."""
from __future__ import annotations

from datetime import datetime
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from lucid_logbook.models import (
    Base,
    UserSettingRow,
    UserSettingSchema,
    UserSettingWrite,
)


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s


@pytest.mark.asyncio
async def test_round_trip(session):
    row = UserSettingRow(
        user_id="alice",
        beamline="",
        key="profile_image_id",
        value="abc-123",
    )
    session.add(row)
    await session.commit()

    result = await session.execute(
        select(UserSettingRow).where(UserSettingRow.user_id == "alice")
    )
    fetched = result.scalar_one()
    assert fetched.key == "profile_image_id"
    assert fetched.value == "abc-123"
    assert fetched.beamline == ""
    assert isinstance(fetched.updated_at, datetime)


@pytest.mark.asyncio
async def test_pk_uniqueness(session):
    """Same (user_id, beamline, key) cannot be inserted twice."""
    row1 = UserSettingRow(user_id="alice", beamline="", key="theme", value="dark")
    session.add(row1)
    await session.commit()

    row2 = UserSettingRow(user_id="alice", beamline="", key="theme", value="light")
    session.add(row2)
    with pytest.raises(Exception):  # IntegrityError, but vendor-specific
        await session.commit()


def test_write_schema_accepts_any_json():
    UserSettingWrite(value="string")
    UserSettingWrite(value=42)
    UserSettingWrite(value={"nested": [1, 2, 3]})
    UserSettingWrite(value=None)


@pytest.mark.asyncio
async def test_schema_round_trip(session):
    """After a real DB round-trip, UserSettingSchema validates with a real updated_at."""
    row = UserSettingRow(
        user_id="bob",
        beamline="11.0.1",
        key="favorite_devices",
        value=["d1", "d2"],
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)

    schema = UserSettingSchema.model_validate(row, from_attributes=True)
    assert schema.user_id == "bob"
    assert schema.beamline == "11.0.1"
    assert schema.value == ["d1", "d2"]
    assert isinstance(schema.updated_at, datetime)
