"""Matroska (EBML) element-id registry used by the forensic parser.

Only the ids the tool reasons about are named; unknown ids are kept verbatim
by the parser so nothing is silently dropped.
"""
from __future__ import annotations

from . import ebml

# Header / segment structure
EBML = 0x1A45DFA3
EBML_VERSION = 0x4286
EBML_READ_VERSION = 0x42F7
EBML_MAX_ID_LENGTH = 0x42F2
EBML_MAX_SIZE_LENGTH = 0x42F3
DOC_TYPE = 0x4282
DOC_TYPE_VERSION = 0x4287
DOC_TYPE_READ_VERSION = 0x4285
SEGMENT = 0x18538067
SEEK_HEAD = 0x114D9B74
SEEK = 0x4DBB
SEEK_ID = 0x53AB
SEEK_POSITION = 0x53AC
VOID = 0xEC
CRC32 = 0xBF

# Segment info
INFO = 0x1549A966
TIMECODE_SCALE = 0x2AD7B1
DURATION = 0x4489
MUXING_APP = 0x4D80
WRITING_APP = 0x5741
DATE_UTC = 0x4461

# Tracks
TRACKS = 0x1654AE6B
TRACK_ENTRY = 0xAE
TRACK_NUMBER = 0xD7
TRACK_UID = 0x73C5
TRACK_TYPE = 0x83
FLAG_LACING = 0x9C
DEFAULT_DURATION = 0x23E383
NAME = 0x536E
CODEC_ID = 0x86
LANGUAGE = 0x22B59C
AUDIO = 0xE1
SAMPLING_FREQUENCY = 0xB5
CHANNELS = 0x9F
VIDEO = 0xE0
PIXEL_WIDTH = 0xB0
PIXEL_HEIGHT = 0xBA

# Cluster / block
CLUSTER = 0x1F43B675
CLUSTER_TIMECODE = 0xE7
SIMPLE_BLOCK = 0xA3
BLOCK_GROUP = 0xA0
BLOCK = 0xA1
BLOCK_DURATION = 0x9B
REFERENCE_BLOCK = 0xFB
DISCARD_PADDING = 0x75A2

# Cues
CUES = 0x1C53BB6B
CUE_POINT = 0xBB
CUE_TIME = 0xB3
CUE_TRACK_POSITIONS = 0xB7
CUE_TRACK = 0xF7
CUE_CLUSTER_POSITION = 0xF1
CUE_RELATIVE_POSITION = 0xF0
CUE_DURATION = 0xB2

NAMES: dict[int, str] = {
    EBML: "EBML", EBML_VERSION: "EBMLVersion",
    EBML_READ_VERSION: "EBMLReadVersion",
    EBML_MAX_ID_LENGTH: "EBMLMaxIDLength",
    EBML_MAX_SIZE_LENGTH: "EBMLMaxSizeLength", DOC_TYPE: "DocType",
    DOC_TYPE_VERSION: "DocTypeVersion",
    DOC_TYPE_READ_VERSION: "DocTypeReadVersion",
    SEGMENT: "Segment", SEEK_HEAD: "SeekHead", SEEK: "Seek",
    SEEK_ID: "SeekID", SEEK_POSITION: "SeekPosition", VOID: "Void",
    CRC32: "CRC-32", INFO: "Info", TIMECODE_SCALE: "TimecodeScale",
    DURATION: "Duration", MUXING_APP: "MuxingApp", WRITING_APP: "WritingApp",
    DATE_UTC: "DateUTC", TRACKS: "Tracks", TRACK_ENTRY: "TrackEntry",
    TRACK_NUMBER: "TrackNumber", TRACK_UID: "TrackUID", TRACK_TYPE: "TrackType",
    FLAG_LACING: "FlagLacing", DEFAULT_DURATION: "DefaultDuration",
    NAME: "Name", CODEC_ID: "CodecID", LANGUAGE: "Language", AUDIO: "Audio",
    SAMPLING_FREQUENCY: "SamplingFrequency", CHANNELS: "Channels",
    VIDEO: "Video", PIXEL_WIDTH: "PixelWidth", PIXEL_HEIGHT: "PixelHeight",
    CLUSTER: "Cluster", CLUSTER_TIMECODE: "Timecode",
    SIMPLE_BLOCK: "SimpleBlock", BLOCK_GROUP: "BlockGroup", BLOCK: "Block",
    BLOCK_DURATION: "BlockDuration", REFERENCE_BLOCK: "ReferenceBlock",
    DISCARD_PADDING: "DiscardPadding", CUES: "Cues", CUE_POINT: "CuePoint",
    CUE_TIME: "CueTime", CUE_TRACK_POSITIONS: "CueTrackPositions",
    CUE_TRACK: "CueTrack", CUE_CLUSTER_POSITION: "CueClusterPosition",
    CUE_RELATIVE_POSITION: "CueRelativePosition",
    CUE_DURATION: "CueDuration",
}

CONTAINERS = {
    EBML, SEGMENT, SEEK_HEAD, SEEK, INFO, TRACKS, TRACK_ENTRY, AUDIO, VIDEO,
    CLUSTER, BLOCK_GROUP, CUES, CUE_POINT, CUE_TRACK_POSITIONS,
}

# Level-1 elements that can legally terminate an unknown-size Cluster.
LEVEL1 = {
    SEEK_HEAD, INFO, CLUSTER, TRACKS, CUES, 0x1941A469, 0x1043A770,
    0x1254C367, SEGMENT,
}

# Lacing flag bits inside a Block.
LACING_NONE = 0
LACING_XIPH = 1
LACING_EBML = 3
LACING_FIXED = 2


def configure() -> None:
    ebml.register_names(NAMES)
    ebml.register_containers(CONTAINERS)
    ebml.register_boundaries({CLUSTER: LEVEL1})
