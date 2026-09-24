"""Machine-readable review events.

Every import, derivation, parameter revision, candidate acceptance/rejection
and branch decision is an immutable event. Each event carries rule evidence and
records both adopted and rejected candidates.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Optional


def new_event_id() -> str:
    return f"ev:{uuid.uuid4().hex[:12]}"


@dataclass
class Event:
    id: str
    seq: int
    branch: str
    base_version: int
    timestamp: float
    actor: str
    kind: str
    payload: dict[str, Any]
    rule_refs: list[str] = field(default_factory=list)
    adopted: list[dict] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    affected_uids: list[str] = field(default_factory=list)
    parent_event_id: Optional[str] = None
    derived_version: Optional[int] = None

    def to_dict(self) -> dict:
        return asdict(self)


def make_event(kind: str, payload: dict, *, actor="reviewer",
               branch="main", base_version=0, seq=0,
               rule_refs=None, adopted=None, rejected=None,
               affected_uids=None, parent_event_id=None,
               derived_version=None) -> Event:
    return Event(
        id=new_event_id(), seq=seq, branch=branch,
        base_version=base_version, timestamp=time.time(), actor=actor,
        kind=kind, payload=payload, rule_refs=rule_refs or [],
        adopted=adopted or [], rejected=rejected or [],
        affected_uids=affected_uids or [], parent_event_id=parent_event_id,
        derived_version=derived_version)
