"""Matroska/WebM EBML 元素 ID 表、名称、值类型与容器关系。

仅覆盖本系统需要的元素；未知元素按通用 master/value 处理，不丢字节范围。
"""
from __future__ import annotations

# ---- EBML header ----
EBML = 0x1A45DFA3
EBMLVersion = 0x4286
EBMLReadVersion = 0x42F7
EBMLMaxIDLength = 0x42F2
EBMLMaxSizeLength = 0x42F3
DocType = 0x4282
DocTypeVersion = 0x4287
DocTypeReadVersion = 0x4285

# ---- Segment & top level ----
Segment = 0x18538067
SeekHead = 0x114D9B74
Seek = 0x4DBB
SeekID = 0x53AB
SeekPosition = 0x53AC
Info = 0x1549A966
TimecodeScale = 0x2AD7B1
Duration = 0x4489
MuxingApp = 0x4D80
WritingApp = 0x5741
DateUTC = 0x4461
SegmentUID = 0x73A4
SegmentFilename = 0x7384
Title = 0x7BA9

# ---- Tracks ----
Tracks = 0x1654AE6B
TrackEntry = 0xAE
TrackNumber = 0xD7
TrackUID = 0x73C5
TrackType = 0x83
Name = 0x536E
Language = 0x22B59C
CodecID = 0x86
DefaultDuration = 0x23E383
TrackTimecodeScale = 0x23314F  # 已废弃的旧字段，历史文件可能出现
TrackOffset = 0x537F  # 已废弃，ns 之前以 scale tick 表示
FlagDefault = 0x88
FlagLacing = 0x9C
MaxBlockAdditionID = 0x55EE
Audio = 0xE1
Video = 0xE0
SamplingFrequency = 0xB5
Channels = 0x9F
PixelWidth = 0xB0
PixelHeight = 0xBA
ContentEncodings = 0x6D80

# ---- Cluster / Block ----
Cluster = 0x1F43B675
Timecode = 0xE7
SilentTracks = 0x5854
SilentTrackNumber = 0x58D7
Position = 0xA7
PrevSize = 0xAB
SimpleBlock = 0xA3
BlockGroup = 0xA0
Block = 0xA1
BlockDuration = 0x9B
ReferenceBlock = 0xFB
DiscardPadding = 0x75A2
BlockAdditions = 0x75A1
BlockAddition = 0xA6
BlockMore = 0xA6  # 同值不同上下文
BlockAddID = 0xEE
BlockAdditional = 0xA5
CodecState = 0xA4
LaceNumber = 0xCC

# ---- Cues ----
Cues = 0x1C53BB6B
CuePoint = 0xBB
CueTime = 0xB3
CueTrackPositions = 0xB7
CueTrack = 0xF7
CueClusterPosition = 0xF1
CueRelativePosition = 0xF0
CueDuration = 0xB2
CueBlockNumber = 0x5378

# ---- 其它常见顶层 ----
Attachments = 0x1941A469
Chapters = 0x1043A770
Tags = 0x1254C367
Void = 0xEC
CRC32 = 0xBF

NAMES = {v: k for k, v in dict(locals()).items() if isinstance(v, int) and k.isalnum()}
# 处理 BlockMore/A6 与 BlockAddition 同名值
NAMES[0xA6] = "BlockMore"

# 值类型
T_UINT = "uint"
T_SINT = "sint"
T_FLOAT = "float"
T_STRING = "string"
T_UTF8 = "utf8"
T_BINARY = "binary"
T_DATE = "date"
T_MASTER = "master"
T_BLOCK = "block"

_VALUE_TYPES = {
    EBMLVersion: T_UINT, EBMLReadVersion: T_UINT, EBMLMaxIDLength: T_UINT,
    EBMLMaxSizeLength: T_UINT, DocTypeVersion: T_UINT, DocTypeReadVersion: T_UINT,
    TimecodeScale: T_UINT, TrackNumber: T_UINT, TrackUID: T_UINT, TrackType: T_UINT,
    DefaultDuration: T_UINT, TrackTimecodeScale: T_FLOAT, FlagDefault: T_UINT,
    FlagLacing: T_UINT, MaxBlockAdditionID: T_UINT, Channels: T_UINT,
    PixelWidth: T_UINT, PixelHeight: T_UINT, BlockDuration: T_UINT,
    BlockAddID: T_UINT, CueTime: T_UINT, CueTrack: T_UINT,
    CueClusterPosition: T_UINT, CueRelativePosition: T_UINT, CueDuration: T_UINT,
    CueBlockNumber: T_UINT, SeekPosition: T_UINT, Timecode: T_UINT, Position: T_UINT,
    PrevSize: T_UINT, SilentTrackNumber: T_UINT, LaceNumber: T_UINT,
    ReferenceBlock: T_SINT, TrackOffset: T_SINT, DiscardPadding: T_SINT,
    SamplingFrequency: T_FLOAT, Duration: T_FLOAT, DateUTC: T_DATE,
    DocType: T_STRING, CodecID: T_STRING, MuxingApp: T_STRING, WritingApp: T_STRING,
    Name: T_UTF8, Title: T_UTF8, Language: T_STRING, SegmentFilename: T_STRING,
    SegmentUID: T_BINARY, SeekID: T_BINARY,
    SimpleBlock: T_BLOCK, Block: T_BLOCK,
    BlockAdditional: T_BINARY, CodecState: T_BINARY,
    Void: T_BINARY, CRC32: T_BINARY,
}

_MASTERS = {
    EBML, Segment, SeekHead, Seek, Info, Tracks, TrackEntry, Audio, Video,
    Cluster, BlockGroup, BlockAdditions, BlockMore, ContentEncodings,
    SilentTracks, Cues, CuePoint, CueTrackPositions, Attachments, Chapters, Tags,
}

# 每个容器允许的直接子元素（未知元素仍保留为 unknown-*，但不递归猜）
_CHILDREN = {
    EBML: {EBMLVersion, EBMLReadVersion, EBMLMaxIDLength, EBMLMaxSizeLength,
           DocType, DocTypeVersion, DocTypeReadVersion},
    Segment: {SeekHead, Info, Tracks, Cluster, Cues, Attachments, Chapters,
              Tags, Void, CRC32},
    SeekHead: {Seek, Void, CRC32},
    Seek: {SeekID, SeekPosition},
    Info: {TimecodeScale, Duration, MuxingApp, WritingApp, DateUTC, SegmentUID,
           SegmentFilename, Title, Void, CRC32},
    Tracks: {TrackEntry},
    TrackEntry: {TrackNumber, TrackUID, TrackType, Name, Language, CodecID,
                 DefaultDuration, TrackTimecodeScale, TrackOffset, FlagDefault,
                 FlagLacing, MaxBlockAdditionID, Audio, Video, ContentEncodings},
    Audio: {SamplingFrequency, Channels},
    Video: {PixelWidth, PixelHeight},
    Cluster: {Timecode, SilentTracks, Position, PrevSize, SimpleBlock,
              BlockGroup, CRC32, Void},
    SilentTracks: {SilentTrackNumber},
    BlockGroup: {Block, BlockDuration, ReferenceBlock, DiscardPadding,
                 BlockAdditions, CodecState},
    BlockAdditions: {BlockMore},
    BlockMore: {BlockAddID, BlockAdditional},
    Cues: {CuePoint, Void, CRC32},
    CuePoint: {CueTime, CueTrackPositions},
    CueTrackPositions: {CueTrack, CueClusterPosition, CueRelativePosition,
                        CueDuration, CueBlockNumber},
}

# 顶层（Segment 直接子级）
LEVEL1 = {SeekHead, Info, Tracks, Cluster, Cues, Attachments, Chapters, Tags,
          Void, CRC32}


def elem_name(eid: int) -> str:
    return NAMES.get(eid, f"Unknown_0x{eid:X}")


def value_type(eid: int) -> str:
    if eid in _VALUE_TYPES:
        return _VALUE_TYPES[eid]
    if eid in _MASTERS or is_known_master(eid):
        return T_MASTER
    return T_BINARY


def is_known_master(eid: int) -> bool:
    return eid in _MASTERS


def known_children(parent_id: int) -> set:
    return _CHILDREN.get(parent_id, set())
