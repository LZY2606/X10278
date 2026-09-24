"""Low-level EBML element model: variable-length integers, parsing, serialization.

Byte offsets and raw ranges are first-class data here.  Nothing in this module
knows about Matroska time semantics; see :mod:`mkvcourt.matroska`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

UNKNOWN_SIZE = -1
ID_HEADER = 0x1A45DFA3
ID_SEGMENT = 0x18538067
ID_INFO = 0x1549B9
ID_TIMESTAMP_SCALE = 0x2AD7B1
ID_DURATION = 0x4489
ID_TRACKS = 0x1654AE6B
ID_TRACK_ENTRY = 0xAE
ID_TRACK_NUMBER = 0xD7
ID_TRACK_UID = 0x73C5
ID_TRACK_TYPE = 0x83
ID_DEFAULT_DURATION = 0x23E383
ID_TRACK_TIMECODE_SCALE = 0x23314F
ID_CODEC_ID = 0x86
ID_CODEC_PRIVATE = 0x63A2
ID_CLUSTER = 0x1F43B675
ID_CLUSTER_TIMECODE = 0xE7
ID_SIMPLE_BLOCK = 0xA3
ID_BLOCK_GROUP = 0xA0
ID_BLOCK = 0xA1
ID_BLOCK_DURATION = 0x9B
ID_DISCARD_PADDING = 0x75A2
ID_CUES = 0x1C53BB6B
ID_CUE_POINT = 0xBB
ID_CUE_TIME = 0xB3
ID_CUE_TRACK_POSITIONS = 0xB7
ID_CUE_TRACK = 0xF7
ID_CUE_CLUSTER_POSITION = 0xF1
ID_CUE_RELATIVE_POSITION = 0xF0
ID_VOID = 0xEC

# Element IDs whose payloads are master trees.
_MASTER_IDS = {
    ID_HEADER, ID_SEGMENT, ID_INFO, ID_TRACKS, ID_TRACK_ENTRY, ID_CLUSTER,
    ID_BLOCK_GROUP, ID_CUES, ID_CUE_POINT, ID_CUE_TRACK_POSITIONS,
}
# Default (unsigned) interpretation for leaf elements; others get explicit
# handling in :func:`leaf_value`.
_SIGNED_IDS = {ID_DISCARD_PADDING}
_FLOAT_IDS = {ID_DURATION}
_STRING_IDS = {ID_CODEC_ID}

_ELEMENT_NAMES = {
    ID_HEADER: "EBML", ID_SEGMENT: "Segment", ID_INFO: "Info",
    ID_TIMESTAMP_SCALE: "TimecodeScale", ID_DURATION: "Duration",
    ID_TRACKS: "Tracks", ID_TRACK_ENTRY: "TrackEntry",
    ID_TRACK_NUMBER: "TrackNumber", ID_TRACK_UID: "TrackUID",
    ID_TRACK_TYPE: "TrackType", ID_DEFAULT_DURATION: "DefaultDuration",
    ID_TRACK_TIMECODE_SCALE: "TrackTimecodeScale", ID_CODEC_ID: "CodecID",
    ID_CODEC_PRIVATE: "CodecPrivate", ID_CLUSTER: "Cluster",
    ID_CLUSTER_TIMECODE: "Timecode", ID_SIMPLE_BLOCK: "SimpleBlock",
    ID_BLOCK_GROUP: "BlockGroup", ID_BLOCK: "Block",
    ID_BLOCK_DURATION: "BlockDuration", ID_DISCARD_PADDING: "DiscardPadding",
    ID_CUES: "Cues", ID_CUE_POINT: "CuePoint", ID_CUE_TIME: "CueTime",
    ID_CUE_TRACK_POSITIONS: "CueTrackPositions", ID_CUE_TRACK: "CueTrack",
    ID_CUE_CLUSTER_POSITION: "CueClusterPosition",
    ID_CUE_RELATIVE_POSITION: "CueRelativePosition", ID_VOID: "Void",
}


@dataclass
class VInt:
    """A parsed variable-length integer with its original encoding retained."""

    value: int
    length: int
    raw: bytes
    unknown: bool = False


def read_vint(buf: bytes, pos: int, *, keep_marker: bool = False) -> VInt:
    """Read one EBML VINT starting at *pos*.

    *keep_marker=True* returns the raw on-disk numeric value with the marker
    bit set (used for element ids); normally the marker bit is stripped and an
    all-ones payload becomes :data:`UNKNOWN_SIZE`.
    """
    if pos >= len(buf):
        raise EBMLError("truncated VINT", pos)
    first = buf[pos]
    if first == 0:
        raise EBMLError("invalid VINT leading octet 0x00", pos)
    width = (first.bit_length() - 7) if first >= 0x80 else (8 - first.bit_length())
    # Leading octet bit length gives the width directly:
    width = 8 - (first & 0xFF).bit_length() + 1
    if pos + width > len(buf):
        raise EBMLError("truncated VINT payload", pos)
    raw = buf[pos:pos + width]
    marker = 1 << (7 * width)
    numeric = int.from_bytes(raw, "big")
    if keep_marker:
        return VInt(numeric, width, raw)
    value = numeric - marker
    unknown = value == marker - 1
    return VInt(UNKNOWN_SIZE if unknown else value, width, raw, unknown)


def encode_vint(value: int, *, unknown: bool = False, width: int | None = None) -> bytes:
    """Encode *value* in minimal width (or explicit *width*), marker bit set."""
    if unknown:
        width = width or 8
        return b"\x01" + b"\xff" * (width - 1) if width == 1 else (
            bytes([1 << (8 - width)]) + b"\xff" * (width - 1))
    if width is None:
        width = 1
        while value >= (1 << (7 * width)) - 1:
            width += 1
    if value < 0 or value >= (1 << (7 * width)) - 1:
        raise ValueError(f"value {value} does not fit VINT width {width}")
    numeric = value | (1 << (7 * width))
    return numeric.to_bytes(width, "big")


def encode_id(element_id: int) -> bytes:
    """Element ids are VINTs with the marker bit retained."""
    width = max(1, (element_id.bit_length() + 7) // 8)
    raw = element_id.to_bytes(width, "big")
    if width != 8 - (raw[0] & 0xFF).bit_length() + 1:
        raise ValueError(f"0x{element_id:X} is not a canonical id of width {width}")
    return raw


@dataclass
class Element:
    id: int
    name: str
    offset: int                 # absolute offset of the id octet in the file
    header_len: int             # id + size octets
    size: int                   # UNKNOWN_SIZE (-1) when the length is unknown
    data_offset: int
    data_end: int               # segment/body end; equal to file end for unknown
    raw: bytes                  # id+size header bytes as found on disk
    value: object = None
    children: list["Element"] = field(default_factory=list)
    uid: str = ""               # stable hierarchical identity, e.g. S/C2/B0
    known: bool = True

    @property
    def range(self) -> tuple[int, int]:
        return (self.offset, self.data_end)

    def find_all(self, element_id: int) -> list["Element"]:
        out: list[Element] = []
        for child in self.children:
            if child.id == element_id:
                out.append(child)
            out.extend(child.find_all(element_id))
        return out

    def first(self, element_id: int) -> "Element | None":
        for child in self.children:
            if child.id == element_id:
                return child
        hit = None
        for child in self.children:
            hit = child.first(element_id)
            if hit is not None:
                return hit
        return None


class EBMLError(Exception):
    def __init__(self, message: str, offset: int = -1):
        super().__init__(message)
        self.offset = offset


def _leaf_value(eid: int, payload: bytes) -> object:
    if eid in _SIGNED_IDS:
        return int.from_bytes(payload, "big", signed=True)
    if eid in _FLOAT_IDS:
        if len(payload) == 4:
            import struct
            return struct.unpack(">f", payload)[0]
        if len(payload) == 8:
            import struct
            return struct.unpack(">d", payload)[0]
        return None
    if eid in _STRING_IDS:
        return payload.split(b"\x00", 1)[0].decode("utf-8", "replace")
    if eid == ID_CODEC_PRIVATE:
        return payload
    return int.from_bytes(payload, "big") if payload else 0


def parse_document(buf: bytes, *, base: int = 0) -> list[Element]:
    """Parse all top-level elements of *buf* into an EBML tree.

    Unknown-length master elements consume the remainder of *buf*.
    """
    return _parse_children(buf, 0, len(buf), base, depth=0)


def _parse_children(buf: bytes, start: int, end: int, base: int, depth: int,
                    uid_prefix: str = "") -> list[Element]:
    out: list[Element] = []
    pos = start
    ordinal = 0
    while pos < end:
        idv = read_vint(buf, pos, keep_marker=True)
        id_start = pos
        pos += idv.length
        sizev = read_vint(buf, pos)
        pos += sizev.length
        header_len = idv.length + sizev.length
        data_offset = id_start + header_len
        if sizev.unknown:
            data_end = end
        else:
            data_end = data_offset + sizev.value
            if data_end > end:
                raise EBMLError(
                    f"element 0x{idv.value:X} payload extends past parent end",
                    id_start)
        eid = idv.value
        name = _ELEMENT_NAMES.get(eid, f"Unknown(0x{eid:X})")
        known = eid in _ELEMENT_NAMES
        uid = f"{uid_prefix}{ordinal}" if uid_prefix else str(ordinal)
        el = Element(eid, name, base + id_start, header_len,
                     UNKNOWN_SIZE if sizev.unknown else sizev.value,
                     base + data_offset, base + data_end,
                     bytes(buf[id_start:data_offset]), uid=uid, known=known)
        payload = buf[data_offset:end if sizev.unknown else data_end]
        if depth < 64 and eid in _MASTER_IDS:
            el.children = _parse_children(
                buf, data_offset,
                end if sizev.unknown else data_end,
                base, depth + 1, uid_prefix=uid + "/")
        else:
            el.value = _leaf_value(eid, payload)
        out.append(el)
        ordinal += 1
        pos = data_end
    return out


def find_segment(tree: Iterable[Element]) -> Element | None:
    for top in tree:
        if top.id == ID_SEGMENT:
            return top
    return None


def element_bytes(buf: bytes, el: Element) -> bytes:
    start = el.offset
    return buf[start:el.data_end]


def serialize_element(eid: int, payload: bytes, *, unknown_size: bool = False,
                      size_width: int | None = None) -> bytes:
    head = encode_id(eid)
    if unknown_size:
        head += encode_vint(0, unknown=True, width=size_width)
    else:
        head += encode_vint(len(payload), width=size_width)
    return head + payload
