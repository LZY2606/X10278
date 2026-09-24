"""Domain quantities always carry a unit or coordinate basis.

Raw import keeps the original representation; only the derivation layer
normalizes.  Values that cannot be converted stay in place with a diagnostic
instead of being dropped for the sake of drawing a timeline.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Measure:
    """A scalar value with an explicit unit/coordinate basis.

    ``raw`` preserves the original on-wire representation (often identical to
    ``value`` for integral quantities).  ``diagnostic`` is set when the value
    could not be converted into ``unit`` -- the value is retained, never
    silently discarded.
    """

    value: float | int | None
    unit: str
    raw: object = None
    basis: str = ""
    diagnostic: str | None = None

    def to_dict(self) -> dict:
        return {
            "value": self.value,
            "unit": self.unit,
            "raw": self.raw,
            "basis": self.basis,
            "diagnostic": self.diagnostic,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Measure":
        return cls(
            value=data.get("value"),
            unit=data["unit"],
            raw=data.get("raw"),
            basis=data.get("basis", ""),
            diagnostic=data.get("diagnostic"),
        )


# Coordinate bases used across the project.
BYTE_OFFSET = "byte-offset"  # 0-based offset from file start
SEGMENT_OFFSET = "segment-relative-byte"  # relative to Segment data start
RAW_INT = "raw-uint"  # unconverted on-wire integer
RAW_BYTES = "raw-bytes"
NS = "ns"
MS = "ms"
TICKS = "ticks"  # unscaled Matroska integer timecode
FRAMES = "frames"


def scale_ticks_to_ms(ticks: int, timecode_scale_ns: int) -> float:
    """Convert unscaled timecode ticks to milliseconds."""
    return ticks * timecode_scale_ns / 1_000_000.0


def ms_to_ns(ms: float) -> int:
    return int(round(ms * 1_000_000.0))

