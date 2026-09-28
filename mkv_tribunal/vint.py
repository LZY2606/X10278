"""EBML VINT (可变长整数) 编解码与带符号解读。

本模块刻意只处理 RFC 9559 规定的 1..8 字节 VINT；任何越宽、截断、
全 0 首字节都显式报错，绝不猜测长度。
"""

from __future__ import annotations


class VintError(ValueError):
    """非法 VINT 编码（截断、全零、超过 8 字节等）。"""


def first_byte_width(first: int) -> int:
    """首字节中最高置位位置决定宽度：0b10000000->1 ... 0b00000001->8。"""
    if first == 0:
        raise VintError("VINT 首字节全 0，宽度未知")
    width = 0
    bit = 0x80
    while bit and not (first & bit):
        width += 1
        bit >>= 1
    if width > 7:
        raise VintError("VINT 宽度超过 8 字节")
    return width + 1


def decode(buf: bytes, pos: int, keep_marker: bool = False) -> tuple[int, int, int]:
    """读取一个 VINT，返回 (值, 宽度, 标记位掩码)。

    keep_marker=True 时保留长度标记位（用于 EBML 元素 ID；ID 必须区分宽度，
    例如 0x80 与 0x01_80 是不同 ID）。
    """
    if pos >= len(buf):
        raise VintError("VINT 在偏移 %d 处截断" % pos)
    width = first_byte_width(buf[pos])
    if pos + width > len(buf):
        raise VintError("VINT 在偏移 %d 处需要 %d 字节，但缓冲区结束" % (pos, width))
    mask = 1 << (7 * width)
    raw = mask - 1
    for i in range(width):
        raw = (raw << 8) | buf[pos + i]
    value = raw if keep_marker else raw - mask
    return value, width, mask


def encode(value: int, width: int | None = None, force_marker: bool = False) -> bytes:
    """编码 VINT。width 为 None 时选择能容纳值的最窄宽度。

    force_marker=True 用于元素 ID：标记位必须 1。
    """
    if value < 0:
        raise VintError("VINT 不能编码负数: %d" % value)
    if width is None:
        width = 1
        limit = 1 << (7 * width)
        while value >= limit:
            width += 1
            if width > 8:
                raise VintError("值 %d 超过 8 字节 VINT" % value)
            limit <<= 7
    if width < 1 or width > 8:
        raise VintError("VINT 宽度非法: %r" % width)
    marker = 1 << (7 * width)
    stored = value if force_marker else value + marker
    max_bits = 8 * width
    if stored >= 1 << max_bits:
        raise VintError("值 %d 无法编码为 %d 字节 VINT" % (value, width))
    out = bytearray(width)
    for i in range(width - 1, -1, -1):
        out[i] = stored & 0xFF
        stored >>= 8
    return bytes(out)


def encode_unknown(width: int = 8) -> bytes:
    """编码“未知长度”VINT：长度位全 1（保留值全 1 表示 unknown）。"""
    if width < 1 or width > 8:
        raise VintError("未知长度 VINT 宽度非法: %r" % width)
    raw = (1 << (8 * width)) - 1
    return bytes(
        (raw >> (8 * (width - 1 - i))) & 0xFF for i in range(width)
    )


def is_unknown_length(length_value: int, width: int) -> bool:
    """长度位是否为“未知长度”保留值（全 1）。"""
    return length_value == (1 << (7 * width)) - 1


def decode_signed(value: int, width_bits: int) -> int:
    """把无符号 VINT 值按有符号位宽解释为二补码整数。

    Matroska 的 signed-integer EBML 类型走这条路径；relative timecode 是
    Block 内自定义的 16 位有符号整数，用 decode_signed(v, 16) 解释。
    """
    sign_bit = 1 << (width_bits - 1)
    return value - (1 << width_bits) if value & sign_bit else value
