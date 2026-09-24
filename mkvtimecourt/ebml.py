"""Minimal EBML reader/writer that preserves offsets and raw byte ranges.

The parser never normalizes away the physical layout. Every element keeps:

* ``offset`` / ``header_len`` / ``data_len`` / ``total_len`` in file bytes;
* ``size_unknown`` when the size VINT encodes "unknown length";
* the parsed value only for the value classes we know how to interpret.

Unknown-length *master* elements span to the end of the containing range
(which mirrors how a muxer/reader must treat them); unknown-length value
elements stay ``value=None`` with a diagnostic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# VINT
# ---------------------------------------------------------------------------

VINT_MAX_WIDTH = 8


class EbmlError(ValueError):
    """Raised for structurally unusable EBML byte streams."""


def read_vint(buf: bytes, offset: int) -> tuple[int, int, bool, int]:
    """Read a VINT at ``offset``.

    Returns ``(value, length, is_unknown, header_offset)``.  The all-ones
    marker form (e.g. ``0xFF``) is decoded as unknown length with value set
    to the field maximum so downstream code can see the wire width.
    """
    if offset >= len(buf):
        raise EbmlError(f"VINT out of range at {offset}")
    first = buf[offset]
    if first == 0:
        raise EbmlError(f"VINT length marker must not be zero at {offset}")
    width = 8 - first.bit_length() + 1
    if width > VINT_MAX_WIDTH or offset + width > len(buf):
        raise EbmlError(f"VINT header truncated at {offset} (width {width})")
    value = first & ((1 << (8 - width)) - 1)
    all_ones = value == (1 << (8 - width)) - 1 and width > 1
    if width == 1:
        all_ones = first == 0xFF
    for idx in range(1, width):
        value = (value << 8) | buf[offset + idx]
    return value, width, bool(all_ones), offset


def encode_uint(value: int, width: int) -> bytes:
    if value < 0 or width < 1:
        raise ValueError("unsigned value must be non-negative")
    if value.bit_length() > width * 8:
        raise ValueError(f"value {value} does not fit in {width} bytes")
    return value.to_bytes(width, "big")


def encode_vint(value: int, width: int | None = None) -> bytes:
    """Encode an explicit VINT using the requested (or smallest) width."""
    if value < 0:
        raise ValueError("VINT value must be non-negative")
    if width is None:
        for candidate in range(1, VINT_MAX_WIDTH + 1):
            if value < (1 << (7 * candidate)) - 1:
                width = candidate
                break
        if width is None:  # pragma no cover - 56 bit values are absurd here
            raise EbmlError("value too large for VINT")
    marker = 1 << (8 - width)
    payload_bits = 7 * width
    if value >= (1 << payload_bits) - 1:
        raise EbmlError(f"value {value} collides with unknown marker for width {width}")
    raw = marker | value
    return raw.to_bytes(width, "big")


def unknown_size_vint(width: int) -> bytes:
    """Return the all-ones 'unknown size' VINT marker for a given width."""
    if not 1 <= width <= VINT_MAX_WIDTH:
        raise ValueError("width out of range")
    return b"\xff" if width == 1 else (b"\x01\xff" + b"\xff" * (width - 2))[1 - 1:]


def read_element_id(buf: bytes, offset: int) -> tuple[int, int]:
    value, width, unknown, _ = read_vint(buf, offset)
    if unknown:
        raise EbmlError(f"element id VINT must not be all-ones at {offset}")
    return value, width


# ---------------------------------------------------------------------------
# Raw elements
# ---------------------------------------------------------------------------

@dataclass
class RawElement:
    element_id: int
    id_hex: str
    offset: int
    header_len: int
    data_offset: int
    data_len: int | None  # None => unknown length
    size_unknown: bool
    value: Any = None
    value_kind: str = "master"
    children: list["RawElement"] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)

    @property
    def total_len(self) -> int | None:
        if self.data_len is None:
            return None
        return self.header_len + self.data_len

    @property
    def end_offset(self) -> int | None:
        if self.data_len is None:
            return None
        return self.data_offset + self.data_len

    def to_dict(self) -> dict:
        return {
            "element_id": self.element_id,
            "id_hex": self.id_hex,
            "offset": self.offset,
            "header_len": self.header_len,
            "data_offset": self.data_offset,
            "data_len": self.data_len,
            "size_unknown": self.size_unknown,
            "value": self.value,
            "value_kind": self.value_kind,
            "diagnostics": list(self.diagnostics),
            "children": [c.to_dict() for c in self.children],
        }


def id_hex(element_id: int) -> str:
    """Canonical Matroska-style hex id (1A45DFA3 with no 0x prefix)."""
    width = max(1, (element_id.bit_length() + 7) // 8)
    return element_id.to_bytes(width, "big").hex().upper()


def _parse_uint(data: bytes) -> int:
    return int.from_bytes(data, "big", signed=False)


def _parse_int(data: bytes) -> int:
    return int.from_bytes(data, "big", signed=True)


def _parse_float(data: bytes) -> float:
    if len(data) == 4:
        import struct

        return float(struct.unpack(">f", data)[0])
    if len(data) == 8:
        import struct

        return struct.unpack(">d", data)[0]
    raise EbmlError(f"float must be 4 or 8 bytes, got {len(data)}")


def _parse_string(data: bytes) -> str:
    return data.rstrip(b"\x00").decode("utf-8", errors="replace")


# value class lookup is populated by the schema layer to avoid a circular
# dependency; the parser defaults to binary.
VALUE_KIND_DEFAULT: dict[int, str] = {}


def parse_elements(
    buf: bytes,
    start: int,
    end: int,
    master_ids: set[int] | None = None,
    value_kinds: dict[int, str] | None = None,
    diagnostics: list[str] | None = None,
) -> list[RawElement]:
    """Parse elements in ``buf[start:end]``.

    ``master_ids`` controls which ids are recursively parsed as masters.
    Unknown-length masters consume the remainder of ``end``.
    """
    master_ids = master_ids or set()
    value_kinds = value_kinds or VALUE_KIND_DEFAULT
    out: list[RawElement] = []
    pos = start
    while pos < end:
        el_start = pos
        try:
            element_id, id_len = read_element_id(buf, pos)
        except EbmlError as exc:
            (diagnostics if diagnostics is not None else []).append(str(exc))
            break
        pos += id_len
        if pos >= end:
            diag = f"truncated size VINT for element {id_hex(element_id)} at {el_start}"
            if diagnostics is not None:
                diagnostics.append(diag)
            break
        size_value, size_len, size_unknown, _ = read_vint(buf, pos)
        pos += size_len
        header_len = id_len + size_len
        data_offset = pos
        local_diag: list[str] = []
        if size_unknown:
            data_len: int | None = None
            if element_id in master_ids:
                child_end = end
            else:
                child_end = None
                local_diag.append(
                    f"unknown length on non-master element {id_hex(element_id)} "
                    f"at {el_start}; value not interpreted"
                )
        else:
            data_len = size_value
            child_end = data_offset + data_len
            if child_end > end:
                local_diag.append(
                    f"element {id_hex(element_id)} at {el_start} declares "
                    f"{data_len} bytes but only {end - data_offset} remain"
                )
                child_end = end
        value = None
        value_kind = "binary"
        children: list[RawElement] = []
        if element_id in master_ids:
            value_kind = "master"
            if child_end is not None:
                children = parse_elements(
                    buf,
                    data_offset,
                    child_end,
                    master_ids,
                    value_kinds,
                    local_diag,
                )
        else:
            value_kind = value_kinds.get(element_id, "binary")
            value_end = child_end if child_end is not None else end
            data = buf[data_offset:value_end] if child_end is not None else b""
            if not size_unknown and data_len is not None:
                data = buf[data_offset : data_offset + data_len]
                try:
                    if value_kind == "uint":
                        value = _parse_uint(data)
                    elif value_kind == "int":
                        value = _parse_int(data)
                    elif value_kind == "float":
                        value = _parse_float(data) if data else None
                    elif value_kind in ("string", "utf8"):
                        value = _parse_string(data)
                    else:
                        value = data.hex()
                except EbmlError as exc:
                    local_diag.append(str(exc))
                    value = data.hex()
        el = RawElement(
            element_id=element_id,
            id_hex=id_hex(element_id),
            offset=el_start,
            header_len=header_len,
            data_offset=data_offset,
            data_len=(child_end - data_offset) if size_unknown and element_id in master_ids else data_len,
            size_unknown=size_unknown,
            value=value,
            value_kind=value_kind,
            children=children,
            diagnostics=local_diag,
        )
        out.append(el)
        if size_unknown:
            # unknown length consumes remainder by definition
            break
        pos = child_end  # type: ignore[assignment]
        if pos is None:
            break
    return out


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------

def encode_element(
    element_id: int,
    payload: bytes | list["RawElement"],
    unknown_length: bool = False,
    size_width: int | None = None,
) -> bytes:
    id_bytes = id_hex(element_id)
    id_raw = bytes.fromhex(id_bytes)
    if isinstance(payload, list):
        body = b"".join(
            encode_element(
                c.element_id,
                c.children if c.value_kind == "master" else _encode_value(c),
                unknown_length=c.size_unknown,
            )
            for c in payload
        )
    else:
        body = payload
    if unknown_length:
        size_raw = unknown_size_vint(size_width or 1)
    else:
        size_raw = encode_vint(len(body), size_width)
    return id_raw + size_raw + body


def _encode_value(el: RawElement) -> bytes:
    if el.value_kind == "binary" or el.value is None:
        return bytes.fromhex(el.value) if isinstance(el.value, str) else b""
    if el.value_kind in ("uint", "int"):
        signed = el.value_kind == "int"
        width = max(1, (int(el.value).bit_length() + (8 if signed else 7)) // 8)
        return int(el.value).to_bytes(width, "big", signed=signed)
    if el.value_kind == "float":
        import struct

        return struct.pack(">d", float(el.value))
    return str(el.value).encode("utf-8")


def element(element_id: int, value: bytes | list, width: int | None = None,
            unknown: bool = False) -> bytes:
    """Convenience serializer for hand-built trees."""
    if isinstance(value, list):
        body = b"".join(value)
    else:
        body = value
    size = unknown_size_vint(width or 1) if unknown else encode_vint(len(body), width)
    return bytes.fromhex(id_hex(element_id)) + size + body


def uint_field(element_id: int, value: int, width: int | None = None) -> bytes:
    if width is None:
        width = max(1, (value.bit_length() + 7) // 8) or 1
    return element(element_id, encode_uint(value, width))


def int_field(element_id: int, value: int, width: int | None = None) -> bytes:
    if width is None:
        width = max(1, (int(value).bit_length() + 7) // 8) or 1
        if value < 0:
            width = max(1, (8 + (-(value + 1)).bit_length() + 7) // 8)
    return element(element_id, int(value).to_bytes(width, "big", signed=True))


def float_field(element_id: int, value: float) -> bytes:
    import struct

    return element(element_id, struct.pack(">d", value))


def text_field(element_id: int, value: str) -> bytes:
    return element(element_id, value.encode("utf-8"))


def binary_field(element_id: int, value: bytes) -> bytes:
    return element(element_id, value)
