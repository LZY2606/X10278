"""EBML/Matroska structural parser (source layer).

Preserves exact byte offsets, header sizes and unknown-size lengths.
Nothing is normalized here: values keep their raw representation, and
damaged regions are recorded as diagnostics with byte ranges instead of
being repaired.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import struct

ELEMENT_NAMES = {
    0x1A45DFA3: "EBML",
    0x4286: "EBMLVersion",
    0x42F7: "EBMLReadVersion",
    0x42F2: "EBMLMaxIDLength",
    0x42F3: "EBMLMaxSizeLength",
    0x4282: "DocType",
    0x4287: "DocTypeVersion",
    0x4285: "DocTypeReadVersion",
    0x18538067: "Segment",
    0x1549A966: "Info",
    0x2AD7B1: "TimecodeScale",
    0x4D80: "MuxingApp",
    0x5741: "WritingApp",
    0x4489: "Duration",
    0x1654AE6B: "Tracks",
    0xAE: "TrackEntry",
    0xD7: "TrackNumber",
    0x73C5: "TrackUID",
    0x83: "TrackType",
    0x86: "CodecID",
    0x23314F: "TrackTimecodeScale",
    0x23E383: "DefaultDuration",
    0x1F43B675: "Cluster",
    0xE7: "Timecode",
    0xA3: "SimpleBlock",
    0xA0: "BlockGroup",
    0xA1: "Block",
    0x9B: "BlockDuration",
    0x75A2: "DiscardPadding",
    0x1C53BB6B: "Cues",
    0xBB: "CuePoint",
    0xB3: "CueTime",
    0xB7: "CueTrackPositions",
    0xF7: "CueTrack",
    0xF1: "CueClusterPosition",
}

MASTER_IDS = {
    0x1A45DFA3, 0x18538067, 0x1549A966, 0x1654AE6B, 0xAE,
    0x1F43B675, 0xA0, 0x1C53BB6B, 0xBB, 0xB7,
}

# IDs that terminate an unknown-size master element when seen at child level.
SEGMENT_CHILD_STOP = {
    0x18538067, 0x1549A966, 0x1654AE6B, 0x1C53BB6B, 0x1F43B675,
    0x1043A770, 0x114D9B74, 0x1A45DFA3,
}
CLUSTER_STOP = SEGMENT_CHILD_STOP

UINT_IDS = {
    0x4286, 0x42F7, 0x42F2, 0x42F3, 0x4287, 0x4285,
    0x2AD7B1, 0xD7, 0x73C5, 0x83, 0x23E383, 0xE7, 0x9B,
    0xB3, 0xF7, 0xF1,
}
INT_IDS = {0x75A2}
FLOAT_IDS = {0x23314F, 0x4489}
STRING_IDS = {0x4282, 0x86, 0x4D80, 0x5741}


class ParseError(Exception):
    pass


def read_id(buf: bytes, pos: int) -> tuple[int, int]:
    """Read an element ID (VINT with marker bits kept)."""
    if pos >= len(buf):
        raise ParseError("unexpected end of buffer while reading element id")
    first = buf[pos]
    if first == 0:
        raise ParseError(f"invalid element id at offset {pos}")
    length = 1
    mask = 0x80
    while not (first & mask):
        mask >>= 1
        length += 1
        if length > 4:
            raise ParseError(f"element id longer than 4 bytes at offset {pos}")
    if pos + length > len(buf):
        raise ParseError(f"truncated element id at offset {pos}")
    return int.from_bytes(buf[pos:pos + length], "big"), length


def read_size(buf: bytes, pos: int) -> tuple[int | None, int, bool]:
    """Read an element size VINT. Returns (value, length, unknown)."""
    if pos >= len(buf):
        raise ParseError("unexpected end of buffer while reading element size")
    first = buf[pos]
    if first == 0:
        raise ParseError(f"invalid element size at offset {pos}")
    length = 1
    mask = 0x80
    while not (first & mask):
        mask >>= 1
        length += 1
        if length > 8:
            raise ParseError(f"element size longer than 8 bytes at offset {pos}")
    if pos + length > len(buf):
        raise ParseError(f"truncated element size at offset {pos}")
    raw = int.from_bytes(buf[pos:pos + length], "big")
    value = raw & ((1 << (7 * length)) - 1)
    unknown = value == (1 << (7 * length)) - 1
    return (None if unknown else value), length, unknown


@dataclass
class Element:
    eid: int
    offset: int
    id_len: int
    size_len: int
    data_offset: int
    data_size: int | None
    end: int
    unknown_size: bool = False
    value: object = None
    children: list["Element"] = field(default_factory=list)
    path: str = ""

    @property
    def name(self) -> str:
        return ELEMENT_NAMES.get(self.eid, f"Unknown(0x{self.eid:x})")

    @property
    def header_size(self) -> int:
        return self.id_len + self.size_len

    @property
    def is_master(self) -> bool:
        return self.eid in MASTER_IDS

    def find(self, eid: int) -> "Element | None":
        for child in self.children:
            if child.eid == eid:
                return child
        return None

    def find_all(self, eid: int) -> list["Element"]:
        return [c for c in self.children if c.eid == eid]

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "id": f"0x{self.eid:X}",
            "name": self.name,
            "offset": self.offset,
            "header_size": self.header_size,
            "data_offset": self.data_offset,
            "data_size": self.data_size,
            "end": self.end,
            "unknown_size": self.unknown_size,
            "value": self.value,
            "children": [c.to_dict() for c in self.children],
        }


@dataclass
class Document:
    elements: list[Element]
    diagnostics: list[dict]
    length: int

    def iter_elements(self):
        stack = list(self.elements)
        while stack:
            el = stack.pop(0)
            yield el
            stack[0:0] = el.children

    def find_by_path(self, path: str) -> Element | None:
        node = None
        children = self.elements
        for part in path.split("/"):
            idx = int(part)
            if idx >= len(children):
                return None
            node = children[idx]
            children = node.children
        return node


def _decode_leaf(el: Element, buf: bytes) -> None:
    data = buf[el.data_offset:el.end]
    try:
        if el.eid in UINT_IDS:
            el.value = int.from_bytes(data, "big") if data else 0
        elif el.eid in INT_IDS:
            el.value = int.from_bytes(data, "big", signed=True) if data else 0
        elif el.eid in FLOAT_IDS:
            if len(data) == 4:
                el.value = struct.unpack(">f", data)[0]
            elif len(data) == 8:
                el.value = struct.unpack(">d", data)[0]
            else:
                el.value = None
        elif el.eid in STRING_IDS:
            el.value = data.decode("utf-8", errors="replace")
    except (ValueError, struct.error):
        el.value = None


def _parse_children(
    buf: bytes,
    start: int,
    end: int,
    stop_ids: set[int],
    diagnostics: list[dict],
    path_prefix: str,
) -> tuple[list[Element], int]:
    elements: list[Element] = []
    pos = start
    index = 0
    while pos < end:
        el_offset = pos
        path = f"{path_prefix}/{index}" if path_prefix else str(index)
        try:
            eid, id_len = read_id(buf, pos)
        except ParseError as exc:
            diagnostics.append({
                "offset": el_offset,
                "length": max(1, end - el_offset),
                "reason": f"unparseable element id: {exc}",
                "element_path": path,
            })
            break
        if eid in stop_ids:
            break
        try:
            size_val, size_len, unknown = read_size(buf, pos + id_len)
        except ParseError as exc:
            diagnostics.append({
                "offset": el_offset,
                "length": max(1, end - el_offset),
                "reason": f"unparseable element size: {exc}",
                "element_path": path,
            })
            break
        data_offset = el_offset + id_len + size_len
        if eid in MASTER_IDS:
            if unknown:
                child_stop = CLUSTER_STOP if eid == 0x1F43B675 else SEGMENT_CHILD_STOP
                children, child_end = _parse_children(
                    buf, data_offset, end, child_stop, diagnostics, path)
                el_end = child_end
                data_size = None
            else:
                el_end = min(data_offset + size_val, len(buf))
                if data_offset + size_val > len(buf):
                    diagnostics.append({
                        "offset": el_offset,
                        "length": len(buf) - el_offset,
                        "reason": "master element extends past end of buffer (truncated)",
                        "element_path": path,
                    })
                children, _ = _parse_children(buf, data_offset, el_end, set(), diagnostics, path)
                data_size = size_val
            elements.append(Element(
                eid=eid, offset=el_offset, id_len=id_len, size_len=size_len,
                data_offset=data_offset, data_size=data_size, end=el_end,
                unknown_size=unknown, children=children, path=path,
            ))
        else:
            if unknown:
                diagnostics.append({
                    "offset": el_offset,
                    "length": max(1, end - data_offset),
                    "reason": "unknown-size leaf element; data extends to parent end",
                    "element_path": path,
                })
                el_end = end
                data_size = None
            else:
                el_end = data_offset + size_val
                data_size = size_val
                if el_end > len(buf):
                    diagnostics.append({
                        "offset": el_offset,
                        "length": len(buf) - el_offset,
                        "reason": "leaf element extends past end of buffer (truncated)",
                        "element_path": path,
                    })
                    el_end = len(buf)
            el = Element(
                eid=eid, offset=el_offset, id_len=id_len, size_len=size_len,
                data_offset=data_offset, data_size=data_size, end=el_end,
                unknown_size=unknown, path=path,
            )
            _decode_leaf(el, buf)
            elements.append(el)
        if el_end <= el_offset:  # safety: never loop forever
            diagnostics.append({
                "offset": el_offset,
                "length": 1,
                "reason": "zero-length parse step; aborting level",
                "element_path": path,
            })
            break
        pos = el_end
        index += 1
    return elements, pos


def parse(buf: bytes) -> Document:
    diagnostics: list[dict] = []
    elements, _ = _parse_children(buf, 0, len(buf), set(), diagnostics, "")
    return Document(elements=elements, diagnostics=diagnostics, length=len(buf))
