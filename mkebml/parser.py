"""Streaming EBML tree parser.

Every parsed element remembers its on-disk coordinates:

* ``id_offset``  – absolute byte offset of the element id
* ``header_size``– bytes used by id + length VINT
* ``data_start`` – absolute offset of the first payload byte
* ``size``       – declared payload length, or ``None`` for unknown length
* ``data_end``   – resolved payload end (unknown length is resolved against
                    the surrounding container / end of file, never guessed
                    in the middle of a sibling stream)

Unknown lengths are recorded, not hidden.  Malformed bytes stop the affected
master and surface as diagnostics; already-parassed elements stay intact.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import ids
from .units import Diagnostic
from .vint import read_vint, UNKNOWN_LENGTH, VintError


@dataclass
class Element:
    id: int
    id_offset: int
    header_size: int
    size: int | None
    size_width: int
    size_raw_hex: str
    data_start: int
    data_end: int            # resolved absolute end
    unknown_length: bool
    children: list["Element"] = field(default_factory=list)
    blob: bytes | None = None
    diagnostics: list[Diagnostic] = field(default_factory=list)

    @property
    def name(self) -> str:
        return ids.name_of(self.id)

    @property
    def elem_end(self) -> int:
        return self.data_end + (self.data_start - self.id_offset)

    def payload(self) -> bytes:
        return self.blob[self.data_start:self.data_end]

    def find(self, element_id: int) -> "Element | None":
        for child in self.children:
            if child.id == element_id:
                return child
        return None

    def find_all(self, element_id: int) -> list["Element"]:
        return [c for c in self.children if c.id == element_id]

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()

    def to_json(self) -> dict[str, Any]:
        return {
            "id": f"0x{self.id:X}",
            "name": self.name,
            "id_offset": self.id_offset,
            "header_size": self.header_size,
            "size": self.size,
            "unknown_length": self.unknown_length,
            "size_width": self.size_width,
            "size_raw": self.size_raw_hex,
            "data_start": self.data_start,
            "data_end": self.data_end,
            "byte_range": [self.id_offset, self.elem_end],
            "known": self.id in ids.NAMES,
            "is_master": bool(self.children) or self.id in ids.MASTER,
            "diagnostics": [d.to_json() for d in self.diagnostics],
            "children": [c.to_json() for c in self.children],
        }


def _parse_master(blob: bytes, start: int, end: int, depth: int,
                  diagnostics: list[Diagnostic],
                  parent_id: int | None) -> list[Element]:
    elements: list[Element] = []
    pos = start
    while pos < end:
        try:
            elem_id, id_width, _id_raw = read_vint(blob, pos,
                                                   allow_all_ones=True)
        except VintError as exc:
            diagnostics.append(Diagnostic(
                "EBML_ID_TRUNCATED", f"cannot read element id @{pos}: {exc}",
                "error", evidence={"offset": pos, "container_end": end}))
            break
        id_offset = pos
        len_pos = pos + id_width
        try:
            size, size_width, size_raw = read_vint(blob, len_pos,
                                                   allow_all_ones=True)
        except VintError as exc:
            diagnostics.append(Diagnostic(
                "EBML_LEN_TRUNCATED", f"cannot read length @{len_pos}: {exc}",
                "error", evidence={"id_offset": id_offset}))
            break
        data_start = len_pos + size_width
        unknown = size is UNKNOWN_LENGTH
        if unknown:
            size_value = None
        else:
            size_value = size
            if data_start + size_value > end:
                diagnostics.append(Diagnostic(
                    "EBML_PAYLOAD_OVERFLOW",
                    f"element 0x{elem_id:X} payload exceeds container end",
                    "error",
                    evidence={"id_offset": id_offset, "data_start": data_start,
                              "declared_size": size_value, "end": end}))
                size_value = end - data_start
        if elem_id in ids.MASTER:
            if unknown:
                if elem_id == ids.SEGMENT or elem_id in ids.SEGMENT_LEVEL:
                    child_end = end
                else:
                    diagnostics.append(Diagnostic(
                        "UNKNOWN_LENGTH_UNRESOLVED",
                        f"unknown-length master 0x{elem_id:X} has no boundary "
                        "rule inside this container", "error",
                        evidence={"id_offset": id_offset}))
                    child_end = end
            else:
                child_end = data_start + size_value
            elem = Element(elem_id, id_offset, id_width + size_width,
                           size_value, size_width, size_raw, data_start,
                           child_end, True, blob=blob)
            elem.children = _parse_master(blob, data_start, child_end,
                                          depth + 1, elem.diagnostics, elem_id)
            elements.append(elem)
            if unknown and elem_id in ids.SEGMENT_LEVEL:
                # Unknown length extends to end of segment; no more siblings.
                break
        else:
            data_end = end if unknown else data_start + size_value
            elem = Element(elem_id, id_offset, id_width + size_width,
                           size_value, size_width, size_raw, data_start,
                           data_end, unknown, blob=blob)
            if unknown:
                diagnostics.append(Diagnostic(
                    "UNKNOWN_LENGTH_LEAF",
                    f"unknown length on non-master 0x{elem_id:X}", "warning",
                    evidence={"id_offset": id_offset}))
            elements.append(elem)
        pos = data_start + (size_value if size_value is not None
                            else end - data_start)
        if size is UNKNOWN_LENGTH:
            break
    return elements


def parse_ebml(blob: bytes) -> tuple[list[Element], list[Diagnostic]]:
    """Parse a Matroska/WebM byte string into a top-level element tree."""
    diagnostics: list[Diagnostic] = []
    roots = _parse_master(blob, 0, len(blob), 0, diagnostics, None)
    return roots, diagnostics
