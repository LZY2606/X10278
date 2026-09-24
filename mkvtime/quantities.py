"""Domain quantities that always carry a unit or coordinate basis.

Raw Matroska values are kept in their original representation at import time;
normalisation only happens in the derived layer. A value that cannot be
converted stays at its original location with a diagnostic instead of being
silently dropped to make a picture render.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass(frozen=True)
class Quantity:
    """A scalar with an explicit unit or coordinate basis."""

    value: Any
    unit: str
    basis: str = ""
    raw_repr: Any = None
    diagnostics: tuple[str, ...] = ()

    @classmethod
    def raw_ticks(cls, value: int, basis: str) -> "Quantity":
        return cls(value=int(value), unit="matroska-tick", basis=basis, raw_repr=int(value))

    @classmethod
    def nanos(cls, value: float, basis: str, raw_repr: Any = None) -> "Quantity":
        return cls(value=float(value), unit="ns", basis=basis, raw_repr=raw_repr)

    @classmethod
    def bytes_offset(cls, value: int) -> "Quantity":  # noqa: N805 - dataclass-style factory
        return cls(value=int(value), unit="byte-offset", basis="file-start", raw_repr=int(value))

    @classmethod
    def unconvertible(cls, value: Any, unit: str, basis: str, reason: str) -> "Quantity":
        return cls(
            value=value,
            unit=unit,
            basis=basis,
            raw_repr=value,
            diagnostics=(f"unconvertible: {reason}",),
        )

    def to_dict(self) -> dict:
        d = asdict(self)
        d["diagnostics"] = list(self.diagnostics)
        return d

    def with_diagnostic(self, reason: str) -> "Quantity":
        return Quantity(
            value=self.value,
            unit=self.unit,
            basis=self.basis,
            raw_repr=self.raw_repr,
            diagnostics=self.diagnostics + (reason,),
        )
