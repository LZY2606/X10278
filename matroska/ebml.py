"""Minimal EBML/Matroska parser.

Design rules:
- Every element keeps its raw byte range (offset, header size, data range).
- Unknown-size elements (streaming style) are preserved as such.
- Block relative timecodes are interpreted as *signed* 16-bit integers.
- Corrupt lacing never leads to guessed frame boundaries; frames become
  indeterminate and the raw bytes stay attached.
"""
from __future__ import annotations

import struct


class ParseError(Exception):
    pass


# id -> (name, type).  type in {master, uint, int, float, string, utf8, binary, date}
SCHEMA = {
    0x1A45DFA3: ("EBML", "master"),
    0x4286: ("EBMLVersion", "uint"),
    0x42F7: ("EBMLReadVersion", "uint"),
    0x42F2: ("EBMLMaxIDLength", "uint"),
    0x42F3: ("EBMLMaxSizeLength", "uint"),
    0x4282: ("DocType", "string"),
    0x18538067: ("Segment", "master"),
    0x1549A966: ("Info", "master"),
    0x2AD7B1: ("TimecodeScale", "uint"),
    0x4489: ("Duration", "float"),
    0x4D80: ("MuxingApp", "utf8"),
    0x5741: ("WritingApp", "utf8"),
    0x1654AE6B: ("Tracks", "master"),
    0xAE: ("TrackEntry", "master"),
    0xD7: ("TrackNumber", "uint"),
    0x73C5: ("TrackUID", "uint"),
    0x83: ("TrackType", "uint"),
    0x86: ("CodecID", "string"),
    0x22B59C: ("Language", "string"),
    0x23314F: ("TrackTimecodeScale", "float"),
    0x23E383: ("DefaultDuration", "uint"),
    0x1F43B675: ("Cluster", "master"),
    0xE7: ("Timestamp", "int"),
    0xA3: ("SimpleBlock", "binary"),
    0xA0: ("BlockGroup", "master"),
    0xA1: ("Block", "binary"),
    0x9B: ("BlockDuration", "uint"),
    0x75A2: ("DiscardPadding", "int"),
    0x1C53BB6B: ("Cues", "master"),
    0xBB: ("CuePoint", "master"),
    0xB3: ("CueTime", "uint"),
    0xB7: ("CueTrackPositions", "master"),
    0xF7: ("CueTrack", "uint"),
    0xF1: ("CueClusterPosition", "uint"),
}

MASTER_IDS = {eid for eid, (_, t) in SCHEMA.items() if t == "master"}

LACING_NAMES = {0: "none", 1: "xiph", 2: "fixed", 3: "ebml"}


def read_vint(data: bytes, pos: int):
    """Read a variable-length integer at pos.

    Returns (data_value, length, raw_value).  data_value has the marker bit
    stripped (for sizes); raw_value keeps the marker bit (for element IDs).
    """
    if pos >= len(data):
        raise ParseError(f"vint beyond end of data at {pos}")
    first = data[pos]
    mask = 0x80
    length = 1
    while length <= 8 and not (first & mask):
        mask >>= 1
        length += 1
    if length > 8:
        raise ParseError(f"invalid vint marker at {pos}")
    if pos + length > len(data):
        raise ParseError(f"truncated vint at {pos}")
    raw = int.from_bytes(data[pos:pos + length], "big")
    value = first & (mask - 1)
    for i in range(1, length):
        value = (value << 8) | data[pos + i]
    return value, length, raw


def _decode_value(etype: str, payload: bytes):
    if etype == "uint":
        return int.from_bytes(payload, "big") if payload else 0
    if etype == "int":
        return int.from_bytes(payload, "big", signed=True) if payload else 0
    if etype == "float":
        if len(payload) == 4:
            return struct.unpack(">f", payload)[0]
        if len(payload) == 8:
            return struct.unpack(">d", payload)[0]
        raise ParseError(f"bad float size {len(payload)}")
    if etype == "string":
        return payload.decode("ascii", errors="replace").rstrip("\x00")
    if etype == "utf8":
        return payload.decode("utf-8", errors="replace").rstrip("\x00")
    return None  # binary / master / unknown: keep raw only


def parse_elements(data: bytes, start: int, end: int, diagnostics: list):
    """Parse sibling elements in data[start:end]."""
    elements = []
    pos = start
    while pos < end:
        el_offset = pos
        try:
            _, id_len, el_id = read_vint(data, pos)
        except ParseError as exc:
            diagnostics.append({"code": "TRUNCATED_ID", "offset": pos, "detail": str(exc)})
            break
        pos += id_len
        try:
            size, size_len, _ = read_vint(data, pos)
        except ParseError as exc:
            diagnostics.append({"code": "TRUNCATED_SIZE", "offset": pos, "detail": str(exc)})
            break
        pos += size_len
        unknown_size = size == (1 << (7 * size_len)) - 1
        name, etype = SCHEMA.get(el_id, (f"Unknown<0x{el_id:x}>", "binary"))
        header_size = id_len + size_len
        data_start = el_offset + header_size
        if unknown_size:
            data_end = end  # extends to end of the enclosing master
        else:
            data_end = data_start + size
        truncated = data_end > end
        if truncated:
            diagnostics.append({
                "code": "TRUNCATED_ELEMENT",
                "offset": el_offset,
                "detail": f"{name} declares size past enclosing end",
            })
            data_end = end
        el = {
            "el_id": f"0x{el_id:x}",
            "name": name,
            "type": etype,
            "offset": el_offset,
            "header_size": header_size,
            "size": None if unknown_size else size,
            "unknown_size": unknown_size,
            "data_start": data_start,
            "data_end": data_end,
            "byte_range": [el_offset, data_end],
            "value": None,
            "children": [],
            "diagnostics": [],
        }
        if truncated:
            el["diagnostics"].append("TRUNCATED_ELEMENT")
        payload = data[data_start:data_end]
        if etype == "master":
            el["children"] = parse_elements(data, data_start, data_end, el["diagnostics"])
        elif etype == "binary":
            el["value"] = None  # raw bytes stay in the source blob
        else:
            try:
                el["value"] = _decode_value(etype, payload)
            except ParseError as exc:
                el["diagnostics"].append(f"DECODE_ERROR:{exc}")
        elements.append(el)
        if unknown_size:
            break  # unknown-size element consumes the rest of the container
        pos = data_end
    return elements


def parse_segment(data: bytes):
    """Parse a whole file. Returns (elements, diagnostics)."""
    diagnostics: list = []
    elements = parse_elements(data, 0, len(data), diagnostics)
    return elements, diagnostics


def iter_elements(elements, name=None):
    for el in elements:
        if name is None or el["name"] == name:
            yield el
        yield from iter_elements(el["children"], name)


# ---------------------------------------------------------------- blocks

def _parse_ebml_lacing(payload: bytes, pos: int, nframes: int):
    sizes = []
    first, vlen, _ = read_vint(payload, pos)
    sizes.append(first)
    pos += vlen
    prev = first
    for _ in range(nframes - 2):
        diff, dlen, _ = read_vint(payload, pos)
        bias = (1 << (7 * dlen - 1)) - 1
        prev = prev + diff - bias
        if prev < 0:
            raise ParseError("negative lace size")
        sizes.append(prev)
        pos += dlen
    return sizes, pos


def parse_block(payload: bytes, base_offset: int):
    """Parse a SimpleBlock/Block payload.

    Returns a dict with the signed relative timecode, lacing info and either
    concrete frame byte ranges or an indeterminate status.  Frame boundaries
    are never guessed when lacing data is inconsistent.
    """
    diags = []
    track, tlen, _ = read_vint(payload, 0)
    if tlen + 3 > len(payload):
        raise ParseError("block header truncated")
    timecode = int.from_bytes(payload[tlen:tlen + 2], "big", signed=True)
    flags = payload[tlen + 2]
    lacing_code = (flags >> 1) & 0x3
    lacing = LACING_NAMES[lacing_code]
    body = payload[tlen + 3:]
    body_off = base_offset + tlen + 3
    frames = None
    status = "ok"
    if lacing_code == 0:
        frames = [{"offset": body_off, "size": len(body)}]
    else:
        try:
            if not body:
                raise ParseError("missing lace count")
            nframes = body[0] + 1
            pos = 1
            if lacing_code == 1:  # xiph
                sizes = []
                for _ in range(nframes - 1):
                    run = 0
                    while True:
                        if pos >= len(body):
                            raise ParseError("xiph lace size truncated")
                        run += body[pos]
                        pos += 1
                        if body[pos - 1] != 255:
                            break
                    sizes.append(run)
                last = len(body) - pos - sum(sizes)
                if last < 0:
                    raise ParseError("xiph lace sizes exceed payload")
                sizes.append(last)
            elif lacing_code == 2:  # fixed
                remain = len(body) - 1
                if nframes <= 0 or remain % nframes != 0:
                    raise ParseError("fixed lace size not divisible")
                sizes = [remain // nframes] * nframes
            else:  # ebml
                sizes, pos = _parse_ebml_lacing(body, pos, nframes)
                last = len(body) - pos - sum(sizes)
                if last < 0:
                    raise ParseError("ebml lace sizes exceed payload")
                sizes.append(last)
            frames = []
            cur = body_off + pos
            for sz in sizes:
                frames.append({"offset": cur, "size": sz})
                cur += sz
        except ParseError as exc:
            status = "indeterminate"
            diags.append({
                "code": "LACING_CORRUPT",
                "detail": f"{lacing} lacing inconsistent: {exc}; "
                          f"frame boundaries not guessed",
            })
            frames = None
    return {
        "track_number": track,
        "timecode": timecode,  # signed int16, relative to cluster timestamp
        "flags": flags,
        "lacing": lacing,
        "frame_count": len(frames) if frames else None,
        "frames": frames,
        "status": status,
        "diagnostics": diags,
        "payload_range": [base_offset, base_offset + len(payload)],
    }
