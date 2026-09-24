"""Structural EBML parser.

The parser only understands EBML framing plus the small set of Matroska
master membership rules from :mod:`mke.catalog`.  Every element keeps its
absolute byte coordinates so that later evidence (cues, damaged ranges) can
be projected back onto the original bytes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from . import catalog
from .ebml import (
    EBMLError,
    decode_float,
    decode_sint,
    decode_string,
    decode_uint,
    encode_id,
    read_vint,
)


class ParseError(EBMLError):
    def __init__(self, message: str, position: int):
        super().__init__(f"@{position}: {message}")
        self.position = position


@dataclass
class Element:
    element_id: int
    start: int  # absolute offset of the id VINT
    size_pos: int  # absolute offset of the size VINT
    size_width: int
    data_start: int
    data_end: int  # exclusive; for unknown size this is where parsing stopped
    unknown_size: bool
    declared_size: Optional[int]
    path: list[int]  # child index path from the document root
    value: object = None
    children: list["Element"] = field(default_factory=list)
    data_hex: str = ""

    @property
    def end(self) -> int:
        return self.data_end

    @property
    def name(self) -> str:
        return catalog.elem_name(self.element_id)

    @property
    def type(self) -> str:
        return catalog.elem_type(self.element_id)

    def as_range(self) -> dict:
        return {
            "id_start": self.start,
            "size_pos": self.size_pos,
            "size_width": self.size_width,
            "data_start": self.data_start,
            "data_end": self.data_end,
            "element_end": self.data_end,
            "unknown_size": self.unknown_size,
            "declared_size": self.declared_size,
        }

    def to_json(self) -> dict:
        return {
            "element_id": self.element_id,
            "id_hex": encode_id(self.element_id).hex(),
            "name": self.name,
            "type": self.type,
            **self.as_range(),
            "path": list(self.path),
            "value": self.value,
            "data_hex": self.data_hex,
            "children": [c.to_json() for c in self.children],
        }


def _decode_leaf(buf: bytes, element_id: int) -> object:
    etype = catalog.elem_type(element_id)
    if not buf:
        return None
    try:
        if etype == catalog.UINT:
            return decode_uint(buf)
        if etype == catalog.SINT:
            return decode_sint(buf)
        if etype == catalog.FLOAT:
            return decode_float(buf)
        if etype == catalog.STRING:
            return decode_string(buf)
    except EBMLError:
        return None
    return None


def _parse_element(buf: bytes, pos: int, path: list[int],
                   parent_id: Optional[int]) -> tuple[Element, int]:
    id_vint = read_vint(buf, pos, is_id=True)
    element_id = id_vint.value
    size_pos = pos + id_vint.width
    try:
        size_vint = read_vint(buf, size_pos)
    except EBMLError as exc:
        raise ParseError(str(exc), size_pos) from exc
    data_start = size_pos + size_vint.width
    etype = catalog.elem_type(element_id)

    children: list[Element] = []
    value: object = None
    data_hex = ""

    if size_vint.unknown:
        if etype != catalog.MASTER:
            raise ParseError("unknown length on non-master element", size_pos)
        data_end = _parse_unknown_master(
            buf, data_start, element_id, path, children
        )
        elem = Element(
            element_id=element_id, start=pos, size_pos=size_pos,
            size_width=size_vint.width, data_start=data_start,
            data_end=data_end, unknown_size=True, declared_size=None,
            path=list(path), children=children,
        )
        return elem, data_end

    size = size_vint.value
    data_end = data_start + size
    if data_end > len(buf):
        raise ParseError(
            f"element {catalog.elem_name(element_id)} declares {size} bytes, "
            f"only {len(buf) - data_start} available",
            size_pos,
        )
    payload = buf[data_start:data_end]
    if etype == catalog.MASTER:
        children = _parse_children(payload, data_start, element_id, path)
    elif etype == catalog.BINARY:
        data_hex = payload.hex()
    else:
        value = _decode_leaf(payload, element_id)
    elem = Element(
        element_id=element_id, start=pos, size_pos=size_pos,
        size_width=size_vint.width, data_start=data_start, data_end=data_end,
        unknown_size=False, declared_size=size, path=list(path),
        value=value, children=children, data_hex=data_hex,
    )
    return elem, data_end


def _parse_children(payload: bytes, base: int, parent_id: int,
                    path: list[int]) -> list[Element]:
    elements: list[Element] = []
    pos = 0
    index = 0
    while pos < len(payload):
        child_path = path + [index]
        elem, _ = _parse_element(payload, pos, child_path, parent_id)
        elements.append(elem)
        pos = elem.data_end
        index += 1
    _rebase(elements, base)
    return elements


def _rebase(elements: list[Element], base: int) -> None:
    for elem in elements:
        elem.start += base
        elem.size_pos += base
        elem.data_start += base
        elem.data_end += base
        if elem.children:
            _rebase(elem.children, base)


def _walk(elem: Element):
    yield elem
    for child in elem.children:
        yield from _walk(child)


def _parse_unknown_master(buf: bytes, start: int, parent_id: int,
                          path: list[int], out: list[Element]) -> int:
    allowed = catalog.allowed_children(parent_id)
    pos = start
    index = 0
    end_limit = len(buf)
    while pos < end_limit:
        try:
            probe = read_vint(buf, pos, is_id=True)
        except EBMLError:
            break
        candidate = probe.value
        if allowed is not None and candidate not in allowed:
            break
        if catalog.elem_type(candidate) == catalog.BINARY and candidate not in (allowed or set()):
            break
        try:
            elem, next_pos = _parse_element(buf, pos, path + [index], parent_id)
        except ParseError:
            break
        if elem.data_end < pos:
            break
        out.append(elem)
        pos = next_pos
        index += 1
    return pos


def parse_document(buf: bytes) -> list[Element]:
    """Parse a complete EBML document into its top level elements."""
    elements: list[Element] = []
    pos = 0
    index = 0
    while pos < len(buf):
        elem, next_pos = _parse_element(buf, pos, [index], None)
        elements.append(elem)
        if next_pos <= pos:
            raise ParseError("parser made no progress", pos)
        pos = next_pos
        index += 1
    return elements


def flatten(elements: list[Element]) -> list[Element]:
    out: list[Element] = []
    for elem in elements:
        out.append(elem)
        out.extend(flatten(elem.children))
    return out
