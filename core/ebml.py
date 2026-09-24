"""Minimal, evidence-preserving EBML parser for Matroska/WebM.

The parser never throws away positional evidence. Every returned element keeps
its id-offset, header length, content range and (for scalars) its raw bytes.
Unknown-length masters are represented with ``size is None``; their effective
span is determined by level boundaries (EBML sibling rule) rather than by
guessing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# Element IDs -----------------------------------------------------------------
EBML = 0x1A45DFA3
SEGMENT = 0x18538067
SEEKHEAD = 0x114D9B74
INFO = 0x1549A966
TRACKS = 0x1654AE6B
TRACK_ENTRY = 0xAE
TRACK_NUMBER = 0xD7
TRACK_UID_EL = 0x73C5
TRACK_TYPE = 0x83
FLAG_DEFAULT = 0x88
DEFAULT_DURATION = 0x23E383
TRACK_TIMESTAMP_SCALE = 0x23314F
NAME = 0x536E
CODEC_ID = 0x86
TIMESTAMP_SCALE = 0x2AD7B1
DURATION_EL = 0x4489
MUXING_APP = 0x4D80
WRITING_APP = 0x5741
CLUSTER = 0x1F43B675
TIMESTAMP_CLUSTER = 0xE7
SIMPLE_BLOCK = 0xA3
BLOCK_GROUP = 0xA0
BLOCK = 0xA1
BLOCK_DURATION = 0x9B
DISCARD_PADDING = 0x75A2
REFERENCE_BLOCK = 0xFB
CUES = 0x1C53BB6B
CUE_POINT = 0xBB
CUE_TIME = 0xB3
CUE_TRACK_POSITIONS = 0xB7
CUE_TRACK = 0xF7
CUE_CLUSTER_POSITION = 0xF1
CUE_RELATIVE_POSITION = 0xF0
VOID = 0xEC

# (name, type, level)
_ELEMENTS = {
    EBML: ("EBML", "master", 0),
    SEGMENT: ("Segment", "master", 1),
    SEEKHEAD: ("SeekHead", "master", 2),
    INFO: ("Info", "master", 2),
    TRACKS: ("Tracks", "master", 2),
    TRACK_ENTRY: ("TrackEntry", "master", 3),
    TRACK_NUMBER: ("TrackNumber", "uint", 3),
    TRACK_UID_EL: ("TrackUID", "uint", 3),
    TRACK_TYPE: ("TrackType", "uint", 3),
    FLAG_DEFAULT: ("FlagDefault", "uint", 3),
    DEFAULT_DURATION: ("DefaultDuration", "uint", 3),
    TRACK_TIMESTAMP_SCALE: ("TrackTimestampScale", "float", 3),
    NAME: ("Name", "string", 3),
    CODEC_ID: ("CodecID", "string", 3),
    TIMESTAMP_SCALE: ("TimecodeScale", "uint", 2),
    DURATION_EL: ("Duration", "float", 2),
    MUXING_APP: ("MuxingApp", "string", 2),
    WRITING_APP: ("WritingApp", "string", 2),
    CLUSTER: ("Cluster", "master", 2),
    TIMESTAMP_CLUSTER: ("Timecode", "uint", 2),
    SIMPLE_BLOCK: ("SimpleBlock", "binary", 2),
    BLOCK_GROUP: ("BlockGroup", "master", 2),
    BLOCK: ("Block", "binary", 2),
    BLOCK_DURATION: ("BlockDuration", "uint", 2),
    DISCARD_PADDING: ("DiscardPadding", "sint", 2),
    REFERENCE_BLOCK: ("ReferenceBlock", "sint", 2),
    CUES: ("Cues", "master", 2),
    CUE_POINT: ("CuePoint", "master", 3),
    CUE_TIME: ("CueTime", "uint", 3),
    CUE_TRACK_POSITIONS: ("CueTrackPositions", "master", 3),
    CUE_TRACK: ("CueTrack", "uint", 3),
    CUE_CLUSTER_POSITION: ("CueClusterPosition", "uint", 3),
    CUE_RELATIVE_POSITION: ("CueRelativePosition", "uint", 3),
    VOID: ("Void", "binary", 2),
}

# Level-1 (direct Segment children) element ids. An unknown-length Cluster ends
# where the next level-1 element begins (EBML sibling rule).
LEVEL1_IDS = frozenset(
    {SEEKHEAD, INFO, TRACKS, CLUSTER, CUES, VOID,
     0x1043A770, 0x1254C367, 0x1941A469, 0x1C53BB6B, 0x184D80,
     0x114D9B74, 0x1549A966, 0x1654AE6B, 0x1F43B675}
)
MASTER_IDS = frozenset(
    {k for k, v in _ELEMENTS.items() if v[1] == "master"}
)
UNKNOWN_MASTER_BOUNDARIES = {CLUSTER: LEVEL1_IDS}


class EbmlError(ValueError):
    pass


@dataclass
class Element:
    id: int
    name: str
    type: str
    level: int
    pos: int                    # offset of the element id
    header_len: int             # id bytes + size vint bytes
    size: Optional[int]         # None == unknown length
    data_pos: int
    data_end: Optional[int]     # None for unknown-length masters
    value: object = None        # decoded scalar value
    raw: bytes = b""            # raw content bytes (scalars)
    diagnostic: Optional[str] = None
    children: list["Element"] = field(default_factory=list)
    effective_end: Optional[int] = None  # resolved span for unknown masters

    @property
    def id_hex(self) -> str:
        width = {1: 2, 2: 4, 3: 6, 4: 8}[_id_width(self.id)]
        return f"0x{self.id:0{width}X}"

    def to_dict(self) -> dict:
        d = {
            "id": self.id_hex,
            "name": self.name,
            "type": self.type,
            "level": self.level,
            "pos": self.pos,
            "header_len": self.header_len,
            "size": self.size,
            "unknown_length": self.size is None,
            "data_pos": self.data_pos,
            "data_end": self.data_end,
            "effective_end": self.effective_end,
            "diagnostic": self.diagnostic,
        }
        if self.type != "master":
            d["raw_hex"] = self.raw.hex()
            d["value"] = _json_value(self.value)
        else:
            d["children"] = [c.to_dict() for c in self.children]
        return d


def _json_value(v):
    if isinstance(v, bytes):
        return v.hex()
    return v


def _id_width(eid: int) -> int:
    if eid <= 0xFF:
        return 1
    if eid <= 0xFFFF:
        return 2
    if eid <= 0xFFFFFF:
        return 3
    return 4


def read_vint(data: bytes, pos: int) -> tuple[int, int, int]:
    """Return (value_without_length_marker, byte_length, marker_bit)."""
    if pos >= len(data):
        raise EbmlError(f"truncated VINT at {pos}")
    first = data[pos]
    width = 0
    marker = 0x80
    while marker and not (first & marker):
        width += 1
        marker >>= 1
    if not marker:
        raise EbmlError(f"invalid VINT length descriptor at {pos}")
    width += 1
    if pos + width > len(data):
        raise EbmlError(f"truncated VINT data at {pos}")
    val = first & (marker - 1)
    for i in range(1, width):
        val = (val << 8) | data[pos + i]
    all_data_bits = (1 << (7 * width)) - 1
    return val, width, 1 if val == all_data_bits else 0


def read_element_id(data: bytes, pos: int) -> tuple[int, int]:
    if pos >= len(data):
        raise EbmlError(f"truncated element id at {pos}")
    first = data[pos]
    width = 0
    marker = 0x80
    while marker and not (first & marker):
        width += 1
        marker >>= 1
    if not marker:
        raise EbmlError(f"invalid element id descriptor at {pos}")
    width += 1
    if pos + width > len(data):
        raise EbmlError(f"truncated element id at {pos}")
    eid = 0
    for i in range(width):
        eid = (eid << 8) | data[pos + i]
    return eid, width


def _decode_scalar(kind: str, raw: bytes):
    if kind == "uint":
        return int.from_bytes(raw, "big", signed=False) if raw else 0
    if kind == "sint":
        return int.from_bytes(raw, "big", signed=True) if raw else 0
    if kind == "float":
        import struct
        if len(raw) == 4:
            return struct.unpack(">f", raw)[0]
        if len(raw) == 8:
            return struct.unpack(">d", raw)[0]
        return None
    if kind == "string":
        return raw.rstrip(b"\x00").decode("utf-8", errors="replace")
    return None


def _parse_children(data: bytes, start: int, end: Optional[int], level: int,
                    hard_stop: Optional[frozenset[int]]
                    ) -> tuple[list[Element], int]:
    out: list[Element] = []
    pos = start
    limit = len(data) if end is None else end
    while pos < limit:
        try:
            eid, id_len = read_element_id(data, pos)
        except EbmlError:
            break
        if hard_stop is not None and eid in hard_stop:
            break
        name, kind, el_level = _ELEMENTS.get(
            eid, (f"Unknown_0x{eid:X}", "binary", level))
        try:
            size_val, size_len, marker = read_vint(data, pos + id_len)
        except EbmlError:
            break
        data_pos = pos + id_len + size_len
        header_len = id_len + size_len
        unknown_size = marker == 1
        if unknown_size:
            size = None
            data_end = None
        else:
            size = size_val
            data_end = data_pos + size
            if data_end > limit:
                # Truncated declared master/scalar: keep the element with a
                # diagnostic and stop scanning this level.
                raw = data[data_pos:limit]
                el = Element(eid, name, kind, el_level, pos, header_len,
                             size, data_pos, data_end,
                             value=_decode_scalar(kind, raw) if kind != "binary"
                             else None,
                             raw=raw if kind != "master" else b"",
                             diagnostic="declared size exceeds available bytes")
                out.append(el)
                pos = limit
                break
        if kind == "master":
            boundary = UNKNOWN_MASTER_BOUNDARIES.get(eid)
            if unknown_size:
                children, next_pos = _parse_children(
                    data, data_pos, None, el_level + 1,
                    boundary if boundary is not None else hard_stop)
                effective_end = next_pos
                el = Element(eid, name, kind, el_level, pos, header_len,
                             None, data_pos, None, children=children,
                             effective_end=effective_end)
            else:
                children, _ = _parse_children(
                    data, data_pos, data_end, el_level + 1, None)
                el = Element(eid, name, kind, el_level, pos, header_len,
                             size, data_pos, data_end, children=children,
                             effective_end=data_end)
            out.append(el)
            pos = effective_end if unknown_size else data_end
        else:
            raw = data[data_pos: limit] if unknown_size else \
                data[data_pos:data_end]
            value = _decode_scalar(kind, raw) if kind != "binary" else None
            diag = None
            if kind == "float" and value is None and raw:
                diag = f"unsupported float width {len(raw)} bytes"
            el = Element(eid, name, kind, el_level, pos, header_len,
                         size, data_pos, data_end, value=value, raw=raw,
                         diagnostic=diag,
                         effective_end=limit if unknown_size else data_end)
            out.append(el)
            pos = limit if unknown_size else data_end
    return out, pos


def parse_document(data: bytes) -> list[Element]:
    """Parse a whole Matroska/WebM byte string into a level-0 element tree."""
    roots, _ = _parse_children(data, 0, len(data), 0, None)
    return roots


def find_first(root: Element, eid: int) -> Optional[Element]:
    for c in root.children:
        if c.id == eid:
            return c
    return None


def find_all(root: Element, eid: int) -> list[Element]:
    return [c for c in root.children if c.id == eid]
