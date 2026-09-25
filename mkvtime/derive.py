"""派生层：依赖图 + 领域裁定。

节点：
  ebml_tree      <- raw_bytes
  tracks         <- ebml_tree
  clusters       <- ebml_tree
  blocks/<uid>   <- clusters + tracks + params
  cues           <- ebml_tree + clusters
  timeline       <- blocks/* + tracks + cues + params

领域裁定（写入机器可读事件）：
  R1 relative timecode 按有符号 int16 解释；
  R2 lacing 损坏时不猜帧边界（frames=None）；
  R3 cue 是索引证据而不是时间真值（只做一致性比对）；
  R4 多轨不按文件顺序强对齐（各自按自身时基换算）；
  R5 无法唯一决定时保留全部合法候选（candidates 列表）。
"""
from __future__ import annotations

import hashlib
import json

import numpy as np

from .ebml import (UNKNOWN_SIZE, iter_elements, parse_block, parse_document)

INT16_MIN, INT16_MAX = -32768, 32767

DEFAULT_PARAMS = {
    "timecode_scale_ns": None,   # None = 使用文件内 TimecodeScale
    "assume_track_scale": True,  # 是否应用 TrackTimecodeScale
}

NODE_DEPS = {
    "ebml_tree": [],
    "tracks": ["ebml_tree"],
    "clusters": ["ebml_tree"],
    "cues": ["ebml_tree", "clusters"],
    "timeline": ["tracks", "clusters", "cues"],
}


def _find(nodes, name):
    return [n for n in iter_elements(nodes) if n["name"] == name]


def _child(node, name):
    for c in node["children"]:
        if c["name"] == name:
            return c
    return None


def _children(node, name):
    return [c for c in node["children"] if c["name"] == name]


# ---------------------------------------------------------------- 节点计算

def derive_tracks(tree, params):
    tracks = []
    for entry in _find(tree["elements"], "TrackEntry"):
        def val(name, default=None):
            c = _child(entry, name)
            return c["value"] if c and c["value"] is not None else default
        number = val("TrackNumber")
        scale = val("TrackTimecodeScale", 1.0)
        diagnostics = []
        if scale is None or scale == 0:
            diagnostics.append({"code": "TRACK_SCALE_INVALID",
                                "detail": "TrackTimecodeScale 缺失或为 0，按 1.0 处理"})
            scale = 1.0
        tracks.append({
            "uid": f"track-{number}",
            "element_uid": entry["uid"],
            "number": number,
            "type": val("TrackType"),
            "name": val("Name", f"track-{number}"),
            "codec": val("CodecID", ""),
            "flag_lacing": val("FlagLacing", 1),
            "timecode_scale": {"value": scale, "unit": "ratio",
                               "raw": (val("TrackTimecodeScale") if val("TrackTimecodeScale") is not None else "absent")},
            "diagnostics": diagnostics,
        })
    return {"tracks": tracks}


def derive_clusters(tree, params):
    clusters = []
    for cl in _find(tree["elements"], "Cluster"):
        ts = _child(cl, "Timestamp")
        clusters.append({
            "uid": "cl-" + cl["uid"][3:],
            "element_uid": cl["uid"],
            "offset": cl["offset"],
            "end": cl["end"],
            "size": cl["size"],
            "unknown_size": cl["size"] == UNKNOWN_SIZE,
            "timestamp_ticks": ts["value"] if ts else None,
            "timestamp_raw_range": ts["raw_range"] if ts else None,
            "raw_range": cl["raw_range"],
            "diagnostics": list(cl["diagnostics"]),
        })
    return {"clusters": clusters}


def derive_cues(tree, clusters_out, params):
    """cue 是索引证据：只做指向校验，不改写任何时间。"""
    cluster_offsets = {c["offset"]: c["uid"] for c in clusters_out["clusters"]}
    cues = []
    for cp in _find(tree["elements"], "CuePoint"):
        ct = _child(cp, "CueTime")
        for ctp in _children(cp, "CueTrackPositions"):
            track = _child(ctp, "CueTrack")
            pos = _child(ctp, "CueClusterPosition")
            target = pos["value"] if pos else None
            hit = cluster_offsets.get(target)
            diagnostics = []
            verdict = "unverified"
            if target is None:
                verdict = "missing_position"
            elif hit is None:
                verdict = "mispointed"
                diagnostics.append({
                    "code": "CUE_MISPOINTED",
                    "detail": f"CueClusterPosition={target} 不指向任何 Cluster 起始偏移",
                })
            else:
                verdict = "consistent"
            cues.append({
                "uid": f"cue-{cp['uid'][3:]}-{track['value'] if track else '?'}",
                "element_uid": cp["uid"],
                "cue_time_ticks": ct["value"] if ct else None,
                "track": track["value"] if track else None,
                "cluster_position": target,
                "resolves_to": hit,
                "verdict": verdict,
                "role": "index-evidence",   # R3：不是时间真值
                "raw_range": cp["raw_range"],
                "diagnostics": diagnostics,
            })
    return {"cues": cues}


def _block_uid(cluster_uid, offset):
    return f"blk-{cluster_uid}-{offset}"


def derive_blocks(tree, tracks_out, clusters_out, params):
    """逐 cluster 解析块。返回 {cluster_uid: blocks_result}，支持局部重算。"""
    track_by_number = {t["number"]: t for t in tracks_out["tracks"]}
    scale_ns = params.get("timecode_scale_ns")
    info = _find(tree["elements"], "TimecodeScale")
    file_scale = info[0]["value"] if info else 1000000
    effective_scale_ns = scale_ns if scale_ns else file_scale

    per_cluster = {}
    for cl in _find(tree["elements"], "Cluster"):
        cl_uid = "cl-" + cl["uid"][3:]
        cl_info = next((c for c in clusters_out["clusters"] if c["uid"] == cl_uid), None)
        base_ticks = cl_info["timestamp_ticks"] if cl_info else None
        blocks = []
        for node in iter_elements([cl]):
            if node["name"] not in ("SimpleBlock", "Block"):
                continue
            payload = _slice(node)
            parsed = parse_block(payload) if payload is not None else {
                "ok": False, "diagnostics": [{"code": "PAYLOAD_UNAVAILABLE",
                                              "detail": "块载荷不可用"}]}
            group = _parent_group(cl, node)
            discard = None
            duration = None
            if group is not None:
                dp = _child(group, "DiscardPadding")
                bd = _child(group, "BlockDuration")
                if dp is not None:
                    discard = {"value_ns": dp["value"], "unit": "ns",
                               "raw_range": dp["raw_range"]}
                if bd is not None:
                    duration = {"value_ticks": bd["value"], "unit": "ticks"}
            rel = parsed.get("relative_timecode")
            diagnostics = list(parsed.get("diagnostics", []))
            overflow = False
            if rel is not None and not (INT16_MIN <= rel <= INT16_MAX):
                overflow = True  # 理论上不会发生（int16 域内），保留防御
            abs_ticks = None
            candidates = []
            if base_ticks is None:
                diagnostics.append({"code": "CLUSTER_TIMESTAMP_MISSING",
                                    "detail": "Cluster 无 Timestamp，绝对时码无法唯一决定"})
                candidates = [{"assumption": "prev_cluster_end", "value": None},
                              {"assumption": "zero_base", "value": rel}]
            elif rel is None:
                diagnostics.append({"code": "BLOCK_TIMECODE_MISSING",
                                    "detail": "块缺少 relative timecode"})
            else:
                abs_ticks = base_ticks + rel
                if rel < 0:
                    diagnostics.append({
                        "code": "NEGATIVE_RELATIVE_TIMECODE",
                        "detail": f"relative timecode={rel}（有符号解释），绝对时码前移",
                    })
            track = track_by_number.get(parsed.get("track"))
            ns = None
            if abs_ticks is not None:
                tscale = track["timecode_scale"]["value"] if (track and params.get("assume_track_scale", True)) else 1.0
                ns = int(round(abs_ticks * effective_scale_ns * tscale))
            blocks.append({
                "uid": _block_uid(cl_uid, node["offset"]),
                "element_uid": node["uid"],
                "cluster_uid": cl_uid,
                "kind": node["name"],
                "offset": node["offset"],
                "raw_range": node["raw_range"],
                "track": parsed.get("track"),
                "relative_timecode": {"value": rel, "unit": "ticks",
                                      "interpretation": "signed-int16",
                                      "raw_uint16": parsed.get("relative_timecode_raw")},
                "lacing": parsed.get("lacing"),
                "frames": parsed.get("frames"),
                "frames_unknown": parsed.get("frames") is None,
                "discard_padding": discard,
                "block_duration": duration,
                "abs_timecode": {"value": abs_ticks, "unit": "ticks"},
                "abs_time_ns": {"value": ns, "unit": "ns",
                                "scale_ns_per_tick": effective_scale_ns},
                "candidates": candidates,
                "diagnostics": diagnostics,
            })
        per_cluster[cl_uid] = {"cluster_uid": cl_uid, "blocks": blocks}
    return per_cluster


def derive_timeline(tracks_out, clusters_out, cues_out, blocks_per_cluster, params):
    entries = []
    for cl in clusters_out["clusters"]:
        bres = blocks_per_cluster.get(cl["uid"], {"blocks": []})
        for b in bres["blocks"]:
            entries.append({
                "uid": b["uid"], "track": b["track"],
                "cluster_uid": b["cluster_uid"],
                "abs_ticks": b["abs_timecode"]["value"],
                "abs_ns": b["abs_time_ns"]["value"],
                "frames_unknown": b["frames_unknown"],
                "diagnostics": [d["code"] for d in b["diagnostics"]],
            })
    # R4：每条轨独立排序，不按文件顺序强对齐
    per_track = {}
    for t in tracks_out["tracks"]:
        rows = [e for e in entries if e["track"] == t["number"]]
        # R4：每轨独立按自身时码排序，未知时码排最后；不按文件顺序强对齐
        keys = np.array([e["abs_ticks"] if e["abs_ticks"] is not None
                         else np.iinfo(np.int64).max for e in rows],
                        dtype=np.int64)
        order = np.argsort(keys, kind="stable")
        per_track[t["number"]] = [rows[i] for i in order]
    gaps = []
    offsets = sorted(c["offset"] for c in clusters_out["clusters"])
    ends = {c["offset"]: c["end"] for c in clusters_out["clusters"]}
    for prev, nxt in zip(offsets, offsets[1:]):
        e = ends.get(prev)
        if e is not None and nxt > e:
            gaps.append({"from": e, "to": nxt, "code": "CLUSTER_GAP",
                         "detail": f"cluster 之间存在 {nxt - e} 字节缺口/损坏区间"})
    return {"entries": entries, "per_track": per_track, "cluster_gaps": gaps,
            "cues_summary": {"total": len(cues_out["cues"]),
                             "mispointed": sum(1 for c in cues_out["cues"]
                                               if c["verdict"] == "mispointed")}}


# 需要原始字节切片：derive 在内存中持有 data
_DATA_HOLDER = {"data": b"", "tree": None}


def _slice(node):
    data = _DATA_HOLDER["data"]
    s, e = node["data_offset"], node["end"]
    if e is None:
        e = len(data)
    return data[s:e]


def _parent_group(cluster_node, block_node):
    for g in cluster_node["children"]:
        if g["name"] == "BlockGroup":
            for c in g["children"]:
                if c["uid"] == block_node["uid"]:
                    return g
    return None


# ---------------------------------------------------------------- 依赖图执行

def compute_all(data: bytes, params: dict) -> dict:
    tree = parse_document(data)
    _DATA_HOLDER["data"] = data
    tracks = derive_tracks(tree, params)
    clusters = derive_clusters(tree, params)
    cues = derive_cues(tree, clusters, params)
    blocks = derive_blocks(tree, tracks, clusters, params)
    timeline = derive_timeline(tracks, clusters, cues, blocks, params)
    return {"ebml_tree": tree, "tracks": tracks, "clusters": clusters,
            "cues": cues, "blocks": blocks, "timeline": timeline}


def node_hash(node: str, payload) -> str:
    return hashlib.sha256(json.dumps([node, payload], sort_keys=True,
                                     default=str).encode()).hexdigest()[:16]


def affected_nodes(changed: str) -> set:
    """从变更节点出发做依赖传播，返回需重算节点集合。"""
    out = {changed}
    frontier = [changed]
    while frontier:
        cur = frontier.pop()
        for node, deps in NODE_DEPS.items():
            if cur in deps and node not in out:
                out.add(node)
                frontier.append(node)
    if changed in ("tracks", "clusters", "cues"):
        out.add("blocks")
    if "blocks" in out or "cues" in out or "tracks" in out:
        out.add("timeline")
    return out
