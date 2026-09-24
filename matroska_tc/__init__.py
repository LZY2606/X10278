"""Matroska/EBML multi-track timecode review toolkit.

Layered design:
  ebml    - byte-exact EBML element parsing / building (no Matroska semantics)
  matroska - Matroska element catalogue and Block/BlockGroup semantics
  timecode - signed relative-timecode arithmetic
  rules   - domain rule versioning (lacing, cue, multi-track semantics)
  derive  - frame summaries, timelines, anomalies with stable identities
  graph   - dependency graph propagation / partial recompute
  events  - machine-readable decision/adjudication events
  storage - event-sourced, versioned SQLite store (atomic, concurrent)
  court   - narrative workflow (import -> challenge -> adjudicate -> review)
"""

RULESET_VERSION = "tc-court-2026.1"
