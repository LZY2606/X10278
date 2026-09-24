"""Matroska / EBML element catalogue used by the parser.

Only the subset the timecode court needs is modelled, but every id carries
its type so that raw and normalised views can always be kept apart.
"""

from __future__ import annotations

# EBML header
EBML_HEADER = 0x1A45DFA3
EBML_VERSION = 0x4286
EBML_READ_VERSION = 0x42F7
EBML_MAX_ID_LENGTH = 0x42F2
EBML_MAX_SIZE_LENGTH = 0x42F3
DOC_TYPE = 0x4282
DOC_TYPE_VERSION = 0x4287
DOC_TYPE_READ_VERSION = 0x4285

# Segment level
SEGMENT = 0x18538067
SEEK_HEAD = 0x114D9B74
INFO = 0x1549A966
TIMESTAMP_SCALE = 0x2AD7B1
DURATION = 0x4489
MUXING_APP = 0x4D80
WRITING_APP = 0x5741
SEGMENT_UID = 0x73A4
TRACKS = 0x1654AE6B
TRACK_ENTRY = 0xAE
TRACK_NUMBER = 0xD7
TRACK_UID = 0x73C5
TRACK_TYPE = 0x83
FLAG_DEFAULT = 0x88
DEFAULT_DURATION = 0x23E383
TRACK_TIMECODE_SCALE = 0x23314F  # legacy per-track scale, optional
CODEC_ID = 0x86
NAME = 0x536E
AUDIO = 0xE1
SAMPLING_FREQ = 0xB5
VIDEO = 0xE0
CLUSTER = 0x1F43B675
CLUSTER_TIMESTAMP = 0xE7
BLOCK_GROUP = 0xA0
BLOCK = 0xA1
SIMPLE_BLOCK = 0xA3
BLOCK_DURATION = 0x9B
REFERENCE_BLOCK = 0xFB
DISCARD_PADDING = 0x75A2
CUES = 0x1C53BB6B
CUE_POINT = 0xBB
CUE_TIME = 0xB3
CUE_TRACK_POSITIONS = 0xB7
CUE_TRACK = 0xF7
CUE_CLUSTER_POSITION = 0xF1
CUE_RELATIVE_POSITION = 0xF0
CUE_DURATION = 0xB2

UINT = "uint"
SINT = "sint"
FLOAT = "float"
STRING = "string"
BINARY = "binary"
MASTER = "master"

TYPES = {
    EBML_HEADER: MASTER,
    EBML_VERSION: UINT,
    EBML_READ_VERSION: UINT,
    EBML_MAX_ID_LENGTH: UINT,
    EBML_MAX_SIZE_LENGTH: UINT,
    DOC_TYPE: STRING,
    DOC_TYPE_VERSION: UINT,
    DOC_TYPE_READ_VERSION: UINT,
    SEGMENT: MASTER,
    SEEK_HEAD: MASTER,
    INFO: MASTER,
    TIMESTAMP_SCALE: UINT,
    DURATION: FLOAT,
    MUXING_APP: STRING,
    WRITING_APP: STRING,
    SEGMENT_UID: BINARY,
    TRACKS: MASTER,
    TRACK_ENTRY: MASTER,
    TRACK_NUMBER: UINT,
    TRACK_UID: UINT,
    TRACK_TYPE: UINT,
    FLAG_DEFAULT: UINT,
    DEFAULT_DURATION: UINT,
    TRACK_TIMECODE_SCALE: FLOAT,
    CODEC_ID: STRING,
    NAME: STRING,
    AUDIO: MASTER,
    SAMPLING_FREQ: FLOAT,
    VIDEO: MASTER,
    CLUSTER: MASTER,
    CLUSTER_TIMESTAMP: SINT,
    BLOCK_GROUP: MASTER,
    BLOCK: BINARY,
    SIMPLE_BLOCK: BINARY,
    BLOCK_DURATION: UINT,
    REFERENCE_BLOCK: SINT,
    DISCARD_PADDING: SINT,
    CUES: MASTER,
    CUE_POINT: MASTER,
    CUE_TIME: UINT,
    CUE_TRACK_POSITIONS: MASTER,
    CUE_TRACK: UINT,
    CUE_CLUSTER_POSITION: UINT,
    CUE_RELATIVE_POSITION: UINT,
    CUE_DURATION: UINT,
}

NAMES = {
    EBML_HEADER: "EBML",
    EBML_VERSION: "EBMLVersion",
    EBML_READ_VERSION: "EBMLReadVersion",
    EBML_MAX_ID_LENGTH: "EBMLMaxIDLength",
    EBML_MAX_SIZE_LENGTH: "EBMLMaxSizeLength",
    DOC_TYPE: "DocType",
    DOC_TYPE_VERSION: "DocTypeVersion",
    DOC_TYPE_READ_VERSION: "DocTypeReadVersion",
    SEGMENT: "Segment",
    SEEK_HEAD: "SeekHead",
    INFO: "Info",
    TIMESTAMP_SCALE: "TimecodeScale",
    DURATION: "Duration",
    MUXING_APP: "MuxingApp",
    WRITING_APP: "WritingApp",
    SEGMENT_UID: "SegmentUID",
    TRACKS: "Tracks",
    TRACK_ENTRY: "TrackEntry",
    TRACK_NUMBER: "TrackNumber",
    TRACK_UID: "TrackUID",
    TRACK_TYPE: "TrackType",
    FLAG_DEFAULT: "FlagDefault",
    DEFAULT_DURATION: "DefaultDuration",
    TRACK_TIMECODE_SCALE: "TrackTimecodeScale",
    CODEC_ID: "CodecID",
    NAME: "Name",
    AUDIO: "Audio",
    SAMPLING_FREQ: "SamplingFrequency",
    VIDEO: "Video",
    CLUSTER: "Cluster",
    CLUSTER_TIMESTAMP: "Timecode",
    BLOCK_GROUP: "BlockGroup",
    BLOCK: "Block",
    SIMPLE_BLOCK: "SimpleBlock",
    BLOCK_DURATION: "BlockDuration",
    REFERENCE_BLOCK: "ReferenceBlock",
    DISCARD_PADDING: "DiscardPadding",
    CUES: "Cues",
    CUE_POINT: "CuePoint",
    CUE_TIME: "CueTime",
    CUE_TRACK_POSITIONS: "CueTrackPositions",
    CUE_TRACK: "CueTrack",
    CUE_CLUSTER_POSITION: "CueClusterPosition",
    CUE_RELATIVE_POSITION: "CueRelativePosition",
    CUE_DURATION: "CueDuration",
}


def elem_type(element_id: int) -> str:
    return TYPES.get(element_id, BINARY)


def elem_name(element_id: int) -> str:
    return NAMES.get(element_id, f"Unknown_{element_id:#010x}")


# Membership rules used to terminate unknown-size master elements.
_CHILDREN = {
    EBML_HEADER: {EBML_VERSION, EBML_READ_VERSION, EBML_MAX_ID_LENGTH,
                  EBML_MAX_SIZE_LENGTH, DOC_TYPE, DOC_TYPE_VERSION,
                  DOC_TYPE_READ_VERSION},
    SEGMENT: {SEEK_HEAD, INFO, TRACKS, CLUSTER, CUES},
    INFO: {TIMESTAMP_SCALE, DURATION, MUXING_APP, WRITING_APP, SEGMENT_UID},
    TRACKS: {TRACK_ENTRY},
    TRACK_ENTRY: {TRACK_NUMBER, TRACK_UID, TRACK_TYPE, FLAG_DEFAULT,
                  DEFAULT_DURATION, TRACK_TIMECODE_SCALE, CODEC_ID, NAME,
                  AUDIO, VIDEO},
    AUDIO: {SAMPLING_FREQ},
    VIDEO: set(),
    CLUSTER: {CLUSTER_TIMESTAMP, BLOCK_GROUP, SIMPLE_BLOCK},
    BLOCK_GROUP: {BLOCK, BLOCK_DURATION, REFERENCE_BLOCK, DISCARD_PADDING},
    CUES: {CUE_POINT},
    CUE_POINT: {CUE_TIME, CUE_TRACK_POSITIONS},
    CUE_TRACK_POSITIONS: {CUE_TRACK, CUE_CLUSTER_POSITION,
                          CUE_RELATIVE_POSITION, CUE_DURATION},
    SEEK_HEAD: set(),
}


def allowed_children(parent_id: int) -> set[int] | None:
    """Known child ids for a parent, or None when not a known master."""
    return _CHILDREN.get(parent_id)
