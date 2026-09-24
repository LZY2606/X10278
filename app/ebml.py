"""EBML 读写层（Matroska 是 EBML 的一种 DocType）。

只实现 Matroska 审查所需的子集：
- VINT（含 unknown-size：长度位全 1 的 marker，读时按 None 保留，绝不猜测真实长度）；
- 元素树：偏移、ID、名、data 偏移、长度/未知长度、原始字节范围、递归子元素；
- leaf 值类型 u/i/f/s/8/d。

偏移与字节范围全部按"源字节绝对坐标"记录，规范化只发生在派生层。
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Optional

# Matroska 元素注册表：ID -> (名称, 类型)
# 类型: m=master, u=uint, i=int, f=float, s=string, 8=utf8, d=date, b=binary
ELEMENTS: dict[int, tuple[str, str]] = {
    0x1A45DFA3: ("EBML", "m"),
    0x4286: ("EBMLVersion", "u"),
    0x42F7: ("EBMLReadVersion", "u"),
    0x42F2: ("EBMLMaxIDLength", "u"),
    0x42F3: ("EBMLMaxSizeLength", "u"),
    0x4282: ("DocType", "s"),
    0x4287: ("DocTypeVersion", "u"),
    0x4285: ("DocTypeReadVersion", "u"),
    0x18538067: ("Segment", "m"),
    0x114D9B74: ("SeekHead", "m"),
    0x4DBB: ("Seek", "m"),
    0x53AB: ("SeekID", "b"),
    0x53AC: ("SeekPosition", "u"),
    0x1549A966: ("Info", "m"),
    0x2AD7B1: ("TimecodeScale", "u"),
    0x4489: ("Duration", "f"),
    0x4D80: ("MuxingApp", "8"),
    0x5741: ("WritingApp", "8"),
    0x1654AE6B: ("Tracks", "m"),
    0xAE: ("TrackEntry", "m"),
    0xD7: ("TrackNumber", "u"),
    0x73C5: ("TrackUID", "u"),
    0x83: ("TrackType", "u"),
    0x86: ("CodecID", "s"),
    0x23E383: ("DefaultDuration", "u"),
    0x23314F: ("TrackTimecodeScale", "f"),
    0x1C: ("FlagLacing", "u"),
    0x9C: ("FlagDefault", "u"),
    0x536E: ("Name", "8"),
    0xE0: ("Video", "m"),
    0xB0: ("PixelWidth", "u"),
    0xBA: ("PixelHeight", "u"),
    0xE1: ("Audio", "m"),
    0xB5: ("SamplingFrequency", "f"),
    0x1F43B675: ("Cluster", "m"),
    0xE7: ("Timecode", "u"),
    0xA3: ("SimpleBlock", "b"),
    0xA0: ("BlockGroup", "m"),
    0xA1: ("Block", "b"),
    0x9B: ("BlockDuration", "u"),
    0x75A2: ("DiscardPadding", "i"),
    0xFB: ("ReferenceBlock", "i"),
    0x1C53BB6B: ("Cues", "m"),
    0xBB: ("CuePoint", "m"),
    0xB3: ("CueTime", "u"),
    0xB7: ("CueTrackPositions", "m"),
    0xF7: ("CueTrack", "u"),
    0xF1: ("CueClusterPosition", "u"),
    0xF0: ("CueRelativePosition", "u"),
    0x5378: ("CueDuration", "u"),
}

MASTER_IDS = {eid for eid, (_n, t) in ELEMENTS.items() if t == "m"}

# VINT marker 表：字节长度 -> marker 位
_MARKER = {1: 0x80, 2: 0x4000, 3: 0x200000, 4: 0x10000000,
           5: 0x0800000000, 6: 0x040000000000, 7: 0x02000000000000,
           8: 0x0100000000000000}
_ALL_ONES = {k: (1 << (7 * k)) - 1 for k in range(1, 9)}


class EbmlError(ValueError):
    pass


@dataclass
class Vint:
    value: int
    raw_len: int
    all_ones: bool  # True => unknown-length marker


def read_vint(buf: bytes, pos: int, *, is_id: bool = False) -> Vint:
    if pos >= len(buf):
        raise EbmlError(f"VINT 越界 offset={pos}")
    first = buf[pos]
    if first == 0:
        raise EbmlError(f"VINT 首字节为 0（非法）offset={pos}")
    raw_len = 1
    marker = 0x80
    while first & marker == 0:
        marker >>= 1
        raw_len += 1
        if raw_len > 8:
            raise EbmlError(f"VINT 超过 8 字节 offset={pos}")
    if pos + raw_len > len(buf):
        raise EbmlError(f"VINT 数据截断 offset={pos} raw_len={raw_len}")
    val = first & (0xFF >> raw_len)
    for i in range(1, raw_len):
        val = (val << 8) | buf[pos + i]
    all_ones = (not is_id) and val == _ALL_ONES[raw_len]
    return Vint(value=val, raw_len=raw_len, all_ones=all_ones)


def enc_vint(value: int, raw_len: Optional[int] = None, unknown: bool = False) -> bytes:
    if unknown:
        if raw_len is None:
            raw_len = 4
        return bytes([0xFF if raw_len == 1 else (0x100 >> raw_len)]) + b"\xFF" * (raw_len - 1)
    if value < 0:
        raise EbmlError("VINT 不能为负")
    if raw_len is None:
        for length in range(1, 9):
            if value <= _ALL_ONES[length]:
                raw_len = length
                break
        else:
            raise EbmlError("VINT 值过大")
    elif value > _ALL_ONES[raw_len]:
        raise EbmlError(f"VINT 值 {value} 超过 {raw_len} 字节容量")
    marker = _MARKER[raw_len]
    total = marker | value
    return total.to_bytes(raw_len, "big")


def enc_id(element_id: int) -> bytes:
    length = (element_id.bit_length() + 7) // 8
    return element_id.to_bytes(length, "big")


def _decode_leaf(etype: str, data: bytes):
    if etype == "b":
        return {"hex": data.hex(), "size": len(data)}
    if etype in ("u",):
        return int.from_bytes(data, "big", signed=False) if data else 0
    if etype in ("i",):
        return int.from_bytes(data, "big", signed=True) if data else 0
    if etype == "f":
        if len(data) == 4:
            return struct.unpack(">f", data)[0]
        if len(data) == 8:
            return struct.unpack(">d", data)[0]
        if len(data) == 0:
            return None
        raise EbmlError(f"float 长度非法 {len(data)}")
    if etype == "d":
        if len(data) != 8:
            raise EbmlError("date 长度非 8")
        return struct.unpack(">q", data)[0]
    if etype == "s":
        return data.decode("ascii", errors="replace")
    if etype == "8":
        return data.decode("utf-8", errors="replace")
    return data.hex()


def _enc_leaf(etype: str, value) -> bytes:
    if etype == "b":
        return bytes(value) if not isinstance(value, (bytes, bytearray)) else bytes(value)
    if etype == "u":
        value = int(value)
        if value == 0:
            return b""
        return value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big")
    if etype == "i":
        value = int(value)
        length = max(1, (abs(value).bit_length() + 8) // 8)
        return value.to_bytes(length, "big", signed=True)
    if etype == "f":
        return struct.pack(">d", float(value))
    if etype in ("s", "8"):
        return str(value).encode("utf-8")
    if etype == "d":
        return struct.pack(">q", int(value))
    raise EbmlError(f"不支持编码类型 {etype}")


def element(element_id: int, payload: bytes = b"", *, unknown_length: bool = False) -> bytes:
    """构造一个 EBML 元素（master/leaf 的 payload 均已序列化）。"""
    if unknown_length:
        return enc_id(element_id) + enc_vint(0, unknown=True, raw_len=4) + payload
    return enc_id(element_id) + enc_vint(len(payload)) + payload


def leaf(element_id: int, value, etype: str | None = None) -> bytes:
    etype = etype or ELEMENTS[element_id][1]
    return element(element_id, _enc_leaf(etype, value))


@dataclass
class Node:
    offset: int
    id: int
    name: str
    etype: str
    size: Optional[int]            # None 表示 unknown-length marker
    data_offset: int
    end: Optional[int]             # 推断的元素末坐标（不含）；unknown master 在 EOF 时仍可推断
    unknown_length: bool
    value: object = None
    children: list["Node"] = field(default_factory=list)
    error: Optional[str] = None

    def to_dict(self) -> dict:
        d = {
            "offset": self.offset,
            "id_hex": f"0x{self.id:X}",
            "name": self.name,
            "type": self.etype,
            "data_offset": self.data_offset,
            "size": self.size,
            "unknown_length": self.unknown_length,
            "raw_range": [self.offset, self.end],
        }
        if self.error:
            d["error"] = self.error
        if self.etype != "m":
            d["value"] = self.value
        if self.children:
            d["children"] = [c.to_dict() for c in self.children]
        return d


def parse(buf: bytes, *, start: int = 0, end: Optional[int] = None, depth: int = 0) -> list[Node]:
    """递归解析 EBML 元素树。

    start/end 为源字节绝对坐标区间 [start, end)；end=None 表示到 EOF。
    unknown-size master 的边界由父元素边界（或 EOF）界定，并完整保留。
    """
    hard_end = len(buf) if end is None else end
    pos = start
    nodes: list[Node] = []
    while pos < hard_end:
        head = pos
        try:
            idv = read_vint(buf, pos, is_id=True)
            pos += idv.raw_len
            sizev = read_vint(buf, pos)
            pos += sizev.raw_len
            unknown = sizev.all_ones
            size = None if unknown else sizev.value
        except EbmlError as exc:
            nodes.append(Node(head, 0, "PARSE_ERROR", "b", None, pos, hard_end,
                              False, error=str(exc)))
            break
        data_offset = pos
        if not unknown:
            data_end = data_offset + size
            if data_end > hard_end:
                nodes.append(Node(head, idv.value, _name(idv.value)[0],
                                  _name(idv.value)[1], size, data_offset, hard_end,
                                  False, error=f"声明长度超出父级边界 end={data_end}"))
                break
        else:
            data_end = None
        name, etype = _name(idv.value)
        node = Node(head, idv.value, name, etype, size, data_offset,
                    None, unknown)
        if etype == "m":
            child_end = data_end if data_end is not None else hard_end
            try:
                node.children = parse(buf, start=data_offset, end=child_end, depth=depth + 1)
                node.end = data_end if data_end is not None else hard_end
            except EbmlError as exc:
                node.error = f"master 解析失败: {exc}"
                node.end = data_end if data_end is not None else hard_end
        else:
            raw = buf[data_offset:data_end] if data_end is not None else b""
            try:
                node.value = _decode_leaf(etype, raw)
            except EbmlError as exc:
                node.error = str(exc)
            node.end = data_end
        nodes.append(node)
        if unknown:
            # unknown-size 元素只能位于其父级末尾
            break
        pos = data_end
    return nodes


def _name(element_id: int) -> tuple[str, str]:
    return ELEMENTS.get(element_id, (f"Unknown_0x{element_id:X}", "b"))


def flatten(nodes: list[Node], depth: int = 0):
    for n in nodes:
        yield depth, n
        yield from flatten(n.children, depth + 1)
