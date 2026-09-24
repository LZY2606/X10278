"""Builds tiny, valid Matroska/WebM byte streams for offline fixtures.

Only the subset exercised by the case is emitted. Every writer returns the
exact bytes placed on disk so tests can assert original byte ranges.
"""
from __future__ import annotations

from . import ebml
from .ebml import (
    build_element, element_with_unknown_size, uint_bytes, encode_vint,
)


def el(eid: str, value=None, raw: bytes | None = None) -> bytes:
    if raw is not None:
        payload = raw
    elif value is None:
        payload = b""
    elif isinstance(value, bytes):
        payload = value
    elif isinstance(value, str):
        payload = value.encode("utf-8")
    else:
        payload = uint_bytes(int(value))
    return build_element(eid, payload)


def master(eid: str, parts, unknown_size: bool = False) -> bytes:
    payload = b"".join(parts)
    if unknown_size:
        return element_with_unknown_size(eid, payload)
    return build_element(eid, payload)


def encode_track_number(n: int) -> bytes:
    return encode_vint(n)


def block_payload(track: int, rel_tc: int, flags: int, frames: list[bytes],
                  lacing: int = 0) -> bytes:
    """frames: lacing 0 -> exactly one frame; 1 Xiph; 2 fixed; 3 EBML."""
    out = encode_vint(track) + rel_tc.to_bytes(2, "big", signed=True) + bytes([flags])
    n = len(frames)
    if lacing == 0 or n <= 1:
        out += frames[0]
        return out
    f = (flags & 0b110) | ((lacing & 0b11) << 1)
    out = encode_vint(track) + rel_tc.to_bytes(2, "big", signed=True) + bytes([f])
    out += encode_vint(n - 1)
    if lacing == 1:  # Xiph sizes, 255 chaining
        for fr in frames[:-1]:
            size = len(fr)
            while size >= 255:
                out += b"\xff"
                size -= 255
            out += bytes([size])
        out += b"".join(frames)
    elif lacing == 2:  # fixed size
        out += b"".join(frames)
    else:  # EBML lacing with signed deltas
        sizes = [len(x) for x in frames]
        out += encode_vint(sizes[0])
        for i in range(1, n - 1):
            out += _ebml_signed(sizes[i] - sizes[i - 1])
        out += b"".join(frames)
    return out


def _ebml_signed(delta: int) -> bytes:
    # EBML lacing signed VINT: choose a width that holds the two's complement
    for w in range(1, 9):
        bias = 1 << (7 * w - 1)
        marker = 1 << (8 - w)
        low_bits = 7 * w
        v = delta + bias
        if 0 <= v < (1 << low_bits):
            return (marker | v).to_bytes(w, "big")
    raise ValueError("delta out of range")


def simple_block(track: int, rel_tc: int, frames: list[bytes], lacing: int = 0,
                 keyframe: bool = True) -> bytes:
    flags = 0x80 if keyframe else 0x00
    payload = block_payload(track, rel_tc, flags, frames, lacing)
    return el("a3", raw=payload)


def block_group(track: int, rel_tc: int, frames: list[bytes], lacing: int = 0,
                duration: int | None = None, discard: int | None = None,
                reference: int | None = None) -> bytes:
    payload = block_payload(track, rel_tc, 0x00, frames, lacing)
    parts = [el("a1", raw=payload)]
    if duration is not None:
        parts.append(el("9b", duration))
    if discard is not None:
        parts.append(el("75a1", raw=discard.to_bytes(4, "big", signed=True)))
    if reference is not None:
        parts.append(el("fa", raw=reference.to_bytes(1, "big", signed=True)))
    return master("a0", parts)


def cluster(tc: int, blocks: list[bytes], unknown_size: bool = False) -> bytes:
    return master("1f43b675", [el("e7", tc)] + blocks, unknown_size)


def cue_point(cue_time: int, track: int, cluster_pos: int,
              rel_pos: int | None = None, duration: int | None = None) -> bytes:
    pos = [el("f7", track), el("f1", cluster_pos)]
    if rel_pos is not None:
        pos.append(el("f0", rel_pos))
    if duration is not None:
        pos.append(el("5378", duration))
    return master("bb", [el("b3", cue_time), master("b7", pos)])


def cues(points: list[bytes]) -> bytes:
    return master("1c53bb6b", points)


def track_entry(number: int, uid: int, ttype: int, codec: str,
                default_duration: int | None = None,
                track_tc_scale: float | None = None,
                track_offset: int | None = None) -> bytes:
    parts = [
        el("d7", number), el("73c5", uid), el("83", ttype), el("86", codec),
        el("9c", 1),
    ]
    if default_duration is not None:
        parts.append(el("23e383", default_duration))
    if track_tc_scale is not None:
        parts.append(el("23314f", raw=_float64(track_tc_scale)))
    if track_offset is not None:
        parts.append(el("234e7a", raw=track_offset.to_bytes(4, "big", signed=True)))
    if ttype == 1:
        parts.append(master("e1", [el("b5", raw=_float64(48000.0))]))
    else:
        parts.append(master("e0", [el("b0", 16), el("ba", 16)]))
    return master("ae", parts)


def _float64(x: float) -> bytes:
    import struct
    return struct.pack(">d", x)


def tracks(entries: list[bytes]) -> bytes:
    return master("1654ae6b", entries)


def ebml_header(doc_type: str = "matroska", version: int = 4) -> bytes:
    return master("1a45dfa3", [
        el("4286", 1), el("42f7", 1), el("42f2", 4), el("42f3", 8),
        el("4282", doc_type), el("4287", version), el("4285", 2),
    ])


def segment(parts: list[bytes], unknown_size: bool = False) -> bytes:
    return master("18538067", parts, unknown_size=unknown_size)
