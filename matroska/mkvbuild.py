"""Deterministic Matroska/EBML writer used to build in-repo fixtures.

Only used by the case pack / tests so the repository carries its own
domain material instead of generic tables.
"""
from __future__ import annotations

import struct


def el_id(eid: int) -> bytes:
    n = (eid.bit_length() + 7) // 8
    return eid.to_bytes(max(n, 1), "big")


def size_vint(value: int, length: int | None = None) -> bytes:
    if length is None:
        length = 1
        while value >= (1 << (7 * length)) - 1:
            length += 1
    return (value | (1 << (7 * length))).to_bytes(length, "big")


def unknown_size(length: int = 1) -> bytes:
    return bytes([(1 << (8 - length)) - 1 | (1 << (8 - length))])


def element(eid: int, payload: bytes, unknown: bool = False) -> bytes:
    head = el_id(eid)
    if unknown:
        return head + unknown_size(1) + payload
    return head + size_vint(len(payload)) + payload


def uint_el(eid: int, value: int, size: int | None = None) -> bytes:
    if size is None:
        size = max(1, (value.bit_length() + 7) // 8)
    return element(eid, value.to_bytes(size, "big"))


def int_el(eid: int, value: int, size: int | None = None) -> bytes:
    if size is None:
        size = 1
        while not (-(1 << (size * 8 - 1)) <= value < (1 << (size * 8 - 1))):
            size += 1
    return element(eid, value.to_bytes(size, "big", signed=True))


def float_el(eid: int, value: float) -> bytes:
    return element(eid, struct.pack(">d", value))


def str_el(eid: int, value: str) -> bytes:
    return element(eid, value.encode("ascii"))


# ---------------------------------------------------------------- blocks

def _track_vint(track: int) -> bytes:
    if track >= 128:
        raise ValueError("track too large for fixture writer")
    return bytes([0x80 | track])


def simple_block(track: int, timecode: int, frames: list,
                 lacing: str = "none", corrupt: bool = False) -> bytes:
    tc = timecode.to_bytes(2, "big", signed=True)
    if lacing == "none":
        assert len(frames) == 1
        return _track_vint(track) + tc + bytes([0x80]) + frames[0]
    if lacing == "xiph":
        head = bytes([len(frames) - 1])
        for fr in frames[:-1]:
            q, r = divmod(len(fr), 255)
            head += b"\xff" * q + bytes([r])
        flags = 0x80 | (1 << 1)
        body = head + b"".join(frames)
        if corrupt:
            body = bytes([len(frames)]) + b"\xff\xff" + b"".join(frames)
        return _track_vint(track) + tc + bytes([flags]) + body
    if lacing == "fixed":
        flags = 0x80 | (2 << 1)
        body = bytes([len(frames) - 1]) + b"".join(frames)
        if corrupt:
            body = bytes([len(frames)]) + b"".join(frames)  # not divisible
        return _track_vint(track) + tc + bytes([flags]) + body
    raise ValueError(lacing)


def block_group(block_payload: bytes, duration: int = None,
                discard_padding: int = None) -> bytes:
    inner = element(0xA1, block_payload)
    if duration is not None:
        inner += uint_el(0x9B, duration)
    if discard_padding is not None:
        inner += int_el(0x75A2, discard_padding)
    return element(0xA0, inner)


# ---------------------------------------------------------------- fixtures

def _ebml_header() -> bytes:
    return element(0x1A45DFA3,
                   uint_el(0x4286, 1) + uint_el(0x42F7, 1) +
                   uint_el(0x42F2, 4) + uint_el(0x42F3, 8) +
                   str_el(0x4282, "matroska"))


def _info(timecode_scale: int = 1_000_000) -> bytes:
    return element(0x1549A966,
                   uint_el(0x2AD7B1, timecode_scale) +
                   str_el(0x4D80, "casepack-mux"))


def _track_entry(number: int, uid: int, codec: str, track_type: int,
                 timecode_scale: float = None,
                 default_duration: int = None) -> bytes:
    body = (uint_el(0xD7, number) + uint_el(0x73C5, uid) +
            uint_el(0x83, track_type) + str_el(0x86, codec))
    if timecode_scale is not None:
        body += float_el(0x23314F, timecode_scale)
    if default_duration is not None:
        body += uint_el(0x23E383, default_duration)
    return element(0xAE, body)


def _tracks(track_scale_t2: float = 1.0) -> bytes:
    t1 = _track_entry(1, 111, "V_RAWVIDEO", 1,
                      timecode_scale=1.0, default_duration=40_000_000)
    t2 = _track_entry(2, 222, "A_RAW", 2,
                      timecode_scale=track_scale_t2,
                      default_duration=20_833_333)
    return element(0x1654AE6B, t1 + t2)


def _cluster(timestamp: int, blocks: bytes, unknown: bool = False) -> bytes:
    return element(0x1F43B675, int_el(0xE7, timestamp) + blocks,
                   unknown=unknown)


def _cues(points: list) -> bytes:
    body = b""
    for cue_time, track, pos in points:
        body += element(0xBB, uint_el(0xB3, cue_time) +
                        element(0xB7, uint_el(0xF7, track) +
                                uint_el(0xF1, pos)))
    return element(0x1C53BB6B, body)


def build_file(variant: str = "baseline") -> bytes:
    """Build one of the case-pack files.

    Variants each change exactly one direct source aspect relative to the
    baseline:
      unknown_size    - cluster 1 written with unknown (streaming) size
      tc_overflow     - a block carries relative timecode -32768 (0x8000)
      lacing_corrupt  - an audio xiph lace declares sizes beyond the payload
      discard_padding - BlockGroup gains DiscardPadding = -3000000 ns
      cue_mispoint    - second cue points into the middle of a cluster
      track_timebase  - track 2 TrackTimecodeScale becomes 1.001
      cluster_gap     - cluster 1 is removed, leaving a mid-file gap
    """
    out = _ebml_header()
    tscale_t2 = 1.001 if variant == "track_timebase" else 1.0
    info = _info(1_000_000)
    tracks = _tracks(track_scale_t2=tscale_t2)

    # cluster 0
    c0_blocks = (
        element(0xA3, simple_block(1, 0, [b"V0" * 8])) +
        element(0xA3, simple_block(1, 40, [b"V1" * 8])) +
        element(0xA3, simple_block(2, 0, [b"A0" * 5, b"A1" * 5], lacing="xiph",
                                   corrupt=(variant == "lacing_corrupt")))
    )
    c0 = _cluster(0, c0_blocks)

    # cluster 1
    bg_block = simple_block(1, 0, [b"V2" * 8])
    if variant == "discard_padding":
        c1_blocks = element(0xA0, element(0xA1, bg_block) +
                            uint_el(0x9B, 40) + int_el(0x75A2, -3_000_000))
    else:
        c1_blocks = element(0xA0, element(0xA1, bg_block) + uint_el(0x9B, 40))
    c1_blocks += element(0xA3, simple_block(2, 10, [b"A2" * 5]))
    c1 = _cluster(4000, c1_blocks, unknown=(variant == "unknown_size"))

    # cluster 2
    if variant == "tc_overflow":
        c2_blocks = element(0xA3, simple_block(1, -32768, [b"V3" * 8]))
    else:
        c2_blocks = element(0xA3, simple_block(1, 0, [b"V3" * 8]))
    c2_blocks += element(0xA3, simple_block(2, 5, [b"A3" * 5]))
    c2 = _cluster(8000, c2_blocks)

    clusters = c0 + c1 + c2 if variant != "cluster_gap" else c0 + c2

    # CueClusterPosition is relative to the segment data start (spec basis)
    seg_head_len = len(el_id(0x18538067)) + 8  # 8-byte size placeholder
    seg_data_start = len(out) + seg_head_len
    abs_c0 = seg_data_start + len(info) + len(tracks)
    abs_c1 = abs_c0 + len(c0)
    abs_c2 = abs_c1 + (0 if variant == "cluster_gap" else len(c1))

    if variant == "cue_mispoint":
        cues = _cues([(0, 1, abs_c0 - seg_data_start),
                      (8000, 1, abs_c2 - seg_data_start + 17)])
    else:
        cues = _cues([(0, 1, abs_c0 - seg_data_start),
                      (8000, 1, abs_c2 - seg_data_start)])

    seg_body = info + tracks + clusters + cues
    seg = el_id(0x18538067) + size_vint(len(seg_body), length=8) + seg_body
    return out + seg


VARIANTS = [
    "baseline",
    "unknown_size",
    "tc_overflow",
    "lacing_corrupt",
    "discard_padding",
    "cue_mispoint",
    "track_timebase",
    "cluster_gap",
]

VARIANT_LABELS = {
    "baseline": "基线",
    "unknown_size": "未知长度 cluster",
    "tc_overflow": "relative timecode 有符号溢出",
    "lacing_corrupt": "lacing 损坏",
    "discard_padding": "DiscardPadding 负值",
    "cue_mispoint": "cue 错指",
    "track_timebase": "轨道时基差异",
    "cluster_gap": "中途 cluster 缺口",
}
