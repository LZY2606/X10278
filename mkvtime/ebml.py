"""EBML reader scoped to the Matroska element set used by the timecode court.

Parsing preserves, for every element:
  * element-id byte offset and data offset
  * declared data size, including the "unknown size" VINT marker
  * the original byte range [offset, end) (end may be None)
  * the raw data bytes

Unknown-size master elements are parsed greedily: children are read while the
stream allows; an unrecognised continuation that is not an allowed child ends
the element and the cursor rewinds, so the parent can continue.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# Element IDs relevant to this tool (Matroska v4 subset).
IDS = {
    0x1A45DFA3: ("EBML", "master"),
    0x4286: ("EBMLVersion", "uint"),
    0x42F7: ("EBMLReadVersion", "uint"),
    0x42F2: ("EBMLMaxIDLength", "uint"),
    0x42F3: ("EBMLMaxSizeLength", "uint"),
    0x4282: ("DocType", "string"),
    0x4287: ("DocTypeVersion", "uint"),
    0x4285: ("DocTypeReadVersion", "uint"),
    0x18538067: ("Segment", "master"),
    0x114D9B74: ("SeekHead", "master"),
    0x4DBB: ("Seek", "master"),
    0x53AB: ("SeekID", "binary"),
    0x53AC: ("SeekPosition", "uint"),
    0x1549A966: ("Info", "master"),
    0x2AD7B1: ("TimecodeScale", "uint"),
    0x4489: ("Duration", "float"),
    0x4D80: ("MuxingApp", "string"),
    0x5741: ("WritingApp", "string"),
    0x1654AE6B: ("Tracks", "master"),
    0xAE: ("TrackEntry", "master"),
    0xD7: ("TrackNumber", "uint"),
    0x73C5: ("TrackUID", "uint"),
    0x83: ("TrackType", "uint"),
    0x9C: ("FlagLacing", "uint"),
    0x23E383: ("DefaultDuration", "uint"),
    0x23314F: ("DefaultDecodedFieldDuration", "uint"),
    0x2AE7: ("TrackTimecodeScale", "float"),
    0x536E: ("Name", "string"),
    0x86: ("CodecID", "string"),
    0xE0: ("Video", "master"),
    0xE1: ("Audio", "master"),
    0xB0: ("PixelWidth", "uint"),
    0xBA: ("PixelHeight", "uint"),
    0xB5: ("SamplingFrequency", "float"),
    0x1F43B675: ("Cluster", "master"),
    0xE7: ("Timecode", "uint"),
    0xA0: ("BlockGroup", "master"),
    0xA1: ("Block", "binary"),
    0xA3: ("SimpleBlock", "binary"),
    0x9B: ("BlockDuration", "uint"),
    0x75A2: ("ReferenceBlock", "int"),
    0x75A1: ("BlockAdditions", "master"),
    0xA6: ("BlockMore", "master"),
    0xEE: ("BlockAddID", "uint"),
    0xA5: ("BlockAdditional", "binary"),
    0x25A2: ("DiscardPadding", "int"),
    0x1C53BB6B: ("Cues", "master"),
    0xBB: ("CuePoint", "master"),
    0xB3: ("CueTime", "uint"),
    0xB7: ("CueTrackPositions", "master"),
    0xF7: ("CueTrack", "uint"),
    0xF1: ("CueClusterPosition", "uint"),
    0xF0: ("CueRelativePosition", "uint"),
    0x5378: ("CueDuration", "uint"),
    0xEC: ("Void", "binary"),
    0xBF: ("CRC-32", "binary"),
}

NAMES = {v[0]: k for k, v in IDS.items()}

# Allowed children used to bound greedy parsing of unknown-size masters.
_CHILDREN = {
    "EBML": {
        "EBMLVersion", "EBMLReadVersion", "EBMLMaxIDLength", "EBMLMaxSizeLength",
        "DocType", "DocTypeVersion", "DocTypeReadVersion", "Void", "CRC-32",
    },
    "Segment": {"SeekHead", "Info", "Tracks", "Cluster", "Cues", "Void", "CRC-32"},
    "SeekHead": {"Seek", "Void", "CRC-32"},
    "Seek": {"SeekID", "SeekPosition"},
    "Info": {"TimecodeScale", "Duration", "MuxingApp", "WritingApp", "Void", "CRC-32"},
    "Tracks": {"TrackEntry"},
    "TrackEntry": {
        "TrackNumber", "TrackUID", "TrackType", "FlagLacing", "DefaultDuration",
        "DefaultDecodedFieldDuration", "TrackTimecodeScale", "Name", "CodecID",
        "Video", "Audio", "Void", "CRC-32",
    },
    "Video": {"PixelWidth", "PixelHeight"},
    "Audio": {"SamplingFrequency"},
    "Cluster": {"Timecode", "BlockGroup", "SimpleBlock", "CRC-32", "Void"},
    "BlockGroup": {
        "Block", "BlockDuration", "ReferenceBlock", "BlockAdditions",
        "DiscardPadding", "CRC-32",
    },
    "BlockAdditions": {"BlockMore"},
    "BlockMore": {"BlockAddID", "BlockAdditional"},
    "Cues": {"CuePoint"},
    "CuePoint": {"CueTime", "CueTrackPositions"},
    "CueTrackPositions": {
        "CueTrack", "CueClusterPosition", "CueRelativePosition", "CueDuration",
    },
}


class EBMLError(ValueError):
    """Raised when a source cannot be parsed as EBML at all."""


@dataclass
class Element:
    id: int
    name: str
    type: str
    offset: int
    data_offset: int
    size: Optional[int]              # None == unknown size
    end: Optional[int]               # None == not bounded by declared size
    raw: bytes = b""
    children: list["Element"] = field(default_factory=list)
    known: bool = True

    @property
    def unknown_size(self) -> bool:
        return self.size is None

    @property
    def header_range(self) -> tuple[int, int]:
        return (self.offset, self.data_offset)

    def range(self) -> tuple[int, Optional[int]]:
        return (self.offset, self.end)

    def get(self, name: str) -> Optional["Element"]:
        for child in self.children:
            if child.name == name:
                return child
        return None

    def get_all(self, name: str) -> list["Element"]:
        return [c for c in self.children if c.name == name]

    def value(self):
        return decode_value(self)

    def to_dict(self) -> dict:
        d = {
            "id": f"0x{self.id:X}",
            "name": self.name,
            "type": self.type,
            "offset": self.offset,
            "data_offset": self.data_offset,
            "size": self.size,
            "unknown_size": self.unknown_size,
            "end": self.end,
            "known": self.known,
        }
        if self.type == "master":
            d["children"] = [c.to_dict() for c in self.children]
        else:
            d["value"] = _scalar_value(self)
            d["raw_hex"] = self.raw.hex()
        return d


def _scalar_value(el: Element):
    if not el.known:
        return None
    return decode_value(el)


def read_vint(data: bytes, pos: int) -> tuple[int, int, int, int]:
    """Return (value, raw_marker_value, length, all_ones) for an EBML VINT.

    ``value`` has the VINT width marker bit removed. ``all_ones`` indicates an
    unknown-size marker (1..1, 0xFF..FF data part).
    """
    if pos >= len(data):
        raise EBMLError(f"truncated VINT at {pos}")
    first = data[pos]
    width = first.bit_length() - 7 if first != 0 else 0
    if width <= 0 or width > 8:
        raise EBMLError(f"illegal VINT length marker 0x{first:02x} at {pos}")
    if pos + width > len(data):
        raise EBMLError(f"truncated {width}-byte VINT at {pos}")
    raw = int.from_bytes(data[pos:pos + width], "big")
    mask = (1 << (8 * width - width)) - 1
    value = raw & mask
    all_ones = value == mask
    return value, raw, width, all_ones


def read_id(data: bytes, pos: int) -> tuple[int, int]:
    if pos >= len(data):
        raise EBMLError(f"truncated element ID at {pos}")
    first = data[pos]
    width = first.bit_length() - 7 if first != 0 else 0
    if width <= 0 or width > 4:
        raise EBMLError(f"illegal EBML ID at {pos}: 0x{first:02x}")
    if pos + width > len(data):
        raise EBMLError(f"truncated element ID at {pos}")
    value = int.from_bytes(data[pos:pos + width], "big")
    return value, width


def decode_uint(b: bytes) -> int:
    return int.from_bytes(b, "big", signed=False)


def decode_int(b: bytes) -> int:
    return int.from_bytes(b, "big", signed=True) if b else 0


def decode_float(b: bytes) -> float:
    if len(b) == 4:
        import struct

        return struct.unpack(">f", b)[0]
    if len(b) == 8:
        import struct

        return struct.unpack(">d", b)[0]
    if len(b) == 0:
        return 0.0
    raise EBMLError(f"float must be 0/4/8 bytes, got {len(b)}")


def decode_value(el: Element):
    b = el.raw
    if el.type == "uint":
        return decode_uint(b)
    if el.type == "int":
        return decode_int(b)
    if el.type == "float":
        return decode_float(b)
    if el.type == "string":
        return b.split(b"\x00", 1)[0].decode("utf-8", "replace")
    if el.type == "binary":
        return b.hex()
    return None


def _read_element(data: bytes, pos: int, end_limit: Optional[int]) -> tuple[Element, int]:
    el_id, id_len = read_id(data, pos)
    data_pos = pos + id_len
    _v, _raw, size_len, all_ones = read_vint(data, data_pos)
    size = None if all_ones else _v
    body_pos = data_pos + size_len
    info = IDS.get(el_id)
    if info is None:
        # Unknown element: cannot delimit children, leave raw empty range.
        el = Element(
            id=el_id, name=f"Unknown_0x{el_id:X}", type="binary",
            offset=pos, data_offset=body_pos, size=size, end=None, known=False,
        )
        if size is not None:
            el.end = body_pos + size
            el.raw = data[body_pos:el.end]
            return el, el.end
        raise EBMLError(f"unknown-size unknown element at {pos}")
    name, etype = info
    if size is not None:
        end = body_pos + size
        if end > len(data):
            raise EBMLError(f"element {name} at {pos} overruns file ({end}>{len(data)})")
        raw = data[body_pos:end]
        el = Element(el_id, name, etype, pos, body_pos, size, end, raw)
        if etype == "master":
            el.children, _ = parse_master_children(data, body_pos, end, set(_CHILDREN.get(name, ())))
        return el, end

    # Unknown-size master: only legal for master elements in this reader.
    if etype != "master":
        raise EBMLError(f"unknown size on non-master {name} at {pos}")
    el = Element(el_id, name, etype, pos, body_pos, None, None, b"")
    limit = end_limit if end_limit is not None else len(data)
    children, stop = parse_master_children(data, body_pos, limit, set(_CHILDREN.get(name, ())))
    el.children = children
    el.end = stop
    return el, stop


def parse_master_children(
    data: bytes, start: int, end: int, allowed: set[str]
) -> tuple[list[Element], int]:
    out: list[Element] = []
    pos = start
    while pos < end:
        try:
            peek_id, id_len = read_id(data, pos)
        except EBMLError:
            break
        info = IDS.get(peek_id)
        if info is None or info[0] not in allowed:
            # End of this unknown-size master; rewind so parent can continue.
            break
        try:
            child, nxt = _read_element(data, pos, end)
        except EBMLError:
            break
        out.append(child)
        pos = nxt
    return out, pos


def parse(data: bytes) -> list[Element]:
    """Parse top-level EBML stream (EBML header + Segment siblings)."""
    out: list[Element] = []
    pos = 0
    while pos < len(data):
        el, pos = _read_element(data, pos, len(data))
        out.append(el)
    return out


def find_segment(root: list[Element]) -> Element:
    for el in root:
        if el.name == "Segment":
            return el
    raise EBMLError("no Segment element")


def iter_elements(el: Element):
    """Depth-first iteration over every parsed element."""
    yield el
    for c in el.children:
        yield from iter_elements(c)
