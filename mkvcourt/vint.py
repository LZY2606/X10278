"""EBML Variable-Size Integer (VINT) 读写。

严格遵循 Matroska/EBML 语义：长度标记的位宽本身也是证据，未知长度
（数据位全为 0 的保留编码）必须保留，不得猜测。
"""
from __future__ import annotations

from dataclasses import dataclass


class VintError(ValueError):
    pass


@dataclass(frozen=True)
class Vint:
    value: int | None  # None 表示未知长度
    width: int
    raw: bytes
    marker_pos: int  # 长度标记在文件中的偏移

    @property
    def unknown(self) -> bool:
        return self.value is None


def read_vint(data: bytes | bytearray | memoryview, pos: int) -> Vint:
    if pos >= len(data):
        raise VintError(f"VINT 起始越界 @{pos}")
    first = data[pos]
    if first == 0:
        # 在 EBML 中首字节为 0 不是合法长度标记
        raise VintError(f"非法 VINT 首字节 0x00 @{pos}")
    width = 1
    marker = 0x80
    while marker and not (first & marker):
        marker >>= 1
        width += 1
    if width > 8 or marker == 0:
        raise VintError(f"VINT 位宽非法 @{pos} (0x{first:02x})")
    if pos + width > len(data):
        raise VintError(f"VINT 被截断 @{pos} width={width}")
    raw = bytes(data[pos : pos + width])
    value = first & (marker - 1)
    for b in raw[1:]:
        value = (value << 8) | b
    if value == 0:
        return Vint(None, width, raw, pos)
    return Vint(value, width, raw, pos)


def encode_vint(value: int, width: int | None = None) -> bytes:
    if value < 0:
        raise VintError("不能编码负 VINT")
    if width is None:
        for cand in range(1, 9):
            if value < (1 << (7 * cand)) - 1:
                width = cand
                break
        if width is None:
            raise VintError("VINT 值过大")
    cap = (1 << (7 * width)) - 1
    if value > cap:
        raise VintError(f"值 {value} 超过 {width} 字节 VINT 容量 {cap}")
    out = bytearray(width)
    v = value
    for i in range(width - 1, 0, -1):
        out[i] = v & 0xFF
        v >>= 8
    out[0] = v | (0x100 >> width)
    return bytes(out)


def encode_unknown(width: int) -> bytes:
    """未知长度：长度标记合法，但数据位全 0（保留语义）。"""
    if not 1 <= width <= 8:
        raise VintError("未知长度位宽必须在 1..8")
    return bytes([0x100 >> width]) + b"\x00" * (width - 1)


def encode_uint(value: int, width: int | None = None, *, signed: bool = False) -> bytes:
    """EBML 元素载荷中的整数（与长度标记无关）。"""
    if width is None:
        if value == 0:
            return b"\x00"
        if signed:
            width = (value + (value < 0)).bit_length() // 8 + 1
        else:
            width = max(1, (value.bit_length() + 7) // 8)
    if signed:
        return int(value).to_bytes(width, "big", signed=True)
    return int(value).to_bytes(width, "big", signed=False)


def decode_uint(raw: bytes, *, signed: bool = False, width: int | None = None) -> int:
    if not raw:
        return 0
    if signed:
        # 有符号位宽解释：以 *实际位宽* 为补码宽度。
        n = width if width is not None else len(raw) * 8
        val = int.from_bytes(raw, "big", signed=False)
        if val >= 1 << (n - 1):
            val -= 1 << n
        return val
    return int.from_bytes(raw, "big", signed=False)
