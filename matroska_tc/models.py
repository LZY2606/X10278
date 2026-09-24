"""Fact / derived / event model.

Import keeps *raw representations with units/coordinate basis*. Normalisation
to nanoseconds only happens in the derived layer. Nothing is discarded to make
plotting easier: values that cannot be converted stay in place with a
diagnostic.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

# ---- coordinate bases / units ----------------------------------------------
U_BYTE = "byte"                      # file offset, coordinate basis = file
U_SEG_BYTE = "byte@segment"          # offset relative to segment data start
U_TICK = "raw_tick"                  # Matroska integer timecode, scale-dependent
U_S16_TICK = "s16_tick"              # signed relative block timecode
U_NS = "ns"                          # normalised time
U_NS_PER_TICK = "ns/raw_tick"        # TimecodeScale
U_HZ = "Hz"
U_PX = "px"
U_NONE = "unitless"

DEFAULT_TIMESCALE_NS = 1_000_000


def oid(kind: str, *parts: Any) -> str:
    """Deterministic stable identity: same inputs -> same id, always."""
    key = kind + "|" + "|".join(str(p) for p in parts)
    return kind + "_" + hashlib.blake2b(key.encode("utf-8"),
                                        digest_size=8).hexdigest()


def q(value: Any, unit: str, raw: Optional[str] = None) -> dict[str, Any]:
    """A quantity carrying its unit and its original representation."""
    out = {"value": value, "unit": unit}
    if raw is not None:
        out["raw"] = raw
    return out


def brange(start: int, end: int, basis: str = U_BYTE) -> dict[str, Any]:
    return {"start": start, "end": end, "unit": basis}


# ---- facts (direct source evidence) ----------------------------------------
@dataclass
class FactBank:
    source_id: str
    segment_data_start: Optional[int] = None
    segment_unknown: bool = False
    timescale: Optional[dict] = None
    elements: list[dict] = field(default_factory=list)
    unknown_lengths: list[dict] = field(default_factory=list)
    tracks: list[dict] = field(default_factory=list)
    clusters: list[dict] = field(default_factory=list)
    blocks: list[dict] = field(default_factory=list)
    frames: list[dict] = field(default_factory=list)
    cues: list[dict] = field(default_factory=list)
    diagnostics: list[dict] = field(default_factory=list)

    def diag(self, kind: str, severity: str, message: str,
             refs: Optional[list[str]] = None,
             byte_range: Optional[dict] = None,
             detail: Optional[dict] = None) -> dict:
        d = {
            "oid": oid("diag", self.source_id, kind,
                       byte_range["start"] if byte_range else "",
                       message[:24]),
            "kind": kind, "severity": severity, "message": message,
            "refs": refs or [], "byte_range": byte_range, "detail": detail or {},
        }
        self.diagnostics.append(d)
        return d

    def to_dict(self) -> dict:
        return asdict(self)


# ---- derived graph ----------------------------------------------------------
@dataclass
class Candidate:
    candidate_id: str
    node_id: str
    label: str
    values: dict
    basis: list[str]
    status: str = "alternative"   # accepted | rejected | alternative

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DerivedNode:
    node_id: str
    kind: str                     # cluster_time | block_time | frame_time |
                                  # cue_resolution | track_timeline | gap ...
    subject_oid: str
    inputs: list[str]
    rule_version: str
    values: dict
    candidates: list[dict] = field(default_factory=list)
    status: str = "ok"            # ok | ambiguous | corrupted | unconvertible
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ---- events / versions ------------------------------------------------------
@dataclass
class Event:
    event_id: str
    seq: int
    kind: str                     # import | derive | parameter_revise |
                                  # source_mutate | decision | conflict |
                                  # checkout | candidate_accept ...
    actor: str
    summary: str
    basis: list[str]
    payload: dict
    accepted: bool
    parent_event_id: Optional[str]
    rule_version: str
    timestamp: str

    def to_dict(self) -> dict:
        return asdict(self)


TRACK_TYPES = {1: "video", 2: "audio", 17: "subtitles", 3: "complex"}
