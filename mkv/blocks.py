"""Block/SimpleBlock 解析。

语义红线:
- relative timecode 是 2 字节**有符号**大端整数(相对 cluster Timestamp,
  单位 = TimecodeScale tick)。0x8000 必须解释为 -32768, 不得按无符号处理。
- lacing 数据损坏(尺寸和超过负载、出现负帧长、定长不整除)时:
  不猜测帧边界。frames=None, lacing_corrupt=True, 损坏字节范围原样保留。
"""
from __future__ import annotations

from dataclasses import dataclass, field

LACING_NAMES = {0: "none", 1: "xiph", 2: "fixed", 3: "ebml"}


@dataclass
class BlockData:
    track: int
    rel_raw: str            # 2 字节原始 hex
    rel_signed: int         # 有符号解释, 单位 tick, 基准 cluster Timestamp
    flags: int
    keyframe: bool
    invisible: bool
    discardable: bool
    lacing: str
    n_frames_declared: int
    frames: list | None     # list[(off,len)] 绝对文件范围; 损坏时 None
    lacing_corrupt: bool
    lacing_detail: str
    header_range: tuple     # block 头(轨号+时码+flags+lacing头) 文件范围
    payload_range: tuple    # 帧数据区文件范围


def _read_uvint(buf: bytes, pos: int):
    first = buf[pos]
    mask, length = 0x80, 1
    while length <= 8 and not (first & mask):
        mask >>= 1
        length += 1
    if length > 8 or pos + length > len(buf):
        raise ValueError("vint 非法")
    value = first & (mask - 1)
    for i in range(1, length):
        value = (value << 8) | buf[pos + i]
    return value, length


def _read_svint(buf: bytes, pos: int):
    value, length = _read_uvint(buf, pos)
    bias = (1 << (7 * length - 1)) - 1
    return value - bias, length


def parse_block(payload: bytes, base_offset: int) -> BlockData:
    """payload 为 Block 元素数据; base_offset 为数据在文件中的起始偏移。"""
    track, tlen = _read_uvint(payload, 0)
    rel_raw = payload[tlen:tlen + 2]
    rel_signed = int.from_bytes(rel_raw, "big", signed=True)   # 有符号位宽解释
    flags = payload[tlen + 2]
    lacing_code = (flags & 0x06) >> 1
    lacing = LACING_NAMES[lacing_code]
    head = tlen + 3
    frames, corrupt, detail, n_decl = [], False, "", 1
    body = payload[head:]

    if lacing == "none":
        frames = [(base_offset + head, len(body))]
    else:
        if not body:
            corrupt, detail, frames = True, "lacing 头缺失", None
        else:
            n_decl = body[0] + 1
            sizes, pos = [], 1
            try:
                if lacing == "xiph":
                    for _ in range(n_decl - 1):
                        s = 0
                        while True:
                            b = body[pos]; pos += 1
                            s += b
                            if b != 255:
                                break
                        sizes.append(s)
                elif lacing == "ebml":
                    s, ln = _read_uvint(body, pos); pos += ln
                    sizes.append(s)
                    for _ in range(n_decl - 2):
                        d, ln = _read_svint(body, pos); pos += ln
                        sizes.append(sizes[-1] + d)
                elif lacing == "fixed":
                    rest = len(body) - 1
                    if rest % n_decl != 0:
                        raise ValueError(f"定长 lacing 不整除: {rest}/{n_decl}")
                    sizes = [rest // n_decl] * (n_decl - 1)
                used = sum(sizes)
                rest = len(body) - pos
                last = rest - used
                if used > rest or last < 0 or any(s < 0 for s in sizes):
                    raise ValueError(
                        f"lacing 尺寸和越界: 声明 {sizes}+末帧{last}, 可用 {rest}")
                sizes.append(last)
                off = base_offset + head + pos
                frames, cur = [], off
                for s in sizes:
                    frames.append((cur, s))
                    cur += s
            except (ValueError, IndexError) as exc:
                # 不猜帧边界: 帧列表作废, 损坏范围保留
                corrupt, detail, frames = True, str(exc), None

    lace_hdr_end = head + (0 if lacing == "none" else 1)
    return BlockData(
        track=track, rel_raw=rel_raw.hex(), rel_signed=rel_signed, flags=flags,
        keyframe=bool(flags & 0x80), invisible=bool(flags & 0x08),
        discardable=bool(flags & 0x01), lacing=lacing, n_frames_declared=n_decl,
        frames=frames, lacing_corrupt=corrupt, lacing_detail=detail,
        header_range=(base_offset, base_offset + lace_hdr_end),
        payload_range=(base_offset + head, base_offset + len(payload)),
    )
