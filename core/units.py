"""Units and domain quantities.

Every domain value carries a unit or coordinate basis. Raw values keep their
original representation; the derivation layer is the only place where
normalisation happens. Values that cannot be converted stay in place with a
diagnostic instead of being silently dropped.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any


# Coordinate bases -----------------------------------------------------------
BYTE_OFFSET = "byte"               # file-space coordinate, unsigned
RAW_TICKS = "matroska-tick"        # unscaled Matroska ticks (uint element)
REL_TICKS_SIGNED = "matroska-tick-rel-s16"  # Block signed relative ticks
NS = "ns"                          # nanoseconds, normalised timeline
SCALE_DIMENSIONLESS = "dimensionless-ratio"
ELEMENT_ID = "ebml-element-id"
TRACK_UID = "track-uid"


@dataclass(frozen=True)
class Quantity:
    """A value plus the unit/coordinate basis it was measured in.

    ``value`` is the normalised machine value (int/float/str/bytes/None).
    ``raw`` preserves the original on-wire representation when different.
    ``diagnostic`` explains why conversion was impossible, if it was.
    """

    value: Any
    unit: str
    raw: Any = None
    diagnostic: str | None = None

    def with_value(self, value: Any, unit: str | None = None) -> "Quantity":
        return replace(self, value=value, unit=unit or self.unit)

    def to_dict(self) -> dict:
        return {
            "value": self.value,
            "unit": self.unit,
            "raw": self.raw,
            "diagnostic": self.diagnostic,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Quantity":
        return cls(
            value=d["value"],
            unit=d["unit"],
            raw=d.get("raw"),
            diagnostic=d.get("diagnostic"),
        )


def q(value: Any, unit: str, raw: Any = None, diagnostic: str | None = None) -> Quantity:
    return Quantity(value=value, unit=unit, raw=raw, diagnostic=diagnostic)


def scaled_ticks_to_ns(ticks: int, timecode_scale_ns: float,
                       track_ts_scale: float) -> int:
    """Matroska presentation-time normalisation.

    pts_ns = (cluster_ticks + relative_ticks) * TrackTimestampScale
             * TimecodeScale
    """
    return int(round(ticks * track_ts_scale * timecode_scale_ns))


def ticks_to_ns(ticks: int, timecode_scale_ns: float) -> int:
    return int(round(ticks * timecode_scale_ns))
