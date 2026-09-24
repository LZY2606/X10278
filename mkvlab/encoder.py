"""最小 EBML 编码器：供离线案例包构造可解析的 Matroska/WebM 字节片段。"""
from __future__ import annotations

from . import ids


def encode_vint(value: int, width: int | None = None) -> bytes:
    """编码已知长度 EBML VINT；width=None 使用最短表示。"""
    if value < 0:
        raise ValueError("已知长度 VINT 不能为负，未知长度请用 encode_unknown_size")
    if width is None:
        width = size_width(value)
    marker = 1 << (8 - width)
    head = marker | (value >> (8 * (width - 1)))
    return bytes([head]) + value.to_bytes(width - 1, "big")


def encode_unknown_size(width: int = 4) -> bytes:
    """未知长度 VINT：保留位全 1，例如 4 字节 0x1FFFFFFF。"""
    first = 0x1 << (8 - width)
    first |= (1 << (8 - width)) - 1
    return bytes([first]) + b"\xff" * (width - 1)


def size_width(length: int) -> int:
    for w in range(1, 9):
        if length < (1 << (7 * w)) - 1:
            return w
    raise ValueError("长度超出 VINT 范围")


def encode_id(eid: int) -> bytes:
    """元素 ID 以其规范 VINT 宽度（前导 1 决定）编码。"""
    # 直接以最小宽度写出：找到最高位标记
    for w in range(1, 5):
        if eid < 1 << (8 * w):
            return eid.to_bytes(w, "big")
    raise ValueError("ID 过长")


def _id_width(eid: int) -> int:
    return len(encode_id(eid))


def uint_payload(v: int) -> bytes:
    if v == 0:
        return b""
    return v.to_bytes((v.bit_length() + 7) // 8, "big")


def sint_payload(v: int) -> bytes:
    if v == 0:
        return b"\x00"
    n = max(1, (v.bit_length() + 8) // 8)  # 含符号位
    return v.to_bytes(n, "big", signed=True)


def float_payload(v: float) -> bytes:
    import struct
    return struct.pack(">d", v)


def elem(eid: int, payload: bytes, unknown_size: bool = False,
         size_width_override: int | None = None) -> bytes:
    head = encode_id(eid)
    if unknown_size:
        head += encode_unknown_size(size_width_override or 4)
    else:
        sw = size_width_override or size_width(len(payload))
        head += encode_vint(len(payload), sw)
    return head + payload


def uint_elem(eid: int, v: int) -> bytes:
    return elem(eid, uint_payload(v))


def sint_elem(eid: int, v: int) -> bytes:
    return elem(eid, sint_payload(v))


def float_elem(eid: int, v: float) -> bytes:
    return elem(eid, float_payload(v))


def string_elem(eid: int, s: str) -> bytes:
    return elem(eid, s.encode("utf-8"))


def binary_elem(eid: int, b: bytes) -> bytes:
    return elem(eid, b)


# ---- Block / SimpleBlock 构造（含 lacing）----
LACING_NONE = 0
LACING_XIPH = 1
LACING_EBML = 3
LACING_FIXED = 2


def xiph_lace(frames: list[bytes]) -> bytes:
    out = bytearray()
    for f in frames[:-1]:
        n = len(f)
        while n >= 255:
            out.append(255)
            n -= 255
        out.append(n)
    return bytes(out)


def ebml_lace(frames: list[bytes]) -> bytes:
    sizes = [len(f) for f in frames]
    out = bytearray()
    first = sizes[0]
    w = max(1, (first.bit_length() + 7) // 8)
    marker = 1 << (8 - w)
    out.append(marker | (first >> (8 * (w - 1))))
    out += first.to_bytes(w - 1, "big")
    prev = sizes[0]
    for size in sizes[1:-1]:
        diff = size - prev
        out += _encode_signed_vint_diff(diff)
        prev = size
    return bytes(out)


def _encode_signed_vint_diff(diff: int) -> bytes:
    """EBML lacing 差值：有符号，长度位 VINT，最高保留位是符号。"""
    # 确定宽度：先尝试 abs 适配，符号占保留位
    for w in range(1, 9):
        bits = 7 * w - 1  # 1 个符号位
        if -(1 << bits) <= diff < (1 << bits):
            break
    mag = abs(diff)
    marker = 1 << (8 - w)
    sign_bit = 1 << (7 - w) if diff < 0 else 0
    first = marker | sign_bit | (mag >> (8 * (w - 1)))
    return bytes([first]) + mag.to_bytes(w - 1, "big")


def fixed_lace(frames: list[bytes]) -> bytes:
    return b""


def block_payload(track_num: int, rel_tc_s16: int, frames: list[bytes],
                  lacing: int = LACING_NONE, keyframe: bool = True,
                  simple: bool = True, invisible: bool = False,
                  flags_extra: int = 0) -> bytes:
    """构造 Block/SimpleBlock 载荷：track VINT + int16 relative timecode + flags + 帧。"""
    tn = encode_vint(track_num)
    tc = rel_tc_s16.to_bytes(2, "big", signed=True)
    if len(frames) <= 1:
        lacing_code = 0
        body = frames[0] if frames else b""
    else:
        lacing_code = lacing
        if lacing == LACING_XIPH:
            lace = xiph_lace(frames)
        elif lacing == LACING_EBML:
            lace = ebml_lace(frames)
        elif lacing == LACING_FIXED:
            lace = fixed_lace(frames)
        else:
            raise ValueError("多帧必须给出 lacing 类型")
        body = bytes([len(frames) - 1]) + lace + b"".join(frames)
    if simple:
        flags = (0x80 if keyframe else 0) | (lacing_code << 1) | (0x08 if invisible else 0)
    else:
        flags = (lacing_code << 1) | (0x08 if invisible else 0)
    flags |= flags_extra & 0x06
    return tn + tc + bytes([flags]) + body


def simple_block(track_num: int, rel_tc_s16: int, frames: list[bytes],
                 lacing: int = LACING_NONE, keyframe: bool = True,
                 invisible: bool = False) -> bytes:
    return elem(ids.SimpleBlock,
                block_payload(track_num, rel_tc_s16, frames, lacing,
                              keyframe, True, invisible))


def block(track_num: int, rel_tc_s16: int, frames: list[bytes],
          lacing: int = LACING_NONE, invisible: bool = False) -> bytes:
    return elem(ids.Block,
                block_payload(track_num, rel_tc_s16, frames, lacing,
                              False, False, invisible))
