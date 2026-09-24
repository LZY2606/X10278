"""EBML/Matroska 原始解析层。

只负责把字节流切成元素并保留原始表示：
元素偏移、头长、数据范围、未知长度(unknown size)原样保留；
Block 的 relative timecode 按有符号 16 位解释；
lacing 损坏时抛出 LacingCorrupt，绝不猜测帧边界。
"""
from __future__ import annotations

from dataclasses import dataclass, field

ELEMENT_NAMES = {
    0x1A45DFA3: "EBML",
    0x4286: "EBMLVersion", 0x42F7: "EBMLReadVersion", 0x42F2: "EBMLMaxIDLength",
    0x42F3: "EBMLMaxSizeLength", 0x4282: "DocType", 0x4287: "DocTypeVersion",
    0x4285: "DocTypeReadVersion",
    0x18538067: "Segment",
    0x1549A966: "Info", 0x2AD7B1: "TimecodeScale",
    0x1654AE6B: "Tracks", 0xAE: "TrackEntry",
    0xD7: "TrackNumber", 0x73C5: "TrackUID", 0x83: "TrackType",
    0x86: "CodecID", 0x23314F: "TrackTimecodeScale", 0x23E383: "DefaultDuration",
    0x1F43B675: "Cluster", 0xE7: "Timecode",
    0xA0: "BlockGroup", 0xA1: "Block", 0xA3: "SimpleBlock",
    0x75A2: "DiscardPadding", 0xFB: "ReferenceBlock",
    0x1C53BB6B: "Cues", 0xBB: "CuePoint", 0xB3: "CueTime",
    0xB7: "CueTrackPositions", 0xF7: "CueTrack", 0xF1: "CueClusterPosition",
}

MASTER_IDS = {
    0x1A45DFA3, 0x18538067, 0x1549A966, 0x1654AE6B, 0xAE,
    0x1F43B675, 0xA0, 0x1C53BB6B, 0xBB, 0xB7,
}

LACING_NAMES = {0: "none", 1: "xiph", 2: "fixed", 3: "ebml"}


class EbmlError(Exception):
    pass


class LacingCorrupt(Exception):
    """lacing 尺寸表与数据不符；调用方不得猜测帧边界。"""


def read_vint(buf: bytes, pos: int, strip: bool) -> tuple[int, int]:
    """读取 EBML 变长整数，返回 (值, 字节长度)。strip=True 时去掉长度标记位。"""
    if pos >= len(buf):
        raise EbmlError("vint 越界(eof)")
    first = buf[pos]
    if first == 0:
        raise EbmlError("非法 vint(前导字节为 0)")
    mask = 0x80
    length = 1
    while not (first & mask):
        length += 1
        mask >>= 1
        if length > 8:
            raise EbmlError("vint 长度超过 8")
    if pos + length > len(buf):
        raise EbmlError("vint 被截断")
    val = int.from_bytes(buf[pos:pos + length], "big")
    if strip:
        val &= (1 << (7 * length)) - 1
    return val, length


def encode_id(n: int) -> bytes:
    return n.to_bytes((n.bit_length() + 7) // 8, "big")


def encode_size(n: int, length: int | None = None) -> bytes:
    if length is None:
        length = 1
        while n >= (1 << (7 * length)) - 1:
            length += 1
    return ((1 << (7 * length)) | n).to_bytes(length, "big")


@dataclass
class Element:
    seq: int
    eid: int
    offset: int
    header_size: int
    size: int | None
    data_offset: int
    parent: int | None
    name: str = ""

    @property
    def unknown_size(self) -> bool:
        return self.size is None

    def end(self, fallback: int) -> int:
        return fallback if self.size is None else self.data_offset + self.size


@dataclass
class ParseResult:
    elements: list[Element] = field(default_factory=list)
    corrupt_ranges: list[tuple[int, int, str]] = field(default_factory=list)


def parse(data: bytes) -> ParseResult:
    """把字节流解析为元素列表。未知长度元素延伸到父元素末尾(简化裁定，已记录)。"""
    result = ParseResult()

    def walk(start: int, end: int, parent: int | None) -> None:
        pos = start
        while pos < end:
            try:
                eid, idlen = read_vint(data, pos, strip=False)
                size, slen = read_vint(data, pos + idlen, strip=True)
            except EbmlError as exc:
                result.corrupt_ranges.append((pos, end, str(exc)))
                return
            unknown = size == (1 << (7 * slen)) - 1
            el = Element(
                seq=len(result.elements), eid=eid, offset=pos,
                header_size=idlen + slen,
                size=None if unknown else size,
                data_offset=pos + idlen + slen, parent=parent,
                name=ELEMENT_NAMES.get(eid, f"Unknown_0x{eid:x}"),
            )
            result.elements.append(el)
            el_end = end if unknown else min(el.data_offset + el.size, end)
            if eid in MASTER_IDS:
                walk(el.data_offset, el_end, el.seq)
            pos = el_end

    walk(0, len(data), None)
    return result


def split_frames(body: bytes, lacing: int) -> list[int]:
    """按 lacing 模式切出每帧尺寸。损坏时抛 LacingCorrupt，不猜边界。"""
    if lacing == 0:
        return [len(body)]
    if not body:
        raise LacingCorrupt("laced 负载为空")
    count = body[0] + 1
    if lacing == 2:
        payload = body[1:]
        if count <= 1 or len(payload) % count != 0:
            raise LacingCorrupt(
                f"fixed lacing: {len(payload)} 字节无法均分为 {count} 帧")
        return [len(payload) // count] * count
    if lacing == 1:
        pos = 1
        sizes: list[int] = []
        for _ in range(count - 1):
            size = 0
            while True:
                if pos >= len(body):
                    raise LacingCorrupt("xiph lacing: 尺寸链越界")
                byte = body[pos]
                pos += 1
                size += byte
                if byte != 255:
                    break
            sizes.append(size)
        last = len(body) - pos - sum(sizes)
        if last < 0:
            raise LacingCorrupt(
                f"xiph lacing: 尺寸和 {sum(sizes)} 超出剩余数据 {len(body) - pos}")
        return sizes + [last]
    if lacing == 3:
        pos = 1
        first, vlen = read_vint(body, pos, strip=True)
        pos += vlen
        sizes = [first]
        prev = first
        for _ in range(count - 2):
            raw, vlen = read_vint(body, pos, strip=True)
            bias = (1 << (7 * vlen - 1)) - 1
            prev = prev + (raw - bias)
            if prev < 0:
                raise LacingCorrupt("ebml lacing: 帧尺寸为负")
            sizes.append(prev)
            pos += vlen
        last = len(body) - pos - sum(sizes)
        if last < 0:
            raise LacingCorrupt("ebml lacing: 尺寸和超出剩余数据")
        return sizes + [last]
    raise LacingCorrupt(f"未知 lacing 模式 {lacing}")


def parse_block_payload(payload: bytes) -> dict:
    """解析 Block/SimpleBlock 负载。relative timecode 固定按有符号 int16 解释。"""
    track, tlen = read_vint(payload, 0, strip=True)
    if tlen + 3 > len(payload):
        raise EbmlError("block 头被截断")
    rel = int.from_bytes(payload[tlen:tlen + 2], "big", signed=True)
    flags = payload[tlen + 2]
    lacing = (flags >> 1) & 0x3
    frames = split_frames(payload[tlen + 3:], lacing)
    return {
        "track": track,
        "rel_timecode": rel,
        "rel_unit": "int16 ticks (signed)",
        "keyframe": bool(flags & 0x80),
        "lacing": LACING_NAMES[lacing],
        "frames": frames,
    }
