"""Low-level EBML parsing.

Preserves, for every element: byte offset, header size, declared size
(None when the element uses an unknown-size vint), the raw byte range,
and the unknown-size flag itself.  Nothing is normalised away at this
layer; semantics live in mkvtime.model.
"""
from __future__ import annotations

from dataclasses import dataclass, field


class ParseError(Exception):
    pass


# Matroska element ids we understand.  Unknown ids are still parsed
# structurally (offset/size/raw range kept) and reported by name "UNKNOWN".
MASTER_IDS = {
    0x1A45DFA3,  # EBML
    0x18538067,  # Segment
    0x114D9B74,  # SeekHead
    0x1549A966,  # Info
    0x1654AE6B,  # Tracks
    0xAE,        # TrackEntry
    0x1F43B675,  # Cluster
    0xA0,        # BlockGroup
    0x1C53BB6B,  # Cues
    0xBB,        # CuePoint
    0xB7,        # CueTrackPositions
}

ID_NAMES = {
    0x1A45DFA3: "EBML", 0x4286: "EBMLVersion", 0x42F7: "EBMLReadVersion",
    0x42F2: "EBMLMaxIDLength", 0x42F3: "EBMLMaxSizeLength",
    0x4282: "DocType", 0x4287: "DocTypeVersion", 0x4285: "DocTypeReadVersion",
    0x18538067: "Segment", 0x114D9B74: "SeekHead",
    0x1549A966: "Info", 0x2AD7B1: "TimecodeScale", 0x4489: "Duration",
    0x1654AE6B: "Tracks", 0xAE: "TrackEntry", 0xD7: "TrackNumber",
    0x73C5: "TrackUID", 0x83: "TrackType", 0x86: "CodecID",
    0x23E383: "DefaultDuration", 0x23314F: "TrackTimecodeScale",
    0x1F43B675: "Cluster", 0xE7: "Timecode",
    0xA0: "BlockGroup", 0xA1: "Block", 0xA3: "SimpleBlock",
    0x9B: "BlockDuration", 0x75A2: "DiscardPadding",
    0x1C53BB6B: "Cues", 0xBB: "CuePoint", 0xB3: "CueTime",
    0xB7: "CueTrackPositions", 0xF7: "CueTrack",
    0xF1: "CueClusterPosition", 0x5378: "CueBlockNumber",
    0xEC: "Void", 0xBF: "CRC-32",
}


# Ids that may appear directly inside a given master.  Used to terminate
# unknown-size masters: an unknown-size element ends when an id that is
# not one of its children shows up (it belongs to an ancestor).
VALID_CHILDREN = {
    0x1F43B675: {0xE7, 0xA0, 0xA3, 0xAB, 0xEC, 0xBF},          # Cluster
    0xA0: {0xA1, 0x9B, 0x75A2, 0xFB, 0xEC, 0xBF},              # BlockGroup
    0x18538067: {0x114D9B74, 0x1549A966, 0x1654AE6B, 0x1F43B675,
                 0x1C53BB6B, 0x1043A770, 0x1941A469, 0xEC, 0xBF},  # Segment
}


def read_vint(data: bytes, pos: int, strip_marker: bool = True):
    """Read an EBML variable-length integer at pos.

    Returns (value, length, is_unknown).  With strip_marker=True the
    length-marker bit is removed (size semantics); otherwise it is kept
    (id semantics).  is_unknown is True when all data bits are 1.
    """
    if pos >= len(data):
        raise ParseError(f"vint at {pos} beyond end of data ({len(data)})")
    first = data[pos]
    mask = 0x80
    length = 1
    while length <= 8 and not (first & mask):
        mask >>= 1
        length += 1
    if length > 8:
        raise ParseError(f"invalid vint at {pos}: no marker bit")
    if pos + length > len(data):
        raise ParseError(f"truncated vint at {pos}")
    value = (first & (mask - 1)) if strip_marker else first
    for i in range(1, length):
        value = (value << 8) | data[pos + i]
    unknown = strip_marker and value == (1 << (7 * length)) - 1
    return value, length, unknown


@dataclass
class Element:
    eid: int
    offset: int            # absolute offset of the element id
    header_size: int       # id + size vint bytes
    size: int | None       # None => unknown-size element
    unknown_size: bool
    children: list = field(default_factory=list)
    parse_error: str | None = None
    content_end: int | None = None  # where children stopped (unknown-size masters)

    @property
    def name(self) -> str:
        return ID_NAMES.get(self.eid, f"UNKNOWN-0x{self.eid:X}")

    @property
    def data_offset(self) -> int:
        return self.offset + self.header_size

    @property
    def end(self) -> int | None:
        if self.size is None:
            return None
        return self.data_offset + self.size

    @property
    def is_master(self) -> bool:
        return self.eid in MASTER_IDS

    def raw_range(self, parent_end: int) -> tuple[int, int]:
        """Byte range [start, end) actually occupied.  Unknown-size
        elements extend to where their children stopped being valid,
        or to the end of their parent."""
        if self.end is not None:
            return (self.offset, self.end)
        return (self.offset, self.content_end if self.content_end is not None else parent_end)


def parse_elements(data: bytes, start: int, end: int, depth: int = 0,
                   allowed: set[int] | None = None) -> list[Element]:
    """Parse the element sequence in data[start:end].  When `allowed` is
    given, parsing stops before any id outside the set (used to bound
    unknown-size masters)."""
    out: list[Element] = []
    pos = start
    while pos < end:
        try:
            eid, id_len, _ = read_vint(data, pos, strip_marker=False)
            if allowed is not None and eid not in allowed:
                break
            size, size_len, unknown = read_vint(data, pos + id_len)
        except ParseError as exc:
            tail = Element(eid=-1, offset=pos, header_size=0, size=end - pos,
                           unknown_size=False, parse_error=str(exc))
            out.append(tail)
            break
        el = Element(eid=eid, offset=pos, header_size=id_len + size_len,
                     size=None if unknown else size, unknown_size=unknown)
        el_end = el.end if el.end is not None else end
        if el_end > end:
            # Declared size overruns the parent: keep the element, clamp,
            # and record a diagnostic instead of discarding bytes.
            el.parse_error = f"declared end {el_end} overruns parent end {end}"
            el_end = end
            el.size = end - el.data_offset
        if el.is_master:
            child_allowed = VALID_CHILDREN.get(eid) if unknown else None
            el.children = parse_elements(data, el.data_offset, el_end, depth + 1,
                                         child_allowed)
            if unknown and el.children:
                last = el.children[-1]
                el.content_end = last.raw_range(el_end)[1]
            elif unknown:
                el.content_end = el.data_offset
        out.append(el)
        pos = el_end if not unknown else (el.content_end or el_end)
        if el.size == 0 and not el.is_master:
            # zero-length non-master: fine, continue
            pass
    return out


def parse(data: bytes) -> list[Element]:
    return parse_elements(data, 0, len(data))


def find_all(elements: list[Element], eid: int):
    for el in elements:
        if el.eid == eid:
            yield el
        yield from find_all(el.children, eid)


def first_child(el: Element, eid: int) -> Element | None:
    for c in el.children:
        if c.eid == eid:
            return c
    return None


def uint_value(data: bytes, el: Element) -> int:
    raw = data[el.data_offset:el.end]
    return int.from_bytes(raw, "big", signed=False) if raw else 0


def int_value(data: bytes, el: Element) -> int:
    raw = data[el.data_offset:el.end]
    return int.from_bytes(raw, "big", signed=True) if raw else 0


def float_value(data: bytes, el: Element) -> float:
    import struct
    raw = data[el.data_offset:el.end]
    if len(raw) == 4:
        return struct.unpack(">f", raw)[0]
    if len(raw) == 8:
        return struct.unpack(">d", raw)[0]
    raise ParseError(f"bad float size {len(raw)} at {el.offset}")


def str_value(data: bytes, el: Element) -> str:
    return data[el.data_offset:el.end].decode("utf-8", "replace")
