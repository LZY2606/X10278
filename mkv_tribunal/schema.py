"""Matroska / WebM EBML 元素注册表。

只收录本案例需要的元素；未登记 ID 不会被猜测含义。
type: uint / sint / float / string / utf8 / binary / date / master
children 给出主元素允许的直属子元素，未知长度主元素靠它判断边界
（同时也靠父元素边界终止，规则对齐 RFC 9559 §8）。
"""

from __future__ import annotations

from dataclasses import dataclass

VOID = 0xEC
CRC32 = 0xBF
GLOBAL = frozenset({VOID, CRC32})


@dataclass(frozen=True)
class ElementSpec:
    name: str
    type: str
    children: frozenset[int] = frozenset()


# --- Cluster / BlockGroup 内部 ---
_CLUSTER_CHILDREN = frozenset({0xE7, 0xA3, 0xA0}) | GLOBAL
_BLOCKGROUP_CHILDREN = frozenset({0xA1, 0x9B, 0xFB, 0x75A2}) | GLOBAL

# --- 轨道 ---
_TRACKENTRY_CHILDREN = frozenset(
    {0xD7, 0x73C5, 0x83, 0x9C, 0x23E383, 0x23314F, 0x537F, 0x536E, 0x86}
) | GLOBAL
_TRACKS_CHILDREN = frozenset({0xAE}) | GLOBAL

# --- Cue ---
_CTP_CHILDREN = frozenset({0xF7, 0xF1, 0xF0, 0xB2}) | GLOBAL
_CUEPOINT_CHILDREN = frozenset({0xB3, 0xB7}) | GLOBAL
_CUES_CHILDREN = frozenset({0xBB}) | GLOBAL

# --- Info / SeekHead ---
_INFO_CHILDREN = frozenset({0x2AD7B1, 0x4489, 0x4D80, 0x5741, 0x4461}) | GLOBAL
_SEEK_CHILDREN = frozenset({0x53AB, 0x53AC}) | GLOBAL
_SEEKHEAD_CHILDREN = frozenset({0x4DBB}) | GLOBAL

# --- EBML 头 ---
_EBML_CHILDREN = frozenset(
    {0x4286, 0x42F7, 0x42F2, 0x42F3, 0x4282, 0x4287, 0x4285}
) | GLOBAL

_SEGMENT_CHILDREN = frozenset(
    {0x114D9B74, 0x1549A966, 0x1654AE6B, 0x1C53BB6B, 0x1F43B675}
) | GLOBAL


SPECS: dict[int, ElementSpec] = {
    # EBML 头
    0x1A45DFA3: ElementSpec("EBML", "master", _EBML_CHILDREN),
    0x4286: ElementSpec("EBMLVersion", "uint"),
    0x42F7: ElementSpec("EBMLReadVersion", "uint"),
    0x42F2: ElementSpec("EBMLMaxIDLength", "uint"),
    0x42F3: ElementSpec("EBMLMaxSizeLength", "uint"),
    0x4282: ElementSpec("DocType", "string"),
    0x4287: ElementSpec("DocTypeVersion", "uint"),
    0x4285: ElementSpec("DocTypeReadVersion", "uint"),
    # Segment 与顶层
    0x18538067: ElementSpec("Segment", "master", _SEGMENT_CHILDREN),
    0x114D9B74: ElementSpec("SeekHead", "master", _SEEKHEAD_CHILDREN),
    0x4DBB: ElementSpec("Seek", "master", _SEEK_CHILDREN),
    0x53AB: ElementSpec("SeekID", "binary"),
    0x53AC: ElementSpec("SeekPosition", "uint"),
    0x1549A966: ElementSpec("Info", "master", _INFO_CHILDREN),
    0x2AD7B1: ElementSpec("TimecodeScale", "uint"),
    0x4489: ElementSpec("Duration", "float"),
    0x4461: ElementSpec("DateUTC", "date"),
    0x4D80: ElementSpec("MuxingApp", "utf8"),
    0x5741: ElementSpec("WritingApp", "utf8"),
    # Tracks
    0x1654AE6B: ElementSpec("Tracks", "master", _TRACKS_CHILDREN),
    0xAE: ElementSpec("TrackEntry", "master", _TRACKENTRY_CHILDREN),
    0xD7: ElementSpec("TrackNumber", "uint"),
    0x73C5: ElementSpec("TrackUID", "uint"),
    0x83: ElementSpec("TrackType", "uint"),
    0x9C: ElementSpec("FlagLacing", "uint"),
    0x23E383: ElementSpec("DefaultDuration", "uint"),
    0x23314F: ElementSpec("TrackTimecodeScale", "float"),
    0x537F: ElementSpec("TrackOffset", "sint"),
    0x536E: ElementSpec("Name", "utf8"),
    0x86: ElementSpec("CodecID", "string"),
    # Cluster
    0x1F43B675: ElementSpec("Cluster", "master", _CLUSTER_CHILDREN),
    0xE7: ElementSpec("Timecode", "uint"),
    0xA3: ElementSpec("SimpleBlock", "binary"),
    0xA0: ElementSpec("BlockGroup", "master", _BLOCKGROUP_CHILDREN),
    0xA1: ElementSpec("Block", "binary"),
    0x9B: ElementSpec("BlockDuration", "uint"),
    0xFB: ElementSpec("ReferenceBlock", "sint"),
    0x75A2: ElementSpec("DiscardPadding", "sint"),
    # Cues
    0x1C53BB6B: ElementSpec("Cues", "master", _CUES_CHILDREN),
    0xBB: ElementSpec("CuePoint", "master", _CUEPOINT_CHILDREN),
    0xB3: ElementSpec("CueTime", "uint"),
    0xB7: ElementSpec("CueTrackPositions", "master", _CTP_CHILDREN),
    0xF7: ElementSpec("CueTrack", "uint"),
    0xF1: ElementSpec("CueClusterPosition", "uint"),
    0xF0: ElementSpec("CueRelativePosition", "uint"),
    0xB2: ElementSpec("CueDuration", "uint"),
    # 全局
    0xBF: ElementSpec("CRC-32", "binary"),
    0xEC: ElementSpec("Void", "binary"),
}


def spec_for(element_id: int) -> ElementSpec | None:
    return SPECS.get(element_id)


def id_hex(element_id: int) -> str:
    return "0x%X" % element_id
