"""领域量的单位 / 坐标基准。

所有领域量都必须携带单位或坐标基准；导入层保留原始表示，只有派生层才做
规范化。无法转换的值留在原位置并附诊断，绝不丢弃。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

NS_PER_MS = 1_000_000


@dataclass(frozen=True)
class Basis:
    """一个值的单位或坐标基准。"""

    unit: str
    note: str = ""

    def to_json(self) -> dict[str, str]:
        out: dict[str, str] = {"unit": self.unit}
        if self.note:
            out["note"] = self.note
        return out


# 原始表示（不做规范化）
RAW_UINT = Basis("raw_uint", "EBML 无符号整数原始值")
RAW_SINT = Basis("raw_sint", "EBML 有符号整数原始值")
RAW_BYTES = Basis("raw_bytes", "未解释的原始字节")
RAW_ASCII = Basis("raw_ascii", "ASCII/UTF-8 原始字符串")
RAW_FLOAT = Basis("raw_float", "IEEE-754 原始浮点")

# 坐标基准（不要与单位混用）
FILE_OFFSET = Basis("coord:byte_offset_from_file_start", "绝对文件字节偏移，含 0 起算")
SEGMENT_OFFSET = Basis("coord:byte_offset_from_segment_data_start", "相对 Segment 载荷起点的偏移")
BYTE_RANGE = Basis("coord:byte_range[start,end)", "半开字节区间，文件绝对坐标")
BYTE_LENGTH = Basis("byte", "字节长度")
VINT_WIDTH = Basis("vint_width_bytes", "VINT 长度标记自身的字节宽度")
ELEMENT_PATH = Basis("coord:ebml_path", "EBML 元素身份路径")

# 时间单位
T_MS = Basis("ms", "Matroska Block/Cluster Timecode 原生刻度（毫秒，除非轨道另有时基）")
T_NS = Basis("ns", "派生绝对时间：cluster_tc*TimecodeScale + relative_tc*TimecodeScale")
T_TICKS = Basis("ticks@track_default_duration", "按轨道 DefaultDuration 归一的帧序刻度")
TIMECODE_SCALE_NS = Basis("ns/tick", "TimecodeScale：一个 timecode tick 对应的纳秒数")

DIAGNOSTIC = Basis("diagnostic", "无法转换 / 无法唯一决定时留在原位置的诊断")
CANDIDATE_SET = Basis("candidate_set", "未收敛为单值的全部合法候选")
INDEX_EVIDENCE = Basis("cue_index_evidence", "Cue 仅为索引证据，不是时间真值")


def q(value: Any, basis: Basis, *, raw: Any = None, role: str | None = None) -> dict[str, Any]:
    """把一个值打包成携带基准的领域量。"""
    out: dict[str, Any] = {"value": value, "basis": basis.unit}
    if basis.note:
        out["basis_note"] = basis.note
    if raw is not None:
        out["raw"] = raw
    if role:
        out["role"] = role
    return out
