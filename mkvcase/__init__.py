"""Matroska timecode court: EBML structure, cluster/cue evidence, multi-track frame time.

Layers:
- source layer (ebml.py, builder.py): raw bytes, offsets, unknown sizes preserved.
- derived layer (timecode.py, derive.py): normalized values with provenance.
- ruling layer (store.py, service.py): versioned decisions, conflicts, replay.
"""

__version__ = "1.0.0"
