from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Quantity:
    value: Any
    unit: str
    coordinate_basis: str | None = None
    raw_representation: Any = None
    raw_unit: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Diagnostic:
    code: str
    severity: str
    message: str
    subject_id: str
    byte_range: list[int] | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    status: str = "open"
    legal_candidates: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
