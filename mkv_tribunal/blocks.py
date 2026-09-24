"""Block / SimpleBlock header and lacing decoding.

Semantic invariants enforced by the rule layer are deliberately *not* applied
here; this module only reports what the bytes say, including corruption.  In
particular, when lacing metadata is inconsistent the frame boundaries are
returned as ``None`` with an error: callers must never guess frame splits.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import ebml

LACING_NONE = 0
LACING_XIPH = 1
LACING_FIXED = 2
LACING_EBML = 3
LACING_NAMES = {0: "none", 1: "xiph", 2: "fixed", 3: "ebml"}


@dataclass
class BlockParse:
    track_number: int
    rel_tc_signed: int          # always the signed-width interpretation
    rel_tc_raw_hex: str
    rel_tc_bytes: int
    flags: int
    lacing: int
    keyframe: bool | None       # None for Block (no keyframe bit defined)
    discardable: bool | None
    header_len: int
    frame_count: int
    frames: list[tuple[int, int]] | None  # (offset, size) within payload; None if corrupt
    errors: list[str] = field(default_factory=list)
    payload_start: int = 0


def _read_track_vint(data: bytes, pos: int) -> tuple[int, int]:
    val, length, _ = ebml.read_vint(data, pos)
    return val, length


def parse_block(payload: bytes) -> BlockParse:
    errors: list[str] = []
    try:
        track, tn_len = _read_track_vint(payload, 0)
    except ebml.EbmlError as exc:
        return BlockParse(0, 0, "", 0, 0, LACING_NONE, None, None, 0, 0, None,
                          [f"track vint: {exc}"], 0)
    p = tn_len
    if p + 2 > len(payload):
        return BlockParse(track, 0, "", 0, 0, LACING_NONE, None, None, tn_len, 0,
                          None, ["truncated relative timecode (need 2 bytes)"], p)
    tc_raw = payload[p:p + 2]
    rel_tc = ebml.read_int_signed(tc_raw)
    p += 2
    if p >= len(payload):
        return BlockParse(track, rel_tc, tc_raw.hex(), 2, 0, LACING_NONE, None,
                          None, p, 0, None, ["truncated flags byte"], p)
    flags = payload[p]
    p += 1
    lacing = (flags >> 1) & 0x03
    keyframe = bool(flags & 0x80)
    discardable = bool(flags & 0x01)

    bp = BlockParse(track, rel_tc, tc_raw.hex(), 2, flags, lacing, keyframe,
                    discardable, p, 0, None, [], payload_start=p)

    frame_data = payload[p:]
    if lacing == LACING_NONE:
        bp.frame_count = 1
        bp.frames = [(p, len(frame_data))]
        return bp

    if not frame_data:
        bp.errors.append("lacing flag set but no frame data present")
        bp.frame_count = 0
        return bp
    count = frame_data[0] + 1
    bp.frame_count = count
    sizes, off, lerr = _lacing_sizes(frame_data, count, lacing)
    bp.errors.extend(lerr)
    if lerr:
        # Do NOT guess frame boundaries on corrupted lacing.
        bp.frames = None
        return bp
    frames: list[tuple[int, int]] = []
    cur = p + off
    for sz in sizes:
        frames.append((cur, sz))
        cur += sz
    if cur != len(payload):
        bp.errors.append(
            f"lacing sizes sum to {sum(sizes)} bytes but frame payload is "
            f"{len(payload) - p - off} bytes"
        )
        bp.frames = None
        return bp
    bp.frames = frames
    return bp


def _lacing_sizes(data: bytes, count: int, lacing: int):
    # ``data`` starts at the lacing header byte.
    sizes: list[int] = []
    if lacing == LACING_XIPH:
        pos = 1
        for i in range(count - 1):
            total = 0
            while True:
                if pos >= len(data):
                    return sizes, pos, ["xiph lacing size truncated"]
                b = data[pos]
                pos += 1
                total += b
                if b != 255:
                    break
            sizes.append(total)
        used = sum(sizes)
        last = len(data) - pos - used
        if last < 0:
            return sizes, pos, ["xiph lacing sizes already exceed payload"]
        sizes.append(last)
        return sizes, pos, []

    if lacing == LACING_FIXED:
        body = len(data) - 1
        if body % count:
            return [], 1, [
                f"fixed lacing: {body} payload bytes not divisible into "
                f"{count} equal frames"
            ]
        each = body // count
        return [each] * count, 1, []

    if lacing == LACING_EBML:
        pos = 1
        try:
            first, head_len, _ = ebml.read_vint(data, pos)
        except ebml.EbmlError as exc:
            return [], pos, [f"ebml lacing first size: {exc}"]
        sizes.append(first)
        pos += head_len
        width = head_len
        for _i in range(count - 2):
            if pos + width > len(data):
                return sizes, pos, ["ebml lacing difference truncated"]
            raw = int.from_bytes(data[pos:pos + width], "big")
            diff = _signed_lacing_value(raw, width)
            sizes.append(sizes[-1] + diff)
            if sizes[-1] < 0:
                return sizes, pos, ["ebml lacing produced negative frame size"]
            pos += width
        last = len(data) - pos - sum(sizes)
        if last < 0:
            return sizes, pos, ["ebml lacing sizes exceed payload"]
        sizes.append(last)
        return sizes, pos, []

    return [], 1, [f"unsupported lacing type {lacing}"]


def _signed_lacing_value(raw: int, width: int) -> int:
    # Strip the VINT marker bit conceptually: 7 bits per byte, top bit is sign.
    # The first size was a normal VINT; subsequent diffs are signed integers in
    # the same byte width, per Matroska lacing rules.
    nbits = 8 * width
    if raw & (1 << (nbits - 1)):
        raw -= 1 << nbits
    return raw
