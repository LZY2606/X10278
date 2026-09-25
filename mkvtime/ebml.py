"""EBML/Matroska 原始解析层。

只负责把字节解析成带偏移、长度、未知长度标记与原始字节范围的元素树，
以及 Block/SimpleBlock 的底层拆分（track、有符号 relative timecode、lacing）。
不做任何时间规范化——那是派生层的事。
"""
from __future__ import annotations

ELEMENTS = {
    0x1A45DFA3: ("EBML", "master"),
    0x4286: ("EBMLVersion", "uint"),
    0x42F7: ("EBMLReadVersion", "uint"),
    0x42F2: ("EBMLMaxIDLength", "uint"),
    0x42F3: ("EBMLMaxSizeLength", "uint"),
    0x4282: ("DocType", "string"),
    0x4287: ("DocTypeVersion", "uint"),
    0x4285: ("DocTypeReadVersion", "uint"),
    0x18538067: ("Segment", "master"),
    0x1549A966: ("Info", "master"),
    0x2AD7B1: ("TimecodeScale", "uint"),
    0x4489: ("Duration", "float"),
    0x4D80: ("MuxingApp", "string"),
    0x5741: ("WritingApp", "string"),
    0x1654AE6B: ("Tracks", "master"),
    0xAE: ("TrackEntry", "master"),
    0xD7: ("TrackNumber", "uint"),
    0x73C5: ("TrackUID", "uint"),
    0x83: ("TrackType", "uint"),
    0x9C: ("FlagLacing", "uint"),
    0x536E: ("Name", "string"),
    0x86: ("CodecID", "string"),
    0x22B59C: ("Language", "string"),
    0x23E383: ("DefaultDuration", "uint"),
    0x23314F: ("TrackTimecodeScale", "float"),
    0x1F43B675: ("Cluster", "master"),
    0xE7: ("Timecode", "uint"),
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
    0xEC: ("Void", "binary"),
}

TRACK_TYPES = {1: "video", 2: "audio", 3: "complex", 16: "logo", 17: "subtitle", 18: "buttons", 32: "control"}

# 未知长度 master 的合法子元素集合：遇到集合之外的 ID 即认为该 master 结束。
CHILDREN = {
    0x1A45DFA3: {0x4286, 0x42F7, 0x42F2, 0x42F3, 0x4282, 0x4287, 0x4285, 0xEC},
    0x18538067: {0x1549A966, 0x1654AE6B, 0x1F43B675, 0x1C53BB6B, 0xEC},
    0x1549A966: {0x2AD7B1, 0x4489, 0x4D80, 0x5741, 0xEC},
    0x1654AE6B: {0xAE, 0xEC},
    0xAE: {0xD7, 0x73C5, 0x83, 0x9C, 0x536E, 0x86, 0x22B59C, 0x23E383, 0x23314F, 0xEC},
    0x1F43B675: {0xE7, 0xA3, 0xA0, 0xEC},
    0xA0: {0xA1, 0x9B, 0x75A2, 0xEC},
    0x1C53BB6B: {0xBB, 0xEC},
    0xBB: {0xB3, 0xB7, 0xEC},
    0xB7: {0xF7, 0xF1, 0xEC},
}


def read_id(data: bytes, pos: int):
    """读元素 ID（保留 marker 位）。返回 (id_int, length) 或 (None, 0)。"""
    if pos >= len(data):
        return None, 0
    b0 = data[pos]
    if b0 == 0:
        return None, 0
    mask, length = 0x80, 1
    while length <= 4 and not (b0 & mask):
        mask >>= 1
        length += 1
    if length > 4 or pos + length > len(data):
        return None, 0
    return int.from_bytes(data[pos:pos + length], "big"), length


def read_size(data: bytes, pos: int):
    """读 VINT 尺寸。返回 (value, length, unknown)。"""
    if pos >= len(data):
        return None, 0, False
    b0 = data[pos]
    if b0 == 0:
        return None, 0, False
    mask, length = 0x80, 1
    while length <= 8 and not (b0 & mask):
        mask >>= 1
        length += 1
    if length > 8 or pos + length > len(data):
        return None, 0, False
    value = int.from_bytes(data[pos:pos + length], "big") & ((1 << (7 * length)) - 1)
    unknown = value == (1 << (7 * length)) - 1
    return value, length, unknown


def read_vint_value(data: bytes, pos: int):
    """读 VINT 数值（去掉 marker），用于 track number / EBML lacing 尺寸。"""
    value, length, _ = read_size(data, pos)
    return value, length


def read_signed_vint(data: bytes, pos: int):
    """EBML lacing 的有符号尺寸差：value - bias，bias = 2^(7n-1) - 1。"""
    value, length = read_vint_value(data, pos)
    bias = (1 << (7 * length - 1)) - 1
    return value - bias, length


def _decode_leaf(etype: str, payload: bytes):
    try:
        if etype == "uint":
            return int.from_bytes(payload, "big") if payload else 0
        if etype == "int":
            return int.from_bytes(payload, "big", signed=True) if payload else 0
        if etype == "float":
            import struct
            if len(payload) == 4:
                return struct.unpack(">f", payload)[0]
            if len(payload) == 8:
                return struct.unpack(">d", payload)[0]
            return None
        if etype == "string":
            return payload.decode("utf-8", "replace")
    except Exception:
        return None
    return None


def parse(data: bytes):
    """解析整个文件，返回按文件顺序排列的元素 dict 列表。

    每个元素携带：uid / eid / name / etype / offset / header_size / size
    （未知长度为 None）/ unknown_size / data_offset / end / depth / parent /
    raw_range。未知长度 master 的 end 由子元素扫描实际终止位置回填。
    """
    elements: list[dict] = []

    def walk(start: int, end: int, parent_uid, parent_eid, depth: int) -> int:
        pos = start
        allowed = CHILDREN.get(parent_eid) if parent_eid is not None else None
        while pos < end:
            el_start = pos
            eid, idlen = read_id(data, pos)
            if eid is None:
                break
            if allowed is not None and eid not in allowed:
                break
            name, etype = ELEMENTS.get(eid, (f"Unknown(0x{eid:X})", "binary"))
            size, slen, unknown = read_size(data, pos + idlen)
            if size is None and not unknown:
                break
            data_off = pos + idlen + slen
            uid = f"el:{len(elements)}"
            rec = {
                "uid": uid,
                "eid": f"0x{eid:X}",
                "eid_int": eid,
                "name": name,
                "etype": etype,
                "offset": el_start,
                "header_size": idlen + slen,
                "size": None if unknown else size,
                "unknown_size": bool(unknown),
                "data_offset": data_off,
                "end": None if unknown else min(data_off + size, len(data)),
                "depth": depth,
                "parent": parent_uid,
            }
            elements.append(rec)
            if etype == "master":
                child_end = len(data) if unknown else rec["end"]
                real_end = walk(data_off, child_end, uid, eid, depth + 1)
                if unknown:
                    rec["end"] = real_end
                pos = rec["end"]
            else:
                if not unknown:
                    rec["value"] = _decode_leaf(etype, data[data_off:rec["end"]])
                pos = rec["end"]
            rec["raw_range"] = [rec["offset"], rec["end"]]
            if pos is None or pos <= el_start:
                pos = el_start + max(idlen + slen, 1)
                break
        return pos

    walk(0, len(data), None, None, 0)
    return elements


LACING_MODES = {0: "none", 1: "xiph", 2: "fixed", 3: "ebml"}


def parse_block(data: bytes, data_off: int, end: int):
    """拆分 Block/SimpleBlock 负载。

    relative timecode 一律按有符号 int16 解释。lacing 损坏时返回
    frames=None 与 lace_error 诊断，绝不猜测帧边界。
    """
    track, tlen = read_vint_value(data, data_off)
    p = data_off + tlen
    rel_tc = int.from_bytes(data[p:p + 2], "big", signed=True)
    p += 2
    flags = data[p]
    p += 1
    lace_mode = (flags >> 1) & 0x3
    result = {
        "track_number": track,
        "rel_tc": rel_tc,
        "rel_tc_hex": f"0x{(rel_tc & 0xFFFF):04X}",
        "flags": flags,
        "keyframe": bool(flags & 0x80),
        "invisible": bool(flags & 0x08),
        "lacing": LACING_MODES[lace_mode],
        "frames": None,
        "lace_error": None,
        "lace_header_range": None,
        "payload_range": [data_off, end],
    }
    if lace_mode == 0:
        result["frames"] = [[p, end - p]]
        return result
    if p >= end:
        result["lace_error"] = {"reason": "lacing 头缺失", "available": 0}
        return result
    count = data[p] + 1
    p += 1
    header_start = p
    sizes: list[int] = []
    error = None
    if lace_mode == 1:  # xiph
        for _ in range(count - 1):
            s = 0
            while p < end and data[p] == 255:
                s += 255
                p += 1
            if p >= end:
                error = {"reason": "xiph lacing 尺寸截断", "declared_sizes": sizes}
                break
            s += data[p]
            p += 1
            sizes.append(s)
    elif lace_mode == 2:  # fixed
        rem = end - p
        if rem % count != 0:
            error = {"reason": "fixed lacing 负载不能均分", "available": rem, "count": count}
        else:
            sizes = [rem // count] * (count - 1)
    else:  # ebml
        first, flen = read_vint_value(data, p)
        p += flen
        sizes.append(first)
        cur = first
        for _ in range(count - 2):
            diff, dlen = read_signed_vint(data, p)
            p += dlen
            cur += diff
            sizes.append(cur)
    result["lace_header_range"] = [header_start, p]
    if error is None:
        available = end - p
        used = sum(sizes)
        last = available - used
        if any(s < 0 for s in sizes) or last < 0:
            error = {
                "reason": "lacing 声明尺寸超出可用负载",
                "declared_sizes": sizes,
                "available": available,
                "deficit": -last if last < 0 else 0,
            }
        else:
            frames = []
            q = p
            for s in sizes + [last]:
                frames.append([q, s])
                q += s
            result["frames"] = frames
    if error is not None:
        result["lace_error"] = error
        result["frames"] = None
    return result
