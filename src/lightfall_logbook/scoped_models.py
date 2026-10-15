"""ORM tables for scoped settings (spec §3 "Tables")."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from lightfall_logbook.models import Base


def _now() -> datetime:
    return datetime.now(UTC)


class ScopedSettingRow(Base):
    __tablename__ = "scoped_settings"

    scope: Mapped[str] = mapped_column(String(300), primary_key=True)
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_by: Mapped[str] = mapped_column(String(256), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class SettingLockRow(Base):
    __tablename__ = "setting_locks"

    scope: Mapped[str] = mapped_column(String(300), primary_key=True)
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    locked_by: Mapped[str] = mapped_column(String(256), nullable=False)
    reason: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
    locked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class SettingAuditRow(Base):
    __tablename__ = "setting_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String(300), index=True, nullable=False)
    key: Mapped[str] = mapped_column(String(200), index=True, nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    old: Mapped[Any] = mapped_column(JSON, nullable=True)
    new: Mapped[Any] = mapped_column(JSON, nullable=True)
    actor: Mapped[str] = mapped_column(String(256), nullable=False)
    agent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class SettingKeyRow(Base):
    __tablename__ = "setting_keys"

    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # pending|approved|rejected
    spec: Mapped[Any] = mapped_column(JSON, nullable=True)
    proposed_spec: Mapped[Any] = mapped_column(JSON, nullable=True)
    proposed_by: Mapped[str | None] = mapped_column(String(256), nullable=True)
    proposed_scope: Mapped[str | None] = mapped_column(String(300), nullable=True)
    proposed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(256), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
