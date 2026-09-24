"""Matroska/WebM EBML 元素身份表（按原始字节匹配，保留官方语义）。"""
from __future__ import annotations

from collections.abc import Iterable

# id 字节 -> (名称, 类型)  类型: master/uint/float/string/utf8/binary/date
_ELEMENTS: dict[bytes, tuple[str, str]] = {
    b"\x1a\x45\xdf\xa3": ("EBML", "master"),
    b"\x42\x86": ("EBMLVersion", "uint"),
    b"\x42\xf7": ("EBMLReadVersion", "uint"),
    b"\x42\xf2": ("EBMLMaxIDLength", "uint"),
    b"\x42\xf3": ("EBMLMaxSizeLength", "uint"),
    b"\x42\x82": ("DocType", "string"),
    b"\x42\x87": ("DocTypeVersion", "uint"),
    b"\x42\x85": ("DocTypeReadVersion", "uint"),
    b"\x18\x53\x80\x67": ("Segment", "master"),
    b"\x11\x4d\x9b\x74": ("SeekHead", "master"),
    b"\x4d\xbb": ("Seek", "master"),
    b"\x53\xab": ("SeekID", "binary"),
    b"\x53\xac": ("SeekPosition", "uint"),
    b"\x15\x49\xa9\x66": ("Info", "master"),
    b"\x2a\xd7\xb1": ("TimecodeScale", "uint"),
    b"\x44\x89": ("Duration", "float"),
    b"\x4d\x80": ("MuxingApp", "utf8"),
    b"\x57\x41": ("WritingApp", "utf8"),
    b"\x16\x54\xae\x6b": ("Tracks", "master"),
    b"\xae": ("TrackEntry", "master"),
    b"\xd7": ("TrackNumber", "uint"),
    b"\x73\xc5": ("TrackUID", "uint"),
    b"\x83": ("TrackType", "uint"),
    b"\x9c": ("FlagLacing", "uint"),
    b"\x23\xe3\x83": ("DefaultDuration", "uint"),
    b"\x23\x31\x4f": ("TrackTimecodeScale", "float"),
    b"\x53\x6e": ("Name", "utf8"),
    b"\x86": ("CodecID", "string"),
    b"\x1f\x43\xb6\x75": ("Clusters", "master"),  # 非官方，防误识别；保留未知名
    b"\x1c\x53\xbb\x6b": ("Cues", "master"),
    b"\xbb": ("CuePoint", "master"),
    b"\xb3": ("CueTime", "uint"),
    b"\xb7": ("CueTrackPositions", "master"),
    b"\xf7": ("CueTrack", "uint"),
    b"\xf1": ("CueClusterPosition", "uint"),
    b"\xf0": ("CueRelativePosition", "uint"),
    b"\x1f\x43\xb6\x74": ("Cluster", "master"),
    b"\xe7": ("Timecode", "uint"),
    b"\xa0": ("BlockGroup", "master"),
    b"\xa1": ("Block", "binary"),
    b"\xa3": ("SimpleBlock", "binary"),
    b"\x9b": ("BlockDuration", "uint"),
    b"\x9e": ("ReferenceBlock", "sint"),
    b"\x75\xa2": ("DiscardPadding", "sint"),
    b"\xec": ("Void", "binary"),
    b"\xbf": ("CRC-32", "binary"),
}

CONTAINERS: set[bytes] = {
    eid for eid, (_name, typ) in _ELEMENTS.items() if typ == "master"
}


def name_of(eid: bytes) -> str:
    info = _ELEMENTS.get(eid)
    if info:
        return info[0]
    return "Unknown(" + eid.hex().upper() + ")"


def type_of(eid: bytes) -> str:
    info = _ELEMENTS.get(eid)
    return info[1] if info else "binary"


def known_ids() -> Iterable[bytes]:
    return tuple(_ELEMENTS)
