"""EBML/Matroska parser.

Design rules:
- Every element keeps its absolute offset, header size, data range and raw bytes.
- Unknown-size elements (all size bits set) extend to the end of their parent.
- Unparseable byte ranges are preserved as `gap` regions, never dropped.
- Elements that overrun their parent are clamped and flagged `corrupt`.
"""
from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass, field

NAMES = {
    0x1A45DFA3: "EBML", 0x4286: "EBMLVersion", 0x42F7: "EBMLReadVersion",
    0x42F2: "EBMLMaxIDLength", 0x42F3: "EBMLMaxSizeLength", 0x4282: "DocType",
    0x4287: "DocTypeVersion", 0x4285: "DocTypeReadVersion",
    0x18538067: "Segment", 0x1549A966: "Info", 0x2AD7B1: "TimecodeScale",
    0x4489: "Duration", 0x1654AE6B: "Tracks", 0xAE: "TrackEntry",
    0xD7: "TrackNumber", 0x73C5: "TrackUID", 0x83: "TrackType",
    0x536E: "Name", 0x86: "CodecID", 0x23314F: "TrackTimecodeScale",
    0xE0: "Video", 0xB0: "PixelWidth", 0xBA: "PixelHeight",
    0xE1: "Audio", 0xB5: "SamplingFrequency", 0x9F: "Channels",
    0x1C53BB6B: "Cues", 0xBB: "CuePoint", 0xB3: "CueTime",
    0xB7: "CueTrackPositions", 0xF7: "CueTrack", 0xF1: "CueClusterPosition",
    0x5378: "CueBlockNumber",
    0x1F43B675: "Cluster", 0xE7: "Timecode", 0xA3: "SimpleBlock",
    0xA0: "BlockGroup", 0xA1: "Block", 0x9B: "BlockDuration",
    0x75A2: "DiscardPadding", 0xA7: "Position", 0xAB: "PrevSize",
    0xEC: "Void", 0xBF: "CRC32",
}

MASTER_IDS = {
    0x1A45DFA3, 0x18538067, 0x1549A966, 0x1654AE6B, 0xAE, 0xE0, 0xE1,
    0x1C53BB6B, 0xBB, 0xB7, 0x1F43B675, 0xA0,
}

# IDs used to re-sync after a corrupt / gap region.
SCAN_IDS = [0x1F43B675, 0x1C53BB6B, 0x1549A966, 0x1654AE6B, 0x18538067]


class ParseError(Exception):
    pass


def read_vint(buf: bytes, pos: int, strip_marker: bool) -> tuple[int, int]:
    """Read an EBML variable-length integer. Returns (value, length)."""
    if pos >= len(buf):
        raise ParseError("vint beyond end of buffer")
    b0 = buf[pos]
    if b0 == 0:
        raise ParseError("invalid vint (zero first byte)")
    mask = 0x80
    length = 1
    while not (b0 & mask):
        mask >>= 1
        length += 1
    if pos + length > len(buf):
        raise ParseError("truncated vint")
    value = (b0 & (mask - 1)) if strip_marker else b0
    for i in range(1, length):
        value = (value << 8) | buf[pos + i]
    return value, length


def read_signed_vint(buf: bytes, pos: int) -> tuple[int, int]:
    """EBML-lacing signed size: vint value biased by half its range."""
    value, length = read_vint(buf, pos, strip_marker=True)
    return value - ((1 << (7 * length - 1)) - 1), length


def is_unknown_size(value: int, length: int) -> bool:
    return value == (1 << (7 * length)) - 1


@dataclass
class Element:
    eid: int
    name: str
    path: str
    offset: int
    header_size: int
    data_offset: int
    size: int | None
    unknown_size: bool
    end: int
    corrupt: bool = False
    children: list = field(default_factory=list)


def _scan_known_id(buf: bytes, pos: int, end: int) -> int | None:
    needles = [i.to_bytes(4, "big") for i in SCAN_IDS]
    i = pos
    while i + 4 <= end:
        for n in needles:
            if buf[i:i + 4] == n:
                return i
        i += 1
    return None


def _parse_level(buf, start, end, parent_path, depth, out_flat, regions):
    pos = start
    counters: dict[str, int] = {}
    while pos < end:
        try:
            eid, ilen = read_vint(buf, pos, strip_marker=False)
            sval, slen = read_vint(buf, pos + ilen, strip_marker=True)
        except ParseError:
            nxt = _scan_known_id(buf, pos + 1, end)
            regions.append({
                "id": f"region:{pos}",
                "kind": "gap",
                "start": pos,
                "end": nxt if nxt is not None else end,
                "reason": "unparseable bytes: no valid EBML element id",
                "raw_hex": buf[pos:(nxt if nxt is not None else end)].hex(),
            })
            if nxt is None:
                return
            pos = nxt
            continue
        name = NAMES.get(eid, f"Unknown_0x{eid:X}")
        unknown = is_unknown_size(sval, slen)
        data_off = pos + ilen + slen
        corrupt = False
        if unknown:
            data_end = end
        else:
            data_end = data_off + sval
            if data_end > end:
                corrupt = True
                data_end = end
                regions.append({
                    "id": f"region:{data_off}",
                    "kind": "corrupt",
                    "start": data_off,
                    "end": end,
                    "reason": f"element {name} declares size beyond parent end",
                    "raw_hex": buf[data_off:end].hex(),
                })
        idx = counters.get(name, 0)
        counters[name] = idx + 1
        path = f"{parent_path}/{name}[{idx}]"
        el = Element(
            eid=eid, name=name, path=path, offset=pos,
            header_size=ilen + slen, data_offset=data_off,
            size=None if unknown else sval, unknown_size=unknown,
            end=data_end, corrupt=corrupt,
        )
        out_flat.append((el, parent_path, depth))
        if eid in MASTER_IDS and not corrupt:
            _parse_level(buf, data_off, data_end, path, depth + 1, out_flat, regions)
        pos = data_end
        if unknown:
            return  # unknown-size element extends to parent end


def parse(buf: bytes) -> dict:
    """Parse a buffer into a flat element list plus gap/corrupt regions."""
    flat: list = []
    regions: list = []
    _parse_level(buf, 0, len(buf), "", 0, flat, regions)
    elements = []
    for el, parent_path, depth in flat:
        elements.append({
            "id": f"el:{el.path}",
            "ebml_id": f"0x{el.eid:X}",
            "name": el.name,
            "path": el.path,
            "parent": parent_path or None,
            "depth": depth,
            "offset": el.offset,
            "header_size": el.header_size,
            "data_offset": el.data_offset,
            "size": el.size,
            "unknown_size": el.unknown_size,
            "end": el.end,
            "corrupt": el.corrupt,
            "raw_hex": buf[el.offset:el.end].hex(),
        })
    return {
        "elements": elements,
        "regions": regions,
        "size": len(buf),
        "sha256": hashlib.sha256(buf).hexdigest(),
    }


# ---- value helpers (raw representation preserved; decoding is derived) ----

def el_uint(buf: bytes, el: dict) -> int:
    return int.from_bytes(buf[el["data_offset"]:el["end"]], "big")


def el_int(buf: bytes, el: dict) -> int:
    return int.from_bytes(buf[el["data_offset"]:el["end"]], "big", signed=True)


def el_float(buf: bytes, el: dict) -> float:
    data = buf[el["data_offset"]:el["end"]]
    if len(data) == 4:
        return struct.unpack(">f", data)[0]
    if len(data) == 8:
        return struct.unpack(">d", data)[0]
    raise ParseError(f"float element with {len(data)} bytes")


def el_str(buf: bytes, el: dict) -> str:
    return buf[el["data_offset"]:el["end"]].decode("utf-8", "replace")


def children_of(elements: list[dict], path: str) -> list[dict]:
    return [e for e in elements if e["parent"] == path]


def child(elements: list[dict], path: str, name: str) -> dict | None:
    for e in elements:
        if e["parent"] == path and e["name"] == name:
            return e
    return None
