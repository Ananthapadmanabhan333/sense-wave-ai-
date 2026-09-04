"""Response models for SenseWave's own API.

The vitals shape here is deliberate. ``VitalsOut`` carries every rate together
with its confidence and a server-computed ``*_is_low_confidence`` flag. A client
cannot receive a breathing rate without also receiving the confidence and the
verdict, which is what makes product rule 1 hard to skip rather than merely
documented: there is no field to bind a number to on its own.
"""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, ConfigDict, Field


class VitalsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    breathing_rate_bpm: float | None = None
    breathing_confidence: float | None = None
    breathing_is_low_confidence: bool = True

    heart_rate_bpm: float | None = None
    heartbeat_confidence: float | None = None
    heartbeat_is_low_confidence: bool = True

    signal_quality: float | None = None

    # True when the room is in privacy mode and vitals were dropped at ingest.
    # The UI must distinguish this from "not detected" -- one is a consent
    # decision, the other is a measurement outcome.
    suppressed: bool = False

    @classmethod
    def build(
        cls,
        *,
        breathing_rate_bpm: float | None,
        breathing_confidence: float | None,
        heart_rate_bpm: float | None,
        heartbeat_confidence: float | None,
        signal_quality: float | None,
        suppressed: bool,
        min_confidence: float,
    ) -> VitalsOut:
        """Compute the low-confidence verdicts server-side.

        A missing confidence counts as low. The rate is still returned so a
        client can show it in a diagnostic view, but the flag tells every
        ordinary view to render "low confidence" instead of the number.
        """
        return cls(
            breathing_rate_bpm=breathing_rate_bpm,
            breathing_confidence=breathing_confidence,
            breathing_is_low_confidence=(
                breathing_confidence is None or breathing_confidence < min_confidence
            ),
            heart_rate_bpm=heart_rate_bpm,
            heartbeat_confidence=heartbeat_confidence,
            heartbeat_is_low_confidence=(
                heartbeat_confidence is None or heartbeat_confidence < min_confidence
            ),
            signal_quality=signal_quality,
            suppressed=suppressed,
        )


class NodeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    node_id: int
    name: str | None = None
    room_id: int | None = None
    rssi_dbm: float | None = None
    frame_rate_hz: float | None = None
    novelty_score: float | None = None
    last_seen: dt.datetime | None = None
    # Upstream's verdict, plus ours from last_seen. Either one makes it stale.
    upstream_stale: bool = False
    stale: bool = False


class RoomOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    site_id: int
    name: str
    monitoring_enabled: bool
    privacy_mode: bool
    node_count: int = 0


class ReadingOut(BaseModel):
    """One sample. ``time`` is the upstream frame time, not our write time."""

    model_config = ConfigDict(from_attributes=True)

    time: dt.datetime
    presence: bool | None = None
    presence_confidence: float | None = None
    motion_level: str | None = None
    motion_band_power: float | None = None
    breathing_band_power: float | None = None
    mean_rssi: float | None = None
    estimated_persons: int | None = None
    vitals: VitalsOut


class RoomLatestOut(BaseModel):
    room: RoomOut
    reading: ReadingOut | None = None
    nodes: list[NodeOut] = Field(default_factory=list)
    # True when no node in this room has reported inside the stale window.
    # The UI greys the card out; it must not show the last known value as live.
    stale: bool = True
    stale_reason: str | None = None


class HistoryPoint(BaseModel):
    """A single point on a history series.

    ``value`` is null where no reading exists. The series is NOT gap-filled:
    rule 2 forbids interpolating across a hole, so the client renders the null
    as a visible break in the line rather than a straight segment across it.
    """

    time: dt.datetime
    value: float | None = None
    confidence: float | None = None


class HistoryOut(BaseModel):
    room_id: int
    metric: str
    # Present only for vitals metrics; null for motion/presence series.
    min_confidence: float | None = None
    points: list[HistoryPoint] = Field(default_factory=list)


class HealthOut(BaseModel):
    status: str
    database: str
    storage_mode: str | None = None
    upstream: dict[str, object] = Field(default_factory=dict)
    ws_clients: int = 0
