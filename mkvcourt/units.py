"""Domain quantities that always carry a unit / coordinate basis.

Two layers are kept distinct on purpose:

* :class:`Raw` preserves the representation exactly as it was found in the
  Matroska bytes (field, original int/float, encoding basis, byte offset).
* :class:`Quant` is a *derived*, normalised quantity expressed in one of the
  canonical bases (ns, ms, raw-ticks, bytes, ...).

A value that cannot be converted is *not* dropped: it stays at its original
location as a :class:`Raw` annotated with a diagnostic, so the UI can render
"unconvertible" instead of silently losing evidence.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# Canonical coordinate bases used by the derived layer.
NS = "ns"              # absolute presentation time, nanoseconds
MS = "ms"              # cluster / file time, milliseconds
TICKS = "ticks"        # block relative timecode, signed, in the track's raw tc unit
BYTES = "bytes"        # file coordinate
RATIO = "ratio"        # dimensionless scale factor
FRAMES = "frames"      # ordinal, only inside one block/lace
UNKNOWN = "unknown"    # preserved but not normalised


@dataclass(frozen=True)
class Raw:
    """A value exactly as encoded, with the basis needed to interpret it."""

    value: Any
    field: str
    basis: str = UNKNOWN
    signed_width_bits: Optional[int] = None
    source: Optional[str] = None          # element uid / byte range anchor
    note: Optional[str] = None
    diagnostics: tuple[str, ...] = ()

    def with_diag(self, msg: str) -> "Raw":
        return Raw(self.value, self.field, self.basis, self.signed_width_bits,
                   self.source, self.note, self.diagnostics + (msg,))


@dataclass(frozen=True)
class Quant:
    """A normalised derived quantity."""

    value: float
    unit: str
    basis: str
    source_raw: tuple[str, ...] = field(default_factory=tuple)
    uncertain: bool = False
    note: Optional[str] = None

    def is_valid(self) -> bool:
        return self.value == self.value  # NaN guard


def s16(word: int) -> int:
    """Interpret a 16-bit block relative timecode as signed (Matroska §Block)."""
    word &= 0xFFFF
    return word - 0x10000 if word & 0x8000 else word


def s64(v: int) -> int:
    v &= 0xFFFFFFFFFFFFFFFF
    return v - (1 << 64) if v & (1 << 63) else v


def block_time_ns(cluster_ticks: float, rel_ticks: float,
                  timecode_scale_ns: float, track_ts_scale: float) -> float:
    """Frame presentation time relative to segment origin, in ns.

    Matroska ordering: the global TimestampScale is applied first, then the
    per-track TrackTimestampScale, to the *sum* of cluster timecode and the
    signed block relative timecode::

        t_ns = (ClusterTimecode + signed_block_tc)
               * TimestampScale_ns * TrackTimestampScale
    """
    return (cluster_ticks + rel_ticks) * timecode_scale_ns * track_ts_scale
