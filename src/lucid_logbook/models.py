"""Data models for the logbook Entry→Fragment system.

Provides both SQLAlchemy ORM models (for persistence) and Pydantic v2
schemas (for API serialization). The design supports:

- A per-user Logbook containing ordered Entries
- Entries composed of Fragments (editable text or read-only system records)
- Flexible JSON ``data`` column on Fragment for extensible readonly subtypes
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, Uuid
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# ---------------------------------------------------------------------------
# SQLAlchemy ORM models
# ---------------------------------------------------------------------------


class Base(DeclarativeBase):
    """Shared declarative base for logbook tables."""


class LogbookRow(Base):
    __tablename__ = "logbooks"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[str] = mapped_column(String(256), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    entries: Mapped[list[EntryRow]] = relationship(
        back_populates="logbook", cascade="all, delete-orphan", lazy="selectin"
    )


class EntryRow(Base):
    __tablename__ = "entries"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    logbook_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("logbooks.id"), nullable=False
    )
    title: Mapped[str | None] = mapped_column(String(512), nullable=True, default=None)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    # Sync tracking (used by local_store offline mode)
    sync_status: Mapped[str] = mapped_column(
        String(16), default="synced", server_default="synced"
    )

    logbook: Mapped[LogbookRow] = relationship(back_populates="entries")
    fragments: Mapped[list[FragmentRow]] = relationship(
        back_populates="entry",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="FragmentRow.position",
    )


class FragmentRow(Base):
    __tablename__ = "fragments"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    entry_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("entries.id"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="text")
    subtype: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    content: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    # Sync tracking
    sync_status: Mapped[str] = mapped_column(
        String(16), default="synced", server_default="synced"
    )

    entry: Mapped[EntryRow] = relationship(back_populates="fragments")


# ---------------------------------------------------------------------------
# Pydantic API schemas
# ---------------------------------------------------------------------------


class FragmentCreate(BaseModel):
    """Payload for creating a new fragment."""

    kind: str = "text"
    subtype: str | None = None
    content: str = ""
    data: dict[str, Any] | None = None
    position: int | None = None  # auto-assigned if omitted


class FragmentUpdate(BaseModel):
    """Payload for updating an existing (text) fragment."""

    content: str | None = None
    position: int | None = None


class FragmentSchema(BaseModel):
    """Read-only representation returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    entry_id: uuid.UUID
    position: int
    kind: str
    subtype: str | None = None
    content: str
    data: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime


class EntryCreate(BaseModel):
    title: str | None = None
    tags: list[str] = Field(default_factory=list)


class EntryUpdate(BaseModel):
    title: str | None = None
    tags: list[str] | None = None


class EntrySchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    logbook_id: uuid.UUID
    title: str | None = None
    tags: list[str]
    created_at: datetime
    updated_at: datetime
    fragments: list[FragmentSchema] = Field(default_factory=list)


class LogbookSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: str
    created_at: datetime
