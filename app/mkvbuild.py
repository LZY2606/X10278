"""Matroska/WebM 二进制构造器:fixtures 与测试共用,确定性输出。"""
from __future__ import annotations

import struct


def eid_bytes(eid: int) -> bytes:
    if eid <= 0xFF:
        return bytes([eid])
    if eid <= 0xFFFF:
        return eid.to_bytes(2, "big")
    if eid <= 0xFFFFFF:
        return eid.to_bytes(3, "big")
    return eid.to_bytes(4, "big")


def size_vint(value: int, length: int | None = None) -> bytes:
    if length is None:
        length = 1
        while value >= (1 << (7 * length)) - 1:
            length += 1
    return ((1 << (7 * length)) | value).to_bytes(length, "big")


def unknown_size(length: int = 1) -> bytes:
    return ((1 << (7 * length + 1)) - 1).to_bytes(length, "big")


def el(eid: int, payload: bytes, unknown: bool = False) -> bytes:
    size = unknown_size() if unknown else size_vint(len(payload))
    return eid_bytes(eid) + size + payload


def uint_el(eid: int, value: int) -> bytes:
    n = max(1, (value.bit_length() + 7) // 8)
    return el(eid, value.to_bytes(n, "big"))


def int_el(eid: int, value: int) -> bytes:
    n = 1
    while not (-(1 << (8 * n - 1)) <= value < (1 << (8 * n - 1))):
        n += 1
    return el(eid, value.to_bytes(n, "big", signed=True))


def float_el(eid: int, value: float) -> bytes:
    return el(eid, struct.pack(">d", value))


def str_el(eid: int, value: str) -> bytes:
    return el(eid, value.encode())


def track_vint(track: int) -> bytes:
    return size_vint(track)


def block_payload(track: int, rel_timecode: int, frames: list[bytes],
                  lacing: str = "none", keyframe: bool = True) -> bytes:
    """构造 Block 载荷。rel_timecode 按有符号 int16 写入。"""
    out = bytearray()
    out += track_vint(track)
    out += rel_timecode.to_bytes(2, "big", signed=True)
    lace_bits = {"none": 0, "xiph": 1, "fixed": 2, "ebml": 3}[lacing]
    flags = (0x80 if keyframe else 0) | (lace_bits << 1)
    out.append(flags)
    if lacing == "none":
        out += frames[0]
    elif lacing == "fixed":
        out.append(len(frames) - 1)
        out += b"".join(frames)
    elif lacing == "xiph":
        out.append(len(frames) - 1)
        for f in frames[:-1]:
            n = len(f)
            while n >= 255:
                out.append(255)
                n -= 255
            out.append(n)
        out += b"".join(frames)
    elif lacing == "ebml":
        out.append(len(frames) - 1)
        out += size_vint(len(frames[0]))
        prev = len(frames[0])
        for f in frames[1:-1]:
            diff = len(f) - prev
            bias = (1 << (7 * 1 - 1)) - 1  # 1 字节偏置 63
            assert -63 <= diff <= 64, "fixture 只用 1 字节差分"
            out.append(diff + bias)
            prev = len(f)
        out += b"".join(frames)
    return bytes(out)


def simple_block(track: int, rel_timecode: int, frames: list[bytes],
                 lacing: str = "none", keyframe: bool = True) -> bytes:
    return el(0xA3, block_payload(track, rel_timecode, frames, lacing, keyframe))


def block_group(track: int, rel_timecode: int, frames: list[bytes],
                lacing: str = "none", duration: int | None = None,
                discard_padding_ns: int | None = None) -> bytes:
    inner = el(0xA1, block_payload(track, rel_timecode, frames, lacing))
    payload = inner
    if duration is not None:
        payload += uint_el(0x9B, duration)
    if discard_padding_ns is not None:
        payload += int_el(0x75A2, discard_padding_ns)
    return el(0xA0, payload)


def cluster(timecode: int, blocks: list[bytes], unknown: bool = False) -> bytes:
    payload = uint_el(0xE7, timecode) + b"".join(blocks)
    return el(0x1F43B675, payload, unknown=unknown)


def cue_point(cue_time: int, cue_track: int, cue_cluster_position: int) -> bytes:
    pos = uint_el(0xF7, cue_track) + uint_el(0xF1, cue_cluster_position)
    return el(0xBB, uint_el(0xB3, cue_time) + el(0xB7, pos))


def track_entry(number: int, track_type: int, codec: str,
                timecode_scale: float | None = None,
                default_duration_ns: int | None = None) -> bytes:
    payload = uint_el(0xD7, number) + uint_el(0x73C5, number * 1111)
    payload += uint_el(0x83, track_type) + str_el(0x86, codec)
    if timecode_scale is not None:
        payload += float_el(0x23314F, timecode_scale)
    if default_duration_ns is not None:
        payload += uint_el(0x23E383, default_duration_ns)
    return el(0xAE, payload)


def ebml_header() -> bytes:
    return el(0x1A45DFA3, str_el(0x4282, "matroska"))


def segment(payload: bytes, unknown: bool = False) -> bytes:
    return el(0x18538067, payload, unknown=unknown)


def build_court_case() -> bytes:
    """“Matroska 时码庭”主 fixture:单文件覆盖全部七类边界情形。

    1. unknown length        —— 第二个 Cluster 使用未知长度
    2. relative timecode 溢出 —— Block 相对时码 0x8000(-32768,有符号解释)
    3. lacing 损坏           —— xiph 尺寸表越过帧区
    4. discard padding       —— BlockGroup 携带 DiscardPadding(单位 ns)
    5. cue 错指              —— 一个 CueTime 不对应任何帧
    6. 轨道时基差异          —— Track 2 的 TrackTimecodeScale = 0.5
    7. 中途 cluster 缺口     —— cluster 时码 0 / 1000 / 9000
    """
    info = el(0x1549A966, uint_el(0x2AD7B1, 1_000_000))  # TimecodeScale = 1ms(ns)
    tracks = el(0x1654AE6B,
                track_entry(1, 1, "V_RAWVIDEO", timecode_scale=1.0,
                            default_duration_ns=40_000_000) +
                track_entry(2, 2, "A_RAW", timecode_scale=0.5,
                            default_duration_ns=20_000_000))

    # cluster 0 @ tick 0:正常双轨 + 多轨乱序(track2 的块写在 track1 之前)
    c0 = cluster(0, [
        simple_block(2, 0, [b"a0" * 4]),
        simple_block(1, 0, [b"v0" * 8]),
        simple_block(1, 40, [b"v1" * 8]),
        simple_block(2, 40, [b"a1" * 4]),
    ])

    # cluster 1 @ tick 40:未知长度 + relative timecode 溢出 + discard padding
    # rel = -32768(0x8000):有符号解释 => 绝对时码 40-32768 = -32728 tick
    c1 = cluster(40, [
        simple_block(1, -32768, [b"vneg" * 4]),
        block_group(1, 140, [b"vpad" * 4], duration=40,
                    discard_padding_ns=-12_000_000),
    ], unknown=True)

    # cluster 2 @ tick 80:lacing 损坏(xiph 尺寸表越过帧区)+ 正常 fixed lacing
    corrupt_xiph = bytearray(block_payload(1, 0, [b"x" * 3, b"y" * 3], "xiph"))
    corrupt_xiph[5] = 250  # 篡改 lace 尺寸表:声称第一帧 250 字节,远超帧区
    c2 = cluster(80, [
        el(0xA3, bytes(corrupt_xiph)),
        simple_block(2, 0, [b"f0" * 3, b"f1" * 3, b"f2" * 3], lacing="fixed"),
    ])

    # cluster 3 @ tick 9000:中途缺口(80 -> 9000 远超阈值)
    c3 = cluster(9000, [
        simple_block(1, 0, [b"vend" * 4]),
        simple_block(2, 0, [b"aend" * 4]),
    ])

    # cues:一个正确指向 + 一个错指(cue_time 777 不对应任何帧)
    cues = el(0x1C53BB6B,
              cue_point(0, 1, 0) +
              cue_point(777, 1, 0) +      # cue 错指
              cue_point(0, 2, 0))

    body = info + tracks + c0 + c1 + c2 + c3 + cues
    return ebml_header() + segment(body, unknown=True)


def build_minimal() -> bytes:
    """最小合法文件:单轨、两帧、一个 cue。测试用。"""
    info = el(0x1549A966, uint_el(0x2AD7B1, 1_000_000))
    tracks = el(0x1654AE6B, track_entry(1, 1, "V_RAWVIDEO",
                                        default_duration_ns=40_000_000))
    c0 = cluster(0, [simple_block(1, 0, [b"v0" * 4]),
                     simple_block(1, 40, [b"v1" * 4])])
    cues = el(0x1C53BB6B, cue_point(0, 1, 0))
    return ebml_header() + segment(info + tracks + c0 + cues)


FIXTURES = {
    "court_case.webm": build_court_case,
    "minimal.webm": build_minimal,
}


def main() -> None:
    import os
    os.makedirs("fixtures", exist_ok=True)
    for name, fn in FIXTURES.items():
        path = os.path.join("fixtures", name)
        with open(path, "wb") as fh:
            fh.write(fn())
        print(f"wrote {path} ({os.path.getsize(path)} bytes)")


if __name__ == "__main__":
    main()
