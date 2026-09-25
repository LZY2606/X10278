"""确定性地构造案例样例 sample.mkv。

文件内置七种边界情形：
1. Segment 与第二个 Cluster 使用未知长度；
2. 一个 SimpleBlock 的 relative timecode 原始字节为 0x8000（有符号解释 = -32768）；
3. 一个 EBML-lacing SimpleBlock 声明尺寸 [4,60,5] 超出可用负载（损坏）；
4. 一个 BlockGroup 携带 DiscardPadding（有符号，单位 ns）；
5. 第二个 CuePoint 的 CueClusterPosition 错指到 Cluster#2 内部；
6. 两条轨道的 TrackTimecodeScale 不同（1.0 vs 0.5）；
7. Cluster#1 结束于 ~120 tick，Cluster#2 从 1000 tick 开始 —— 中途 cluster 缺口。
"""
from __future__ import annotations

import struct

IDS = {
    "EBML": 0x1A45DFA3, "EBMLVersion": 0x4286, "EBMLReadVersion": 0x42F7,
    "EBMLMaxIDLength": 0x42F2, "EBMLMaxSizeLength": 0x42F3,
    "DocType": 0x4282, "DocTypeVersion": 0x4287, "DocTypeReadVersion": 0x4285,
    "Segment": 0x18538067, "Info": 0x1549A966, "TimecodeScale": 0x2AD7B1,
    "MuxingApp": 0x4D80, "WritingApp": 0x5741,
    "Tracks": 0x1654AE6B, "TrackEntry": 0xAE, "TrackNumber": 0xD7,
    "TrackUID": 0x73C5, "TrackType": 0x83, "FlagLacing": 0x9C,
    "Name": 0x536E, "CodecID": 0x86, "Language": 0x22B59C,
    "DefaultDuration": 0x23E383, "TrackTimecodeScale": 0x23314F,
    "Cluster": 0x1F43B675, "Timecode": 0xE7, "SimpleBlock": 0xA3,
    "BlockGroup": 0xA0, "Block": 0xA1, "BlockDuration": 0x9B,
    "DiscardPadding": 0x75A2,
    "Cues": 0x1C53BB6B, "CuePoint": 0xBB, "CueTime": 0xB3,
    "CueTrackPositions": 0xB7, "CueTrack": 0xF7, "CueClusterPosition": 0xF1,
}

UNKNOWN_SIZE_8 = b"\x01\xff\xff\xff\xff\xff\xff\xff"


def _id_bytes(eid: int) -> bytes:
    return eid.to_bytes((eid.bit_length() + 7) // 8, "big")


def size_bytes(n: int) -> bytes:
    length = 1
    while n >= (1 << (7 * length)) - 1:
        length += 1
    return ((1 << (7 * length)) | n).to_bytes(length, "big")


def elem(name: str, payload: bytes, unknown: bool = False) -> bytes:
    head = _id_bytes(IDS[name])
    return head + (UNKNOWN_SIZE_8 if unknown else size_bytes(len(payload))) + payload


def uint_el(name: str, value: int) -> bytes:
    n = max(1, (value.bit_length() + 7) // 8)
    return elem(name, value.to_bytes(n, "big"))


def int_el(name: str, value: int) -> bytes:
    n = 1
    while not (-(1 << (8 * n - 1)) <= value < (1 << (8 * n - 1))):
        n += 1
    return elem(name, value.to_bytes(n, "big", signed=True))


def float_el(name: str, value: float) -> bytes:
    return elem(name, struct.pack(">d", value))


def str_el(name: str, text: str) -> bytes:
    return elem(name, text.encode("utf-8"))


def _lace_size_vint(value: int) -> bytes:
    length = 1
    while value >= (1 << (7 * length)) - 1:
        length += 1
    return ((1 << (7 * length)) | value).to_bytes(length, "big")


def _lace_diff_vint(diff: int) -> bytes:
    length = 1
    while not (-((1 << (7 * length - 1)) - 1) <= diff <= (1 << (7 * length - 1)) - 1):
        length += 1
    bias = (1 << (7 * length - 1)) - 1
    return ((1 << (7 * length)) | (diff + bias)).to_bytes(length, "big")


def block_payload(track: int, rel_tc: int, flags: int, frames: list[bytes],
                  lacing: str = "none", declared_sizes: list[int] | None = None) -> bytes:
    out = bytes([0x80 | track])
    out += (rel_tc & 0xFFFF).to_bytes(2, "big")
    out += bytes([flags])
    if lacing == "xiph":
        out += bytes([len(frames) - 1])
        for f in frames[:-1]:
            assert len(f) < 255
            out += bytes([len(f)])
    elif lacing == "ebml":
        sizes = declared_sizes if declared_sizes is not None else [len(f) for f in frames]
        out += bytes([len(frames) - 1])
        out += _lace_size_vint(sizes[0])
        for a, b in zip(sizes, sizes[1:]):
            out += _lace_diff_vint(b - a)
    elif lacing == "fixed":
        out += bytes([len(frames) - 1])
    out += b"".join(frames)
    return out


def simple_block(track: int, rel_tc: int, frames: list[bytes], lacing: str = "none",
                 keyframe: bool = True, declared_sizes: list[int] | None = None) -> bytes:
    lace_bits = {"none": 0, "xiph": 1, "fixed": 2, "ebml": 3}[lacing]
    flags = (0x80 if keyframe else 0) | (lace_bits << 1)
    return elem("SimpleBlock", block_payload(track, rel_tc, flags, frames, lacing, declared_sizes))


def build_sample() -> tuple[bytes, dict]:
    ebml_header = elem("EBML",
                       uint_el("EBMLVersion", 1) + uint_el("EBMLReadVersion", 1)
                       + uint_el("EBMLMaxIDLength", 4) + uint_el("EBMLMaxSizeLength", 8)
                       + str_el("DocType", "matroska")
                       + uint_el("DocTypeVersion", 4) + uint_el("DocTypeReadVersion", 2))

    info = elem("Info",
                uint_el("TimecodeScale", 1_000_000)
                + str_el("MuxingApp", "mkvtime-case-1.0")
                + str_el("WritingApp", "mkvtime-case-1.0"))

    track1 = elem("TrackEntry",
                  uint_el("TrackNumber", 1) + uint_el("TrackUID", 101)
                  + uint_el("TrackType", 1) + uint_el("FlagLacing", 1)
                  + str_el("Name", "video-main") + str_el("CodecID", "V_FFV1")
                  + str_el("Language", "und")
                  + uint_el("DefaultDuration", 40_000_000)
                  + float_el("TrackTimecodeScale", 1.0))
    track2 = elem("TrackEntry",
                  uint_el("TrackNumber", 2) + uint_el("TrackUID", 102)
                  + uint_el("TrackType", 2) + uint_el("FlagLacing", 1)
                  + str_el("Name", "audio-main") + str_el("CodecID", "A_PCM/INT/LIT")
                  + str_el("Language", "eng")
                  + uint_el("DefaultDuration", 20_000_000)
                  + float_el("TrackTimecodeScale", 0.5))
    tracks = elem("Tracks", track1 + track2)

    # Cluster#1, timecode 0
    sb1 = simple_block(1, 0, [b"V1aaa", b"V1bbb"], lacing="xiph")          # 正常 xiph lacing
    sb2 = simple_block(2, 0, [b"A1" * 4])                                    # 音频，无 lacing
    sb3 = simple_block(1, 40, [b"V2" * 4], keyframe=False)
    bg_block = elem("Block", block_payload(1, 80, 0, [b"V3" * 6]))
    blockgroup = elem("BlockGroup",
                      bg_block + uint_el("BlockDuration", 40)
                      + int_el("DiscardPadding", -2_000_000))                # discard padding
    sb5 = simple_block(1, -32768, [b"VX" * 2], keyframe=False)               # 0x8000 有符号溢出
    cluster1 = elem("Cluster", uint_el("Timecode", 0) + sb1 + sb2 + sb3 + blockgroup + sb5)

    # Cluster#2, timecode 1000（与 cluster1 末尾之间是缺口），未知长度
    sb6 = simple_block(1, 0, [b"F1aa", b"F2bbbbbb", b"F3ccc"], lacing="ebml",
                       declared_sizes=[4, 60, 5])                            # lacing 损坏
    sb7 = simple_block(2, 5, [b"A2" * 4])
    cluster2 = elem("Cluster", uint_el("Timecode", 1000) + sb6 + sb7, unknown=True)

    body_prefix = info + tracks
    cluster1_off = len(body_prefix)                      # 相对 Segment 数据起点
    cluster2_off = cluster1_off + len(cluster1)

    cue1 = elem("CuePoint", uint_el("CueTime", 0)
                + elem("CueTrackPositions", uint_el("CueTrack", 1)
                       + uint_el("CueClusterPosition", cluster1_off)))
    cue2 = elem("CuePoint", uint_el("CueTime", 1000)
                + elem("CueTrackPositions", uint_el("CueTrack", 1)
                       + uint_el("CueClusterPosition", cluster2_off + 5)))   # cue 错指
    cues = elem("Cues", cue1 + cue2)

    segment = elem("Segment", body_prefix + cluster1 + cluster2 + cues, unknown=True)
    data = ebml_header + segment

    meta = {
        "cluster1_rel_offset": cluster1_off,
        "cluster2_rel_offset": cluster2_off,
        "cue2_claimed_rel_offset": cluster2_off + 5,
        "corrupt_lace_declared": [4, 60, 5],
        "corrupt_lace_fixed": [4, 6, 5],
        "length": len(data),
    }
    return data, meta


def fix_corrupt_lacing(data: bytes, lace_header_range: list[int]) -> bytes:
    """源数据改动：只把损坏的 EBML lacing 尺寸头改回正确值 [4,6,5]。

    正确编码与损坏编码长度相同（各 3 字节），因此其它元素偏移不变，
    改动严格局限于该 block 的 lacing 头。
    """
    start, end = lace_header_range
    fixed = _lace_size_vint(4) + _lace_diff_vint(6 - 4) + _lace_diff_vint(5 - 6)
    assert len(fixed) == end - start, (len(fixed), end - start)
    return data[:start] + fixed + data[end:]
