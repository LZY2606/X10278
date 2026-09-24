"""Dependency-graph derivation engine.

Nodes form a DAG. On an edit the dirty set propagates along the graph; only
affected nodes recompute. Copied nodes carry an explanation of why they are
still valid, so any result can answer "why valid / why recomputed".
"""
from __future__ import annotations

import hashlib

from . import ebml, rules

ROOTS = ("src:raw", "params", "decisions")


def make_source(data: bytes, name: str = "input.mkv") -> dict:
    return {"name": name, "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "hex": data.hex()}


def _tracks_payload(buf: bytes, elements: list[dict]) -> dict:
    tracks = []
    anomalies = []
    for entry in elements:
        if entry["name"] != "TrackEntry":
            continue
        p = entry["path"]
        num = ebml.child(elements, p, "TrackNumber")
        uid = ebml.child(elements, p, "TrackUID")
        ttype = ebml.child(elements, p, "TrackType")
        codec = ebml.child(elements, p, "CodecID")
        scale = ebml.child(elements, p, "TrackTimecodeScale")
        tracks.append({
            "id": f"track:{ebml.el_uint(buf, num)}",
            "number": ebml.el_uint(buf, num),
            "uid": ebml.el_uint(buf, uid) if uid else None,
            "type": ebml.el_uint(buf, ttype) if ttype else None,
            "codec": ebml.el_str(buf, codec) if codec else None,
            "timecode_scale": (ebml.el_float(buf, scale)
                               if scale else 1.0),
            "timecode_scale_source": ("TrackTimecodeScale element"
                                      if scale else "default 1.0"),
            "byte_range": [entry["offset"], entry["end"]],
        })
    scales = {t["timecode_scale"] for t in tracks}
    if len(scales) > 1:
        anomalies.append({
            "kind": "track_timebase_divergence",
            "detail": f"tracks declare different TrackTimecodeScale values "
                      f"{sorted(scales)}; each track keeps its own timebase",
            "policy": "no_file_order_alignment",
            "rule_id": "rule:no-file-order-alignment",
        })
    return {"tracks": tracks, "anomalies": anomalies}


def _clusters_payload(buf: bytes, elements: list[dict],
                      regions: list[dict]) -> dict:
    clusters = []
    for ci, entry in enumerate(e for e in elements
                               if e["name"] == "Cluster" and e["depth"] == 1):
        p = entry["path"]
        tcel = ebml.child(elements, p, "Timecode")
        blocks = []
        for bi, ch in enumerate(ebml.children_of(elements, p)):
            if ch["name"] == "SimpleBlock":
                blocks.append({"key": f"block:{ci}:{bi}",
                               "kind": "SimpleBlock",
                               "element_id": ch["id"],
                               "byte_range": [ch["offset"], ch["end"]],
                               "data_range": [ch["data_offset"], ch["end"]]})
            elif ch["name"] == "BlockGroup":
                block_el = ebml.child(elements, ch["path"], "Block")
                if block_el is None:
                    continue
                duration_el = ebml.child(elements, ch["path"], "BlockDuration")
                discard_el = ebml.child(elements, ch["path"], "DiscardPadding")
                discard = None
                if discard_el is not None:
                    discard = {
                        "value": ebml.el_int(buf, discard_el),
                        "unit": "ns",
                        "raw_hex": buf[discard_el["data_offset"]:
                                       discard_el["end"]].hex(),
                        "byte_range": [discard_el["offset"], discard_el["end"]],
                    }
                blocks.append({
                    "key": f"block:{ci}:{bi}",
                    "kind": "BlockGroup",
                    "element_id": block_el["id"],
                    "byte_range": [block_el["offset"], block_el["end"]],
                    "data_range": [block_el["data_offset"], block_el["end"]],
                    "block_duration": (
                        {"value": ebml.el_uint(buf, duration_el),
                         "unit": "ticks"} if duration_el else None),
                    "discard_padding": discard,
                })
        clusters.append({
            "id": f"cluster:{ci}",
            "timecode": {"value": ebml.el_uint(buf, tcel) if tcel else 0,
                         "unit": "ticks"},
            "offset": entry["offset"],
            "end": entry["end"],
            "unknown_size": entry["unknown_size"],
            "byte_range": [entry["offset"], entry["end"]],
            "blocks": blocks,
        })
    gaps = [{"id": r["id"], "byte_range": [r["start"], r["end"]],
             "reason": r["reason"], "raw_hex": r["raw_hex"]}
            for r in regions if r["kind"] == "gap"]
    return {"clusters": clusters, "gaps": gaps}


def _raw_block_payload(buf: bytes, ref: dict) -> dict:
    start, end = ref["data_range"]
    raw = buf[start:end]
    payload = rules.parse_block_header(raw)
    payload.update({
        "key": ref["key"],
        "kind": ref["kind"],
        "raw_hex": raw.hex(),
        "byte_range": ref["byte_range"],
        "discard_padding": ref.get("discard_padding"),
        "block_duration": ref.get("block_duration"),
        "amended": False,
    })
    return payload


def _cues_payload(buf: bytes, elements: list[dict],
                  seg_data_offset: int) -> list[dict]:
    cues = []
    for cp in (e for e in elements if e["name"] == "CuePoint"):
        time_el = ebml.child(elements, cp["path"], "CueTime")
        pos_el = None
        track_el = None
        for ctp in ebml.children_of(elements, cp["path"]):
            if ctp["name"] != "CueTrackPositions":
                continue
            pos_el = ebml.child(elements, ctp["path"], "CueClusterPosition")
            track_el = ebml.child(elements, ctp["path"], "CueTrack")
        position = ebml.el_uint(buf, pos_el) if pos_el else 0
        cues.append({
            "time": {"value": ebml.el_uint(buf, time_el) if time_el else 0,
                     "unit": "ticks"},
            "track": ebml.el_uint(buf, track_el) if track_el else None,
            "cluster_position": {"value": position, "unit": "bytes",
                                 "basis": "segment_data_start"},
            "cluster_position_abs": seg_data_offset + position,
            "byte_range": [cp["offset"], cp["end"]],
        })
    return cues


class _Ctx:
    def __init__(self, resolved: dict):
        self.resolved = resolved

    def get(self, key: str):
        return self.resolved[key]


def compute_graph(data: bytes) -> dict:
    """Return ordered graph: key -> (input keys, function(ctx))."""
    parsed = ebml.parse(data)
    elements = parsed["elements"]
    seg = next(e for e in elements
               if e["name"] == "Segment" and e["depth"] == 0)
    seg_data_offset = seg["data_offset"]
    tracks_node = _tracks_payload(data, elements)
    clusters_node = _clusters_payload(data, elements, parsed["regions"])
    cues_raw = _cues_payload(data, elements, seg_data_offset)

    graph: dict[str, tuple[list, object]] = {}
    graph["parse:ebml"] = (["src:raw"], lambda ctx: parsed)
    graph["parse:tracks"] = (
        ["parse:ebml"],
        lambda ctx: tracks_node if ctx.get("parse:ebml")["sha256"]
        == parsed["sha256"] else _tracks_payload(
            bytes.fromhex(ctx.get("src:raw")["hex"]),
            ctx.get("parse:ebml")["elements"]))
    graph["cluster:list"] = (
        ["parse:ebml"],
        lambda ctx: clusters_node if ctx.get("parse:ebml")["sha256"]
        == parsed["sha256"] else _clusters_payload(
            bytes.fromhex(ctx.get("src:raw")["hex"]),
            ctx.get("parse:ebml")["elements"],
            ctx.get("parse:ebml")["regions"]))

    track_block_inputs: dict[int, list[str]] = {}
    for cluster in clusters_node["clusters"]:
        ci = int(cluster["id"].split(":")[1])
        for ref in cluster["blocks"]:
            raw_key = f"{ref['key']}:raw"
            frames_key = f"{ref['key']}:frames"
            tc_key = f"{ref['key']}:timecode"
            graph[raw_key] = (
                ["parse:ebml", "src:raw"],
                lambda ref=ref: (
                    lambda ctx: _raw_block_payload(
                        bytes.fromhex(ctx.get("src:raw")["hex"]), ref)))
            graph[frames_key] = (
                [raw_key],
                (lambda rk=raw_key: (
                    lambda ctx: rules.decode_frames(
                        ctx.get(rk)["lacing"],
                        bytes.fromhex(ctx.get(rk)["body_hex"]))))())
            graph[tc_key] = (
                [raw_key, "cluster:list", "params"],
                (lambda rk=raw_key, cid=ci:
                 lambda ctx: rules.block_timecode(
                    next(c for c in ctx.get("cluster:list")["clusters"]
                         if c["id"] == f"cluster:{cid}")["timecode"]["value"],
                    ctx.get(rk)["relative_timecode"]["value"],
                    ctx.get("params")))())
            header = rules.parse_block_header(
                data[ref["data_range"][0]:ref["data_range"][1]])
            track_block_inputs.setdefault(header["track"], []).extend(
                [raw_key, frames_key, tc_key])

    def _timeline(track_number):
        def _fn(ctx):
            tnode = ctx.get("parse:tracks")
            track = next(t for t in tnode["tracks"]
                         if t["number"] == track_number)
            entries = []
            clist = {c["id"]: c for c in ctx.get("cluster:list")["clusters"]}
            for key in track_block_inputs.get(track_number, []):
                if not key.endswith(":raw"):
                    continue
                base = key[:-4]
                raw_payload = ctx.get(key)
                if raw_payload["track"] != track_number:
                    continue
                ci = int(base.split(":")[1])
                entries.append({
                    "cluster": ci, "block": base,
                    "timecode": ctx.get(f"{base}:timecode"),
                    "frames": ctx.get(f"{base}:frames"),
                })
            return rules.build_timeline(
                track, entries, ctx.get("params"))
        return _fn

    for track in tracks_node["tracks"]:
        tn = track["number"]
        graph[f"track:{tn}:timeline"] = (
            ["parse:tracks", "params"] + track_block_inputs.get(tn, []),
            _timeline(tn))

    graph["cues:verify"] = (
        ["parse:ebml", "cluster:list", "decisions"],
        lambda ctx: rules.verify_cues(
            cues_raw if ctx.get("parse:ebml")["sha256"] == parsed["sha256"]
            else _cues_payload(bytes.fromhex(ctx.get("src:raw")["hex"]),
                               ctx.get("parse:ebml")["elements"],
                               next(e for e in ctx.get("parse:ebml")["elements"]
                                    if e["name"] == "Segment"
                                    and e["depth"] == 0)["data_offset"]),
            ctx.get("cluster:list")["clusters"],
            ctx.get("decisions")))

    all_keys = list(graph.keys())
    graph["anomalies"] = (
        all_keys,
        lambda ctx: _aggregate_anomalies(ctx, all_keys,
                                         clusters_node, cues_raw))
    return graph


def _aggregate_anomalies(ctx, keys, clusters_node, cues_raw) -> dict:
    anomalies = []
    seen = set()

    def add(obj, items):
        for a in items:
            aid = f"anom:{a['kind']}:{obj}"
            if aid in seen:
                continue
            seen.add(aid)
            anomalies.append({"id": aid, "object": obj, **a})

    add("tracks", ctx.get("parse:tracks")["anomalies"])
    for region in ctx.get("parse:ebml")["regions"]:
        add(region["id"], [{
            "kind": "cluster_gap" if region["kind"] == "gap"
            else "element_overrun",
            "detail": f"{region['kind']} region bytes "
                      f"{region['start']}..{region['end']}: {region['reason']}",
            "byte_range": [region["start"], region["end"]],
            "policy": "raw_range_preserved",
            "rule_id": None,
        }])
    for cluster in ctx.get("cluster:list")["clusters"]:
        if cluster["unknown_size"]:
            add(cluster["id"], [{
                "kind": "unknown_size",
                "detail": f"{cluster['id']} uses an unknown-size element; its "
                          f"end is taken as the segment end {cluster['end']}",
                "policy": "extend_to_parent_end",
                "rule_id": None,
            }])
        for ref in cluster["blocks"]:
            key = ref["key"]
            add(key, ctx.get(f"{key}:frames")["anomalies"])
            add(key, ctx.get(f"{key}:timecode")["anomalies"])
            raw = ctx.get(f"{key}:raw")
            if raw.get("discard_padding"):
                add(key, [{
                    "kind": "discard_padding",
                    "detail": f"BlockGroup keeps DiscardPadding "
                              f"{raw['discard_padding']['value']} ns (raw "
                              f"representation preserved)",
                    "policy": "keep_raw_with_unit",
                    "rule_id": None,
                }])
    for cue in ctx.get("cues:verify")["cues"]:
        add(cue["id"], cue["anomalies"])
    return {"anomalies": anomalies}


def derive(store, parent_vid: int | None, label: str,
           overrides: dict | None = None, events: list[dict] | None = None,
           actor: str = "system", expected_parent: int | None = None) -> dict:
    """Build a new version from `parent_vid` plus `overrides`.

    Only nodes whose inputs changed are recomputed; untouched nodes are
    copied with an explanation of why they remain valid.
    """
    overrides = overrides or {}
    events = events or []
    parent_nodes = store.version_nodes(parent_vid) if parent_vid else {}

    def parent_payload(key, default=None):
        node = parent_nodes.get(key)
        return node["payload"] if node else default

    src = overrides.get("src:raw", parent_payload("src:raw"))
    if src is None:
        raise ValueError("no src:raw available; import a case first")
    params = overrides.get("params", parent_payload(
        "params", dict(rules.DEFAULT_PARAMS)))
    decisions = overrides.get("decisions", parent_payload(
        "decisions", {"selections": {}}))

    data = bytes.fromhex(src["hex"])
    graph = compute_graph(data)

    vid = store.begin_version(parent_vid, label, actor)
    resolved: dict = {}
    dirty: set[str] = set(overrides.keys())
    if parent_vid is None:
        dirty.update(ROOTS)
    recomputed, reused, overridden = [], [], []

    def emit(key, payload, inputs, explanation):
        resolved[key] = payload
        store.put_node(vid, key, payload, inputs, explanation)

    emit("src:raw", src, [], "root: source bytes"
         + (" (overridden)" if "src:raw" in overrides else ""))
    emit("params", params, [], "root: review parameters"
         + (" (revised)" if "params" in overrides else ""))
    emit("decisions", decisions, [], "root: human decisions"
         + (" (updated)" if "decisions" in overrides else ""))

    ctx = _Ctx(resolved)
    for key, (inputs, fn) in graph.items():
        if key in overrides:
            payload = overrides[key]
            overridden.append(key)
            emit(key, payload, inputs,
                 "overridden directly by edit; original kept in parent version")
            dirty.add(key)
            continue
        changed_inputs = [i for i in inputs if i in dirty]
        missing = parent_vid is None or key not in parent_nodes
        if not missing and not changed_inputs:
            payload = parent_nodes[key]["payload"]
            reused.append(key)
            emit(key, payload, inputs,
                 f"still valid: inputs unchanged since v{parent_vid}")
            continue
        payload = fn(ctx)
        recomputed.append(key)
        reason = ("no prior version" if missing
                  else f"inputs changed: {', '.join(changed_inputs)}")
        emit(key, payload, inputs, f"recomputed: {reason}")
        dirty.add(key)

    for ev in events:
        store.stage_event(vid, ev["kind"], ev.get("payload", {}),
                          ev.get("actor", actor))
    store.stage_event(vid, "derive.summary", {
        "label": label,
        "recomputed": recomputed,
        "reused": reused,
        "overridden": overridden,
        "statement": "dependency-graph propagation updated only affected "
                     "nodes; reused nodes explain why they remain valid",
    }, actor="system")
    result = store.commit_version(vid, expected_parent=expected_parent)
    result["version"] = vid
    result["recomputed"] = recomputed
    result["reused"] = reused
    return result
