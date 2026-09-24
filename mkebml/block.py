"""Matroska Block structure and lacing decoder.

Rules enforced here (rule set ``rules:v1``, see ``mkebml.judgments``):

* Relative timecode is interpreted as a **signed**, fixed-width 16-bit
  integer (relative to the cluster timecode).
* When lacing is corrupt the parser reports it and refuses to guess frame
  boundaries: no frame ranges are fabricated for display.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .units import Diagnostic, signed_of
from .vint import read_vint, VintError, UNKNOWN_LENGTH

LACING_NONE = 0
LACING_XIPH = 1
LACING_FIXED = 2
LACING_EBML = 3

LACING_NAMES = {0: "none", 1: "xiph", 2: "fixed", 3: "ebml"}


@dataclass
class BlockParse:
    track_number: int
    rel_timecode: int                  # signed int16, cluster ticks
    rel_width_bits: int
    flags: int
    keyframe: bool
    lacing: int
    header_size: int
    frame_count: int
    frames: list[dict[str, int]]       # [{start,end}] offsets in payload
    corrupt: bool
    diagnostics: list[Diagnostic] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "track_number": self.track_number,
            "relative_timecode": {
                "value": self.rel_timecode,
                "width_bits": self.rel_width_bits,
                "signed": True,
                "unit": "tick",
                "basis": "cluster-timecode",
            },
            "flags": self.flags,
            "keyframe": self.keyframe,
            "lacing": LACING_NAMES[self.lacing],
            "header_size": self.header_size,
            "frame_count": self.frame_count,
            "frames": self.frames,
            "corrupt": self.corrupt,
            "diagnostics": [d.to_json() for d in self.diagnostics],
        }


def _ebml_lace_sizes(data: bytes, pos: int, count: int, end: int,
                     diag: list[Diagnostic]) -> tuple[list[int], int] | None:
    """Decode EBML lacing sizes. Return (sizes, new_pos) or None if corrupt."""
    sizes: list[int] = []
    try:
        first, width, _ = read_vint(data, pos, allow_all_ones=False)
    except VintError as exc:
        diag.append(Diagnostic("LACING_EBML_FIRST",
                               f"EBML lace first size unreadable: {exc}",
                               "error"))
        return None
    pos += width
    sizes.append(first)
    for _ in range(count - 2):
        try:
            raw, width, _ = read_vint(data, pos, allow_all_ones=False)
        except VintError as exc:
            diag.append(Diagnostic("LACING_EBML_DELTA",
                                   f"EBML lace delta unreadable: {exc}",
                                   "error"))
            return None
        pos += width
        center = (1 << (7 * width)) // 2 - 1
        delta = raw - center
        nxt = sizes[-1] + delta
        if nxt < 0:
            diag.append(Diagnostic("LACING_EBML_NEGATIVE",
                                   "EBML lace delta produced negative size",
                                   "error",
                                   evidence={"raw": raw, "width": width,
                                             "previous": sizes[-1]}))
            return None
        sizes.append(nxt)
    if len(sizes) != count - 1:
        diag.append(Diagnostic("LACING_EBML_TRUNCATED",
                               "not enough EBML lace size values", "error"))
        return None
    consumed_by_sizes = sum(sizes)
    last = (end - pos) - consumed_by_sizes
    if last < 0:
        diag.append(Diagnostic("LACING_EBML_OVERFLOW",
                               "EBML lace sizes exceed lace payload", "error",
                               evidence={"declared": consumed_by_sizes,
                                         "available": end - pos}))
        return None
    sizes.append(last)
    return sizes, pos


def _xiph_lace_sizes(data: bytes, pos: int, count: int, end: int,
                     diag: list[Diagnostic]) -> tuple[list[int], int] | None:
    sizes: list[int] = []
    start = pos
    for idx in range(count - 1):
        total = 0
        while True:
            if pos >= end:
                diag.append(Diagnostic("LACING_XIPH_TRUNCATED",
                                       "Xiph lace size sequence runs past "
                                       "payload", "error"))
                return None
            byte = data[pos]
            pos += 1
            total += byte
            if byte != 255:
                break
        sizes.append(total)
    last = (end - start) - (pos - start) - sum(sizes)
    # Recompute available tail correctly:
    last = (end - pos) - sum(sizes)
    if last < 0:
        diag.append(Diagnostic("LACING_XIPH_OVERFLOW",
                               "Xiph lace sizes exceed payload", "error",
                               evidence={"available": end - pos,
                                         "declared": sum(sizes)}))
        return None
    sizes.append(last)
    return sizes, pos


def parse_block(data: bytes) -> BlockParse:
    diag: list[Diagnostic] = []
    try:
        track_number, tn_width, _ = read_vint(data, 0, allow_all_ones=False)
    except VintError as exc:
        diag.append(Diagnostic("BLOCK_TRACK_TRUNCATED",
                               f"block track number unreadable: {exc}",
                               "error"))
        return BlockParse(0, 0, 0, 0, False, LACING_NONE, 0, 0, [], True, diag)
    rel_pos = tn_width
    if rel_pos + 2 > len(data):
        diag.append(Diagnostic("BLOCK_TIMECODE_TRUNCATED",
                               "block lacks a 16-bit relative timecode",
                               "error"))
        return BlockParse(track_number, 0, 16, 0, False, LACING_NONE,
                          rel_pos + 2, 0, [], True, diag)
    rel = signed_of(data[rel_pos:rel_pos + 2])
    flags = data[rel_pos + 2]
    header_size = rel_pos + 3
    lacing = (flags >> 1) & 0b11
    keyframe = bool(flags & 0x80)

    result = BlockParse(track_number, rel, 16, flags, keyframe, lacing,
                        header_size, 1, [], bool(diag), diag)
    body = data[header_size:]
    body_end = len(data)

    if lacing == LACING_NONE:
        result.frames = [{"start": header_size, "end": body_end}]
        result.frame_count = 1
        return result

    if not body:
        diag.append(Diagnostic("LACING_HEADER_MISSING",
                               "laced block has no frames-count byte", "error"))
        result.corrupt = True
        return result
    count = body[0] + 1
    pos = header_size + 1
    result.frame_count = count

    if lacing == LACING_FIXED:
        total = body_end - pos
        if count == 0 or total % count != 0:
            diag.append(Diagnostic(
                "LACING_FIXED_UNEVEN",
                "fixed-size lace payload is not divisible by frame count; "
                "frame boundaries are not unique", "error",
                evidence={"payload_bytes": total, "frames": count}))
            result.corrupt = True
            return result
        step = total // count
        sizes = [step] * count
    elif lacing == LACING_XIPH:
        decoded = _xiph_lace_sizes(data, pos, count, body_end, diag)
        if decoded is None:
            result.corrupt = True
            return result
        sizes, pos = decoded
    elif lacing == LACING_EBML:
        decoded = _ebml_lace_sizes(data, pos, count, body_end, diag)
        if decoded is None:
            result.corrupt = True
            return result
        sizes, pos = decoded
    else:  # pragma: no cover - 2-bit field cannot reach here
        diag.append(Diagnostic("LACING_UNKNOWN",
                               f"unknown lacing type {lacing}", "error"))
        result.corrupt = True
        return result

    frames: list[dict[str, int]] = []
    cursor = pos
    for size in sizes:
        frames.append({"start": cursor, "end": cursor + size})
        cursor += size
    if cursor != body_end:
        diag.append(Diagnostic("LACING_SIZE_MISMATCH",
                               "lace frame sizes do not exactly exhaust the "
                               "block payload", "error",
                               evidence={"consumed": cursor - pos,
                                         "available": body_end - pos}))
        result.corrupt = True
        # Even if exact sizes disagreed, do not fabricate frames beyond data.
        result.frames = []
        result.frame_count = count
        return result
    result.frames = frames
    return result
