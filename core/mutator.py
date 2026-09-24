"""反例变异器：只改动一个直接来源，生成可重复的边界字节片段。

七种情形：
unknown_len / rel_tc_overflow / lace_corrupt / discard_padding /
cue_mispoint / timebase_diff / cluster_gap

每个变异都返回新字节与变异字节范围，影响严格限定在对应来源；
基线字节永不被修改（写时复制）。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Optional

from .parser import parse_file
from .facts import extract
from . import ebml

MUTATION_KINDS = [
    "unknown_len", "rel_tc_overflow", "lace_corrupt", "discard_padding",
    "cue_mispoint", "timebase_diff", "cluster_gap",
]


@dataclass
class MutationResult:
    blob: bytes
    kind: str
    label: str
    changed_ranges: list[tuple[int, int]]   # [(start,end), ...)
    params: dict
    description: str


def _find_first(elements, eid, parent_eid=None):
    for e in elements:
        if e.id == eid and (parent_eid is None or
                            (e.parent_key and
                             next((p for p in elements if p.key == e.parent_key),
                                  None) and
                             next(p for p in elements if p.key == e.parent_key).id
                             == parent_eid)):
            return e
    return None


def _find_clusters(elements):
    return [e for e in elements if e.id == 0x1F43B675]


def _insert_with_ancestor_growth(blob: bytes, elements, target,
                                  extra_payload: bytes):
    """在 target 元素数据区末尾插入 extra_payload，沿祖先链向上重编码。

    每一层记录"插入点相对该层数据区起点的偏移"与新头长度，逐层换算成
    绝对文件偏移。已知大小祖先重算 size；未知大小祖先（如未知长度 Segment）
    保留等长 unknown marker，其后字节整体平移。
    返回 (新字节, 插入内容绝对起点, 绝对终点, 受影响范围)。
    """
    by_key = {e.key: e for e in elements}
    chain = []
    cur = target
    while cur is not None:
        chain.append(cur)
        cur = by_key.get(cur.parent_key) if cur.parent_key else None
    segment = chain[-1]

    # ---- 最深层：target ----
    old_body_len = target.raw_end - target.data_start
    new_body = blob[target.data_start:target.raw_end] + extra_payload
    new_head = ebml.encode_id(target.id) + ebml.encode_vint(len(new_body))
    rebuilt = new_head + new_body
    # 插入点相对 target 新数据区起点的偏移
    insert_rel = old_body_len
    prev = target
    prev_new_head = len(new_head)          # 当前重建子元素的新头长度

    for anc in chain[1:]:
        before = blob[anc.data_start:prev.id_start]
        after = blob[prev.raw_end:anc.raw_end]
        # prev 新元素在 anc body 中起点 = len(before)；插入点相对 anc
        # 新数据区 = before + prev 新头 + 旧 insert_rel
        insert_rel = len(before) + prev_new_head + insert_rel
        body = before + rebuilt + after
        if anc.unknown_size:
            id_len = (anc.id.bit_length() + 7) // 8
            sv = ebml.read_vint(blob, anc.id_start + id_len)
            ahead = ebml.encode_id(anc.id) + (b"\xff" * sv.length)
        else:
            ahead = ebml.encode_id(anc.id) + ebml.encode_vint(len(body))
        rebuilt = ahead + body
        prev_new_head = len(ahead)
        prev = anc

    seg_head_len = len(rebuilt) - (
        len(blob[segment.data_start:segment.raw_end]) + len(extra_payload)
        + sum(0 for _ in ()))
    # 插入点绝对偏移 = segment 新 data_start + insert_rel
    segment_head = rebuilt[:len(ebml.encode_id(segment.id))]
    # segment 新 head 长度：
    import core.ebml as _e
    seg_id_len = (segment.id.bit_length() + 7) // 8
    if segment.unknown_size:
        seg_sv = _e.read_vint(blob, segment.id_start + seg_id_len)
        seg_new_head_len = seg_id_len + seg_sv.length
    else:
        new_seg_body_len = (segment.raw_end - segment.data_start
                            + len(extra_payload))
        seg_new_head_len = seg_id_len + len(ebml.encode_vint(new_seg_body_len))
    ins_abs = segment.id_start + seg_new_head_len + insert_rel
    out = blob[:segment.id_start] + rebuilt + blob[segment.raw_end:]
    return (bytes(out), ins_abs, ins_abs + len(extra_payload),
            [(segment.id_start, segment.id_start + len(rebuilt))])


def new_head_for(el, rebuilt_bytes):
    """从重建后的子元素字节中取出其头长度（id+size VINT）。"""
    id_len = (el.id.bit_length() + 7) // 8
    sv = ebml.read_vint(rebuilt_bytes, id_len)
    # rebuilt_bytes 是该子元素的完整新字节，size 一定已知（变异目标已知大小）
    return id_len + sv.length


def mutate(blob: bytes, kind: str, params: Optional[dict] = None) -> MutationResult:
    params = dict(params or {})
    elements, _ = parse_file(blob, "base")
    facts = extract(elements)
    buf = bytearray(blob)
    ranges: list[tuple[int, int]] = []

    if kind == "unknown_len":
        # 把目标 cluster 改写为未知长度（size 固定 4 字节 0x0fffffff）。
        # 原 cluster size 通常是 1 字节，需要重排其后全部字节；
        # 影响范围严格限定在"该簇 size VINT + 簇后所有偏移"，且该簇终点
        # 只能由下一个 Level-1 元素边界证明。
        clusters = _find_clusters(elements)
        target = clusters[params.get("cluster_index", 1)]
        idv = ebml.read_id(blob, target.id_start)
        sv = ebml.read_vint(blob, target.id_start + idv.length)
        spos = target.id_start + idv.length
        body = blob[target.data_start:target.data_start + sv.value]
        # 裁掉 body 尾部到下一个 Level-1 边界前的部分（未知长度簇不再声明终点，
        # 这里保留完整 body；终点由扫描器用下一簇/Cues 起点证明）
        head = bytes(buf[:spos])
        # body 已包含该簇完整内容；tail 从原声明簇尾开始（下一个 Level-1 元素）
        tail = bytes(buf[target.data_start + sv.value:])
        buf = bytearray(head + ebml.encode_unknown_size(4) + body + tail)
        marker_start = spos
        ranges.append((marker_start, marker_start + 4))
        desc = (f"cluster@{target.id_start} 改为未知长度：解析器只能用下一个 "
                "Level-1 元素边界证明终点，簇自身不再携带长度真值")

    elif kind == "rel_tc_overflow":
        # 把一个 SimpleBlock 的有符号相对时码改为 0x8000（-32768 ticks）
        target_el = _find_block(elements, params.get("cluster_index", 0),
                                params.get("block_index", 0))
        pos = _rel_tc_pos(blob, target_el)
        buf[pos:pos + 2] = bytes([0x80, 0x00])
        ranges.append((pos, pos + 2))
        desc = ("相对时码字节改为 0x8000：必须按有符号解释为 -32768 ticks，"
                "不是 32768；并保留 +65536 包裹候选")

    elif kind == "lace_corrupt":
        # 把一个 laced block 的声明帧数调大，使 size 头越过载荷（不猜边界）
        target_el = _find_laced_block(elements)
        count_pos = _lace_count_pos(blob, target_el)
        orig = buf[count_pos]
        buf[count_pos] = orig + 5  # 多出 5 个不存在的帧
        ranges.append((count_pos, count_pos + 1))
        desc = (f"laced block@{target_el.id_start} 帧数声明 {orig+1}→{orig+6}，"
                "xiph size 头越过载荷：不猜测任何额外帧边界")

    elif kind == "discard_padding":
        tgt = _find_blockgroup(elements, params.get("group_index", 0))
        dp = ebml.encode_element(0x75A2,
                                 (-60_000_000).to_bytes(4, "big", signed=True))
        out, ins_s, ins_e, affected = _insert_with_ancestor_growth(
            blob, elements, tgt, dp)
        buf = bytearray(out)
        ranges.append((ins_s, ins_e))
        desc = ("注入 DiscardPadding=-60,000,000 ns：时间线真值不变，"
                "仅标记解码端应丢弃的前缀；单位保持 ns")

    elif kind == "cue_mispoint":
        # 把第一个 CueClusterPosition 改写为簇区间之外的偏移
        ccp = _find_first(elements, 0xF1)
        if ccp is None:
            raise ValueError("基线没有 CueClusterPosition 可改")
        bad = int(params.get("bad_offset", 9_999_999))
        val = bad.to_bytes(ccp.raw_end - ccp.data_start, "big")
        buf[ccp.data_start:ccp.raw_end] = val
        ranges.append((ccp.data_start, ccp.raw_end))
        desc = (f"CueClusterPosition@{ccp.id_start} 改写为 {bad}：cue 悬空，"
                "记录索引证据与真值分歧，不改 cluster 时间")

    elif kind == "timebase_diff":
        te2 = _find_track_entry(elements, params.get("track_number", 2))
        if te2 is None:
            raise ValueError("基线缺少 2 号轨")
        wrong_ns = params.get("wrong_default_duration_ns", 33_000_000)
        dd = ebml.encode_element(0x23E383, wrong_ns.to_bytes(4, "big"))
        out, ins_s, ins_e, affected = _insert_with_ancestor_growth(
            blob, elements, te2, dd)
        buf = bytearray(out)
        ranges.append((ins_s, ins_e))
        desc = (f"轨道 {params.get('track_number', 2)} 注入 DefaultDuration="
                f"{wrong_ns} ns：仅影响该轨帧时长估计，且不与文件顺序对齐")

    elif kind == "cluster_gap":
        # 把最后一个 cluster 时码推远，制造中途时间缺口（保留字节位置）
        clusters = _find_clusters(elements)
        tgt = clusters[params.get("cluster_index", len(clusters) - 1)]
        tc = _find_first(elements, 0xE7)
        # 找到目标簇内 timecode
        for e in elements:
            if e.id == 0xE7 and e.parent_key == tgt.key:
                tc = e
                break
        big = int(params.get("gap_ticks", 600_000))
        raw = big.to_bytes(tc.raw_end - tc.data_start, "big", signed=True)
        buf[tc.data_start:tc.raw_end] = raw
        ranges.append((tc.data_start, tc.raw_end))
        desc = (f"cluster@{tgt.id_start} 时码改为 {big} ticks："
                "相邻簇之间出现 >500ms 缺口诊断")
    else:
        raise ValueError(f"未知变异类型: {kind}")

    return MutationResult(
        blob=bytes(buf), kind=kind,
        label=params.get("label", kind), changed_ranges=ranges,
        params={**params, "sha1_base": hashlib.sha1(blob).hexdigest()},
        description=desc)


# ---------------------------------------------------------------------------
def _find_block(elements, cluster_index=0, block_index=0):
    clusters = [e for e in elements if e.id == 0x1F43B675]
    cl = clusters[cluster_index]
    blocks = [e for e in elements if e.parent_key == cl.key
              and e.id in (0xA3,)]
    if not blocks:
        # BlockGroup 包裹的 Block
        for g in [e for e in elements if e.parent_key == cl.key
                  and e.id == 0xA0]:
            for b in [e for e in elements if e.parent_key == g.key
                      and e.id == 0xA1]:
                blocks.append(b)
    return blocks[block_index]


def _find_laced_block(elements):
    for e in elements:
        if e.id not in (0xA3, 0xA1):
            continue
        raw = bytes.fromhex(e.value_raw_hex or "")
        try:
            tn = ebml.read_vint(raw, 0)
            lacing = (raw[tn.length + 2] & 0x06) >> 1
        except Exception:
            continue
        if lacing in (1, 2, 3):
            return e
    raise ValueError("基线中没有 laced block；请先在 fixture 中加入")


def _rel_tc_pos(blob, el):
    tn = ebml.read_vint(blob, el.data_start)
    return el.data_start + tn.length


def _lace_count_pos(blob, el):
    tn = ebml.read_vint(blob, el.data_start)
    return el.data_start + tn.length + 3


def _find_blockgroup(elements, index=0):
    groups = [e for e in elements if e.id == 0xA0]
    if not groups:
        raise ValueError("基线中没有 BlockGroup")
    return groups[min(index, len(groups) - 1)]


def _find_track_entry(elements, number):
    for e in elements:
        if e.id != 0xAE:
            continue
        for c in [x for x in elements if x.parent_key == e.key and x.id == 0xD7]:
            if c.value == number:
                return e
    return None
