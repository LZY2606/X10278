"""Block / SimpleBlock 载荷规则：有符号相对时码、lacing 帧边界证明。

核心语义（必须守住）：
1. relative timecode 是 *有符号 16 位*（-32768..32767），溢出不是 65535。
2. lacing 损坏时不猜帧边界：任何无法由 lacing 头严格推出的帧大小都不输出；
   若存在多种合法解释（数据恰好同时满足），全部候选都保留。
3. 解析结果按"帧起始偏移 + 帧长"给出可证明范围，损坏点给出字节范围。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional

from . import ebml

LACING_NONE = 0
LACING_XIPH = 1
LACING_FIXED = 2
LACING_EBML = 3

LACING_NAME = {0: "none", 1: "xiph", 2: "fixed", 3: "ebml"}

# Block flags 位布局（SimpleBlock 与 Block 相同的关键位）
FLAG_KEYFRAME = 0x80
FLAG_LACING_MASK = 0x0C       # bits 3-2（Matroska: lacing = (flags>>1)&0x06...）


@dataclass
class LaceDiagnostic:
    code: str
    byte_start: int
    byte_end: int
    message: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BlockParse:
    track_number: int
    tn_length: int
    rel_tc_signed: int
    rel_tc_raw_hex: str
    flags: int
    lacing: int
    lacing_name: str
    frame_count: Optional[int]            # 无法证明时为 None
    frame_ranges: list[list[int]] = field(default_factory=list)  # [start,end)
    candidates: list[list[list[int]]] = field(default_factory=list)
    diagnostics: list[LaceDiagnostic] = field(default_factory=list)
    lace_header_end: Optional[int] = None  # lacing 头可证明的终点
    payload_start: int = 0
    payload_end: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def parse_block(raw: bytes) -> BlockParse:
    """解析 Block/SimpleBlock 的载荷（不含外层 EBML 头）。"""
    diags: list[LaceDiagnostic] = []
    try:
        tn = ebml.read_vint(raw, 0)
    except ebml.EbmlError as exc:
        raise ValueError(f"track number 不可读: {exc}")
    if tn.length + 3 > len(raw):
        raise ValueError("block 载荷不足以容纳 track number + 相对时码 + flags")
    rel_raw = raw[tn.length:tn.length + 2]
    rel = ebml.signed_rel_timecode(rel_raw)
    flags = raw[tn.length + 2]
    lacing = (flags & 0x06) >> 1
    p_start = tn.length + 3
    p_end = len(raw)

    bp = BlockParse(
        track_number=tn.value, tn_length=tn.length, rel_tc_signed=rel,
        rel_tc_raw_hex=rel_raw.hex(), flags=flags, lacing=lacing,
        lacing_name=LACING_NAME.get(lacing, f"reserved({lacing})"),
        frame_count=None, payload_start=p_start, payload_end=p_end,
    )

    if lacing == LACING_NONE:
        bp.frame_count = 1
        bp.lace_header_end = p_start
        if p_end > p_start:
            bp.frame_ranges = [[p_start, p_end]]
        elif p_end == p_start:
            diags.append(LaceDiagnostic("EMPTY_FRAME", p_start, p_end,
                                        "无 lacing 但帧数据为空"))
        bp.diagnostics = diags
        return bp

    if lacing not in (LACING_XIPH, LACING_FIXED, LACING_EBML):
        diags.append(LaceDiagnostic("LACING_RESERVED", p_start, p_end,
                                    f"flags 声明保留 lacing 类型 {lacing}"))
        bp.diagnostics = diags
        return bp

    if p_start >= p_end:
        diags.append(LaceDiagnostic("LACING_HEADER_MISSING", p_start, p_end,
                                    "flags 声明 lacing 但载荷中无帧数字节"))
        bp.diagnostics = diags
        return bp

    frame_count = raw[p_start] + 1
    bp.frame_count = frame_count
    head_pos = p_start + 1

    if lacing == LACING_FIXED:
        # 固定帧长：只有整除时边界才可证明，否则不猜
        payload_len = p_end - head_pos
        if payload_len % frame_count != 0:
            diags.append(LaceDiagnostic(
                "LACING_CORRUPT", head_pos, p_end,
                f"fixed lacing 剩余 {payload_len} 字节无法被 {frame_count} "
                "整除，不猜测帧边界"))
            bp.lace_header_end = head_pos
        else:
            step = payload_len // frame_count
            bp.frame_ranges = [[head_pos + i * step, head_pos + (i + 1) * step]
                               for i in range(frame_count)]
            bp.lace_header_end = head_pos
        bp.diagnostics = diags
        return bp

    sizes, hdr_end, hdr_diag = _decode_lace_sizes(raw, head_pos, p_end,
                                                  frame_count, lacing)
    diags.extend(hdr_diag)
    bp.lace_header_end = hdr_end
    if sizes is not None:
        ranges = _materialize_frames(raw, hdr_end, p_end, sizes, lacing, diags)
        if ranges is not None:
            bp.frame_ranges = ranges
    bp.diagnostics = diags
    return bp


def _decode_lace_sizes(raw: bytes, pos: int, end: int, frame_count: int,
                       lacing: int):
    """返回 (帧大小列表或 None, lacing 头终点, 诊断)。

    Xiph：每帧 1 字节，255 表示继续累加；帧大小之和头损坏（读出范围越过 end）
    则返回 None。
    EBML：首帧为无符号 VINT，其后为 *有符号差值* VINT；任何一步越界即损坏。
    Fixed：头部不含大小（帧大小 = 剩余数据 / frame_count）。
    """
    diags: list[LaceDiagnostic] = []
    if lacing == LACING_FIXED:
        return None, pos, diags  # 固定帧长：大小留给 _materialize 证明

    if lacing == LACING_XIPH:
        sizes: list[int] = []
        for i in range(frame_count - 1):
            total = 0
            size_start = pos
            while True:
                if pos >= end:
                    diags.append(LaceDiagnostic(
                        "LACING_CORRUPT", size_start, min(pos + 1, end),
                        f"xiph 第 {i} 帧大小读取越过 lacing 头/载荷终点 {end}"))
                    return None, size_start, diags
                b = raw[pos]
                pos += 1
                total += b
                if b != 255:
                    break
            sizes.append(total)
        return sizes, pos, diags

    # EBML lacing
    sizes = []
    try:
        first = ebml.read_vint(raw, pos)
    except ebml.EbmlError as exc:
        diags.append(LaceDiagnostic("LACING_CORRUPT", pos, end,
                                    f"ebml lacing 首帧大小不可读: {exc}"))
        return None, pos, diags
    sizes.append(first.value)
    pos += first.length
    for i in range(frame_count - 2):
        ds = pos
        try:
            v = ebml.read_vint(raw, pos)
        except ebml.EbmlError as exc:
            diags.append(LaceDiagnostic(
                "LACING_CORRUPT", ds, end,
                f"ebml lacing 第 {i + 1} 个差值不可读: {exc}"))
            return None, ds, diags
        # 有符号差值：marker 剥离后的范围中心对称
        delta = _ebml_delta(v)
        nxt = sizes[-1] + delta
        if nxt < 0:
            diags.append(LaceDiagnostic(
                "LACING_CORRUPT", ds, ds + v.length,
                f"ebml lacing 差值使第 {i + 1} 帧大小为负 ({nxt})"))
            return None, ds + v.length, diags
        sizes.append(nxt)
        pos += v.length
    return sizes, pos, diags


def _ebml_delta(v: ebml.VInt) -> int:
    # RFC 8794 §6.3.2：delta = value - (2^(7*len-1) - 1)
    return v.value - ((1 << (7 * v.length - 1)) - 1)


def _materialize_frames(raw: bytes, hdr_end: int, end: int,
                        sizes: Optional[list[int]], lacing: int,
                        diags: list[LaceDiagnostic]):
    """根据帧大小严格证明帧边界；不满足即返回 None（不猜）。"""
    payload_len = end - hdr_end
    total = sum(sizes)
    if total > payload_len:
        diags.append(LaceDiagnostic(
            "LACING_CORRUPT", hdr_end, end,
            f"lacing 声明帧大小合计 {total} 超过剩余载荷 {payload_len}"))
        return None
    last = payload_len - total
    if last < 0:
        return None
    all_sizes = sizes + [last]
    ranges = []
    p = hdr_end
    for s in all_sizes:
        ranges.append([p, p + s])
        p += s
    if p != end:
        diags.append(LaceDiagnostic("LACING_TRAILING", p, end,
                                    "帧边界严格推出后仍有剩余字节"))
        return None
    return ranges

