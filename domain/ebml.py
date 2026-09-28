from __future__ import annotations

from dataclasses import dataclass
import struct


class EbmlError(ValueError):
    pass


@dataclass(frozen=True)
class Vint:
    value: int
    width: int
    unknown: bool = False


@dataclass
class Element:
    id: bytes
    offset: int
    header_end: int
    data_end: int
    unknown_length: bool
    size: int | None
    children: list["Element"]
    raw: bytes

    @property
    def header_range(self) -> tuple[int, int]:
        return (self.offset, self.header_end)

    @property
    def data_range(self) -> tuple[int, int]:
        return (self.header_end, self.data_end)

    @property
    def byte_range(self) -> tuple[int, int]:
        return (self.offset, self.data_end)

    def find(self, element_id: bytes) -> "Element | None":
        for child in self.children:
            if child.id == element_id:
                return child
        return None

    def find_all(self, element_id: bytes) -> list["Element"]:
        return [child for child in self.children if child.id == element_id]


def _vint_width(first_byte: int) -> int:
    if first_byte == 0:
        raise EbmlError("VINT marker byte is zero")
    width = 1
    marker = 0x80
    while marker and first_byte & marker == 0:
        marker >>= 1
        width += 1
    if marker == 0:
        raise EbmlError("VINT marker is missing")
    return width


def read_vint(buffer: bytes, offset: int, *, keep_marker: bool = False) -> Vint:
    if offset >= len(buffer):
        raise EbmlError("VINT starts beyond buffer")
    width = _vint_width(buffer[offset])
    if offset + width > len(buffer):
        raise EbmlError("VINT is truncated")
    first = buffer[offset]
    marker = 0x80 >> (width - 1)
    if width == 8 and first == marker:
        value = 0
    else:
        value = first & (marker - 1)
    unknown = first == marker and all(b == 0xFF for b in buffer[offset + 1 : offset + width])
    for byte in buffer[offset + 1 : offset + width]:
        value = (value << 8) | byte
    if keep_marker:
        value = int.from_bytes(buffer[offset : offset + width], "big")
    return Vint(value=value, width=width, unknown=unknown)


def encode_vint_length(length: int | None, width: int | None = None) -> bytes:
    if length is None:
        if width is None:
            raise EbmlError("Unknown length needs an explicit VINT width")
        if not 1 <= width <= 8:
            raise EbmlError("VINT width must be 1..8")
        if width == 1:
            return bytes([0xFF])
        return bytes([0x80 >> (width - 1)]) + b"\xff" * (width - 1)
    if length < 0:
        raise EbmlError("Length cannot be negative")
    if width is None:
        width = 1
        maximum = 2 ** (7 * width) - 2
        while length > maximum and width < 8:
            width += 1
            maximum = 2 ** (7 * width) - 2
        if length > maximum:
            raise EbmlError("Length does not fit in an EBML VINT")
    marker = 0x80 >> (width - 1)
    payload_bits = 7 * width
    first = marker | ((length >> (payload_bits - 7)) & (marker - 1))
    encoded = bytearray([first])
    for shift in range(payload_bits - 15, -1, -8):
        encoded.append((length >> shift) & 0xFF)
    return bytes(encoded)


def unsigned_int(payload: bytes) -> int:
    return int.from_bytes(payload or b"\x00", "big", signed=False)


def signed_int(payload: bytes) -> int:
    return int.from_bytes(payload or b"\x00", "big", signed=True)


def float_value(payload: bytes) -> float:
    if not payload:
        return 0.0
    if len(payload) == 4:
        return struct.unpack(">f", payload)[0]
    if len(payload) == 8:
        return struct.unpack(">d", payload)[0]
    raise EbmlError("EBML float must be 0, 4 or 8 bytes")


def encode_unsigned(value: int, *, width: int | None = None) -> bytes:
    if value < 0:
        raise EbmlError("Expected an unsigned integer")
    if width is None:
        width = max(1, (value.bit_length() + 7) // 8)
    return value.to_bytes(width, "big")


def encode_signed(value: int, width: int) -> bytes:
    return value.to_bytes(width, "big", signed=True)


def encode_float(value: float) -> bytes:
    return struct.pack(">d", value)


def el(element_id: bytes, payload: bytes | list["Element"] = b"", *, unknown: bool = False, size_width: int | None = None) -> Element:
    if isinstance(payload, list):
        body = b"".join(_encoded(child) for child in payload)
    else:
        body = payload
    size = None if unknown else len(body)
    header = element_id + encode_vint_length(None if unknown else size, size_width)
    raw = header + body
    children = payload if isinstance(payload, list) else []
    return Element(element_id, 0, len(header), len(header) + (len(body) if size is not None else 0), unknown, size, children, raw)


def _encoded(element: Element) -> bytes:
    return element.raw


def _parse_one(data: bytes, offset: int, outer_end: int) -> tuple[Element, int]:
    id_vint = read_vint(data, offset, keep_marker=True)
    element_id = data[offset : offset + id_vint.width]
    size_offset = offset + id_vint.width
    size_vint = read_vint(data, size_offset)
    data_start = size_offset + size_vint.width
    if size_vint.unknown:
        data_end = outer_end
        next_offset = data_end
    else:
        data_end = data_start + size_vint.value
        if data_end > outer_end:
            raise EbmlError(f"Element {element_id.hex()} ends beyond its parent at offset {offset}")
        next_offset = data_end
    raw = data[offset:data_end]
    element = Element(element_id, offset, data_start, data_end, size_vint.unknown, None if size_vint.unknown else size_vint.value, [], raw)
    return element, next_offset


def parse_children(data: bytes, start: int, end: int, *, unknown_parent: bool = False) -> list[Element]:
    children: list[Element] = []
    offset = start
    while offset < end:
        child, next_offset = _parse_one(data, offset, end)
        children.append(child)
        offset = next_offset
        if unknown_parent and next_offset == end:
            break
    return children


def parse_children_with_boundaries(data: bytes, start: int, end: int, boundary_ids: set[bytes]) -> list[Element]:
    children: list[Element] = []
    offset = start
    while offset < end:
        try:
            identifier = read_vint(data, offset, keep_marker=True)
            element_id = data[offset : offset + identifier.width]
            if element_id in boundary_ids:
                break
            child, next_offset = _parse_one(data, offset, end)
        except EbmlError:
            break
        children.append(child)
        offset = next_offset
        if child.unknown_length:
            boundary_end = _next_boundary(data, child.header_end, end, boundary_ids)
            child.data_end = boundary_end
            child.size = None
            child.raw = data[child.offset : boundary_end]
            child.children = parse_children_with_boundaries(data, child.header_end, boundary_end, set())
            offset = boundary_end
    return children


def _next_boundary(data: bytes, start: int, end: int, boundary_ids: set[bytes]) -> int:
    offset = start
    while offset < end:
        try:
            identifier = read_vint(data, offset, keep_marker=True)
            element_id = data[offset : offset + identifier.width]
            if element_id in boundary_ids:
                return offset
            _child, next_offset = _parse_one(data, offset, end)
            offset = next_offset
        except EbmlError:
            return end
    return end


def parse_tree(data: bytes, *, offset: int = 0, end: int | None = None) -> list[Element]:
    outer_end = len(data) if end is None else end
    roots: list[Element] = []
    position = offset
    while position < outer_end:
        element, next_position = _parse_one(data, position, outer_end)
        roots.append(element)
        position = next_position
        if roots and roots[-1].unknown_length:
            element.children = parse_children(data, element.header_end, outer_end, unknown_parent=True)
            break
    for root in roots:
        populate_children(data, root)
    return roots


def populate_children(data: bytes, parent: Element) -> None:
    start, end = parent.header_end, parent.data_end
    if start >= end:
        return
    try:
        position = start
        children: list[Element] = []
        while position < end:
            child, next_position = _parse_one(data, position, end)
            children.append(child)
            position = next_position
            if child.unknown_length:
                child.children = parse_children(data, child.header_end, end, unknown_parent=True)
                for nested in child.children:
                    populate_children(data, nested)
                break
        parent.children = children
        for child in parent.children:
            populate_children(data, child)
    except EbmlError:
        parent.children = []
