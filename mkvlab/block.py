"""Block/SimpleBlock 结构解析与 lacing 切分。

语义纪律：
- relative timecode 原始为 16bit，按“有符号位宽”解释（默认 int16）；同时保留
  无符号候选为未采纳候选，供反例档案复核，绝不静默改写。
- lacing 损坏时不猜帧边界：返回 frames=None 与受损坏字节范围，最多给出能被
  唯一验证的安全前缀（标记为 proven，不假装完整）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .parser import read_vint, EBMLParseError
from .encoder import LACING_NONE, LACING_XIPH, LACING_EBML, LACING_FIXED


@dataclass
class LaceResult:
    frames: Optional[list[bytes]]
    proven_prefix_frames: int
    frame_ranges: list[tuple[int, int]]
    diagnostics: list[str] = field(default_factory=list)
    corrupt_range: Optional[tuple[int, int]] = None
    candidates: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "frames": None if self.frames is None else
            [{"hex": f.hex(), "len": len(f)} for f in self.frames],
            "proven_prefix_frames": self.proven_prefix_frames,
            "frame_ranges": self.frame_ranges,
            "diagnostics": self.diagnostics,
            "corrupt_range": self.corrupt_range,
            "candidates": self.candidates,
        }


@dataclass
class BlockHeader:
    track_num: int
    rel_tc_raw: int          # 16bit 无符号原始数值
    rel_tc_signed: int       # 有符号位宽解释（int16）
    flags: int
    lacing: int
    keyframe: Optional[bool]
    invisible: bool
    header_len: int
    frame_count: Optional[int]
    diagnostics: list[str] = field(default_factory=list)


def parse_header(payload: bytes) -> tuple[BlockHeader, int]:
    tn = read_vint(payload, 0)
    tc0 = tn.width
    tc_raw = int.from_bytes(payload[tc0:tc0 + 2], "big")
    tc_signed = int.from_bytes(payload[tc0:tc0 + 2], "big", signed=True)
    flags = payload[tc0 + 2]
    lacing = (flags >> 1) & 0x3
    keyframe = bool(flags & 0x80) if True else None
    invisible = bool(flags & 0x08)
    frame_start = tc0 + 3
    hdr = BlockHeader(track_num=tn.value, rel_tc_raw=tc_raw,
                      rel_tc_signed=tc_signed, flags=flags, lacing=lacing,
                      keyframe=keyframe, invisible=invisible,
                      header_len=frame_start, frame_count=None)
    return hdr, frame_start


def _xiph_sizes(data: bytes, count: int) -> tuple[Optional[list[int]], int, list[str]]:
    sizes: list[int] = []
    pos = 0
    diags: list[str] = []
    for i in range(count):
        total = 0
        while pos < len(data) and data[pos] == 255:
            total += 255
            pos += 1
        if pos >= len(data):
            diags.append(f"XIPH_LACE_TRUNCATED:第 {i} 帧尺寸字段被截断")
            return None, pos, diags
        total += data[pos]
        pos += 1
        sizes.append(total)
    return sizes, pos, diags


def _ebml_sizes(data: bytes, count: int) -> tuple[Optional[list[int]], int, int, list[str]]:
    """返回 (前 count 个尺寸含首帧, 占用字节, 已验证帧数, 诊断)。"""
    diags: list[str] = []
    if count == 0:
        return [], 0, 0, diags
    try:
        first = read_vint(data, 0)
    except EBMLParseError as exc:
        diags.append(f"EBML_LACE_BAD_FIRST:{exc}")
        return None, 0, 0, diags
    sizes = [first.value]
    pos = first.width
    prev = first.value
    proven = 1
    for i in range(1, count):
        try:
            v = read_vint(data, pos)
        except EBMLParseError as exc:
            diags.append(f"EBML_LACE_TRUNCATED:第 {i} 差值被截断:{exc}")
            return sizes, pos, proven, diags
        w = v.width
        raw = v.value
        # 符号位：宽度 w 时，最高“保留位”下一位是符号
        sign_bit = 1 << (7 - w)
        negative = bool(raw & sign_bit)
        mag = raw & ((1 << (7 - w)) - 1)
        diff = -mag if negative else mag
        nxt = prev + diff
        if nxt < 0:
            diags.append(f"EBML_LACE_NEGATIVE_SIZE:第 {i} 帧尺寸推导为负 {nxt}")
            return sizes, pos + w, proven, diags
        sizes.append(nxt)
        prev = nxt
        pos += w
        proven += 1
    return sizes, pos, proven, diags


def unlace(payload: bytes, simple: bool = True) -> tuple[BlockHeader, LaceResult]:
    hdr, frame_start = parse_header(payload)
    data = payload[frame_start:]
    res = LaceResult(frames=None, proven_prefix_frames=0, frame_ranges=[])
    if hdr.lacing == LACING_NONE:
        hdr.frame_count = 1
        res.frames = [data]
        res.frame_ranges = [(frame_start, len(payload))]
        res.proven_prefix_frames = 1
        if len(data) == 0:
            res.diagnostics.append("EMPTY_BLOCK:块无帧数据")
        return hdr, res

    if not data:
        res.diagnostics.append("LACE_HEADER_MISSING:多帧块缺少 lace 计数字节")
        res.corrupt_range = (frame_start, len(payload))
        return hdr, res
    count = data[0] + 1
    hdr.frame_count = count
    lace_start = frame_start + 1

    if hdr.lacing == LACING_FIXED:
        body = data[1:]
        if count == 0 or len(body) % count != 0:
            res.diagnostics.append(
                f"FIXED_LACE_BAD_DIVISION:载荷 {len(body)} 字节无法被 {count} 帧整除；"
                "不猜测帧边界")
            res.corrupt_range = (lace_start, len(payload))
            res.candidates.append({
                "kind": "unsigned_tc", "note": "与本错误无关，仅列出解释候选",
            })
            return hdr, res
        size = len(body) // count
        frames, ranges = [], []
        for i in range(count):
            a = lace_start + i * size
            frames.append(payload[a:a + size])
            ranges.append((a, a + size))
        res.frames, res.frame_ranges = frames, ranges
        res.proven_prefix_frames = count
        return hdr, res

    if hdr.lacing == LACING_XIPH:
        sizes, used, diags = _xiph_sizes(data[1:], count - 1)
        res.diagnostics.extend(diags)
        if sizes is None:
            res.corrupt_range = (lace_start, len(payload))
            return hdr, res
        last = len(body_after := payload[lace_start + used:])
        sizes.append(last)
        return _assemble(payload, lace_start + used, sizes, count, res, "XIPH")

    if hdr.lacing == LACING_EBML:
        sizes, used, proven, diags = _ebml_sizes(data[1:], count - 1)
        res.diagnostics.extend(diags)
        if sizes is None:
            res.corrupt_range = (lace_start, len(payload))
            return hdr, res
        # 至少有首帧尺寸；差值部分完整时才尝试组装全部
        body_abs = lace_start + used
        if diags:
            # 尺寸表损坏：只验证安全前缀（不猜后续）
            res.proven_prefix_frames = _safe_prefix(payload, body_abs, sizes, res)
            res.corrupt_range = (lace_start + used, len(payload))
            res.frames = None
            return hdr, res
        last = len(payload) - body_abs - sum(sizes)
        sizes = sizes + [last] if last >= 0 else sizes
        if last < 0:
            res.diagnostics.append(
                "EBML_LACE_SIZE_OVERRUN:声明帧尺寸之和超过载荷，不猜边界")
            res.corrupt_range = (body_abs, len(payload))
            res.frames = None
            return hdr, res
        return _assemble(payload, body_abs, sizes, count, res, "EBML")

    res.diagnostics.append(f"UNKNOWN_LACING_CODE:lacing={hdr.lacing}")
    res.corrupt_range = (frame_start, len(payload))
    return hdr, res


def _safe_prefix(payload, body_abs, sizes, res) -> int:
    pos = body_abs
    proven = 0
    for size in sizes:
        if pos + size > len(payload):
            break
        res.frame_ranges.append((pos, pos + size))
        pos += size
        proven += 1
    return proven


def _assemble(payload, body_start, sizes, count, res, tag):
    if len(sizes) != count:
        res.diagnostics.append(
            f"{tag}_LACE_COUNT_MISMATCH:尺寸数 {len(sizes)} != 帧数 {count}")
        res.corrupt_range = (body_start, len(payload))
        res.frames = None
        return res
    frames, ranges = [], []
    pos = body_start
    for i, size in enumerate(sizes):
        a, b = pos, pos + size
        if b > len(payload):
            res.diagnostics.append(
                f"{tag}_LACE_FRAME_OVERRUN:第 {i} 帧越界 ({b}>{len(payload)})")
            res.corrupt_range = (a, len(payload))
            res.frames = None
            res.proven_prefix_frames = i
            return res
        frames.append(payload[a:b])
        ranges.append((a, b))
        pos = b
    if pos != len(payload):
        res.diagnostics.append(
            f"{tag}_LACE_TRAILING_BYTES:帧组装后余 {len(payload) - pos} 字节")
        res.corrupt_range = (pos, len(payload))
        # 帧边界仍然唯一确定，但记录尾随字节；帧仍可用
    res.frames, res.frame_ranges = frames, ranges
    res.proven_prefix_frames = count
    return res


def rel_tc_candidates(raw16: int) -> list[dict]:
    """保留两种位宽解释；采纳有符号，无符号仅作未采纳候选/溢出证据。"""
    signed = raw16 if raw16 < 0x8000 else raw16 - 0x10000
    return [
        {"interpretation": "signed16", "value": signed, "adopted": True,
         "basis": "Matroska Block: signed 16bit relative timecode"},
        {"interpretation": "unsigned16", "value": raw16, "adopted": False,
         "basis": "未采纳：relative timecode 必须按有符号位宽解释"},
    ]
