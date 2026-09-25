"""领域量：所有值都携带单位/坐标基准。

导入层保留原始表示（raw_value/raw_unit/coord），派生层才做规范化。
无法转换的值留在原位置并附诊断（convertible=False）。
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Optional


# EBML 数值的原始单位/坐标基准
RAW_UINT = "ebml.uint"
RAW_INT = "ebml.int"
RAW_FLOAT = "ebml.float"
RAW_ASCII = "ebml.ascii"
RAW_UTF8 = "ebml.utf8"
RAW_BYTES = "ebml.bytes"
RAW_VINT_UNKNOWN = "ebml.vint_unknown"

# 规范化后的领域单位
NS = "time.ns"
TIMECODE_TICKS = "timecode.ticks"  # 坐标基准：所属 Cluster 起点（相对时码）
BYTES = "bytes"
TRACK_TICKS = "track.ticks"  # 轨道私有时基刻度

# 坐标基准
COORD_SEGMENT_ZERO = "segment.time-zero"
COORD_CLUSTER_RELATIVE = "cluster.relative"
COORD_TRACK_PRIVATE = "track.private-timebase"
COORD_FILE_OFFSET = "file.offset"
COORD_SEGMENT_OFFSET = "segment.offset"


@dataclass
class Quantity:
    """带单位与坐标基准的量。raw_* 为导入时的原始表示。"""

    raw_value: Any
    raw_unit: str
    normalized: Optional[float] = None
    unit: Optional[str] = None
    coord: Optional[str] = None
    convertible: bool = True
    note: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Quantity":
        return cls(**d)


def raw_uint(value: int, *, coord: Optional[str] = None) -> Quantity:
    return Quantity(raw_value=value, raw_unit=RAW_UINT, normalized=float(value),
                    unit=RAW_UINT, coord=coord)


def raw_int(value: int, *, coord: Optional[str] = None) -> Quantity:
    return Quantity(raw_value=value, raw_unit=RAW_INT, normalized=float(value),
                    unit=RAW_INT, coord=coord)


def time_ns(value_ns: float, *, raw_value: Any = None, raw_unit: str = RAW_INT,
            coord: str = COORD_SEGMENT_ZERO, note: Optional[str] = None) -> Quantity:
    return Quantity(raw_value=raw_value if raw_value is not None else int(value_ns),
                    raw_unit=raw_unit, normalized=float(value_ns), unit=NS,
                    coord=coord, note=note)


def ticks(value: int, *, coord: str = COORD_CLUSTER_RELATIVE) -> Quantity:
    return Quantity(raw_value=value, raw_unit=RAW_UINT, normalized=float(value),
                    unit=TIMECODE_TICKS, coord=coord)
