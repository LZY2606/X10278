"""离线案例包：Matroska 时码庭 —— cluster、cue 索引与多轨帧时间证据。

在仓库内确定性地构造一段 WebM 字节流，覆盖：
- Segment 未知长度 (unknown size)
- relative timecode 有符号溢出 (raw 0x8000 -> -32768)
- xiph lacing 损坏 (尺寸和超出数据)
- BlockGroup + DiscardPadding
- cue 错指 (CueClusterPosition 不落在 cluster 起点)
- 轨道时基差异 (TrackTimecodeScale 1.0 vs 0.5)
- 中途 cluster 缺口 (cluster 时码 0 / 32700 / 90000)
"""
from __future__ import annotations

import struct

from .ebml import encode_id, encode_size

EID = {
    "EBML": 0x1A45DFA3, "EBMLVersion": 0x4286, "EBMLReadVersion": 0x42F7,
    "EBMLMaxIDLength": 0x42F2, "EBMLMaxSizeLength": 0x42F3,
    "DocType": 0x4282, "DocTypeVersion": 0x4287, "DocTypeReadVersion": 0x4285,
    "Segment": 0x18538067, "Info": 0x1549A966, "TimecodeScale": 0x2AD7B1,
    "Tracks": 0x1654AE6B, "TrackEntry": 0xAE, "TrackNumber": 0xD7,
    "TrackUID": 0x73C5, "TrackType": 0x83, "CodecID": 0x86,
    "TrackTimecodeScale": 0x23314F, "DefaultDuration": 0x23E383,
    "Cluster": 0x1F43B675, "Timecode": 0xE7, "BlockGroup": 0xA0,
    "Block": 0xA1, "SimpleBlock": 0xA3, "DiscardPadding": 0x75A2,
    "Cues": 0x1C53BB6B, "CuePoint": 0xBB, "CueTime": 0xB3,
    "CueTrackPositions": 0xB7, "CueTrack": 0xF7, "CueClusterPosition": 0xF1,
}

TIMECODE_SCALE = 1_000_000          # ns/tick = 1ms
TRACK1_DEFAULT_DURATION = 40_000_000  # ns
TRACK2_DEFAULT_DURATION = 20_000_000  # ns
CLUSTER_TIMECODES = (0, 32700, 90000)
DISCARD_PADDING_NS = -2_000_000


def _el(eid: int, payload: bytes, unknown: bool = False) -> bytes:
    head = encode_id(eid)
    if unknown:
        return head + b"\xff" + payload
    return head + encode_size(len(payload)) + payload


def _uint(eid: int, value: int) -> bytes:
    raw = value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big")
    return _el(eid, raw)


def _sint(eid: int, value: int) -> bytes:
    length = 1
    while not (-(1 << (7 * length)) <= value < (1 << (7 * length))):
        length += 1
    return _el(eid, value.to_bytes(length, "big", signed=True))


def _float(eid: int, value: float) -> bytes:
    return _el(eid, struct.pack(">d", value))


def _str(eid: int, text: str) -> bytes:
    return _el(eid, text.encode("utf-8"))


def block_payload(track: int, rel: int, frames: list[bytes],
                  lacing: str = "none", keyframe: bool = False,
                  corrupt: bool = False) -> bytes:
    lace_bits = {"none": 0, "xiph": 1, "fixed": 2, "ebml": 3}[lacing]
    head = encode_size(track)
    head += rel.to_bytes(2, "big", signed=True)
    head += bytes([(0x80 if keyframe else 0) | (lace_bits << 1)])
    if lacing == "none":
        return head + frames[0]
    if lacing == "xiph":
        if corrupt:
            # 声称 3 帧，前两个尺寸各 200，但实际负载只有 10 字节
            return head + bytes([2, 200, 200]) + b"".join(frames)
        body = bytes([len(frames) - 1])
        for frame in frames[:-1]:
            size = len(frame)
            while size >= 255:
                body += b"\xff"
                size -= 255
            body += bytes([size])
        return head + body + b"".join(frames)
    raise ValueError(lacing)


def _ebml_header() -> bytes:
    return _el(EID["EBML"],
               _uint(EID["EBMLVersion"], 1)
               + _uint(EID["EBMLReadVersion"], 1)
               + _uint(EID["EBMLMaxIDLength"], 4)
               + _uint(EID["EBMLMaxSizeLength"], 8)
               + _str(EID["DocType"], "webm")
               + _uint(EID["DocTypeVersion"], 4)
               + _uint(EID["DocTypeReadVersion"], 2))


def _info() -> bytes:
    return _el(EID["Info"], _uint(EID["TimecodeScale"], TIMECODE_SCALE))


def _tracks() -> bytes:
    track1 = _el(EID["TrackEntry"],
                 _uint(EID["TrackNumber"], 1)
                 + _uint(EID["TrackUID"], 11)
                 + _uint(EID["TrackType"], 1)
                 + _str(EID["CodecID"], "V_TEST")
                 + _float(EID["TrackTimecodeScale"], 1.0)
                 + _uint(EID["DefaultDuration"], TRACK1_DEFAULT_DURATION))
    track2 = _el(EID["TrackEntry"],
                 _uint(EID["TrackNumber"], 2)
                 + _uint(EID["TrackUID"], 22)
                 + _uint(EID["TrackType"], 2)
                 + _str(EID["CodecID"], "A_TEST")
                 + _float(EID["TrackTimecodeScale"], 0.5)
                 + _uint(EID["DefaultDuration"], TRACK2_DEFAULT_DURATION))
    return _el(EID["Tracks"], track1 + track2)


def _clusters(fix_lacing: bool) -> tuple[bytes, ...]:
    cl1 = _el(EID["Cluster"],
              _uint(EID["Timecode"], CLUSTER_TIMECODES[0])
              + _el(EID["SimpleBlock"],
                    block_payload(1, 0, [b"\x01" * 24], keyframe=True))
              + _el(EID["BlockGroup"],
                    _el(EID["Block"], block_payload(2, 5, [b"\x02" * 300]))
                    + _sint(EID["DiscardPadding"], DISCARD_PADDING_NS)))
    cl2 = _el(EID["Cluster"],
              _uint(EID["Timecode"], CLUSTER_TIMECODES[1])
              + _el(EID["SimpleBlock"],
                    block_payload(1, -32768, [b"\x03" * 16], keyframe=True))
              + _el(EID["SimpleBlock"],
                    block_payload(1, 100, [b"\x04" * 10, b"\x05" * 20, b"\x06" * 30],
                                  lacing="xiph"))
              + _el(EID["SimpleBlock"],
                    block_payload(2, 120, [b"\x07" * 10], lacing="xiph",
                                  corrupt=not fix_lacing)))
    cl3 = _el(EID["Cluster"],
              _uint(EID["Timecode"], CLUSTER_TIMECODES[2])
              + _el(EID["SimpleBlock"],
                    block_payload(1, 0, [b"\x08" * 24], keyframe=True)))
    return cl1, cl2, cl3


def build_case(fix_lacing: bool = False) -> bytes:
    """构造案例字节流。fix_lacing=True 时修复损坏的 lacing(局部源数据改动)。"""
    header = _ebml_header()
    info = _info()
    tracks = _tracks()
    cl1, cl2, cl3 = _clusters(fix_lacing)
    off_cl1 = len(info) + len(tracks)
    off_cl3 = off_cl1 + len(cl1) + len(cl2)
    cues = _el(EID["Cues"],
               _el(EID["CuePoint"],
                   _uint(EID["CueTime"], CLUSTER_TIMECODES[0])
                   + _el(EID["CueTrackPositions"],
                         _uint(EID["CueTrack"], 1)
                         + _uint(EID["CueClusterPosition"], off_cl1)))
               + _el(EID["CuePoint"],
                     _uint(EID["CueTime"], CLUSTER_TIMECODES[2])
                     + _el(EID["CueTrackPositions"],
                           _uint(EID["CueTrack"], 1)
                           + _uint(EID["CueClusterPosition"], off_cl3 + 7))))
    segment = _el(EID["Segment"], info + tracks + cl1 + cl2 + cl3 + cues,
                  unknown=True)
    return header + segment


CASE_NAME = "matroska-timecode-court.webm"

CASE_DOC = {
    "title": "Matroska 时码庭：cluster、cue 索引与多轨帧时间证据",
    "source": CASE_NAME,
    "scenarios": [
        "unknown_length", "rel_timecode_overflow", "lacing_corrupt",
        "discard_padding", "cue_mispoint", "track_timebase_diff",
        "cluster_gap",
    ],
    "timecode_scale_ns": TIMECODE_SCALE,
    "cluster_timecodes_ticks": list(CLUSTER_TIMECODES),
    "discard_padding_ns": DISCARD_PADDING_NS,
}
