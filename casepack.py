"""Offline case pack: "Matroska 时码庭：cluster、cue 索引与多轨帧时间证据".

Builds a deterministic Matroska/EBML fixture that contains every boundary
condition under review, then replays the case narrative:

1. baseline import (all raw evidence preserved)
2. one parameter revision (TimecodeScale)
3. one local source change (repair cluster:2 lacing bytes)
4. two conflicting human judgments on the cue mis-point anomaly

The fixture exercises: unknown element size, signed relative timecode
overflow, corrupt lacing, discard padding (incl. negative), cue mis-point,
divergent track timebases and a mid-stream cluster timecode gap.
"""
from __future__ import annotations

import json
from pathlib import Path

import ebml
from ebml import el, el_unknown_size, sint_el, str_el, uint_el
from store import ConflictError, Store
import service

FIXTURE_NAME = "case_alpha.webm"
PACK_NAME = "case_pack.json"

EXPECTED_ANOMALY_KINDS = [
    "unknown_size",
    "rel_overflow",
    "lacing_corrupt",
    "discard_padding",
    "cue_mispoint",
    "track_timebase_divergence",
    "cluster_gap",
]

RULES = {
    "signed-relative-timecode": "relative timecode 按有符号位宽解释",
    "lacing-no-guess": "lacing 损坏时不猜帧边界",
    "cue-is-evidence": "cue 是索引证据而不是时间真值",
    "no-file-order-alignment": "多轨不按文件顺序强对齐",
}


def _block(track: int, rel: int, flags: int, body: bytes) -> bytes:
    return (bytes([0x80 | track])
            + rel.to_bytes(2, "big", signed=True)
            + bytes([flags]) + body)


def _xiph_body(sizes: list[int], payload: bytes) -> bytes:
    out = bytes([len(sizes)])  # num_frames - 1
    for s in sizes:
        out += bytes([s])
    return out + payload


def build_fixture_bytes() -> bytes:
    header = el(0x1A45DFA3, b"".join([
        uint_el(0x4286, 1), uint_el(0x42F7, 1), uint_el(0x42F2, 4),
        uint_el(0x42F3, 8), str_el(0x4282, "matroska"),
        uint_el(0x4287, 4), uint_el(0x4285, 2),
    ]))
    info = el(0x1549A966, uint_el(0x2AD7B1, 1_000_000))
    tracks = el(0x1654AE6B, b"".join([
        el(0xAE, b"".join([
            uint_el(0xD7, 1), uint_el(0x83, 1), str_el(0x536E, "V1-video"),
            uint_el(0x23E383, 33_333_333),
        ])),
        el(0xAE, b"".join([
            uint_el(0xD7, 2), uint_el(0x83, 2), str_el(0x536E, "A1-audio"),
            uint_el(0x23E383, 21_333_333),
        ])),
    ]))

    # cluster 0: clean xiph-laced video block + audio simple block
    c0 = el(0x1F43B675, b"".join([
        uint_el(0xE7, 0),
        el(0xA0, el(0xA1, _block(1, 0, 0x02,
                                 _xiph_body([4, 4], b"v0f0v0f1v0f2")))),
        el(0xA3, _block(2, 0, 0x80, b"aud0")),
    ]))
    # cluster 1: signed relative timecode overflow + positive discard padding
    c1 = el(0x1F43B675, b"".join([
        uint_el(0xE7, 1000),
        el(0xA0, b"".join([
            el(0xA1, _block(1, -32768, 0x00, b"OVERFLOW!!")),
            sint_el(0x75A2, 5000, 2),
        ])),
        el(0xA3, _block(2, 40, 0x80, b"aud1")),
    ]))
    # cluster 2: corrupt xiph lacing (sizes 5+5 exceed 4-byte payload)
    #            + negative discard padding
    c2 = el(0x1F43B675, b"".join([
        uint_el(0xE7, 2000),
        el(0xA0, b"".join([
            el(0xA1, _block(1, 0, 0x02, _xiph_body([5, 5], b"DATA"))),
            sint_el(0x75A2, -40, 1),
        ])),
    ]))
    # cluster 3: unknown-size element, timecode jumps 2000 -> 9000 (gap)
    c3 = el_unknown_size(0x1F43B675, b"".join([
        uint_el(0xE7, 9000),
        el(0xA3, _block(1, 0, 0x80, b"v3!!")),
        el(0xA3, _block(2, 0, 0x80, b"aud3")),
    ]))
    clusters = [c0, c1, c2, c3]

    def build_cues(pos_cluster0: int) -> bytes:
        return el(0x1C53BB6B, b"".join([
            el(0xBB, b"".join([
                uint_el(0xB3, 0),
                el(0xB7, b"".join([
                    uint_el(0xF7, 1), uint_el(0xF1, pos_cluster0, 8)])),
            ])),
            # mis-pointed cue: claims time 1000 but points at cluster 0
            el(0xBB, b"".join([
                uint_el(0xB3, 1000),
                el(0xB7, b"".join([
                    uint_el(0xF7, 1), uint_el(0xF1, pos_cluster0, 8)])),
            ])),
        ]))

    cues_probe = build_cues(0)
    cluster0_pos = len(info) + len(tracks) + len(cues_probe)
    cues = build_cues(cluster0_pos)
    assert len(cues) == len(cues_probe)

    segment = el(0x18538067, info + tracks + cues + b"".join(clusters))
    return header + segment


def ensure_fixtures(fixtures_dir) -> Path:
    fixtures_dir = Path(fixtures_dir)
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    fixture = fixtures_dir / FIXTURE_NAME
    if not fixture.exists():
        fixture.write_bytes(build_fixture_bytes())
    pack = fixtures_dir / PACK_NAME
    if not pack.exists():
        pack.write_text(json.dumps({
            "title": "Matroska 时码庭：cluster、cue 索引与多轨帧时间证据",
            "fixture": FIXTURE_NAME,
            "narrative": [
                "1 基线导入：EBML 元素偏移、未知长度、track 参数、"
                "cluster/block 时码、discard padding、cue 和原字节范围均保留",
                "2 参数修订：TimecodeScale 1ms -> 2ms",
                "3 源数据改动：修复 cluster:2 的 lacing 字节（局部依赖重算）",
                "4 人工决定冲突：两位审阅者对 cue 错指作出相反判定，"
                "系统保留两个分支",
            ],
            "boundary_conditions": EXPECTED_ANOMALY_KINDS,
            "rules": RULES,
            "units": {
                "timecode": "TimecodeScale units",
                "absolute_time": "ns",
                "default_duration": "ns",
                "discard_padding": "TimecodeScale units",
                "offsets": "byte offset",
            },
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    return fixture


def replay(store: Store, fixture_path) -> dict:
    """Run the scripted case narrative on a fresh store."""
    v1 = service.import_fixture(store, fixture_path, label="基线导入")
    v2 = service.revise_params(
        store, v1, {"timecode_scale_ns": 2_000_000},
        note="TimecodeScale 1ms -> 2ms，全量时间派生重算", reviewer="reviewer-A")

    payload = store.get_version(v2)["payload"]
    block = payload["nodes"]["cluster:2"]["blocks"][0]
    assert block["lacing_corrupt"]
    # lace-size bytes sit after track(1) + timecode(2) + flags(1) + count(1)
    patch_off = block["data_offset"] + 5
    v3 = service.patch_source(
        store, v2, patch_off, bytes([2, 1]),
        note="修复 cluster:2 xiph lace sizes 5,5 -> 2,1（仅局部依赖重算）",
        reviewer="reviewer-A")

    cue_anomaly = "anom:cue_mispoint:cue:1"
    v4 = service.judge(
        store, v3, cue_anomaly, "accept", candidate_id="cue:1:parsed",
        note="采纳被指向 cluster 的解析时码；cue 仅为索引证据",
        reviewer="reviewer-A", rules=["cue-is-evidence"])
    try:
        service.judge(store, v3, cue_anomaly, "reject",
                      note="认为 cue 声明时间更可信", reviewer="reviewer-B",
                      rules=["cue-is-evidence"])
        raise AssertionError("expected ConflictError")
    except ConflictError:
        v5 = service.judge(
            store, v3, cue_anomaly, "reject",
            note="认为 cue 声明时间更可信（冲突，保存为分支）",
            reviewer="reviewer-B", rules=["cue-is-evidence"],
            allow_branch=True)
    service.select_branch(store, v4, reviewer="reviewer-A",
                          note="继续 reviewer-A 分支；reviewer-B 分支保留")
    return {
        "versions": {"baseline": v1, "param_revision": v2,
                     "source_patch": v3, "judgment_A": v4, "judgment_B": v5},
        "current": store.current_version_id(),
        "anomaly": cue_anomaly,
    }
