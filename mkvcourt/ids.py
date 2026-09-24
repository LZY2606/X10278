"""Matroska/WebM element catalogue used by the parser.

Only the subset needed for the timecode evidence domain is modelled. Values
are the canonical Matroska element IDs (VINT form including the length
marker), e.g. Segment = 0x18538067.
"""
from __future__ import annotations

# Element ID -> (name, kind). kind in:
#   master, uint, int, float, string, binary, unknown
ELEMENTS: dict[int, tuple[str, str]] = {
    # EBML header
    0x1A45DFA3: ("EBML", "master"),
    0x4286: ("EBMLVersion", "uint"),
    0x42F7: ("EBMLReadVersion", "uint"),
    0x42F2: ("EBMLMaxIDLength", "uint"),
    0x42F3: ("EBMLMaxSizeLength", "uint"),
    0x4282: ("DocType", "string"),
    0x4287: ("DocTypeVersion", "uint"),
    0x4285: ("DocTypeReadVersion", "uint"),
    # top level / structure
    0x18538067: ("Segment", "master"),
    0x114D9B74: ("SeekHead", "master"),
    0x4DBB: ("Seek", "master"),
    0x53AB: ("SeekID", "binary"),
    0x53AC: ("SeekPosition", "uint"),
    0x1549A966: ("Info", "master"),
    0x2AD7B1: ("TimestampScale", "uint"),
    0x4489: ("Duration", "float"),
    0x4D80: ("MuxingApp", "string"),
    0x5741: ("WritingApp", "string"),
    0x1654AE6B: ("Tracks", "master"),
    0xAE: ("TrackEntry", "master"),
    0xD7: ("TrackNumber", "uint"),
    0x73C5: ("TrackUID", "uint"),
    0x83: ("TrackType", "uint"),
    0x9C: ("FlagLacing", "uint"),
    0x23E383: ("DefaultDuration", "uint"),
    0x23314F: ("TrackTimestampScale", "float"),
    0x536E: ("Name", "string"),
    0x86: ("CodecID", "string"),
    # clusters / blocks
    0x1F43B675: ("Cluster", "master"),
    0xE7: ("Timestamp", "uint"),
    0xE1: ("BlockGroup", "master"),
    0xA3: ("SimpleBlock", "binary"),
    0xA0: ("Block", "binary"),
    0x9B: ("BlockDuration", "uint"),
    0x75A2: ("DiscardPadding", "int"),
    0x75A1: ("ReferenceBlock", "int"),
    # cues
    0x1C53BB6B: ("Cues", "master"),
    0xBB: ("CuePoint", "master"),
    0xB3: ("CueTime", "uint"),
    0xB7: ("CueTrackPositions", "master"),
    0xF7: ("CueTrack", "uint"),
    0xF1: ("CueClusterPosition", "uint"),
    0xF0: ("CueRelativePosition", "uint"),
    0x5378: ("CueDuration", "uint"),
    # misc
    0xEC: ("Void", "binary"),
    0xBF: ("CRC-32", "binary"),
}

NAME_TO_ID = {name: eid for eid, (name, _kind) in ELEMENTS.items()}


def lookup(eid: int) -> tuple[str, str]:
    return ELEMENTS.get(eid, (f"Unknown_{eid:02X}", "unknown"))
