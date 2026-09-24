"""构造仓库自带的 Matroska 测试流（casepack fixture 的唯一来源）。

该流确定性地包含全部边界情形：
- Segment 与 Cluster2 使用未知长度
- Cluster2 含 relative timecode = -10（有符号）与 -32768（下溢）
- Cluster2 含 xiph lacing 损坏的 Block（不猜帧边界）
- Cluster1 BlockGroup 含 DiscardPadding = 3_000_000 ns
- Track1/Track2 的 TrackTimecodeScale 不同（1.0 / 0.5，轨道时基差异）
- Cluster2(100) 与 Cluster3(500) 之间存在中途 cluster 缺口
- Cue2 故意错指到 Cluster1 数据中部（cue 错指）
"""
from __future__ import annotations

import struct


def eid_bytes(eid: int) -> bytes:
    nbytes = max(1, (eid.bit_length() + 7) // 8)
    return eid.to_bytes(nbytes, "big")


def vint_size(n: int, length: int | None = None) -> bytes:
    if length is None:
        length = 1
        while n >= (1 << (7 * length)) - 1:
            length += 1
    return (n | (1 << (7 * length))).to_bytes(length, "big")


def signed_vint(diff: int) -> bytes:
    length = 1
    while not (0 <= diff + (1 << (7 * length - 1)) - 1 < (1 << (7 * length)) - 1):
        length += 1
    bias = (1 << (7 * length - 1)) - 1
    return vint_size(diff + bias, length)


def el(eid: int, payload: bytes, unknown: bool = False) -> bytes:
    size = b"\xff" if unknown else vint_size(len(payload))
    return eid_bytes(eid) + size + payload


def uint_el(eid: int, value: int) -> bytes:
    nbytes = max(1, (value.bit_length() + 7) // 8)
    return el(eid, value.to_bytes(nbytes, "big"))


def int_el(eid: int, value: int) -> bytes:
    nbytes = 1
    while not (-(1 << (8 * nbytes - 1)) <= value < (1 << (8 * nbytes - 1))):
        nbytes += 1
    return el(eid, value.to_bytes(nbytes, "big", signed=True))


def float_el(eid: int, value: float) -> bytes:
    return el(eid, struct.pack(">d", value))


def block_payload(track: int, rel: int, frames: list[bytes], lacing: str = "none") -> bytes:
    out = vint_size(track)
    out += rel.to_bytes(2, "big", signed=True)
    lace_bits = {"none": 0, "xiph": 1, "fixed": 2, "ebml": 3}[lacing]
    out += bytes([lace_bits << 1])
    if lacing == "none":
        out += frames[0]
        return out
    out += bytes([len(frames) - 1])
    if lacing == "xiph":
        for frame in frames[:-1]:
            size = len(frame)
            while size >= 255:
                out += b"\xff"
                size -= 255
            out += bytes([size])
    elif lacing == "ebml":
        out += vint_size(len(frames[0]))
        for prev, cur in zip(frames, frames[1:-1]):
            out += signed_vint(len(cur) - len(prev))
    out += b"".join(frames)
    return out


def simple_block(track: int, rel: int, frames: list[bytes], lacing: str = "none") -> bytes:
    return el(0xA3, block_payload(track, rel, frames, lacing))


def build_fixture() -> bytes:
    ebml_header = el(0x1A45DFA3,
                     uint_el(0x4286, 1) + uint_el(0x42F7, 1) + uint_el(0x42F2, 4)
                     + uint_el(0x42F3, 8) + el(0x4282, b"matroska")
                     + uint_el(0x4287, 2) + uint_el(0x4285, 2))
    info = el(0x1549A966, uint_el(0x2AD7B1, 1_000_000))  # TimecodeScale = 1ms
    track1 = el(0xAE, uint_el(0xD7, 1) + uint_el(0x83, 1) + float_el(0x23314F, 1.0))
    track2 = el(0xAE, uint_el(0xD7, 2) + uint_el(0x83, 2) + float_el(0x23314F, 0.5))
    tracks = el(0x1654AE6B, track1 + track2)

    # Cluster1: 正常块 + BlockGroup(DiscardPadding) + 合法 EBML lacing
    c1_body = uint_el(0xE7, 0)
    c1_body += simple_block(1, 0, [b"\x01" * 20])
    c1_body += el(0xA0, el(0xA1, block_payload(2, 5, [b"\x02" * 10]))
                  + uint_el(0x9B, 12) + int_el(0x75A2, 3_000_000))
    c1_body += simple_block(1, 40, [b"\x03" * 8, b"\x04" * 8, b"\x05" * 4], lacing="ebml")
    c1 = el(0x1F43B675, c1_body)

    # Cluster2: 未知长度；负 relative timecode；xiph lacing 损坏；int16 下溢
    c2_body = uint_el(0xE7, 100)
    c2_body += simple_block(1, -10, [b"\x06" * 15])
    corrupt = bytes([2]) + bytes([6, 6]) + b"\x07" * 5  # 声明 3 帧 6/6/? 但负载不足
    c2_body += el(0xA3, vint_size(2) + (0).to_bytes(2, "big", signed=True)
                  + bytes([0x02]) + corrupt)
    c2_body += simple_block(1, -32768, [b"\x08" * 9])
    c2 = el(0x1F43B675, c2_body, unknown=True)

    # Cluster3: 与 Cluster2 之间存在中途缺口（100 -> 500）
    c3_body = uint_el(0xE7, 500) + simple_block(1, 0, [b"\x09" * 20])
    c3 = el(0x1F43B675, c3_body)

    # Cues 需要 cluster 相对 Segment 数据区的偏移
    seg_header_len = len(eid_bytes(0x18538067)) + 1
    seg_data_off = len(ebml_header) + seg_header_len
    off = seg_data_off + len(info) + len(tracks)
    c1_pos = off - seg_data_off
    off += len(c1)
    c2_pos = off - seg_data_off
    off += len(c2)
    c3_pos = off - seg_data_off

    cue1 = el(0xBB, uint_el(0xB3, 0)
              + el(0xB7, uint_el(0xF7, 1) + uint_el(0xF1, c1_pos)))
    cue2 = el(0xBB, uint_el(0xB3, 100)
              + el(0xB7, uint_el(0xF7, 1) + uint_el(0xF1, c1_pos + 7)))  # 错指
    cue3 = el(0xBB, uint_el(0xB3, 500)
              + el(0xB7, uint_el(0xF7, 1) + uint_el(0xF1, c3_pos)))
    cues = el(0x1C53BB6B, cue1 + cue2 + cue3)

    segment = el(0x18538067, info + tracks + c1 + c2 + c3 + cues, unknown=True)
    return ebml_header + segment


def find_cluster3_block_flags_offset(data: bytes) -> int:
    """定位 Cluster3 内 SimpleBlock 的 flags 字节偏移（供局部源改动演示）。"""
    from . import ebml
    elements = ebml.parse(data)
    clusters = ebml.find_all(elements, "Cluster")
    target = clusters[2]
    for child in target["children"]:
        if child["name"] == "SimpleBlock":
            return child["data_offset"] + 1 + 2  # track vint + int16
    raise AssertionError("fixture 结构不符合预期")
