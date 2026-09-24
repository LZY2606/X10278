"""EBML / Matroska primitives: VINT, element table, parser and builder.

The parser is intentionally a strict evidence-preserving parser: it records
absolute byte offsets, header sizes, unknown-length markers and raw byte
ranges. Unknown-length master elements are bounded by permitted-child grammar
(see CHILDREN). Nothing about time truth is decided here.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional

# ---- Matroska element identifiers (id hex -> name) -------------------------
NAMES = {
    "1a45dfa3": "EBML",
    "4286": "EBMLVersion",
    "42f7": "EBMLReadVersion",
    "42f2": "EBMLMaxIDLength",
    "42f3": "EBMLMaxSizeLength",
    "4282": "DocType",
    "4287": "DocTypeVersion",
    "4285": "DocTypeReadVersion",
    "18538067": "Segment",
    "1549a966": "Info",
    "114d9b74": "SeekHead",
    "2ad7b1": "TimecodeScale",
    "4d80": "MuxingApp",
    "5741": "WritingApp",
    "4489": "Duration",
    "53ab": "SeekID",
    "53ac": "SeekPosition",
    "1654ae6b": "Tracks",
    "ae": "TrackEntry",
    "d7": "TrackNumber",
    "73c5": "TrackUID",
    "83": "TrackType",
    "86": "CodecID",
    "23e383": "DefaultDuration",
    "23314f": "TrackTimecodeScale",
    "234e7a": "TrackOffset",
    "9c": "FlagLacing",
    "e0": "Video",
    "b0": "PixelWidth",
    "ba": "PixelHeight",
    "e1": "Audio",
    "b5": "SamplingFrequency",
    "1f43b675": "Cluster",
    "e7": "Timecode",
    "a0": "BlockGroup",
    "a1": "Block",
    "a2": "BlockVirtual",
    "9b": "BlockDuration",
    "fa": "ReferenceBlock",
    "fb": "ReferencePriority",
    "75a1": "DiscardPadding",
    "a3": "SimpleBlock",
    "1c53bb6b": "Cues",
    "bb": "CuePoint",
    "b3": "CueTime",
    "b7": "CueTrackPositions",
    "f7": "CueTrack",
    "f1": "CueClusterPosition",
    "f0": "CueRelativePosition",
    "5378": "CueDuration",
    "1254c367": "Tags",
    "7373": "Tag",
    "67c8": "SimpleTag",
    "45a3": "TagName",
    "4487": "TagString",
    "2ad7b1": "TimecodeScale",
    "4d80": "MuxingApp",
    "5741": "WritingApp",
    "4489": "Duration",
    "1043a770": "Chapters",
    "1549a966": "Info",
    "114d9b74": "SeekHead",
}

# Master element -> permitted direct children (used to bound unknown length).
CHILDREN = {
    "1a45dfa3": {"4286", "42f7", "42f2", "42f3", "4282", "4287", "4285"},
    "18538067": {
        "1549a966", "114d9b74", "1654ae6b", "1f43b675", "1c53bb6b",
        "1254c367", "1043a770",
    },
    "1549a966": {"2ad7b1", "4d80", "5741", "4489"},
    "1654ae6b": {"ae"},
    "ae": {
        "d7", "73c5", "83", "86", "23e383", "23314f", "234e7a", "9c",
        "e0", "e1",
    },
    "e0": {"b0", "ba"},
    "e1": {"b5"},
    "1f43b675": {"e7", "a0", "a3"},
    "a0": {"a1", "a2", "9b", "fa", "fb", "75a1"},
    "1c53bb6b": {"bb"},
    "bb": {"b3", "b7"},
    "b7": {"f7", "f1", "f0", "5378"},
    "1254c367": {"7373"},
    "7373": {"67c8"},
    "67c8": {"45a3", "4487"},
}
CHILDREN["114d9b74"] = {"114d9b74"}
CHILDREN["114d9b74"] = {"53ab", "53ac"}

MASTER_IDS = set(CHILDREN.keys())
BLOCK_GROUP_ID = "a0"
SIMPLE_BLOCK_ID = "a3"
BLOCK_ID = "a1"
CLUSTER_ID = "1f43b675"
SEGMENT_ID = "18538067"


class EbmlError(Exception):
    pass


@dataclass
class Vint:
    raw: int
    length: int
    marker_pos: int          # 1-based index of the VINT width marker
    unknown: bool = False    # size reserved all-ones-with-marker => unknown

    @property
    def value(self) -> Optional[int]:
        return None if self.unknown else self.raw


def read_vint(buf: bytes, pos: int, is_id: bool = False) -> Vint:
    """Read one EBML VINT.

    Width is fixed by the first set bit (0b1000_0000 -> 1 byte, etc.).
    0x00 (no set bit) is illegal. For sizes, the all-data-bits-set pattern of
    the *actual* width means unknown (FF, 7FFF, 3FFFFF, 1FFFFFFF, ...).
    """
    if pos >= len(buf):
        raise EbmlError(f"truncated vint at {pos}")
    first = buf[pos]
    width = 0
    for i in range(8):
        if first & (0x80 >> i):
            width = i + 1
            break
    if width == 0:
        raise EbmlError(f"invalid vint marker (0x00) at {pos}")
    if pos + width > len(buf):
        raise EbmlError(f"truncated {width}-byte vint at {pos}")
    raw = int.from_bytes(buf[pos:pos + width], "big")
    data_bits = width * 8 - width
    data_mask = (1 << data_bits) - 1
    value = raw & data_mask
    unknown = (not is_id) and value == data_mask and width >= 1
    return Vint(raw=raw if is_id else value, length=width,
                marker_pos=width, unknown=unknown)



def encode_vint(value: int, width: Optional[int] = None,
                unknown: bool = False) -> bytes:
    if unknown:
        return _unknown_bytes(width or 1)
    if width is None:
        # smallest canonical width; all-ones data pattern reserved for unknown
        width = 1
        while value > (1 << (8 * width - width)) - 2 and width < 8:
            width += 1
    data_bits = 8 * width - width
    marker = 1 << data_bits
    head = marker | (value & ((1 << data_bits) - 1))
    return head.to_bytes(width, "big")



def _unknown_bytes(width: int) -> bytes:
    # marker bit set plus all data-bits set: e.g. width1 0xFF, width2 0x7FFF
    return ((1 << (8 * width)) - 1).to_bytes(width, "big")


def encode_id(eid: str) -> bytes:
    return bytes.fromhex(eid)


def uint_bytes(value: int) -> bytes:
    length = max(1, (value.bit_length() + 7) // 8)
    return value.to_bytes(length, "big")


def build_element(eid: str, payload: bytes) -> bytes:
    return encode_id(eid) + encode_vint(len(payload)) + payload


def element_with_unknown_size(eid: str, payload: bytes, width: int = 1) -> bytes:
    return encode_id(eid) + _unknown_bytes(width) + payload


SIGNED_INT_IDS = {"75a1", "fa", "234e7a"}  # DiscardPadding, ReferenceBlock, TrackOffset
STRING_IDS = {"4282", "4487", "45a3"}  # DocType, TagString, TagName
FLOAT_IDS = {"b5", "23314f", "23314f00"}  # SamplingFrequency, TrackTimecodeScale


@dataclass
class Element:
    id: str
    name: str
    start: int
    id_len: int
    size_len: int
    data_start: int
    data_end: int
    end: int
    unknown_size: bool = False
    declared_size: Optional[int] = None
    value: object = None          # typed leaf value
    raw: Optional[str] = None     # hex payload (bounded) for leaves
    children: list = field(default_factory=list)
    diagnostics: list = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


def _leaf_value(eid: str, data: bytes) -> object:
    if eid in FLOAT_IDS:
        import struct as _st
        if len(data) == 4:
            return _st.unpack(">f", data)[0]
        if len(data) == 8:
            return _st.unpack(">d", data)[0]
        return None
    if eid in STRING_IDS:
        try:
            return data.decode("utf-8").rstrip("\x00")
        except UnicodeDecodeError:
            return data.hex()
    if not data:
        return 0
    if eid in SIGNED_INT_IDS:
        return int.from_bytes(data, "big", signed=True)
    return int.from_bytes(data, "big", signed=False)


def _parse_element(buf: bytes, pos: int, limit: int, depth: int) -> Element:
    idv = read_vint(buf, pos, is_id=True)
    eid = buf[pos:pos + idv.length].hex()
    size_pos = pos + idv.length
    sv = read_vint(buf, size_pos, is_id=False)
    data_start = size_pos + sv.length
    unknown = sv.unknown
    declared = None if unknown else sv.value
    name = NAMES.get(eid, f"Unknown_{eid}")
    el = Element(
        id=eid, name=name, start=pos, id_len=idv.length, size_len=sv.length,
        data_start=data_start, data_end=data_start, end=data_start,
        unknown_size=unknown, declared_size=declared,
    )
    is_master = eid in MASTER_IDS
    if is_master:
        if unknown:
            data_end = _bound_unknown(buf, data_start, limit, eid, depth + 1, el)
        else:
            data_end = data_start + (declared or 0)
            if data_end > limit:
                el.diagnostics.append(
                    f"declared size overruns container: end {data_end} > {limit}")
                data_end = limit
        if not unknown:
            p = data_start
            while p < data_end:
                try:
                    child = _parse_element(buf, p, data_end, depth + 1)
                except EbmlError as exc:
                    el.diagnostics.append(f"child parse stop: {exc}")
                    break
                el.children.append(child)
                p = child.end
        # unknown-length children were already collected by _bound_unknown.
        el.data_end = data_end
        el.end = data_end
    else:
        data_end = limit if unknown else min(data_start + (declared or 0), limit)
        if unknown:
            el.diagnostics.append("unknown-size leaf treated as zero-bounded")
            data_end = data_start
        if not unknown and data_start + (declared or 0) > limit:
            el.diagnostics.append("leaf declared size overruns container")
        payload = buf[data_start:data_end]
        el.value = _leaf_value(eid, payload)
        el.raw = payload.hex()
        el.data_end = data_end
        el.end = data_end
    return el


def _bound_unknown(buf, pos, limit, eid, depth, el) -> int:
    permitted = CHILDREN.get(eid, set())
    p = pos
    while p < limit:
        try:
            idv = read_vint(buf, p, is_id=True)
        except EbmlError:
            el.diagnostics.append(f"unknown-length bound: bad id at {p}")
            break
        child_eid = buf[p:p + idv.length].hex()
        if child_eid not in permitted:
            break
        try:
            child = _parse_element(buf, p, limit, depth)
        except EbmlError as exc:
            el.diagnostics.append(f"unknown-length child stop: {exc}")
            break
        el.children.append(child)
        p = child.end
    return p


def parse_document(buf: bytes) -> list:
    """Parse top-level elements (EBML header + Segment). Returns roots."""
    roots: list[Element] = []
    pos = 0
    limit = len(buf)
    while pos < limit:
        el = _parse_element(buf, pos, limit, 0)
        roots.append(el)
        if el.end <= pos:
            break
        pos = el.end
    return roots


def find_all(root: Element, eid: str) -> list:
    out = []
    for c in root.children:
        if c.id == eid:
            out.append(c)
        out.extend(find_all(c, eid))
    return out


def first(root: Element, eid: str) -> Optional[Element]:
    res = find_all(root, eid)
    return res[0] if res else None
