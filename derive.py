"""Dependency-graph derivation engine.

Each version's payload is a set of derived nodes. A node is recomputed only
when one of its dependencies changed; otherwise the previous output is reused
and the reason is recorded, so the UI can explain why a result is still valid
or why it had to be recomputed.
"""
from __future__ import annotations

import hashlib
import json

import numpy as np

from ebml import element_to_dict, parse_document
from matroska import extract_semantics


def _hash_obj(obj) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]


def build_payload(source_bytes: bytes, events: list[dict],
                  parent: dict | None = None) -> dict:
    parent_nodes = (parent or {}).get("nodes", {})
    parent_meta = (parent or {}).get("node_meta", {})
    nodes: dict = {}
    meta: dict = {}
    hashes: dict = {
        "source": hashlib.sha256(source_bytes).hexdigest()[:16],
    }
    param_events = [e for e in events if e["kind"] == "param_revision"]
    judgment_events = [e for e in events if e["kind"] == "judgment"]
    hashes["events:param"] = _hash_obj(
        [(e["seq"], e["payload"]) for e in param_events])
    hashes["events:judgment"] = _hash_obj(
        [(e["seq"], e["payload"]) for e in judgment_events])

    def node(name, deps, compute):
        dep_hashes = {d: hashes[d] for d in deps}
        dep_sig = _hash_obj(dep_hashes)
        pm = parent_meta.get(name)
        if pm and pm.get("dep_sig") == dep_sig and name in parent_nodes:
            out = parent_nodes[name]
            hashes[name] = pm["hash"]
            meta[name] = {
                "hash": pm["hash"], "dep_sig": dep_sig, "dep_hashes": dep_hashes,
                "status": "reused",
                "reason": "依赖未变化，结果仍然有效，沿用上一版本",
            }
        else:
            out = compute()
            hashes[name] = _hash_obj(out)
            if pm is None:
                reason = "首次计算"
            else:
                changed = [d for d in deps
                           if pm.get("dep_hashes", {}).get(d) != hashes[d]]
                reason = "依赖变化，重新计算: " + ",".join(changed)
            meta[name] = {
                "hash": hashes[name], "dep_sig": dep_sig,
                "dep_hashes": dep_hashes, "status": "computed", "reason": reason,
            }
        nodes[name] = out
        return out

    elements = node("elements", ["source"],
                    lambda: [element_to_dict(e)
                             for e in parse_document(source_bytes)])
    sem = node("semantics", ["elements"],
               lambda: extract_semantics(parse_document(source_bytes)))

    def compute_params():
        scale = sem["timecode_scale_ns"]
        revisions = []
        for e in param_events:
            for key, value in e["payload"].get("set", {}).items():
                if key == "timecode_scale_ns":
                    scale = value
            revisions.append({"seq": e["seq"], "set": e["payload"].get("set", {}),
                              "note": e["payload"].get("note", "")})
        return {
            "timecode_scale_ns": scale,
            "unit": "ns",
            "coordinate": "absolute time = (cluster + block) * TimecodeScale",
            "revisions": revisions,
        }

    params = node("params", ["semantics", "events:param"], compute_params)

    cluster_nodes = []
    for i in range(len(sem["clusters"])):
        cluster_nodes.append(node(
            f"cluster:{i}", ["semantics", "params"],
            lambda i=i: _compute_cluster(sem["clusters"][i], i, params)))

    cluster_ids = [c["id"] for c in cluster_nodes]
    tracks = node("tracks", ["semantics", "params"] + cluster_ids,
                  lambda: _compute_tracks(sem, cluster_nodes))
    cues = node("cues", ["semantics", "params"] + cluster_ids,
                lambda: _compute_cues(sem, params, cluster_nodes))
    anomalies = node("anomalies", ["tracks", "cues", "params"] + cluster_ids,
                     lambda: _compute_anomalies(cluster_nodes, tracks, cues))

    def compute_conclusions():
        out = {}
        for e in judgment_events:
            p = e["payload"]
            out[p["anomaly_id"]] = {
                "decision": p["decision"],
                "candidate_id": p.get("candidate_id"),
                "note": p.get("note", ""),
                "reviewer": p.get("reviewer", ""),
                "rules": p.get("rules", []),
                "event_seq": e["seq"],
            }
        return out

    node("conclusions", ["anomalies", "events:judgment"], compute_conclusions)

    return {
        "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "source_len": len(source_bytes),
        "nodes": nodes,
        "node_meta": meta,
    }


def _compute_cluster(cluster: dict, index: int, params: dict) -> dict:
    scale = params["timecode_scale_ns"]
    tc = cluster["timecode"]
    blocks = []
    for b in cluster["blocks"]:
        rel = b["rel_timecode"]
        abs_units = tc + rel
        candidates = [{
            "id": f"{b['id']}:signed",
            "label": "有符号 int16 解释",
            "value": abs_units * scale,
            "unit": "ns",
        }]
        if rel == -32768 or abs_units < 0:
            candidates.append({
                "id": f"{b['id']}:unsigned",
                "label": "无符号 16 位误读",
                "value": (tc + (rel & 0xFFFF)) * scale,
                "unit": "ns",
                "rejected_by_rule": "signed-relative-timecode",
            })
        blocks.append({**b,
                       "abs_timecode_units": abs_units,
                       "abs_timecode_ns": abs_units * scale,
                       "candidates": candidates})
    return {
        "id": f"cluster:{index}",
        "index": index,
        "offset": cluster["offset"],
        "end": cluster["end"],
        "unknown_size": cluster["unknown_size"],
        "timecode": tc,
        "timecode_ns": tc * scale,
        "timecode_unit": "TimecodeScale units",
        "blocks": blocks,
    }


def _compute_tracks(sem: dict, cluster_nodes: list[dict]) -> dict:
    tracks = []
    for t in sem["tracks"]:
        duration = t.get("default_duration_ns")
        frames = []
        for cn in cluster_nodes:
            for b in cn["blocks"]:
                if b["track"] != t["number"]:
                    continue
                if b["lacing_corrupt"]:
                    frames.append({
                        "block": b["id"], "cluster": cn["id"],
                        "status": "unknown_boundaries",
                        "start_ns": b["abs_timecode_ns"],
                        "diagnostic": "lacing 损坏：不猜帧边界，仅保留块范围",
                    })
                elif duration is None:
                    frames.append({
                        "block": b["id"], "cluster": cn["id"],
                        "status": "unresolved",
                        "start_ns": b["abs_timecode_ns"],
                        "diagnostic": "缺少 DefaultDuration，值保留原始表示",
                    })
                else:
                    count = len(b["frame_sizes"])
                    times = (int(b["abs_timecode_ns"])
                             + np.arange(count, dtype=np.int64) * int(duration))
                    frames.append({
                        "block": b["id"], "cluster": cn["id"],
                        "status": "ok", "count": count,
                        "times_ns": [int(x) for x in times.tolist()],
                    })
        tracks.append({
            "id": f"track:{t['number']}",
            "number": t["number"],
            "type": t["type"],
            "name": t.get("name"),
            "default_duration_ns": duration,
            "unit": "ns",
            "frames": frames,
        })
    return {
        "tracks": tracks,
        "alignment_rule": "多轨不按文件顺序强对齐，各自保留自己的时基网格",
    }


def _compute_cues(sem: dict, params: dict, cluster_nodes: list[dict]) -> dict:
    scale = params["timecode_scale_ns"]
    by_offset = {c["offset"]: c for c in cluster_nodes}
    cues = []
    for i, cue in enumerate(sem["cues"]):
        cid = f"cue:{i}"
        cue_ns = (cue["cue_time"] or 0) * scale
        positions = []
        for pos in cue["positions"]:
            target = sem["segment_data_offset"] + (pos["cluster_position"] or 0)
            hit = by_offset.get(target)
            actual_ns = hit["timecode_ns"] if hit else None
            if hit is None:
                status = "dangling"
            elif actual_ns == cue_ns:
                status = "ok"
            else:
                status = "mispoint"
            positions.append({
                **pos,
                "target_offset": target,
                "hit_cluster": hit["id"] if hit else None,
                "actual_timecode_ns": actual_ns,
                "status": status,
            })
        status = "ok" if positions and all(
            p["status"] == "ok" for p in positions) else "mispoint"
        candidates = [{"id": f"{cid}:claimed",
                       "label": "cue 声明时间（索引证据）",
                       "value": cue_ns, "unit": "ns"}]
        actuals = sorted({p["actual_timecode_ns"] for p in positions
                          if p["actual_timecode_ns"] is not None})
        for actual in actuals:
            candidates.insert(0, {"id": f"{cid}:parsed",
                                  "label": "被指向 cluster 的解析时码",
                                  "value": actual, "unit": "ns"})
        cues.append({
            "id": cid,
            "cue_time": cue["cue_time"],
            "cue_time_ns": cue_ns,
            "positions": positions,
            "status": status,
            "candidates": candidates,
            "rule": "cue 是索引证据而不是时间真值",
        })
    return {"cues": cues}


def _anomaly(kind, oid, severity, detail, object_refs, candidates=None,
             extra=None):
    anom = {
        "id": f"anom:{kind}:{oid}",
        "kind": kind,
        "severity": severity,
        "object_refs": object_refs,
        "detail": detail,
        "candidates": candidates or [],
        "status": "undecided",
    }
    if extra:
        anom.update(extra)
    return anom


def _compute_anomalies(cluster_nodes, tracks_node, cues_node) -> list[dict]:
    anomalies = []
    for cn in cluster_nodes:
        if cn["unknown_size"]:
            anomalies.append(_anomaly(
                "unknown_size", cn["id"], "info",
                f"{cn['id']} 以未知长度存储，范围延伸至父元素末尾 "
                f"[{cn['offset']},{cn['end']})",
                [cn["id"]],
                extra={"unit": "byte offset"}))
        for b in cn["blocks"]:
            if b["rel_timecode"] == -32768 or b["abs_timecode_units"] < 0:
                anomalies.append(_anomaly(
                    "rel_overflow", b["id"], "warn",
                    f"{b['id']} 相对时码 {b['rel_timecode']}（有符号 int16），"
                    f"绝对时码 {b['abs_timecode_units']} "
                    f"TimecodeScale units；无符号误读已被规则拒绝",
                    [b["id"], cn["id"]],
                    candidates=b["candidates"],
                    extra={"unit": "TimecodeScale units"}))
            if b["lacing_corrupt"]:
                anomalies.append(_anomaly(
                    "lacing_corrupt", b["id"], "error",
                    f"{b['id']} lacing 损坏：{b['lacing_diagnostic']}；"
                    f"不猜帧边界，仅保留块范围 {b['raw_range']}",
                    [b["id"], cn["id"]],
                    candidates=[
                        {"id": f"{b['id']}:extent",
                         "label": "仅保留块范围（规则采纳）",
                         "value": b["raw_range"], "unit": "byte offset"},
                        {"id": f"{b['id']}:guess",
                         "label": "按平均尺寸猜帧边界",
                         "value": None, "unit": "ns",
                         "rejected_by_rule": "lacing-no-guess"},
                    ],
                    extra={"unit": "byte offset"}))
            padding = b.get("discard_padding")
            if padding is not None:
                severity = "warn" if padding["value"] < 0 else "info"
                anomalies.append(_anomaly(
                    "discard_padding", b["id"], severity,
                    f"{b['id']} DiscardPadding = {padding['value']} "
                    f"{padding['unit']}（原始值保留）",
                    [b["id"], cn["id"]],
                    extra={"unit": padding["unit"]}))
    ordered = sorted(cluster_nodes, key=lambda c: c["timecode"])
    if len(ordered) > 1:
        tcs = np.array([c["timecode"] for c in ordered], dtype=np.int64)
        diffs = np.diff(tcs)
        median = int(np.median(diffs)) if len(diffs) else 0
        for left, right, diff in zip(ordered, ordered[1:], diffs.tolist()):
            if median > 0 and diff > 2 * median:
                anomalies.append(_anomaly(
                    "cluster_gap", f"{left['id']}:{right['id']}", "warn",
                    f"{left['id']} → {right['id']} 时码跳变 {diff} "
                    f"TimecodeScale units（中位间隔 {median}），中途 cluster 缺口",
                    [left["id"], right["id"]],
                    extra={"unit": "TimecodeScale units"}))
    durations = {t["default_duration_ns"] for t in tracks_node["tracks"]}
    if len(durations) > 1:
        anomalies.append(_anomaly(
            "track_timebase_divergence", "tracks", "info",
            "轨道 DefaultDuration 不一致 "
            f"{sorted(durations)} ns；按规则不做文件顺序强对齐，"
            "各自保留时基网格",
            [t["id"] for t in tracks_node["tracks"]],
            extra={"unit": "ns"}))
    for cue in cues_node["cues"]:
        if cue["status"] != "ok":
            anomalies.append(_anomaly(
                "cue_mispoint", cue["id"], "warn",
                f"{cue['id']} 声明时间 {cue['cue_time_ns']} ns 与指向 cluster "
                f"解析时码不一致；cue 是索引证据而不是时间真值",
                [cue["id"]] + [p["hit_cluster"] for p in cue["positions"]
                               if p["hit_cluster"]],
                candidates=cue["candidates"],
                extra={"unit": "ns"}))
    return anomalies
