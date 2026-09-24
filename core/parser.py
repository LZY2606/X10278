"""把 Matroska 字节流解析为带偏移事实的 EBML 元素树。

保留的事实（每个元素）：
- element_id 编码起点 id_start / 数据起点 data_start / 原始字节终点 raw_end
- 是否未知长度 unknown_size；未知长度时 raw_end 为"可证明的边界"（下一个
  Level-1 元素起点或 EOF），而不是凭空推算的长度
- value_raw：原始字节表示（时间量先不规范化，只带规范单位）
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional

from . import ebml

# Level-1 元素：未知长度 Segment/Cluster 的可证明边界（Void/CRC 不算，
# 它们可合法出现在 Cluster 内部——这是 Matroska 编解码实践中的裁定，
# 会在 domain 诊断中注明）。
LEVEL1_IDS = {
    0x114D9B74,  # SeekHead
    0x1549A966,  # Info
    0x1654AE6B,  # Tracks
    0x1F43B675,  # Cluster
    0x1C53BB6B,  # Cues
    0x1941A469,  # Attachments
    0x1043A770,  # Chapters
    0x1254C367,  # Tags
}


@dataclass
class ParseDiag:
    code: str
    offset: int
    message: str
    severity: str = "error"        # error / warn / info
    span_end: Optional[int] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class EbmlElement:
    key: str                       # 文件内稳定身份（content/offset 派生）
    id: int
    name: str
    etype: str
    unit: Optional[str]
    id_start: int                  # 元素 ID 首字节（文件绝对偏移）
    data_start: int                # 数据载荷首字节
    header_end: int                # = data_start
    raw_end: int                   # 元素原始字节范围终点（不含）
    declared_size: Optional[int]   # size VINT 解码值；未知长度为 None
    unknown_size: bool
    depth: int
    parent_key: Optional[str]
    value: object = None           # 类型化值（uint/sint/float/str）
    value_raw_hex: Optional[str] = None
    children: list[str] = field(default_factory=list)
    inferred_end: bool = False     # raw_end 来自边界推断（未知长度）

    def to_dict(self) -> dict:
        return asdict(self)


def _el_key(file_key: str, id_start: int, eid: int, declared_size, end: int) -> str:
    import hashlib
    h = hashlib.sha1()
    h.update(file_key.encode())
    h.update(id_start.to_bytes(8, "big"))
    h.update(eid.to_bytes(max(4, (eid.bit_length()+7)//8), "big"))
    h.update((declared_size if declared_size is not None else 0xFFFFFFFF).to_bytes(8, "big"))
    h.update(end.to_bytes(8, "big"))
    return "el_" + h.hexdigest()[:16]


def parse_file(data: bytes, file_key: str) -> tuple[list[EbmlElement], list[ParseDiag]]:
    elements: list[EbmlElement] = []
    diags: list[ParseDiag] = []
    _parse_range(data, 0, len(data), depth=0, parent_key=None, file_key=file_key,
                 elements=elements, diags=diags, top_level=True)
    return elements, diags


def _parse_range(data: bytes, start: int, end: int, *, depth: int,
                 parent_key, file_key, elements, diags, top_level=False,
                 boundary_ids: Optional[set[int]] = None) -> Optional[int]:
    """解析 [start, end) 内的兄弟元素。

    boundary_ids 非空时（未知长度 Segment/Cluster 内部），一旦遇到边界
    元素，返回它的起点（不消费）；否则消费到 end。
    """
    pos = start
    while pos < end:
        try:
            eid_v = ebml.read_id(data, pos)
        except ebml.EbmlError as exc:
            diags.append(ParseDiag("ID_TRUNCATED", pos, str(exc), "error",
                                   min(pos + 1, end)))
            return None
        eid = eid_v.value
        size_pos = pos + eid_v.length
        try:
            size_v = ebml.read_vint(data, size_pos)
        except ebml.EbmlError as exc:
            diags.append(ParseDiag("SIZE_TRUNCATED", pos, str(exc), "error",
                                   min(size_pos + 1, end)))
            return None
        unknown = ebml.is_unknown_size(size_v)
        data_start = size_pos + size_v.length
        declared = None if unknown else size_v.value
        if not unknown and data_start + declared > end:
            diags.append(ParseDiag(
                "ELEMENT_OVERRUN", pos,
                f"{ebml.spec_for(eid)[0]}@{pos} 声明 {declared} 字节越过父"
                f"范围终点 {end}",
                "error", min(data_start + declared, end)))
            declared = max(0, end - data_start)

        name, etype, unit = ebml.spec_for(eid)
        sub_boundary = (LEVEL1_IDS if unknown
                        and eid in (0x18538067, 0x1F43B675) else None)
        if unknown:
            scan_end = end
            if sub_boundary is not None:
                scan_end = _find_boundary(data, data_start, end,
                                          sub_boundary)
            raw_end = scan_end
            inferred = True
            if sub_boundary is None and etype == ebml.T_MASTER:
                diags.append(ParseDiag(
                    "UNKNOWN_SIZE", pos,
                    f"{name}@{pos} 使用未知长度，终点 {end} 由父容器推断",
                    "info", end))
            elif sub_boundary is not None and raw_end >= end:
                diags.append(ParseDiag(
                    "UNKNOWN_SIZE_END", pos,
                    f"{name}@{pos} 使用未知长度且扫描至 {end} 才结束，"
                    "终点由 EOF 推断（保留为推断边界，非声明长度）",
                    "warn", end))
            elif sub_boundary is not None:
                diags.append(ParseDiag(
                    "UNKNOWN_SIZE", pos,
                    f"{name}@{pos} 使用未知长度，终点 {raw_end} "
                    "由下一个 Level-1 边界推断",
                    "info", raw_end))
        else:
            raw_end = data_start + declared
            inferred = False

        key = _el_key(file_key, pos, eid, declared, raw_end)
        el = EbmlElement(
            key=key, id=eid, name=name, etype=etype, unit=unit,
            id_start=pos, data_start=data_start, header_end=data_start,
            raw_end=raw_end, declared_size=declared, unknown_size=unknown,
            depth=depth, parent_key=parent_key, inferred_end=inferred,
        )
        elements.append(el)
        if parent_key is not None:
            for pp in elements:
                if pp.key == parent_key:
                    pp.children.append(key)
                    break

        if etype == ebml.T_MASTER:
            if unknown and sub_boundary is not None:
                _scan_unknown_container(data, data_start, el, depth, key,
                                        file_key, elements, diags,
                                        sub_boundary)
                pos = el.raw_end
            else:
                _parse_range(data, data_start, raw_end, depth=depth + 1,
                             parent_key=key, file_key=file_key,
                             elements=elements, diags=diags)
                pos = el.raw_end
        else:
            raw = data[data_start:raw_end]
            el.value_raw_hex = raw.hex()
            try:
                el.value = ebml.decode_value(eid, raw)
            except ebml.EbmlError as exc:
                diags.append(ParseDiag("BAD_VALUE", pos,
                                       f"{name}@{pos}: {exc}", "error",
                                       raw_end))
                el.value = None
            pos = raw_end
    return None


def _scan_unknown_container(data, data_start, el, depth, key, file_key,
                             elements, diags, boundary_ids):
    """解析未知长度 Segment/Cluster 的直接子元素。

    逐段扫描 Level-1 边界：边界之前的内容按普通 EBML 解析；边界元素本身
    用"自身声明长度 / 再下一个边界"确定区间后按普通方式解析。
    绝不把 0xFF marker 当作元素 ID。
    """
    raw_end = el.raw_end
    scan_pos = data_start
    while scan_pos < raw_end:
        bpos = _find_boundary(data, scan_pos, raw_end, boundary_ids)
        if bpos > scan_pos:
            _parse_range(data, scan_pos, bpos, depth=depth + 1,
                         parent_key=key, file_key=file_key,
                         elements=elements, diags=diags)
        if bpos >= raw_end:
            break
        # 计算边界元素自身的可证明终点
        idv2 = ebml.read_id(data, bpos)
        sv2 = ebml.read_vint(data, bpos + idv2.length)
        bdata = bpos + idv2.length + sv2.length
        if ebml.is_unknown_size(sv2):
            bend = _find_boundary(data, bdata, raw_end, boundary_ids)
            # 递归处理未知长度的边界元素（例如未知长度 Cluster 相连）
            _parse_one_unknown_element(data, bpos, idv2, sv2, bend,
                                       depth=depth + 1, parent_key=key,
                                       file_key=file_key, elements=elements,
                                       diags=diags, boundary_ids=boundary_ids)
        else:
            bend = min(bdata + sv2.value, raw_end)
            _parse_range(data, bpos, bend, depth=depth + 1, parent_key=key,
                         file_key=file_key, elements=elements, diags=diags)
        el.raw_end = max(el.raw_end, bend)
        scan_pos = bend


def _parse_one_unknown_element(data, pos, idv, sv, raw_end, *, depth,
                               parent_key, file_key, elements, diags,
                               boundary_ids):
    """解析一个已知 [pos, raw_end) 的未知长度 Level-1 元素及其子元素。"""
    name, etype, unit = ebml.spec_for(idv.value)
    data_start = pos + idv.length + sv.length
    dkey = _el_key(file_key, pos, idv.value, None, raw_end)
    child = EbmlElement(
        key=dkey, id=idv.value, name=name, etype=etype, unit=unit,
        id_start=pos, data_start=data_start, header_end=data_start,
        raw_end=raw_end, declared_size=None, unknown_size=True,
        depth=depth, parent_key=parent_key, inferred_end=True)
    elements.append(child)
    for pp in elements:
        if pp.key == parent_key:
            pp.children.append(dkey)
            break
    diags.append(ParseDiag(
        "UNKNOWN_SIZE", pos,
        f"{name}@{pos} 使用未知长度，终点 {raw_end} 由下一个 Level-1 边界推断",
        "info", raw_end))
    if etype == ebml.T_MASTER and idv.value in (0x18538067, 0x1F43B675):
        _scan_unknown_container(data, data_start, child, depth, dkey,
                                file_key, elements, diags, boundary_ids)
    elif etype == ebml.T_MASTER:
        _parse_range(data, data_start, raw_end, depth=depth + 1,
                     parent_key=dkey, file_key=file_key, elements=elements,
                     diags=diags)
    return raw_end


def _find_boundary(data: bytes, start: int, end: int, boundary_ids: set[int]) -> int:
    """扫描未知长度容器内部，返回第一个 boundary 元素起点（否则 EOF）。

    为了稳妥，仅在"4 字节对齐式的完整元素头 + 合法 size"成立时承认边界；
    扫描到的数据无法构成元素头时继续前进（这可能慢，但 fixture 很小）。
    """
    pos = start
    while pos < end:
        try:
            idv = ebml.read_id(data, pos)
        except ebml.EbmlError:
            pos += 1
            continue
        if idv.value in boundary_ids:
            return pos
        try:
            sv = ebml.read_vint(data, pos + idv.length)
        except ebml.EbmlError:
            pos += 1
            continue
        if ebml.is_unknown_size(sv):
            pos += idv.length + sv.length
            continue
        nxt = pos + idv.length + sv.length + sv.value
        if nxt <= pos or nxt > end:
            pos += 1
            continue
        pos = nxt
    return end


def segment_data_origin(elements: list[EbmlElement]) -> Optional[int]:
    """Segment 数据区起点：CueClusterPosition/SeekPosition 的坐标基准。"""
    for el in elements:
        if el.id == 0x18538067:
            return el.data_start
    return None
