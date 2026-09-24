"""EBML/Matroska 解析器。

设计约束(对应反例档案语义):
- 每个元素保留: 元素 ID、文件内偏移、头部长度、数据偏移、声明长度、
  未知长度标记、推断出的结束位置、原始字节范围 [offset, end)。
- 未知长度(尺寸位全 1)的元素不丢弃: 结束位置按"下一个同级/父级元素 ID 或父边界"
  推断, 并标记 inferred=True, 原始声明原样保留。
- 数值元素解码为 int/float/str, 但原始字节(raw hex)始终保留在 value_raw 中。
"""
from __future__ import annotations

from dataclasses import dataclass, field

IDS = {
    0x1A45DFA3: "EBML", 0x4286: "EBMLVersion", 0x42F7: "EBMLReadVersion",
    0x42F2: "EBMLMaxIDLength", 0x42F3: "EBMLMaxSizeLength", 0x4282: "DocType",
    0x4287: "DocTypeVersion", 0x4285: "DocTypeReadVersion",
    0x18538067: "Segment", 0x1549A966: "Info", 0x2AD7B1: "TimecodeScale",
    0x4D80: "MuxingApp", 0x5741: "WritingApp", 0x7BA9: "Title",
    0x1654AE6B: "Tracks", 0xAE: "TrackEntry", 0xD7: "TrackNumber",
    0x73C5: "TrackUID", 0x83: "TrackType", 0x9C: "FlagLacing",
    0x23E383: "DefaultDuration", 0x86: "CodecID", 0x536E: "Name",
    0x23314F: "TrackTimecodeScale",
    0x1F43B675: "Cluster", 0xE7: "Timestamp",
    0xA3: "SimpleBlock", 0xA0: "BlockGroup", 0xA1: "Block",
    0x9B: "BlockDuration", 0x75A2: "DiscardPadding",
    0x1C53BB6B: "Cues", 0xBB: "CuePoint", 0xB3: "CueTime",
    0xB7: "CueTrackPositions", 0xF7: "CueTrack",
    0xF1: "CueClusterPosition", 0x5378: "CueBlockNumber",
    0xEC: "Void", 0xBF: "CRC-32", 0x114D9B74: "SeekHead",
}
MASTER_IDS = {0x1A45DFA3, 0x18538067, 0x1549A966, 0x1654AE6B, 0xAE,
              0x1F43B675, 0xA0, 0x1C53BB6B, 0xBB, 0xB7, 0x114D9B74}
UINT_IDS = {0x4286, 0x42F7, 0x42F2, 0x42F3, 0x4287, 0x4285, 0x2AD7B1,
            0xD7, 0x73C5, 0x83, 0x9C, 0x23E383, 0xE7, 0x9B, 0xB3, 0xF7,
            0xF1, 0x5378}
INT_IDS = {0x75A2}                      # DiscardPadding: 有符号整数, 单位 ns
FLOAT_IDS = {0x23314F}                  # TrackTimecodeScale
STRING_IDS = {0x4282, 0x86, 0x536E, 0x4D80, 0x5741, 0x7BA9}

# 解析未知长度元素时, 遇到这些 ID 说明已越出该元素(父级/同级边界)
SEGMENT_LEVEL_IDS = {0x1549A966, 0x1654AE6B, 0x1F43B675, 0x1C53BB6B,
                     0x114D9B74, 0x1043A770, 0x1941A469}
TERMINATORS = {
    0x1F43B675: SEGMENT_LEVEL_IDS,      # Cluster 遇段级 ID 结束
    0x18538067: {0x1A45DFA3},           # Segment 遇下一个 EBML 头结束
}


class ParseError(Exception):
    pass


def read_vint(data: bytes, pos: int, strip_marker: bool):
    """读 EBML 变长整数。返回 (value, length, unknown)。"""
    if pos >= len(data):
        raise ParseError(f"vint 越界 @{pos}")
    first = data[pos]
    mask, length = 0x80, 1
    while length <= 8 and not (first & mask):
        mask >>= 1
        length += 1
    if length > 8 or pos + length > len(data):
        raise ParseError(f"vint 长度非法 @{pos}")
    value = first & (mask - 1) if strip_marker else first
    for i in range(1, length):
        value = (value << 8) | data[pos + i]
    unknown = strip_marker and value == (1 << (7 * length)) - 1
    return value, length, unknown


@dataclass
class Element:
    eid: int
    name: str
    offset: int                 # 元素 ID 起始文件偏移
    header_size: int            # ID + 尺寸字段字节数
    data_offset: int
    size: int | None            # None = 未知长度
    end: int                    # 解析后(可能推断)的结束偏移
    unknown_size: bool = False
    inferred_end: bool = False  # end 是推断而非声明
    value: object = None        # uint/int/float/str 解码值
    value_raw: str = ""         # 原始字节 hex, 导入时原样保留
    children: list = field(default_factory=list)

    @property
    def raw_range(self):
        return (self.offset, self.end)


def _decode_scalar(eid: int, payload: bytes):
    if eid in UINT_IDS:
        return int.from_bytes(payload, "big")
    if eid in INT_IDS:
        return int.from_bytes(payload, "big", signed=True)
    if eid in FLOAT_IDS:
        import struct
        if len(payload) == 4:
            return struct.unpack(">f", payload)[0]
        if len(payload) == 8:
            return struct.unpack(">d", payload)[0]
        raise ParseError("float 长度须为 4 或 8")
    if eid in STRING_IDS:
        return payload.decode("utf-8", "replace")
    return None


def parse_elements(data: bytes, pos: int, end: int, depth: int = 0,
                   parent_eid: int | None = None) -> list[Element]:
    """解析 [pos, end) 区间内的兄弟元素序列。"""
    out = []
    while pos < end:
        start = pos
        try:
            eid, id_len, _ = read_vint(data, pos, strip_marker=False)
        except ParseError:
            break
        # 未知长度父元素的终止判定: 当前 ID 属于父级的同级/上级
        if parent_eid in TERMINATORS and eid in TERMINATORS[parent_eid]:
            break
        pos += id_len
        size, size_len, unknown = read_vint(data, pos, strip_marker=True)
        pos += size_len
        header = id_len + size_len
        data_off = pos
        declared = None if unknown else size
        el = Element(eid=eid, name=IDS.get(eid, f"Unknown-{eid:x}"),
                     offset=start, header_size=header, data_offset=data_off,
                     size=declared, end=0, unknown_size=unknown)
        if eid in MASTER_IDS:
            child_end = len(data) if unknown else data_off + size
            child_end = min(child_end, len(data))
            el.children = parse_elements(data, data_off, child_end, depth + 1, eid)
            if unknown:
                # 结束位置 = 最后一个子元素之后; 无子元素则到父边界
                el.end = el.children[-1].end if el.children else data_off
                el.inferred_end = True
            else:
                el.end = data_off + size
        else:
            if unknown:
                raise ParseError(f"非主元素 {el.name} 声明未知长度 @{start}")
            el.end = data_off + size
            if el.end > len(data):
                raise ParseError(f"元素 {el.name} 越出文件 @{start}")
            payload = data[data_off:el.end]
            el.value_raw = payload.hex()
            el.value = _decode_scalar(eid, payload)
        out.append(el)
        pos = el.end
    return out


def parse_file(data: bytes) -> list[Element]:
    return parse_elements(data, 0, len(data))


def flatten(elements: list[Element]) -> list[Element]:
    out = []
    def walk(els):
        for e in els:
            out.append(e)
            walk(e.children)
    walk(elements)
    return out
