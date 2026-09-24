"""Offline Matroska case fixtures (committed to the repository).

Baseline preserves: EBML element offsets, unknown-length markers (segment),
track parameters, cluster/block timecodes, discard padding, cues and original
byte ranges. Seven single-source mutations exercise each boundary condition.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass

from . import builder as B
from . import ebml


@dataclass
class BuiltCase:
    name: str
    description: str
    data: bytes
    markers: dict
    expected: dict


def _info(timecode_scale: int = 1_000_000) -> bytes:
    return B.master("1549a966", [
        B.el("2ad7b1", timecode_scale), B.el("4d80", "tc-court"),
        B.el("5741", "tc-court-fixture"),
    ])


def _build_parts(scale=1_000_000, video_dur=40_000_000,
                 track2_scale=None, gap=False, video_offset=0):
    tracks = B.tracks([
        B.track_entry(1, 1001, 1, "V_UNCOMPRESSED", default_duration=video_dur,
                      track_offset=video_offset),
        B.track_entry(2, 2002, 2, "A_PCM/INT/LIT",
                      default_duration=20_833_333, track_tc_scale=track2_scale),
    ])

    # Cluster A at tc=0
    vframe0 = bytes([0x10] * 8)
    aframe0 = bytes([0xAA] * 6)
    cl_a_blocks = [
        B.simple_block(1, 0, [vframe0], keyframe=True),
        B.block_group(2, 0, [aframe0], duration=20, discard=5_000_000),
    ]
    cl_a = B.cluster(0, cl_a_blocks)

    # Cluster B: video at tc=40 (contiguous with 40ms duration)
    vframe1 = bytes([0x11] * 8)
    aframe1 = bytes([0xAB] * 6)
    video_tc_b = 40 if not gap else 300   # video cluster time
    audio_tc_b = 20 if not gap else 300   # dense audio unless gap injected
    cl_b = B.cluster(video_tc_b, [
        B.simple_block(1, 0, [vframe1], keyframe=False),
        B.simple_block(2, audio_tc_b - video_tc_b, [aframe1], keyframe=True),
    ])

    parts = [_info(scale), tracks, cl_a, cl_b]
    layout = {"tc_b": video_tc_b, "cl_a": cl_a, "cl_b": cl_b}
    return parts, layout


def build_baseline() -> BuiltCase:
    parts, layout = _build_parts()
    seg = B.ebml_header() + B.segment(parts, unknown_size=True)
    # Cues appended after the (unknown-size) segment, with positions measured
    # from segment data start. Two-pass layout to get cluster offsets.
    seg_data = b"".join(parts)
    cl_a_off = seg_data.find(b"\x1f\x43\xb6\x75")
    # second cluster
    first = cl_a_off
    cl_b_off = seg_data.find(b"\x1f\x43\xb6\x75", first + 4)
    cue = B.cues([
        B.cue_point(0, 1, cl_a_off, rel_pos=0, duration=100),
        B.cue_point(40, 1, cl_b_off, rel_pos=0),
    ])
    data = seg + cue
    return BuiltCase(
        name="baseline",
        description="Two-track WebM: unknown-size segment, video+audio, "
                    "BlockGroup DiscardPadding, Xiph-capable blocks, cues.",
        data=data,
        markers={"cluster_a_pos": cl_a_off, "cluster_b_pos": cl_b_off},
        expected={
            "tracks": 2, "clusters": 2, "cues": 2,
            "unknown_lengths_min": 1, "discard_padding_blocks": 1,
        },
    )


def case_unknown_length() -> BuiltCase:
    # Cluster A also unknown-size (still bounded by grammar via next cluster).
    parts, layout = _build_parts()
    # rebuild cluster A unknown length
    vframe0 = bytes([0x10] * 8)
    aframe0 = bytes([0xAA] * 6)
    cl_a = B.cluster(0, [
        B.simple_block(1, 0, [vframe0]),
        B.block_group(2, 0, [aframe0], duration=20, discard=5_000_000),
    ], unknown_size=True)
    parts[2] = cl_a
    data = B.ebml_header() + B.segment(parts, unknown_size=True)
    return BuiltCase(
        "unknown_length", "Unknown-size Segment AND unknown-size Cluster.",
        data, {}, {"unknown_lengths_min": 2, "clusters": 2})


def case_tc_overflow() -> BuiltCase:
    # Relative timecode raw 0xFFFF => signed -1 (valid but extreme); keep both.
    parts, _ = _build_parts()
    vframe = bytes([0x11] * 8)
    parts[3] = B.cluster(100, [
        B.simple_block(1, -1,
                       [vframe]),
    ])
    data = B.ebml_header() + B.segment(parts, unknown_size=True)
    return BuiltCase(
        "tc_overflow",
        "Relative timecode at signed int16 extreme (-1/0xFFFF).",
        data, {}, {"overflow_blocks_min": 1})


def case_lacing_corrupt() -> BuiltCase:
    # Xiph lacing header: 3 frames, sizes 200/10/0 then almost no frame bytes.
    # Declared sizes overrun the payload, so boundaries cannot be trusted.
    track = bytes([0x81])              # track 1
    tc = (5).to_bytes(2, "big")        # relative timecode
    flags = bytes([0x80 | (1 << 1)])   # keyframe + Xiph lacing
    count = bytes([0x80 | 2])          # 3 frames (count-1 = 2)
    sizes = bytes([200, 10])           # xiph sizes for first two frames
    frame_bytes = b"\x01\x02\x03"    # far fewer than 210 declared bytes
    corrupted = track + tc + flags + count + sizes + frame_bytes
    bad_block = B.el("a3", raw=corrupted)
    parts, _ = _build_parts()
    parts[3] = B.cluster(40, [bad_block])
    data = B.ebml_header() + B.segment(parts, unknown_size=True)
    return BuiltCase(
        "lacing_corrupt",
        "Xiph lacing metadata overruns payload: boundaries not guessed.",
        data, {}, {"corrupt_blocks_min": 1})


def case_discard_padding() -> BuiltCase:
    parts, _ = _build_parts()
    # negative DiscardPadding trims presentation end
    aframe = bytes([0xAB] * 6)
    parts[3] = B.cluster(100, [
        B.block_group(2, 10, [aframe], duration=20, discard=-2_000_000),
    ])
    data = B.ebml_header() + B.segment(parts, unknown_size=True)
    return BuiltCase(
        "discard_padding",
        "Negative DiscardPadding (trim end), evidence retained per block.",
        data, {}, {"discard_padding_blocks_min": 1})


def case_cue_mispoint() -> BuiltCase:
    base = build_baseline()
    # rebuild with a cue pointing at a bogus cluster position
    parts, layout = _build_parts()
    seg_data = b"".join(parts)
    cl_a_off = seg_data.find(b"\x1f\x43\xb6\x75")
    cl_b_off = seg_data.find(b"\x1f\x43\xb6\x75", cl_a_off + 4)
    cue = B.cues([
        B.cue_point(0, 1, cl_a_off),
        B.cue_point(40, 1, cl_b_off + 5000),  # resolves to no cluster
    ])
    data = B.ebml_header() + B.segment(parts, unknown_size=True) + cue
    return BuiltCase(
        "cue_mispoint",
        "Second cue points to a wrong byte position (index evidence only).",
        data, {"cluster_b_pos": cl_b_off}, {"cue_mismatch_min": 1})


def case_track_timebase() -> BuiltCase:
    # Audio track uses TrackTimecodeScale 0.5 -> distinct time base. In cluster
    # B (video tc=40ms) the audio block sits at relative -20 raw ticks, which
    # normalises to (40 - 20) * 0.5 = 10ms for audio vs 40ms for video.
    parts, _ = _build_parts(track2_scale=0.5)
    aframe = bytes([0xAB] * 6)
    parts[3] = B.cluster(40, [
        B.simple_block(1, 0, [bytes([0x11] * 8)], keyframe=False),
        B.simple_block(2, -20, [aframe], keyframe=True),
    ])
    data = B.ebml_header() + B.segment(parts, unknown_size=True)
    return BuiltCase(
        "track_timebase",
        "Track 2 TrackTimecodeScale=0.5 with negative relative timecode: "
        "distinct per-track time base, not file-order aligned.",
        data, {}, {"distinct_timebases": True})


def case_cluster_gap() -> BuiltCase:
    parts, _ = _build_parts(gap=True)
    data = B.ebml_header() + B.segment(parts, unknown_size=True)
    return BuiltCase(
        "cluster_gap",
        "Cluster B jumps to tc=300ms producing a mid-stream gap.",
        data, {}, {"gaps_min": 1})


ALL_CASES = [
    build_baseline, case_unknown_length, case_tc_overflow,
    case_lacing_corrupt, case_discard_padding, case_cue_mispoint,
    case_track_timebase, case_cluster_gap,
]


def write_casepack(directory: str) -> dict:
    import os, json
    os.makedirs(directory, exist_ok=True)
    manifest = []
    for fn in ALL_CASES:
        c = fn()
        path = os.path.join(directory, c.name + ".mkv")
        with open(path, "wb") as f:
            f.write(c.data)
        manifest.append({
            "name": c.name, "description": c.description,
            "file": c.name + ".mkv", "byte_length": len(c.data),
            "markers": c.markers, "expected": c.expected,
        })
    with open(os.path.join(directory, "manifest.json"), "w") as f:
        json.dump({"cases": manifest}, f, indent=2)
    return {"cases": manifest}
