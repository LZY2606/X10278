"""领域量：所有值携带单位或坐标基准；导入层保留原始表示，派生层才规范化。

Basis 描述一个量的坐标基准，例如文件字节偏移、相对 Segment data 起点的
偏移、ticks（TimecodeScale 缩放前）、纳秒等。无法规范化的值保留在原位置
（raw_value + 诊断），不为了画图而丢弃。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional


@dataclass(frozen=True)
class Basis:
    unit: str
    origin: str = "file"
    scale: Optional[str] = None
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# 文件/段字节坐标
B_FILE_OFFSET = Basis("byte", origin="file_start", note="绝对文件偏移")
B_FILE_RANGE = Basis("byte", origin="file_start", note="文件内原始字节范围")
B_SEG_OFFSET = Basis("byte", origin="segment_data_start", note="相对 Segment data 起点")
# 时码
B_TICKS = Basis("tick", origin="cluster_timecode", scale="TimecodeScale",
                note="Block relative timecode 的有符号 int16 tick")
B_CLUSTER_TICKS = Basis("tick", origin="cluster_timecode", note="Cluster Timecode 原始 tick")
B_CUE_TICKS = Basis("tick", origin="segment_timecode_scale", note="CueTime 原始 tick")
B_NS = Basis("ns", origin="segment_start", scale="TimecodeScale", note="规范化时间，纳秒")
B_DISCARD_NS = Basis("ns", origin="block_presentation", note="DiscardPadding，块内前导丢弃")
# 数值（无单位）
B_RAW = Basis("raw", origin="element_payload", note="保留原始表示")
B_INDEX = Basis("ordinal", origin="file_order", note="文件顺序序号，仅作展示不作时间真值")
B_TRACK_TICKS = Basis("tick", origin="track_clock", scale="track_timebase",
                      note="轨道时基 tick（DefaultDuration/TrackOffset 换算前）")


@dataclass
class Quantity:
    """带单位/坐标基准的值；raw 是导入时的原始表示，value 是规范化结果。

    转换失败时 value=None，diagnostics 记录原因，raw 永不丢失。
    """
    raw: Any
    basis: Basis
    value: Any = None
    converted: bool = False
    diagnostics: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "raw": self.raw,
            "value": self.value,
            "converted": self.converted,
            "basis": self.basis.to_dict(),
            "diagnostics": list(self.diagnostics),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Quantity":
        b = d["basis"]
        return cls(
            raw=d["raw"],
            basis=Basis(unit=b["unit"], origin=b.get("origin", ""),
                        scale=b.get("scale"), note=b.get("note", "")),
            value=d.get("value"),
            converted=d.get("converted", False),
            diagnostics=list(d.get("diagnostics", [])),
        )


def ticks_to_ns(ticks: int, timecode_scale_ns: int) -> int:
    """Cluster/Block tick -> 纳秒。TimecodeScale 单位是纳秒/tick。"""
    return int(ticks) * int(timecode_scale_ns)


def normalize_tick(raw: int, basis: Basis, timecode_scale_ns: Optional[int]) -> Quantity:
    q = Quantity(raw=raw, basis=basis)
    if timecode_scale_ns is None:
        q.diagnostics.append("NO_TIMESCALE:缺少 TimecodeScale，tick 无法规范化为 ns，保留原值")
        q.value = None
        return q
    q.value = ticks_to_ns(raw, timecode_scale_ns)
    q.converted = True
    return q
