"""EBML / Matroska 原语：VINT 编解码、元素 ID 表、低层元素解析。

该层只做字节事实提取：
- 不规范化时间（保留原始表示与单位，交给 domain 层）；
- 不猜测未知长度元素的语义终点（同时记录元素头范围与原始字节范围）；
- 遇到 lacing 损坏时不猜帧边界，只保留能严格证明的帧大小候选。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# 元素类型
T_MASTER = "master"
T_UINT = "uint"
T_SINT = "ssize"     # EBML signed element value（DiscardPadding 等）
T_FLOAT = "float"
T_STR = "str"
T_BIN = "binary"
T_DATE = "date"

# ---------------------------------------------------------------------------
# Matroska / EBML 元素规格表（仅收录时码审查需要的元素）
# (name, type, unit)；unit 是规范原始单位，派生层才做规范化。
# ---------------------------------------------------------------------------
ELEMENT_SPECS: dict[int, tuple[str, str, Optional[str]]] = {
    0x1A45DFA3: ("EBML", T_MASTER, None),
    0x4286: ("EBMLVersion", T_UINT, None),
    0x42F7: ("EBMLReadVersion", T_UINT, None),
    0x42F2: ("EBMLMaxIDLength", T_UINT, "bytes"),
    0x42F3: ("EBMLMaxSizeLength", T_UINT, "bytes"),
    0x4282: ("DocType", T_STR, None),
    0x4287: ("DocTypeVersion", T_UINT, None),
    0x4285: ("DocTypeReadVersion", T_UINT, None),
    0xEC: ("Void", T_BIN, None),
    0xBF: ("CRC-32", T_BIN, None),
    0x18538067: ("Segment", T_MASTER, None),
    0x114D9B74: ("SeekHead", T_MASTER, None),
    0x4DBB: ("Seek", T_MASTER, None),
    0x53AB: ("SeekID", T_BIN, None),
    0x53AC: ("SeekPosition", T_UINT, "bytes(segment-data-relative)"),
    0x1549A966: ("Info", T_MASTER, None),
    0x2AD7B1: ("TimecodeScale", T_UINT, "ns/tick"),
    0x4489: ("Duration", T_FLOAT, "ticks(TimecodeScale)"),
    0x4D80: ("MuxingApp", T_STR, None),
    0x5741: ("WritingApp", T_STR, None),
    0x1654AE6B: ("Tracks", T_MASTER, None),
    0xAE: ("TrackEntry", T_MASTER, None),
    0xD7: ("TrackNumber", T_UINT, None),
    0x73C5: ("TrackUID", T_UINT, None),
    0x83: ("TrackType", T_UINT, None),
    0x86: ("CodecID", T_STR, None),
    0x23E383: ("DefaultDuration", T_UINT, "ns/frame"),
    0xE0: ("Video", T_MASTER, None),
    0xE1: ("Audio", T_MASTER, None),
    0xB0: ("PixelWidth", T_UINT, "pixels"),
    0xBA: ("PixelHeight", T_UINT, "pixels"),
    0x1F43B675: ("Cluster", T_MASTER, None),
    0xE7: ("Timecode", T_SINT, "ticks(TimecodeScale)"),
    0xA3: ("SimpleBlock", T_BIN, None),
    0xA0: ("BlockGroup", T_MASTER, None),
    0xA1: ("Block", T_BIN, None),
    0x9B: ("BlockDuration", T_UINT, "ticks(TimecodeScale)"),
    0xFB: ("BlockAdditional", T_BIN, None),
    0x75A2: ("DiscardPadding", T_SINT, "ns"),
    0xFB: ("BlockAdditional", T_BIN, None),
    0x1C53BB6B: ("Cues", T_MASTER, None),
    0xBB: ("CuePoint", T_MASTER, None),
    0xB3: ("CueTime", T_UINT, "ticks(TimecodeScale)"),
    0xB7: ("CueTrackPositions", T_MASTER, None),
    0xF7: ("CueTrack", T_UINT, None),
    0xF1: ("CueClusterPosition", T_UINT, "bytes(segment-data-relative)"),
    0x5378: ("CueBlockNumber", T_UINT, None),
    0xB2: ("CueDuration", T_UINT, "ticks(TimecodeScale)"),
}


def spec_for(eid: int) -> tuple[str, str, Optional[str]]:
    return ELEMENT_SPECS.get(eid, (f"Unknown_{eid:02X}", T_BIN, None))


def is_master(eid: int) -> bool:
    return spec_for(eid)[1] == T_MASTER


# ---------------------------------------------------------------------------
# VINT（可变长度整数）
# ---------------------------------------------------------------------------
class EbmlError(ValueError):
    pass


@dataclass(frozen=True)
class VInt:
    value: int
    length: int           # 编码字节数
    marker_stripped: bool


def read_vint(buf: bytes, pos: int) -> VInt:
    """读取一个前导 marker 被剥离语义的 VINT（ID/Size 通用）。"""
    if pos >= len(buf):
        raise EbmlError(f"vint: 偏移 {pos} 超出字节范围 {len(buf)}")
    first = buf[pos]
    if first == 0:
        raise EbmlError(f"vint: 偏移 {pos} 首字节为 0（无 marker 位）")
    length = 0
    marker = 0x80
    while first & marker == 0:
        length += 1
        marker >>= 1
        if length >= 8:
            raise EbmlError(f"vint: 偏移 {pos} 编码超过 8 字节")
    length += 1
    if pos + length > len(buf):
        raise EbmlError(f"vint: 偏移 {pos} 需要 {length} 字节，仅剩 {len(buf) - pos}")
    value = first & (0xFF >> length)
    for i in range(1, length):
        value = (value << 8) | buf[pos + i]
    return VInt(value=value, length=length, marker_stripped=True)


def read_id(buf: bytes, pos: int) -> VInt:
    """元素 ID：保留 marker 位作为 ID 的一部分（Matroska 规范）。"""
    if pos >= len(buf):
        raise EbmlError(f"id: 偏移 {pos} 超出字节范围")
    first = buf[pos]
    if first == 0:
        raise EbmlError(f"id: 偏移 {pos} 首字节为 0")
    length = 0
    marker = 0x80
    while first & marker == 0:
        length += 1
        marker >>= 1
        if length >= 8:
            raise EbmlError(f"id: 偏移 {pos} 编码超过 8 字节")
    length += 1
    if pos + length > len(buf):
        raise EbmlError(f"id: 偏移 {pos} 需要 {length} 字节，仅剩 {len(buf) - pos}")
    value = int.from_bytes(buf[pos:pos + length], "big")
    return VInt(value=value, length=length, marker_stripped=False)


UNKNOWN_SIZE_MARKER = {
    1: 0x7F,
    2: 0x3FFF,
    3: 0x1FFFFF,
    4: 0x0FFFFFFF,
    5: 0x07FFFFFFFF,
    6: 0x03FFFFFFFFFF,
    7: 0x01FFFFFFFFFFFF,
    8: 0x00FFFFFFFFFFFFFF,
}


def is_unknown_size(v: VInt) -> bool:
    return v.value == UNKNOWN_SIZE_MARKER.get(v.length)


def encode_vint(value: int, length: Optional[int] = None) -> bytes:
    """编码数据大小 VINT（自动选择最短长度，或强制 length）。"""
    if value < 0:
        raise EbmlError("vint 大小不能为负")
    lens = [length] if length else list(range(1, 9))
    for ln in lens:
        if value <= UNKNOWN_SIZE_MARKER[ln] - 1:
            raw = value.to_bytes(ln, "big")
            head = bytearray(raw)
            head[0] |= (0x80 >> (ln - 1))
            return bytes(head)
    raise EbmlError(f"值 {value} 无法在 {length or 8} 字节内编码")


def encode_unknown_size(length: int) -> bytes:
    # 未知长度 = 所有数据位全 1（含 marker 位），因此是 length 个 0xFF
    return b"\xff" * length


def encode_id(eid: int) -> bytes:
    bl = (eid.bit_length() + 7) // 8
    if eid.to_bytes(bl, "big")[0] == 0:
        bl += 1
    return eid.to_bytes(bl, "big")


def encode_element(eid: int, payload: bytes, unknown_size: bool = False,
                   size_len: Optional[int] = None) -> bytes:
    if unknown_size:
        return encode_id(eid) + encode_unknown_size(size_len or 4) + payload
    return encode_id(eid) + encode_vint(len(payload), size_len) + payload


# ---------------------------------------------------------------------------
# 类型化取值（uint / ssize / float / str），保留原始字节表示
# ---------------------------------------------------------------------------
def decode_uint(raw: bytes) -> int:
    return int.from_bytes(raw, "big", signed=False)


def decode_sint(raw: bytes) -> int:
    return int.from_bytes(raw, "big", signed=True) if raw else 0


def decode_float(raw: bytes) -> float:
    import struct
    if len(raw) == 4:
        return struct.unpack(">f", raw)[0]
    if len(raw) == 8:
        return struct.unpack(">d", raw)[0]
    raise EbmlError(f"float 长度非法: {len(raw)}")


def decode_str(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


def decode_value(eid: int, raw: bytes):
    _, etype, _ = spec_for(eid)
    if etype == T_UINT:
        return decode_uint(raw)
    if etype == T_SINT:
        return decode_sint(raw)
    if etype == T_FLOAT:
        return decode_float(raw)
    if etype == T_STR:
        return decode_str(raw)
    return None


def signed_rel_timecode(raw2: bytes) -> int:
    """Block 相对时码：按 *有符号* 16 位解释（-32768..32767 ticks）。"""
    return int.from_bytes(raw2, "big", signed=True)
