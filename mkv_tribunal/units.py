"""Quantities always carry their unit / coordinate basis."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Q:
    value: float
    unit: str
    basis: str = ""

    def as_dict(self) -> dict:
        return {"value": self.value, "unit": self.unit, "basis": self.basis}


def scaled_to_ns(scale_units: float, timecode_scale_ns: float) -> float:
    return float(scale_units) * float(timecode_scale_ns)
