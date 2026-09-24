"""Raw EBML/Matroska parsing layer.

Everything in this module keeps the *original* representation of the file:
element offsets, raw sizes (including unknown lengths), signed relative
timecodes and raw byte ranges.  No normalisation happens here; the derived
layer (mkv.derive) is responsible for interpretation.
"""
from __future__ import annotations

from dataclasses import dataclass, field

CLUSTER_ID = 0x1F43B675
SEGMENT_ID = 0x18538067

ELEMENT_NAMES = {
    0x1A45DFA3: "EBML",
    0x4286: "EBMLVersion",
    0x42F7: "EBMLReadVersion",
    0x42F2: "EBMLMaxIDLength",
    0x42F3: "EBMLMaxSizeLength",
    0x4282: "DocType",
    0x18538067: "Segment",
    0x1549A966: "Info",
    0x2AD7B1: "TimecodeScale",
    0x4D80: "MuxingApp",
    0x5741: "WritingApp",
    0x1654AE6B: "Tracks",
    0xAE: "TrackEntry",
    0xD7: "TrackNumber",
    0x73C5: "TrackUID",
    0x83: "TrackType",
    0x86: "CodecID",
    0x536E: "Name",
    0x23E383: "DefaultDuration",
    0x23314F: "TrackTimecodeScale",
    0x1C53BB6B: "Cues",
    0xBB: "CuePoint",
    0xB3: "CueTime",
    0xB7: "CueTrackPositions",
    0xF7: "CueTrack",
    0xF1: "CueClusterPosition",
    0x1F43B675: "Cluster",
    0xE7: "Timecode",
    0xA0: "BlockGroup",
    0xA1: "Block",
    0x9B: "BlockDuration",
    0x75A2: "DiscardPadding",
    0xA3: "SimpleBlock",
}

MASTER_IDS = {
    0x1A45DFA3, 0x18538067, 0x1549A966, 0x1654AE6B, 0xAE,
    0x1C53BB6B, 0xBB, 0xB7, 0x1F43B675, 0xA0,
}

LACING_NAMES = {0: "none", 1: "xiph", 2: "fixed", 3: "ebml"}


class ParseError(Exception):
    pass


def read_vint(data: bytes, pos: int):
    """Read an EBML variable-size integer with the marker bit stripped."""
    if pos >= len(data):
        raise ParseError("vint starts past end of data")
    first = data[pos]
    mask = 0x80
    length = 1
    while length <= 8 and not first & mask:
        mask >>= 1
        length += 1
    if length > 8:
        raise ParseError("invalid vint marker byte")
    value = first & (mask - 1)
    for i in range(1, length):
        if pos + i >= len(data):
            raise ParseError("truncated vint")
        value = (value << 8) | data[pos + i]
    return value, length


def read_signed_vint(data: bytes, pos: int):
    """EBML signed vint (used by EBML lacing size diffs)."""
    value, length = read_vint(data, pos)
    bias = (1 << (7 * length - 1)) - 1
    return value - bias, length


def read_id(data: bytes, pos: int):
    """Read an EBML element ID (marker bits kept)."""
    if pos >= len(data):
        raise ParseError("id starts past end of data")
    first = data[pos]
    mask = 0x80
    length = 1
    while length <= 4 and not first & mask:
        mask >>= 1
        length += 1
    if length > 4:
        raise ParseError("invalid element id")
    value = first
    for i in range(1, length):
        if pos + i >= len(data):
            raise ParseError("truncated element id")
        value = (value << 8) | data[pos + i]
    return value, length


def is_unknown_size(value: int, length: int) -> bool:
    return value == (1 << (7 * length)) - 1


@dataclass
class Element:
    eid: int
    name: str
    offset: int            # absolute offset of the element ID
    header_size: int
    size: int | None       # None == unknown length
    data_offset: int
    depth: int
    resolved_end: int = 0  # best-known end (unknown sizes resolved by scan)
    unknown_size: bool = False
    children: list = field(default_factory=list)
    diagnostics: list = field(default_factory=list)

    @property
    def end(self):
        return self.resolved_end


def parse_elements(data: bytes, start: int = 0, end: int | None = None,
                   depth: int = 0) -> list[Element]:
    if end is None:
        end = len(data)
    elements = []
    pos = start
    while pos < end:
        try:
            eid, id_len = read_id(data, pos)
            size_val, size_len = read_vint(data, pos + id_len)
        except ParseError:
            break
        unknown = is_unknown_size(size_val, size_len)
        size = None if unknown else size_val
        data_offset = pos + id_len + size_len
        el = Element(
            eid=eid,
            name=ELEMENT_NAMES.get(eid, "Unknown(0x%x)" % eid),
            offset=pos,
            header_size=id_len + size_len,
            size=size,
            data_offset=data_offset,
            depth=depth,
            unknown_size=unknown,
        )
        if unknown:
            el.diagnostics.append("unknown_length")
            if eid == CLUSTER_ID:
                nxt = data.find(b"\x1f\x43\xb6\x75", data_offset, end)
                if nxt != -1:
                    el.resolved_end = nxt
                    el.diagnostics.append(
                        "unknown_length_resolved_by_next_cluster_scan")
                else:
                    el.resolved_end = end
            else:
                el.resolved_end = end
        else:
            el.resolved_end = data_offset + size
        if el.resolved_end > len(data):
            el.diagnostics.append("truncated_beyond_file")
            el.resolved_end = len(data)
        if eid in MASTER_IDS and el.resolved_end >= data_offset:
            el.children = parse_elements(data, data_offset, el.resolved_end,
                                         depth + 1)
        elements.append(el)
        if el.resolved_end <= pos:
            break
        pos = el.resolved_end
    return elements


@dataclass
class BlockInfo:
    track_number: int
    relative_timecode: int   # signed 16-bit, interpreted as signed
    flags: int
    lacing: str
    num_frames: int | None
    frame_sizes: list[int] | None   # None when lacing is corrupt
    lacing_error: str | None
    header_size: int


def _xiph_sizes(body: bytes) -> list[int]:
    if not body:
        raise ParseError("empty xiph lace")
    n = body[0] + 1
    pos = 1
    sizes = []
    for _ in range(n - 1):
        s = 0
        while True:
            if pos >= len(body):
                raise ParseError("xiph lace size runs past payload")
            b = body[pos]
            pos += 1
            s += b
            if b != 255:
                break
        sizes.append(s)
    last = len(body) - pos - sum(sizes)
    if last < 0:
        raise ParseError("xiph lace sizes exceed payload")
    sizes.append(last)
    return sizes


def _fixed_sizes(body: bytes) -> list[int]:
    if not body:
        raise ParseError("empty fixed lace")
    n = body[0] + 1
    rest = len(body) - 1
    if n <= 0 or rest % n != 0:
        raise ParseError("fixed lace payload not divisible by frame count")
    return [rest // n] * n


def _ebml_sizes(body: bytes) -> list[int]:
    if not body:
        raise ParseError("empty ebml lace")
    n = body[0] + 1
    first, l1 = read_vint(body, 1)
    pos = 1 + l1
    sizes = [first]
    cur = first
    for _ in range(n - 2):
        diff, dl = read_signed_vint(body, pos)
        pos += dl
        cur += diff
        if cur < 0:
            raise ParseError("ebml lace produced negative frame size")
        sizes.append(cur)
    last = len(body) - pos - sum(sizes)
    if last < 0:
        raise ParseError("ebml lace sizes exceed payload")
    sizes.append(last)
    return sizes


def parse_block(payload: bytes) -> BlockInfo:
    """Parse a Block/SimpleBlock payload.

    The relative timecode is always interpreted as a *signed* 16-bit
    big-endian integer.  When lacing is corrupt we do not guess frame
    boundaries: frame_sizes is None and lacing_error carries the reason.
    """
    track, tlen = read_vint(payload, 0)
    if tlen + 3 > len(payload):
        raise ParseError("block header truncated")
    rel = int.from_bytes(payload[tlen:tlen + 2], "big", signed=True)
    flags = payload[tlen + 2]
    lacing = LACING_NAMES[(flags >> 1) & 0x3]
    body = payload[tlen + 3:]
    header = tlen + 3
    num_frames = None
    frame_sizes = None
    lacing_error = None
    try:
        if lacing == "none":
            frame_sizes = [len(body)]
            num_frames = 1
        elif lacing == "xiph":
            frame_sizes = _xiph_sizes(body)
            num_frames = len(frame_sizes)
        elif lacing == "fixed":
            frame_sizes = _fixed_sizes(body)
            num_frames = len(frame_sizes)
        else:
            frame_sizes = _ebml_sizes(body)
            num_frames = len(frame_sizes)
    except ParseError as exc:
        lacing_error = str(exc)
        if body:
            num_frames = body[0] + 1
    return BlockInfo(
        track_number=track,
        relative_timecode=rel,
        flags=flags,
        lacing=lacing,
        num_frames=num_frames,
        frame_sizes=frame_sizes,
        lacing_error=lacing_error,
        header_size=header,
    )
