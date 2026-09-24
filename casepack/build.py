"""Builds the offline case pack for "Matroska timecode court".

Every fixture is a synthetic Matroska/WebM byte stream generated with the
repository's own EBML writer.  The baseline preserves element offsets,
unknown lengths, track parameters, cluster/block timecodes, discard
padding, cues and raw byte ranges; each variant changes exactly one direct
source relative to the baseline.
"""
from __future__ import annotations

import json
import os

from mkv import mkvwrite as w

FIXTURES_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "fixtures")

TIMECODE_SCALE = 1_000_000          # 1 ms per timecode unit (unit: ns)
VIDEO_DEFAULT_DURATION = 40_000_000  # ns (25 fps)
AUDIO_DEFAULT_DURATION = 21_333_333  # ns


def _header() -> bytes:
    return w.elem("EBML",
                  w.uint_elem("EBMLVersion", 1)
                  + w.uint_elem("EBMLReadVersion", 1)
                  + w.str_elem("DocType", "matroska"))


def _info(scale: int = TIMECODE_SCALE) -> bytes:
    return w.elem("Info",
                  w.uint_elem("TimecodeScale", scale)
                  + w.str_elem("MuxingApp", "casepack-builder 1.0")
                  + w.str_elem("WritingApp", "casepack-builder 1.0"))


def _tracks(track_timecode_scale: float | None = None) -> bytes:
    t1 = w.elem("TrackEntry",
                w.uint_elem("TrackNumber", 1)
                + w.uint_elem("TrackUID", 101)
                + w.uint_elem("TrackType", 1)          # video
                + w.str_elem("CodecID", "V_FFV1")
                + w.str_elem("Name", "video-25fps")
                + w.uint_elem("DefaultDuration", VIDEO_DEFAULT_DURATION))
    t2_payload = (w.uint_elem("TrackNumber", 2)
                  + w.uint_elem("TrackUID", 202)
                  + w.uint_elem("TrackType", 2)        # audio
                  + w.str_elem("CodecID", "A_PCM/INT/LIT")
                  + w.str_elem("Name", "audio-pcm")
                  + w.uint_elem("DefaultDuration", AUDIO_DEFAULT_DURATION))
    if track_timecode_scale is not None:
        t2_payload += w.float_elem("TrackTimecodeScale", track_timecode_scale)
    return w.elem("Tracks", t1 + w.elem("TrackEntry", t2_payload))


def _cluster(timecode: int, blocks: list[bytes], unknown: bool = False) -> bytes:
    payload = w.uint_elem("Timecode", timecode) + b"".join(blocks)
    return w.elem("Cluster", payload, unknown=unknown, unknown_len=1)


def _cues(points: list[tuple[int, int, int]]) -> bytes:
    """points: list of (cue_time, cue_track, cluster_position)."""
    out = b""
    for cue_time, track, pos in points:
        out += w.elem("CuePoint",
                      w.uint_elem("CueTime", cue_time)
                      + w.elem("CueTrackPositions",
                               w.uint_elem("CueTrack", track)
                               + w.uint_elem("CueClusterPosition", pos)))
    return w.elem("Cues", out)


def _assemble(info: bytes, tracks: bytes, clusters: list[bytes],
              cue_points: list[tuple[int, int, int]],
              segment_unknown: bool = False) -> bytes:
    prefix = info + tracks
    offsets = []
    pos = len(prefix)
    for c in clusters:
        offsets.append(pos)
        pos += len(c)
    cues = _cues([(t, tr, offsets[i]) for (t, tr, i) in cue_points])
    payload = prefix + b"".join(clusters) + cues
    segment = w.elem("Segment", payload, unknown=segment_unknown,
                     unknown_len=8)
    return _header() + segment


def _frame(tag: bytes, n: int) -> bytes:
    return tag * n


def build_baseline(segment_unknown: bool = False,
                   cluster1_unknown: bool = False,
                   extra_cluster2_blocks: list[bytes] | None = None,
                   corrupt_lacing: bool = False,
                   discard_padding: int = 5_000_000,
                   cue_override: list[tuple[int, int, int]] | None = None,
                   track_timecode_scale: float | None = None,
                   cluster2_timecode: int = 240) -> bytes:
    info = _info()
    tracks = _tracks(track_timecode_scale)

    if corrupt_lacing:
        # xiph lace header claims sizes that exceed the payload
        bad_xiph = w.elem("SimpleBlock",
                          bytes([0x81]) + (0).to_bytes(2, "big", signed=True)
                          + bytes([0x02]) + bytes([1]) + bytes([200])
                          + _frame(b"\x11", 10))
        # ebml lace whose diff drives a frame size negative
        bad_ebml = w.elem("SimpleBlock",
                          bytes([0x82]) + (2).to_bytes(2, "big", signed=True)
                          + bytes([0x06]) + bytes([2])
                          + w.size_vint(10)
                          + bytes([0x80 | 0x01])  # diff = 1 - 63 = -62
                          + _frame(b"\x22", 10))
        c0_blocks = [bad_xiph,
                     w.simple_block(2, 2, [_frame(b"\xa0", 8)])]
        c1_blocks = [bad_ebml]
    else:
        c0_blocks = [
            w.simple_block(1, 0, [_frame(b"\x01", 12), _frame(b"\x02", 12)],
                           lacing="xiph"),
            w.simple_block(2, 2, [_frame(b"\xa0", 8)]),
            w.block_group(1, 80, [_frame(b"\x03", 16)], duration=80,
                          discard_padding=discard_padding),
        ]
        c1_blocks = [
            w.simple_block(1, 0, [_frame(b"\x04", 10), _frame(b"\x05", 10),
                                  _frame(b"\x06", 10)], lacing="fixed"),
            w.simple_block(2, 5, [_frame(b"\xb0", 6), _frame(b"\xb1", 6)],
                           lacing="ebml"),
        ]
    cluster0 = _cluster(0, c0_blocks)
    cluster1 = _cluster(120, c1_blocks, unknown=cluster1_unknown)
    c2_blocks = [w.simple_block(1, 0, [_frame(b"\x07", 10)])]
    if extra_cluster2_blocks:
        c2_blocks += extra_cluster2_blocks
    cluster2 = _cluster(cluster2_timecode, c2_blocks)

    cue_points = cue_override if cue_override is not None else [
        (0, 1, 0), (120, 1, 1), (cluster2_timecode, 1, 2),
    ]
    return _assemble(info, tracks, [cluster0, cluster1, cluster2],
                     cue_points, segment_unknown=segment_unknown)


def build_variant(name: str) -> bytes:
    if name == "baseline":
        return build_baseline()
    if name == "unknown_length":
        return build_baseline(segment_unknown=True, cluster1_unknown=True)
    if name == "rel_tc_overflow":
        extra = [w.simple_block(1, -32768, [_frame(b"\x08", 10)]),
                 w.simple_block(2, -300, [_frame(b"\xc0", 8)])]
        return build_baseline(extra_cluster2_blocks=extra)
    if name == "lacing_corrupt":
        return build_baseline(corrupt_lacing=True)
    if name == "discard_padding":
        # discard padding larger than the block duration, plus a negative one
        extra = [w.block_group(2, 40, [_frame(b"\xd0", 8)], duration=20,
                               discard_padding=-10_000_000)]
        return build_baseline(discard_padding=200_000_000,
                              extra_cluster2_blocks=extra)
    if name == "cue_mispoint":
        return build_baseline(cue_override=[(0, 1, 0), (999, 1, 0),
                                            (240, 1, 2)])
    if name == "track_timebase":
        return build_baseline(track_timecode_scale=0.5)
    if name == "cluster_gap":
        return build_baseline(cluster2_timecode=5000)
    raise KeyError(name)


VARIANTS = ["baseline", "unknown_length", "rel_tc_overflow", "lacing_corrupt",
            "discard_padding", "cue_mispoint", "track_timebase",
            "cluster_gap"]

VARIANT_LABELS = {
    "baseline": "基线：偏移/未知长度/轨道参数/时码/discard padding/cue/字节范围全保留",
    "unknown_length": "未知长度：Segment 与 Cluster#1 使用 unknown size",
    "rel_tc_overflow": "relative timecode 溢出：有符号 int16 相对时码使绝对时码为负",
    "lacing_corrupt": "lacing 损坏：xiph/ebml lace 尺寸与负载不符",
    "discard_padding": "discard padding：超过块时长与负值两种情况",
    "cue_mispoint": "cue 错指：CuePoint 指向错误的 cluster 偏移/时间",
    "track_timebase": "轨道时基差异：Track#2 TrackTimecodeScale=0.5",
    "cluster_gap": "中途 cluster 缺口：Cluster#2 时码跳到 5000",
}


def manifest() -> dict:
    return {
        "case": "Matroska 时码庭：cluster、cue 索引与多轨帧时间证据",
        "story": ("媒体工程团队导入 Matroska/WebM 结构和帧摘要，审查 "
                  "Segment/Cluster、TimecodeScale、BlockGroup、lacing 与 cue "
                  "的时间对应。"),
        "variants": {name: VARIANT_LABELS[name] for name in VARIANTS},
        "steps": [
            {"id": "import", "label": "导入基线案例包"},
            {"id": "param_revision", "label": "参数修订：cue 容差 5 -> 50 单位"},
            {"id": "source_change", "label": "局部源数据改动：修正 CuePoint#2 时间"},
            {"id": "conflict", "label": "两位审阅者对 cue:1 裁定互相冲突"},
            {"id": "replay", "label": "重放：依次经过七种边界情形"},
        ],
    }


def build_all(dest: str = FIXTURES_DIR) -> list[str]:
    os.makedirs(dest, exist_ok=True)
    written = []
    for name in VARIANTS:
        path = os.path.join(dest, name + ".mkv")
        with open(path, "wb") as fh:
            fh.write(build_variant(name))
        written.append(path)
    mpath = os.path.join(dest, "casepack.json")
    with open(mpath, "w", encoding="utf-8") as fh:
        json.dump(manifest(), fh, ensure_ascii=False, indent=2)
    written.append(mpath)
    return written


def load_fixture(name: str) -> bytes:
    path = os.path.join(FIXTURES_DIR, name + ".mkv")
    if not os.path.exists(path):
        build_all()
    with open(path, "rb") as fh:
        return fh.read()


if __name__ == "__main__":
    for p in build_all():
        print(p)
