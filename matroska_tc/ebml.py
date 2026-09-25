"""Minimal, deterministic EBML reader/writer used by the Matroska timecode court.

The parser preserves *raw evidence*: every element keeps its file offset,
header span, data span, original bytes and whether its size was unknown
(VINT all-ones marker). Nothing here normalises time.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# Element catalogue (only the IDs this project reasons about).
# Sizes matter only for pretty naming; unknown IDs are kept verbatim.
# ---------------------------------------------------------------------------

ELEMENTS: dict[int, tuple[str, str]] = {
    0x1A45DFA3: ("EBMLHeader", "master"),
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
    0x2CB9C2: ("TrackTimecodeScale", "float"),
    0x536E: ("Name", "string"),
    0x86: ("CodecID", "string"),
    0x1F43B675: ("Cluster", "master"),
    0xE7: ("Timecode", "sint"),
    0xA0: ("BlockGroup", "master"),
    0xA1: ("Block", "binary"),
    0xA3: ("SimpleBlock", "binary"),
    0x9B: ("BlockDuration", "uint"),
    0x75A2: ("DiscardPadding", "sint"),
    0x1C53BB6B: ("Cues", "master"),
    0xBB: ("CuePoint", "master"),
    0xB3: ("CueTime", "uint"),
    0xB7: ("CueTrackPositions", "master"),
    0xF7: ("CueTrack", "uint"),
    0xF1: ("CueClusterPosition", "uint"),
    0xF0: ("CueRelativePosition", "uint"),
}

NAME_TO_ID = {name: eid for eid, (name, _kind) in ELEMENTS.items()}


def element_name(eid: int) -> str:
    return ELEMENTS.get(eid, (f"Unknown_0x{eid:X}", "unknown"))[0]


def element_kind(eid: int) -> str:
    return ELEMENTS.get(eid, ("Unknown", "unknown"))[1]


# ---------------------------------------------------------------------------
# VINT
# ---------------------------------------------------------------------------

def _vint_info(first: int) -> tuple[int, int]:
    """Return (length, marker bit mask) for a VINT leading byte."""
    for length in range(1, 9):
        if first & (0x80 >> (length - 1)):
            return length, 0xFF >> length
    raise ValueError("not a VINT leading byte: 0x%02x" % first)


def read_vint(buf: bytes, pos: int) -> tuple[int, int, bool]:
    """Decode an EBML VINT. Returns (value, raw_length, unknown_marker)."""
    if pos >= len(buf):
        raise EBMLError("unexpected end of data reading VINT")
    first = buf[pos]
    length, marker_mask = _vint_info(first)
    if pos + length > len(buf):
        raise EBMLError("truncated VINT")
    raw = buf[pos:pos + length]
    value = first & marker_mask
    unknown = (first & marker_mask) == marker_mask
    for b in raw[1:]:
        value = (value << 8) | b
        unknown = unknown and b == 0xFF
    return value, length, unknown


def encode_vint(value: int, length: Optional[int] = None) -> bytes:
    """Encode value; with explicit length the marker bit is placed directly."""
    if length is not None:
        if not 1 <= length <= 8:
            raise ValueError("bad VINT length")
        max_payload = (1 << (7 * length)) - 2
        if not 0 <= value <= max_payload:
            raise ValueError("value %d does not fit VINT of %d bytes" % (value, length))
        out = bytearray(length)
        v = value
        for i in range(length - 1, 0, -1):
            out[i] = v & 0xFF
            v >>= 8
        out[0] = v | (0x80 >> (length - 1))
        return bytes(out)
    for candidate in range(1, 9):
        if value < (1 << (7 * candidate)) - 2:
            return encode_vint(value, candidate)
    raise ValueError("value too large for VINT")


def encode_unknown_size(length: int = 1) -> bytes:
    if not 1 <= length <= 8:
        raise ValueError("bad unknown-size length")
    b = bytearray(length)
    b[0] = 0x80 >> (length - 1)
    for i in range(1, length):
        b[i] = 0xFF
    return bytes(b)


# ---------------------------------------------------------------------------
# Tree
# ---------------------------------------------------------------------------

class EBMLError(Exception):
    """Raised for structurally unrecoverable EBML input."""


@dataclass
class Element:
    eid: int
    name: str
    kind: str
    offset: int                 # absolute file offset of the ID
    header_len: int             # ID length + size VINT length
    data_offset: int
    size: Optional[int]         # None == unknown size
    unknown_size: bool
    data_end: Optional[int]     # None == runs to container/EOF
    raw: bytes = b""            # full element bytes (ID+size+data), if captured
    children: list["Element"] = field(default_factory=list)
    stable_id: str = ""

    @property
    def end(self) -> Optional[int]:
        return self.data_end if self.data_end is not None else (
            self.data_offset if self.size is None else self.data_end
        )

    def child_value(self, name_or_id: str | int) -> Optional["Element"]:
        target = NAME_TO_ID[name_or_id] if isinstance(name_or_id, str) else name_or_id
        for c in self.children:
            if c.eid == target:
                return c
        return None

    def children_named(self, name_or_id: str | int) -> list["Element"]:
        target = NAME_TO_ID[name_or_id] if isinstance(name_or_id, str) else name_or_id
        return [c for c in self.children if c.eid == target]

    def as_uint(self) -> Optional[int]:
        body = self.raw[self.header_len:]
        return decode_uint(body)

    def as_sint(self) -> Optional[int]:
        body = self.raw[self.header_len:]
        return decode_sint(body)

    def as_float(self) -> Optional[float]:
        body = self.raw[self.header_len:]
        return decode_float(body)

    def as_string(self) -> str:
        body = self.raw[self.header_len:]
        return body.rstrip(b"\x00").decode("utf-8", "replace")

    def as_bytes(self) -> bytes:
        return self.raw[self.header_len:]


def decode_uint(body: bytes) -> Optional[int]:
    if not body:
        return None
    return int.from_bytes(body, "big", signed=False)


def decode_sint(body: bytes) -> Optional[int]:
    if not body:
        return None
    return int.from_bytes(body, "big", signed=True)


def decode_float(body: bytes) -> Optional[float]:
    if len(body) == 4:
        import struct
        return struct.unpack(">f", body)[0]
    if len(body) == 8:
        import struct
        return struct.unpack(">d", body)[0]
    if len(body) == 0:
        return None
    raise EBMLError("float width %d unsupported" % len(body))


def encode_id(eid: int) -> bytes:
    length = (eid.bit_length() + 7) // 8
    return eid.to_bytes(length, "big")


# IDs that legitimately sit directly under a Segment. An unknown-length
# Cluster terminates when one of these is encountered.
SEGMENT_CHILD_IDS = {
    0x114D9B74,  # SeekHead
    0x1549A966,  # Info
    0x1654AE6B,  # Tracks
    0x1F43B675,  # Cluster
    0x1C53BB6B,  # Cues
    0x1043A770,  # Chapters
    0x1254C367,  # Tags
    0x1941A469,  # Void / placeholder variants are handled elsewhere
}


def _peek_id(buf: bytes, pos: int, end: int) -> Optional[int]:
    try:
        value, length, _ = read_vint(buf, pos)
    except EBMLError:
        return None
    if pos + length > end:
        return None
    return value


def parse(
    data: bytes,
    *,
    pos: int = 0,
    end: Optional[int] = None,
    container_id: Optional[int] = None,
    id_prefix: str = "el",
    capture: bool = True,
    seq: Optional[list[int]] = None,
    sibling_terminators: Optional[set[int]] = None,
) -> list[Element]:
    """Parse EBML elements from data[pos:end].

    Unknown-size master elements are terminated by their parent's boundary,
    EOF, or (for e.g. an unknown-length Cluster) a known sibling ID.
    ``seq`` is a shared document-ordinal counter for stable IDs.
    """
    if seq is None:
        seq = [0]
    if end is None:
        end = len(data)
    out: list[Element] = []
    while pos < end:
        if sibling_terminators and _peek_id(data, pos, end) in sibling_terminators:
            break
        el, pos = _parse_one(data, pos, end, container_id, id_prefix, capture, seq,
                             sibling_terminators)
        if el is not None:
            out.append(el)
    return out


def _parse_one(
    data: bytes,
    pos: int,
    end: int,
    container_id: Optional[int],
    id_prefix: str,
    capture: bool,
    seq: list[int],
    sibling_terminators: Optional[set[int]],
) -> tuple[Optional[Element], int]:
    start = pos
    try:
        eid, id_len, _ = read_vint(data, pos)
    except EBMLError:
        raise
    pos += id_len
    size, size_len, unknown = read_vint(data, pos)
    pos += size_len
    header_len = id_len + size_len
    data_offset = start + header_len

    if unknown:
        data_end: Optional[int] = None
        content_end = end  # tentative; resolved while reading children
    else:
        if data_offset + size > end:
            raise EBMLError(
                "element %s at %d declares size %d beyond end %d"
                % (hex(eid), start, size, end)
            )
        data_end = data_offset + size
        content_end = data_end

    kind = element_kind(eid)
    seq[0] += 1
    sid = f"{id_prefix}-{seq[0]:06d}"

    el = Element(
        eid=eid,
        name=element_name(eid),
        kind=kind,
        offset=start,
        header_len=header_len,
        data_offset=data_offset,
        size=None if unknown else size,
        unknown_size=unknown,
        data_end=data_end,
        stable_id=sid,
    )

    if kind == "master":
        if unknown:
            # An unknown-length Cluster ends at the next Segment-level
            # sibling; an unknown-length Segment runs to EOF.
            if eid == 0x1F43B675:
                child_terms = SEGMENT_CHILD_IDS
            else:
                child_terms = None
        else:
            child_terms = None
        el.children = parse(
            data,
            pos=data_offset,
            end=content_end,
            container_id=eid,
            id_prefix=id_prefix,
            capture=capture,
            seq=seq,
            sibling_terminators=child_terms,
        )
        if unknown:
            # Unknown-size master consumes through its last child.
            el.data_end = el.children[-1].data_end if el.children else data_offset
        next_pos = el.data_end
    else:
        next_pos = content_end

    if capture:
        el.raw = data[start:next_pos]
    return el, next_pos


def parse_document(data: bytes) -> dict:
    """Convenience: parse a full Matroska-ish document and split top level."""
    seq = [0]
    roots = parse(data, seq=seq)
    segment: Optional[Element] = None
    for r in roots:
        if r.eid == 0x18538067:
            segment = r
    return {"roots": roots, "segment": segment}


# ---------------------------------------------------------------------------
# Writer (used by the fixture builder)
# ---------------------------------------------------------------------------

def build(eid: int, payload: bytes, *, unknown_size: bool = False,
          size_len: Optional[int] = None) -> bytes:
    head = encode_id(eid)
    if unknown_size:
        head += encode_unknown_size(size_len or 1)
    else:
        if size_len is not None:
            head += encode_vint(len(payload), size_len)
        else:
            head += encode_vint(len(payload))
    return head + payload


def build_master(eid: int, children: bytes, *, unknown_size: bool = False,
                 size_len: Optional[int] = None) -> bytes:
    return build(eid, children, unknown_size=unknown_size, size_len=size_len)


def uint_bytes(value: int, width: Optional[int] = None) -> bytes:
    if width is None:
        width = max(1, (value.bit_length() + 7) // 8)
    return value.to_bytes(width, "big")


def sint_bytes(value: int, width: Optional[int] = None) -> bytes:
    # smallest width that holds the signed value, minimum 1
    if width is not None:
        return value.to_bytes(width, "big", signed=True)
    for w in range(1, 9):
        lo = -(1 << (7 * w - 1))
        hi = (1 << (7 * w - 1)) - 1
        if lo <= value <= hi:
            return value.to_bytes(w, "big", signed=True)
    return value.to_bytes(8, "big", signed=True)
