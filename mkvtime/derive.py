"""Dependency-graph derivation engine.

Every derivation run produces one immutable version.  When a source or a
parameter changes, only the downstream nodes are recomputed; each node
reports whether it was recomputed or reused, and why.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field

import numpy as np

from . import ebml, model


DEFAULT_PARAMS = {
    # None => use the TimecodeScale element as-is
    "timecode_scale_override_ns": None,
    # "apply" => DiscardPadding trims the block end; "observe" => report only
    "discard_padding_policy": "apply",
    # multi-track timelines are never force-aligned by file order; this
    # param only controls whether a diagnostic comparison row is emitted
    "show_file_order_comparison": True,
}

# node -> direct dependencies ("source" and "param:*" are external inputs)
GRAPH = {
    "ebml_tree": ["source"],
    "segment": ["ebml_tree", "param:timecode_scale_override_ns"],
    "frames": ["segment", "frame_summary"],
    "timelines": ["frames", "param:discard_padding_policy",
                  "param:show_file_order_comparison"],
    "cue_check": ["segment"],
    "anomalies": ["segment", "timelines", "cue_check"],
}

ORDER = ["ebml_tree", "segment", "frames", "timelines", "cue_check", "anomalies"]


@dataclass
class NodeResult:
    node: str
    status: str          # "recomputed" | "reused"
    explain: str
    data: dict

    def to_dict(self):
        return {"node": self.node, "status": self.status,
                "explain": self.explain, "data": self.data}


def downstream_of(changed: set[str]) -> set[str]:
    """All nodes invalidated by a set of changed inputs/nodes."""
    dirty: set[str] = set()
    work = list(changed)
    while work:
        item = work.pop()
        for node, deps in GRAPH.items():
            if item in deps and node not in dirty:
                dirty.add(node)
                work.append(node)
    return dirty


def _node_ebml_tree(source: bytes, params, inputs):
    roots = ebml.parse(source)

    def ser(el: ebml.Element, parent_end):
        start, end = el.raw_range(parent_end)
        return {
            "id": f"el:{el.offset}",
            "name": el.name, "eid": f"0x{el.eid:X}" if el.eid >= 0 else "TRUNCATED",
            "offset": el.offset, "header_size": el.header_size,
            "size": el.size, "unknown_size": el.unknown_size,
            "range": [start, end],
            "parse_error": el.parse_error,
            "children": [ser(c, end) for c in el.children],
        }
    return {"elements": [ser(e, len(source)) for e in roots],
            "file_size": len(source)}, roots


def _node_segment(source, params, inputs):
    roots = inputs["ebml_tree"]
    m = model.build_segment_model(source, roots)
    d = model.to_dict(m)
    override = params.get("timecode_scale_override_ns")
    d["effective_timecode_scale_ns"] = override or m.timecode_scale_ns
    if override:
        d["diagnostics"].append({
            "code": "PARAM_OVERRIDE", "subject": "param:timecode_scale_override_ns",
            "message": f"reviewer override: TimecodeScale {m.timecode_scale_ns} ns "
                       f"-> {override} ns", "raw_range": None,
            "detail": {"raw_ns": m.timecode_scale_ns, "override_ns": override}})
    return d, m


def _frame_rows(m: model.SegmentModel):
    """One row per frame (or per block when boundaries are unknown)."""
    rows = []
    for c in m.clusters:
        for b in c.blocks:
            track = next((t for t in m.tracks if t.number == b.track_number), None)
            dur_ticks = None
            if track and track.default_duration_ns:
                dur_ticks = track.default_duration_ns / m.timecode_scale_ns
            base_abs = None if c.timecode is None else c.timecode + b.rel_timecode
            candidates = []
            if base_abs is None:
                # never invent a single answer: keep every legal candidate
                cue = next((q for q in m.cues if q.track == b.track_number), None)
                if cue is not None:
                    candidates.append({"basis": "cue-evidence",
                                       "abs_tick": cue.time_ticks + b.rel_timecode})
                prev = m.clusters[c.index - 1] if c.index > 0 else None
                if prev is not None and prev.timecode is not None:
                    candidates.append({"basis": "previous-cluster-end",
                                       "abs_tick": prev.timecode + 1 + b.rel_timecode})
            if b.lacing_corrupt:
                rows.append({"id": b.id, "kind": "block-unsegmented",
                             "track": b.track_number, "abs_tick": base_abs,
                             "candidates": candidates,
                             "note": "lacing corrupt: frame boundaries not guessed"})
                continue
            for k, fr in enumerate(b.frames):
                tick = base_abs
                if tick is not None and dur_ticks is not None:
                    tick = base_abs + round(k * dur_ticks)
                elif k > 0:
                    tick = None  # undecidable without a duration
                rows.append({"id": fr.id, "kind": "frame", "track": b.track_number,
                             "block": b.id, "frame_index": k, "abs_tick": tick,
                             "size": fr.size,
                             "candidates": candidates if tick is None else []})
    return rows


def _node_frames(source, params, inputs):
    m = inputs["segment"]
    rows = _frame_rows(m)
    summary = inputs.get("frame_summary") or {}
    for block_id, info in summary.items():
        # external frame summaries enter as evidence candidates; they never
        # overwrite EBML lacing facts
        for r in rows:
            if r["id"] == block_id or r.get("block") == block_id:
                r.setdefault("external_evidence", []).append(
                    {"origin": info.get("origin", "unknown"),
                     "sizes": info.get("sizes"), "unit": info.get("unit", "byte"),
                     "role": info.get("role", "evidence-candidate")})
                break
        else:
            rows.append({"id": f"{block_id}/ext", "kind": "external-evidence",
                         "track": None, "abs_tick": None,
                         "block": block_id, "sizes": info.get("sizes"),
                         "origin": info.get("origin"),
                         "candidates": []})
    return {"rows": rows}, rows


def _node_timelines(source, params, inputs):
    m = inputs["segment"]
    rows = inputs["frames"]
    scale_ns = params.get("timecode_scale_override_ns") or m.timecode_scale_ns
    apply_dp = params.get("discard_padding_policy") == "apply"
    timelines = {}
    for t in m.tracks:
        ticks = [r["abs_tick"] for r in rows
                 if r["track"] == t.number and r["abs_tick"] is not None]
        arr = np.array(sorted(set(ticks)), dtype=np.int64) if ticks else np.zeros(0, np.int64)
        # per-track timebase honoured; no cross-track force alignment
        ns = (arr.astype(np.float64) * t.timecode_scale * scale_ns).round().astype(np.int64)
        timelines[str(t.number)] = {
            "track_id": t.id, "codec": t.codec,
            "track_timecode_scale": t.timecode_scale,
            "ticks": arr.tolist(), "ns": ns.tolist(),
            "unit": "ns", "coordinate_basis": "segment-timecode-scale",
        }
    # discard padding effects (reported per block; applied to end time only)
    dp_rows = []
    for c in m.clusters:
        for b in c.blocks:
            if b.discard_padding_ns is None:
                continue
            trim_ticks = b.discard_padding_ns / scale_ns  # ns -> segment ticks
            dp_rows.append({
                "block": b.id, "raw_ns": b.discard_padding_ns, "raw_unit": "ns",
                "trim_ticks": trim_ticks, "applied": apply_dp,
                "note": "trims block end" if b.discard_padding_ns > 0
                        else "trims block start"})
    comparison = []
    if params.get("show_file_order_comparison"):
        order = [r["id"] for r in rows]
        comparison.append({
            "policy": "no-file-order-alignment",
            "note": "tracks are aligned by timecode only; file order is not a time axis",
            "file_order": order})
    return {"timelines": timelines, "discard_padding": dp_rows,
            "comparison": comparison}, None


def _node_cue_check(source, params, inputs):
    m = inputs["segment"]
    rows = []
    offsets = {c.element_offset - m.segment_data_offset: c for c in m.clusters}
    for cue in m.cues:
        target = offsets.get(cue.cluster_position)
        rows.append({
            "cue": cue.id, "time_ticks": cue.time_ticks, "track": cue.track,
            "claimed_position": cue.cluster_position,
            "resolves_to": target.id if target else None,
            "verdict": ("consistent" if target and target.timecode == cue.time_ticks
                        else "mispoint" if target is None else "time-mismatch"),
            "role": "index-evidence-not-truth",
        })
    return {"rows": rows}, rows


def _node_anomalies(source, params, inputs):
    m = inputs["segment"]
    return {"diagnostics": model.to_dict(m.diagnostics)}, None


NODE_FNS = {
    "ebml_tree": _node_ebml_tree,
    "segment": _node_segment,
    "frames": _node_frames,
    "timelines": _node_timelines,
    "cue_check": _node_cue_check,
    "anomalies": _node_anomalies,
}


def run(source: bytes, params: dict, prev: dict[str, dict] | None = None,
        changed: set[str] | None = None, frame_summary: dict | None = None
        ) -> dict[str, dict]:
    """Run the derivation graph.  `prev` maps node -> NodeResult dict of a
    previous version; `changed` lists external inputs that differ
    ("source", "param:<name>").  Nodes not downstream of a change are
    reused with an explanation."""
    params = {**DEFAULT_PARAMS, **(params or {})}
    if changed is None:
        changed = {"source", "frame_summary"} | {f"param:{k}" for k in params}
    dirty = downstream_of(changed)
    results: dict[str, dict] = {}
    raw_inputs: dict[str, object] = {"frame_summary": frame_summary}
    for node in ORDER:
        if prev and node in prev and node not in dirty:
            old = prev[node]
            results[node] = {**old, "status": "reused",
                             "explain": f"no dependency of [{', '.join(sorted(changed))}] "
                                        f"reaches '{node}'; result still valid"}
            # reused nodes must still feed raw inputs to dirty consumers
            raw_inputs[node] = _raw_from_result(node, results[node], source, params,
                                                raw_inputs)
            continue
        fn = NODE_FNS[node]
        data, raw = fn(source, params, raw_inputs)
        raw_inputs[node] = raw
        why = ", ".join(sorted(c for c in changed if c in GRAPH[node])) or "initial"
        results[node] = {"node": node, "status": "recomputed",
                         "explain": f"recomputed because [{why}] changed", "data": data}
    return results


def _raw_from_result(node, result, source, params, raw_inputs):
    """Re-materialise the in-memory by-product of a reused node so dirty
    downstream nodes can consume it without recomputing the node itself."""
    if node == "ebml_tree":
        return ebml.parse(source)
    if node == "segment":
        return model.build_segment_model(source, raw_inputs["ebml_tree"])
    if node == "frames":
        return result["data"]["rows"]
    return None


def encode_source(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def decode_source(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))
