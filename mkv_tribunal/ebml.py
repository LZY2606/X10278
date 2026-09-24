"""Minimal Matroska/EBML reader/writer.

The reader keeps *raw* facts only: element ids, byte offsets, header sizes,
declared/unknown sizes and raw payload ranges.  No time semantics live here so
that the rule layer (see :mod:`mkv_tribunal.rules`) can be versioned and
re-adjudicated independently.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from typing import Iterator


class EbmlError(Exception):
    """A byte-level problem (truncated VINT, malformed header, ...)."""


# ---------------------------------------------------------------------------
# VINT / integer primitives
# ---------------------------------------------------------------------------
def read_vint(buf: bytes, pos: int) -> tuple[int, int, bool]:
    """Return ``(value, length, unknown)`` for an EBML VINT at ``pos``.

    ``unknown`` is True for the all-data-bits marker (e.g. ``0xff``), which in
    a size field means "unknown length".
    """
    if pos >= len(buf):
        raise EbmlError(f"vint out of range at {pos}")
    first = buf[pos]
    if first == 0:
        raise EbmlError(f"zero VINT marker at {pos}")
    width = 0
    marker = 0x80
    while marker:
        if first & marker:
            break
        marker >>= 1
        width += 1
    length = width + 1
    if pos + length > len(buf):
        raise EbmlError(f"truncated {length}-byte vint at {pos}")
    mask = marker - 1
    value = first & mask
    unknown = first & mask == mask
    for i in range(1, length):
        b = buf[pos + i]
        if b != 0xFF:
            unknown = False
        value = (value << 8) | b
    return value, length, unknown


def encode_vint(value: int, width: int | None = None, unknown: bool = False) -> bytes:
    """Encode ``value`` as a size VINT, optionally forcing ``width`` (1..8)."""
    if unknown:
        width = width or 1
        return bytes([0x100 >> width]) + b"\xff" * (width - 1)
    if value < 0:
        raise ValueError("vint value must be non-negative")
    chosen = width
    if chosen is None:
        for w in range(1, 9):
            if value < (1 << (7 * w)) - 1:
                chosen = w
                break
        if chosen is None:
            raise ValueError("vint too large")
    marker = 0x100 >> chosen
    total = value | (marker << (8 * (chosen - 1)))
    out = total.to_bytes(chosen, "big")
    if out[0] & marker == 0:
        raise ValueError(f"value {value} does not fit in {chosen} bytes")
    return out


def encode_id(eid: int) -> bytes:
    width = 4 if eid.bit_length() > 21 else 3 if eid.bit_length() > 14 else 2 if eid.bit_length() > 7 else 1
    return eid.to_bytes(width, "big")


def read_id(buf: bytes, pos: int) -> tuple[int, int]:
    # Element IDs never have an "unknown" meaning; read as VINT but accept the
    # all-ones payload too (it is still a valid id).
    value, length, _ = read_vint(buf, pos)
    return value, length


def uint_bytes(value: int, width: int | None = None) -> bytes:
    if value == 0:
        return b"\x00"
    width = width or (value.bit_length() + 7) // 8
    return value.to_bytes(width, "big")


def read_uint(buf: bytes) -> int:
    return int.from_bytes(buf, "big")


def read_int_signed(buf: bytes) -> int:
    """Interpret ``buf`` as a signed big-endian integer (relative timecodes)."""
    if not buf:
        return 0
    n = len(buf)
    v = int.from_bytes(buf, "big", signed=False)
    if v & (1 << (8 * n - 1)):
        v -= 1 << (8 * n)
    return v


def el(eid: int, payload: bytes) -> bytes:
    return encode_id(eid) + encode_vint(len(payload)) + payload


def u(eid: int, value: int, width: int | None = None) -> bytes:
    return el(eid, uint_bytes(value, width))


def f(eid: int, value: float) -> bytes:
    return el(eid, struct_pack_f64(value))


def s(eid: int, text: str) -> bytes:
    return el(eid, text.encode("utf-8"))


def struct_pack_f64(value: float) -> bytes:
    import struct

    return struct.pack(">d", value)


# ---------------------------------------------------------------------------
# Element tree
# ---------------------------------------------------------------------------
@dataclass
class Element:
    eid: int
    offset: int          # offset of the ID byte (absolute in file)
    header_size: int     # id + size VINT
    size: int | None     # None == unknown length
    payload_start: int
    payload_end: int | None  # None if unknown
    depth: int
    ordinal: int         # sibling index among parent's children (0-based)
    parent_path: tuple[int, ...]
    raw_blob: bytes = b""  # complete bytes (for unknown length ends at parent EOF)
    identity: str = ""

    @property
    def end(self) -> int | None:
        return None if self.payload_end is None else self.payload_end

    def byte_range(self) -> tuple[int, int | None]:
        return self.offset, self.end


# Container ids we descend into (everything else is treated as binary payload).
CONTAINER_IDS = {
    0x1A45DFA3,  # EBML
    0x18538067,  # Segment
    0x1549A966,  # Info
    0x1654AE6B,  # Tracks
    0xAE,        # TrackEntry
    0xE0,        # Video
    0xE1,        # Audio
    0x1F43B675,  # Cluster
    0xA0,        # BlockGroup
    0x1C53BB6B,  # Cues
    0xBB,        # CuePoint
    0xB7,        # CueTrackPositions
}


def _stable_identity(eid: int, parent: tuple[str, ...], ordinal: int) -> str:
    h = hashlib.sha1(b"\x1f".join(x.encode() for x in parent)).digest()[:8]
    return f"e-{eid:02x}-{ordinal:03d}-{h.hex()}"


def parse_document(
    data: bytes, start: int = 0, end: int | None = None
) -> tuple[list[Element], list[str]]:
    """Parse ``data[start:end]`` into a flat ordered list of Elements.

    Unknown-sized elements consume up to the end of their parent.  Returns the
    elements and a list of diagnostic strings for recovered byte errors.
    """
    end = len(data) if end is None else end
    elements: list[Element] = []
    diagnostics: list[str] = []

    def walk(lo: int, hi: int | None, depth: int, parent_path: tuple[int, ...],
             parent_ids: tuple[str, ...]) -> None:
        pos = lo
        ordinal_by_eid: dict[int, int] = {}
        while pos < (hi if hi is not None else end):
            try:
                eid, id_len = read_id(data, pos)
                size_val, size_len, unknown = read_vint(data, pos + id_len)
            except EbmlError as exc:
                diagnostics.append(f"{pos}: {exc}")
                return
            header = id_len + size_len
            pstart = pos + header
            ordinal = ordinal_by_eid.get(eid, 0)
            ordinal_by_eid[eid] = ordinal + 1
            ident = _stable_identity(eid, parent_ids, ordinal)
            if unknown:
                size = None
                pend: int | None = None
                blob_end = hi if hi is not None else end
            else:
                size = size_val
                pend = pstart + size_val
                blob_end = pend
                if pend > (hi if hi is not None else end):
                    diagnostics.append(
                        f"{pos}: element 0x{eid:x} declares size {size_val} "
                        f"exceeding parent end"
                    )
                    pend = min(pend, hi if hi is not None else end)
                    blob_end = pend
            elem = Element(
                eid=eid, offset=pos, header_size=header, size=size,
                payload_start=pstart, payload_end=pend, depth=depth,
                ordinal=ordinal, parent_path=parent_path,
                raw_blob=data[pos:blob_end], identity=ident,
            )
            elements.append(elem)
            my_path = parent_path + (eid,)
            my_ids = parent_ids + (ident,)
            if eid in CONTAINER_IDS:
                child_hi = hi if size is None else pend
                if child_hi is not None and pstart <= child_hi:
                    walk(pstart, child_hi, depth + 1, my_path, my_ids)
            pos = pend if pend is not None else end

    walk(start, end, 0, (), ())
    return elements, diagnostics


def find(elements: list[Element], eid: int) -> list[Element]:
    return [e for e in elements if e.eid == eid]


def children(elements: list[Element], parent: Element) -> list[Element]:
    """Direct children of ``parent`` in the flat list (by offset ordering)."""
    out: list[Element] = []
    end = parent.payload_end if parent.payload_end is not None else 10**18
    for e in elements:
        if e.depth == parent.depth + 1 and e.offset >= parent.payload_start and e.offset < end:
            if e.parent_path == parent.parent_path + (parent.eid,):
                out.append(e)
    return out


def payload(data: bytes, e: Element) -> bytes:
    end = e.payload_end if e.payload_end is not None else len(data)
    return data[e.payload_start:end]
