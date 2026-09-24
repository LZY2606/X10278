"""Matroska Block/SimpleBlock 负载解析。

语义红线：
- 相对时码按有符号 16 位解释；
- lacing 损坏时不猜帧边界，frames 置 None 并保留损坏字节范围。
"""
from __future__ import annotations

from .ebml import read_vint, EbmlError

LACING_NAMES = {0: "none", 1: "xiph", 2: "fixed", 3: "ebml"}


def _signed_vint_value(value, length):
    bias = (1 << (7 * length - 1)) - 1
    return value - bias


def decode_lacing(data, p, mode):
    """返回 (frames|None, diagnostics)。frames 为 [(相对负载偏移, 尺寸)]。"""
    if mode == 0:
        return [(p, len(data) - p)], []
    hdr_start = p
    if p >= len(data):
        return None, [{"kind": "lacing_corrupt", "detail": "lacing 头缺失",
                       "byte_range": {"start": hdr_start, "end": len(data)}}]
    count = data[p] + 1
    p += 1
    sizes = []
    try:
        if mode == 1:  # xiph
            for _ in range(count - 1):
                s = 0
                while True:
                    if p >= len(data):
                        raise EbmlError("xiph lace 尺寸截断")
                    b = data[p]
                    p += 1
                    s += b
                    if b < 255:
                        break
                sizes.append(s)
        elif mode == 3:  # ebml
            first, ln, unknown = read_vint(data, p)
            if unknown:
                raise EbmlError("ebml lace 首尺寸为未知长度")
            p += ln
            sizes.append(first)
            for _ in range(count - 2):
                d, ln, _ = read_vint(data, p)
                p += ln
                sizes.append(sizes[-1] + _signed_vint_value(d, ln))
                if sizes[-1] < 0:
                    raise EbmlError("ebml lace 尺寸为负")
        elif mode == 2:  # fixed
            remaining = len(data) - p
            if remaining % count != 0:
                raise EbmlError(f"fixed lace 无法均分: 剩余 {remaining} 字节 / {count} 帧")
            size = remaining // count
            return [(p + i * size, size) for i in range(count)], []
        last = len(data) - p - sum(sizes)
        if last < 0:
            raise EbmlError("lace 尺寸总和超过块负载")
        sizes.append(last)
    except EbmlError as exc:
        return None, [{"kind": "lacing_corrupt", "detail": str(exc),
                       "byte_range": {"start": hdr_start, "end": len(data)}}]
    frames = []
    off = p
    for s in sizes:
        frames.append((off, s))
        off += s
    return frames, []


def parse_block(payload, base_offset):
    """解析 Block/SimpleBlock 负载。base_offset 为负载在文件中的起始偏移。"""
    track, tlen, _ = read_vint(payload, 0)
    if tlen + 3 > len(payload):
        raise EbmlError("block 头截断")
    rel = int.from_bytes(payload[tlen:tlen + 2], "big", signed=True)
    flags = payload[tlen + 2]
    mode = (flags >> 1) & 0x3
    frames, diags = decode_lacing(payload, tlen + 3, mode)
    for d in diags:
        r = d["byte_range"]
        d["byte_range"] = {"start": r["start"] + base_offset,
                           "end": r["end"] + base_offset, "basis": "file"}
    frame_list = None
    if frames is not None:
        frame_list = [
            {"index": i,
             "offset": {"value": base_offset + o, "unit": "byte", "basis": "file"},
             "size": {"value": s, "unit": "byte"}}
            for i, (o, s) in enumerate(frames)
        ]
    return {
        "track": track,
        "relative": {"value": rel, "unit": "tick", "bits": 16, "signed": True},
        "flags": flags,
        "keyframe": bool(flags & 0x80),
        "discardable": bool(flags & 0x01),
        "lacing": {"mode": LACING_NAMES[mode], "frames": frame_list, "diagnostics": diags},
    }
