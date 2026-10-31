"""ORM tables for scoped settings (spec §3 "Tables")."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Index, Integer, String
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

    # Optimistic concurrency: UPDATE ... WHERE version = <loaded>; a concurrent
    # writer makes the loser raise StaleDataError (mapped to 412 by the API).
    __mapper_args__ = {"version_id_col": version}


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


class ScopeNodeRow(Base):
    """One beamline or branch of the scope tree (facility and users are implicit).

    Seeded from SCOPES_CONFIG at startup (add-only); facility admins add and remove
    nodes through the API afterwards.
    """

    __tablename__ = "scope_nodes"

    scope: Mapped[str] = mapped_column(String(300), primary_key=True)  # beamline:/branch:
    parent: Mapped[str | None] = mapped_column(String(300), nullable=True)
    label: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    created_by: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ContentItemRow(Base):
    """A shared plan/plugin/agent/skill at a scope; ``current_revision`` None = retracted."""

    __tablename__ = "content_items"

    scope: Mapped[str] = mapped_column(String(300), primary_key=True)
    type: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    current_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class ContentRevisionRow(Base):
    """One immutable published bundle. Never deleted; retract only moves the item's pointer."""

    __tablename__ = "content_revisions"
    __table_args__ = (Index("ix_content_revisions_item", "scope", "type", "name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String(300), nullable=False)
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    manifest: Mapped[Any] = mapped_column(JSON, nullable=False)
    published_by: Mapped[str] = mapped_column(String(256), nullable=False)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    author: Mapped[str | None] = mapped_column(String(256), nullable=True)
    message: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
    agent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
