"""Block header and lacing decoding.

Semantics enforced here (domain rules, not heuristics):

* Relative timecode is interpreted with a *signed fixed bit width* (16-bit for
  Matroska Blocks). 0xFFFF is -1, 0x8000 is -32768. Overflow relative to the
  cluster timestamp is flagged as an anomaly, not wrapped away.
* When lacing is damaged, frame boundaries are **not guessed**. The whole
  lace is returned as one opaque payload with a structured diagnostic and the
  exact corrupt byte range.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np

LACING_NONE = 0
LACING_XIPH = 1
LACING_EBML = 3
LACING_FIXED = 2

LACING_NAME = {0: "none", 1: "xiph", 2: "fixed", 3: "ebml"}


@dataclass
class Frame:
    index: int
    data_pos: int
    size: int

    def to_dict(self) -> dict:
        return {"index": self.index, "data_pos": self.data_pos,
                "size": self.size, "unit": "byte"}


@dataclass
class LaceResult:
    track_number: int
    relative_ticks_raw: int
    relative_ticks_signed: int
    flags: int
    keyframe: bool
    lacing: int
    lacing_name: str
    frames: list[Frame]
    header_len: int
    payload_pos: int
    payload_end: int
    corrupt: bool
    diagnostic: Optional[str]
    corrupt_range: Optional[tuple[int, int]]

    def to_dict(self) -> dict:
        return {
            "track_number": self.track_number,
            "relative_ticks_raw": self.relative_ticks_raw,
            "relative_ticks_signed": self.relative_ticks_signed,
            "flags": self.flags,
            "keyframe": self.keyframe,
            "lacing": self.lacing,
            "lacing_name": self.lacing_name,
            "frames": [f.to_dict() for f in self.frames],
            "header_len": self.header_len,
            "payload_pos": self.payload_pos,
            "payload_end": self.payload_end,
            "corrupt": self.corrupt,
            "diagnostic": self.diagnostic,
            "corrupt_range": list(self.corrupt_range) if self.corrupt_range
            else None,
        }


def read_block_vint(data: bytes, pos: int) -> tuple[int, int, int, int]:
    """Read a Block-style VINT -> (value, marker_bit, start, length)."""
    first = data[pos]
    width = 0
    marker_bit = 0x80
    while marker_bit and not (first & marker_bit):
        width += 1
        marker_bit >>= 1
    width += 1
    val = first & (marker_bit - 1)
    for i in range(1, width):
        val = (val << 8) | data[pos + i]
    all_bits = (1 << (7 * width)) - 1
    return val, 1 if val == all_bits else 0, pos, width


def signed_relative_timecode(raw2: bytes) -> int:
    """Interpret the 2-byte relative timecode as a signed 16-bit value."""
    return int(np.frombuffer(raw2, dtype=">i2")[0])


def decode_block(data: bytes, pos: int, end: int) -> LaceResult:
    """Decode a Block/SimpleBlock payload spanning ``data[pos:end]``.

    ``pos`` points at the first byte of the Block *content* (after the EBML
    id/size header).
    """
    try:
        track_num, tn_marker, _, tn_len = read_block_vint(data, pos)
    except IndexError:
        return _fail(data, pos, end, "truncated block: track number",
                     (pos, min(end, pos + 1)))
    hdr = pos + tn_len
    if hdr + 3 > end:
        return _fail(data, pos, end,
                     "truncated block: relative timecode/flags",
                     (pos, end))
    raw_tc = int.from_bytes(data[hdr:hdr + 2], "big")
    rel = signed_relative_timecode(data[hdr:hdr + 2])
    flags = data[hdr + 2]
    hdr += 3
    lacing = (flags >> 1) & 0x3
    keyframe = bool(flags & 0x80)

    payload_end = end
    if lacing == LACING_NONE:
        frames = [Frame(0, hdr, end - hdr)] if end > hdr else []
        return LaceResult(track_num, raw_tc, rel, flags, keyframe, lacing,
                          LACING_NAME[lacing], frames, hdr - pos, hdr,
                          payload_end, False, None, None)
    if hdr >= end:
        return _fail(data, pos, end, "truncated block: missing lace header",
                     (hdr - 1, end))
    frame_count = data[hdr] + 1
    sizes_pos = hdr + 1
    if lacing == LACING_FIXED:
        body = end - sizes_pos
        if frame_count == 0 or body % frame_count != 0:
            return _fail(data, pos, end,
                         f"fixed lace: {body} payload bytes not divisible by "
                         f"{frame_count} frames",
                         (hdr, end))
        each = body // frame_count
        frames = [Frame(i, sizes_pos + i * each, each)
                  for i in range(frame_count)]
        return LaceResult(track_num, raw_tc, rel, flags, keyframe, lacing,
                          LACING_NAME[lacing], frames, hdr - pos + 1,
                          sizes_pos, payload_end, False, None, None)
    if lacing == LACING_XIPH:
        sizes: list[int] = []
        p = sizes_pos
        for i in range(frame_count - 1):
            total = 0
            start = p
            while True:
                if p >= end:
                    return _fail(data, pos, end,
                                 "xiph lace: size run overruns block",
                                 (start, end))
                b = data[p]
                p += 1
                total += b
                if b != 0xFF:
                    break
            sizes.append(total)
        consumed = p - sizes_pos
        used = sum(sizes)
        body = end - p
        if used > body:
            return _fail(data, pos, end,
                         f"xiph lace: declared frame bytes {used} exceed "
                         f"available {body}",
                         (sizes_pos, end))
        last = body - used
        all_sizes = sizes + [last]
        frames, fp = [], p
        for i, s in enumerate(all_sizes):
            frames.append(Frame(i, fp, s))
            fp += s
        _ = consumed
        return LaceResult(track_num, raw_tc, rel, flags, keyframe, lacing,
                          LACING_NAME[lacing], frames, sizes_pos - pos,
                          p, payload_end, False, None, None)
    if lacing == LACING_EBML:
        sizes = []
        p = sizes_pos
        for i in range(frame_count - 1):
            start = p
            if p >= end:
                return _fail(data, pos, end,
                             "ebml lace: size VINT overruns block",
                             (start, end))
            first = data[p]
            mb = 0x80
            w = 0
            while mb and not (first & mb):
                w += 1
                mb >>= 1
            if not mb:
                return _fail(data, pos, end,
                             "ebml lace: invalid VINT length descriptor",
                             (start, min(end, start + 8)))
            w += 1
            if p + w > end:
                return _fail(data, pos, end,
                             "ebml lace: truncated size VINT",
                             (start, end))
            val = first & (mb - 1)
            for j in range(1, w):
                val = (val << 8) | data[p + j]
            p += w
            all_bits = (1 << (7 * w)) - 1
            if val == all_bits:
                return _fail(data, pos, end,
                             "ebml lace: reserved all-ones VINT",
                             (start, p))
            if i == 0:
                first_size = val
                sizes.append(first_size)
            else:
                sign = val & (1 << (7 * w - 1))
                if sign:
                    val -= 1 << (7 * w)
                sizes.append(sizes[-1] + val)
        used = sum(sizes)
        body = end - p
        if used > body:
            return _fail(data, pos, end,
                         f"ebml lace: declared frame bytes {used} exceed "
                         f"available {body}",
                         (sizes_pos, end))
        all_sizes = sizes + [body - used]
        frames, fp = [], p
        for i, s in enumerate(all_sizes):
            frames.append(Frame(i, fp, s))
            fp += s
        return LaceResult(track_num, raw_tc, rel, flags, keyframe, lacing,
                          LACING_NAME[lacing], frames, sizes_pos - pos,
                          p, payload_end, False, None, None)
    return _fail(data, pos, end, f"unknown lacing type {lacing}",
                 (hdr - 1, hdr))


def _fail(data, pos, end, msg, crange) -> LaceResult:
    return LaceResult(
        track_number=-1, relative_ticks_raw=0, relative_ticks_signed=0,
        flags=0, keyframe=False, lacing=-1, lacing_name="corrupt",
        frames=[], header_len=0, payload_pos=pos, payload_end=end,
        corrupt=True, diagnostic=msg, corrupt_range=crange)
