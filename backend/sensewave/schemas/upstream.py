"""Pydantic model of RuView's ``SensingUpdate`` WebSocket frame.

Contract source: the appendix in the SenseWave build brief, itself read from
RuView at commit ``a3b6e1d``. It has NOT yet been reconciled against a live
sensing-server (see docs/ARCHITECTURE.md, "Upstream contract discrepancies") —
when it is, the server wins and this file changes.

Two rules shape this module and neither is negotiable:

* **Never coerce.** Models are ``strict``: a string where a float belongs is a
  validation error, not a silent cast. Bad frames are dropped and counted, so a
  contract drift shows up as a rising drop counter and a log line naming the
  offending field, rather than as plausible-looking garbage in the database.
* **Never fabricate.** ``breathing_rate_bpm`` / ``heart_rate_bpm`` are genuinely
  nullable; null means "not detected" and must reach the UI as an absence, never
  as 0. Optional sections are *absent*, not null, when unavailable.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

# Frames are validated strictly but must tolerate fields RuView adds later:
# unknown keys are preserved rather than rejected, so a server upgrade does not
# take the ingest worker down.
_STRICT = ConfigDict(strict=True, extra="allow")

# Fixed-length coordinate arrays. Annotated list rather than tuple: frames
# arrive as JSON, JSON has no tuple type, and strict mode will not convert one
# into the other -- a tuple annotation silently rejects every real frame.
Vec3 = Annotated[list[float], Field(min_length=3, max_length=3)]
Grid3 = Annotated[list[int], Field(min_length=3, max_length=3)]


class NodeSnapshot(BaseModel):
    model_config = _STRICT

    node_id: int
    rssi_dbm: float
    position: Vec3 | None = None
    amplitude: list[float] = Field(default_factory=list)
    subcarrier_count: int = 0


class Features(BaseModel):
    model_config = _STRICT

    mean_rssi: float = 0.0
    variance: float = 0.0
    motion_band_power: float = 0.0
    breathing_band_power: float = 0.0
    dominant_freq_hz: float = 0.0
    change_points: int = 0
    spectral_power: float = 0.0


class Classification(BaseModel):
    model_config = _STRICT

    motion_level: str = "unknown"
    presence: bool = False
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class VitalSigns(BaseModel):
    """Vitals always arrive with their confidences. Product rule 1 depends on
    this pairing surviving all the way to the component that renders it, so the
    rate and its confidence are never separated in any model below this one."""

    model_config = _STRICT

    # Nullable by contract. null = not detected = renders "—", never 0.
    breathing_rate_bpm: float | None = None
    heart_rate_bpm: float | None = None
    breathing_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    heartbeat_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    signal_quality: float = Field(default=0.0, ge=0.0, le=1.0)


class NodeFeatures(BaseModel):
    """Per-node health. ``stale`` and ``last_seen_ms`` are the staleness source
    of truth (product rule 3) — we do not infer staleness from arrival time when
    the server has told us directly."""

    model_config = _STRICT

    node_id: int
    rssi_dbm: float = 0.0
    last_seen_ms: int = 0
    frame_rate_hz: float = 0.0
    novelty_score: float = 0.0
    stale: bool = False
    features: dict[str, Any] = Field(default_factory=dict)
    classification: dict[str, Any] = Field(default_factory=dict)


class Person(BaseModel):
    """An anonymous track. Product rule 5: RuView measured that WiFi-only
    channels cannot separate individuals (gap ~0.0005), so ``id`` is a
    within-frame track handle with no identity meaning and is never persisted
    as though it named a person.

    ``keypoints`` and ``pose`` are accepted so the frame validates, and then
    discarded — see product rule 4.
    """

    model_config = _STRICT

    id: int
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    zone: str | None = None
    position: Vec3 | None = None
    motion_score: float = 0.0


class SignalField(BaseModel):
    model_config = _STRICT

    grid_size: Grid3 | None = None
    values: list[float] = Field(default_factory=list)


class SensingUpdate(BaseModel):
    """One broadcast tick from ``/ws/sensing`` (10 Hz at default --tick-ms).

    ``pose_keypoints`` is deliberately absent from this model. RuView's
    on-device pose checkpoint is PCK@20 = 3.0% and its runtime path returns
    confidence=0; ``extra="allow"`` means the key survives on the raw object but
    nothing downstream reads it, and no skeleton view exists to consume it.
    """

    model_config = _STRICT

    type: str
    timestamp: float
    source: str
    tick: int

    nodes: list[NodeSnapshot] = Field(default_factory=list)
    features: Features = Field(default_factory=Features)
    classification: Classification = Field(default_factory=Classification)
    signal_field: SignalField | None = None

    # Optional sections — absent, not null, when the server has nothing to say.
    vital_signs: VitalSigns | None = None
    posture: str | None = None
    signal_quality_score: float | None = None
    quality_verdict: str | None = None
    bssid_count: int | None = None
    estimated_persons: int | None = None
    persons: list[Person] = Field(default_factory=list)
    node_features: list[NodeFeatures] = Field(default_factory=list)
