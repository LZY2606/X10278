"""Offline case pack: "Matroska 时码庭 — cluster、cue 索引与多轨帧时间证据".

Builds the full narrative as a sequence of immutable versions:

  v1  baseline import   -- EBML offsets, unknown sizes, track params,
                           cluster/block timecodes, discard padding, cues
                           and raw byte ranges all preserved
  v2  parameter revision (TimecodeScale override) -- partial recompute
  v3  local source change (frame summary for one block) -- local deps only
  v4  two reviewers edit v3 -> conflict -> resolution (both branches kept)
  s*  counterexample archive: one direct source mutated per scenario
"""
from __future__ import annotations

from . import build, derive, model
from .store import Store

CASE_TITLE = "Matroska 时码庭：cluster、cue 索引与多轨帧时间证据"

RULE_VERDICTS = [
    {"rule": "signed-rel-timecode",
     "statement": "relative timecode 按有符号位宽解释",
     "adopted": {"interpretation": "signed-int16",
                 "example": {"raw": "0x8000", "value": -32768}},
     "rejected": [{"interpretation": "uint16",
                   "example": {"raw": "0x8000", "value": 32768}}],
     "basis": "Matroska Block timecode is a signed 16-bit big-endian integer "
              "relative to the Cluster Timecode"},
    {"rule": "no-guess-on-corrupt-lacing",
     "statement": "lacing 损坏时不猜帧边界",
     "adopted": {"policy": "keep-block-unsegmented",
                 "frames": "unknown"},
     "rejected": [{"policy": "split-evenly"}, {"policy": "use-default-duration"}],
     "basis": "corrupt lace sizes make every boundary hypothesis unverifiable; "
              "the block is kept whole and flagged"},
    {"rule": "cue-is-evidence-not-truth",
     "statement": "cue 是索引证据而不是时间真值",
     "adopted": {"role": "index-evidence",
                 "on_conflict": "cluster/block timecodes win"},
     "rejected": [{"role": "time-truth"}],
     "basis": "Cues are a seek index written by the muxer; a mispointing cue "
              "must not move any frame in time"},
    {"rule": "no-file-order-alignment",
     "statement": "多轨不按文件顺序强对齐",
     "adopted": {"alignment": "by-timecode-only"},
     "rejected": [{"alignment": "by-file-order"}],
     "basis": "interleave order in the file is a muxing detail, not a time axis"},
]


def _derive_artifacts(source: bytes, params: dict, prev=None, changed=None,
                      frame_summary=None):
    results = derive.run(source, params, prev=prev, changed=changed,
                         frame_summary=frame_summary)
    artifacts = {"source": derive.encode_source(source), "params": params}
    if frame_summary:
        artifacts["frame_summary"] = frame_summary
    for node, res in results.items():
        artifacts[f"node:{node}"] = res
    return artifacts, results


def build_case(store: Store) -> dict:
    """Create the whole case in `store` (expected empty). Returns a map of
    version ids for the tour."""
    ids: dict[str, str] = {}

    # ---- v1: baseline import -------------------------------------------------
    source = build.sample_file()
    artifacts, results = _derive_artifacts(source, {})
    preserved = {
        "ebml_element_offsets": True, "unknown_sizes": True,
        "track_parameters": True, "cluster_block_timecodes": True,
        "discard_padding": True, "cues": True, "raw_byte_ranges": True,
    }
    v1 = store.save_version(
        None, "v1 基线导入：Matroska/WebM 结构 + 帧摘要", artifacts,
        [("import", {"case": CASE_TITLE, "file_size": len(source),
                     "preserved": preserved}),
         *[( "rule_verdict", rv) for rv in RULE_VERDICTS]])
    ids["v1"] = v1

    # ---- v2: parameter revision ----------------------------------------------
    params2 = {"timecode_scale_override_ns": 2_000_000}
    prev_nodes = {n: artifacts[f"node:{n}"] for n in derive.ORDER}
    artifacts2, _ = _derive_artifacts(source, params2, prev=prev_nodes,
                                      changed={"param:timecode_scale_override_ns"})
    v2 = store.save_version(
        v1, "v2 参数修订：TimecodeScale 覆盖 1ms -> 2ms（审阅假设）", artifacts2,
        [("param_revision",
          {"param": "timecode_scale_override_ns", "old": None, "new": 2_000_000,
           "unit": "ns", "reason": "审阅者核对采集端报告的 2ms tick 假设",
           "affected_nodes": [n for n in derive.ORDER
                              if artifacts2[f"node:{n}"]["status"] == "recomputed"],
           "reused_nodes": [n for n in derive.ORDER
                            if artifacts2[f"node:{n}"]["status"] == "reused"]})])
    ids["v2"] = v2

    # ---- v3: local source change (frame summary for the corrupt-lace drill) --
    frame_summary = {"clu:1/b0": {"sizes": [40, 40, 40], "unit": "byte",
                                  "origin": "external-frame-summary",
                                  "role": "evidence-candidate"}}
    prev_nodes2 = {n: artifacts2[f"node:{n}"] for n in derive.ORDER}
    artifacts3, _ = _derive_artifacts(source, params2, prev=prev_nodes2,
                                      changed={"frame_summary"},
                                      frame_summary=frame_summary)
    v3 = store.save_version(
        v2, "v3 源数据局部改动：导入 clu:1/b0 的外部帧摘要（仅局部依赖重算）",
        artifacts3,
        [("source_mutation",
          {"kind": "frame_summary_import", "subject": "clu:1/b0",
           "changed_input": "frame_summary",
           "affected_nodes": [n for n in derive.ORDER
                              if artifacts3[f"node:{n}"]["status"] == "recomputed"],
           "reused_nodes": [n for n in derive.ORDER
                            if artifacts3[f"node:{n}"]["status"] == "reused"],
           "note": "帧摘要作为候选证据进入，不覆盖 EBML lacing 原值"})])
    ids["v3"] = v3

    # ---- v4/v5: conflicting human decisions -----------------------------------
    base_nodes = {n: artifacts3[f"node:{n}"] for n in derive.ORDER}
    art_a, _ = _derive_artifacts(source, params2, prev=base_nodes, changed=set())
    art_b, _ = _derive_artifacts(source, params2, prev=base_nodes, changed=set())
    v4a = store.save_version(
        v3, "v4a 审阅者甲：接受 cue:2 为错指并维持 cluster 时码", art_a,
        [("decision", {"subject": "cue:2", "verdict": "reject-cue",
                       "rationale": "cue 指向非 cluster 起点；cluster 时码保留",
                       "reviewer": "reviewer-A"})])
    ids["v4a"] = v4a
    conflict = None
    try:
        store.save_version(
            v3, "v4b 审阅者乙：按 cue:2 调整 clu:2 时码（与甲冲突）", art_b,
            [("decision", {"subject": "cue:2", "verdict": "accept-cue",
                           "rationale": "尝试以 cue 时间为准",
                           "reviewer": "reviewer-B"})])
    except Exception as exc:  # Conflict
        conflict = exc
    ids["conflict"] = f"{conflict.base_id} vs {conflict.current_id}" if conflict else "none"
    v5 = store.resolve_conflict(
        v3, "v5 冲突裁定：维持甲分支（cue 是索引证据不是时间真值）", art_a,
        [("decision", {"subject": "cue:2", "verdict": "reject-cue",
                       "rationale": "规则 cue-is-evidence-not-truth；乙分支保留未选",
                       "reviewer": "lead", "unchosen_candidate": "v4b"})])
    ids["v5"] = v5

    # ---- counterexample archive: one mutation per scenario --------------------
    ids["scenarios"] = {}
    prev_base = {n: artifacts[f"node:{n}"] for n in derive.ORDER}
    for spec in SCENARIOS:
        mutated = spec["mutate"]()
        arts, res = _derive_artifacts(mutated, {}, prev=prev_base,
                                      changed={"source"})
        diff = _subject_diff(artifacts, arts)
        contained = set(diff["changed_subjects"]) <= set(spec["expected_subjects"])
        vid = store.save_version(
            v1, f"反例 {spec['name']}：{spec['title']}", arts,
            [("scenario",
              {"name": spec["name"], "title": spec["title"],
               "mutation": spec["mutation"], "changed_source": spec["changed_source"],
               "affected_subjects": diff["changed_subjects"],
               "expected_subjects": spec["expected_subjects"],
               "impact_contained": contained,
               "adopted": spec["adopted"], "rejected": spec["rejected"]})],
            allow_behind=True)
        ids["scenarios"][spec["name"]] = vid
    return ids


def _subject_diff(base_art: dict, new_art: dict) -> dict:
    """Which stable subjects changed between two derivations."""
    def subjects(art):
        out = {}
        seg = art.get("node:segment", {}).get("data", {})
        for d in seg.get("diagnostics", []):
            out[d["subject"]] = d["code"]
        frames = art.get("node:frames", {}).get("data", {}).get("rows", [])
        for r in frames:
            out[r["id"]] = (r.get("abs_tick"), r.get("kind"))
        return out
    a, b = subjects(base_art), subjects(new_art)
    changed = sorted({k for k in set(a) | set(b)
                      if a.get(k) != b.get(k) and not k.startswith("param:")})
    return {"changed_subjects": changed}


# -- scenario mutations: each changes exactly one direct source ---------------

def _base_kwargs():
    return {}


def _scenario_unknown_size():
    return build.sample_file(unknown_size_cluster_b=True)


def _scenario_rel_overflow():
    return build.sample_file(audio_rel=32767)


def _scenario_lacing_corrupt():
    return build.sample_file(corrupt_lace=True)


def _scenario_discard_padding():
    return build.sample_file(discard_padding_ns=-3_000_000)


def _scenario_cue_mispoint():
    return build.sample_file(cue2_target="clu0")


def _scenario_track_timebase():
    return build.sample_file(track2_scale=0.5)


def _scenario_cluster_gap():
    return build.sample_file(cluster_c_timecode=9000)


SCENARIOS = [
    {"name": "unknown-size", "title": "未知长度 cluster",
     "mutation": "cluster B 改为 unknown-size vint",
     "changed_source": "ebml:cluster-B-size-vint",
     "expected_subjects": ["clu:1"],
     "adopted": {"extent": "inferred-from-valid-children"},
     "rejected": [{"extent": "extends-to-eof"}]},
    {"name": "rel-overflow", "title": "relative timecode 溢出边界",
     "mutation": "cluster B 音频块相对时码改为 int16 最大值 32767",
     "changed_source": "block:clu:1/b1-rel-timecode",
     "expected_subjects": ["clu:1/b1", "clu:1/b1/f0"],
     "adopted": {"interpretation": "signed-int16", "value": 32767},
     "rejected": [{"interpretation": "uint16-wrap", "value": -32769}]},
    {"name": "lacing-corrupt", "title": "lacing 损坏",
     "mutation": "cluster B 的 Xiph lace 表追加未终止 0xFF 序列",
     "changed_source": "block:clu:1/b0-lace-table",
     "expected_subjects": ["clu:1/b0"],
     "adopted": {"frames": "unknown", "policy": "no-guess"},
     "rejected": [{"policy": "split-evenly", "frames": 3}]},
    {"name": "discard-padding", "title": "discard padding 为负",
     "mutation": "BlockGroup DiscardPadding 改为 -3000000 ns",
     "changed_source": "blockgroup:clu:0/b2-discard-padding",
     "expected_subjects": ["clu:0/b2"],
     "adopted": {"trim": "block-start", "unit": "ns"},
     "rejected": [{"trim": "ignored"}]},
    {"name": "cue-mispoint", "title": "cue 错指",
     "mutation": "cue:2 改指 cluster A 的位置（时间不匹配）",
     "changed_source": "cue:2-cluster-position",
     "expected_subjects": ["cue:2"],
     "adopted": {"role": "index-evidence", "time_truth": "cluster-timecode"},
     "rejected": [{"role": "time-truth"}]},
    {"name": "track-timebase", "title": "轨道时基差异",
     "mutation": "track 2 TrackTimecodeScale 2.0 -> 0.5",
     "changed_source": "track:2-timecode-scale",
     "expected_subjects": ["trk:2"],
     "adopted": {"alignment": "per-track-timebase"},
     "rejected": [{"alignment": "force-file-order"}]},
    {"name": "cluster-gap", "title": "中途 cluster 缺口",
     "mutation": "cluster C 时码 900 -> 9000（缺口拉大）",
     "changed_source": "cluster:clu:2-timecode",
     "expected_subjects": ["clu:2", "clu:2/b0", "clu:2/b1",
                           "clu:2/b0/f0", "clu:2/b1/f0"],
     "adopted": {"gap": "reported-not-filled"},
     "rejected": [{"gap": "interpolated"}]},
]


_MUTATORS = {
    "unknown-size": _scenario_unknown_size,
    "rel-overflow": _scenario_rel_overflow,
    "lacing-corrupt": _scenario_lacing_corrupt,
    "discard-padding": _scenario_discard_padding,
    "cue-mispoint": _scenario_cue_mispoint,
    "track-timebase": _scenario_track_timebase,
    "cluster-gap": _scenario_cluster_gap,
}
for _spec in SCENARIOS:
    _spec["mutate"] = _MUTATORS[_spec["name"]]


def replay(store: Store) -> dict:
    """Re-derive every complete version from its recorded source+params and
    confirm the stored conclusions reproduce.  This is the 重放 record."""
    report = []
    for v in store.list_versions():
        src_b64 = store.get_artifact(v["id"], "source")
        params = store.get_artifact(v["id"], "params") or {}
        if src_b64 is None:
            continue
        source = derive.decode_source(src_b64)
        fs = store.get_artifact(v["id"], "frame_summary")
        fresh = derive.run(source, params, frame_summary=fs)
        ok = True
        mismatches = []
        for node in derive.ORDER:
            stored = store.get_artifact(v["id"], f"node:{node}")
            if stored is None:
                continue
            if stored["data"] != fresh[node]["data"]:
                ok = False
                mismatches.append(node)
        report.append({"version": v["id"], "message": v["message"],
                       "reproduced": ok, "mismatched_nodes": mismatches})
    return {"case": CASE_TITLE, "all_reproduced": all(r["reproduced"] for r in report),
            "versions": report,
            "scenarios_covered": [s["name"] for s in SCENARIOS]}


def provenance(store: Store, subject: str) -> dict:
    """Trace which material, which rule version and which decision caused
    the visible state of `subject` (从最终状态反查)."""
    hits = []
    for ev in store.list_events():
        payload = ev["payload"]
        text = str(payload)
        if subject in text:
            hits.append({"event": ev["id"], "kind": ev["kind"],
                         "version": ev["version_id"],
                         "message": store.get_version(ev["version_id"])["message"]
                         if store.get_version(ev["version_id"]) else None})
    return {"subject": subject, "events": hits}
