"""EBML/Matroska 原始解析层。

只负责把字节流整理成带稳定身份的元素树，保留：
- 每个元素的偏移 / 头长 / 数据长度（含未知长度）
- 原始字节范围（raw_range）
- 解析诊断（不猜测、不吞错）

所有派生解释（时码换算、帧边界、cue 校验）在 derive.py 中完成。
"""
from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass, field

# ---------------------------------------------------------------- 元素表
# name, type: master/uint/int/string/binary/float
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
    0x1654AE6B: ("Tracks", "master"),
    0xAE: ("TrackEntry", "master"),
    0xD7: ("TrackNumber", "uint"),
    0x73C5: ("TrackUID", "uint"),
    0x83: ("TrackType", "uint"),
    0x9C: ("FlagLacing", "uint"),
    0x536E: ("Name", "string"),
    0x86: ("CodecID", "string"),
    0x23314F: ("TrackTimecodeScale", "float"),
    0x1F43B675: ("Cluster", "master"),
    0xE7: ("Timestamp", "uint"),
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
    0xBF: ("CRC-32", "binary"),
}

TRACK_TYPE = {1: "video", 2: "audio", 3: "complex", 17: "subtitle"}

UNKNOWN_SIZE = "unknown"


class EBMLFormatError(Exception):
    pass


def read_vint(data: bytes, pos: int, for_id: bool = False):
    """读取 VINT。返回 (value, length, raw_bytes)。

    for_id=True 时保留长度标记位（元素 ID）；否则剥离标记位。
    数据位全 1 表示未知长度，value 返回 UNKNOWN_SIZE。
    """
    if pos >= len(data):
        raise EBMLFormatError(f"vint 越界: pos={pos}")
    first = data[pos]
    if first == 0:
        raise EBMLFormatError(f"非法 VINT 首字节 0x00 @ {pos}")
    mask = 0x80
    length = 1
    while length <= 8 and not (first & mask):
        mask >>= 1
        length += 1
    if length > 8:
        raise EBMLFormatError(f"VINT 长度超限 @ {pos}")
    if pos + length > len(data):
        raise EBMLFormatError(f"VINT 截断 @ {pos}")
    raw = data[pos:pos + length]
    if for_id:
        value = int.from_bytes(raw, "big")
    else:
        value = first & (mask - 1)
        for b in raw[1:]:
            value = (value << 8) | b
        if value == (1 << (7 * length)) - 1:
            value = UNKNOWN_SIZE
    return value, length, raw


def _decode_value(etype: str, payload: bytes):
    if etype == "uint":
        return int.from_bytes(payload, "big") if payload else 0
    if etype == "int":
        return int.from_bytes(payload, "big", signed=True) if payload else 0
    if etype == "string":
        return payload.decode("utf-8", "replace").rstrip("\x00")
    if etype == "float":
        if len(payload) == 4:
            return struct.unpack(">f", payload)[0]
        if len(payload) == 8:
            return struct.unpack(">d", payload)[0]
        return None
    return None  # master/binary 不内联值


@dataclass
class Element:
    uid: str
    eid: int
    name: str
    etype: str
    offset: int
    header_size: int
    size: object          # int 或 UNKNOWN_SIZE
    data_offset: int
    end: object           # int 或 None（未知）
    depth: int
    value: object = None
    raw_range: list = field(default_factory=list)   # [offset, end_or_None]
    children: list = field(default_factory=list)
    diagnostics: list = field(default_factory=list)

    def to_dict(self):
        return {
            "uid": self.uid, "id": hex(self.eid), "name": self.name,
            "type": self.etype, "offset": self.offset,
            "header_size": self.header_size,
            "size": self.size, "data_offset": self.data_offset,
            "end": self.end, "depth": self.depth, "value": self.value,
            "raw_range": self.raw_range, "diagnostics": self.diagnostics,
            "children": [c.to_dict() for c in self.children],
        }


def _elem_uid(offset: int, eid: int) -> str:
    return "el-" + hashlib.sha1(f"{offset}:{eid:x}".encode()).hexdigest()[:12]


def parse_elements(data: bytes, start: int, end: int, depth: int,
                   diagnostics: list, max_depth: int = 8) -> list[Element]:
    """解析 [start, end) 区间内的兄弟元素序列。

    遇到无法对齐的字节（损坏区间）时停止并记录 GAP 诊断，
    绝不跳过猜测中的元素头。
    """
    out = []
    pos = start
    while pos < end:
        elem_start = pos
        try:
            eid, id_len, _ = read_vint(data, pos, for_id=True)
        except EBMLFormatError as exc:
            diagnostics.append({
                "code": "UNPARSEABLE_REGION", "offset": pos,
                "length": end - pos, "detail": str(exc),
            })
            break
        pos += id_len
        try:
            size, size_len, _ = read_vint(data, pos)
        except EBMLFormatError as exc:
            diagnostics.append({
                "code": "UNPARSEABLE_REGION", "offset": elem_start,
                "length": end - elem_start, "detail": str(exc),
            })
            break
        pos += size_len
        name, etype = ELEMENTS.get(eid, (f"Unknown-{eid:x}", "binary"))
        if size is UNKNOWN_SIZE:
            data_end = None
            eff_end = end  # 未知长度：延伸至父边界，由上层裁定
        else:
            data_end = pos + size
            eff_end = min(data_end, end)
        el = Element(
            uid=_elem_uid(elem_start, eid), eid=eid, name=name, etype=etype,
            offset=elem_start, header_size=id_len + size_len, size=size,
            data_offset=pos, end=data_end, depth=depth,
            raw_range=[elem_start, data_end],
        )
        if size is UNKNOWN_SIZE:
            el.diagnostics.append({
                "code": "UNKNOWN_SIZE",
                "detail": f"{name} 使用未知长度，范围延伸至父元素边界",
            })
        if data_end is not None and data_end > end:
            el.diagnostics.append({
                "code": "SIZE_OVERRUNS_PARENT",
                "detail": f"声明长度超出父边界 {end}",
            })
        if etype == "master":
            sub_end = eff_end
            el.children = parse_elements(data, pos, sub_end, depth + 1,
                                         el.diagnostics, max_depth)
        else:
            payload = data[pos:eff_end]
            if etype != "binary":
                el.value = _decode_value(etype, payload)
        out.append(el)
        # 未知长度 master 消耗到父边界为止
        pos = eff_end if data_end is None else data_end
        if data_end is None:
            break
    return out


def parse_document(data: bytes) -> dict:
    """解析完整文件，返回 {'elements': [...], 'diagnostics': [...], 'length': n}。"""
    diags: list = []
    roots = parse_elements(data, 0, len(data), 0, diags)
    return {
        "length": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "elements": [e.to_dict() for e in roots],
        "diagnostics": diags,
    }


def iter_elements(nodes: list[dict]):
    for n in nodes:
        yield n
        yield from iter_elements(n["children"])


# ---------------------------------------------------------------- Block 解析
LACING_MODES = {0: "none", 1: "xiph", 2: "fixed", 3: "ebml"}


def parse_block(payload: bytes) -> dict:
    """解析 SimpleBlock/Block 载荷。

    规则（领域裁定，机器可读）：
    - relative timecode 一律按有符号 int16 大端解释；
    - lacing 损坏时不猜帧边界，frames=None 并附诊断。
    """
    diags: list = []
    if len(payload) < 4:
        return {"ok": False, "diagnostics": [
            {"code": "BLOCK_TRUNCATED", "detail": "块载荷不足 4 字节"}]}
    track, tlen, _ = read_vint(payload, 0)
    rel = int.from_bytes(payload[tlen:tlen + 2], "big", signed=True)  # 有符号
    flags = payload[tlen + 2]
    lacing = LACING_MODES[(flags >> 1) & 0x3]
    body = payload[tlen + 3:]
    frames = None
    if lacing == "none":
        frames = [{"offset": 0, "size": len(body)}]
    elif lacing == "fixed":
        if not body:
            diags.append({"code": "LACING_CORRUPT",
                          "detail": "fixed lacing 缺少帧数字节"})
        else:
            count = body[0] + 1
            rest = len(body) - 1
            if count <= 0 or rest % count != 0:
                diags.append({"code": "LACING_CORRUPT",
                              "detail": f"fixed lacing 余量 {rest} 不能被 {count} 整除"})
            else:
                sz = rest // count
                frames = [{"offset": 1 + i * sz, "size": sz} for i in range(count)]
    elif lacing == "xiph":
        if not body:
            diags.append({"code": "LACING_CORRUPT", "detail": "xiph lacing 缺少帧数字节"})
        else:
            count = body[0] + 1
            pos = 1
            sizes = []
            corrupt = False
            for _ in range(count - 1):
                acc = 0
                while True:
                    if pos >= len(body):
                        corrupt = True
                        break
                    b = body[pos]
                    pos += 1
                    acc += b
                    if b != 255:
                        break
                if corrupt:
                    break
                sizes.append(acc)
            if not corrupt and sum(sizes) > len(body) - pos:
                corrupt = True
            if corrupt:
                diags.append({"code": "LACING_CORRUPT",
                              "detail": "xiph lace 声明尺寸超出载荷，不猜帧边界"})
            else:
                off = pos
                for sz in sizes:
                    frames = frames or []
                    frames.append({"offset": off, "size": sz})
                    off += sz
                frames = frames or []
                frames.append({"offset": off, "size": len(body) - off})
    elif lacing == "ebml":
        if not body:
            diags.append({"code": "LACING_CORRUPT", "detail": "ebml lacing 缺少帧数字节"})
        else:
            count = body[0] + 1
            pos = 1
            try:
                first, n1, _ = read_vint(body, pos)
                pos += n1
                if first is UNKNOWN_SIZE:
                    raise EBMLFormatError("首帧尺寸为未知长度")
                sizes = [first]
                prev = first
                for _ in range(count - 2):
                    diff, nl, _ = read_vint(body, pos)
                    pos += nl
                    bias = (1 << (7 * nl - 1)) - 1
                    prev = prev + (diff - bias)
                    if prev < 0:
                        raise EBMLFormatError("帧尺寸为负")
                    sizes.append(prev)
                if sum(sizes) > len(body) - pos:
                    raise EBMLFormatError("尺寸总和超出载荷")
                off = pos
                frames = []
                for sz in sizes:
                    frames.append({"offset": off, "size": sz})
                    off += sz
                frames.append({"offset": off, "size": len(body) - off})
            except EBMLFormatError as exc:
                diags.append({"code": "LACING_CORRUPT",
                              "detail": f"ebml lacing 损坏: {exc}，不猜帧边界"})
                frames = None
    return {
        "ok": not diags,
        "track": track,
        "relative_timecode": rel,          # 有符号 int16
        "relative_timecode_raw": rel & 0xFFFF,
        "flags": flags,
        "lacing": lacing,
        "frames": frames,                  # None = 边界未知
        "body_size": len(body),
        "diagnostics": diags,
    }
