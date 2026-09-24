"""EBML parsing/building primitives tailored to the Matroska timecode court.

The parser preserves everything the review needs:

* absolute element offsets and raw byte ranges,
* unknown (streaming) data sizes,
* leaf values in their original representation,
* diagnostics for truncation / invalid sizes without throwing away the tree.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Optional

# --- Element registry -------------------------------------------------------

def _eid(value: int) -> int:
    return value


REGISTRY = {}
REV_REGISTRY = {}


def reg(name: str, eid: int, kind: str) -> int:
    REGISTRY[name] = eid
    REV_REGISTRY[eid] = (name, kind)
    return eid


# EBML header / top level
EBML_HEADER = reg("EBML", 0x1A45DFA3, "m")
EBML_VERSION = reg("EBMLVersion", 0x4286, "u")
EBML_READ_VERSION = reg("EBMLReadVersion", 0x42F7, "u")
EBML_MAX_ID_LENGTH = reg("EBMLMaxIDLength", 0x42F2, "u")
EBML_MAX_SIZE_LENGTH = reg("EBMLMaxSizeLength", 0x42F3, "u")
DOC_TYPE = reg("DocType", 0x4282, "s")
DOC_TYPE_VERSION = reg("DocTypeVersion", 0x4287, "u")
DOC_TYPE_READ_VERSION = reg("DocTypeReadVersion", 0x4285, "u")

# Level 0 / Segment
SEGMENT = reg("Segment", 0x18538067, "m")
SEEK_HEAD = reg("SeekHead", 0x114D9B74, "m")
SEEK = reg("Seek", 0x4DBB, "m")
SEEK_ID = reg("SeekID", 0x53AB, "b")
SEEK_POSITION = reg("SeekPosition", 0x53AC, "u")
VOID = reg("Void", 0xEC, "b")
CRC32 = reg("CRC-32", 0xBF, "b")

# Info
INFO = reg("Info", 0x1549A966, "m")
TIMECODE_SCALE = reg("TimecodeScale", 0x2AD7B1, "u")
TIMESTAMP_SCALE = reg("TimestampScale", 0x2AD7B1, "u")  # alias, same ID
DURATION_E = reg("Duration", 0x4489, "f")
MUXING_APP = reg("MuxingApp", 0x4D80, "s")
WRITING_APP = reg("WritingApp", 0x5741, "s")

# Tracks
TRACKS = reg("Tracks", 0x1654AE6B, "m")
TRACK_ENTRY = reg("TrackEntry", 0xAE, "m")
TRACK_NUMBER = reg("TrackNumber", 0xD7, "u")
TRACK_UID = reg("TrackUID", 0x73C5, "u")
TRACK_TYPE = reg("TrackType", 0x83, "u")
CODEC_ID = reg("CodecID", 0x86, "s")
FLAG_DEFAULT = reg("FlagDefault", 0x88, "u")
DEFAULT_DURATION = reg("DefaultDuration", 0x23E383, "u")
TRACK_TIMECODE_SCALE = reg("TrackTimecodeScale", 0x23314F, "f")  # legacy ID
TRACK_TIMESTAMP_SCALE = reg("TrackTimestampScale", 0x23314F, "f")
AUDIO = reg("Audio", 0xE1, "m")
SAMPLING_FREQUENCY = reg("SamplingFrequency", 0xB5, "f")
OUTPUT_SAMPLING_FREQUENCY = reg("OutputSamplingFrequency", 0x78B5, "f")
CHANNELS = reg("Channels", 0x9F, "u")
BIT_DEPTH = reg("BitDepth", 0x6264, "u")

# Cluster / blocks
CLUSTER = reg("Cluster", 0x1F43B675, "m")
CLUSTER_TIMECODE = reg("Timecode", 0xE7, "u")
CLUSTER_TIMESTAMP = reg("Timestamp", 0xE7, "u")  # alias
SIMPLE_BLOCK = reg("SimpleBlock", 0xA3, "b")
BLOCK_GROUP = reg("BlockGroup", 0xA0, "m")
BLOCK = reg("Block", 0xA1, "b")
BLOCK_DURATION = reg("BlockDuration", 0x9B, "u")
DISCARD_PADDING = reg("DiscardPadding", 0x75A2, "i")
REFERENCE_PRIORITY = reg("ReferencePriority", 0xFA, "u")
REFERENCE_BLOCK = reg("ReferenceBlock", 0xFB, "i")
CODEC_STATE = reg("CodecState", 0xA4, "b")

# Cues
CUES = reg("Cues", 0x1C53BB6B, "m")
CUE_POINT = reg("CuePoint", 0xBB, "m")
CUE_TIME = reg("CueTime", 0xF3, "u")
CUE_TRACK_POSITIONS = reg("CueTrackPositions", 0xB7, "m")
CUE_TRACK = reg("CueTrack", 0xF7, "u")
CUE_CLUSTER_POSITION = reg("CueClusterPosition", 0xF1, "u")
CUE_RELATIVE_POSITION = reg("CueRelativePosition", 0xF0, "u")
CUE_DURATION = reg("CueDuration", 0xB2, "u")
CUE_BLOCK_NUMBER = reg("CueBlockNumber", 0x5378, "u")

# Attachments/Tags/Chapters (streaming-capable level 1)
ATTACHMENTS = reg("Attachments", 0x1941A469, "m")
TAGS = reg("Tags", 0x1254C367, "m")
CHAPTERS = reg("Chapters", 0x1043A770, "m")

# Level-1 masters that may legally have unknown size and terminate a
# streaming (unknown-size) child scope.
LEVEL1_MASTERS = {
    SEEK_HEAD, INFO, TRACKS, CUES, ATTACHMENTS, TAGS, CHAPTERS, CLUSTER,
}
STREAM_CAPABLE_MASTERS = {SEGMENT} | LEVEL1_MASTERS

DEFAULT_TIMECODE_SCALE_NS = 1_000_000


class EbmlError(ValueError):
    pass


@dataclass
class VInt:
    value: Optional[int]
    length: int
    unknown: bool
    marker_index: int  # 0-based bit index of the VINT marker
    raw: bytes

    def to_json(self) -> dict:
        return {
            "value": self.value,
            "length": self.length,
            "unknown": self.unknown,
            "marker_index": self.marker_index,
            "raw_hex": self.raw.hex(),
        }


def _marker_index(first_byte: int) -> int:
    for i in range(8):
        if first_byte & (0x80 >> i):
            return i
    return -1


def read_vint(buf: bytes, pos: int, *, unknown_lenient: bool = False) -> VInt:
    """Read a VINT. Element IDs never use unknown semantics; sizes may."""
    if pos >= len(buf):
        raise EbmlError("vint: truncated at offset %d" % pos)
    first = buf[pos]
    idx = _marker_index(first)
    if idx < 0:
        # Matroska streaming allows an all-zero first octet; the marker then
        # appears in a later octet (max 8).
        if not unknown_lenient:
            raise EbmlError("vint: zero first octet at %d" % pos)
        idx = None
        for scan in range(1, 8):
            if pos + scan >= len(buf):
                raise EbmlError("vint: truncated zero-prefix size")
            sub = _marker_index(buf[pos + scan])
            if sub >= 0:
                idx = scan * 8 + sub
                break
        if idx is None:
            raise EbmlError("vint: no marker within 8 octets")
        length = idx + 1
    else:
        length = idx + 1
    if pos + length > len(buf):
        raise EbmlError("vint: truncated value")
    raw = buf[pos:pos + length]
    data_bits = 8 * length - length
    mask = (1 << data_bits) - 1
    value = 0
    for b in raw:
        value = (value << 8) | b
    value &= mask
    max_data = (1 << data_bits) - 1
    unknown = value == max_data
    if first == 0x00:
        unknown = True
    return VInt(None if unknown else value, length, unknown, idx if idx is not None else 7, raw)


def encode_vint(value: Optional[int], length: Optional[int] = None,
                *, unknown: bool = False) -> bytes:
    if unknown:
        length = length or 8
        first = 1 << (8 - length)
        return bytes([first]) + b"\xff" * (length - 1)
    if value is None:
        return encode_vint(None, length or 8, unknown=True)
    if value < 0:
        raise EbmlError("unsigned vint cannot be negative")
    if length is None:
        length = 1
        while value >= (1 << (7 * length)) and length < 8:
            length += 1
        if value >= (1 << (7 * length)):
            raise EbmlError("vint value too large")
    marker = 1 << (8 - length)
    data_bits = 7 * length
    if value >= (1 << data_bits):
        raise EbmlError("vint value does not fit in %d octets" % length)
    top_bits = 8 - length
    top = value >> (8 * (length - 1))
    first = marker | (top & ((1 << top_bits) - 1))
    out = bytearray([first])
    for i in range(length - 2, -1, -1):
        out.append((value >> (8 * i)) & 0xFF)
    return bytes(out)


def _signed_len_for(value: int) -> int:
    length = 1
    while True:
        bias = 1 << (7 * length - 1)
        if -bias <= value <= bias - 1:
            return length
        length += 1
        if length > 8:
            raise EbmlError("signed vint too large")


def encode_signed_vint(value: int, length: Optional[int] = None) -> bytes:
    """Lacing delta encoding: normal VINT framing, top data bit is sign."""
    if length is None:
        length = _signed_len_for(value)
    bias = 1 << (7 * length - 1)
    if not -bias <= value <= bias - 1:
        raise EbmlError("signed vint does not fit %d octets" % length)
    return encode_vint(value + bias, length)


def read_signed_vint(buf: bytes, pos: int) -> tuple:
    v = read_vint(buf, pos)
    bias = 1 << (7 * v.length - 1)
    return (v.value - bias if v.value is not None else None), v.length


# --- Elements ---------------------------------------------------------------

@dataclass
class Element:
    name: str
    eid: int
    kind: str
    id_offset: int          # absolute offset of the ID VINT
    size_offset: int        # absolute offset of the size VINT
    data_offset: int        # absolute offset of the first data byte
    end_offset: Optional[int]  # exclusive; None for unknown size
    size_unknown: bool
    id_raw: bytes
    size_raw: bytes
    raw: bytes              # ID + size + data (data truncated if malformed)
    value: object = None
    children: list = field(default_factory=list)
    problems: list = field(default_factory=list)

    @property
    def size(self) -> Optional[int]:
        if self.size_unknown or self.end_offset is None:
            return None
        return self.end_offset - self.data_offset

    def child(self, name: str) -> Optional["Element"]:
        for c in self.children:
            if c.name == name:
                return c
        return None

    def all_children(self, name: str) -> list:
        return [c for c in self.children if c.name == name]

    def to_tree(self) -> dict:
        entry = REV_REGISTRY.get(self.eid)
        out = {
            "name": self.name,
            "id_hex": "0x%X" % self.eid,
            "kind": self.kind,
            "id_offset": self.id_offset,
            "size_offset": self.size_offset,
            "data_offset": self.data_offset,
            "end_offset": self.end_offset,
            "size_unknown": self.size_unknown,
            "size_bytes": self.size,
            "id_raw_hex": self.id_raw.hex(),
            "size_raw_hex": self.size_raw.hex(),
            "raw_range": [self.id_offset, self.end_offset],
            "value": self.value_to_json(),
            "problems": list(self.problems),
            "children": [c.to_tree() for c in self.children],
        }
        return out

    def value_to_json(self) -> object:
        if self.kind == "m":
            return None
        if isinstance(self.value, (bytes, bytearray)):
            return {"hex": bytes(self.value).hex(), "length": len(self.value)}
        return self.value


def _read_uint(b: bytes) -> int:
    return int.from_bytes(b, "big", signed=False)


def _read_int(b: bytes) -> int:
    return int.from_bytes(b, "big", signed=True)


def _decode_float(b: bytes) -> float:
    if len(b) == 0:
        return 0.0
    if len(b) == 4:
        return struct.unpack(">f", b)[0]
    if len(b) == 8:
        return struct.unpack(">d", b)[0]
    # Non-canonical length: normalize via big-endian int reinterpretation,
    # keep raw representation attached by the caller.
    pad = b.rjust(8, b"\x00" if b[0] < 0x80 else b"\xff")
    return struct.unpack(">d", pad)[0]


def _decode_leaf(kind: str, data: bytes, problems: list) -> object:
    if kind == "u":
        if not data:
            problems.append({"level": "warning", "code": "empty_uint",
                             "message": "empty unsigned integer treated as 0"})
            return 0
        return _read_uint(data)
    if kind == "i":
        if not data:
            problems.append({"level": "warning", "code": "empty_int",
                             "message": "empty signed integer treated as 0"})
            return 0
        return _read_int(data)
    if kind == "f":
        if len(data) not in (0, 4, 8):
            problems.append({"level": "warning", "code": "noncanonical_float",
                             "message": "float has non-canonical width %d" % len(data)})
        return _decode_float(data)
    if kind == "s":
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            problems.append({"level": "warning", "code": "bad_utf8",
                             "message": "string is not valid UTF-8"})
            return data.decode("utf-8", "replace")
    if kind == "b":
        return bytes(data)
    return bytes(data)


def _lookup(eid: int):
    entry = REV_REGISTRY.get(eid)
    if entry is None:
        return ("Unknown-0x%X" % eid, "b", True)
    name, kind = entry
    return name, kind, False


def parse_element(buf: bytes, pos: int) -> Element:
    """Parse one element (possibly a master) starting at ``pos``."""
    id_vint = read_vint(buf, pos)
    eid = id_vint.value
    if eid is None:
        raise EbmlError("element id cannot be unknown at %d" % pos)
    size_pos = pos + id_vint.length
    size_vint = read_vint(buf, size_pos, unknown_lenient=True)
    data_pos = size_pos + size_vint.length
    name, kind, _unknown_name = _lookup(eid)
    problems: list = []
    if size_vint.unknown:
        data_end = None
    else:
        data_end = data_pos + size_vint.value
        if data_end > len(buf):
            problems.append({
                "level": "error", "code": "declared_size_exceeds_container",
                "message": "declared size exceeds remaining bytes",
                "declared_end": data_end, "available_end": len(buf),
            })
            data_end = len(buf)
    el = Element(name, eid, kind, pos, size_pos, data_pos, data_end,
                 bool(size_vint.unknown), id_vint.raw, size_vint.raw,
                 buf[pos:data_end if data_end is not None else len(buf)],
                 problems=problems)
    if kind == "m":
        _fill_children(buf, el)
    else:
        data = buf[data_pos:data_end] if data_end is not None else b""
        el.value = _decode_leaf(kind, data, problems)
    return el


def _level1_boundary(buf: bytes, pos: int, limit: Optional[int]) -> Optional[int]:
    """If a level-1 master ID begins at pos, return its header end offset."""
    end = limit if limit is not None else len(buf)
    if pos + 1 > end:
        return None
    first = buf[pos]
    if first in (0x11, 0x15, 0x16, 0x19, 0x12, 0x10, 0x1F):
        try:
            vid = read_vint(buf, pos)
        except EbmlError:
            return None
        if vid.value in LEVEL1_MASTERS:
            return pos + vid.length
    return None


def _fill_children(buf: bytes, parent: Element) -> None:
    pos = parent.data_offset
    hard_end = len(buf)
    declared_end = parent.end_offset
    streaming_parent = declared_end is None and parent.eid in LEVEL1_MASTERS

    while pos < hard_end:
        if declared_end is not None and pos >= declared_end:
            break
        if streaming_parent and _level1_boundary(buf, pos, hard_end) is not None:
            # A sibling level-1 master starts here; it terminates the
            # unknown-size streaming scope of this parent.
            parent.end_offset = pos
            parent.raw = buf[parent.id_offset:pos]
            return
        try:
            child = parse_element(buf, pos)
        except EbmlError as exc:
            parent.problems.append({
                "level": "error", "code": "unparseable_child",
                "message": str(exc), "offset": pos,
            })
            break
        parent.children.append(child)

        if child.end_offset is None:
            # Unknown-size child: recover the boundary by scanning for the
            # next level-1 master; recursion inside ``parse_element`` already
            # stops there for streaming-capable masters.
            stop = declared_end if declared_end is not None else hard_end
            boundary_offset = None
            scan = child.data_offset
            while scan < stop:
                if _level1_boundary(buf, scan, stop) is not None:
                    boundary_offset = scan
                    break
                scan += 1
            if boundary_offset is None:
                boundary_offset = stop
            child.end_offset = boundary_offset
            child.raw = buf[child.id_offset:boundary_offset]
        pos = child.end_offset
        if declared_end is not None and pos > declared_end:
            parent.problems.append({
                "level": "error", "code": "child_overrun",
                "message": "child at %d extends past declared parent end" % child.id_offset,
            })
            break


def parse_document(buf: bytes) -> list:
    elements = []
    pos = 0
    while pos < len(buf):
        try:
            el = parse_element(buf, pos)
        except EbmlError as exc:
            raise EbmlError("top-level parse failure at %d: %s" % (pos, exc))
        elements.append(el)
        if el.end_offset is None:
            el.end_offset = len(buf)
        pos = el.end_offset
    return elements


def find_segment(elements: list) -> Optional[Element]:
    for el in elements:
        if el.eid == SEGMENT:
            return el
    return None


# --- Block / SimpleBlock / lacing ------------------------------------------

@dataclass
class ParsedBlock:
    track_number: Optional[int]
    relative_timecode: Optional[int]   # signed 16-bit already
    flags: int
    keyframe: bool
    invisible: bool
    lacing: int                         # 0 none, 1 Xiph, 3 EBML, 2 fixed
    discardable: bool                  # SimpleBlock only
    frame_count: int
    frame_ranges: list                 # [start,end) offsets within payload
    payload: bytes
    problems: list = field(default_factory=list)
    corruption: Optional[dict] = None

    def to_json(self) -> dict:
        return {
            "track_number": self.track_number,
            "relative_timecode": self.relative_timecode,
            "flags": self.flags,
            "keyframe": self.keyframe,
            "invisible": self.invisible,
            "lacing": self.lacing,
            "lacing_name": {0: "none", 1: "xiph", 2: "fixed", 3: "ebml"}.get(self.lacing, "unknown"),
            "discardable": self.discardable,
            "frame_count": self.frame_count,
            "frame_ranges": self.frame_ranges,
            "payload_range": [0, len(self.payload)],
            "problems": list(self.problems),
            "corruption": self.corruption,
        }


def parse_block(payload: bytes, *, simple: bool) -> ParsedBlock:
    problems: list = []
    try:
        tv = read_vint(payload, 0)
        track_number = tv.value
    except EbmlError as exc:
        pb = ParsedBlock(None, None, 0, False, False, 0, False, 0, [], payload,
                         [{"level": "error", "code": "bad_track_vint",
                           "message": str(exc)}])
        pb.corruption = {"kind": "bad_track_vint", "offset": 0}
        return pb
    p = tv.length
    if len(payload) < p + 3:
        pb = ParsedBlock(track_number, None, 0, False, False, 0, False, 0, [], payload,
                         [{"level": "error", "code": "truncated_block_header",
                           "message": "block header shorter than track vint + 3 bytes"}])
        pb.corruption = {"kind": "truncated_header", "offset": p}
        return pb
    rel = int.from_bytes(payload[p:p + 2], "big", signed=True)
    flags = payload[p + 2]
    p += 3
    lacing = (flags >> 1) & 0x03
    keyframe = bool(flags & 0x80) if simple else False
    invisible = bool(flags & 0x08)
    discardable = bool(flags & 0x01) if simple else False
    pb = ParsedBlock(track_number, rel, flags, keyframe, invisible, lacing,
                     discardable, 1, [], payload, problems)

    data = payload[p:]
    if lacing == 0:
        pb.frame_count = 1
        pb.frame_ranges = [[p, p + len(data)]]
        return pb
    if not data:
        pb.problems.append({"level": "error", "code": "lacing_no_count",
                            "message": "laced block has no frame-count byte"})
        pb.corruption = {"kind": "lacing_no_count", "offset": p}
        pb.frame_ranges = []
        return pb
    count = data[0]
    pb.frame_count = count + 1
    body = data[1:]
    if lacing == 2:  # fixed size
        n = pb.frame_count
        if n == 0 or len(body) % n != 0:
            pb.problems.append({
                "level": "error", "code": "fixed_lacing_indivisible",
                "message": "fixed-lacing payload %d not divisible by %d frames"
                           % (len(body), n)})
            pb.corruption = {"kind": "fixed_lacing_indivisible",
                             "offset": p + 1, "payload_bytes": len(body),
                             "declared_frames": n}
            # Do not invent frame boundaries for timeline use.
            pb.frame_ranges = []
            return pb
        w = len(body) // n
        pb.frame_ranges = [[p + 1 + i * w, p + 1 + (i + 1) * w] for i in range(n)]
        return pb
    if lacing == 1:  # Xiph
        sizes, consumed, corrupt = _xiph_sizes(body, pb.frame_count)
        if corrupt is not None:
            pb.problems.append(corrupt)
            pb.corruption = {"kind": "xiph_size_overflow", "offset": p + 1, **corrupt.get("detail", {})}
            pb.frame_ranges = []
            return pb
        return _finish_laced_ranges(pb, p + 1, sizes, consumed, body)
    if lacing == 3:  # EBML
        sizes, consumed, corrupt = _ebml_sizes(body, pb.frame_count)
        if corrupt is not None:
            pb.problems.append(corrupt)
            pb.corruption = {"kind": "ebml_lacing_size_overflow", "offset": p + 1,
                             **corrupt.get("detail", {})}
            pb.frame_ranges = []
            return pb
        return _finish_laced_ranges(pb, p + 1, sizes, consumed, body)

    pb.problems.append({"level": "error", "code": "reserved_lacing",
                        "message": "reserved lacing value %d" % lacing})
    pb.corruption = {"kind": "reserved_lacing", "offset": p - 1}
    pb.frame_ranges = []
    return pb


def _xiph_sizes(body: bytes, n: int):
    sizes = []
    i = 0
    for frame in range(n - 1):
        total = 0
        start = i
        while True:
            if i >= len(body):
                return None, None, {"level": "error", "code": "xiph_truncated",
                                    "message": "Xiph lacing size header truncated",
                                    "detail": {"frame_index": frame}}
            b = body[i]
            i += 1
            total += b
            if b != 255:
                break
        sizes.append(total)
    if not sizes and n == 1:
        return [], 0, None
    return sizes, i, None


def _ebml_sizes(body: bytes, n: int):
    sizes = []
    i = 0
    if n - 1 >= 1:
        try:
            first, ln = read_vint(body, 0), None
        except EbmlError as exc:
            return None, None, {"level": "error", "code": "ebml_lacing_bad_vint",
                                "message": str(exc), "detail": {}}
        sizes.append(first.value)
        i = first.length
        prev = first.value
        for frame in range(1, n - 1):
            try:
                delta, ln = read_signed_vint(body, i)
            except EbmlError as exc:
                return None, None, {"level": "error", "code": "ebml_lacing_bad_vint",
                                    "message": str(exc),
                                    "detail": {"frame_index": frame, "offset": i}}
            if delta is None:
                return None, None, {"level": "error", "code": "ebml_lacing_unknown_size",
                                    "message": "unknown size inside lacing",
                                    "detail": {"frame_index": frame, "offset": i}}
            i += ln
            prev += delta
            if prev < 0:
                return None, None, {"level": "error",
                                    "code": "ebml_lacing_negative_frame",
                                    "message": "accumulated lacing size became negative",
                                    "detail": {"frame_index": frame, "offset": i}}
            sizes.append(prev)
    return sizes, i, None


def _finish_laced_ranges(pb: ParsedBlock, base: int, sizes: list,
                         consumed: int, body: bytes) -> ParsedBlock:
    declared_total = sum(sizes)
    remaining = len(body) - consumed
    if declared_total > remaining:
        pb.problems.append({
            "level": "error", "code": "lacing_size_overflow",
            "message": "laced sizes sum %d but only %d payload bytes remain"
                       % (declared_total, remaining),
            "detail": {"declared_total": declared_total, "remaining": remaining}})
        pb.corruption = {"kind": "lacing_size_overflow", "offset": base,
                         "declared_total": declared_total, "remaining": remaining}
        # Semantic rule: never guess frame boundaries on damaged lacing.
        pb.frame_ranges = []
        return pb
    ranges = []
    cur = base + consumed
    for size in sizes:
        ranges.append([cur, cur + size])
        cur += size
    # Last frame gets all remaining bytes (Matroska rule).
    ranges.append([cur, base + consumed + remaining])
    pb.frame_ranges = ranges
    tail = remaining - declared_total
    if tail < 0:
        pb.problems.append({"level": "error", "code": "lacing_size_overflow",
                            "message": "negative tail"})
        pb.frame_ranges = []
    return pb
