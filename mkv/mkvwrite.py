"""Minimal EBML/Matroska writer used to build the repository's own
domain fixtures (synthetic Matroska/WebM byte streams)."""
from __future__ import annotations

import struct

IDS = {
    "EBML": b"\x1a\x45\xdf\xa3",
    "EBMLVersion": b"\x42\x86",
    "EBMLReadVersion": b"\x42\xf7",
    "DocType": b"\x42\x82",
    "Segment": b"\x18\x53\x80\x67",
    "Info": b"\x15\x49\xa9\x66",
    "TimecodeScale": b"\x2a\xd7\xb1",
    "MuxingApp": b"\x4d\x80",
    "WritingApp": b"\x57\x41",
    "Tracks": b"\x16\x54\xae\x6b",
    "TrackEntry": b"\xae",
    "TrackNumber": b"\xd7",
    "TrackUID": b"\x73\xc5",
    "TrackType": b"\x83",
    "CodecID": b"\x86",
    "Name": b"\x53\x6e",
    "DefaultDuration": b"\x23\xe3\x83",
    "TrackTimecodeScale": b"\x23\x31\x4f",
    "Cues": b"\x1c\x53\xbb\x6b",
    "CuePoint": b"\xbb",
    "CueTime": b"\xb3",
    "CueTrackPositions": b"\xb7",
    "CueTrack": b"\xf7",
    "CueClusterPosition": b"\xf1",
    "Cluster": b"\x1f\x43\xb6\x75",
    "Timecode": b"\xe7",
    "BlockGroup": b"\xa0",
    "Block": b"\xa1",
    "BlockDuration": b"\x9b",
    "DiscardPadding": b"\x75\xa2",
    "SimpleBlock": b"\xa3",
}


def size_vint(value: int, length: int | None = None) -> bytes:
    if length is None:
        length = 1
        while value >= (1 << (7 * length)) - 1:
            length += 1
    return ((1 << (7 * length)) | value).to_bytes(length, "big")


def unknown_size(length: int = 1) -> bytes:
    return size_vint((1 << (7 * length)) - 1, length)


def elem(name: str, payload: bytes, unknown: bool = False,
         unknown_len: int = 1) -> bytes:
    head = IDS[name]
    if unknown:
        size = unknown_size(unknown_len)
    else:
        size = size_vint(len(payload))
    return head + size + payload


def uint_elem(name: str, value: int) -> bytes:
    n = max(1, (value.bit_length() + 7) // 8)
    return elem(name, value.to_bytes(n, "big"))


def int_elem(name: str, value: int) -> bytes:
    n = 1
    while not (-(1 << (8 * n - 1)) <= value < (1 << (8 * n - 1))):
        n += 1
    return elem(name, value.to_bytes(n, "big", signed=True))


def float_elem(name: str, value: float) -> bytes:
    return elem(name, struct.pack(">d", value))


def str_elem(name: str, text: str) -> bytes:
    return elem(name, text.encode("utf-8"))


def _signed_vint(value: int) -> bytes:
    length = 1
    while True:
        bias = (1 << (7 * length - 1)) - 1
        raw = value + bias
        if 0 <= raw < (1 << (7 * length)) - 1:
            return size_vint(raw, length)
        length += 1


def _xiph_size(value: int) -> bytes:
    out = bytearray()
    while value >= 255:
        out.append(255)
        value -= 255
    out.append(value)
    return bytes(out)


def block_body(frames: list[bytes], lacing: str = "none") -> tuple[int, bytes]:
    """Return (flags, laced body) for the given frames."""
    if lacing == "none":
        return 0x00, frames[0]
    n = len(frames)
    if lacing == "xiph":
        head = bytes([n - 1]) + b"".join(_xiph_size(len(f)) for f in frames[:-1])
        return 0x02, head + b"".join(frames)
    if lacing == "fixed":
        sizes = {len(f) for f in frames}
        if len(sizes) != 1:
            raise ValueError("fixed lacing needs equal frame sizes")
        return 0x04, bytes([n - 1]) + b"".join(frames)
    if lacing == "ebml":
        head = bytearray([n - 1])
        head += size_vint(len(frames[0]))
        prev = len(frames[0])
        for f in frames[1:-1]:
            head += _signed_vint(len(f) - prev)
            prev = len(f)
        return 0x06, bytes(head) + b"".join(frames)
    raise ValueError(lacing)


def block_payload(track: int, rel: int, frames: list[bytes],
                  lacing: str = "none") -> bytes:
    flags, body = block_body(frames, lacing)
    return bytes([0x80 | track]) + rel.to_bytes(2, "big", signed=True) \
        + bytes([flags]) + body


def simple_block(track: int, rel: int, frames: list[bytes],
                 lacing: str = "none") -> bytes:
    return elem("SimpleBlock", block_payload(track, rel, frames, lacing))


def block_group(track: int, rel: int, frames: list[bytes],
                lacing: str = "none", duration: int | None = None,
                discard_padding: int | None = None) -> bytes:
    payload = elem("Block", block_payload(track, rel, frames, lacing))
    if duration is not None:
        payload += uint_elem("BlockDuration", duration)
    if discard_padding is not None:
        payload += int_elem("DiscardPadding", discard_padding)
    return elem("BlockGroup", payload)
