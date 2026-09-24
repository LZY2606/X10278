"""Byte-exact EBML reader/writer.

The parser never throws away raw structure: every element keeps its absolute
offset, the raw header span, declared length (including "unknown length"),
and raw payload bytes.  Nothing here is Matroska-specific.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

UNKNOWN = -1  # declared length sentinel: all remaining bytes until parent end


@dataclass
class ParseError:
    offset: int
    code: str
    detail: str

    def to_dict(self) -> dict:
        return {"offset": self.offset, "code": self.code, "detail": self.detail}


@dataclass
class Element:
    eid: int                      # full EBML id as read (e.g. 0x1A45DFA3)
    name: str                     # catalogue name or "Unknown(0x..)"
    offset: int                   # absolute file offset of the id's first byte
    header_len: int               # id vint length + size vint length
    size: int                     # declared payload length, UNKNOWN (-1) if unknown
    raw_size: bytes               # exact size-vint bytes from the file
    data: bytes = b""             # exact payload bytes as present in the file
    children: List["Element"] = field(default_factory=list)
    parse_error: Optional[ParseError] = None

    # ---- geometry -----------------------------------------------------
    @property
    def data_offset(self) -> int:
        return self.offset + self.header_len

    @property
    def end_offset(self) -> int:
        """Absolute offset just past the element; None-equivalent is -1 for unknown."""
        if self.size == UNKNOWN:
            return -1
        return self.data_offset + self.size

    @property
    def byte_range(self) -> Tuple[int, int]:
        """Half-open [start, end) byte range actually occupied in the file."""
        return (self.offset, self._occupied_end())

    def _occupied_end(self) -> int:
        if self.size != UNKNOWN:
            return self.data_offset + self.size
        if self.children:
            return self.children[-1]._occupied_end()
        return self.data_offset + len(self.data)

    @property
    def known_end(self) -> Optional[int]:
        if self.size != UNKNOWN:
            return self.data_offset + self.size
        return None

    # ---- lookup -------------------------------------------------------
    def find(self, eid: int) -> Optional["Element"]:
        for child in self.children:
            if child.eid == eid:
                return child
        return None

    def find_all(self, eid: int) -> List["Element"]:
        return [c for c in self.children if c.eid == eid]

    def select(self, *path: int) -> Optional["Element"]:
        node: Optional[Element] = self
        for eid in path:
            if node is None:
                return None
            node = node.find(eid)
        return node

    def to_dict(self) -> dict:
        start, end = self.byte_range
        return {
            "eid": f"0x{self.eid:X}",
            "name": self.name,
            "offset": self.offset,
            "data_offset": self.data_offset,
            "header_len": self.header_len,
            "size": None if self.size == UNKNOWN else self.size,
            "unknown_size": self.size == UNKNOWN,
            "byte_range": [start, end],
            "raw_size_vint": self.raw_size.hex(),
            "parse_error": self.parse_error.to_dict() if self.parse_error else None,
            "children": [c.to_dict() for c in self.children],
        }


# ----------------------------------------------------------------------
# VINT
# ----------------------------------------------------------------------
def read_vint(buf: bytes, pos: int) -> Tuple[int, int, bytes]:
    """Return (value, vint_length, raw_bytes).  value is -1 for an
    all-ones data part, i.e. EBML "unknown length"."""
    if pos >= len(buf):
        raise EBMLError(pos, "TRUNCATED_VINT", "no byte for VINT")
    first = buf[pos]
    if first == 0:
        raise EBMLError(pos, "BAD_VINT", "VINT length marker is zero")
    length = first.bit_length() - 8 + 1 if first < 0x100 else 0
    length = (first - 1).bit_length() ^ 7  # robust: see _vint_len
    length = _vint_len(first)
    if pos + length > len(buf):
        raise EBMLError(pos, "TRUNCATED_VINT",
                        f"need {length} bytes, have {len(buf) - pos}")
    raw = buf[pos:pos + length]
    value = first & (0xFF >> length)
    for b in raw[1:]:
        value = (value << 8) | b
    if value == (1 << (7 * length)) - 1:
        return UNKNOWN, length, raw
    return value, length, raw


def _vint_len(first: int) -> int:
    for i in range(8):
        if first & (0x80 >> i):
            return i + 1
    raise EBMLError(0, "BAD_VINT", "no length marker in VINT first byte")


def encode_vint(value: int, length: Optional[int] = None) -> bytes:
    """Encode an unsigned integer as an EBML VINT.  Defaults to shortest."""
    if value < 0:
        raise ValueError("cannot encode negative VINT")
    if length is None:
        length = 1
        while value >= (1 << (7 * length)) - 1 and length < 8:
            length += 1
    marker = 1 << (8 - length)
    bits = 7 * length
    if value >= (1 << bits) - 1:
        raise ValueError(f"value {value} needs VINT all-ones (reserved)")
    out = marker | value
    return out.to_bytes(length, "big")


def encode_unknown(length: int) -> bytes:
    return bytes([0xFF >> (8 - length) | 0] if length == 1 else
                 _unknown_bytes(length))


def _unknown_bytes(length: int) -> bytes:
    if length == 1:
        return b"\xff"
    return bytes([0x01 << (8 - length)] + [0xFF] * (length - 1))


def encode_id(eid: int) -> bytes:
    n = (eid.bit_length() + 7) // 8
    return eid.to_bytes(n, "big")


def encode_uint(value: int) -> bytes:
    if value == 0:
        return b"\x00"
    return value.to_bytes((value.bit_length() + 7) // 8, "big")


# ----------------------------------------------------------------------
# Parsing
# ----------------------------------------------------------------------
class EBMLError(Exception):
    def __init__(self, offset: int, code: str, detail: str):
        super().__init__(f"{code}@{offset}: {detail}")
        self.offset = offset
        self.code = code
        self.detail = detail


@dataclass
class Catalogue:
    names: Dict[int, str] = field(default_factory=dict)
    container_ids: set = field(default_factory=set)

    def name_of(self, eid: int) -> str:
        return self.names.get(eid, f"Unknown(0x{eid:X})")

    def is_container(self, eid: int) -> bool:
        return eid in self.container_ids


def parse_element(buf: bytes, pos: int, end: int, cat: Catalogue,
                  depth: int = 0) -> Element:
    id_start = pos
    try:
        eid, id_len, _ = read_vint(buf, pos)
    except EBMLError as exc:
        raise
    pos += id_len
    try:
        size, size_len, raw_size = read_vint(buf, pos)
    except EBMLError as exc:
        exc.offset = pos
        raise
    header_len = id_len + size_len

    if size == UNKNOWN:
        data_end = end  # extends to parent boundary
    else:
        data_end = pos + size_len + size
        if data_end > end:
            data_end = min(end, data_end)
    declared_end = (pos + size_len + size) if size != UNKNOWN else end

    elem = Element(eid=eid, name=cat.name_of(eid), offset=id_start,
                   header_len=header_len, size=size, raw_size=raw_size)

    body_start = pos + size_len
    actual_end = min(declared_end, end)
    elem.data = buf[body_start:actual_end]

    if cat.is_container(eid):
        _parse_children(elem, buf, body_start, actual_end, cat, depth)
    return elem


def _parse_children(elem: Element, buf: bytes, start: int, end: int,
                    cat: Catalogue, depth: int) -> None:
    pos = start
    while pos < end:
        try:
            child = parse_element(buf, pos, end, cat, depth + 1)
        except EBMLError as exc:
            elem.parse_error = ParseError(exc.offset, exc.code, exc.detail)
            return
        elem.children.append(child)
        if child.end_offset == -1:
            return
        pos = child.end_offset


def parse_tree(buf: bytes, cat: Catalogue, root_id: Optional[int] = None,
               start: int = 0, end: Optional[int] = None) -> List[Element]:
    end = len(buf) if end is None else end
    out: List[Element] = []
    pos = start
    while pos < end:
        elem = parse_element(buf, pos, end, cat)
        out.append(elem)
        if elem.end_offset == -1:
            break
        pos = elem.end_offset
    return out


# ----------------------------------------------------------------------
# Building
# ----------------------------------------------------------------------
def element(eid: int, payload: bytes = b"", *, unknown: bool = False,
            size_len: Optional[int] = None) -> bytes:
    head = encode_id(eid)
    if unknown:
        head += _unknown_bytes(size_len or 1)
    else:
        head += encode_vint(len(payload), size_len)
    return head + payload


def uint_element(eid: int, value: int) -> bytes:
    return element(eid, encode_uint(value))


def float_element(eid: int, value: float) -> bytes:
    return element(eid, b"".join(
        int.from_bytes(b, "big").to_bytes(1, "big") for b in
        __import__("struct").pack(">d", value)))


def string_element(eid: int, value: str) -> bytes:
    return element(eid, value.encode("utf-8"))


def binary_element(eid: int, value: bytes) -> bytes:
    return element(eid, value)


def container(eid: int, *parts: bytes, unknown: bool = False,
              size_len: Optional[int] = None) -> bytes:
    return element(eid, b"".join(parts), unknown=unknown, size_len=size_len)
