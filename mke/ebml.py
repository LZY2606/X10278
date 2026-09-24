"""EBML low level coding primitives.

Everything here works on raw ``bytes`` and keeps the distinction between the
on-disk representation and normalised Python values explicit.  Nothing in this
module knows anything about Matroska semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
from struct import unpack


class EBMLError(ValueError):
    """Raised when a byte sequence is not a legal EBML encoding."""


UNKNOWN_ALL = 0xFFFFFFFFFF  # 5 bytes max for the sizes we emit


def length_of_vint(first_byte: int) -> int:
    """Return total VINT width indicated by a leading marker bit."""
    if not first_byte:
        raise EBMLError("VINT length marker is zero")
    width = 1
    bit = 0x80
    while width <= 8:
        if first_byte & bit:
            return width
        bit >>= 1
        width += 1
    raise EBMLError("no VINT length marker in first byte")


def is_all_ones(payload: bytes, width: int) -> bool:
    """True when every payload bit of a VINT is one (unknown length)."""
    return payload == b"\xff" * (width - 1) and _marker_masked(
        payload_width=width - 1
    )


def _marker_masked(payload_width: int) -> bool:
    return True


@dataclass(frozen=True)
class VInt:
    """A decoded variable sized integer.

    ``unknown`` is set only when the encoded payload is all ones; the raw
    width is retained so callers can reproduce the bytes exactly.
    """

    value: int
    width: int
    unknown: bool = False


def read_vint(buf: bytes, pos: int = 0, *, is_id: bool = False) -> VInt:
    """Decode one VINT starting at ``pos``.

    Element IDs keep their marker bit (``is_id=True``); data sizes do not.
    """
    if pos >= len(buf):
        raise EBMLError("unexpected end of data reading VINT")
    first = buf[pos]
    width = length_of_vint(first)
    if pos + width > len(buf):
        raise EBMLError("VINT extends past end of data")
    raw = int.from_bytes(buf[pos:pos + width], "big")
    if is_id:
        # The whole byte sequence, marker included, is the element id.
        value = raw
        return VInt(value=value, width=width)
    value = raw & ((1 << (7 * width)) - 1)
    unknown = value == (1 << (7 * width)) - 1
    return VInt(value=value, width=width, unknown=unknown)


def encode_uint_width(value: int, width: int) -> bytes:
    """Encode ``value`` as an unsigned big endian integer of fixed width."""
    if value < 0 or value >= 1 << (8 * width):
        raise EBMLError(f"value {value} does not fit in {width} bytes")
    return value.to_bytes(width, "big")


def encode_uint(value: int) -> bytes:
    """Minimal big endian unsigned encoding (zero -> single zero byte)."""
    if value < 0:
        raise EBMLError("uint must be non-negative")
    width = max(1, (value.bit_length() + 7) // 8)
    return value.to_bytes(width, "big")


def encode_sint(value: int) -> bytes:
    """Minimal big endian signed integer with sign extension."""
    width = 1
    while True:
        lo = -(1 << (8 * width - 1))
        hi = (1 << (8 * width - 1)) - 1
        if lo <= value <= hi:
            return value.to_bytes(width, "big", signed=True)
        width += 1
        if width > 8:
            raise EBMLError("sint too large")


def encode_vint(value: int, *, unknown: bool = False, width: int | None = None) -> bytes:
    """Encode a data-size VINT.

    With ``unknown`` the all-ones payload encoding of the requested/grown
    width is produced (the Matroska 'unknown size' marker).
    """
    if unknown:
        width = width or 4
        if not 1 <= width <= 8:
            raise EBMLError("bad unknown VINT width")
        return bytes([0x100 >> width]) + b"\xff" * (width - 1)
    if value < 0:
        raise EBMLError("size VINT must be non-negative")
    need = max(1, ((value + 1).bit_length() + 6) // 7)
    if width is None:
        width = need
    if width < need or width > 8:
        raise EBMLError(f"value {value} does not fit in width {width}")
    marker = 1 << (8 - width)
    headroom = (1 << (7 * width)) - 1
    if value > headroom:
        raise EBMLError("value too large for VINT width")
    raw = value | (marker << (8 * (width - 1)))
    return raw.to_bytes(width, "big")


def encode_id(element_id: int) -> bytes:
    """Encode an EBML element id using its natural width marker."""
    width = max(1, (element_id.bit_length() + 7) // 8)
    return element_id.to_bytes(width, "big")


def decode_uint(data: bytes) -> int:
    return int.from_bytes(data, "big", signed=False)


def decode_sint(data: bytes) -> int:
    return int.from_bytes(data, "big", signed=True)


def decode_float(data: bytes) -> float:
    if len(data) == 4:
        return unpack(">f", data)[0]
    if len(data) == 8:
        return unpack(">d", data)[0]
    raise EBMLError(f"float width {len(data)} unsupported")


def decode_string(data: bytes) -> str:
    return data.rstrip(b"\x00").decode("utf-8", errors="replace")


# --- EBML lacing (the integer coding used inside lace size tables) ---------


def decode_lace_signed(raw: int, bits: int = 32) -> int:
    """Interpret ``raw`` as an EBML-lacing signed delta of ``bits`` width.

    EBML lacing stores differences using sign-magnitude style encoding:
    odd values shift to a negative number.  The on-disk field keeps its
    bit width so callers can reason about overflow boundary cases.
    """
    if raw & 1:
        return -(raw >> 1) - 1
    return raw >> 1


def encode_lace_signed(value: int, bits: int = 32) -> int:
    """Inverse of :func:`decode_lace_signed`."""
    if value < 0:
        return ((-value - 1) << 1) | 1
    return value << 1


def apply_sign_width(raw: int, width_bytes: int) -> int:
    """Sign extend a raw unsigned tick value read from ``width_bytes``.

    Relative block timecodes in Matroska are signed integers of the exact
    on-disk width; a 0xFF..FF pattern therefore means -1 and never the
    unsigned maximum.
    """
    bits = 8 * width_bytes
    sign = 1 << (bits - 1)
    return (raw ^ sign) - sign
