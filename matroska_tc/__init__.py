"""Matroska/EBML multi-track timecode review toolchain.

Domain quantities always carry their unit or coordinate base. Raw
representations survive import; the derivation layer is the only place
normalization happens.
"""

__version__ = "1.0.0"
SCHEMA_VERSION = 2
ARCHIVE_KIND = "matroska-timecode-court"
