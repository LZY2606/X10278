"""Raw EBML/VINT decoding.

Only this module understands VINT framing. Everything produced here keeps the
exact file offsets and byte ranges so the review UI can highlight raw bytes and
so no information is discarded at import time.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


class EBMLDecodeError(ValueError):
    """Raised when a VINT/element cannot be framed at all."""


@dataclass
class VInt:
    """A variable-length integer as stored on disk.

    raw_len is the marker width (1..8); marker_unknown means the all-ones
    payload that EBML defines as "unknown/unspecified" length.
    """

    value: Optional[int]
    raw_len: int
    marker_unknown: bool
    offset: int


def read_vint(buf: bytes, offset: int, is_id: bool = False) -> VInt:
    if offset >= len(buf):
        raise EBMLDecodeError(f"vint starts past EOF at {offset}")
    first = buf[offset]
    if first == 0:
        raise EBMLDecodeError(f"zero VINT length marker at {offset}")
    width = 0
    marker = 0x80
    while marker:
        if first & marker:
            break
        width += 1
        marker >>= 1
    width += 1
    if is_id and width > 4:
        raise EBMLDecodeError(f"element id wider than 4 bytes at {offset}")
    if width > 8 or offset + width > len(buf):
        raise EBMLDecodeError(f"vint {width} bytes exceeds buffer at {offset}")
    payload_mask = (1 << (8 - width)) - 1 if width < 8 else 0xFF
    value = first & payload_mask
    unknown = value == payload_mask and width > 0
    for i in range(1, width):
        value = (value << 8) | buf[offset + i]
    if is_id:
        # IDs keep the marker bit as part of their numeric value.
        id_value = first
        for i in range(1, width):
            id_value = (id_value << 8) | buf[offset + i]
        return VInt(id_value, width, False, offset)
    return VInt(None if unknown else value, width, unknown, offset)


def encode_vint(value: int, width: Optional[int] = None,
                unknown: bool = False) -> bytes:
    """Encode a VINT; unknown=True emits the all-ones payload marker."""
    if unknown:
        if width is None:
            raise ValueError("unknown vint needs explicit width")
        if not 1 <= width <= 8:
            raise ValueError("vint width out of range")
        first = 0x100 >> width
        rest = b"\xff" * (width - 1) if width > 1 else b""
        if width == 1:
            return bytes([first | 0x7F]) if False else bytes([0xFF])
        # marker bit plus all-ones payload in every byte
        return bytes([first | ((1 << (8 - width)) - 1)]) + b"\xff" * (width - 1)
    if value < 0:
        raise ValueError("vint value negative")
    if width is None:
        width = _min_width(value)
    if not 1 <= width <= 8:
        raise ValueError("vint width out of range")
    max_payload = (7 * width) + (8 - width)  # payload bits
    if value >= (1 << max_payload) - 1:
        raise ValueError(f"value {value} needs unknown-marker or wider vint")
    bits = width * 7 if width < 8 else 56
    marker_pos = (width - 1) * 8 + (8 - width)
    raw = (1 << marker_pos) | value
    return raw.to_bytes(width, "big")


def _min_width(value: int) -> int:
    for width in range(1, 9):
        payload_bits = 7 * width if width < 8 else 56
        if value < (1 << payload_bits) - 1:
            return width
    raise ValueError("value too large")


@dataclass
class Element:
    id: int
    name: str
    header_start: int          # offset of the id vint
    id_len: int
    size_len: int
    data_start: int
    size: Optional[int]        # None == unknown length
    data_end: int              # resolved end (exclusive); EOF or boundary
    value: Optional[bytes] = None
    children: list["Element"] = field(default_factory=list)

    @property
    def header_end(self) -> int:
        return self.data_start

    @property
    def total_end(self) -> int:
        return self.data_end

    def find(self, eid: int) -> Optional["Element"]:
        for child in self.children:
            if child.id == eid:
                return child
        return None

    def find_all(self, eid: int) -> list["Element"]:
        return [c for c in self.children if c.id == eid]

    def child_value(self, eid: int) -> Optional[bytes]:
        child = self.find(eid)
        return None if child is None or child.value is None else child.value


# Level-1 Matroska ids that can legally terminate an unknown-size Cluster.
_CLUSTER_BOUNDARY_IDS = {
    0x114D9B74,  # SeekHead
    0x1549A966,  # Info
    0x1F43B675,  # Cluster (!) next cluster
    0x1654AE6B,  # Tracks
    0x1C53BB6B,  # Cues
    0x1941A469,  # Attachments
    0x1043A770,  # Chapters
    0x1254C367,  # Tags
    0x18538067,  # Segment
}

_ID_NAMES: dict[int, str] = {}

# Maps an unknown-size container id to the ids that legally terminate it.
UNKNOWN_BOUNDARY: dict[int, set[int]] = {}


def register_names(mapping: dict[int, str]) -> None:
    _ID_NAMES.update(mapping)


def register_boundaries(mapping: dict[int, set[int]]) -> None:
    UNKNOWN_BOUNDARY.update(mapping)


def _looks_like_id(buf: bytes, off: int, boundary_ids: set[int]) -> bool:
    if off >= len(buf) or buf[off] == 0:
        return False
    try:
        vid = read_vint(buf, off, is_id=True)
    except EBMLDecodeError:
        return False
    if vid.raw_len > 4:
        return False
    if vid.id_value in boundary_ids:
        return True
    return False


def parse_element(buf: bytes, offset: int, end: int,
                  container_ids: set[int]) -> Element:
    vid = read_vint(buf, offset, is_id=True)
    size_off = offset + vid.raw_len
    vsize = read_vint(buf, size_off)
    data_start = size_off + vsize.raw_len
    name = _ID_NAMES.get(vid.id_value, f"Unknown_{vid.id_value:08X}")
    if vsize.marker_unknown:
        size = None
    else:
        size = vsize.value
    if size is not None:
        data_end = data_start + size
        if data_end > end:
            data_end = end
    else:
        data_end = -1  # resolved below
    el = Element(
        id=vid.id_value, name=name, header_start=offset,
        id_len=vid.raw_len, size_len=vsize.raw_len, data_start=data_start,
        size=size, data_end=data_end,
    )
    child_boundary = UNKNOWN_BOUNDARY.get(el.id, set())
    if size is None:
        # Unknown length: children run until the parent boundary / EOF or a
        # sibling boundary element appears. Segment (no registered boundary)
        # legitimately runs to EOF.
        if el.id in container_ids:
            resolved_end = _parse_children(buf, el, end, container_ids,
                                           child_boundary)
        else:
            resolved_end = end
        el.data_end = resolved_end
        return el
    if el.id in container_ids:
        _parse_children(buf, el, el.data_end, container_ids, child_boundary)
    else:
        el.value = buf[el.data_start:el.data_end]
    return el


def _parse_children(buf: bytes, parent: Element, end: int,
                    container_ids: set[int],
                    boundary_ids: set[int]) -> int:
    off = parent.data_start
    hard_end = end
    while off < hard_end:
        if parent.size is None and boundary_ids:
            # Unknown-size container: a boundary id at this position closes it.
            if _looks_like_id(buf, off, boundary_ids):
                hard_end = off
                break
        try:
            child = parse_element(buf, off, hard_end, container_ids)
        except EBMLDecodeError:
            break
        parent.children.append(child)
        if child.data_end <= off:
            break
        off = child.data_end
    return hard_end


def parse_root(buf: bytes) -> list[Element]:
    """Parse a whole Matroska stream at the top level (EBML header + Segment)."""
    elements: list[Element] = []
    off = 0
    while off < len(buf):
        try:
            el = parse_element(buf, off, len(buf), CONTAINER_IDS)
        except EBMLDecodeError:
            break
        elements.append(el)
        if el.data_end <= off:
            break
        off = el.data_end
    return elements


CONTAINER_IDS: set[int] = set()


def register_containers(ids: set[int]) -> None:
    CONTAINER_IDS.update(ids)

