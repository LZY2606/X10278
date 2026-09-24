"""A small evidence-preserving EBML/Matroska parser.

Unlike a normal demuxer, the parser keeps *everything* required by the
timecode audit:

* element id / data byte ranges (absolute file offsets),
* unknown-length markers, without guessing a length,
* original encoded integer/float representations,
* the raw block payloads (so lacing and padding can be inspected),
* hierarchical structure and sibling order.

It never fabricates frame boundaries. Unknown master size is resolved using
the *enclosing* element's declared end (or EOF); the parser records that the
size was unknown rather than inventing one.
"""
from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass, field
from typing import Optional

from . import ids
from .units import Raw

VINT_MAX_BYTES = 8


class EbmlError(ValueError):
    pass


def read_vint(buf: bytes, pos: int) -> tuple[int, int, int, bool]:
    """Return (value, length, marker) with ``value`` including length bit.

    ``unknown`` is True when all data bits are one (unknown-size VINT).
    """
    if pos >= len(buf):
        raise EbmlError(f"VINT out of range @ {pos}")
    first = buf[pos]
    if first == 0:
        raise EbmlError(f"invalid VINT leading octet 0x00 @ {pos}")
    length = 0
    marker = 0x80
    while marker:
        if first & marker:
            break
        length += 1
        marker >>= 1
    length += 1  # marker octet counts
    if pos + length > len(buf):
        raise EbmlError(f"truncated VINT @ {pos}")
    raw = int.from_bytes(buf[pos:pos + length], "big")
    mask = (1 << (7 * length)) - 1
    value = raw & mask
    unknown = value == mask
    return value, length, raw, unknown


def encode_vint(value: int, length: Optional[int] = None,
                keep_marker: bool = False) -> bytes:
    """Encode an unsigned VINT. ``length`` forces the width in octets."""
    for width in (range(length, length + 1) if length else range(1, VINT_MAX_BYTES + 1)):
        if value < (1 << (7 * width)) - 1:
            data = value | (1 << (7 * width)) if keep_marker else value
            return data.to_bytes(width, "big")
    raise EbmlError(f"value {value} does not fit VINT")


@dataclass
class Element:
    eid: int
    name: str
    kind: str
    id_start: int
    id_end: int
    size_start: int
    size_end: int
    data_start: int
    data_end: int
    unknown_size: bool = False
    raw_size: Optional[int] = None
    parent: Optional["Element"] = None
    children: list["Element"] = field(default_factory=list)
    ordinal: int = 0
    uid: str = ""
    path: str = ""
    _value: object = None
    raw_payload: bytes = b""

    @property
    def header_range(self) -> tuple[int, int]:
        return (self.id_start, self.size_end)

    @property
    def range(self) -> tuple[int, int]:
        return (self.id_start, self.data_end)

    def get(self, name: str) -> Optional["Element"]:
        for c in self.children:
            if c.name == name:
                return c
        return None

    def findall(self, name: str) -> list["Element"]:
        return [c for c in self.children if c.name == name]

    def value(self):
        return self._value


def _decode_scalar(kind: str, payload: bytes) -> object:
    if kind in ("uint",):
        return int.from_bytes(payload, "big", signed=False) if payload else 0
    if kind in ("int",):
        return int.from_bytes(payload, "big", signed=True) if payload else 0
    if kind == "float":
        if len(payload) == 4:
            return struct.unpack(">f", payload)[0]
        if len(payload) == 8:
            return struct.unpack(">d", payload)[0]
        return None
    if kind == "string":
        return payload.split(b"\x00", 1)[0].decode("utf-8", "replace")
    if kind in ("binary", "unknown"):
        return payload
    return payload


def _stable_uid(el: "Element", path_prefix: str) -> str:
    basis = f"{path_prefix}/{el.name}[{el.ordinal}]@{el.id_start}"
    return "el_" + hashlib.sha1(basis.encode()).hexdigest()[:16]


def parse(buf: bytes) -> list[Element]:
    """Parse the full file into a forest of level-0 :class:`Element` trees."""
    roots: list[Element] = []
    pos = 0
    seen0: dict[str, int] = {}
    while pos < len(buf):
        eid0, _idl, _r, _u = read_vint(buf, pos)
        n0, _k = ids.lookup(eid0)
        ord0 = seen0.get(n0, 0)
        el, pos = _parse_element(buf, pos, None, end=len(buf),
                                 path_prefix="", ordinal=ord0)
        seen0[n0] = ord0 + 1
        roots.append(el)
    return roots


def _parse_element(buf, pos, parent, end, path_prefix, ordinal) -> tuple[Element, int]:
    id_start = pos
    eid, id_len, _raw, _unk = read_vint(buf, pos)
    pos += id_len
    id_end = pos
    size_start = pos
    size, size_len, raw_size, unknown = read_vint(buf, pos)
    pos += size_len
    size_end = pos
    data_start = pos
    name, kind = ids.lookup(eid)
    el = Element(eid=eid, name=name, kind=kind, id_start=id_start, id_end=id_end,
                 size_start=size_start, size_end=size_end, data_start=data_start,
                 data_end=data_start, unknown_size=unknown, raw_size=raw_size,
                 parent=parent, ordinal=ordinal)
    el.path = f"{path_prefix}/{name}[{ordinal}]"
    el.uid = _stable_uid(el, path_prefix)

    if unknown:
        data_end = end  # bounded by enclosing element/EOF; size remains unknown
    else:
        data_end = data_start + size
        if data_end > end:
            raise EbmlError(
                f"element {name} @ {id_start} overruns parent end {end}")
    payload = buf[data_start:data_end]
    el.data_end = data_end
    el.raw_payload = payload

    if kind == "master":
        child_pos = data_start
        seen: dict[str, int] = {}
        while child_pos < data_end:
            cid, cid_len, _r, _u = read_vint(buf, child_pos)
            cname, _ckind = ids.lookup(cid)
            cord = seen.get(cname, 0)
            child, child_pos = _parse_element(
                buf, child_pos, el, end=data_end, path_prefix=el.path, ordinal=cord)
            seen[cname] = cord + 1
            el.children.append(child)
    else:
        el._value = _decode_scalar(kind, payload)

    return el, data_end


def segment_of(roots: list[Element]) -> Optional[Element]:
    for r in roots:
        if r.name == "Segment":
            return r
    return None


def file_digest(buf: bytes) -> str:
    return "sha256:" + hashlib.sha256(buf).hexdigest()
