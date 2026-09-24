"""EBML/Matroska 解析层。

原则：解析阶段只保留原始事实（元素偏移、未知长度、原始字节范围、
有符号 relative timecode、lacing 原始声明），不做任何时间真值裁定。
"""
from __future__ import annotations

ELEMENT_NAMES = {
    0x1A45DFA3: "EBML",
    0x4286: "EBMLVersion", 0x42F7: "EBMLReadVersion", 0x42F2: "EBMLMaxIDLength",
    0x42F3: "EBMLMaxSizeLength", 0x4282: "DocType", 0x4287: "DocTypeVersion",
    0x4285: "DocTypeReadVersion",
    0x18538067: "Segment",
    0x1549A966: "Info", 0x2AD7B1: "TimecodeScale",
    0x1654AE6B: "Tracks", 0xAE: "TrackEntry", 0xD7: "TrackNumber",
    0x83: "TrackType", 0x23314F: "TrackTimecodeScale", 0x86: "CodecID",
    0x1F43B675: "Cluster", 0xE7: "Timecode",
    0xA0: "BlockGroup", 0xA1: "Block", 0x9B: "BlockDuration",
    0x75A2: "DiscardPadding", 0xA3: "SimpleBlock",
    0x1C53BB6B: "Cues", 0xBB: "CuePoint", 0xB3: "CueTime",
    0xB7: "CueTrackPositions", 0xF7: "CueTrack", 0xF1: "CueClusterPosition",
}

MASTER_IDS = {
    0x1A45DFA3, 0x18538067, 0x1549A966, 0x1654AE6B, 0xAE,
    0x1F43B675, 0xA0, 0x1C53BB6B, 0xBB, 0xB7,
}

# level-1 元素：未知长度 Cluster 遇到它们时结束
LEVEL1_IDS = {0x1A45DFA3, 0x18538067, 0x1F43B675, 0x1C53BB6B}

LACING_NAMES = {0: "none", 1: "xiph", 2: "fixed", 3: "ebml"}


class EbmlError(Exception):
    pass


def read_vint(data: bytes, pos: int, for_id: bool = False):
    """读取 EBML 变长整数。返回 (value, length, unknown)。"""
    if pos >= len(data):
        raise EbmlError(f"vint 越界 @{pos}")
    first = data[pos]
    mask = 0x80
    length = 1
    while length <= 8 and not (first & mask):
        mask >>= 1
        length += 1
    if length > 8 or pos + length > len(data):
        raise EbmlError(f"非法 vint @{pos}")
    value = first if for_id else (first & (mask - 1))
    for i in range(1, length):
        value = (value << 8) | data[pos + i]
    unknown = (not for_id) and value == (1 << (7 * length)) - 1
    return value, length, unknown


def read_signed_vint(data: bytes, pos: int):
    value, length, _ = read_vint(data, pos)
    bias = (1 << (7 * length - 1)) - 1
    return value - bias, length


def _peek_id(data: bytes, pos: int):
    try:
        eid, _, _ = read_vint(data, pos, for_id=True)
        return eid
    except EbmlError:
        return None


def _damaged_element(pos: int, end: int, reason: str) -> dict:
    return {
        "eid": None, "name": "_damaged", "offset": pos, "header_size": 0,
        "size": end - pos, "unknown_size": False, "data_offset": pos,
        "end": end, "children": [], "diagnostics": [reason],
    }


def parse_container(data: bytes, start: int, end: int, stop_ids=frozenset()):
    """解析 [start, end) 区间内的元素序列，返回 (elements, consumed_end)。"""
    elements = []
    pos = start
    end = min(end, len(data))
    while pos < end:
        nxt = _peek_id(data, pos)
        if nxt is not None and nxt in stop_ids:
            break
        try:
            eid, id_len, _ = read_vint(data, pos, for_id=True)
            size, size_len, unknown = read_vint(data, pos + id_len)
        except EbmlError as exc:
            elements.append(_damaged_element(pos, end, f"header-unreadable: {exc}"))
            pos = end
            break
        header = id_len + size_len
        data_off = pos + header
        el = {
            "eid": format(eid, "X"),
            "name": ELEMENT_NAMES.get(eid, f"Unknown-{eid:X}"),
            "offset": pos,
            "header_size": header,
            "size": None if unknown else size,
            "unknown_size": unknown,
            "data_offset": data_off,
            "end": None,
            "children": [],
            "diagnostics": [],
        }
        is_master = eid in MASTER_IDS
        if unknown:
            if is_master:
                child_stop = LEVEL1_IDS if eid != 0x18538067 else frozenset()
                children, child_end = parse_container(data, data_off, end, child_stop)
                el["children"] = children
                el["end"] = child_end
                el["diagnostics"].append("unknown-size-master")
                pos = child_end
            else:
                el["end"] = end
                el["diagnostics"].append("unknown-size-on-non-master")
                pos = end
        else:
            el["end"] = min(data_off + size, len(data))
            if data_off + size > len(data):
                el["diagnostics"].append("size-overruns-file")
            if is_master:
                children, _ = parse_container(data, data_off, el["end"])
                el["children"] = children
            pos = data_off + size
        elements.append(el)
    return elements, pos


def parse(data: bytes):
    elements, _ = parse_container(data, 0, len(data))
    return elements


def iter_elements(elements):
    for el in elements:
        yield el
        yield from iter_elements(el["children"])


def find_all(elements, name):
    return [el for el in iter_elements(elements) if el["name"] == name]


def find(elements, name):
    for el in iter_elements(elements):
        if el["name"] == name:
            return el
    return None


def element_data(data: bytes, el: dict) -> bytes:
    return data[el["data_offset"]:el["end"]]


def read_uint(data: bytes, el: dict) -> int:
    return int.from_bytes(element_data(data, el), "big")


def read_int(data: bytes, el: dict) -> int:
    return int.from_bytes(element_data(data, el), "big", signed=True)


def read_float(data: bytes, el: dict) -> float:
    import struct
    raw = element_data(data, el)
    if len(raw) == 4:
        return struct.unpack(">f", raw)[0]
    return struct.unpack(">d", raw)[0]


def parse_block(payload: bytes) -> dict:
    """解析 Block/SimpleBlock 负载。

    relative timecode 一律按有符号 int16 解释；lacing 损坏时 frames=None，
    绝不猜测帧边界。
    """
    result = {"frames": None, "diagnostics": []}
    try:
        track, tlen, _ = read_vint(payload, 0)
        rel = int.from_bytes(payload[tlen:tlen + 2], "big", signed=True)
        flags = payload[tlen + 2]
    except (EbmlError, IndexError) as exc:
        result["diagnostics"].append(f"block-header-unreadable: {exc}")
        return result
    lacing_bits = (flags >> 1) & 0x3
    lacing = LACING_NAMES[lacing_bits]
    body_off = tlen + 3
    body = payload[body_off:]
    result.update({
        "track": track, "rel_timecode": rel, "rel_interpretation": "signed-int16",
        "flags": flags, "lacing": lacing, "header_size": body_off,
    })
    if lacing == "none":
        result["frames"] = [{"offset": body_off, "size": len(body)}]
        return result
    if not body:
        result["diagnostics"].append("lacing-corrupt: 缺少帧计数字节")
        return result
    nframes = body[0] + 1
    pos = 1
    sizes = []
    try:
        if lacing == "xiph":
            for _ in range(nframes - 1):
                acc = 0
                while True:
                    byte = body[pos]
                    pos += 1
                    acc += byte
                    if byte != 255:
                        break
                sizes.append(acc)
        elif lacing == "ebml":
            first, used = read_vint(body, pos)
            pos += used
            sizes.append(first)
            for _ in range(nframes - 2):
                diff, used = read_signed_vint(body, pos)
                pos += used
                sizes.append(sizes[-1] + diff)
        elif lacing == "fixed":
            remaining = len(body) - pos
            if remaining % nframes != 0:
                raise ValueError("fixed lacing 无法整除")
            sizes = [remaining // nframes] * (nframes - 1)
        remaining = len(body) - pos
        last = remaining - sum(sizes)
        if any(s < 0 for s in sizes) or last < 0:
            raise ValueError("lace 尺寸之和超过负载")
        sizes.append(last)
        frames = []
        off = pos
        for size in sizes:
            frames.append({"offset": body_off + off, "size": size})
            off += size
        result["frames"] = frames
    except (IndexError, ValueError, EbmlError) as exc:
        result["frames"] = None
        result["diagnostics"].append(f"lacing-corrupt: {exc}; 不猜测帧边界")
    return result
