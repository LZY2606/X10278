"""Minimal EBML / Matroska reader primitives.

Only what the tribunal needs: variable-length integers (with the
"unknown length" marker), a compact element-id registry, and a strict
parser that records byte offsets and original bytes for every element.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

VINT_UNKNOWN = -1


class EbmlError(Exception):
    pass


# ---------------------------------------------------------------- vint

def vint_width(first_byte: int) -> int:
    if first_byte == 0:
        raise EbmlError("invalid vint lead byte 0x00")
    width = 1
    marker = 0x80
    while marker:
        if first_byte & marker:
            return width
        width += 1
        marker >>= 1
    raise EbmlError("invalid vint lead byte")


def read_vint(buf: bytes, pos: int, keep_marker: bool = False):
    """Return (value, width). VINT_UNKNOWN marks an unknown-size value."""
    if pos >= len(buf):
        raise EbmlError("vint beyond buffer")
    first = buf[pos]
    width = vint_width(first)
    if pos + width > len(buf):
        raise EbmlError("truncated vint")
    raw = int.from_bytes(buf[pos:pos + width], "big")
    marker = 1 << (8 * width - width)
    all_ones = (1 << (7 * width)) - 1
    value = raw if keep_marker else raw - marker
    if not keep_marker and value == all_ones:
        return VINT_UNKNOWN, width
    return value, width


def encode_vint(value: int, width: int) -> bytes:
    if not 1 <= width <= 8:
        raise EbmlError("bad vint width")
    if value < 0 or value >= (1 << (7 * width)) - 1:
        raise EbmlError("vint value out of range")
    marker = 1 << (8 * width - width)
    return (marker | value).to_bytes(width, "big")


def encode_vint_min(value: int) -> bytes:
    for width in range(1, 9):
        if 0 <= value < (1 << (7 * width)) - 1:
            return encode_vint(value, width)
    raise EbmlError("vint too large")


def encode_unknown_size(width: int) -> bytes:
    marker = 1 << (8 * width - width)
    all_data = (1 << (7 * width)) - 1
    return (marker | all_data).to_bytes(width, "big")


# ------------------------------------------------------- id registry

@dataclass(frozen=True)
class Spec:
    eid: int
    name: str
    etype: str = "u"  # u|s|f|8|b|m
    multiple: bool = False


SPEC: dict[int, Spec] = {}
_NAME: dict[str, Spec] = {}


def _reg(spec: Spec) -> Spec:
    SPEC[spec.eid] = spec
    _NAME[spec.name] = spec
    return spec


def spec(eid: int) -> Spec:
    return SPEC.get(eid, Spec(eid, f"Unknown_{eid:#x}", "b"))


def by_name(name: str) -> Spec:
    return _NAME[name]


def _ids(hexid: str) -> int:
    return int(hexid.replace(" ", ""), 16)


def _build_registry() -> None:
    def add(name: str, hexid: str, etype: str = "u", multiple: bool = False) -> None:
        _reg(Spec(_ids(hexid), name, etype, multiple))

    add("EBML", "1A 45 DF A3", "m")
    add("EBMLVersion", "42 86")
    add("EBMLReadVersion", "42 F7")
    add("EBMLMaxIDLength", "42 F2")
    add("EBMLMaxSizeLength", "42 F3")
    add("DocType", "42 82", "8")
    add("DocTypeVersion", "42 87")
    add("DocTypeReadVersion", "42 85")

    add("Segment", "18 53 80 67", "m")
    add("SeekHead", "11 4D 9B 74", "m")
    add("Seek", "4D BB", "m", True)
    add("SeekID", "53 AB", "b")
    add("SeekPosition", "53 AC")
    add("Void", "EC", "b")
    add("CRC-32", "BF", "b")

    add("Info", "15 49 A9 66", "m")
    add("TimecodeScale", "2A D7 B1")
    add("TimecodeScaleNanoseconds", "2A D7 B2", "u")
    add("Duration", "44 89", "f")
    add("MuxingApp", "4D 80", "8")
    add("WritingApp", "57 41", "8")

    add("Tracks", "16 54 AE 6B", "m")
    add("TrackEntry", "AE", "m", True)
    add("TrackNumber", "D7")
    add("TrackUID", "73 C5")
    add("TrackType", "83")
    add("FlagLacing", "9C")
    add("DefaultDuration", "23 E3 83")
    add("TimecodeScale", "2A D7 B1")
    add("Name", "53 6E", "8")
    add("CodecID", "86", "8")

    add("Cluster", "1F 43 B6 75", "m", True)
    add("Timecode", "E7")
    add("SilentTracks", "58 54", "m")
    add("Position", "A7")
    add("PrevSize", "AB")

    add("BlockGroup", "A0", "m", True)
    add("Block", "A1", "b")
    add("BlockAdditions", "75 A1", "m")
    add("BlockDuration", "9B")
    add("ReferenceBlock", "FB", "s", True)
    add("DiscardPadding", "75 A2", "s")
    add("SimpleBlock", "A3", "b")

    add("Cues", "1C 53 BB 6B", "m")
    add("CuePoint", "BB", "m", True)
    add("CueTime", "B3")
    add("CueTrackPositions", "B7", "m", True)
    add("CueTrack", "F7")
    add("CueClusterPosition", "F1")
    add("CueRelativePosition", "F0")
    add("CueDuration", "B2")


_build_registry()
MASTER_IDS = {s.eid for s in SPEC.values() if s.etype == "m"}
SEGMENT_TOPLEVEL = {
    _NAME[n].eid for n in (
        "SeekHead", "Info", "Tracks", "Cluster", "Cues", "Void", "CRC-32")
}

# ---------------------------------------------------------------- tree


@dataclass
class Element:
    eid: int
    name: str
    id_offset: int
    size_offset: int
    header_len: int
    data_offset: int
    size: int  # VINT_UNKNOWN when unknown
    known_size: bool
    raw: bytes = b""
    children: list["Element"] = field(default_factory=list)
    parse_error: Optional[str] = None

    @property
    def end_offset(self) -> Optional[int]:
        if not self.known_size:
            return None
        return self.data_offset + self.size

    @property
    def byte_range(self) -> tuple[int, Optional[int]]:
        return (self.id_offset, self.end_offset)

    def get_child(self, name_or_id: "str | int") -> Optional["Element"]:
        target = name_or_id if isinstance(name_or_id, int) else _NAME[name_or_id].eid
        for child in self.children:
            if child.eid == target:
                return child
        return None

    def find_all(self, name_or_id: "str | int") -> list["Element"]:
        target = name_or_id if isinstance(name_or_id, int) else _NAME[name_or_id].eid
        out: list[Element] = []
        for child in self.children:
            if child.eid == target:
                out.append(child)
            out.extend(child.find_all(target))
        return out

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()


def _element_header(buf: bytes, pos: int) -> tuple[Element, int]:
    id_start = pos
    eid_first = buf[pos]
    id_width = vint_width(eid_first)
    eid = int.from_bytes(buf[pos:pos + id_width], "big")
    pos += id_width
    size_start = pos
    size, size_width = read_vint(buf, pos)
    pos += size_width
    sp = spec(eid)
    el = Element(
        eid=eid,
        name=sp.name,
        id_offset=id_start,
        size_offset=size_start,
        header_len=id_width + size_width,
        data_offset=pos,
        size=size,
        known_size=size != VINT_UNKNOWN,
    )
    return el, pos


def _parse_master(buf: bytes, start: int, end: Optional[int],
                  boundary: Optional[set[int]]) -> list[Element]:
    """Parse a master element's data region [start, end).

    ``boundary`` is the set of element ids that *terminate* parsing at this
    level when they appear after at least one sibling (used for unknown-size
    segment bodies).  It is never inherited by nested masters.
    """
    children: list[Element] = []
    pos = start
    limit = len(buf) if end is None else end
    while pos < limit:
        try:
            el, _ = _element_header(buf, pos)
        except EbmlError as exc:
            raise EbmlError(f"@{pos}: {exc}") from exc
        if boundary is not None and el.eid in boundary and children:
            break
        if not el.known_size:
            if el.eid in MASTER_IDS:
                el.children = _parse_master(buf, el.data_offset, limit, None)
                tails = [c.end_offset for c in el.children
                         if c.known_size and c.end_offset is not None]
                tail = max(tails) if tails else el.data_offset
            else:
                tail = limit
            el.raw = bytes(buf[el.data_offset:tail])
            children.append(el)
            pos = limit
            break
        data_end = el.data_offset + el.size
        if data_end > limit:
            el.parse_error = "declared size exceeds parent data"
            el.raw = bytes(buf[el.data_offset:limit])
            children.append(el)
            pos = limit
            break
        el.raw = bytes(buf[el.data_offset:data_end])
        if el.eid in MASTER_IDS:
            el.children = _parse_master(buf, el.data_offset, data_end, None)
        children.append(el)
        pos = data_end
    return children


# ------------------------------------------------------------ decoding

def decode_uint(el: Element) -> int:
    return int.from_bytes(el.raw, "big") if el.raw else 0


def decode_sint(el: Element) -> int:
    if not el.raw:
        return 0
    return int.from_bytes(el.raw, "big", signed=True)


def decode_float(el: Element) -> float:
    if len(el.raw) == 4:
        import struct
        return struct.unpack(">f", el.raw)[0]
    if len(el.raw) == 8:
        import struct
        return struct.unpack(">d", el.raw)[0]
    if len(el.raw) == 0:
        return 0.0
    raise EbmlError(f"bad float width {len(el.raw)}")


def decode_string(el: Element) -> str:
    try:
        return el.raw.split(b"\x00", 1)[0].decode("utf-8")
    except UnicodeDecodeError:
        return el.raw.decode("latin-1")


def signed_int_width(raw: bytes) -> int:
    """Interpret *raw* as a signed big-endian integer of exactly its width.

    This is the heart of the relative-timecode rule: a 16-bit relative
    timecode with the high bit set is negative, not an unsigned overflow.
    """
    return int.from_bytes(raw, "big", signed=True)


# ------------------------------------------------------------ builder

def elem(name_or_id: "str | int", payload: bytes = b"",
         unknown_size_width: Optional[int] = None,
         size_width: Optional[int] = None) -> Element:
    eid = by_name(name_or_id).eid if isinstance(name_or_id, str) else name_or_id
    id_bytes = eid.to_bytes((eid.bit_length() + 7) // 8 or 1, "big")
    if unknown_size_width is not None:
        size_bytes = encode_unknown_size(unknown_size_width)
    elif size_width is not None:
        size_bytes = encode_vint(len(payload), size_width)
    else:
        size_bytes = encode_vint_min(len(payload))
    header = id_bytes + size_bytes
    dummy = Element(
        eid=eid, name=spec(eid).name, id_offset=0,
        size_offset=len(id_bytes), header_len=len(header),
        data_offset=len(header), size=(
            VINT_UNKNOWN if unknown_size_width is not None else len(payload)),
        known_size=unknown_size_width is None,
    )
    dummy.raw = payload
    if eid in MASTER_IDS:
        dummy.children = _parse_master(payload, 0, len(payload), None)
    return dummy


def master(name: str, children: list[Element],
           unknown_size_width: Optional[int] = None,
           size_width: Optional[int] = None) -> Element:
    payload = b"".join(serialize(c) for c in children)
    if size_width is not None and unknown_size_width is None:
        return elem(name, payload, size_width=size_width)
    return elem(name, payload, unknown_size_width=unknown_size_width)


def uint_el(name: str, value: int, width: Optional[int] = None) -> Element:
    if width is None:
        width = max(1, (value.bit_length() + 7) // 8)
    return elem(name, value.to_bytes(width, "big"))


def sint_el(name: str, value: int, width: int) -> Element:
    return elem(name, value.to_bytes(width, "big", signed=True))


def float_el(name: str, value: float, width: int = 8) -> Element:
    import struct
    fmt = ">f" if width == 4 else ">d"
    return elem(name, struct.pack(fmt, value))


def string_el(name: str, value: str) -> Element:
    return elem(name, value.encode("utf-8"))


def serialize(el: Element, payload: Optional[bytes] = None) -> bytes:
    id_bytes = el.eid.to_bytes((el.eid.bit_length() + 7) // 8 or 1, "big")
    data = el.raw if payload is None else payload
    size_width = el.header_len - len(id_bytes)
    if not el.known_size:
        size_bytes = encode_unknown_size(size_width)
    else:
        size_bytes = encode_vint_min(len(data))
    return id_bytes + size_bytes + data


def finalize_unknown_master(el: Element, size_width: int) -> None:
    """Replace an unknown-size marker with the real size (in-memory)."""
    payload = b"".join(serialize(c) for c in el.children)
    id_bytes = el.eid.to_bytes((el.eid.bit_length() + 7) // 8 or 1, "big")
    new_header = len(id_bytes) + size_width
    el.size = len(payload)
    el.known_size = True
    el.header_len = new_header
    el.data_offset = new_header
    el.raw = payload
