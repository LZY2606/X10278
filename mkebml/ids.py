"""Matroska / EBML element identifiers used by the tribunal.

IDs are stored in their canonical marked (on-disk) form, e.g. ``0x1A45DFA3``.
Only the slice of the schema the case pack exercises is enumerated; unknown
ids are preserved by the parser as *unknown elements*, never discarded.
"""
from __future__ import annotations

# --- EBML header -----------------------------------------------------------
EBML = 0x1A45DFA3
EBML_VERSION = 0x4286
EBML_READ_VERSION = 0x42F7
EBML_MAX_ID_LENGTH = 0x42F2
EBML_MAX_SIZE_LENGTH = 0x42F3
DOC_TYPE = 0x4282
DOC_TYPE_VERSION = 0x4287
DOC_TYPE_READ_VERSION = 0x4285

# --- Segment ---------------------------------------------------------------
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
FLAG_LACING = 0x9C
DEFAULT_DURATION = 0x23E383
TRACK_TIMECODE_SCALE = 0x23314F
TRACK_OFFSET = 0x537F
NAME = 0x536E
CODEC_ID = 0x86
AUDIO = 0xE1
SAMPLING_FREQUENCY = 0xB5

CLUSTER = 0x1F43B675
TIMESTAMP = 0xE7
SIMPLE_BLOCK = 0xA3
BLOCK_GROUP = 0xA0
BLOCK = 0xA1
BLOCK_ADDITIONS = 0x75A1
BLOCK_MORE = 0xA6
BLOCK_ADD_ID = 0xEE
BLOCK_ADDITIONAL = 0xA5
BLOCK_DURATION = 0x9B
REFERENCE_BLOCK = 0xFB

CUES = 0x1C53BB6B
CUE_POINT = 0xBB
CUE_TIME = 0xB3
CUE_TRACK_POSITIONS = 0xB7
CUE_TRACK = 0xF7
CUE_CLUSTER_POSITION = 0xF1
CUE_RELATIVE_POSITION = 0xF0

NAMES: dict[int, str] = {}


def _name(element_id: int, text: str) -> int:
    NAMES[element_id] = text
    return element_id


for _id, _text in [
    (EBML, "EBML"), (EBML_VERSION, "EBMLVersion"),
    (EBML_READ_VERSION, "EBMLReadVersion"),
    (EBML_MAX_ID_LENGTH, "EBMLMaxIDLength"),
    (EBML_MAX_SIZE_LENGTH, "EBMLMaxSizeLength"),
    (DOC_TYPE, "DocType"), (DOC_TYPE_VERSION, "DocTypeVersion"),
    (DOC_TYPE_READ_VERSION, "DocTypeReadVersion"),
    (SEGMENT, "Segment"), (SEEK_HEAD, "SeekHead"), (INFO, "Info"),
    (TIMESTAMP_SCALE, "TimecodeScale/TimestampScale"), (DURATION, "Duration"),
    (MUXING_APP, "MuxingApp"), (WRITING_APP, "WritingApp"),
    (SEGMENT_UID, "SegmentUID"),
    (TRACKS, "Tracks"), (TRACK_ENTRY, "TrackEntry"),
    (TRACK_NUMBER, "TrackNumber"), (TRACK_UID, "TrackUID"),
    (TRACK_TYPE, "TrackType"), (FLAG_LACING, "FlagLacing"),
    (DEFAULT_DURATION, "DefaultDuration"),
    (TRACK_TIMECODE_SCALE, "TrackTimecodeScale"),
    (TRACK_OFFSET, "TrackOffset"), (NAME, "Name"), (CODEC_ID, "CodecID"),
    (AUDIO, "Audio"), (SAMPLING_FREQUENCY, "SamplingFrequency"),
    (CLUSTER, "Cluster"), (TIMESTAMP, "Timestamp/ClusterTimecode"),
    (SIMPLE_BLOCK, "SimpleBlock"), (BLOCK_GROUP, "BlockGroup"),
    (BLOCK, "Block"), (BLOCK_ADDITIONS, "BlockAdditions"),
    (BLOCK_MORE, "BlockMore"), (BLOCK_ADD_ID, "BlockAddID"),
    (BLOCK_ADDITIONAL, "BlockAdditional"), (BLOCK_DURATION, "BlockDuration"),
    (REFERENCE_BLOCK, "ReferenceBlock"),
    (CUES, "Cues"), (CUE_POINT, "CuePoint"), (CUE_TIME, "CueTime"),
    (CUE_TRACK_POSITIONS, "CueTrackPositions"), (CUE_TRACK, "CueTrack"),
    (CUE_CLUSTER_POSITION, "CueClusterPosition"),
    (CUE_RELATIVE_POSITION, "CueRelativePosition"),
]:
    _name(_id, _text)


def name_of(element_id: int) -> str:
    return NAMES.get(element_id, f"Unknown(0x{element_id:X})")


# Container elements: their payload is a sequence of child EBML elements.
MASTER = {
    EBML, SEGMENT, SEEK_HEAD, INFO, TRACKS, TRACK_ENTRY, AUDIO, CLUSTER,
    BLOCK_GROUP, BLOCK_ADDITIONS, BLOCK_MORE, CUES, CUE_POINT,
    CUE_TRACK_POSITIONS,
}

# Segment-level elements whose unknown length may extend to end of file.
SEGMENT_LEVEL = {SEEK_HEAD, INFO, TRACKS, CLUSTER, CUES}

# Matroska element ids may be 1..4 bytes.
def is_matroska_id(element_id: int) -> bool:
    return 1 <= element_id.bit_length() <= 32
