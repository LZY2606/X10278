"""Matroska 语义层：Block/SimpleBlock 解析（含 lacing）、Cue 解析、带单位量值。

规则：
- Block 相对时码是 int16 大端有符号数（R1）。
- lacing 损坏时绝不猜测帧边界：整个 Block 降级为单帧 + 诊断（R2）。
- 所有量值携带单位/坐标基准；无法转换的值留在原位置并附诊断。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .ebml import read_vint, EbmlError

NS = "ns"  # 规范时间单位：纳秒


def quantity(value, unit: str, basis: str, raw_repr: str | None = None) -> dict:
    """带单位/坐标基准的量值。raw_repr 保留导入时的原始表示。"""
    return {"value": value, "unit": unit, "basis": basis, "raw": raw_repr}


def tcq(ticks: int | None, basis: str) -> dict:
    """以 cluster timecode ticks 为单位的量值。"""
    return quantity(ticks, "timecode_ticks", basis)


def nsq(ns: int | float | None, basis: str, raw_repr: str | None = None) -> dict:
    return quantity(ns, NS, basis, raw_repr)


@dataclass
class Frame:
    index: int
    offset: int          # 源字节绝对坐标
    size: int
    raw_range: list
    status: str          # ok | degraded（lacing 损坏降级）
    diagnostic: Optional[str] = None

    def to_dict(self):
        return {"index": self.index, "offset": self.offset, "size": self.size,
                "raw_range": self.raw_range, "status": self.status,
                "diagnostic": self.diagnostic}


@dataclass
class BlockInfo:
    offset: int                 # Block/SimpleBlock 元素头起始（绝对坐标）
    data_offset: int            # 元素 payload 起始
    size: int
    end: int
    track_number: int
    timecode: int               # int16 有符号相对时码（ticks）
    timecode_raw_hex: str
    keyframe: bool
    lacing: str                 # none | xiph | fixed | ebml
    flags: int
    frames: list = field(default_factory=list)      # list[Frame]
    lace_diagnostic: Optional[str] = None
    raw_range: list = field(default_factory=list)

    def to_dict(self):
        return {
            "offset": self.offset, "data_offset": self.data_offset, "size": self.size,
            "end": self.end, "track_number": self.track_number,
            "timecode": self.timecode, "timecode_raw_hex": self.timecode_raw_hex,
            "keyframe": self.keyframe, "lacing": self.lacing, "flags": self.flags,
            "frames": [f.to_dict() for f in self.frames],
            "lace_diagnostic": self.lace_diagnostic,
            "raw_range": self.raw_range,
        }


def parse_block(buf: bytes, offset: int, data_offset: int, size: int) -> BlockInfo:
    """解析 Block/SimpleBlock payload。offset/data_offset 为源字节绝对坐标。"""
    end = data_offset + size
    payload = buf[data_offset:end]
    pos = 0
    track_vint = read_vint(payload, 0)
    track_number = track_vint.value
    pos += track_vint.raw_len
    if pos + 2 > len(payload):
        raise EbmlError("Block 缺少相对时码字段")
    tc_raw = payload[pos:pos + 2]
    timecode = int.from_bytes(tc_raw, "big", signed=True)  # R1: int16 有符号
    pos += 2
    flags = payload[pos]
    pos += 1
    keyframe = bool(flags & 0x80)
    lace_type = (flags >> 1) & 0x3
    lacing = {0: "none", 1: "xiph", 2: "fixed", 3: "ebml"}[lace_type]
    info = BlockInfo(offset=offset, data_offset=data_offset, size=size, end=end,
                     track_number=track_number, timecode=timecode,
                     timecode_raw_hex=tc_raw.hex(), keyframe=keyframe,
                     lacing=lacing, flags=flags,
                     raw_range=[offset, end])
    frames_data = payload[pos:]
    if lacing == "none":
        info.frames.append(Frame(0, data_offset + pos, len(frames_data),
                                 [data_offset + pos, end], "ok"))
        return info
    # laced：读取 lace 数与 lace sizes；损坏则整体降级，不猜帧边界
    try:
        if pos >= len(payload):
            raise EbmlError("lacing 缺少 lace 计数字节")
        lace_count = payload[pos] + 1
        pos += 1
        sizes: list[int] = []
        if lacing == "xiph":
            for _ in range(lace_count - 1):
                acc = 0
                while True:
                    if pos >= len(payload):
                        raise EbmlError("xiph lace size 截断")
                    b = payload[pos]
                    pos += 1
                    acc += b
                    if b != 255:
                        break
                sizes.append(acc)
        elif lacing == "fixed":
            if lace_count <= 0:
                raise EbmlError("fixed lacing 计数非法")
            remain = len(payload) - pos
            if lace_count == 0 or remain % lace_count != 0:
                raise EbmlError(f"fixed lacing 余量 {remain} 不能均分为 {lace_count}")
            sizes = [remain // lace_count] * (lace_count - 1)
        else:  # ebml
            first = read_vint(payload, pos)
            pos += first.raw_len
            sizes.append(first.value)
            prev = first.value
            for _ in range(lace_count - 2):
                sv = read_vint(payload, pos)
                pos += sv.raw_len
                # 有符号差值：减去 bias 2^(7*n - 1) - 1
                bias = (1 << (7 * sv.raw_len - 1)) - 1
                diff = sv.value - bias
                prev = prev + diff
                if prev < 0:
                    raise EbmlError("ebml lace size 出现负值")
                sizes.append(prev)
        used = sum(sizes)
        remain = len(payload) - pos
        last = remain - used
        if last < 0:
            raise EbmlError(f"lace sizes 总和 {used} 超出剩余 {remain}")
        sizes.append(last)
        cursor = data_offset + pos
        for i, sz in enumerate(sizes):
            info.frames.append(Frame(i, cursor, sz, [cursor, cursor + sz], "ok"))
            cursor += sz
    except EbmlError as exc:
        # R2：lacing 损坏，不猜帧边界 —— 整个 Block 降级为单帧
        info.lace_diagnostic = f"lacing_corrupt: {exc}"
        info.frames = [Frame(0, data_offset + pos, max(0, len(payload) - pos),
                             [data_offset + pos, end], "degraded",
                             diagnostic=info.lace_diagnostic)]
    return info


def parse_cue_point(children) -> dict:
    """从 CuePoint 子节点提取结构化 cue。"""
    cue = {"cue_time": None, "positions": []}
    for ch in children:
        if ch.name == "CueTime":
            cue["cue_time"] = ch.value
        elif ch.name == "CueTrackPositions":
            pos = {"track": None, "cluster_position": None, "relative_position": None}
            for g in ch.children:
                if g.name == "CueTrack":
                    pos["track"] = g.value
                elif g.name == "CueClusterPosition":
                    pos["cluster_position"] = g.value
                elif g.name == "CueRelativePosition":
                    pos["relative_position"] = g.value
            cue["positions"].append(pos)
    return cue
