"""EBML/Matroska 解析层。

只负责原始结构：元素偏移、未知长度、原始字节范围全部保留；
不做任何时间规范化（那是派生层的事）。
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field


class EbmlError(Exception):
    pass


SEGMENT_ID = 0x18538067
CLUSTER_ID = 0x1F43B675

SCHEMA = {
    0x1A45DFA3: ("EBML", "master"),
    0x4286: ("EBMLVersion", "uint"),
    0x42F7: ("EBMLReadVersion", "uint"),
    0x42F2: ("EBMLMaxIDLength", "uint"),
    0x42F3: ("EBMLMaxSizeLength", "uint"),
    0x4282: ("DocType", "string"),
    0x4287: ("DocTypeVersion", "uint"),
    0x18538067: ("Segment", "master"),
    0x114D9B74: ("SeekHead", "master"),
    0x1549A966: ("Info", "master"),
    0x2AD7B1: ("TimecodeScale", "uint"),
    0x4D80: ("MuxingApp", "string"),
    0x5741: ("WritingApp", "string"),
    0x4489: ("Duration", "float"),
    0x1654AE6B: ("Tracks", "master"),
    0xAE: ("TrackEntry", "master"),
    0xD7: ("TrackNumber", "uint"),
    0x73C5: ("TrackUID", "uint"),
    0x83: ("TrackType", "uint"),
    0x9C: ("FlagLacing", "uint"),
    0x536E: ("Name", "string"),
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
    0xFB: ("ReferenceBlock", "int"),
    0x1C53BB6B: ("Cues", "master"),
    0xBB: ("CuePoint", "master"),
    0xB3: ("CueTime", "uint"),
    0xB7: ("CueTrackPositions", "master"),
    0xF7: ("CueTrack", "uint"),
    0xF1: ("CueClusterPosition", "uint"),
    0x5378: ("CueBlockNumber", "uint"),
}

# 未知长度 Cluster 的结束裁定：遇到这些同级 ID 即停止。
CLUSTER_STOP_IDS = frozenset(
    {SEGMENT_ID, CLUSTER_ID, 0x1549A966, 0x1654AE6B, 0x1C53BB6B, 0x114D9B74}
)


def read_vint(buf, pos, keep_marker=False):
    """读取 EBML 变长整数。返回 (value, length, unknown)。"""
    if pos >= len(buf):
        raise EbmlError(f"vint 越界 @{pos}")
    first = buf[pos]
    mask = 0x80
    length = 1
    while length <= 8 and not (first & mask):
        mask >>= 1
        length += 1
    if length > 8 or pos + length > len(buf):
        raise EbmlError(f"非法 vint @{pos}")
    value = first if keep_marker else (first & (mask - 1))
    for i in range(1, length):
        value = (value << 8) | buf[pos + i]
    unknown = (not keep_marker) and value == (1 << (7 * length)) - 1
    return value, length, unknown


@dataclass
class Element:
    eid: int
    name: str
    etype: str
    offset: int
    header_size: int
    data_size: int | None  # None = 未知长度
    data_offset: int
    end: int | None
    depth: int
    value: object = None
    raw_hex: str = ""
    children: list = field(default_factory=list)

    @property
    def uid(self):
        return f"el@{self.offset}"

    @property
    def unknown_size(self):
        return self.data_size is None


def _decode_leaf(etype, data):
    if etype == "uint":
        return int.from_bytes(data, "big")
    if etype == "int":
        return int.from_bytes(data, "big", signed=True)
    if etype == "float":
        if len(data) == 4:
            return struct.unpack(">f", data)[0]
        if len(data) == 8:
            return struct.unpack(">d", data)[0]
        return None
    if etype == "string":
        return data.decode("utf-8", errors="replace")
    return None


def parse_elements(buf, start, end, depth, stop_ids, diagnostics):
    elements = []
    pos = start
    while pos < end:
        try:
            eid, id_len = read_vint(buf, pos, keep_marker=True)
        except EbmlError as exc:
            diagnostics.append({"kind": "parse_stop", "offset": pos, "detail": str(exc)})
            break
        if eid in stop_ids:
            break
        try:
            size, size_len, unknown = read_vint(buf, pos + id_len)
        except EbmlError as exc:
            diagnostics.append({"kind": "parse_stop", "offset": pos, "detail": str(exc)})
            break
        name, etype = SCHEMA.get(eid, (f"Unknown-{eid:X}", "binary"))
        header = id_len + size_len
        data_off = pos + header
        el = Element(eid, name, etype, pos, header, None if unknown else size,
                     data_off, None, depth)
        if etype == "master":
            child_end = end if unknown else min(data_off + size, end)
            child_stop = CLUSTER_STOP_IDS if (eid == CLUSTER_ID and unknown) else frozenset()
            children, consumed = parse_elements(buf, data_off, child_end, depth + 1,
                                                child_stop, diagnostics)
            el.children = children
            el.end = consumed if unknown else child_end
        else:
            el_end = data_off + size
            if el_end > end:
                diagnostics.append({"kind": "truncated_element", "offset": pos,
                                    "detail": f"{name} 数据超出父元素边界"})
                el_end = end
            el.end = el_end
            data = buf[data_off:el_end]
            el.raw_hex = data[:32].hex()
            el.value = _decode_leaf(etype, data)
        elements.append(el)
        pos = el.end if el.end and el.end > pos else data_off + 1
    return elements, pos


def parse_document(buf):
    diagnostics = []
    elements, _ = parse_elements(buf, 0, len(buf), 0, frozenset(), diagnostics)
    return elements, diagnostics


def flatten(elements):
    out = []

    def walk(el):
        out.append(el)
        for c in el.children:
            walk(c)

    for e in elements:
        walk(e)
    return out


def find_first(elements, name):
    for e in flatten(elements):
        if e.name == name:
            return e
    return None


def find_all(elements, name):
    return [e for e in flatten(elements) if e.name == name]


def element_dict(el):
    return {
        "uid": el.uid,
        "id_hex": f"0x{el.eid:X}",
        "name": el.name,
        "type": el.etype,
        "offset": {"value": el.offset, "unit": "byte", "basis": "file"},
        "header_size": el.header_size,
        "data_size": None if el.unknown_size else {"value": el.data_size, "unit": "byte"},
        "unknown_size": el.unknown_size,
        "end": None if el.end is None else {"value": el.end, "unit": "byte", "basis": "file"},
        "depth": el.depth,
        "value": el.value,
        "raw_hex": el.raw_hex,
        "byte_range": {"start": el.offset, "end": el.end, "basis": "file"},
        "children": [element_dict(c) for c in el.children],
    }
