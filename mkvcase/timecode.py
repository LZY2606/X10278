"""Block/lacing/timecode semantics (source-faithful, no guessing).

- Block relative timecodes are signed 16-bit big-endian integers.
- When lacing data is damaged, frame boundaries are reported as unknown;
  the parser never invents frame splits.
- DiscardPadding is a signed nanosecond value: positive trims the end,
  negative trims the beginning of the block duration window.
"""
from __future__ import annotations

from dataclasses import dataclass, field

LACING_NAMES = {0: "none", 1: "xiph", 2: "fixed", 3: "ebml"}


@dataclass
class FrameSlice:
    index: int
    data_offset: int
    data_size: int


@dataclass
class BlockParse:
    track_number: int
    rel_timecode: int
    rel_timecode_raw: str
    flags: int
    lacing: str
    frames: list
    damaged: bool
    diagnostics: list = field(default_factory=list)
    header_end: int = 0


def _read_vint_value(data, pos):
    if pos >= len(data):
        raise ValueError("vint past end of block data")
    first = data[pos]
    if first == 0:
        raise ValueError("invalid vint (0x00) in block data")
    length = 1
    mask = 0x80
    while not (first & mask):
        mask >>= 1
        length += 1
        if length > 8:
            raise ValueError("vint longer than 8 bytes in block data")
    if pos + length > len(data):
        raise ValueError("truncated vint in block data")
    raw = int.from_bytes(data[pos:pos + length], "big")
    return raw & ((1 << (7 * length)) - 1), length


def _read_signed_vint(data, pos):
    value, length = _read_vint_value(data, pos)
    bias = (1 << (7 * length - 1)) - 1
    return value - bias, length


def parse_block(data, base_offset=0):
    """Parse a Block/SimpleBlock payload; base_offset is the absolute file
    offset of the payload so diagnostics cite real byte ranges."""
    diagnostics = []
    if len(data) < 4:
        return BlockParse(0, 0, "0x0000", 0, "none", None, True,
                          [{"offset": base_offset, "length": len(data),
                            "reason": "block payload shorter than 4-byte minimum"}])
    try:
        track_number, track_len = _read_vint_value(data, 0)
    except ValueError as exc:
        return BlockParse(0, 0, "0x0000", 0, "none", None, True,
                          [{"offset": base_offset, "length": 1, "reason": str(exc)}])
    rel_raw = data[track_len:track_len + 2]
    rel_timecode = int.from_bytes(rel_raw, "big", signed=True)
    flags = data[track_len + 2]
    lacing = LACING_NAMES[(flags >> 1) & 0x03]
    pos = track_len + 3

    frames = []
    damaged = False

    if lacing == "none":
        frames = [FrameSlice(0, base_offset + pos, len(data) - pos)]
    else:
        if pos >= len(data):
            frames = None
            damaged = True
            diagnostics.append({
                "offset": base_offset + pos, "length": 0,
                "reason": lacing + " lacing declared but frame-count byte missing",
            })
        else:
            frame_count = data[pos] + 1
            count_pos = pos
            pos += 1
            sizes = []
            ok = True
            if lacing == "xiph":
                for _ in range(frame_count - 1):
                    size = 0
                    while True:
                        if pos >= len(data):
                            ok = False
                            break
                        byte = data[pos]
                        pos += 1
                        size += byte
                        if byte < 255:
                            break
                    if not ok:
                        break
                    sizes.append(size)
                if not ok:
                    damaged = True
                    diagnostics.append({
                        "offset": base_offset + count_pos, "length": pos - count_pos,
                        "reason": "xiph lace size table truncated",
                    })
            elif lacing == "fixed":
                payload = len(data) - pos
                if payload < 0 or payload % frame_count != 0:
                    ok = False
                    damaged = True
                    diagnostics.append({
                        "offset": base_offset + count_pos, "length": 1,
                        "reason": ("fixed lacing payload %d bytes not divisible into %d frames"
                                   % (payload, frame_count)),
                    })
                else:
                    sizes = [payload // frame_count] * (frame_count - 1)
            elif lacing == "ebml":
                try:
                    first_size, used = _read_vint_value(data, pos)
                    pos += used
                    sizes.append(first_size)
                    previous = first_size
                    for _ in range(frame_count - 2):
                        diff, used = _read_signed_vint(data, pos)
                        pos += used
                        previous = previous + diff
                        if previous < 0:
                            raise ValueError("ebml lace size went negative")
                        sizes.append(previous)
                except ValueError as exc:
                    ok = False
                    damaged = True
                    diagnostics.append({
                        "offset": base_offset + count_pos, "length": max(1, pos - count_pos),
                        "reason": "ebml lace size table damaged: " + str(exc),
                    })
            if ok:
                declared = sum(sizes)
                remaining = len(data) - pos
                if declared > remaining:
                    damaged = True
                    diagnostics.append({
                        "offset": base_offset + count_pos,
                        "length": pos - count_pos,
                        "reason": ("%s lace sizes declare %d bytes but only %d payload bytes "
                                   "remain; frame boundaries unknown"
                                   % (lacing, declared, remaining)),
                    })
                    frames = None
                else:
                    sizes.append(remaining - declared)
                    frames = []
                    cursor = pos
                    for index, size in enumerate(sizes):
                        frames.append(FrameSlice(index, base_offset + cursor, size))
                        cursor += size
            else:
                frames = None
    return BlockParse(
        track_number=track_number,
        rel_timecode=rel_timecode,
        rel_timecode_raw="0x" + rel_raw.hex().upper(),
        flags=flags,
        lacing=lacing,
        frames=frames,
        damaged=damaged,
        diagnostics=diagnostics,
        header_end=pos,
    )


def apply_discard_padding(start_ns, duration_ns, padding_ns):
    """Return the kept (start, end) window after DiscardPadding.

    Positive padding discards from the end; negative discards from the start.
    """
    end_ns = start_ns + duration_ns
    if padding_ns >= 0:
        return start_ns, max(start_ns, end_ns - padding_ns)
    return min(end_ns, start_ns - padding_ns), end_ns


def ticks_to_ns(ticks, timecode_scale_ns, track_scale=1.0):
    """Normalize raw ticks to nanoseconds (derived layer only)."""
    return ticks * timecode_scale_ns * track_scale
