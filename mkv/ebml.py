"""Low-level EBML primitives shared by the builder and the parser.

Everything here keeps the raw byte representation available: a parsed
element always knows its absolute offset, header size and payload range so
that the review UI can point back at the original bytes.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Matroska element table: id bytes -> (name, type)
# types: master, uint, int, float, string, binary, date
ELEMENTS = {
    "1A45DFA3": ("EBML", "master"),
    "4286": ("EBMLVersion", "uint"),
    "42F7": ("EBMLReadVersion", "uint"),
    "42F2": ("EBMLMaxIDLength", "uint"),
    "42F3": ("EBMLMaxSizeLength", "uint"),
    "4282": ("DocType", "string"),
    "4287": ("DocTypeVersion", "uint"),
    "4285": ("DocTypeReadVersion", "uint"),
    "18538067": ("Segment", "master"),
    "1549A966": ("Info", "master"),
    "2AD7B1": ("TimecodeScale", "uint"),
    "1654AE6B": ("Tracks", "master"),
    "AE": ("TrackEntry", "master"),
    "D7": ("TrackNumber", "uint"),
    "73C5": ("TrackUID", "uint"),
    "83": ("TrackType", "uint"),
    "86": ("CodecID", "string"),
    "23E383": ("DefaultDuration", "uint"),
    "23314F": ("TrackTimecodeScale", "float"),
    "1F43B675": ("Cluster", "master"),
    "E7": ("Timecode", "uint"),
    "A0": ("BlockGroup", "master"),
    "A1": ("Block", "binary"),
    "A3": ("SimpleBlock", "binary"),
    "75A2": ("DiscardPadding", "int"),
    "FB": ("ReferenceBlock", "int"),
    "1C53BB6B": ("Cues", "master"),
    "BB": ("CuePoint", "master"),
    "B3": ("CueTime", "uint"),
    "B7": ("CueTrackPositions", "master"),
    "F7": ("CueTrack", "uint"),
    "F1": ("CueClusterPosition", "uint"),
    "5378": ("CueBlockNumber", "uint"),
    "EC": ("Void", "binary"),
}

NAME_TO_ID = {name: eid for eid, (name, _t) in ELEMENTS.items()}

MASTER_IDS = {eid for eid, (_n, t) in ELEMENTS.items() if t == "master"}

# IDs that may appear as direct children of Segment; used to terminate an
# unknown-size Cluster and to re-sync after a corrupted gap. Void (0xEC) is
# deliberately excluded: single-byte ids occur in random junk too often to
# be trusted as re-sync points.
TOP_LEVEL_IDS = ("1549A966", "1654AE6B", "1F43B675", "1C53BB6B")

DEFAULT_TIMECODE_SCALE_NS = 1_000_000  # Matroska default: 1 ms


class EBMLFormatError(Exception):
    """Raised when bytes cannot be read as EBML at all."""


def read_vint(data: bytes, pos: int, keep_marker: bool = False):
    """Read a variable-length integer at ``pos``.

    Returns (value, length). With keep_marker=False the length marker bit is
    stripped (size semantics); with keep_marker=True it is kept (id semantics).
    A size with all data bits set is the "unknown size" sentinel -> None.
    """
    if pos >= len(data):
        raise EBMLFormatError(f"vint read past end at {pos}")
    first = data[pos]
    if first == 0:
        raise EBMLFormatError(f"invalid vint (leading zero byte) at {pos}")
    mask = 0x80
    length = 1
    while not (first & mask):
        mask >>= 1
        length += 1
    if pos + length > len(data):
        raise EBMLFormatError(f"truncated vint at {pos}")
    raw = int.from_bytes(data[pos:pos + length], "big")
    if keep_marker:
        return raw, length
    value = raw & ((1 << (7 * length)) - 1)
    if value == (1 << (7 * length)) - 1:
        return None, length  # unknown size
    return value, length


def encode_vint(value: int, length: int | None = None) -> bytes:
    """Encode a size as an EBML vint (marker bit set)."""
    if length is None:
        for cand in range(1, 9):
            if value < (1 << (7 * cand)) - 1:
                length = cand
                break
        else:
            raise ValueError("value too large for 8-byte vint")
    if value >= (1 << (7 * length)) - 1:
        raise ValueError(f"value {value} does not fit in {length}-byte vint")
    return ((1 << (7 * length)) | value).to_bytes(length, "big")


def unknown_size_bytes(length: int = 1) -> bytes:
    """All-ones size field of the given length = unknown size.

    The encoded field keeps the length marker bit set, so an L-byte unknown
    size is 2^(7L+1)-1 (e.g. 0xFF for L=1, 0x01FF..FF for L=8).
    """
    return ((1 << (7 * length + 1)) - 1).to_bytes(length, "big")


def encode_id(id_hex: str) -> bytes:
    return bytes.fromhex(id_hex)


@dataclass
class Element:
    id_hex: str
    name: str
    type: str
    offset: int            # absolute offset of the element id
    header_size: int       # id + size field length
    size: int | None       # None = unknown size
    data_start: int
    data_end: int          # exclusive; for unknown-size masters: resolved end
    children: list = field(default_factory=list)

    @property
    def unknown_size(self) -> bool:
        return self.size is None

    @property
    def end(self) -> int:
        return self.data_end

    def byte_range(self):
        return [self.offset, self.data_end]
