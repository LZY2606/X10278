"""派生层：把原始对象规范化为带单位/坐标基准的时间结论。

- 每个派生节点声明依赖（源对象 uid 或其它派生节点 uid）；
- 重算按依赖图传播，只更新受影响节点；
- 未受影响节点保留旧值并说明"为何仍有效"，受影响节点说明"为何重算"；
- 无法转换的值留在原位置并附诊断，不为画图丢掉。
"""
from __future__ import annotations

import numpy as np

SOURCE_KINDS = {"blob", "param", "element", "track", "cluster", "block", "cue", "damage"}


def plan_nodes(sources: dict) -> dict:
    """返回 node_uid -> (kind, deps) 的派生计划。"""
    nodes: dict[str, tuple[str, list[str]]] = {}
    blocks = [o for o in sources.values() if o.get("kind") == "block"]
    clusters = [o for o in sources.values() if o.get("kind") == "cluster"]
    tracks = [o for o in sources.values() if o.get("kind") == "track"]
    cues = [o for o in sources.values() if o.get("kind") == "cue"]
    for b in blocks:
        nodes[f"der:block_time:{b['uid']}"] = (
            "block_time", [b["uid"], b["cluster"], b["track"], "param:timecode_scale"])
        nodes[f"der:frames:{b['uid']}"] = ("frames", [b["uid"], b["track"]])
    for c in clusters:
        deps = [c["uid"]] + [f"der:block_time:{u}" for u in c["blocks"]]
        nodes[f"der:cluster_interval:{c['uid']}"] = ("cluster_interval", deps)
    for t in tracks:
        tblocks = [b["uid"] for b in blocks if b["track"] == t["uid"]]
        deps = [t["uid"]] + [f"der:block_time:{u}" for u in tblocks]
        nodes[f"der:timeline:{t['uid']}"] = ("timeline", deps)
    for c in cues:
        nodes[f"der:cue_check:{c['uid']}"] = ("cue_check", [c["uid"], "idx:elements"])
    nodes["der:gaps"] = ("gaps", [f"der:cluster_interval:{c['uid']}" for c in clusters])
    return nodes


def _block_time(block_uid: str, sources: dict) -> dict:
    b = sources[block_uid]
    cl = sources[b["cluster"]]
    t = sources[b["track"]]
    scale = sources["param:timecode_scale"]["raw"]
    ts = t.get("timecode_scale", 1.0)
    # (cluster_tc + rel_tc * TrackTimecodeScale) * TimecodeScale
    start_ns = (cl["timecode_raw"] + b["rel_tc_raw"] * ts) * scale
    dur_ns = None
    if b.get("block_duration_raw") is not None:
        dur_ns = b["block_duration_raw"] * scale * ts
    elif t.get("default_duration_raw") is not None:
        dur_ns = t["default_duration_raw"] * ts
    value = {
        "start_ns": start_ns,
        "end_ns": start_ns + dur_ns if dur_ns is not None else None,
        "unit": "ns",
        "basis": "segment_time",
        "start_ticks": cl["timecode_raw"] + b["rel_tc_raw"],
        "ticks_basis": "TimecodeScale",
        "track_timecode_scale": ts,
        "formula": "(cluster_tc + rel_tc*track_scale) * TimecodeScale",
    }
    diagnostic = None
    if b["rel_tc_raw"] < 0:
        diagnostic = (f"relative timecode 原始值 {b['rel_tc_hex']} 按有符号 int16 解释为 "
                      f"{b['rel_tc_raw']}，块起点早于 cluster 起点")
    return {"value": value, "diagnostic": diagnostic}


def _frames(block_uid: str, sources: dict, derived: dict) -> dict:
    b = sources[block_uid]
    t = sources[b["track"]]
    if b.get("lace_error") or b.get("frames") is None:
        return {
            "value": None,
            "diagnostic": ("lacing 损坏：不猜测帧边界，整块保留为未切分证据；"
                           f"声明尺寸 {b['lace_error'].get('declared_sizes')}，"
                           f"可用负载 {b['lace_error'].get('available')} 字节"
                           if b.get("lace_error") else "无帧数据"),
        }
    bt = derived[f"der:block_time:{block_uid}"]["value"]
    dd = t.get("default_duration_raw")
    ts = t.get("timecode_scale", 1.0)
    frames = []
    for i, (off, size) in enumerate(b["frames"]):
        f = {"index": i, "byte_range": [off, off + size], "unit": "byte", "basis": "file_offset"}
        if dd is not None and bt is not None:
            f["start_ns"] = bt["start_ns"] + i * dd * ts
            f["time_unit"] = "ns"
        else:
            f["start_ns"] = None
            f["diagnostic"] = "缺少 DefaultDuration，帧时间不可唯一转换，保留原始字节范围"
        frames.append(f)
    return {"value": {"frames": frames}, "diagnostic": None}


def _cluster_interval(cluster_uid: str, sources: dict, derived: dict) -> dict:
    c = sources[cluster_uid]
    scale = sources["param:timecode_scale"]["raw"]
    start_ns = c["timecode_raw"] * scale
    ends = []
    unknown = []
    for bu in c["blocks"]:
        bt = derived.get(f"der:block_time:{bu}", {}).get("value")
        if bt and bt.get("end_ns") is not None:
            ends.append(bt["end_ns"])
        else:
            unknown.append(bu)
    value = {
        "start_ns": start_ns,
        "end_ns": max(ends) if ends else None,
        "unit": "ns",
        "basis": "segment_time",
        "byte_range": [c["offset"], c["end"]],
        "byte_unit": "byte",
        "byte_basis": "file_offset",
    }
    diagnostic = None
    if unknown:
        diagnostic = f"块 {unknown} 的结束时间不可确定，区间右端只取可确定部分"
    if c.get("unknown_size"):
        note = "cluster 使用未知长度，字节范围由子元素扫描回填"
        diagnostic = f"{diagnostic}；{note}" if diagnostic else note
    return {"value": value, "diagnostic": diagnostic}


def _timeline(track_uid: str, sources: dict, derived: dict) -> dict:
    blocks = [o for o in sources.values()
              if o.get("kind") == "block" and o["track"] == track_uid]
    entries = []
    for b in blocks:
        bt = derived.get(f"der:block_time:{b['uid']}", {}).get("value")
        entries.append({
            "block": b["uid"],
            "start_ns": bt["start_ns"] if bt else None,
            "end_ns": bt.get("end_ns") if bt else None,
            "file_offset": b["raw_range"][0],
            "lacing": b["lacing"],
            "damaged": bool(b.get("lace_error")),
        })
    # 多轨不按文件顺序强对齐：每条轨各自按计算时间排序
    entries.sort(key=lambda e: (e["start_ns"] is None, e["start_ns"] or 0))
    return {
        "value": {
            "entries": entries,
            "unit": "ns",
            "basis": "segment_time",
            "policy": "按各轨自身时基的计算时间排序，不按文件顺序与其它轨强对齐",
        },
        "diagnostic": None,
    }


def _cue_check(cue_uid: str, sources: dict) -> dict:
    cue = sources[cue_uid]
    clusters = sorted((o for o in sources.values() if o.get("kind") == "cluster"),
                      key=lambda c: c["offset"])
    claimed = cue["claimed_abs_offset"]
    offsets = [[c["uid"], c["offset"]] for c in clusters]
    hit = next((c for c in clusters if c["offset"] == claimed), None)
    containing = next((c for c in clusters if c["offset"] <= claimed < (c["end"] or 0)), None)
    value = {
        "claimed_offset": claimed,
        "claimed_basis": "file_offset = Segment 数据起点 + CueClusterPosition",
        "cluster_offsets": offsets,
        "match": hit["uid"] if hit else None,
        "unit": "byte",
    }
    diagnostic = None
    if hit is None:
        where = (f"落在 {containing['uid']} 内部" if containing else "不在任何 cluster 内")
        diagnostic = (f"cue 指向偏移 {claimed} {where}，与任何 cluster 起点都不符；"
                      "cue 是索引证据而不是时间真值，不覆盖 block 时码链")
    return {"value": value, "diagnostic": diagnostic}


def _gaps(sources: dict, derived: dict) -> dict:
    clusters = sorted((o for o in sources.values() if o.get("kind") == "cluster"),
                      key=lambda c: c["timecode_raw"])
    starts = np.array([c["timecode_raw"] for c in clusters], dtype=np.int64)
    order = np.argsort(starts, kind="stable")
    clusters = [clusters[i] for i in order]
    gaps = []
    for prev, nxt in zip(clusters, clusters[1:]):
        pi = derived.get(f"der:cluster_interval:{prev['uid']}", {}).get("value")
        ni = derived.get(f"der:cluster_interval:{nxt['uid']}", {}).get("value")
        if not pi or not ni or pi.get("end_ns") is None:
            gaps.append({"between": [prev["uid"], nxt["uid"]],
                         "diagnostic": "区间端点不可确定，缺口宽度不可唯一转换"})
            continue
        gap = ni["start_ns"] - pi["end_ns"]
        if gap > 0:
            gaps.append({
                "between": [prev["uid"], nxt["uid"]],
                "start_ns": pi["end_ns"],
                "end_ns": ni["start_ns"],
                "duration_ns": int(gap),
                "unit": "ns",
                "basis": "segment_time",
                "diagnostic": "中途 cluster 缺口：不插值、不虚构帧",
            })
    return {"value": {"gaps": gaps}, "diagnostic": None}


def compute_derived(sources: dict, changed: set[str] | None = None,
                    prev: dict | None = None):
    """计算派生层。changed 为发生变化的源 uid 集合；prev 为上一版派生结果。

    返回 (derived, report)。report 说明每个节点是重算还是沿用及原因。
    """
    plan = plan_nodes(sources)
    derived: dict[str, dict] = {}
    if prev is None or changed is None:
        affected = set(plan)
        default_reason = "初始计算"
    else:
        # 反向依赖传播
        dependents: dict[str, list[str]] = {}
        for nid, (_, deps) in plan.items():
            for d in deps:
                dependents.setdefault(d, []).append(nid)
        affected = set()
        queue = [n for n, (_, deps) in plan.items() if deps and set(deps) & changed]
        while queue:
            nid = queue.pop()
            if nid in affected:
                continue
            affected.add(nid)
            queue.extend(dependents.get(nid, []))
        default_reason = ""

    report = {"recomputed": [], "kept": []}
    # 拓扑序：按依赖深度反复推进
    pending = dict(plan)
    guard = 0
    while pending and guard < 10:
        guard += 1
        for nid in list(pending):
            kind, deps = pending[nid]
            if any(d in pending for d in deps):
                continue
            del pending[nid]
            if nid in affected:
                if kind == "block_time":
                    r = _block_time(nid.rsplit(":", 1)[1], sources)
                elif kind == "frames":
                    r = _frames(nid.rsplit(":", 1)[1], sources, derived)
                elif kind == "cluster_interval":
                    r = _cluster_interval(nid.rsplit(":", 1)[1], sources, derived)
                elif kind == "timeline":
                    r = _timeline(nid.rsplit(":", 1)[1], sources, derived)
                elif kind == "cue_check":
                    r = _cue_check(nid.rsplit(":", 1)[1], sources)
                else:
                    r = _gaps(sources, derived)
                why = default_reason or f"依赖变化：{sorted(set(deps) & changed)}"
                derived[nid] = {"uid": nid, "kind": "derived", "node": kind,
                                "deps": deps, "note": f"重算（{why}）", **r}
                report["recomputed"].append({"uid": nid, "reason": why})
            else:
                old = prev[nid]
                unchanged = sorted(set(deps) - changed)
                derived[nid] = {**old,
                                "note": f"仍有效：依赖未变化（{', '.join(unchanged) or '无'}）"}
                report["kept"].append({"uid": nid, "reason": "依赖未变化"})
    return derived, report


def diff_sources(old: dict, new: dict) -> set[str]:
    """两个版本源对象集合的差集（只看源类型）。"""
    import json
    changed = set()
    for uid, obj in new.items():
        if obj.get("kind") not in SOURCE_KINDS:
            continue
        o = old.get(uid)
        if o is None or json.dumps(o, sort_keys=True) != json.dumps(obj, sort_keys=True):
            changed.add(uid)
    for uid, obj in old.items():
        if obj.get("kind") in SOURCE_KINDS and uid not in new:
            changed.add(uid)
    return changed
