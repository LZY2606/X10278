"""Domain quantities, raw representations and unit / coordinate bases.

Imported values keep their *raw* representation; only the derivation layer
normalises them.  Every domain quantity carries a unit or a coordinate basis.
Values that cannot be converted stay in place with a diagnostic instead of
being silently dropped for display purposes.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


# ---------------------------------------------------------------- units ----
NS = "ns"                       # nanoseconds, absolute segment time axis
MS = "ms"
TICKS = "tick"                  # TimecodeScale ticks (segment time base)
CODEC_SAMPLES = "codec_sample"
HZ = "Hz"
BYTE = "byte"
DIMENSIONLESS = "1"

# ------------------------------------------------------------- bases -------
SEGMENT_BASIS = "segment-timecode-scale"
TRACK_BASIS = "track-timecode-scale"
FILE_BASIS = "byte-offset"
RAW_BASIS = "raw-element-bytes"


@dataclass
class Diagnostic:
    code: str
    message: str
    severity: str = "error"        # error | warning | info
    subject: str | None = None     # stable id of the affected object
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def signed_of(data: bytes, width: int | None = None) -> int:
    """Interpret *data* as a signed big-endian integer of its own width."""
    width = width or len(data)
    value = int.from_bytes(data[:width], "big", signed=False)
    if data and data[0] & 0x80:
        value -= 1 << (8 * len(data[:width]))
    return value


def unsigned_of(data: bytes) -> int:
    return int.from_bytes(data, "big", signed=False)


@dataclass
class Raw:
    """An imported scalar with its on-disk representation preserved."""

    value: Any
    unit: str
    basis: str
    raw_hex: str | None = None
    encoding: str | None = None    # e.g. "uint-be", "sint-be", "float64-be"
    convertible: bool = True
    diagnostics: list[Diagnostic] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        out = asdict(self)
        out["diagnostics"] = [d.to_json() for d in self.diagnostics]
        return out

    @classmethod
    def from_bytes(
        cls,
        data: bytes,
        unit: str,
        basis: str,
        *,
        signed: bool = False,
        encoding: str = "uint-be",
    ) -> "Raw":
        diag: list[Diagnostic] = []
        if not data:
            return cls(
                None, unit, basis, raw_hex="", encoding=encoding,
                convertible=False,
                diagnostics=[Diagnostic("EMPTY_INTEGER", "zero-length integer")],
            )
        value = signed_of(data) if signed else unsigned_of(data)
        return cls(value, unit, basis, data.hex(), encoding, True, diag)


@dataclass
class Q:
    """A normalised derived quantity with unit and coordinate basis."""

    value: float | int | None
    unit: str
    basis: str
    diagnostics: list[Diagnostic] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        value = self.value
        if hasattr(value, "item"):
            value = value.item()
        return {
            "value": value,
            "unit": self.unit,
            "basis": self.basis,
            "diagnostics": [d.to_json() for d in self.diagnostics],
        }


def py(value: Any) -> Any:
    """Convert numpy scalars to plain Python values."""
    return value.item() if hasattr(value, "item") else value
