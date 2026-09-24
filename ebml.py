"""Minimal EBML parser.

Every element keeps its byte offset, header layout, unknown-size flag and the
raw byte range it occupies, so the review UI can always trace a derived value
back to the exact bytes it came from.
"""
from __future__ import annotations

from dataclasses import dataclass, field

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
    0x1654AE6B: "Tracks",
    0xAE: "TrackEntry",
    0xD7: "TrackNumber",
    0x83: "TrackType",
    0x23E383: "DefaultDuration",
    0x536E: "Name",
    0x1F43B675: "Cluster",
    0xE7: "Timecode",
    0xA0: "BlockGroup",
    0xA1: "Block",
    0xA3: "SimpleBlock",
    0x75A2: "DiscardPadding",
    0x1C53BB6B: "Cues",
    0xBB: "CuePoint",
    0xB3: "CueTime",
    0xB7: "CueTrackPositions",
    0xF7: "CueTrack",
    0xF1: "CueClusterPosition",
    0xEC: "Void",
}

MASTER_IDS = {
    0x1A45DFA3, 0x18538067, 0x1549A966, 0x1654AE6B, 0xAE,
    0x1F43B675, 0xA0, 0x1C53BB6B, 0xBB, 0xB7,
}
UINT_IDS = {
    0x4286, 0x42F7, 0x42F2, 0x42F3, 0x4287, 0x4285,
    0x2AD7B1, 0xD7, 0x83, 0x23E383, 0xE7, 0xB3, 0xF7, 0xF1,
}
SINT_IDS = {0x75A2}
STRING_IDS = {0x4282, 0x536E}
BINARY_IDS = {0xA1, 0xA3}


class EbmlError(Exception):
    pass


def read_vint(buf: bytes, off: int):
    """Read a variable-length integer. Returns (raw_with_marker, value, length)."""
    if off >= len(buf):
        raise EbmlError("vint read past end of buffer")
    first = buf[off]
    mask = 0x80
    length = 1
    while length <= 8 and not (first & mask):
        mask >>= 1
        length += 1
    if length > 8:
        raise EbmlError(f"invalid vint at offset {off}")
    raw = int.from_bytes(buf[off:off + length], "big")
    value = raw & ((1 << (7 * length)) - 1)
    return raw, value, length


@dataclass
class Element:
    eid: int
    offset: int
    id_len: int
    size_len: int
    data_offset: int
    data_size: int | None
    unknown_size: bool
    parent_end: int
    path: str = ""
    children: list = field(default_factory=list)
    value: object = None
    data: bytes = b""

    @property
    def name(self) -> str:
        return ELEMENT_NAMES.get(self.eid, f"Unknown(0x{self.eid:x})")

    @property
    def header_size(self) -> int:
        return self.id_len + self.size_len

    @property
    def end(self) -> int:
        if self.unknown_size:
            return self.parent_end
        return self.data_offset + (self.data_size or 0)


def _decode_leaf(el: Element, buf: bytes) -> None:
    data = buf[el.data_offset:el.end]
    el.data = data
    if el.eid in UINT_IDS:
        el.value = int.from_bytes(data, "big") if data else 0
    elif el.eid in SINT_IDS:
        el.value = int.from_bytes(data, "big", signed=True) if data else 0
    elif el.eid in STRING_IDS:
        el.value = data.decode("utf-8", "replace")
    else:
        el.value = None


def parse_element(buf: bytes, off: int, parent_end: int, path: str) -> Element:
    raw_id, _, id_len = read_vint(buf, off)
    _, size_val, size_len = read_vint(buf, off + id_len)
    unknown = size_val == (1 << (7 * size_len)) - 1
    data_offset = off + id_len + size_len
    el = Element(
        eid=raw_id, offset=off, id_len=id_len, size_len=size_len,
        data_offset=data_offset, data_size=None if unknown else size_val,
        unknown_size=unknown, parent_end=parent_end, path=path,
    )
    end = el.end
    if raw_id in MASTER_IDS:
        idx = 0
        pos = data_offset
        while pos < end:
            child_raw, _, _ = read_vint(buf, pos)
            child_name = ELEMENT_NAMES.get(child_raw, f"Unknown(0x{child_raw:x})")
            child = parse_element(buf, pos, end, f"{path}/{child_name}[{idx}]")
            el.children.append(child)
            pos = child.end
            idx += 1
    else:
        _decode_leaf(el, buf)
    return el


def parse_document(buf: bytes) -> list[Element]:
    out = []
    pos = 0
    idx = 0
    while pos < len(buf):
        raw_id, _, _ = read_vint(buf, pos)
        name = ELEMENT_NAMES.get(raw_id, f"Unknown(0x{raw_id:x})")
        el = parse_element(buf, pos, len(buf), f"/{name}[{idx}]")
        out.append(el)
        pos = el.end
        idx += 1
    return out


def element_to_dict(el: Element) -> dict:
    return {
        "id": el.path,
        "name": el.name,
        "eid": f"0x{el.eid:x}",
        "offset": el.offset,
        "end": el.end,
        "header_size": el.header_size,
        "data_offset": el.data_offset,
        "data_size": el.data_size,
        "unknown_size": el.unknown_size,
        "raw_range": [el.offset, el.end],
        "raw_hex": el.data.hex() if not el.children else "",
        "value": el.value,
        "children": [element_to_dict(c) for c in el.children],
    }


# ---- encoder helpers (used to build repo fixtures deterministically) ----

def encode_id(n: int) -> bytes:
    length = max(1, (n.bit_length() + 7) // 8)
    return n.to_bytes(length, "big")


def encode_size(n: int, length: int | None = None) -> bytes:
    if length is None:
        length = 1
        while n >= (1 << (7 * length)) - 1:
            length += 1
    return ((1 << (7 * length)) | n).to_bytes(length, "big")


def el(eid: int, data: bytes, size_len: int | None = None) -> bytes:
    return encode_id(eid) + encode_size(len(data), size_len) + data


def el_unknown_size(eid: int, data: bytes) -> bytes:
    return encode_id(eid) + b"\xff" + data


def uint_el(eid: int, value: int, length: int | None = None) -> bytes:
    if length is None:
        length = max(1, (value.bit_length() + 7) // 8)
    return el(eid, value.to_bytes(length, "big"))


def sint_el(eid: int, value: int, length: int) -> bytes:
    return el(eid, value.to_bytes(length, "big", signed=True))


def str_el(eid: int, text: str) -> bytes:
    return el(eid, text.encode("utf-8"))
