"""Matroska/EBML multi-track timecode forensics package.

Layers:
  ebml    - raw EBML/VINT decoding (offsets, unknown lengths, byte ranges)
  tags    - Matroska element id registry
  models  - fact / derived / event dataclasses with units and provenance
  matroska- domain extraction (clusters, blocks, lacing, cues, timecodes)
  engine  - dependency graph derivation, candidates, decisions
  storage - versioned SQLite store with atomic pointer switching
  archive - counterexample archive (baseline + single-source mutations)
  cases   - offline domain case pack (synthetic Matroska fixtures)
"""

RULESET_VERSION = "rules-2026.09.0"
SCHEMA_VERSION = 3
