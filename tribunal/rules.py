"""Versioned domain rules.

Every adjudication / candidate recorded in an event references one of these
rule ids plus its version, so replaying history never silently adopts new
semantics.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Rule:
    rid: str
    version: int
    title: str
    statement: str


RULES: dict[str, Rule] = {}


def rule(rid: str, version: int, title: str, statement: str) -> Rule:
    r = Rule(rid, version, title, statement)
    RULES[rid] = r
    return r


R_SIGNED = rule(
    "R-SIGNED-16", 1,
    "Relative timecode is signed fixed-width",
    "Block relative timecodes are interpreted as signed big-endian integers "
    "of their declared width (normally 16-bit): a high-bit-set value wraps "
    "into the negative range instead of becoming a large positive overflow.")

R_LACING = rule(
    "R-LACING-NO-GUESS", 1,
    "Damaged lacing yields no frame boundaries",
    "When Xiph/EBML/fixed lacing metadata is inconsistent with the payload, "
    "frame boundaries are not guessed: the block stays one opaque payload and "
    "the damaged lacing byte range is reported.")

R_CUE = rule(
    "R-CUE-EVIDENCE", 1,
    "Cues are index evidence, not temporal truth",
    "CuePoint/CueTime is an index hint that must be cross-checked against the "
    "actual Cluster/Block byte range and cluster timecode; a contradicting cue "
    "is diagnosed, never used to rewrite frame times.")

R_MULTITRACK = rule(
    "R-MULTITRACK-NO-FILE-ORDER", 1,
    "Tracks keep independent timebases",
    "Per-track timelines are built from each track's own ticks/scale; tracks "
    "are never force-aligned by interleaved file order or by a shared frame "
    "index.")

R_UNIT = rule(
    "R-UNIT-RAW", 1,
    "Raw representation precedes normalization",
    "All quantities keep their original representation and unit; only the "
    "derived layer normalizes to nanoseconds/bytes, and unconvertible values "
    "stay at their origin with a diagnostic.")

R_UNKNOWN = rule(
    "R-UNKNOWN-LENGTH", 1,
    "Unknown-size elements delimit by siblings",
    "An EBML unknown-length master element is parsed using known sibling/parent "
    "boundaries; its size stays unknown, and only discovered bytes are "
    "claimed.")
