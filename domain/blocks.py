"""Block structure, signed relative timecodes and lacing decoding.

Hard rules encoded here:
* relative timecode is read from a fixed-width signed field (Matroska: int16);
  overflow/underflow is reported, never folded silently.
* when lacing metadata is corrupt, no frame boundaries are guessed: the whole
  laced payload is retained as a single untrusted span with a diagnostic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .ebml import read_vint, EbmlError


@dataclass
class FrameSpan:
    index: int
    start: int          # offset relative to block payload start
    end: int
    trusted: bool = True
    raw_range: tuple[int, int] | None = None


@dataclass
class BlockInfo:
    track_raw: int
    track_value: int
    tc_uint: int
    tc_signed: int
    tc_width_bits: int
    tc_overflow: bool
    flags: int
    lacing: int
    keyframe: bool
    frames: list[FrameSpan] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)
    header_len: int = 0
    corrupt: bool = False


def _interpret_signed_width(value: int, width_bits: int) -> tuple[int, bool]:
    """Interpret an unsigned field value with a fixed signed bit width.

    Returns (signed_value, overflow_flag). Overflow means the raw field sat at
    an extreme that a reviewer should inspect (kept verbatim either way).
    """
    sign_bit = 1 << (width_bits - 1)
    signed = value - (1 << width_bits) if value & sign_bit else value
    overflow = value in ((1 << width_bits) - 1, (1 << (width_bits - 1)) - 1)
    return signed, overflow


def decode_block(payload: bytes, base: int = 0) -> BlockInfo:
    """Decode a Block / SimpleBlock payload.

    `base` is the absolute file offset of the payload; frame ranges are returned
    relative to payload start (caller adds base for absolute ranges).
    """
    pos = 0
    tv = read_vint(payload, pos, is_id=False)
    track = tv.raw & ((1 << (tv.length * 8 - tv.length)) - 1)
    pos += tv.length
    if pos + 2 > len(payload):
        return BlockInfo(tv.raw, track, 0, 0, 16, False, 0, 0, False,
                         ["truncated block: missing timecode"], pos, True)
    tc_uint = int.from_bytes(payload[pos:pos + 2], "big")
    tc_signed, overflow = _interpret_signed_width(tc_uint, 16)
    pos += 2
    flags = payload[pos]
    pos += 1
    lacing = (flags >> 1) & 0b11
    keyframe = bool(flags & 0x80)
    info = BlockInfo(
        track_raw=tv.raw, track_value=track, tc_uint=tc_uint,
        tc_signed=tc_signed, tc_width_bits=16, tc_overflow=overflow,
        flags=flags, lacing=lacing, keyframe=keyframe, header_len=pos,
    )
    data_start = pos
    if lacing == 0:
        info.frames.append(FrameSpan(0, data_start, len(payload), True,
                                    (base + data_start, base + len(payload))))
        return info
    _decode_lacing(payload, data_start, lacing, info, base)
    return info


def _decode_lacing(buf: bytes, start: int, kind: int, info: BlockInfo,
                   base: int) -> None:
    try:
        nv = read_vint(buf, start, is_id=False)
        count = nv.value + 1
        sizes: list[int] = []
        p = start + nv.length
        if kind == 1:  # Xiph
            for i in range(count - 1):
                total = 0
                while True:
                    if p >= len(buf):
                        raise EbmlError("xiph size chain truncated")
                    byte = buf[p]
                    p += 1
                    total += byte
                    if byte != 0xFF:
                        break
                sizes.append(total)
        elif kind == 2:  # fixed
            remaining = len(buf) - p
            if count == 0 or remaining % count != 0:
                raise EbmlError("fixed lacing payload not divisible by count")
            each = remaining // count
            sizes = [each] * (count - 1)
        elif kind == 3:  # EBML signed deltas
            first = read_vint(buf, p, is_id=False)
            p += first.length
            prev = first.value
            sizes.append(prev)
            for _ in range(count - 2):
                sv = read_vint(buf, p, is_id=False)
                p += sv.length
                w = sv.length * 8 - sv.length
                delta, _ = _interpret_signed_width(sv.value, w)
                prev = prev + delta
                if prev < 0:
                    raise EbmlError("ebml lacing produced negative frame size")
                sizes.append(prev)
        else:
            raise EbmlError(f"unknown lacing kind {kind}")
    except (EbmlError, IndexError, ValueError) as exc:
        info.corrupt = True
        info.diagnostics.append(f"lacing metadata corrupt: {exc}; frame "
                                "boundaries not guessed")
        info.frames.append(FrameSpan(0, start, len(buf), False,
                                    (base + start, base + len(buf))))
        return

    # Validate computed frame slices exactly consume the payload.
    spans = []
    cursor = p
    for i, size in enumerate(sizes):
        end = cursor + size
        if end > len(buf):
            info.corrupt = True
            info.diagnostics.append(
                f"laced frame {i} overruns payload ({end}>{len(buf)}); "
                "boundaries not trusted")
            info.frames.append(FrameSpan(0, start, len(buf), False,
                                         (base + start, base + len(buf))))
            return
        spans.append(FrameSpan(i, cursor, end, True,
                               (base + cursor, base + end)))
        cursor = end
    last = FrameSpan(len(sizes), cursor, len(buf), True,
                     (base + cursor, base + len(buf)))
    spans.append(last)
    info.frames = spans
