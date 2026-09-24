"""离线案例包: “Matroska 时码庭:cluster、cue 索引与多轨帧时间证据”。

叙事(完整链路,不拆成无关按钮):
  v1 初始导入   —— EBML 偏移/未知长度/track 参数/cluster/block 时码/
                   discard padding/cue/原字节范围全部保留;七类边界情形各自
                   生成机器可读判定事件。
  v2 参数修订   —— cue 容差 1ms -> 5ms,只传播到 cues:validation 与汇总。
  v3 源数据改动 —— 只修补错指 cue 的 CueTime 字节(777 -> 180 tick),
                   时间线节点整体复用,仅局部依赖重算。
  v4/v5 冲突    —— 两位审阅者从 v3 分别作出决定:接受 lacing 损坏现状 /
                   排除 cluster 2。后保存者触发可解决冲突,未选候选保留。
"""
from __future__ import annotations

import json
import os

from . import derive, ebml
from .store import Store, ConflictError

FIXTURE = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                       "fixtures", "court_case.webm")
CASE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                        "cases", "matroska_timecode_court")


def load_fixture(path: str = FIXTURE) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def _find_mispointed_cue(data: bytes) -> dict:
    """定位错指 cue 的 CueTime 字节范围(源数据改动的直接目标)。"""
    roots, _ = ebml.parse_file(data)
    for el in ebml.walk(roots):
        if el.name == "CueTime" and el.value == 777:
            return {"offset": el.data_offset, "length": el.end - el.data_offset,
                    "old_value": 777, "new_value": 180,
                    "unit": "tick", "basis": "CueTime*TimecodeScale"}
    raise RuntimeError("fixture 中未找到错指 cue")


def seed_case(store: Store, fixture_path: str = FIXTURE) -> dict:
    data = load_fixture(fixture_path)
    versions: list[str] = []

    # --- v1 初始导入:原始数据全保留 + 七类边界判定事件 ---
    nodes = derive.compute_nodes(data, None)
    edge_events = [{
        "type": "edge_case_verdict",
        "payload": {**e, "rule_id": f"rule:{e['kind']}"},
    } for e in derive.edge_case_events(nodes)]
    v1_events = [{
        "type": "import",
        "payload": {
            "file": os.path.basename(fixture_path),
            "preserved": ["ebml_element_offsets", "unknown_length_marks",
                          "track_parameters", "cluster_block_timecodes",
                          "discard_padding", "cues", "raw_byte_ranges"],
            "unit_policy": "导入层保留原始表示,派生层才规范化;无法转换的值留在原位置并附诊断",
        },
    }] + edge_events
    r = store.save_version(None, "import", "v1 初始导入:原始数据全保留",
                           "media-eng", data, None, v1_events)
    versions.append(r["version_id"])

    # --- v2 参数修订:cue 容差 1ms -> 5ms ---
    r = store.save_version(versions[-1], "param_revision",
                           "v2 参数修订:cue 容差 1ms→5ms", "reviewer-a",
                           data, {"cue_tolerance_ns": 5_000_000},
                           [{"type": "param_revision",
                             "payload": {"param": "cue_tolerance_ns",
                                         "old": 1_000_000, "new": 5_000_000,
                                         "unit": "ns",
                                         "expect": "仅 cues:validation 与汇总重算"}}])
    versions.append(r["version_id"])

    # --- v3 源数据改动:只修补错指 cue 的 CueTime 字节 ---
    patch = _find_mispointed_cue(data)
    new_bytes = patch["new_value"].to_bytes(patch["length"], "big")
    patched = data[:patch["offset"]] + new_bytes + \
        data[patch["offset"] + patch["length"]:]
    r = store.save_version(versions[-1], "source_patch",
                           "v3 源数据改动:修补错指 cue (777→180 tick)",
                           "media-eng", patched,
                           {"cue_tolerance_ns": 5_000_000},
                           [{"type": "source_patch",
                             "payload": {"patch": patch,
                                         "expect": "时间线复用,仅 parse/cues/summary 重算"}}])
    versions.append(r["version_id"])

    # --- v4 审阅者 A:接受 lacing 损坏现状(不猜帧边界) ---
    r = store.save_version(versions[-1], "decision",
                           "v4 决定A:接受 lacing 损坏诊断,保留未知帧边界",
                           "reviewer-a", patched,
                           {"cue_tolerance_ns": 5_000_000},
                           [{"type": "decision",
                             "payload": {"action": "accept",
                                         "target": "cluster:2 lacing_corrupt",
                                         "basis": "lacing 损坏时不猜帧边界,未知状态合法"}}])
    versions.append(r["version_id"])

    # --- v5 审阅者 B 从 v3 出发:排除 cluster 2 —— 与 v4 冲突 ---
    conflict_id = None
    try:
        store.save_version(versions[2], "decision",
                           "v5 决定B:排除 cluster 2(exclude_clusters=[2])",
                           "reviewer-b", patched,
                           {"cue_tolerance_ns": 5_000_000, "exclude_clusters": [2]},
                           [{"type": "decision",
                             "payload": {"action": "adjust",
                                         "param": "exclude_clusters",
                                         "value": [2],
                                         "basis": "损坏 cluster 不参与时间线"}}],
                           base_version=versions[2])
    except ConflictError as exc:
        conflict_id = exc.conflict_id
        versions.append(exc.version_id)

    # 裁定:采纳 A(保持 v4 为当前),B 的版本作为未选候选永久保留
    if conflict_id:
        store.resolve_conflict(conflict_id, choose="a", actor="lead-reviewer")

    return {"versions": versions, "conflict_id": conflict_id,
            "current": store.current_version()}


def write_case_readme():
    os.makedirs(CASE_DIR, exist_ok=True)
    doc = {
        "case": "Matroska 时码庭:cluster、cue 索引与多轨帧时间证据",
        "fixture": "fixtures/court_case.webm",
        "edge_cases": [k for k, _ in derive.EDGE_CASE_RULES],
        "narrative": [
            "v1 初始导入:EBML 偏移/未知长度/track 参数/block 时码/discard padding/cue/原字节范围全保留",
            "v2 参数修订:cue 容差 1ms→5ms(局部重算)",
            "v3 源数据改动:修补错指 cue 的 CueTime 字节(时间线复用)",
            "v4/v5 冲突人工决定:接受损坏诊断 vs 排除 cluster 2(未选候选保留)",
        ],
        "rules": [rule for _, rule in derive.EDGE_CASE_RULES],
    }
    with open(os.path.join(CASE_DIR, "case.json"), "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)
    return doc


if __name__ == "__main__":
    store = Store(os.path.join(CASE_DIR, "case.db"))
    info = seed_case(store)
    write_case_readme()
    print(json.dumps(info, ensure_ascii=False, indent=2))
