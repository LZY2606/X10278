"""EBML variable-length integer (VINT) encode/decode, incl. unknown length.

An EBML VINT uses a leading marker bit.  A length descriptor where every
payload bit is 1 (after the marker) means "unknown length" for master
elements and is preserved, never guessed away.
"""

from __future__ import annotations


class VIntError(ValueError):
    pass


def marker_for_len(length: int) -> int:
    if not 1 <= length <= 8:
        raise VIntError(f"VINT length out of range: {length}")
    return 1 << (8 - length)


def is_unknown_length(first_byte: int, length: int) -> bool:
    """True if the descriptor payload is all-ones (the reserved unknown form)."""
    if length == 8:
        # The 8-byte form has no payload bits left under the marker; the
        # canonical unknown-length descriptor is 0xFF.
        return first_byte == 0xFF
    return first_byte == (marker_for_len(length) | (marker_for_len(length) - 1))


def read_vint(buf: bytes, pos: int) -> tuple[int, int, bool]:
    """Return ``(value, descriptor_length, unknown)`` at ``pos``.

    For unknown length, ``value`` is ``-1``.
    """
    if pos >= len(buf):
        raise VIntError(f"VINT out of bounds at {pos}")
    first = buf[pos]
    if first == 0:
        raise VIntError(f"VINT length prefix 0x00 at {pos} is not encodable")
    length = 8 - first.bit_length() + 1
    if pos + length > len(buf):
        raise VIntError(f"VINT header truncated at {pos}")
    unknown = is_unknown_length(first, length)
    if unknown:
        return -1, length, True
    value = first ^ marker_for_len(length)
    for i in range(1, length):
        value = (value << 8) | buf[pos + i]
    return value, length, False


def read_uint(buf: bytes, pos: int) -> tuple[int, int, bool]:
    """Read an EBML element/data size VINT (alias kept for readability)."""
    return read_vint(buf, pos)


def len_vint(value: int) -> int:
    """Minimum descriptor length needed to encode *value* (1..8)."""
    if value < 0:
        raise VIntError("cannot VINT-encode a negative value")
    for length in range(1, 9):
        # Payload is 7*L bits; all-ones is reserved for "unknown length".
        if value < (1 << (7 * length)) - 1:
            return length
    raise VIntError(f"value {value} does not fit in 8-byte VINT")


def write_vint(value: int, length: int | None = None) -> bytes:
    if value < 0:
        raise VIntError("cannot VINT-encode a negative value")
    if length is None:
        length = len_vint(value)
    marker = marker_for_len(length)
    total = value | (marker << (8 * (length - 1)))
    return total.to_bytes(length, "big")


def write_unknown_length(length: int) -> bytes:
    """The reserved all-ones data-size descriptor of the given byte length."""
    if not 1 <= length <= 8:
        raise VIntError(f"unknown-length descriptor size out of range: {length}")
    return b"\xff" if length == 8 else bytes([marker_for_len(length) | (marker_for_len(length) - 1)]) + b"\xff" * (length - 1)


def encode_uint_minimal(value: int) -> bytes:
    """Minimal big-endian representation of an unsigned EBML data payload."""
    if value == 0:
        return b""
    return value.to_bytes((value.bit_length() + 7) // 8, "big")


def encode_sint(value: int, bit_width: int) -> bytes:
    """Signed big-endian encoding into exactly ``bit_width`` bytes."""
    if bit_width <= 0:
        raise VIntError("sint width must be positive")
    return value.to_bytes(bit_width, "big", signed=True)


def decode_sint(raw: bytes) -> int:
    """Interpret *raw* as a signed big-endian integer (fixed bit width)."""
    if not raw:
        raise VIntError("empty signed integer")
    return int.from_bytes(raw, "big", signed=True)


def decode_uint(raw: bytes) -> int:
    return int.from_bytes(raw, "big", signed=False)
