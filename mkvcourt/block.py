"""Block / SimpleBlock structural inspection.

Hard rules enforced here (see README):

* the two-octet relative timecode is interpreted **signed** at its fixed
  16-bit width;
* when lacing metadata is corrupt the parser reports the declared frame count
  and the bytes it could account for, but it **never guesses** frame
  boundaries (``frames`` stays ``None``);
* discard padding and flags are preserved verbatim.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .ebml import read_vint, EbmlError
from .units import Raw, s16, s64

NO_LACING = 0
XIPH_LACING = 1
FIXED_LACING = 2
EBML_LACING = 3

KEYFRAME_BIT = 0x80
LACING_MASK = 0x06
LACING_SHIFT = 1


@dataclass
class FrameRef:
    index: int
    start: int           # offset within block payload
    end: int


@dataclass
class BlockParse:
    track: int
    timecode: Raw                 # signed 16-bit relative timecode, ticks
    flags: int
    lacing: int
    declared_frames: int
    frames: Optional[list[FrameRef]] = None
    keyframe: bool = False
    diagnostics: tuple[str, ...] = ()
    track_vint_width: int = 0
    data_offset: int = 0          # first frame byte offset within payload
    intact: bool = True

    @property
    def rel_ticks(self) -> int:
        return self.timecode.value


def _xiph_sizes(data: bytes, start: int, frame_count: int):
    sizes: list[int] = []
    pos = start
    for i in range(frame_count - 1):
        total = 0
        while True:
            if pos >= len(data):
                return None, pos, f"xiph lacing truncated while reading size of frame {i}"
            octet = data[pos]
            pos += 1
            total += octet
            if octet != 255:
                break
        sizes.append(total)
    return sizes, pos, None


def _ebml_lace_sizes(data: bytes, start: int, frame_count: int):
    sizes: list[int] = []
    pos = start
    for i in range(frame_count - 1):
        try:
            value, width, _raw, unknown = read_vint(data, pos)
        except EbmlError as exc:
            return None, pos, f"ebml lacing size truncated for frame {i}: {exc}"
        if unknown:
            return None, pos, f"ebml lacing size unknown for frame {i}"
        pos += width
        if i == 0:
            sizes.append(value)
        else:
            sizes.append(sizes[-1] + (s64(value >> 0)))  # deltas encoded without marker
    return sizes, pos, None


def parse_block(payload: bytes, block_kind: str) -> BlockParse:
    bp = BlockParse(
        track=0,
        timecode=Raw(0, "relative_timecode", "ticks", signed_width_bits=16),
        flags=0, lacing=NO_LACING, declared_frames=1,
    )
    try:
        track, tvw, _raw, track_unknown = read_vint(payload, 0)
    except EbmlError as exc:
        bp.diagnostics = (f"track VINT unreadable: {exc}",)
        bp.intact = False
        return bp
    bp.track = track
    bp.track_vint_width = tvw
    if track_unknown:
        bp.diagnostics = ("track number uses unknown-length VINT",)
    if len(payload) < tvw + 3:
        bp.diagnostics = bp.diagnostics + ("block header truncated",)
        bp.intact = False
        return bp
    word = int.from_bytes(payload[tvw:tvw + 2], "big")
    bp.timecode = Raw(s16(word), "relative_timecode", TICKS := "ticks",
                      signed_width_bits=16, note="signed 16-bit block timecode")
    flags = payload[tvw + 2]
    bp.flags = flags
    bp.keyframe = bool(flags & KEYFRAME_BIT) if block_kind == "SimpleBlock" else False
    bp.lacing = (flags & LACING_MASK) >> LACING_SHIFT
    bp.data_offset = tvw + 3

    if bp.lacing == NO_LACING or len(payload) == bp.data_offset:
        bp.declared_frames = 1
        if len(payload) > bp.data_offset:
            bp.frames = [FrameRef(0, bp.data_offset, len(payload))]
        return bp

    if bp.data_offset >= len(payload):
        bp.diagnostics = bp.diagnostics + ("lacing declared but no frame-count octet",)
        bp.intact = False
        return bp

    lace_head = payload[bp.data_offset]
    frame_count = lace_head + 1
    bp.declared_frames = frame_count
    size_start = bp.data_offset + 1

    diags: list[str] = []
    frames: Optional[list[FrameRef]] = None
    data = payload

    if bp.lacing == XIPH_LACING:
        sizes, pos, err = _xiph_sizes(data, size_start, frame_count)
        if err:
            diags.append(err)
            bp.intact = False
        else:
            frames, err2 = _materialize(data, pos, sizes, frame_count)
            if err2:
                diags.append(err2)
                bp.intact = False
    elif bp.lacing == FIXED_LACING:
        remaining = len(data) - size_start
        if frame_count == 0 or remaining % frame_count != 0:
            diags.append(
                f"fixed lacing: {remaining} bytes not divisible into {frame_count} frames")
            bp.intact = False
        else:
            each = remaining // frame_count
            frames = [FrameRef(i, size_start + i * each, size_start + (i + 1) * each)
                      for i in range(frame_count)]
    elif bp.lacing == EBML_LACING:
        sizes, pos, err = _ebml_lace_sizes(data, size_start, frame_count)
        if err:
            diags.append(err)
            bp.intact = False
        else:
            frames, err2 = _materialize(data, pos, sizes, frame_count)
            if err2:
                diags.append(err2)
                bp.intact = False
    else:
        diags.append(f"reserved lacing type {bp.lacing}")
        bp.intact = False

    # Core rule: do not invent boundaries on corruption.
    bp.frames = None if not bp.intact else frames
    bp.diagnostics = bp.diagnostics + tuple(diags)
    return bp


def _materialize(data: bytes, pos: int, sizes: list[int], frame_count: int):
    if len(sizes) != frame_count - 1:
        return None, "lacing size count mismatch"
    frames: list[FrameRef] = []
    cur = pos
    for i, size in enumerate(sizes):
        end = cur + size
        if end > len(data):
            return None, (f"frame {i} lacing range [{cur},{end}) exceeds block "
                          f"payload length {len(data)}")
        frames.append(FrameRef(i, cur, end))
        cur = end
    last_size = len(data) - cur
    if last_size < 0:
        return None, "lacing sizes overrun payload; last frame has negative size"
    frames.append(FrameRef(frame_count - 1, cur, len(data)))
    return frames, None
