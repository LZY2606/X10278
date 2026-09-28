"""Matroska/EBML element identifier registry and VINT primitives.

Element IDs and type semantics follow the Matroska container specification.
Only the elements needed by the timecode-review workflow are registered;
unknown ids are still preserved as opaque elements by the parser.
"""
from __future__ import annotations

from dataclasses import dataclass

# --- well known EBML / Matroska element ids ---
ID_EBML = 0x1A45DFA3
ID_EBML_VERSION = 0x4286
ID_EBML_READ_VERSION = 0x42F7
ID_EBML_MAX_ID_LENGTH = 0x42F2
ID_EBML_MAX_SIZE_LENGTH = 0x42F3
ID_DOC_TYPE = 0x4282
ID_DOC_TYPE_VERSION = 0x4287
ID_DOC_TYPE_READ_VERSION = 0x4288

ID_SEGMENT = 0x18538067
ID_SEEK_HEAD = 0x114D9B74
ID_SEEK = 0x4DBB
ID_SEEK_ID = 0x53AB
ID_SEEK_POSITION = 0x53AC
ID_VOID = 0xEC

ID_INFO = 0x1549A966
ID_TIMECODE_SCALE = 0x2AD7B1
ID_DURATION = 0x4489
ID_MUXING_APP = 0x4D80
ID_WRITING_APP = 0x5741

ID_TRACKS = 0x1654AE6B
ID_TRACK_ENTRY = 0xAE
ID_TRACK_NUMBER = 0xD7
ID_TRACK_UID = 0x73C5
ID_TRACK_TYPE = 0x83
ID_FLAG_LACING = 0x9C
ID_DEFAULT_DURATION = 0x23E383
ID_TRACK_TIMECODE_SCALE = 0x23314F
ID_CODEC_ID = 0x86
ID_VIDEO = 0xE0
ID_AUDIO = 0xE1
ID_SAMPLING_FREQUENCY = 0xB5
ID_PIXEL_WIDTH = 0xB0
ID_PIXEL_HEIGHT = 0xBA

ID_CLUSTER = 0x1F43B675
ID_CLUSTER_TIMECODE = 0xE7
ID_SIMPLE_BLOCK = 0xA3
ID_BLOCK_GROUP = 0xA0
ID_BLOCK = 0xA1
ID_DISCARD_PADDING = 0x75A2
ID_REFERENCE_BLOCK = 0xFB
ID_BLOCK_DURATION = 0x9B

ID_CUES = 0x1C53BB6B
ID_CUE_POINT = 0xBB
ID_CUE_TIME = 0xB3
ID_CUE_TRACK_POSITIONS = 0xB7
ID_CUE_TRACK = 0xF7
ID_CUE_CLUSTER_POSITION = 0xF1
ID_CUE_RELATIVE_POSITION = 0xF0
ID_CUE_DURATION = 0xB2

# Matroska element data types
T_MASTER = "master"
T_UINT = "uint"
T_FLOAT = "float"
T_STR = "string"
T_BINARY = "binary"
T_SINT = "sint"
T_BLOCK = "block"


@dataclass(frozen=True)
class ElemSpec:
    eid: int
    name: str
    etype: str
    children: frozenset[int] = frozenset()
    ordered: bool = False


def _m(eid: int, name: str, *children: "ElemSpec", ordered: bool = False) -> ElemSpec:
    return ElemSpec(eid, name, T_MASTER, frozenset(c.eid for c in children), ordered)


def _s(eid: int, name: str, etype: str) -> ElemSpec:
    return ElemSpec(eid, name, etype)


SEEK = _m(ID_SEEK, "Seek",
          _s(ID_SEEK_ID, "SeekID", T_BINARY),
          _s(ID_SEEK_POSITION, "SeekPosition", T_UINT), ordered=True)
SEEK_HEAD = _m(ID_SEEK_HEAD, "SeekHead", SEEK)

INFO = _m(ID_INFO, "Info",
          _s(ID_TIMECODE_SCALE, "TimecodeScale", T_UINT),
          _s(ID_DURATION, "Duration", T_FLOAT),
          _s(ID_MUXING_APP, "MuxingApp", T_STR),
          _s(ID_WRITING_APP, "WritingApp", T_STR))

VIDEO = _m(ID_VIDEO, "Video",
           _s(ID_PIXEL_WIDTH, "PixelWidth", T_UINT),
           _s(ID_PIXEL_HEIGHT, "PixelHeight", T_UINT))
AUDIO = _m(ID_AUDIO, "Audio", _s(ID_SAMPLING_FREQUENCY, "SamplingFrequency", T_FLOAT))
TRACK_ENTRY = _m(ID_TRACK_ENTRY, "TrackEntry",
                 _s(ID_TRACK_NUMBER, "TrackNumber", T_UINT),
                 _s(ID_TRACK_UID, "TrackUID", T_UINT),
                 _s(ID_TRACK_TYPE, "TrackType", T_UINT),
                 _s(ID_FLAG_LACING, "FlagLacing", T_UINT),
                 _s(ID_DEFAULT_DURATION, "DefaultDuration", T_UINT),
                 _s(ID_TRACK_TIMECODE_SCALE, "TrackTimecodeScale", T_FLOAT),
                 _s(ID_CODEC_ID, "CodecID", T_STR),
                 VIDEO, AUDIO)
TRACKS = _m(ID_TRACKS, "Tracks", TRACK_ENTRY)

BLOCK_GROUP = _m(ID_BLOCK_GROUP, "BlockGroup",
                 _s(ID_BLOCK, "Block", T_BLOCK),
                 _s(ID_BLOCK_DURATION, "BlockDuration", T_UINT),
                 _s(ID_REFERENCE_BLOCK, "ReferenceBlock", T_SINT),
                 _s(ID_DISCARD_PADDING, "DiscardPadding", T_SINT))
CLUSTER = _m(ID_CLUSTER, "Cluster",
             _s(ID_CLUSTER_TIMECODE, "Timecode", T_UINT),
             _s(ID_SIMPLE_BLOCK, "SimpleBlock", T_BLOCK),
             BLOCK_GROUP)

CUE_TRACK_POSITIONS = _m(ID_CUE_TRACK_POSITIONS, "CueTrackPositions",
                         _s(ID_CUE_TRACK, "CueTrack", T_UINT),
                         _s(ID_CUE_CLUSTER_POSITION, "CueClusterPosition", T_UINT),
                         _s(ID_CUE_RELATIVE_POSITION, "CueRelativePosition", T_UINT),
                         _s(ID_CUE_DURATION, "CueDuration", T_UINT))
CUE_POINT = _m(ID_CUE_POINT, "CuePoint",
               _s(ID_CUE_TIME, "CueTime", T_UINT), CUE_TRACK_POSITIONS)
CUES = _m(ID_CUES, "Cues", CUE_POINT)

SEGMENT = _m(ID_SEGMENT, "Segment", SEEK_HEAD, INFO, TRACKS, CLUSTER, CUES)

EBML_HEADER = _m(ID_EBML, "EBML",
                 _s(ID_EBML_VERSION, "EBMLVersion", T_UINT),
                 _s(ID_EBML_READ_VERSION, "EBMLReadVersion", T_UINT),
                 _s(ID_EBML_MAX_ID_LENGTH, "EBMLMaxIDLength", T_UINT),
                 _s(ID_EBML_MAX_SIZE_LENGTH, "EBMLMaxSizeLength", T_UINT),
                 _s(ID_DOC_TYPE, "DocType", T_STR),
                 _s(ID_DOC_TYPE_VERSION, "DocTypeVersion", T_UINT),
                 _s(ID_DOC_TYPE_READ_VERSION, "DocTypeReadVersion", T_UINT))

REGISTRY: dict[int, ElemSpec] = {}


def _register(spec: ElemSpec) -> None:
    REGISTRY[spec.eid] = spec
    for child in spec.children:
        pass


def _register_deep(spec: ElemSpec) -> None:
    REGISTRY[spec.eid] = spec
    # children specs are reachable through module globals below


for _spec in (
    EBML_HEADER,
    _s(ID_EBML_VERSION, "EBMLVersion", T_UINT),
    _s(ID_EBML_READ_VERSION, "EBMLReadVersion", T_UINT),
    _s(ID_EBML_MAX_ID_LENGTH, "EBMLMaxIDLength", T_UINT),
    _s(ID_EBML_MAX_SIZE_LENGTH, "EBMLMaxSizeLength", T_UINT),
    _s(ID_DOC_TYPE, "DocType", T_STR),
    _s(ID_DOC_TYPE_VERSION, "DocTypeVersion", T_UINT),
    _s(ID_DOC_TYPE_READ_VERSION, "DocTypeReadVersion", T_UINT),
    SEGMENT, SEEK_HEAD, SEEK,
    _s(ID_SEEK_ID, "SeekID", T_BINARY),
    _s(ID_SEEK_POSITION, "SeekPosition", T_UINT),
    _s(ID_VOID, "Void", T_BINARY),
    INFO,
    _s(ID_TIMECODE_SCALE, "TimecodeScale", T_UINT),
    _s(ID_DURATION, "Duration", T_FLOAT),
    _s(ID_MUXING_APP, "MuxingApp", T_STR),
    _s(ID_WRITING_APP, "WritingApp", T_STR),
    TRACKS, TRACK_ENTRY,
    _s(ID_TRACK_NUMBER, "TrackNumber", T_UINT),
    _s(ID_TRACK_UID, "TrackUID", T_UINT),
    _s(ID_TRACK_TYPE, "TrackType", T_UINT),
    _s(ID_FLAG_LACING, "FlagLacing", T_UINT),
    _s(ID_DEFAULT_DURATION, "DefaultDuration", T_UINT),
    _s(ID_TRACK_TIMECODE_SCALE, "TrackTimecodeScale", T_FLOAT),
    _s(ID_CODEC_ID, "CodecID", T_STR),
    VIDEO, AUDIO,
    _s(ID_SAMPLING_FREQUENCY, "SamplingFrequency", T_FLOAT),
    _s(ID_PIXEL_WIDTH, "PixelWidth", T_UINT),
    _s(ID_PIXEL_HEIGHT, "PixelHeight", T_UINT),
    CLUSTER,
    _s(ID_CLUSTER_TIMECODE, "Timecode", T_UINT),
    _s(ID_SIMPLE_BLOCK, "SimpleBlock", T_BLOCK),
    BLOCK_GROUP,
    _s(ID_BLOCK, "Block", T_BLOCK),
    _s(ID_BLOCK_DURATION, "BlockDuration", T_UINT),
    _s(ID_REFERENCE_BLOCK, "ReferenceBlock", T_SINT),
    _s(ID_DISCARD_PADDING, "DiscardPadding", T_SINT),
    CUES, CUE_POINT, CUE_TRACK_POSITIONS,
    _s(ID_CUE_TIME, "CueTime", T_UINT),
    _s(ID_CUE_TRACK, "CueTrack", T_UINT),
    _s(ID_CUE_CLUSTER_POSITION, "CueClusterPosition", T_UINT),
    _s(ID_CUE_RELATIVE_POSITION, "CueRelativePosition", T_UINT),
    _s(ID_CUE_DURATION, "CueDuration", T_UINT),
):
    REGISTRY[_spec.eid] = _spec


def spec(eid: int) -> ElemSpec | None:
    return REGISTRY.get(eid)


def name_of(eid: int) -> str:
    known = REGISTRY.get(eid)
    return known.name if known else f"Unknown_{eid:X}"


# Top-level element ids that may legally appear directly inside a Segment.
SEGMENT_TOP_CHILDREN = frozenset(
    s.eid for s in (SEEK_HEAD, INFO, TRACKS, CLUSTER, CUES,
                    REGISTRY[ID_VOID],
                    _m(0x1043A770, "Chapters"),  # tolerate; not parsed in depth
                    _m(0x1254C367, "Tags"),
                    _m(0x1549A966, "InfoDup"),
                    _m(0x1654AE6B, "TracksDup"),
                    _m(0x1F43B675, "ClusterDup"),
                    _m(0x1C53BB6B, "CuesDup"))
) | {0x1043A770, 0x1254C367}


# --- VINT (variable length integer) primitives ---
VINT_UNKNOWN = -1  # marker for an all-ones size field ("unknown length")


def vint_width(first_byte: int) -> int:
    """Number of bytes of a VINT given its first byte."""
    if first_byte == 0:
        raise ValueError("VINT length marker cannot be zero")
    width = 1
    marker = 0x80
    while first_byte & marker == 0:
        marker >>= 1
        width += 1
        if width > 8:
            raise ValueError("VINT longer than 8 bytes")
    return width


def read_vint(buf: bytes, pos: int) -> tuple[int, int, bool]:
    """Return (value, next_pos, unknown). value is -1 for unknown length."""
    if pos >= len(buf):
        raise ValueError("truncated VINT")
    width = vint_width(buf[pos])
    if pos + width > len(buf):
        raise ValueError("truncated VINT payload")
    first = buf[pos]
    marker = 0x80 >> (width - 1)
    raw = first & (marker - 1)
    all_ones_mask = marker - 1
    for i in range(1, width):
        raw = (raw << 8) | buf[pos + i]
    if raw == (1 << (7 * width)) - 1:
        return VINT_UNKNOWN, pos + width, True
    return raw, pos + width, False


def encode_vint(value: int, width: int | None = None) -> bytes:
    """Encode an unsigned integer as a VINT. width forces a fixed encoding."""
    if value < 0:
        raise ValueError("cannot encode negative VINT")
    if width is None:
        width = 1
        while value >= (1 << (7 * width)) - 1 and width < 8:
            width += 1
    if value >= (1 << (7 * width)) - 1:
        raise ValueError(f"value {value} does not fit in {width}-byte VINT")
    marker = 0x80 >> (width - 1)
    out = bytearray(width)
    rest = value
    for i in range(width - 1, 0, -1):
        out[i] = rest & 0xFF
        rest >>= 8
    out[0] = marker | (rest & (marker - 1))
    return bytes(out)


def encode_unknown_vint(width: int = 4) -> bytes:
    return bytes([0x1FF >> 8 & 0xFF]) if False else (
        bytes([0x80 >> (width - 1) | ((1 << (7 - (width - 1))) - (1 << (7 - (width - 1)))])
    ) if width == 1 else bytes([0x80 >> (width - 1)]) + b"\xFF" * (width - 1)


def encode_element_id(eid: int) -> bytes:
    """An element id is itself a VINT with its length marker retained."""
    raw = eid
    for width in range(1, 5):
        marker = 0x80 >> (width - 1)
        if raw < (1 << (8 * width)):
            # ensure the leading marker bit is set as the id implies
            first = (raw >> (8 * (width - 1))) & 0xFF
            if first & marker:
                out = raw.to_bytes(width, "big")
                if read_vint(out, 0)[0] == eid:
                    return out
    raise ValueError(f"element id {eid:X} is not a valid 1-4 byte VINT")
