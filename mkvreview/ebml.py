"""EBML element tree parsing with full provenance.

Every parsed element keeps:

* its element id,
* the absolute header offset and data offset (byte coordinates from file 0),
* declared size and ``unknown_length`` flag,
* the inclusive raw byte range ``[header_start, end)`` covering it,
* the original payload bytes (for leaves) / child elements (for masters).

Unknown-length masters are parsed greedily until the next element id that is
legal for an enclosing container, exactly like a streaming Matroska reader.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .vint import (
    VIntError,
    decode_sint,
    decode_uint,
    read_vint,
)


# --- Matroska element ids ---------------------------------------------------

EBML_HEADER = 0x1A45DFA3
EBML_VERSION = 0x4286
EBML_READ_VERSION = 0x42F7
EBML_MAX_ID_LENGTH = 0x42F2
EBML_MAX_SIZE_LENGTH = 0x42F3
DOC_TYPE = 0x4282
DOC_TYPE_VERSION = 0x4287
DOC_TYPE_READ_VERSION = 0x4285

SEGMENT = 0x18538067
SEEK_HEAD = 0x114D9B74
INFO = 0x1549A966
TIMECODE_SCALE = 0x2AD7B1
DURATION = 0x4489
MUXING_APP = 0x4D80
WRITING_APP = 0x5741
SEGMENT_UID = 0x73A4
SEGMENT_FILENAME = 0x7384
TIMESTAMP_SCALE_ALIAS = TIMECODE_SCALE  # Matroska renamed to TimestampScale

TRACKS = 0x1654AE6B
TRACK_ENTRY = 0xAE
TRACK_NUMBER = 0xD7
TRACK_UID = 0x73C5
TRACK_TYPE = 0x83
FLAG_LACING = 0x9C
DEFAULT_DURATION = 0x23E383
NAME = 0x536E
CODEC_ID = 0x86
AUDIO = 0xE1
VIDEO = 0xE0
SAMPLING_FREQUENCY = 0xB5
DEFAULT_DURATION_NS = DEFAULT_DURATION
PIXEL_WIDTH = 0xB0
PIXEL_HEIGHT = 0xBA
FRAME_RATE = 0x2383E3

CLUSTER = 0x1F43B675
CLUSTER_TIMECODE = 0xE7
SIMPLE_BLOCK = 0xA3
BLOCK_GROUP = 0xA0
BLOCK = 0xA1
DISCARD_PADDING = 0x75A2
REFERENCE_BLOCK = 0xFB
BLOCK_DURATION = 0x9B

CUES = 0x1C53BB6B
CUE_POINT = 0xBB
CUE_TIME = 0xB3
CUE_TRACK_POSITIONS = 0xB7
CUE_TRACK = 0xF7
CUE_CLUSTER_POSITION = 0xF1
CUE_RELATIVE_POSITION = 0xF0
CUE_DURATION = 0xB2

VOID = 0xEC
CRC32 = 0xBF


# Container membership used to terminate unknown-length masters.
SEGMENT_CHILDREN = {
    SEEK_HEAD,
    INFO,
    TRACKS,
    CLUSTER,
    CUES,
    VOID,
    0x1254C367,  # Tags
    0x1043A770,  # Chapters
    0x1941A469,  # Attachments
}

CLUSTER_CHILDREN = {CLUSTER_TIMECODE, SIMPLE_BLOCK, BLOCK_GROUP, VOID, CRC32}
TRACKS_CHILDREN = {TRACK_ENTRY}
TRACK_ENTRY_CHILDREN = {
    TRACK_NUMBER,
    TRACK_UID,
    TRACK_TYPE,
    FLAG_LACING,
    DEFAULT_DURATION,
    NAME,
    CODEC_ID,
    AUDIO,
    VIDEO,
}
INFO_CHILDREN = {TIMECODE_SCALE, DURATION, MUXING_APP, WRITING_APP, SEGMENT_UID, SEGMENT_FILENAME}
AUDIO_CHILDREN = {SAMPLING_FREQUENCY}
VIDEO_CHILDREN = {PIXEL_WIDTH, PIXEL_HEIGHT, FRAME_RATE}
BLOCK_GROUP_CHILDREN = {BLOCK, DISCARD_PADDING, REFERENCE_BLOCK, BLOCK_DURATION}
CUES_CHILDREN = {CUE_POINT}
CUE_POINT_CHILDREN = {CUE_TIME, CUE_TRACK_POSITIONS}
CUE_TRACK_POSITIONS_CHILDREN = {
    CUE_TRACK,
    CUE_CLUSTER_POSITION,
    CUE_RELATIVE_POSITION,
    CUE_DURATION,
}

_STOP_SETS = {
    SEGMENT: SEGMENT_CHILDREN,
    CLUSTER: CLUSTER_CHILDREN,
    TRACKS: TRACKS_CHILDREN,
    TRACK_ENTRY: TRACK_ENTRY_CHILDREN,
    INFO: INFO_CHILDREN,
    AUDIO: AUDIO_CHILDREN,
    VIDEO: VIDEO_CHILDREN,
    BLOCK_GROUP: BLOCK_GROUP_CHILDREN,
    CUES: CUES_CHILDREN,
    CUE_POINT: CUE_POINT_CHILDREN,
    CUE_TRACK_POSITIONS: CUE_TRACK_POSITIONS_CHILDREN,
    EBML_HEADER: {
        EBML_VERSION,
        EBML_READ_VERSION,
        EBML_MAX_ID_LENGTH,
        EBML_MAX_SIZE_LENGTH,
        DOC_TYPE,
        DOC_TYPE_VERSION,
        DOC_TYPE_READ_VERSION,
    },
}

MASTER_ELEMENTS = set(_STOP_SETS)


@dataclass
class Element:
    id: int
    header_start: int  # offset of id vint, byte coordinates
    data_start: int  # offset of payload
    size: int  # declared payload size (-1 when unknown)
    unknown_length: bool
    end: int  # exclusive end of the raw byte range (best effort)
    id_length: int
    size_length: int
    children: list["Element"] = field(default_factory=list)
    data: bytes | None = None
    truncated: bool = False

    @property
    def raw_range(self) -> tuple[int, int]:
        return (self.header_start, self.end)

    def find(self, element_id: int) -> "Element | None":
        for child in self.children:
            if child.id == element_id:
                return child
        return None

    def find_all(self, element_id: int) -> list["Element"]:
        return [child for child in self.children if child.id == element_id]

    def to_dict(self) -> dict:
        return {
            "id": f"0x{self.id:X}",
            "header_start": self.header_start,
            "data_start": self.data_start,
            "size": self.size,
            "unknown_length": self.unknown_length,
            "end": self.end,
            "id_length": self.id_length,
            "size_length": self.size_length,
            "truncated": self.truncated,
            "children": [child.to_dict() for child in self.children],
            "has_data": self.data is not None,
        }


def _read_element_header(buf: bytes, pos: int) -> tuple[int, int, int, int, bool]:
    element_id, id_len, id_unknown = read_vint(buf, pos)
    if id_unknown:
        # An all-ones element id VINT is reserved / invalid.
        raise VIntError(f"reserved (all-ones) element id at {pos}")
    size, size_len, unknown = read_vint(buf, pos + id_len)
    return element_id, id_len, size, size_len, unknown


def parse_tree(buf: bytes, start: int, end: int, stop_ids: set[int] | None = None) -> list[Element]:
    """Parse sibling elements in ``buf[start:end]``.

    ``stop_ids`` terminates parsing when the next element id belongs to an
    enclosing container (used for unknown-length masters).
    """
    elements: list[Element] = []
    pos = start
    while pos < end:
        try:
            element_id, id_len, size, size_len, unknown = _read_element_header(buf, pos)
        except VIntError:
            break
        if stop_ids is not None and element_id in stop_ids:
            break
        data_start = pos + id_len + size_len
        element = Element(
            id=element_id,
            header_start=pos,
            data_start=data_start,
            size=size,
            unknown_length=unknown,
            end=end if unknown else min(end, data_start + size),
            id_length=id_len,
            size_length=size_len,
        )
        declared_end = end if unknown else data_start + size
        truncated = declared_end > end
        if truncated:
            element.truncated = True
        payload_end = min(end, declared_end)

        if element_id in MASTER_ELEMENTS:
            element.children = parse_tree(
                buf,
                data_start,
                payload_end,
                _STOP_SETS.get(element_id),
            )
            if unknown:
                if element.children:
                    element.end = element.children[-1].end
                else:
                    element.end = data_start
        else:
            element.data = buf[data_start:payload_end]
            if unknown:
                # Leaf elements must never use unknown length.
                element.truncated = True
        elements.append(element)
        pos = element.end if unknown else data_start + size
        if pos <= element.header_start:
            break
    return elements


def parse_file(buf: bytes) -> dict:
    """Parse a Matroska/WebM file into top-level elements.

    Returns ``{"elements": [...], "diagnostics": [...]}``; never raises on
    malformed trailing data.
    """
    diagnostics: list[dict] = []
    try:
        elements = parse_tree(buf, 0, len(buf), stop_ids=None)
    except VIntError as exc:
        return {"elements": [], "diagnostics": [{"code": "fatal", "message": str(exc)}]}
    if not elements:
        diagnostics.append({"code": "empty", "message": "no EBML elements decoded"})
    return {"elements": elements, "diagnostics": diagnostics}


def uint_value(element: Element | None, default: int | None = None) -> int | None:
    if element is None or element.data is None or not element.data:
        return default
    return decode_uint(element.data)


def float_value(element: Element | None, default: float | None = None) -> float | None:
    if element is None or element.data is None or not element.data:
        return default
    if len(element.data) == 4:
        import struct

        return struct.unpack(">f", element.data)[0]
    if len(element.data) == 8:
        import struct

        return struct.unpack(">d", element.data)[0]
    return default


def string_value(element: Element | None) -> str | None:
    if element is None or element.data is None:
        return None
    return element.data.rstrip(b"\x00").decode("utf-8", errors="replace")


def sint_value(element: Element | None) -> int | None:
    if element is None or element.data is None or not element.data:
        return None
    return decode_sint(element.data)

