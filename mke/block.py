"""Matroska Block / SimpleBlock parsing and lace decoding.

The boundary rules enforced here are deliberate:

* the relative timecode is a signed integer of its exact on-disk width;
* a damaged lace table produces a structural error and never a guessed set
  of frame boundaries.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .ebml import apply_sign_width, decode_lace_signed

LACING_NONE = 0
LACING_XIPH = 1
LACING_EBML = 3
LACING_FIXED = 2


class LaceError(ValueError):
    """Raised when a lace table cannot be validated."""


@dataclass
class BlockInfo:
    track_number: int
    relative_ticks_raw: int
    relative_ticks_signed: int
    tc_field_width: int
    flags: int
    lacing: int
    keyframe: bool
    frame_count: int
    frame_ranges: list[tuple[int, int]] = field(default_factory=list)
    lace_status: str = "ok"
    lace_error: str | None = None
    payload_start: int = 0  # relative to block data start


def _read_track_vint(data: bytes) -> tuple[int, int]:
    first = data[0]
    width = 1
    bit = 0x80
    while not first & bit:
        bit >>= 1
        width += 1
        if width > 8 or width > len(data):
            raise LaceError("bad track number VINT")
    raw = int.from_bytes(data[:width], "big")
    value = raw & ((1 << (7 * width)) - 1)
    return value, width


def parse_block(data: bytes) -> BlockInfo:
    if len(data) < 4:
        raise LaceError("block shorter than 4 bytes")
    track, tw = _read_track_vint(data)
    tc_raw = int.from_bytes(data[tw:tw + 2], "big")
    tc_signed = apply_sign_width(tc_raw, 2)
    flags = data[tw + 2]
    lacing = (flags >> 1) & 0x3
    keyframe = bool(flags & 0x80)
    frames_minus_one = flags >> 4
    payload_start = tw + 3
    info = BlockInfo(
        track_number=track,
        relative_ticks_raw=tc_raw,
        relative_ticks_signed=tc_signed,
        tc_field_width=2,
        flags=flags,
        lacing=lacing,
        keyframe=keyframe,
        frame_count=max(1, frames_minus_one + 1) if lacing else 1,
        payload_start=payload_start,
    )
    if lacing == LACING_NONE:
        info.frame_count = 1
        info.frame_ranges = [(payload_start, len(data))]
        return info
    count = frames_minus_one + 1
    info.frame_count = count
    try:
        if lacing == LACING_XIPH:
            ranges, end = _decode_xiph(data, payload_start, count)
        elif lacing == LACING_FIXED:
            ranges, end = _decode_fixed(data, payload_start, count)
        elif lacing == LACING_EBML:
            ranges, end = _decode_ebml(data, payload_start, count)
        else:  # pragma: no cover - all 2-bit values enumerated
            raise LaceError(f"reserved lacing {lacing}")
    except LaceError as exc:
        info.lace_status = "corrupt"
        info.lace_error = str(exc)
        return info
    if end != len(data):
        info.lace_status = "corrupt"
        info.lace_error = (
            f"lace payload ends at {end} but block data ends at {len(data)}"
        )
        return info
    info.frame_ranges = ranges
    return info


def _decode_xiph(data: bytes, start: int, count: int):
    sizes: list[int] = []
    pos = start
    for _ in range(count - 1):
        total = 0
        while pos < len(data):
            byte = data[pos]
            pos += 1
            total += byte
            if byte != 255:
                break
        else:
            raise LaceError("xiph size table runs past block")
        sizes.append(total)
    ranges: list[tuple[int, int]] = []
    cur = pos
    used = sum(sizes)
    last = len(data) - pos - used
    if last < 0:
        raise LaceError("xiph frame sizes exceed block payload")
    all_sizes = sizes + [last]
    cur = pos
    for size in all_sizes:
        ranges.append((cur, cur + size))
        cur += size
    return ranges, cur


def _decode_fixed(data: bytes, start: int, count: int):
    payload_len = len(data) - start
    if payload_len % count:
        raise LaceError(
            f"fixed lace payload {payload_len} not divisible by {count} frames"
        )
    each = payload_len // count
    return [(start + i * each, start + (i + 1) * each) for i in range(count)], len(data)


def _read_ebml_vint_signed_payload(data: bytes, pos: int) -> tuple[int, int, int]:
    """Return (raw_unsigned_payload, signed_value, new_pos)."""
    if pos >= len(data):
        raise LaceError("ebml lace size VINT missing")
    first = data[pos]
    width = 1
    bit = 0x80
    while not first & bit:
        bit >>= 1
        width += 1
        if width > 8 or pos + width > len(data):
            raise LaceError("ebml lace VINT too wide")
    raw = int.from_bytes(data[pos:pos + width], "big")
    payload = raw & ((1 << (7 * width)) - 1)
    signed = decode_lace_signed(payload, bits=7 * width)
    return payload, signed, pos + width


def _decode_ebml(data: bytes, start: int, count: int):
    first_raw, _, pos = _read_ebml_vint_signed_payload(data, start)
    sizes = [first_raw]
    for _ in range(count - 2):
        _, delta, pos = _read_ebml_vint_signed_payload(data, pos)
        sizes.append(sizes[-1] + delta)
    used = sum(sizes)
    last = len(data) - pos - used
    if last < 0:
        raise LaceError("ebml lace frame sizes exceed block payload")
    all_sizes = sizes + [last]
    ranges = []
    cur = pos
    for size in all_sizes:
        ranges.append((cur, cur + size))
        cur += size
    return ranges, cur
