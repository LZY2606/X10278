"""最小 Matroska/EBML 字节构造器，只服务于仓库自带 fixture。

支持：主元素、uint/sint/float/string/binary 载荷、未知长度主元素、
SimpleBlock/Block（含三种 lacing）。构造结果必须能被 parser 解析回来。
"""

from __future__ import annotations

import struct

from .vint import encode as encode_vint, encode_unknown


def _id_bytes(element_id: int) -> bytes:
    # ID 在 schema 中保留了宽度标记位，直接按最短宽度重编码会改变宽度，
    # 因此按 element_id 自身有效字节数输出（首字节必含宽度标记）。
    width = (element_id.bit_length() + 7) // 8
    return element_id.to_bytes(width, "big")


def elem(element_id: int, payload: bytes, *, unknown_length: bool = False,
         size_width: int | None = None) -> bytes:
    head = _id_bytes(element_id)
    if unknown_length:
        return head + encode_unknown(size_width or 1) + payload
    if size_width is not None:
        size = encode_vint(len(payload), width=size_width)
    else:
        size = encode_vint(len(payload))
    return head + size + payload


def uint(element_id: int, value: int, *, width: int | None = None) -> bytes:
    if width is None:
        width = max(1, (value.bit_length() + 7) // 8) if value else 1
    return elem(element_id, value.to_bytes(width, "big"))


def sint(element_id: int, value: int, width: int = 4) -> bytes:
    return elem(element_id, value.to_bytes(width, "big", signed=True))


def float64(element_id: int, value: float) -> bytes:
    return elem(element_id, struct.pack(">d", value))


def ascii_el(element_id: int, text: str) -> bytes:
    return elem(element_id, text.encode("ascii"))


def block_bytes(track_number: int, relative_signed: int, flags: int,
                frame_payloads: list[bytes], lacing: int = 0,
                keyframe: bool = False) -> bytes:
    """构造 SimpleBlock 样式载荷（track VINT + int16 + flags + 帧）。"""
    head = encode_vint(track_number)
    head += relative_signed.to_bytes(2, "big", signed=True)
    flag_byte = (lacing & 0x3) << 1
    if keyframe:
        flag_byte |= 0x80
    head += bytes([flag_byte])
    if lacing == 0:
        assert len(frame_payloads) == 1
        return head + frame_payloads[0]
    if lacing == 3:  # fixed：无帧数字节
        assert frame_payloads and len({len(f) for f in frame_payloads}) == 1
        return head + b"".join(frame_payloads)
    if lacing == 1:  # Xiph
        body = bytes([len(frame_payloads) - 1])
        for frame in frame_payloads[:-1]:
            size = len(frame)
            encoded = b"\xff" * (size // 255)
            encoded += bytes([size % 255])
            body += encoded
        return head + body + b"".join(frame_payloads)
    if lacing == 2:  # EBML
        body = bytes([len(frame_payloads) - 1])
        prev = None
        for frame in frame_payloads[:-1]:
            size = len(frame)
            delta = size if prev is None else size - prev
            body += encode_vint(delta)
            prev = size
        return head + body + b"".join(frame_payloads)
    raise ValueError("未知 lacing 类型 %d" % lacing)
