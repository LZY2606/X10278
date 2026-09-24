"""Counter-example archive for the Matroska timecode court.

For every direct-source mutant the archive stores:
* the baseline evidence (EBML offsets, unknown lengths, track parameters,
  cluster/block timecodes, discard padding, cues, raw byte ranges);
* the single direct source that changed;
* expected blast radius (which dependency nodes/objects are affected);
* adopted and rejected results, plus machine-readable judgement events.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable, Optional

MUTANTS = [
    "unknown-length",
    "relative-timecode-overflow",
    "lacing-corruption",
    "discard-padding",
    "cue-mispoint",
    "track-timebase-difference",
    "mid-cluster-gap",
]

JUDGEMENTS = [
    {"id": "J-signed-relative",
     "rule": "rule:relative-timecode-signed-16bit@1",
     "statement": "relative timecode interpreted with signed fixed bit width",
     "applies_to": "relative-timecode-overflow"},
    {"id": "J-no-guess-lacing",
     "rule": "rule:lacing-corrupt-no-guess@1",
     "statement": "on corrupt lacing, do not guess frame boundaries",
     "applies_to": "lacing-corruption"},
    {"id": "J-cue-evidence",
     "rule": "rule:cue-is-index-evidence@1",
     "statement": "cue is index evidence, never the time truth",
     "applies_to": "cue-mispoint"},
    {"id": "J-independent-tracks",
     "rule": "rule:multi-track-independent@1",
     "statement": "multi-track timelines are not force-aligned by file order",
     "applies_to": "track-timebase-difference"},
]


@dataclass
class Variant:
    name: str
    changed_direct_source: str
    input_fragment: dict
    affected_objects: list[str]
    affected_nodes: list[str]
    blast_radius_limited: bool
    adopted: list[dict]
    rejected: list[dict]
    judgement_event: dict
    status: str = "recorded"

    def to_dict(self):
        return asdict(self)


@dataclass
class Archive:
    name: str
    baseline: dict
    variants: list[Variant] = field(default_factory=list)

    def to_dict(self):
        return {"name": self.name, "baseline": self.baseline,
                "judgements": JUDGEMENTS,
                "mutants": MUTANTS,
                "variants": [v.to_dict() for v in self.variants]}

    def save(self, path: str | Path):
        Path(path).write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "Archive":
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(name=d["name"], baseline=d["baseline"],
                   variants=[Variant(**v) for v in d["variants"]])
