"""EBML 可变长度整数（VINT）读写，含“未知长度（all-one）”语义。"""
from __future__ import annotations

from dataclasses import dataclass


class VIntError(ValueError):
    pass


@dataclass(frozen=True)
class VInt:
    length: int          # 自身占用字节数（1..8）
    value: int           # 去掉 marker 后的数值
    marker_mask: int     # marker 位掩码
    unknown: bool        # 是否为“未知长度/未知值”（数据位全 1）
    raw: bytes           # 原始字节

    @property
    def marker(self) -> int:
        return 1 << (8 - self.length)


def read_vint(buf: bytes, pos: int, *, count_vint: bool = False) -> VInt:
    """在 pos 处读取 VINT。count_vint=True 时按 Element ID 规则（允许 4 字节）。

    未知值：去掉 marker 后数据位全为 1（EBML spec 的 unknown 表示）。
    """
    if pos >= len(buf):
        raise VIntError(f"VINT 越界 @ {pos}")
    first = buf[pos]
    if first == 0:
        raise VIntError(f"VINT 首字节为 0（非法）@ {pos}")
    length = 0
    marker = 0x80
    while length < 8:
        length += 1
        if first & marker:
            break
        marker >>= 1
    else:  # pragma: no cover
        raise VIntError(f"VINT marker 缺失 @ {pos}")
    if pos + length > len(buf):
        raise VIntError(f"VINT 数据不完整 @ {pos} 需要 {length} 字节")
    raw = buf[pos:pos + length]
    data_mask = (1 << (8 * length)) - 1
    marker_mask = marker << (8 * (length - 1))
    value = int.from_bytes(raw, "big") & (data_mask ^ marker_mask)
    all_one = data_mask ^ marker_mask
    unknown = value == all_one
    return VInt(length=length, value=value, marker_mask=marker_mask,
                unknown=unknown, raw=raw)


def encode_vint(value: int, length: int) -> bytes:
    """把已知值编码为指定长度 VINT。"""
    if not 1 <= length <= 8:
        raise VIntError("VINT 长度必须在 1..8")
    data_bits = 7 * length
    if value < 0 or value >= (1 << data_bits) - 1:
        raise VIntError(f"值 {value} 无法用 {length} 字节 VINT 表示（all-one 保留为未知）")
    marker = 1 << (7 * length)
    return (marker | value).to_bytes(length, "big")


def encode_unknown(length: int) -> bytes:
    """编码“未知长度/未知值”：数据位全 1。"""
    if not 1 <= length <= 8:
        raise VIntError("VINT 长度必须在 1..8")
    marker = 1 << (7 * length)
    all_one = (1 << (8 * length)) - 1 - (marker - 1)
    return (marker | all_one).to_bytes(length, "big")


def encode_id(element_id: int) -> bytes:
    """Element ID 本身就是带长度标记的 VINT 形态。"""
    if element_id <= 0:
        raise VIntError("非法 Element ID")
    raw = element_id.to_bytes((element_id.bit_length() + 7) // 8, "big")
    # Element ID 首字节必须含 marker；按 ID 固定长度编码
    if not (raw[0] & 0x80):
        raise VIntError(f"Element ID {element_id:#x} 首字节缺 marker")
    return raw
