"""最小 Matroska 字节构造器：仅供离线案例包生成确定性 fixture。

不是通用 muxer，只实现时码审查需要的元素与三种 lacing。
"""
from __future__ import annotations

from . import ebml
from .blocks import LACING_NONE, LACING_XIPH, LACING_FIXED, LACING_EBML


def el(eid: int, payload: bytes = b"", unknown_size: bool = False,
       size_len=None) -> bytes:
    return ebml.encode_element(eid, payload, unknown_size, size_len)


def uint(eid: int, value: int, length: int | None = None) -> bytes:
    if length is None:
        length = max(1, (value.bit_length() + 7) // 8)
    return el(eid, value.to_bytes(length, "big", signed=False))


def sint(eid: int, value: int, length: int | None = None) -> bytes:
    if length is None:
        length = max(1, (8 + value.bit_length() + 7) // 8 if value < 0
                     else max(1, (value.bit_length() + 7) // 8))
    return el(eid, value.to_bytes(length, "big", signed=True))


def text(eid: int, value: str) -> bytes:
    return el(eid, value.encode("utf-8"))


def flo(eid: int, value: float) -> bytes:
    import struct
    return el(eid, struct.pack(">d", value))


# 常用 ID
EBML_HEADER = 0x1A45DFA3
SEGMENT = 0x18538067
INFO = 0x1549A966
TRACKS = 0x1654AE6B
TRACK_ENTRY = 0xAE
CLUSTER = 0x1F43B675
CUES = 0x1C53BB6B
CUE_POINT = 0xBB
CUE_TRACK_POS = 0xB7


def ebml_header(doc_type: str = "matroska", doc_version: int = 4) -> bytes:
    return el(EBML_HEADER,
              uint(0x4286, 1) + uint(0x42F7, 1) +
              uint(0x42F2, 4) + uint(0x42F3, 8) +
              text(0x4282, doc_type) + uint(0x4287, doc_version) +
              uint(0x4285, 2))


def track_entry(number: int, uid: int, track_type: int, codec: str,
                default_duration_ns: int | None = None,
                video: bool = False) -> bytes:
    payload = uint(0xD7, number) + uint(0x73C5, uid) + uint(0x83, track_type) + \
        text(0x86, codec)
    if default_duration_ns is not None:
        payload += uint(0x23E383, default_duration_ns, 4)
    if video:
        payload += el(0xE0, uint(0xB0, 320) + uint(0xBA, 240))
    return el(TRACK_ENTRY, payload)


def _xiph_sizes(sizes: list[int]) -> bytes:
    out = bytearray()
    for s in sizes[:-1]:
        while s >= 255:
            out.append(255)
            s -= 255
        out.append(s)
    return bytes(out)


def _ebml_sizes(sizes: list[int]) -> bytes:
    out = bytearray(ebml.encode_vint(sizes[0]))
    for a, b in zip(sizes, sizes[1:]):
        delta = b - a
        # 有符号差值映射到 1 字节 VINT 值域 [1..126]（0x80 marker 保留）
        val = delta + ((1 << 6) - 1)
        if not (1 <= val <= 126):
            raise ValueError("测试构造器只支持 1 字节差值范围的 ebml lacing")
        out.append(0x80 | val)
    return bytes(out)


def block_payload(track_number: int, rel_tc: int, frames: list[bytes],
                  lacing: int = LACING_NONE, keyframe: bool = False) -> bytes:
    """构造 Block/SimpleBlock 载荷。rel_tc 为有符号 16 位值。"""
    tn = ebml.encode_vint(track_number)
    rel = (rel_tc & 0xFFFF).to_bytes(2, "big")
    if lacing == LACING_NONE or len(frames) <= 1:
        flags = 0x80 if keyframe else 0x00
        return tn + rel + bytes([flags]) + (frames[0] if frames else b"")
    flags = (lacing << 1)
    if keyframe:
        flags |= 0x80
    sizes = [len(f) for f in frames]
    head = tn + rel + bytes([flags, len(frames) - 1])
    if lacing == LACING_XIPH:
        return head + _xiph_sizes(sizes) + b"".join(frames)
    if lacing == LACING_FIXED:
        assert len(set(sizes)) == 1, "fixed lacing 要求等长帧"
        return head + b"".join(frames)
    if lacing == LACING_EBML:
        return head + _ebml_sizes(sizes) + b"".join(frames)
    raise ValueError(lacing)


def simple_block(track_number: int, rel_tc: int, frames: list[bytes],
                 lacing: int = LACING_NONE, keyframe: bool = True) -> bytes:
    return el(0xA3, block_payload(track_number, rel_tc, frames, lacing,
                                  keyframe))


def block_group(track_number: int, rel_tc: int, frames: list[bytes],
                lacing: int = LACING_NONE, duration: int | None = None,
                discard_padding: int | None = None,
                reference_block: int | None = None) -> bytes:
    payload = el(0xA1, block_payload(track_number, rel_tc, frames, lacing,
                                     keyframe=False))
    if duration is not None:
        payload += uint(0x9B, duration)
    if discard_padding is not None:
        payload += sint(0x75A2, discard_padding, 4)
    if reference_block is not None:
        payload += sint(0xFB if False else 0xFB, 0) if False else \
            el(0xFB, reference_block.to_bytes(2, "big", signed=True))
    return el(0xA0, payload)


def cluster(timecode: int, body: bytes = b"", unknown_size: bool = False,
            size_len: int = 4) -> bytes:
    return el(CLUSTER, el(0xE7, timecode.to_bytes(4, "big", signed=True)) + body,
              unknown_size=unknown_size, size_len=size_len)


def cue_point(cue_time: int, track: int, cluster_pos: int,
              block_number: int | None = None,
              cue_duration: int | None = None) -> bytes:
    pos = uint(0xF7, track) + uint(0xF1, cluster_pos, 4)
    if block_number is not None:
        pos += uint(0x5378, block_number)
    if cue_duration is not None:
        pos += uint(0xB2, cue_duration, 4)
    return el(CUE_POINT, uint(0xB3, cue_time) + el(CUE_TRACK_POS, pos))


def cues(points: list[bytes]) -> bytes:
    return el(CUES, b"".join(points))


def segment(body: bytes, unknown_size: bool = False) -> bytes:
    return el(SEGMENT, body, unknown_size=unknown_size, size_len=8)
