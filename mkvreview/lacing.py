"""Block lacing decoders.

Semantic rule (must never be violated): when lacing is damaged the decoder
does **not** invent frame boundaries.  Frames that can be proven from the
wire are returned with their exact byte ranges; everything after the damage
point stays one opaque ``undelimited`` span flagged ``recovered=False``.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class LaceFrame:
    index: int
    start: int  # relative to block payload (after track-number + 2 timecode + flags)
    end: int
    proven: bool

    def to_dict(self) -> dict:
        return {"index": self.index, "start": self.start, "end": self.end, "proven": self.proven}


@dataclass
class LaceResult:
    lacing: int  # 0 none, 1 Xiph, 3 EBML, 2 fixed-size
    frame_count: int | None  # None => not uniquely decidable
    frames: list[LaceFrame] = field(default_factory=list)
    damaged: bool = False
    damage_offset: int | None = None
    codes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "lacing": self.lacing,
            "frame_count": self.frame_count,
            "frames": [frame.to_dict() for frame in self.frames],
            "damaged": self.damaged,
            "damage_offset": self.damage_offset,
            "codes": list(self.codes),
        }


def decode_no_lacing(payload: bytes) -> LaceResult:
    # payload starts at the first frame byte (headers already stripped).
    return LaceResult(
        lacing=0,
        frame_count=1,
        frames=[LaceFrame(0, 0, len(payload), proven=True)],
    )


def decode_xiph(payload: bytes) -> LaceResult:
    if not payload:
        return LaceResult(1, None, [], True, None, ["xiph:missing-frame-count"])
    count = payload[0] + 1
    result = LaceResult(lacing=1, frame_count=count)
    sizes: list[int] = []
    pos = 1
    for index in range(count - 1):
        total = 0
        while True:
            if pos >= len(payload):
                result.damaged = True
                result.damage_offset = pos
                result.codes.append("xiph:size-field-truncated")
                return _emit_partial(result, payload, sizes, pos)
            byte = payload[pos]
            pos += 1
            total += byte
            if byte != 255:
                break
        sizes.append(total)
    frames_start = pos
    cursor = frames_start
    for index, size in enumerate(sizes):
        if cursor + size > len(payload):
            result.damaged = True
            result.damage_offset = cursor
            result.codes.append("xiph:frame-exceeds-payload")
            return _emit_partial(result, payload, sizes, cursor, proven_count=index)
        result.frames.append(LaceFrame(index, cursor, cursor + size, proven=True))
        cursor += size
    last_size = len(payload) - cursor
    if last_size < 0:
        result.damaged = True
        result.damage_offset = cursor
        result.codes.append("xiph:declared-sizes-exceed-payload")
        result.frame_count = None
        return result
    result.frames.append(LaceFrame(len(sizes), cursor, len(payload), proven=True))
    return result


def decode_fixed(payload: bytes, count_from_flag: int) -> LaceResult:
    count = count_from_flag
    result = LaceResult(lacing=2, frame_count=count)
    if count <= 0:
        result.damaged = True
        result.codes.append("fixed:bad-frame-count")
        result.frame_count = None
        return result
    if not payload or len(payload) % count != 0:
        result.damaged = True
        result.damage_offset = 0
        result.codes.append("fixed:payload-not-divisible")
        # Nothing is proven: we will not guess a boundary.
        result.frame_count = None
        return result
    frame_size = len(payload) // count
    for index in range(count):
        start = index * frame_size
        result.frames.append(LaceFrame(index, start, start + frame_size, proven=True))
    return result


def decode_ebml_lacing(payload: bytes) -> LaceResult:
    if not payload:
        return LaceResult(3, None, [], True, None, ["ebml:missing-frame-count"])
    count = payload[0] + 1
    result = LaceResult(lacing=3, frame_count=count)
    pos = 1

    def read_size(here: int) -> tuple[int, int, int] | None:
        if here >= len(payload):
            return None
        first = payload[here]
        length = 8 - first.bit_length() + 1
        if here + length > len(payload):
            return None
        marker = 1 << (8 - length)
        value = first ^ marker
        for offset in range(1, length):
            value = (value << 8) | payload[here + offset]
        return value, here + length, length

    first = read_size(pos)
    if first is None:
        result.damaged = True
        result.damage_offset = pos
        result.codes.append("ebml:first-size-truncated")
        return result
    sizes = [first[0]]
    pos = first[1]
    for _ in range(count - 2):
        read = read_size(pos)
        if read is None:
            result.damaged = True
            result.damage_offset = pos
            result.codes.append("ebml:delta-size-truncated")
            return _emit_partial(result, payload, sizes, pos)
        delta_unsigned, pos, width = read
        # EBML lacing deltas are signed; the sign bit is the highest payload
        # bit of the size VINT (7*width - 1).
        sign_bit = 7 * width - 1
        delta = delta_unsigned
        if delta & (1 << sign_bit):
            delta -= 1 << (sign_bit + 1)
        sizes.append(sizes[-1] + delta)
    cursor = pos
    for index, size in enumerate(sizes):
        if size < 0 or cursor + size > len(payload):
            result.damaged = True
            result.damage_offset = cursor
            result.codes.append("ebml:frame-exceeds-payload")
            return _emit_partial(result, payload, sizes, cursor, proven_count=index)
        result.frames.append(LaceFrame(index, cursor, cursor + size, proven=True))
        cursor += size
    last_size = len(payload) - cursor
    if last_size < 0:
        result.damaged = True
        result.damage_offset = cursor
        result.codes.append("ebml:declared-sizes-exceed-payload")
        result.frame_count = None
        return result
    result.frames.append(LaceFrame(len(sizes), cursor, len(payload), proven=True))
    return result


def _emit_partial(
    result: LaceResult,
    payload: bytes,
    sizes: list[int],
    cursor: int,
    proven_count: int | None = None,
) -> LaceResult:
    # Boundaries proven before the damage are emitted; the trailing span is
    # kept opaque rather than split by guesswork.
    result.frame_count = None
    if proven_count is not None:
        result.frames = result.frames[:proven_count]
    if cursor < len(payload):
        result.frames.append(LaceFrame(len(result.frames), cursor, len(payload), proven=False))
    return result


def decode_block_payload(block_data: bytes) -> tuple[int, int, int, bytes, LaceResult]:
    """Split a Block/SimpleBlock body.

    Returns ``(track_number, signed_relative_timecode, flags, frames_payload, lace)``.
    The relative timecode is ALWAYS read as a signed 16-bit value per spec.
    """
    from .vint import VIntError, read_vint

    pos = 0
    track_number, vint_len, unknown = read_vint(block_data, pos)
    if unknown:
        raise VIntError("reserved track number in block")
    pos += vint_len
    if pos + 3 > len(block_data):
        raise VIntError("block header truncated before timecode/flags")
    relative = int.from_bytes(block_data[pos : pos + 2], "big", signed=True)
    flags = block_data[pos + 2]
    frame_payload = block_data[pos + 3 :]
    lacing = (flags >> 1) & 0x3
    if lacing == 0:
        lace = decode_no_lacing(frame_payload)
    elif lacing == 1:
        lace = decode_xiph(frame_payload)
    elif lacing == 2:
        declared_count = frame_payload[0] + 1 if frame_payload else 0
        lace = decode_fixed(frame_payload[1:] if frame_payload else b"", declared_count)
    else:
        lace = decode_ebml_lacing(frame_payload)
    return track_number, relative, flags, frame_payload, lace
