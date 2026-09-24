"""EBML 基础原语：VINT、元素头、未知长度、元素树。

约定：
- 所有偏移均为文件绝对字节偏移（单位 byte，基准：文件起点）。
- 每个元素保留四段原始字节范围：id / size / payload / full。
- 未知长度（unknown-size）元素的 size 记为 None，范围延伸到父元素末尾，
  并保留诊断，绝不为了继续解析而伪造长度。
"""
from __future__ import annotations

from dataclasses import dataclass, field


class EbmlError(Exception):
    """EBML 原始层错误（截断、非法 VINT 等）。"""


def read_vint(data: bytes, offset: int, strip_marker: bool = True):
    """读取 EBML 变长整数。返回 (value, length, unknown, raw_bytes)。"""
    if offset >= len(data):
        raise EbmlError(f"vint@{offset}: 数据结束")
    first = data[offset]
    if first == 0:
        raise EbmlError(f"vint@{offset}: 前导字节 0x00 非法")
    mask = 0x80
    length = 1
    while length <= 8 and not (first & mask):
        mask >>= 1
        length += 1
    if length > 8 or offset + length > len(data):
        raise EbmlError(f"vint@{offset}: 截断")
    raw = data[offset:offset + length]
    value = int.from_bytes(raw, "big")
    if strip_marker:
        value &= (1 << (7 * length)) - 1
    unknown = value == (1 << (7 * length)) - 1
    return value, length, unknown, raw


def read_vint_signed(data: bytes, offset: int):
    """EBML 有符号 VINT（用于 EBML-lacing 的尺寸差分）。

    偏移基准为 (1 << (7*length - 1)) - 1。
    """
    value, length, _unknown, raw = read_vint(data, offset, strip_marker=True)
    value -= (1 << (7 * length - 1)) - 1
    return value, length, raw


def encode_vint(value: int, length: int | None = None) -> bytes:
    if length is None:
        for cand in range(1, 9):
            if value < (1 << (7 * cand)):
                length = cand
                break
        else:
            raise EbmlError("数值超出 8 字节 VINT 容量")
    if value >= (1 << (7 * length)):
        raise EbmlError("数值放不进指定长度的 VINT")
    return (value | (1 << (7 * length))).to_bytes(length, "big")


def encode_unknown_size(length: int = 8) -> bytes:
    """未知长度尺寸字段：marker 置位且全部值位为 1。"""
    return ((1 << (7 * length + 1)) - 1).to_bytes(length, "big")


def encode_element_id(element_id: int) -> bytes:
    for length in (1, 2, 3, 4):
        if element_id < (1 << (8 * length)):
            return element_id.to_bytes(length, "big")
    raise EbmlError("元素 ID 过大")


ELEMENT_NAMES = {
    0x1A45DFA3: "EBML", 0x4286: "EBMLVersion", 0x42F7: "EBMLReadVersion",
    0x42F2: "EBMLMaxIDLength", 0x42F3: "EBMLMaxSizeLength", 0x4282: "DocType",
    0x4287: "DocTypeVersion", 0x4285: "DocTypeReadVersion",
    0x18538067: "Segment", 0x1549A966: "Info", 0x2AD7B1: "TimecodeScale",
    0x4D80: "MuxingApp", 0x5741: "WritingApp", 0x4489: "Duration",
    0x1654AE6B: "Tracks", 0xAE: "TrackEntry", 0xD7: "TrackNumber",
    0x73C5: "TrackUID", 0x83: "TrackType", 0x86: "CodecID",
    0x536E: "Name", 0x22B59C: "Language", 0x23E383: "DefaultDuration",
    0x23314F: "TrackTimecodeScale", 0x9C: "FlagLacing",
    0x1F43B675: "Cluster", 0xE7: "Timestamp", 0xA3: "SimpleBlock",
    0xA0: "BlockGroup", 0xA1: "Block", 0x9B: "BlockDuration",
    0x75A2: "DiscardPadding", 0xFB: "ReferenceBlock",
    0x1C53BB6B: "Cues", 0xBB: "CuePoint", 0xB3: "CueTime",
    0xB7: "CueTrackPositions", 0xF7: "CueTrack", 0xF1: "CueClusterPosition",
    0xF0: "CueRelativePosition", 0x5378: "CueBlockNumber",
    0xEC: "Void", 0xBF: "CRC-32",
}

MASTER_IDS = {
    0x1A45DFA3, 0x18538067, 0x1549A966, 0x1654AE6B, 0xAE,
    0x1F43B675, 0xA0, 0x1C53BB6B, 0xBB, 0xB7,
}


@dataclass
class Element:
    eid: str                 # 稳定身份：el:<offset>:<id-hex>
    element_id: int
    name: str
    offset: int              # 元素起始（id 首字节），单位 byte
    id_len: int
    size_len: int
    size: int | None         # None = 未知长度
    unknown_size: bool
    payload_offset: int
    end: int                 # 元素结束（不含），未知长度时延伸到父末尾
    depth: int
    parent: int              # 父元素在列表中的下标，-1 为顶层
    children: list[int] = field(default_factory=list)
    truncated: bool = False

    @property
    def header_len(self) -> int:
        return self.id_len + self.size_len

    def ranges(self) -> dict:
        """原始字节范围（单位 byte，半开区间 [start, end)）。"""
        return {
            "id": [self.offset, self.offset + self.id_len],
            "size": [self.offset + self.id_len, self.offset + self.header_len],
            "payload": [self.payload_offset, self.end],
            "full": [self.offset, self.end],
        }

    def to_dict(self) -> dict:
        return {
            "eid": self.eid,
            "element_id": f"0x{self.element_id:X}",
            "name": self.name,
            "offset": {"value": self.offset, "unit": "byte", "basis": "file"},
            "size": None if self.size is None else {"value": self.size, "unit": "byte"},
            "unknown_size": self.unknown_size,
            "depth": self.depth,
            "parent": self.parent,
            "children": self.children,
            "truncated": self.truncated,
            "ranges": self.ranges(),
        }


def parse_elements(data: bytes, start: int, end: int, depth: int,
                   out: list[Element], parent: int,
                   diagnostics: list[dict]) -> None:
    """把 [start, end) 区间解析为兄弟元素序列，递归进入 master 元素。"""
    pos = start
    while pos < end:
        el_start = pos
        try:
            element_id, id_len, _u1, _r1 = read_vint(data, pos, strip_marker=False)
        except EbmlError as exc:
            diagnostics.append({"offset": pos, "error": str(exc), "kind": "element-id"})
            return
        pos += id_len
        try:
            size_value, size_len, unknown, _r2 = read_vint(data, pos, strip_marker=True)
        except EbmlError as exc:
            diagnostics.append({"offset": pos, "error": str(exc), "kind": "element-size"})
            return
        pos += size_len
        payload_offset = pos
        truncated = False
        if unknown:
            size = None
            el_end = end  # 未知长度：延伸到父元素末尾
        else:
            size = size_value
            el_end = payload_offset + size
            if el_end > end:
                el_end = end
                truncated = True
                diagnostics.append({
                    "offset": el_start,
                    "error": f"元素声明大小 {size}B 超出父边界，按截断处理",
                    "kind": "truncated",
                })
        eid = f"el:{el_start:08x}:{element_id:X}"
        idx = len(out)
        out.append(Element(
            eid=eid, element_id=element_id,
            name=ELEMENT_NAMES.get(element_id, f"Unknown-{element_id:X}"),
            offset=el_start, id_len=id_len, size_len=size_len, size=size,
            unknown_size=unknown, payload_offset=payload_offset, end=el_end,
            depth=depth, parent=parent, truncated=truncated,
        ))
        if parent >= 0:
            out[parent].children.append(idx)
        if element_id in MASTER_IDS and el_end > payload_offset:
            parse_elements(data, payload_offset, el_end, depth + 1, out, idx, diagnostics)
        pos = el_end
        if el_end <= el_start:  # 防御：零长元素避免死循环
            diagnostics.append({"offset": el_start, "error": "零长元素", "kind": "zero-length"})
            break


def parse_file(data: bytes):
    """解析整个文件，返回 (elements, diagnostics)。"""
    out: list[Element] = []
    diagnostics: list[dict] = []
    parse_elements(data, 0, len(data), 0, out, -1, diagnostics)
    return out, diagnostics
