"""派生引擎:依赖图传播 + 指纹复用。

每个派生节点记录 inputs(依赖键)与 fingerprint(输入值的哈希)。
新版本只重算 fingerprint 变化的节点;未变节点整体复用并写明
“为何仍有效”,变化节点写明“为何需要重算”。
"""
from __future__ import annotations

import hashlib
import json

from . import ebml, matroska

DEFAULT_PARAMS = {
    "timecode_scale_ns": None,        # None => 采用文件 Info/TimecodeScale
    "track_scale_override": {},       # track_number -> float
    "cue_tolerance_ns": 1_000_000,    # cue 比对容差,单位 ns
    "exclude_clusters": [],           # 人工裁定排除的 cluster 序号
}


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)


def _fp(recipe: str, value) -> str:
    """值指纹:同一值 => 同一指纹。依赖图传播按值相等性截断:
    输入重算后值未变的节点,其下游视为未受影响。"""
    return hashlib.sha1((recipe + "|" + _canon(value)).encode()).hexdigest()


def normalize_params(params: dict | None) -> dict:
    out = dict(DEFAULT_PARAMS)
    if params:
        for k, v in params.items():
            if k in out:
                out[k] = v
    out["track_scale_override"] = {int(k): float(v)
                                   for k, v in out["track_scale_override"].items()}
    out["exclude_clusters"] = [int(i) for i in out["exclude_clusters"]]
    return out


def compute_nodes(data: bytes, params: dict | None) -> dict[str, dict]:
    """从原始字节 + 参数计算全部派生节点。返回 key -> node。"""
    params = normalize_params(params)
    nodes: dict[str, dict] = {}

    def add(key: str, recipe: str, value, input_keys: list[str]):
        nodes[key] = {
            "key": key,
            "value": value,
            "inputs": sorted(input_keys),
            "fingerprint": _fp(recipe, value),
        }

    # --- 来源层:原始表示完整保留 ---
    raw_value = {
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "unit": "byte",
        "basis": "file_offset",
    }
    add("raw:file", "raw-v1", raw_value, [])

    # --- 参数层 ---
    add("params:effective", "params-v1", dict(params), [])

    # --- EBML 结构层 ---
    roots, parse_diags = ebml.parse_file(data)
    tree = [ebml.element_to_dict(el) for el in roots]
    add("parse:ebml", "parse-v1",
        {"tree": tree, "diagnostics": parse_diags, "unit": "byte",
         "basis": "file_offset"}, ["raw:file"])

    segment = next((el for el in roots if el.name == "Segment"), None)
    seg_children = segment.children if segment else []
    seg_data_off = segment.data_offset if segment else 0

    info = next((c for c in seg_children if c.name == "Info"), None)
    file_scale = None
    if info:
        ts = ebml.first_child(info, "TimecodeScale")
        if ts and isinstance(ts.value, int):
            file_scale = ts.value
    effective_scale = params["timecode_scale_ns"] or file_scale \
        or matroska.DEFAULT_TIMECODE_SCALE_NS

    # --- 轨道参数层(原始值 + 生效值分列) ---
    track_params: dict[int, dict] = {}
    tracks_el = next((c for c in seg_children if c.name == "Tracks"), None)
    if tracks_el:
        for te in tracks_el.children:
            if te.name != "TrackEntry":
                continue
            num = (ebml.first_child(te, "TrackNumber") or ebml.Element(
                0, "", "uint", 0, 0, 0, 0, False, 0)).value
            if num is None:
                continue
            tts_el = ebml.first_child(te, "TrackTimecodeScale")
            dd_el = ebml.first_child(te, "DefaultDuration")
            raw_scale = tts_el.value if tts_el else None
            track_params[num] = {
                "track_number": num,
                "raw_track_timecode_scale": raw_scale,
                "raw_default_duration_ns": dd_el.value if dd_el else None,
                "effective_track_timecode_scale":
                    params["track_scale_override"].get(num, raw_scale if raw_scale is not None else 1.0),
                "effective_default_duration_ns": dd_el.value if dd_el else None,
                "unit": "ns",
                "basis": "DefaultDuration 单位 ns;TrackTimecodeScale 无量纲倍率",
            }
    for num, tp in track_params.items():
        add(f"track:{num}:params", "trackparams-v1", tp,
            ["parse:ebml", "params:effective"])

    # --- cluster / block 层 ---
    excluded = set(params["exclude_clusters"])
    cluster_keys: list[str] = []
    clusters_info: list[dict] = []
    cluster_els = [c for c in seg_children if c.name == "Cluster"]
    for idx, cel in enumerate(cluster_els):
        tc_el = ebml.first_child(cel, "Timecode")
        cluster_tc = tc_el.value if tc_el and isinstance(tc_el.value, int) else 0
        blocks = matroska.collect_blocks(cel, data)
        block_dicts = []
        for blk in blocks:
            block_dicts.append({
                "track_number": blk.track_number,
                "timecode_rel_ticks": blk.timecode_rel,
                "timecode_rel_basis": "signed int16, 相对 cluster 时码, 单位 tick",
                "offset": blk.offset,
                "end": blk.end,
                "lacing": blk.lacing,
                "keyframe": blk.keyframe,
                "simple": blk.simple,
                "corrupt": blk.corrupt,
                "diagnostics": blk.diagnostics,
                "block_duration_ticks": blk.block_duration,
                "discard_padding_ns": blk.discard_padding_ns,
                "raw_frame_area": blk.raw_frame_area,
                "frames": [{
                    "index": f.index, "offset": f.offset, "size": f.size,
                    "diagnostics": f.diagnostics,
                } for f in blk.frames],
            })
        value = {
            "index": idx,
            "offset": cel.offset,
            "end": cel.end,
            "unknown_size": cel.unknown_size,
            "timecode_ticks": cluster_tc,
            "excluded": idx in excluded,
            "blocks": block_dicts,
            "unit": "byte/tick",
            "basis": "offset 为文件字节偏移;timecode 单位 tick(TimecodeScale)",
        }
        key = f"cluster:{idx}"
        add(key, "cluster-v1", value, ["parse:ebml", "params:effective"])
        cluster_keys.append(key)
        clusters_info.append({"index": idx, "timecode": cluster_tc})

    # --- 轨道时间线层(多轨独立,不按文件顺序强对齐) ---
    timeline_keys: dict[int, str] = {}
    track_timelines: dict[int, list[matroska.Frame]] = {}
    for num, tp in track_params.items():
        frames_out = []
        frames_obj: list[matroska.Frame] = []
        for idx, cel in enumerate(cluster_els):
            if idx in excluded:
                continue
            tc_el = ebml.first_child(cel, "Timecode")
            cluster_tc = tc_el.value if tc_el and isinstance(tc_el.value, int) else 0
            for blk in matroska.collect_blocks(cel, data):
                if blk.track_number != num:
                    continue
                matroska.frame_timestamps_ns(
                    cluster_tc, blk, effective_scale,
                    tp["effective_track_timecode_scale"],
                    tp["effective_default_duration_ns"])
                for fr in blk.frames:
                    frames_obj.append(fr)
                    frames_out.append({
                        "cluster_index": idx,
                        "frame_index": fr.index,
                        "offset": fr.offset,
                        "size": fr.size,
                        "timecode_rel_ticks": blk.timecode_rel,
                        "timestamp_ns": fr.timestamp_ns,
                        "discard_padding_ns": blk.discard_padding_ns,
                        "keyframe": fr.keyframe,
                        "diagnostics": fr.diagnostics + blk.diagnostics,
                        "unit": "ns",
                        "basis": "(cluster_tc+signed_rel)*TimecodeScale*TrackTimecodeScale",
                    })
        # 按时间排序;无法转换的值留在原位置(文件顺序)并附诊断
        known = sorted((f for f in frames_out if f["timestamp_ns"] is not None),
                       key=lambda f: f["timestamp_ns"])
        unknown = [f for f in frames_out if f["timestamp_ns"] is None]
        for f in unknown:
            f["diagnostics"] = f["diagnostics"] + ["unconverted: 保留在原位置,不参与排序"]
        value = {
            "track_number": num,
            "timecode_scale_ns": effective_scale,
            "track_timecode_scale": tp["effective_track_timecode_scale"],
            "frames": known + unknown,
            "unit": "ns",
        }
        key = f"track:{num}:timeline"
        add(key, "timeline-v1", value,
            cluster_keys + [f"track:{num}:params", "params:effective"])
        timeline_keys[num] = key
        track_timelines[num] = frames_obj

    # --- cluster 缺口 ---
    default_dd = next((tp["effective_default_duration_ns"]
                       for tp in track_params.values()
                       if tp["effective_default_duration_ns"]), None)
    gaps = matroska.detect_cluster_gaps(clusters_info, default_dd, effective_scale)
    add("clusters:gaps", "gaps-v1", {"gaps": gaps, "unit": "ns"}, cluster_keys)

    # --- cue 校验(cue 是索引证据,不是时间真值) ---
    cues_raw = []
    cues_el = next((c for c in seg_children if c.name == "Cues"), None)
    if cues_el:
        for cp in cues_el.children:
            if cp.name != "CuePoint":
                continue
            t = ebml.first_child(cp, "CueTime")
            entry = {"cue_time": t.value if t else None,
                     "cue_time_offset": t.offset if t else None,
                     "cue_time_size": (t.end - t.data_offset) if t else None}
            for ctp in cp.children:
                if ctp.name != "CueTrackPositions":
                    continue
                ct = ebml.first_child(ctp, "CueTrack")
                cpos = ebml.first_child(ctp, "CueClusterPosition")
                if ct:
                    entry["cue_track"] = ct.value
                if cpos:
                    entry["cue_cluster_position"] = cpos.value
            cues_raw.append(entry)
    # 用派生帧时间做比对(不改写时间线)
    tl_for_cues: dict[int, list[matroska.Frame]] = {}
    for num in track_params:
        frs = []
        for idx, cel in enumerate(cluster_els):
            if idx in excluded:
                continue
            tc_el = ebml.first_child(cel, "Timecode")
            cluster_tc = tc_el.value if tc_el and isinstance(tc_el.value, int) else 0
            for blk in matroska.collect_blocks(cel, data):
                if blk.track_number != num:
                    continue
                matroska.frame_timestamps_ns(
                    cluster_tc, blk, effective_scale,
                    track_params[num]["effective_track_timecode_scale"],
                    track_params[num]["effective_default_duration_ns"])
                frs.extend(blk.frames)
        tl_for_cues[num] = frs
    cue_results = matroska.validate_cues(cues_raw, tl_for_cues, effective_scale,
                                         params["cue_tolerance_ns"])
    add("cues:validation", "cues-v1",
        {"cues": cue_results, "tolerance_ns": params["cue_tolerance_ns"],
         "segment_data_offset": seg_data_off, "unit": "ns",
         "note": "cue 是索引证据而不是时间真值;错指仅作诊断"},
        ["parse:ebml", "params:effective"] + sorted(timeline_keys.values()))

    # --- 诊断汇总 ---
    summary: dict[str, list] = {}
    def _cat(diag: str, where: str):
        kind = diag.split(":")[0]
        summary.setdefault(kind, []).append(where)
    for d in parse_diags:
        _cat(d, "parse")
    for key in cluster_keys:
        for blk in nodes[key]["value"]["blocks"]:
            for d in blk["diagnostics"]:
                _cat(d, f"{key}/track{blk['track_number']}@{blk['offset']}")
    for num, key in timeline_keys.items():
        for fr in nodes[key]["value"]["frames"]:
            for d in fr["diagnostics"]:
                _cat(d, f"track{num}/cluster{fr['cluster_index']}/frame{fr['frame_index']}")
    for c in cue_results:
        if c["verdict"] == "mispoint":
            _cat("cue_mispoint", f"cue#{c['cue_index']}")
    for g in gaps:
        _cat("cluster_gap", f"cluster{g['after_cluster']}->{g['before_cluster']}")
    add("diagnostics:summary", "summary-v1",
        {"by_kind": summary, "unit": "ns/byte"},
        ["parse:ebml"] + cluster_keys + sorted(timeline_keys.values())
        + ["cues:validation", "clusters:gaps"])

    return nodes


EDGE_CASE_RULES = [
    ("unknown_length", "EBML 元素长度未知时延伸至父元素末尾,原字节范围保留"),
    ("signed_relative_timecode", "relative timecode 按有符号位宽解释(int16),溢出回绕为负值是合法候选"),
    ("lacing_corrupt", "lacing 损坏时不猜帧边界,帧区原字节保留,帧时间标记 unknown"),
    ("discard_padding", "DiscardPadding 单位 ns,有符号,只影响帧尾裁剪,不改帧起点"),
    ("cue_mispoint", "cue 是索引证据而不是时间真值,错指只产生诊断"),
    ("track_timebase_differ", "轨道时基差异经 TrackTimecodeScale 规范化,多轨不按文件顺序强对齐"),
    ("cluster_gap", "中途 cluster 缺口只标记区间,不插值不补帧"),
]


def edge_case_events(nodes: dict[str, dict]) -> list[dict]:
    """从派生结果生成七类边界情形的机器可读判定事件 payload。"""
    summary = nodes["diagnostics:summary"]["value"]["by_kind"]
    events = []
    mapping = {
        "unknown_length": "unknown_size" in _all_diag_text(nodes),
        "signed_relative_timecode": any(
            blk["timecode_rel_ticks"] < -16384
            for key, node in nodes.items() if key.startswith("cluster:")
            for blk in node["value"]["blocks"]),
        "lacing_corrupt": "lacing_corrupt" in summary,
        "discard_padding": any(
            blk["discard_padding_ns"] is not None
            for key, node in nodes.items() if key.startswith("cluster:")
            for blk in node["value"]["blocks"]),
        "cue_mispoint": "cue_mispoint" in summary,
        "track_timebase_differ": any(
            key.endswith(":params")
            and node["value"]["effective_track_timecode_scale"] != 1.0
            for key, node in nodes.items()),
        "cluster_gap": "cluster_gap" in summary,
    }
    for kind, rule in EDGE_CASE_RULES:
        events.append({
            "kind": kind,
            "observed": bool(mapping.get(kind)),
            "rule": rule,
            "evidence": sorted(summary.get(kind.split("_")[0] + "_corrupt", []))
                        or sorted(summary.get(kind, [])),
        })
    return events


def _all_diag_text(nodes: dict[str, dict]) -> str:
    parts = []
    for node in nodes.values():
        parts.append(_canon(node["value"]))
    return " ".join(parts)
