EBML_HEADER = bytes.fromhex("1a45dfa3")
EBML_VERSION = bytes.fromhex("4286")
EBML_READ_VERSION = bytes.fromhex("42f7")
EBML_MAX_ID_LENGTH = bytes.fromhex("42f2")
EBML_MAX_SIZE_LENGTH = bytes.fromhex("42f3")
DOC_TYPE = bytes.fromhex("4282")
DOC_TYPE_VERSION = bytes.fromhex("4287")
DOC_TYPE_READ_VERSION = bytes.fromhex("4285")

SEGMENT = bytes.fromhex("18538067")
INFO = bytes.fromhex("1549a966")
TIMECODE_SCALE = bytes.fromhex("2ad7b1")
DURATION = bytes.fromhex("4489")

TRACKS = bytes.fromhex("1654ae6b")
TRACK_ENTRY = bytes.fromhex("ae")
TRACK_NUMBER = bytes.fromhex("d7")
TRACK_UID = bytes.fromhex("73c5")
TRACK_TYPE = bytes.fromhex("83")
DEFAULT_DURATION = bytes.fromhex("23e383")
TRACK_TIMESTAMP_SCALE = bytes.fromhex("23314f")
NAME = bytes.fromhex("536e")
CODEC_ID = bytes.fromhex("86")

CUES = bytes.fromhex("1c53bb6b")
CUE_POINT = bytes.fromhex("bb")
CUE_TIME = bytes.fromhex("b3")
CUE_TRACK_POSITIONS = bytes.fromhex("b7")
CUE_TRACK = bytes.fromhex("f7")
CUE_CLUSTER_POSITION = bytes.fromhex("f1")
CUE_RELATIVE_POSITION = bytes.fromhex("f0")

CLUSTER = bytes.fromhex("1f43b675")
TIMECODE = bytes.fromhex("e7")
SIMPLE_BLOCK = bytes.fromhex("a3")
BLOCK_GROUP = bytes.fromhex("a0")
BLOCK = bytes.fromhex("a1")
DISCARD_PADDING = bytes.fromhex("75a2")

VOID = bytes.fromhex("ec")


HUMAN_NAMES = {
    EBML_HEADER: "EBML Header",
    SEGMENT: "Segment",
    INFO: "Info",
    TIMECODE_SCALE: "TimecodeScale",
    DURATION: "Duration",
    TRACKS: "Tracks",
    TRACK_ENTRY: "TrackEntry",
    TRACK_NUMBER: "TrackNumber",
    TRACK_UID: "TrackUID",
    TRACK_TYPE: "TrackType",
    DEFAULT_DURATION: "DefaultDuration",
    TRACK_TIMESTAMP_SCALE: "TrackTimestampScale",
    NAME: "Name",
    CODEC_ID: "CodecID",
    CUES: "Cues",
    CUE_POINT: "CuePoint",
    CUE_TIME: "CueTime",
    CUE_TRACK_POSITIONS: "CueTrackPositions",
    CUE_TRACK: "CueTrack",
    CUE_CLUSTER_POSITION: "CueClusterPosition",
    CUE_RELATIVE_POSITION: "CueRelativePosition",
    CLUSTER: "Cluster",
    TIMECODE: "Timecode",
    SIMPLE_BLOCK: "SimpleBlock",
    BLOCK_GROUP: "BlockGroup",
    BLOCK: "Block",
    DISCARD_PADDING: "DiscardPadding",
    VOID: "Void",
}
