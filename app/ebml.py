"""EBML 解析器。

核心语义:
- 每个元素保留文件偏移、头部长度、数据范围与原始字节(raw bytes 不丢)。
- size 全 1 的 VINT 表示 unknown length,元素延伸到父元素末尾,并打 unknown_size 标记。
- 解析失败不猜结构:记录 diagnostic,保留原始字节范围,继续解析后续兄弟元素。
"""
from __future__ import annotations

from dataclasses import dataclass, field
import struct


# (id, name, type)  type: master/uint/int/float/string/binary/date
ELEMENTS = {
    0x1A45DFA3: ("EBML", "master"),
    0x4286: ("EBMLVersion", "uint"),
    0x42F7: ("EBMLReadVersion", "uint"),
    0x42F2: ("EBMLMaxIDLength", "uint"),
    0x42F3: ("EBMLMaxSizeLength", "uint"),
    0x4282: ("DocType", "string"),
    0x18538067: ("Segment", "master"),
    0x1549A966: ("Info", "master"),
    0x2AD7B1: ("TimecodeScale", "uint"),
    0x4489: ("Duration", "float"),
    0x1654AE6B: ("Tracks", "master"),
    0xAE: ("TrackEntry", "master"),
    0xD7: ("TrackNumber", "uint"),
    0x73C5: ("TrackUID", "uint"),
    0x83: ("TrackType", "uint"),
    0x86: ("CodecID", "string"),
    0x23314F: ("TrackTimecodeScale", "float"),
    0x23E383: ("DefaultDuration", "uint"),
    0x1F43B675: ("Cluster", "master"),
    0xE7: ("Timecode", "uint"),
    0xA3: ("SimpleBlock", "binary"),
    0xA0: ("BlockGroup", "master"),
    0xA1: ("Block", "binary"),
    0x9B: ("BlockDuration", "uint"),
    0x75A2: ("DiscardPadding", "int"),
    0x1C53BB6B: ("Cues", "master"),
    0xBB: ("CuePoint", "master"),
    0xB3: ("CueTime", "uint"),
    0xB7: ("CueTrackPositions", "master"),
    0xF7: ("CueTrack", "uint"),
    0xF1: ("CueClusterPosition", "uint"),
    0xEC: ("Void", "binary"),
    0xBF: ("CRC-32", "binary"),
}

MASTER_IDS = {eid for eid, (_, t) in ELEMENTS.items() if t == "master"}

# EBML 层级(用于 unknown-size 元素的终止判定:
# 遇到层级 <= 当前元素层级的 ID 时,未知长度元素结束)
LEVELS = {
    0x1A45DFA3: 0, 0x18538067: 0,
    0x1549A966: 1, 0x1654AE6B: 1, 0x1F43B675: 1, 0x1C53BB6B: 1, 0xEC: 1,
    0xAE: 2, 0xE7: 2, 0xA3: 2, 0xA0: 2, 0xBB: 2,
    0xD7: 3, 0x73C5: 3, 0x83: 3, 0x86: 3, 0x23314F: 3, 0x23E383: 3,
    0xA1: 3, 0x9B: 3, 0x75A2: 3, 0xB3: 3, 0xB7: 3,
    0xF7: 4, 0xF1: 4,
    0x4286: 1, 0x42F7: 1, 0x42F2: 1, 0x42F3: 1, 0x4282: 1,
    0x2AD7B1: 2, 0x4489: 2,
}


class EBMLFormatError(Exception):
    pass


def read_vint(data: bytes, pos: int, keep_marker: bool) -> tuple[int, int]:
    """读取 VINT,返回 (value, length)。keep_marker=True 用于元素 ID。"""
    if pos >= len(data):
        raise EBMLFormatError(f"vint: offset {pos} 超出数据末尾 {len(data)}")
    first = data[pos]
    if first == 0:
        raise EBMLFormatError(f"vint: offset {pos} 首字节为 0,无法确定长度")
    mask = 0x80
    length = 1
    while length <= 8 and not (first & mask):
        mask >>= 1
        length += 1
    if length > 8:
        raise EBMLFormatError(f"vint: offset {pos} 长度超过 8")
    if pos + length > len(data):
        raise EBMLFormatError(f"vint: offset {pos} 数据不足,需要 {length} 字节")
    value = first if keep_marker else (first & (mask - 1))
    for i in range(1, length):
        value = (value << 8) | data[pos + i]
    return value, length


def vint_is_unknown(value: int, length: int) -> bool:
    """size VINT 数据位全 1 => unknown length。"""
    return value == (1 << (7 * length)) - 1


@dataclass
class Element:
    id: int
    name: str
    type: str
    offset: int            # 元素 ID 的文件偏移(坐标基准: 文件字节偏移)
    header_size: int       # ID + size 字段字节数
    data_offset: int       # 载荷起始偏移
    size: int | None       # None => unknown length
    unknown_size: bool
    end: int               # 元素结束偏移(unknown 时取父元素末尾)
    depth: int = 0
    children: list["Element"] = field(default_factory=list)
    value: object = None   # 原始解码值(未规范化)
    diagnostics: list[str] = field(default_factory=list)

    @property
    def raw_range(self) -> tuple[int, int]:
        return (self.offset, self.end)


def _decode_value(el: "Element", payload: bytes) -> object:
    t = el.type
    try:
        if t == "uint":
            return int.from_bytes(payload, "big")
        if t == "int":
            return int.from_bytes(payload, "big", signed=True)
        if t == "float":
            if len(payload) == 4:
                return struct.unpack(">f", payload)[0]
            if len(payload) == 8:
                return struct.unpack(">d", payload)[0]
            raise EBMLFormatError(f"float 长度 {len(payload)} 非法")
        if t == "string":
            return payload.decode("utf-8", errors="replace")
    except EBMLFormatError:
        raise
    except Exception as exc:  # 解码失败:保留原字节,附诊断
        el.diagnostics.append(f"decode_error:{exc}")
    return None


def parse_elements(data: bytes, start: int, end: int, depth: int = 0,
                   diagnostics: list[str] | None = None,
                   stop_level: int | None = None) -> list[Element]:
    """解析 [start, end) 区间内的兄弟元素序列。

    stop_level: 父元素为未知长度时给出其父层级;遇到层级 <= stop_level
    的已知元素 ID 时停止(未知长度元素在此结束),不消费该元素。
    """
    out: list[Element] = []
    pos = start
    while pos < end:
        el_start = pos
        try:
            eid, id_len = read_vint(data, pos, keep_marker=True)
            if stop_level is not None and eid in LEVELS and LEVELS[eid] <= stop_level:
                break
            size_val, size_len = read_vint(data, pos + id_len, keep_marker=False)
        except EBMLFormatError as exc:
            if diagnostics is not None:
                diagnostics.append(
                    f"parse_error@{el_start}: {exc}; 剩余 {end - el_start} 字节保留为未解析范围")
            break
        unknown = vint_is_unknown(size_val, size_len)
        header = id_len + size_len
        data_off = el_start + header
        size = None if unknown else size_val
        el_end = end if unknown else min(data_off + size_val, end)
        if not unknown and data_off + size_val > end:
            if diagnostics is not None:
                diagnostics.append(
                    f"truncated@{el_start}: 声明大小 {size_val} 超出父元素边界,截断到 {end}")
        name, etype = ELEMENTS.get(eid, (f"Unknown-0x{eid:X}", "binary"))
        el = Element(id=eid, name=name, type=etype, offset=el_start,
                     header_size=header, data_offset=data_off, size=size,
                     unknown_size=unknown, end=el_end, depth=depth)
        if unknown:
            el.diagnostics.append("unknown_size: 元素长度未知,延伸至父元素末尾")
        if etype == "master":
            child_stop = LEVELS.get(eid) if unknown else None
            el.children = parse_elements(data, data_off, el_end, depth + 1,
                                         el.diagnostics, stop_level=child_stop)
            if unknown and el.children:
                # 未知长度元素在实际子元素末尾结束(遇到同级/上级 ID)
                el.end = el.children[-1].end
                el_end = el.end
            elif unknown and not el.children:
                el.end = data_off
                el_end = el.end
        elif etype != "binary":
            try:
                el.value = _decode_value(el, data[data_off:el_end])
            except EBMLFormatError as exc:
                el.diagnostics.append(f"decode_error:{exc}")
        out.append(el)
        if el_end <= pos:  # 防御:零长前进
            break
        pos = el_end
    return out


def parse_file(data: bytes) -> tuple[list[Element], list[str]]:
    diagnostics: list[str] = []
    roots = parse_elements(data, 0, len(data), 0, diagnostics)
    return roots, diagnostics


def walk(elements: list[Element]):
    for el in elements:
        yield el
        yield from walk(el.children)


def find_all(elements: list[Element], name: str):
    return [el for el in walk(elements) if el.name == name]


def first_child(el: Element, name: str) -> Element | None:
    for c in el.children:
        if c.name == name:
            return c
    return None


def element_to_dict(el: Element, data: bytes | None = None) -> dict:
    d = {
        "id": f"0x{el.id:X}",
        "name": el.name,
        "type": el.type,
        "offset": el.offset,
        "header_size": el.header_size,
        "data_offset": el.data_offset,
        "size": el.size,
        "unknown_size": el.unknown_size,
        "end": el.end,
        "value": el.value,
        "diagnostics": list(el.diagnostics),
        "children": [element_to_dict(c, data) for c in el.children],
    }
    if data is not None:
        d["raw_hex"] = data[el.offset:el.end].hex()
    return d
