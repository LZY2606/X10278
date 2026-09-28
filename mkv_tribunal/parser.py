"""EBML 递归下降解析器。

每个元素保留：id 偏移、长度 VINT、数据起止、原字节范围、子元素、
解码标量/二进制载荷。未知长度主元素的边界按 RFC 9559 规则确定：
父元素边界，或遇到一个不属于本主元素的合法子元素 ID 时终止。

解析器对任何异常只产出诊断并尽量停在声明边界上，绝不猜测帧/元素边界。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from . import vint
from .schema import GLOBAL, ElementSpec, spec_for


@dataclass
class Diagnostic:
    code: str
    message: str
    offset: int
    end: Optional[int] = None


@dataclass
class EBMLElement:
    element_id: int
    name: str
    type: str
    offset: int  # id 首字节
    id_width: int
    length_width: int
    data_offset: int
    declared_length: Optional[int]  # None 表示未知长度
    effective_end: int  # 实际消耗到的字节位置（未知长度时为推导边界）
    spec: Optional[ElementSpec]
    children: list["EBMLElement"] = field(default_factory=list)
    value: object = None  # 解码标量；binary 存 bytes
    unknown: bool = False  # 未登记 ID
    diagnostics: list[Diagnostic] = field(default_factory=list)

    @property
    def header_end(self) -> int:
        return self.data_offset

    @property
    def byte_range(self) -> tuple[int, int]:
        return self.offset, self.effective_end

    def child_by_name(self, name: str) -> Optional["EBMLElement"]:
        for child in self.children:
            if child.name == name:
                return child
        return None

    def children_by_name(self, name: str) -> list["EBMLElement"]:
        return [c for c in self.children if c.name == name]


def _decode_leaf(spec: Optional[ElementSpec], etype: str, raw: bytes,
                 diag: list[Diagnostic], offset: int) -> object:
    if etype == "binary":
        return raw
    if etype == "uint":
        return int.from_bytes(raw, "big", signed=False)
    if etype == "sint":
        return vint.decode_signed(int.from_bytes(raw, "big", signed=False),
                                  len(raw) * 8)
    if etype == "float":
        if len(raw) == 4:
            import struct
            return struct.unpack(">f", raw)[0]
        if len(raw) == 8:
            import struct
            return struct.unpack(">d", raw)[0]
        diag.append(Diagnostic(
            "BAD_FLOAT_WIDTH",
            "float 元素宽度为 %d 字节（仅支持 4/8），保留原值" % len(raw),
            offset, offset + len(raw)))
        return None
    if etype in ("string", "utf8"):
        return raw.decode("utf-8", errors="replace").rstrip("\x00")
    if etype == "date":
        return vint.decode_signed(int.from_bytes(raw, "big", signed=False),
                                  len(raw) * 8)
    diag.append(Diagnostic("UNKNOWN_TYPE", "未知元素类型 %r" % etype, offset))
    return raw


@dataclass
class _Scan:
    buf: bytes
    diagnostics: list[Diagnostic]


def _parse_one(scan: _Scan, pos: int, hard_end: int) -> EBMLElement:
    buf = scan.buf
    diag = scan.diagnostics
    try:
        element_id, id_width, _ = vint.decode(buf, pos, keep_marker=True)
    except vint.VintError as exc:
        diag.append(Diagnostic("BAD_ID_VINT", str(exc), pos, hard_end))
        # 无法恢复：消耗到硬边界，不再猜测
        raise _Stop(pos, hard_end)
    length_pos = pos + id_width
    try:
        length_value, length_width, length_mask = vint.decode(buf, length_pos)
    except vint.VintError as exc:
        diag.append(Diagnostic("BAD_SIZE_VINT", str(exc), length_pos, hard_end))
        raise _Stop(pos, hard_end)
    unknown_length = vint.is_unknown_length(length_value, length_width)
    data_offset = length_pos + length_width
    declared = None if unknown_length else length_value
    spec = spec_for(element_id)
    if spec is None:
        local = Diagnostic("UNKNOWN_ID", "未登记 EBML ID 0x%X" % element_id, pos)
        diag.append(local)
        el = EBMLElement(element_id, "Unknown", "binary", pos, id_width,
                         length_width, data_offset, declared, hard_end, None,
                         unknown=True)
        el.diagnostics.append(local)
        if unknown_length:
            el.declared_length = None
            el.effective_end = hard_end
        else:
            el.effective_end = min(hard_end, data_offset + declared)
            el.value = buf[data_offset:el.effective_end]
        return el

    el = EBMLElement(element_id, spec.name, spec.type, pos, id_width,
                     length_width, data_offset, declared, data_offset, spec)

    if unknown_length and spec.type != "master":
        diag.append(Diagnostic(
            "UNKNOWN_SIZE_LEAF",
            "非主元素 %s 使用未知长度，无法界定载荷" % spec.name,
            pos, hard_end))
        el.effective_end = hard_end
        return el

    if unknown_length:
        declared_end = hard_end
    else:
        declared_end = data_offset + declared
        if declared_end > hard_end:
            diag.append(Diagnostic(
                "LENGTH_OVERRUN",
                "%s 声明结束于 %d，越过父边界 %d"
                % (spec.name, declared_end, hard_end),
                data_offset, hard_end))
            declared_end = hard_end

    if spec.type == "master":
        _parse_children(scan, el, declared_end, unknown_length)
    else:
        raw = buf[data_offset:declared_end]
        el.value = _decode_leaf(spec, spec.type, raw, diag, data_offset)
        el.effective_end = declared_end
        if not unknown_length and declared_end < data_offset + (
                declared if declared is not None else 0):
            pass
    return el


class _Stop(Exception):
    def __init__(self, stop_pos: int, hard_end: int):
        self.stop_pos = stop_pos
        self.hard_end = hard_end


def _parse_children(scan: _Scan, parent: EBMLElement, hard_end: int,
                    unknown_length: bool) -> None:
    buf = scan.buf
    pos = parent.data_offset
    allowed = parent.spec.children if parent.spec is not None else frozenset()
    while pos < hard_end:
        try:
            child_id, idw, _ = vint.decode(buf, pos, keep_marker=True)
            # 长度也要可解码，否则同样不是合法边界候选
            vint.decode(buf, pos + idw)
        except vint.VintError:
            if unknown_length:
                break
            scan.diagnostics.append(Diagnostic(
                "CHILD_VINT_TRUNCATED",
                "%s 内偏移 %d 的子元素 VINT 损坏" % (parent.name, pos),
                pos, hard_end))
            pos = hard_end
            break
        if child_id not in allowed and not unknown_length:
            scan.diagnostics.append(Diagnostic(
                "ILLEGAL_CHILD",
                "%s 内出现不允许的 ID 0x%X" % (parent.name, child_id),
                pos, hard_end))
            pos = hard_end
            break
        if child_id not in allowed and unknown_length:
            # RFC 9559：未知大小主元素在遇到“不属于自己”的元素处终止，
            # 该元素留给上层解析，不被吞掉。
            break
        try:
            child = _parse_one(scan, pos, hard_end)
        except _Stop as stop:
            pos = hard_end if not unknown_length else stop.stop_pos
            if unknown_length:
                break
            continue
        parent.children.append(child)
        pos = child.effective_end
        if not unknown_length and pos > hard_end:
            pos = hard_end
    parent.effective_end = pos


@dataclass
class ParsedDocument:
    elements: list[EBMLElement]
    diagnostics: list[Diagnostic]


def parse_document(buf: bytes) -> ParsedDocument:
    """解析整个 Matroska/WebM 字节串。顶层顺序解析 EBML 头与 Segment 等。"""
    scan = _Scan(buf, [])
    elements: list[EBMLElement] = []
    pos = 0
    hard_end = len(buf)
    while pos < hard_end:
        try:
            el = _parse_one(scan, pos, hard_end)
        except _Stop as stop:
            pos = stop.hard_end
            continue
        elements.append(el)
        pos = el.effective_end
    return ParsedDocument(elements, scan.diagnostics)


def flatten(doc: ParsedDocument) -> list[EBMLElement]:
    out: list[EBMLElement] = []

    def walk(el: EBMLElement) -> None:
        out.append(el)
        for child in el.children:
            walk(child)

    for el in doc.elements:
        walk(el)
    return out
