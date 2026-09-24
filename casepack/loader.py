"""The offline case pack: "Matroska 时码庭 — cluster、cue 索引与多轨帧时间证据".

Narrative (one continuous flow, not disconnected buttons):

1.  baseline import -- every EBML offset, unknown size, track parameter,
    cluster/block timecode, discard padding, cue and raw byte range is
    preserved as immutable objects;
2.  a counter-example archive: seven branch versions, each flipping exactly
    one direct source fact, proving the impact stays inside the correct
    derivation scope; adopted and non-adopted results are both retained;
3.  a parameter revision (timeline end policy);
4.  a local source change (one mispointing cue) adopted onto the mainline;
5.  a stress import combining the remaining boundary conditions, emitting
    machine-readable anomaly events that cite the standing rules;
6.  two reviewers make conflicting human decisions from the same version;
    the conflict is recorded, resolved, and the unchosen branch kept.
"""
from __future__ import annotations

from mkv.builder import VARIANTS, build, build_variant
from mkv.parser import parse_file
from mkv.rules import RULE_VERSION

VARIANT_LABELS = {
    "unknown_length": "未知长度 cluster",
    "rel_tc_overflow": "relative timecode 溢出 (0x8000)",
    "lacing_corrupt": "lacing 损坏",
    "discard_padding_overrun": "discard padding 越界",
    "cue_mispoint": "cue 错指",
    "track_timebase": "轨道时基差异",
    "cluster_gap": "中途 cluster 缺口",
}

STRESS_PARAMS = {
    "unknown_cluster_size": True,
    "overflow_rel": -32768,
    "lacing_corrupt": True,
    "discard_padding_ns": 55_000_000,
    "cue_mispoint_bytes": 7,
    "track2_scale": 1.25,
    "cluster_gap": True,
}

DEFAULT_PARAMS = {"timecode_scale_ns": None,
                  "end_policy": "default_duration",
                  "cue_tolerance_bytes": 0}


def _params_object(params: dict) -> dict:
    return {"object_id": "params:review", "kind": "params",
            "data": dict(params)}


def _import_objects(source_name: str, data: bytes, params: dict) -> list[dict]:
    objects = parse_file(data, source_name)
    objects.append(_params_object(params))
    return objects


def _impact(store, version_id) -> dict:
    derivs = store.get_derivations(version_id)
    return {
        "recomputed": sorted(k for k, d in derivs.items()
                             if d["status"] == "recomputed"),
        "kept": sorted(k for k, d in derivs.items()
                       if d["status"] == "kept"),
    }


def _anomaly_events(objects, derivs, actor) -> list[dict]:
    """Turn derivation anomalies + parse diagnostics into machine-readable
    events carrying the adjudicated rule citations."""
    events = []
    for obj in objects:
        if obj["kind"] != "diagnostic":
            continue
        d = obj["data"]
        kind = d["kind"]
        if kind == "unknown_length":
            events.append({
                "kind": "observation.unknown_length", "actor": actor,
                "payload": {"element": d.get("element"), "offset": d.get("offset"),
                            "note": "unknown (streaming) size preserved as-is",
                            "diagnostic_id": obj["object_id"]}})
        elif kind == "corrupted_region":
            events.append({
                "kind": "anomaly.cluster_gap", "actor": actor,
                "payload": {"byte_range": d.get("byte_range"),
                            "note": "mid-file unparsable region; re-synced, "
                                    "range preserved", "rule": "R5",
                            "diagnostic_id": obj["object_id"]}})
        elif kind == "lacing_corrupt":
            events.append({
                "kind": "anomaly.lacing_corrupt", "actor": actor,
                "payload": {"element": d.get("element"),
                            "byte_range": d.get("byte_range"),
                            "lace_sizes_raw": d.get("lace_sizes_raw"),
                            "rule": "R2: lacing 损坏时不猜帧边界",
                            "diagnostic_id": obj["object_id"]}})
    for key, deriv in derivs.items():
        for anomaly in deriv["output"].get("anomalies", []):
            events.append({
                "kind": f"anomaly.{anomaly['kind']}", "actor": actor,
                "payload": anomaly | {"derivation": key,
                                      "rule_version": RULE_VERSION}})
    return events


def _track_timebase_events(objects, actor) -> list[dict]:
    events = []
    for obj in objects:
        if obj["kind"] == "track" and \
                float(obj["data"].get("track_timecode_scale") or 1.0) != 1.0:
            events.append({
                "kind": "observation.track_timebase", "actor": actor,
                "payload": {
                    "track_uid": obj["data"]["track_uid"],
                    "track_timecode_scale": obj["data"]["track_timecode_scale"],
                    "rule": "R4: 多轨不按文件顺序强对齐; each track keeps its "
                            "own timebase",
                    "object_id": obj["object_id"]}})
    return events


def load_case(store) -> dict:
    """Build the full case into ``store`` (must be empty). Returns a summary
    with the important version ids so tests and the UI can deep-link."""
    summary = {"counterexamples": {}}
    actor = "case-loader"

    # -- 1. baseline import -------------------------------------------------
    baseline_objects = _import_objects("baseline", build_variant("baseline"),
                                       DEFAULT_PARAMS)
    events = [{
        "kind": "import.baseline", "actor": actor,
        "payload": {
            "source": "baseline", "objects": len(baseline_objects),
            "sha256": next(o["data"]["sha256"] for o in baseline_objects
                           if o["kind"] == "source"),
            "preserved": ["ebml_offsets", "unknown_lengths", "track_params",
                          "cluster_block_timecodes", "discard_padding",
                          "cues", "raw_byte_ranges"]}}]
    events += _track_timebase_events(baseline_objects, actor)
    v1 = store.create_version(baseline_objects, events,
                              "baseline import (全部原始结构保留)", actor)
    summary["baseline"] = v1

    # -- 2. counter-example archive: one changed source fact per branch -----
    impacts = {}
    for variant in VARIANTS:
        if variant == "baseline":
            continue
        objects = _import_objects(variant, build_variant(variant),
                                  DEFAULT_PARAMS)
        branch = store.create_version(
            objects, [], f"counterexample: {VARIANT_LABELS[variant]}",
            actor, advance_current=False)
        impact = _impact(store, branch)
        impacts[variant] = impact
        adopted = variant == "cue_mispoint"
        store.add_events(branch, [{
            "kind": "counterexample.created", "actor": actor,
            "payload": {
                "variant": variant, "label": VARIANT_LABELS[variant],
                "changed_source_fact": VARIANTS[variant],
                "impact": impact, "adopted": adopted,
                "note": "只改动一个直接来源; 影响必须被限制在正确范围"}}])
        summary["counterexamples"][variant] = branch

    # -- 3. parameter revision ----------------------------------------------
    revised_params = dict(DEFAULT_PARAMS, end_policy="next_block_start")
    objects = [o for o in store.get_objects(v1)]
    objects = [o for o in objects if o["object_id"] != "params:review"]
    objects.append(_params_object(revised_params))
    v2 = store.create_version(objects, [{
        "kind": "param.revision", "actor": "reviewer:mei",
        "payload": {"param": "end_policy",
                    "old": "default_duration", "new": "next_block_start",
                    "unit": "ns",
                    "expected_impact": "track timelines only"}}],
        "参数修订: 块结束时间策略", "reviewer:mei")
    summary["param_revision"] = v2

    # -- 4. local source change adopted from the archive --------------------
    objects = _import_objects("cue_mispoint", build_variant("cue_mispoint"),
                              revised_params)
    v3 = store.create_version(objects, [{
        "kind": "source.mutation", "actor": "reviewer:mei",
        "payload": {"variant": "cue_mispoint",
                    "changed_source_fact": VARIANTS["cue_mispoint"],
                    "adopted_from": summary["counterexamples"]["cue_mispoint"],
                    "note": "单一来源改动: 第二个 cue 的 cluster 位置 +7 字节"}}],
        "源数据改动: cue 错指 (局部影响)", "reviewer:mei")
    summary["cue_mispoint_mainline"] = v3

    # -- 5. stress import: the remaining boundary conditions at once --------
    stress_objects = _import_objects("stress", build(STRESS_PARAMS),
                                     revised_params)
    stress_derivs_preview = None
    v4 = store.create_version(stress_objects, [{
        "kind": "import.stress", "actor": actor,
        "payload": {"source": "stress", "conditions": STRESS_PARAMS,
                    "note": "故意触发边界组合: 未知长度/相对时码溢出/lacing 损坏/"
                            "discard padding/cue 错指/轨道时基差异/cluster 缺口"}}],
        "压力导入: 边界条件组合", actor)
    stress_derivs = store.get_derivations(v4)
    store.add_events(v4, _anomaly_events(stress_objects, stress_derivs, actor)
                     + _track_timebase_events(stress_objects, actor))
    summary["stress"] = v4

    # -- 6. conflicting human decisions --------------------------------------
    lacing_block = None
    for anomaly in stress_derivs["timeline:track:222"]["output"]["anomalies"]:
        if anomaly["kind"] == "lacing_indeterminate":
            lacing_block = anomaly["block_id"]
    base_objects = store.get_objects(v4)

    def with_decision(verdict, reviewer, rationale, seq):
        decision = {"object_id": f"decision:{seq:04d}", "kind": "decision",
                    "data": {"subject": lacing_block, "verdict": verdict,
                             "reviewer": reviewer, "rationale": rationale,
                             "candidates": [
                                 {"label": "block_level_timing_only",
                                  "adopted": verdict == "accept"},
                                 {"label": "drop_block",
                                  "adopted": verdict == "reject"}]}}
        return base_objects + [decision]

    v5a = store.create_version(
        with_decision("accept", "reviewer:alice",
                      "块级时码可信, 帧边界保持 indeterminate (R2)", 1),
        [{"kind": "decision.accept", "actor": "reviewer:alice",
          "payload": {"subject": lacing_block,
                      "rule": "R2: lacing 损坏时不猜帧边界; 接受块级时间, "
                              "不猜帧边界"}}],
        "决定: 接受块级时间 (Alice)", "reviewer:alice")
    summary["decision_accept"] = v5a

    try:
        store.create_version(
            with_decision("reject", "reviewer:bob",
                          "无法确定帧边界的块应整体剔除", 2),
            [{"kind": "decision.reject", "actor": "reviewer:bob",
              "payload": {"subject": lacing_block,
                          "rule": "保守解释: 剔除无法定帧的块"}}],
            "决定: 剔除该块 (Bob)", "reviewer:bob",
            expected_base=v4)
        raise AssertionError("expected a conflict")
    except Exception as exc:  # ConflictError
        summary["conflict"] = exc.conflict_id
        summary["decision_reject_branch"] = exc.branch_version

    v6 = store.resolve_conflict(summary["conflict"], choice="left",
                                actor="reviewer:lead",
                                note="采纳 Alice: 规则 R2 明确不猜帧边界但保留块级时间; "
                                     "Bob 分支保留")
    summary["resolution"] = v6
    summary["impacts"] = impacts
    return summary
