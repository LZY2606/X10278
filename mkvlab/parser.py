"""EBML 解析器：保留每个元素的偏移、原始字节范围、未知长度与值的原始表示。

语义纪律：
- relative timecode 不在此层解释为有符号（结构层只给原始字节），解释在 engine。
- lacing 在此层做结构性切分；损坏时不猜帧边界，返回诊断与已知安全前缀。
- 任何解析异常都不丢原始字节范围。
"""
from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

from . import ids


class EBMLParseError(Exception):
    pass


@dataclass
class VInt:
    value: Optional[int]
    width: int
    unknown: bool = False


def read_vint(buf: bytes, pos: int) -> VInt:
    if pos >= len(buf):
        raise EBMLParseError(f"VINT 越界 @{pos}")
    first = buf[pos]
    if first == 0:
        raise EBMLParseError(f"VINT 首字节为 0x00 @{pos}")
    width = (lambda f: next((w for w in range(1, 9) if f & (0x80 >> (w - 1))), 8))(first)
    if pos + width > len(buf):
        raise EBMLParseError(f"VINT 被截断 @{pos} width={width}")
    keep = first & ((1 << (8 - width)) - 1)
    value = keep
    unknown = keep == (1 << (8 - width)) - 1
    for b in buf[pos + 1: pos + width]:
        value = (value << 8) | b
        unknown = unknown and b == 0xFF
    return VInt(None if unknown else value, width, unknown)


def read_id(buf: bytes, pos: int) -> tuple[int, int]:
    v = read_vint(buf, pos)
    # ID 的数值即原始字节（含标记位），直接读
    width = v.width
    eid = int.from_bytes(buf[pos: pos + width], "big")
    return eid, width


@dataclass
class Element:
    id: int
    name: str
    id_offset: int          # 文件绝对偏移（ID 起点）
    size_offset: int
    payload_offset: int
    size: Optional[int]     # None=未知长度
    unknown_size: bool
    header_len: int
    payload: bytes = b""
    value: Any = None
    value_type: str = ids.T_BINARY
    children: list["Element"] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)
    end_offset: Optional[int] = None  # 已知长度=payload 结尾；未知=扫描出的边界或 None

    @property
    def raw_range(self) -> tuple[int, int]:
        if self.end_offset is not None:
            return (self.id_offset, self.end_offset)
        return (self.id_offset, self.payload_offset + len(self.payload))

    def to_dict(self) -> dict:
        a, b = self.raw_range
        return {
            "id": f"0x{self.id:X}",
            "name": self.name,
            "type": self.value_type,
            "id_offset": self.id_offset,
            "size_offset": self.size_offset,
            "payload_offset": self.payload_offset,
            "size": self.size,
            "unknown_size": self.unknown_size,
            "raw_start": a,
            "raw_end": b,
            "value": _json_value(self.value),
            "diagnostics": list(self.diagnostics),
            "children": [c.to_dict() for c in self.children],
        }


def _json_value(v: Any) -> Any:
    if isinstance(v, (bytes, bytearray)):
        return {"hex": v.hex(), "len": len(v)}
    if isinstance(v, dict):
        return {k: _json_value(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_json_value(x) for x in v]
    return v


def _decode_scalar(eid: int, payload: bytes, vtype: str,
                   diagnostics: list[str]) -> Any:
    try:
        if vtype == ids.T_UINT:
            return int.from_bytes(payload, "big") if payload else 0
        if vtype == ids.T_SINT:
            return int.from_bytes(payload, "big", signed=True) if payload else 0
        if vtype == ids.T_FLOAT:
            if len(payload) == 4:
                return struct.unpack(">f", payload)[0]
            if len(payload) == 8:
                return struct.unpack(">d", payload)[0]
            diagnostics.append(f"BAD_FLOAT_WIDTH:浮点宽度 {len(payload)} 非法")
            return None
        if vtype in (ids.T_STRING, ids.T_UTF8):
            return payload.split(b"\x00")[0].decode("utf-8", errors="replace")
        if vtype == ids.T_DATE:
            if len(payload) != 8:
                diagnostics.append(f"BAD_DATE_WIDTH:日期宽度 {len(payload)}")
                return None
            return struct.unpack(">q", payload)[0]
        return payload
    except Exception as exc:  # 保留原始字节
        diagnostics.append(f"DECODE_FAILED:{exc}")
        return payload


def parse_tree(blob: bytes, start: int = 0, end: Optional[int] = None,
               parent_id: Optional[int] = None, depth: int = 0,
               max_depth: int = 32) -> list[Element]:
    """递归解析；未知长度按已知子元素边界扫描。"""
    if end is None:
        end = len(blob)
    out: list[Element] = []
    pos = start
    while pos < end:
        try:
            eid, idw = read_id(blob, pos)
        except EBMLParseError as exc:
            out.append(_error_element(blob, pos, end, f"BAD_ID:{exc}"))
            break
        size_pos = pos + idw
        try:
            sv = read_vint(blob, size_pos)
        except EBMLParseError as exc:
            out.append(_error_element(blob, pos, min(pos + idw, end),
                                      f"BAD_SIZE:{exc}"))
            break
        payload_pos = size_pos + sv.width
        name = ids.elem_name(eid)
        vtype = ids.value_type(eid)
        el = Element(id=eid, name=name, id_offset=pos, size_offset=size_pos,
                     payload_offset=payload_pos, size=sv.value,
                     unknown_size=sv.unknown, header_len=idw + sv.width,
                     value_type=vtype)
        if not sv.unknown:
            p_end = payload_pos + (sv.value or 0)
            if p_end > end:
                el.diagnostics.append(
                    f"LENGTH_OVERRUN:声明长度超出父容器 ({p_end}>{end})")
                p_end = end
            _fill_payload(blob, el, payload_pos, p_end, depth, max_depth)
            el.end_offset = payload_pos + (sv.value or 0)
            if el.end_offset > end:
                el.end_offset = end
            pos = el.end_offset
        else:
            # 未知长度：根据父容器允许的子元素扫描边界
            scan_end = _scan_unknown_end(blob, payload_pos, end, parent_id,
                                         depth + 1, max_depth)
            _fill_payload(blob, el, payload_pos, scan_end, depth, max_depth,
                          unknown=True, parent_id_for_children=eid)
            el.end_offset = scan_end
            pos = scan_end
        out.append(el)
    return out


def _fill_payload(blob: bytes, el: Element, p_start: int, p_end: int,
                  depth: int, max_depth: int, unknown: bool = False,
                  parent_id_for_children=None) -> None:
    el.payload = blob[p_start:p_end]
    vt = el.value_type
    if vt == ids.T_MASTER:
        if depth >= max_depth:
            el.diagnostics.append("MAX_DEPTH:达到最大递归深度，停止下钻")
            return
        if unknown:
            el.children = parse_tree(blob, p_start, p_end,
                                     parent_id_for_children or el.id,
                                     depth + 1, max_depth)
        else:
            # 已知长度 master：段等大容器按其子集解析
            el.children = parse_tree(blob, p_start, p_end, el.id,
                                     depth + 1, max_depth)
    elif vt == ids.T_BLOCK:
        el.value = {"hex": el.payload.hex(), "len": len(el.payload)}
    else:
        el.value = _decode_scalar(el.id, el.payload, vt, el.diagnostics)


def _scan_unknown_end(blob: bytes, start: int, hard_end: int,
                      parent_id: Optional[int], depth: int,
                      max_depth: int) -> int:
    """未知长度 master 的边界：解析其后跟的、对父级合法的兄弟元素。

    Segment 未知长度时扫描所有 Level1；Cluster 未知长度时扫描到下一个
    Cluster/顶级元素或字节末尾。返回扫描到的安全边界。
    """
    pos = start
    while pos < hard_end:
        try:
            eid, idw = read_id(blob, pos)
            sv = read_vint(blob, pos + idw)
        except EBMLParseError:
            return pos if pos > start else hard_end
        if not _legal_sibling(eid, parent_id):
            return pos
        if sv.unknown:
            # 嵌套未知：递归扫描
            nxt = _scan_unknown_end(blob, pos + idw + sv.width, hard_end,
                                    eid, depth + 1, max_depth)
            pos = nxt
        else:
            pos = pos + idw + sv.width + (sv.value or 0)
            if pos > hard_end:
                return hard_end
    return pos


def _legal_sibling(eid: int, parent_id: Optional[int]) -> bool:
    if parent_id is None:
        return True
    allowed = ids.known_children(parent_id)
    if not allowed:
        # 未知父节点：只要看起来像已知 master/标量即继续
        return True
    return eid in allowed or eid in (ids.CRC32, ids.Void)


def _error_element(blob: bytes, start: int, end: int, diag: str) -> Element:
    el = Element(id=-1, name="CORRUPT", id_offset=start, size_offset=start,
                 payload_offset=start, size=max(0, end - start),
                 unknown_size=False, header_len=0,
                 payload=blob[start:end], value_type=ids.T_BINARY)
    el.end_offset = end
    el.diagnostics.append(diag)
    return el


def parse_document(blob: bytes) -> dict:
    """解析整份文档：返回 EBML 头、Segment、平铺元素索引与摘要。"""
    sha = hashlib.sha256(blob).hexdigest()
    roots = parse_tree(blob, 0, len(blob), None, 0)
    segment = _first(roots, ids.Segment)
    return {
        "sha256": sha,
        "length": len(blob),
        "roots": roots,
        "segment": segment,
    }


def _first(elems, eid):
    for e in elems:
        if e.id == eid:
            return e
        found = _first(e.children, eid)
        if found:
            return found
    return None


def find_all(elems, eid, out=None) -> list[Element]:
    if out is None:
        out = []
    for e in elems:
        if e.id == eid:
            out.append(e)
        find_all(e.children, eid, out)
    return out


def segment_data_start(segment: Element) -> int:
    return segment.payload_offset
