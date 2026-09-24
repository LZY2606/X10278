"""EBML variable-length integers (VINT) and canonical element encoding."""
from __future__ import annotations

UNKNOWN_LENGTH = object()
VINT_WIDTH_BITS = {0x80: 1, 0x40: 2, 0x20: 3, 0x10: 4, 0x08: 5, 0x04: 6, 0x02: 7, 0x01: 8}


class VintError(ValueError):
    pass


def read_vint(buf: bytes, pos: int, *, allow_all_ones: bool = True):
    """Return (value, width, raw_bytes).

    For a length VINT, an all-ones payload means ``UNKNOWN_LENGTH``.
    For an ID VINT, all ones is still a legal (if reserved) payload.
    """
    if pos >= len(buf):
        raise VintError("truncated VINT marker")
    marker = buf[pos]
    width = next((w for bit, w in VINT_WIDTH_BITS.items() if marker & bit), None)
    if width is None:
        raise VintError(f"invalid VINT marker 0x{marker:02x}")
    if pos + width > len(buf):
        raise VintError("truncated VINT body")
    raw = buf[pos:pos + width]
    value = marker & (0xFF >> width)
    for byte in raw[1:]:
        value = (value << 8) | byte
    all_ones = value == (1 << (7 * width)) - 1
    if all_ones and not allow_all_ones:
        raise VintError("reserved all-ones VINT payload")
    if all_ones:
        return UNKNOWN_LENGTH, width, raw.hex()
    return value, width, raw.hex()


def encode_len(value: int | object, width: int | None = None) -> bytes:
    if value is UNKNOWN_LENGTH:
        width = width or 8
        return bytes([VINT_MARK[width]]) + b"\xff" * (width - 1)
    if value < 0:
        raise VintError("cannot encode negative length")
    need = 1
    while value > (1 << (7 * need)) - 2 and need < 8:
        need += 1
    width = width or need
    marker = 1 << (8 - width)
    payload = value + marker
    return payload.to_bytes(width, "big")


VINT_MARK = {1: 0x80, 2: 0x40, 3: 0x20, 4: 0x10, 5: 0x08, 6: 0x04, 7: 0x02, 8: 0x01}


def encode_id(element_id: int) -> bytes:
    """Element ids are already in their canonical marked form."""
    width = (element_id.bit_length() + 7) // 8
    if width < 1 or width > 4:
        raise VintError(f"element id 0x{element_id:x} out of range")
    return element_id.to_bytes(width, "big")


def encode_uint(data: int, width: int | None = None) -> bytes:
    if width is None:
        width = max(1, (data.bit_length() + 7) // 8)
    return data.to_bytes(width, "big")


def encode_float(data: float, width: int = 8) -> bytes:
    import struct
    return struct.pack(">d" if width == 8 else ">f", data)


def encode_element(element_id: int, payload: bytes) -> bytes:
    return encode_id(element_id) + encode_len(len(payload)) + payload
