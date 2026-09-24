"""生成"Matroska 时码庭"离线案例包（确定性，仅依赖仓库内 core）。

产物 tests/fixtures/matroska_court_v1/：
- baseline.mkv            基线：EBML 偏移/未知长度/track/cluster/block/
                           discard padding/cue/原字节范围全部齐备
- mutations/<kind>.mkv    七类单一来源变异
- scenario.json           导入→参数修订→局部源改动→冲突决定的重放剧本
- rulings.jsonl           机器可读的判断依据事件
- README.md               领域说明
"""
from __future__ import annotations

import json
from pathlib import Path

from core import builder as B
from core.blocks import LACING_XIPH, LACING_EBML, LACING_FIXED
from core.mutator import mutate, MUTATION_KINDS

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" \
    / "matroska_court_v1"


def build_baseline() -> tuple[bytes, dict]:
    """构造两轨（视频 40ms / 音频 20ms）、多簇、laced block、BlockGroup、
    DiscardPadding、Cue 的确定性基线。坐标基准全部可回算。"""
    info = B.el(B.INFO,
                B.uint(0x2AD7B1, 1_000_000) +
                B.flo(0x4489, 1200.0) +
                B.text(0x4D80, "mtt-court-fixture") +
                B.text(0x5741, "mtt-court-fixture-v1"))
    tracks = B.el(B.TRACKS,
                  B.track_entry(1, 101, 1, "V_VP8", 40_000_000, True)
                  + B.track_entry(2, 202, 2, "A_OPUS", 20_000_000))

    # cluster 0 @ t=0：xiph laced 视频三帧 + 音频 BlockGroup(含 DiscardPadding)
    v_laced = B.el(0xA3, B.block_payload(
        1, 0, [b"V00", b"V01", b"V02"], LACING_XIPH, keyframe=True))
    a_bg = B.block_group(2, 0, [b"A0000"], duration=20,
                         discard_padding=2_000_000, reference_block=0)
    c0 = B.cluster(0, v_laced + a_bg)

    # cluster 1 @ t=100 ticks (100ms)：含一个有符号负相对时码边界 (-100)
    # 与 ebml laced 两帧（验证三种 lacing 中的第二种）
    v_ebml = B.el(0xA3, B.block_payload(
        1, 0, [b"V10" * 2, b"V11" * 2], LACING_EBML, keyframe=True))
    v_neg = B.simple_block(1, -100, [b"VNEG"])   # 合法负相对时码
    a1 = B.block_group(2, 0, [b"A1000"], duration=20)
    c1 = B.cluster(100, v_ebml + v_neg + a1)

    # cluster 2 @ t=200：普通帧（fixed lacing 等长三帧）
    v_fixed = B.el(0xA3, B.block_payload(
        1, 0, [b"F0", b"F1", b"F2"], LACING_FIXED, keyframe=True))
    c2 = B.cluster(200, v_fixed + B.simple_block(2, 0, [b"A2000"]))

    # cluster 3 @ t=300：远离前面，留出后续 cluster_gap 变异空间
    c3 = B.cluster(300, B.simple_block(1, 0, [b"V30"])
                   + B.simple_block(1, 40, [b"V31"]))

    header = B.ebml_header("matroska", 4)
    seg_head_id = B.ebml if False else None
    # Segment 头长可由解析回读；这里用 builder 编码后计算
    seg_id = bytes.fromhex("18538067")
    # 占位：先构造，再回填 cue 位置
    seg_data_origin = len(header) + 4 + 8
    p0 = seg_data_origin + len(info) + len(tracks)
    p1 = p0 + len(c0)
    p2 = p1 + len(c1)
    p3 = p2 + len(c2)
    cues = B.cues([
        B.cue_point(0, 1, p0),
        B.cue_point(100, 1, p1),
        B.cue_point(200, 1, p2),
        B.cue_point(300, 1, p3, cue_duration=40),
        B.cue_point(100, 2, p1),          # 音频轨 cue
    ])
    body = info + tracks + c0 + c1 + c2 + c3 + cues
    blob = header + B.segment(body)
    meta = {"seg_data_origin": seg_data_origin,
            "cluster_positions": [p0, p1, p2, p3]}
    return blob, meta


RULINGS = [
    {"ruling_id": "R1",
     "topic": "relative_timecode_signed",
     "statement": "Block 相对时码按有符号位宽（16bit）解释：0x8000=-32768，"
                  "不是 32768；无法排除 65536 包裹时保留全部合法候选。",
     "evidence": ["core/blocks.py:signed_rel_timecode",
                  "core/domain.py:_resolve_rel"]},
    {"ruling_id": "R2",
     "topic": "lace_corrupt_no_guess",
     "statement": "lacing 头与剩余载荷不一致时不猜测帧边界：不输出任何无法由 "
                  "size 头严格推出的帧，只给损坏字节范围。",
     "evidence": ["core/blocks.py:parse_block"]},
    {"ruling_id": "R3",
     "topic": "cue_is_evidence_not_truth",
     "statement": "CueClusterPosition/CueTime 是索引证据；与 cluster 时码或"
                  "实际偏移不一致时记录分歧（mismatch/dangling），时间真值"
                  "始终以 cluster/block 为准。",
     "evidence": ["core/domain.py:_derive_cues"]},
    {"ruling_id": "R4",
     "topic": "multitrack_no_file_order_alignment",
     "statement": "每轨按自身时基独立成时间线；不按字节交错顺序强行对齐不同"
                  "轨道的帧，跨轨顺序仅作为证据并列。",
     "evidence": ["core/domain.py:derive"]},
    {"ruling_id": "R5",
     "topic": "unknown_size_boundary_proven",
     "statement": "未知长度元素的终点只能由下一个 Level-1 边界或 EOF 证明，"
                  "推断边界显式标注，不冒充声明长度。",
     "evidence": ["core/parser.py:_scan_unknown_container"]},
    {"ruling_id": "R6",
     "topic": "units_preserved",
     "statement": "TimecodeScale(ns/tick)、DefaultDuration(ns/frame)、"
                  "DiscardPadding(ns)、cue 位置(segment-data-relative bytes) "
                  "在事实层保留原始表示与坐标基准，派生层才规范化为 ns。",
     "evidence": ["core/ebml.py:ELEMENT_SPECS", "core/facts.py"]},
]


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "mutations").mkdir(exist_ok=True)
    blob, meta = build_baseline()
    (OUT / "baseline.mkv").write_bytes(blob)

    muts_meta = {}
    for kind in MUTATION_KINDS:
        m = mutate(blob, kind)
        (OUT / "mutations" / f"{kind}.mkv").write_bytes(m.blob)
        muts_meta[kind] = {
            "label": m.label,
            "changed_ranges": m.changed_ranges,
            "params": m.params,
            "description": m.description,
            "expected_blast_radius": _expected_radius(kind),
        }

    scenario = {
        "name": "Matroska 时码庭：cluster、cue 索引与多轨帧时间证据",
        "baseline": "baseline.mkv",
        "baseline_meta": meta,
        "steps": [
            {"seq": 1, "action": "import", "file": "baseline.mkv",
             "expect": {"cue_status": ["exact"] * 5}},
            {"seq": 2, "action": "param_revision",
             "new_scale_ns": 500_000, "reason": "muxer 写入的实际 tick=0.5ms",
             "expect": {"only_affected": ["timeline"],
                        "elements_unchanged": True}},
            {"seq": 3, "action": "local_source_change",
             "mutation": "cue_mispoint",
             "expect": {"only_affected": ["cues", "diagnostics"],
                        "cluster_timecode_unchanged": True}},
            {"seq": 4, "action": "conflicting_decisions",
             "decisions": [
                 {"actor": "reviewer-A", "decision": "accept",
                  "subject": "cue_mispoint",
                  "note": "接受：保留 dangling cue 证据但不修正时间"},
                 {"actor": "reviewer-B", "decision": "adjust",
                  "subject": "cue_mispoint",
                  "adjusted_params": {"cue_policy": "prefer_cluster_truth"},
                  "note": "调整：界面默认隐藏错指 cue 的时间标注"}]},
            {"seq": 5, "action": "replay_all_mutations",
             "mutations": MUTATION_KINDS},
            {"seq": 6, "action": "export_delete_import",
             "expect": {"stable_identities": True, "order_preserved": True,
                        "source_relations_preserved": True}},
        ],
        "mutations": muts_meta,
    }
    (OUT / "scenario.json").write_text(
        json.dumps(scenario, ensure_ascii=False, indent=2))
    with (OUT / "rulings.jsonl").open("w") as f:
        for r in RULINGS:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    (OUT / "README.md").write_text(_readme(meta))
    print("fixtures written to", OUT)


def _expected_radius(kind: str) -> list[str]:
    return {
        "unknown_len": ["目标 cluster 的 size 字段", "该 cluster 终点标注方式"],
        "rel_tc_overflow": ["单个 block 的 2 字节相对时码",
                            "该 block 的绝对时间候选"],
        "lace_corrupt": ["单个 laced block 的帧数字节",
                         "该 block 的帧边界结论"],
        "discard_padding": ["单个 BlockGroup 的内容",
                            "该块的 discard 标注"],
        "cue_mispoint": ["单个 CueClusterPosition 字段",
                         "该 cue 的 status/mismatch"],
        "timebase_diff": ["单个 TrackEntry 的 DefaultDuration",
                          "该轨帧时长估计"],
        "cluster_gap": ["单个 cluster 的 Timecode",
                        "相邻簇之间的缺口诊断"],
    }[kind]


def _readme(meta) -> str:
    return f"""# Matroska 时码庭离线案例包 v1

领域：Matroska/WebM 的 cluster、cue 索引与多轨帧时间证据审查。
本包不是通用表格：所有材料是真实可解析的 EBML 字节。

## 基线 baseline.mkv
- Segment data origin（CueClusterPosition 坐标基准）= {meta['seg_data_origin']}
- cluster 字节偏移 = {meta['cluster_positions']}
- TimecodeScale=1,000,000 ns/tick；视频轨 40ms/帧，音频轨 20ms/帧
- 含 xiph / ebml / fixed 三种 lacing、负相对时码、BlockGroup、
  DiscardPadding、5 个 cue（含第二轨）
- 保留：EBML 元素偏移、未知长度（变异后）、track 参数、cluster/block
  时码、discard padding、cue 与原字节范围

## 单一来源变异 mutations/*.mkv
每个文件只改一个直接来源，影响半径见 scenario.json 的
`expected_blast_radius`：
unknown_len / rel_tc_overflow / lace_corrupt / discard_padding /
cue_mispoint / timebase_diff / cluster_gap

## 机器可读判断依据 rulings.jsonl
R1 有符号相对时码；R2 lacing 损坏不猜边界；R3 cue 只是索引证据；
R4 多轨不按文件顺序强对齐；R5 未知长度终点须被证明；R6 单位与坐标保留。

## 重放
`.venv/bin/python scripts/replay_scenario.py`
"""


if __name__ == "__main__":
    main()
