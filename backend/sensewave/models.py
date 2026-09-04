"""SQLAlchemy 2 models.

Design notes tied to the product rules:

* ``Room.monitoring_enabled`` and ``Room.privacy_mode`` are consent controls and
  they are enforced in the *ingest path*, not in the UI (product rule 7). A room
  with monitoring off persists no readings at all; a room in privacy mode
  persists presence and motion but drops vitals before they reach the database.
* ``Reading`` stores every vitals estimate next to its confidence. There is no
  path that writes a rate without its confidence.
* Occupants are anonymous counts (product rule 5). There is no person table and
  no identity column anywhere in this schema.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class Site(Base):
    __tablename__ = "sites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    rooms: Mapped[list[Room]] = relationship(back_populates="site")


class Room(Base):
    __tablename__ = "rooms"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(120), nullable=False)

    # ─── Consent controls (product rule 7) ───────────────────────────────
    # Enforced at ingest. See sensewave/ingest/worker.py.
    monitoring_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    privacy_mode: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    site: Mapped[Site] = relationship(back_populates="rooms")
    nodes: Mapped[list[Node]] = relationship(back_populates="room")


class Node(Base):
    """A physical ESP32 sensor. ``node_id`` is RuView's integer id."""

    __tablename__ = "nodes"
    __table_args__ = (UniqueConstraint("node_id", name="uq_nodes_node_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    node_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    room_id: Mapped[int | None] = mapped_column(
        ForeignKey("rooms.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str | None] = mapped_column(String(120), nullable=True)

    pos_x: Mapped[float | None] = mapped_column(Float, nullable=True)
    pos_y: Mapped[float | None] = mapped_column(Float, nullable=True)
    pos_z: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Health, refreshed by the ingest worker from node_features[].
    last_seen: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rssi_dbm: Mapped[float | None] = mapped_column(Float, nullable=True)
    frame_rate_hz: Mapped[float | None] = mapped_column(Float, nullable=True)
    novelty_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Upstream's own staleness verdict; authoritative over our clock (rule 3).
    upstream_stale: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    room: Mapped[Room | None] = relationship(back_populates="nodes")


class Reading(Base):
    """Time-series sample. Hypertable on ``time`` when TimescaleDB is present.

    Written at SENSEWAVE_INGEST_HZ by *decimation* — we keep the most recent
    real frame in each interval. We never average, interpolate or carry a value
    forward, so a gap in the upstream stream is a gap in this table (rule 2).
    """

    __tablename__ = "readings"

    time: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, nullable=False
    )
    room_id: Mapped[int] = mapped_column(
        ForeignKey("rooms.id", ondelete="CASCADE"), primary_key=True, nullable=False
    )

    presence: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    presence_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    motion_level: Mapped[str | None] = mapped_column(String(32), nullable=True)
    motion_band_power: Mapped[float | None] = mapped_column(Float, nullable=True)
    breathing_band_power: Mapped[float | None] = mapped_column(Float, nullable=True)
    mean_rssi: Mapped[float | None] = mapped_column(Float, nullable=True)
    estimated_persons: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # ─── Vitals: rate and confidence are written together or not at all ──
    breathing_rate_bpm: Mapped[float | None] = mapped_column(Float, nullable=True)
    breathing_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    heart_rate_bpm: Mapped[float | None] = mapped_column(Float, nullable=True)
    heartbeat_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    signal_quality: Mapped[float | None] = mapped_column(Float, nullable=True)
    # True when the room was in privacy mode and vitals were dropped at ingest.
    # Distinguishes "suppressed by consent" from "not detected" in the UI.
    vitals_suppressed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class Event(Base):
    """Discrete upstream occurrence (fall, node offline, calibration…)."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    time: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False, index=True
    )
    room_id: Mapped[int | None] = mapped_column(ForeignKey("rooms.id", ondelete="SET NULL"))
    node_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("email", name="uq_users_email"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
