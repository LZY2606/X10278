"""Domain quantities with explicit units / coordinate bases.

Every value that flows into the UI and the archive is represented as a
plain JSON-friendly tuple ``{"raw": ..., "unit": ..., "base": ...,
"normalized_ns": ..., "diagnostics": [...]}``. A value that cannot be
converted stays at its original location with diagnostics instead of
being silently dropped to make a timeline drawable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


# Coordinate bases used across the domain.
BASE_CLUSTER_TC = "cluster_timecode_units"          # signed, track-independent
BASE_BLOCK_REL = "block_relative_int16"             # signed 16-bit per Matroska
BASE_NS = "global_nanoseconds"
BASE_BYTE = "byte_offset_from_container_start"      # absolute file offset
BASE_SEGMENT_BYTE = "byte_offset_from_segment_data_start"
BASE_TICK = "matroska_tick"                         # TimecodeScale-scaled tick
BASE_DISCARD_NS = "discard_padding_nanoseconds"     # signed
BASE_TRACK_RATE = "track_timestamp_scale_ratio"     # dimensionless float
BASE_AUDIO_FREQ = "hertz"

INFO_WARNING = "warning"
INFO_ERROR = "error"


@dataclass(frozen=True)
class Q:
    """A quantity: raw representation, unit, coordinate base, optional norm."""

    raw: Any
    unit: str
    base: str
    normalized_ns: Optional[float] = None
    diagnostics: tuple = field(default_factory=tuple)

    def to_json(self) -> dict:
        return {
            "raw": self.raw,
            "unit": self.unit,
            "base": self.base,
            "normalized_ns": self.normalized_ns,
            "diagnostics": list(self.diagnostics),
        }


def diag(level: str, code: str, message: str, **extra: Any) -> dict:
    out = {"level": level, "code": code, "message": message}
    out.update(extra)
    return out


def q(raw: Any, unit: str, base: str, normalized_ns: Optional[float] = None,
      diagnostics: Optional[list] = None) -> dict:
    return Q(raw, unit, base, normalized_ns, tuple(diagnostics or [])).to_json()


def tick_to_ns(ticks: float, timecode_scale_ns: float,
               track_timestamp_scale: float = 1.0) -> float:
    """Matroska global timestamp conversion.

    Block raw timecode is expressed in TimecodeScale units. TrackTimestampScale
    is a per-track output-rate factor applied after the global scale.
    """
    return float(ticks) * float(timecode_scale_ns) / float(track_timestamp_scale)
