"""Matroska Block / SimpleBlock parsing.

Core semantics enforced here:

* the block relative timecode is a *signed* 16-bit big-endian integer;
* when lacing data is inconsistent the frame boundaries are NOT
  guessed -- the payload stays opaque and a diagnostic is attached.
"""
from __future__ import annotations

import struct

from .ebml import EbmlError, read_vint

LACE_NONE = 0
LACE_XIPH = 2
LACE_FIXED = 4
LACE_EBML = 6

LACE_NAMES = {LACE_NONE: "none", LACE_XIPH: "xiph",
              LACE_FIXED: "fixed", LACE_EBML: "ebml"}

INT16_MIN = -32768
INT16_MAX = 32767


def _read_signed_vint(data: bytes, pos: int):
    """EBML-lacing signed size: vint value minus the width bias."""
    value, length, _ = read_vint(data, pos)
    bias = (1 << (7 * length - 1)) - 1
    return value - bias, length


class Block:
    """Parsed block header plus (possibly zero) laced frame ranges."""

    def __init__(self, track_number, rel_timecode, flags, header_len,
                 payload_len, lacing_mode, frames, corrupted, diagnostics):
        self.track_number = track_number
        self.rel_timecode = rel_timecode      # signed int16, unit: timecode
        self.flags = flags
        self.header_len = header_len
        self.payload_len = payload_len
        self.lacing_mode = lacing_mode
        self.frames = frames                  # [(offset, size)] rel. to payload
        self.corrupted = corrupted
        self.diagnostics = diagnostics        # list of machine-readable dicts

    @property
    def keyframe(self) -> bool:
        return bool(self.flags & 0x80)

    @property
    def invisible(self) -> bool:
        return bool(self.flags & 0x08)

    @property
    def discardable(self) -> bool:
        return bool(self.flags & 0x01)

    def to_dict(self):
        return {
            "track_number": self.track_number,
            "rel_timecode": {"value": self.rel_timecode, "unit": "timecode",
                             "raw": self.rel_timecode, "interpretation": "int16-be-signed"},
            "flags": self.flags,
            "keyframe": self.keyframe,
            "lacing": LACE_NAMES[self.lacing_mode],
            "frames": [{"offset": o, "size": s, "unit": "bytes"} for o, s in self.frames],
            "corrupted": self.corrupted,
            "diagnostics": self.diagnostics,
        }


def parse_block(payload: bytes) -> Block:
    """Parse a Block/SimpleBlock payload.

    ``payload`` is the element data (track number vint first).  Raises
    EbmlError only when the fixed header itself is unreadable; lacing
    problems produce a corrupted Block instead.
    """
    diagnostics: list[dict] = []
    track, tlen, _ = read_vint(payload, 0)
    if tlen + 3 > len(payload):
        raise EbmlError("block header truncated")
    rel = struct.unpack(">h", payload[tlen:tlen + 2])[0]
    flags = payload[tlen + 2]
    lacing = flags & 0x06
    pos = tlen + 3
    frames: list[tuple[int, int]] = []
    corrupted = False

    if lacing == LACE_NONE:
        frames = [(pos, len(payload) - pos)]
    else:
        try:
            frames = _parse_lacing(payload, pos, lacing)
        except EbmlError as exc:
            corrupted = True
            diagnostics.append({
                "kind": "lacing_corrupt",
                "rule": "lacing_corrupt_no_frame_guess",
                "detail": f"{LACE_NAMES[lacing]} lacing inconsistent: {exc}; "
                          f"frame boundaries NOT guessed, payload kept opaque",
            })
            frames = []
    return Block(track, rel, flags, pos, len(payload), lacing,
                 frames, corrupted, diagnostics)


def _parse_lacing(payload: bytes, pos: int, lacing: int) -> list[tuple[int, int]]:
    if pos >= len(payload):
        raise EbmlError("missing lace count")
    count = payload[pos] + 1
    pos += 1
    sizes: list[int] = []
    if lacing == LACE_XIPH:
        for _ in range(count - 1):
            total = 0
            while True:
                if pos >= len(payload):
                    raise EbmlError("xiph lace size overruns header")
                b = payload[pos]
                pos += 1
                total += b
                if b < 255:
                    break
            sizes.append(total)
    elif lacing == LACE_EBML:
        first, ln = read_vint(payload, pos)
        pos += ln
        sizes.append(first)
        prev = first
        for _ in range(count - 2):
            diff, ln = _read_signed_vint(payload, pos)
            pos += ln
            prev = prev + diff
            if prev < 0:
                raise EbmlError("ebml lace size went negative")
            sizes.append(prev)
    elif lacing == LACE_FIXED:
        pass  # sizes derived below
    else:  # pragma: no cover - guarded by caller
        raise EbmlError(f"unknown lacing mode {lacing}")

    remaining = len(payload) - pos
    if lacing == LACE_FIXED:
        if count == 0 or remaining % count != 0:
            raise EbmlError(
                f"fixed lacing: {remaining} bytes not divisible by {count} frames")
        sizes = [remaining // count] * count
    else:
        used = sum(sizes)
        if used > remaining:
            raise EbmlError(
                f"declared lace sizes {used} exceed remaining {remaining} bytes")
        sizes.append(remaining - used)
    frames = []
    off = pos
    for size in sizes:
        frames.append((off, size))
        off += size
    return frames
