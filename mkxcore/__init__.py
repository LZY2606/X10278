"""Matroska/EBML multi-track timecode review core.

All domain quantities carry an explicit unit/coordinate basis.  Raw bytes are
preserved at the import layer; normalisation only happens in the derivation
layer.
"""

RULESET_VERSION = "2026.09.0-r1"
