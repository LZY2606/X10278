"""Derivation layer: raw evidence -> normalised timeline, gaps, cue checks.

Everything computed here is versioned and linked to source/rule evidence.
Derived node names form the dependency graph used for incremental recompute.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np

from .model import MatroskaDoc, Cluster, BlockEntity
from .units import scaled_ticks_to_ns, ticks_to_ns, NS, RAW_TICKS

# Node names in the dependency graph -----------------------------------------
N_SCALE = "scale"
N_BLOCK_PTS = "block_pts"
N_OVERFLOW = "relative_overflow"
N_GAPS = "cluster_gaps"
N_CUE_CHECK = "cue_check"
N_TRACK_TIMELINES = "track_timelines"

ALL_NODES = [N_SCALE, N_BLOCK_PTS, N_OVERFLOW, N_GAPS, N_CUE_CHECK,
             N_TRACK_TIMELINES]

NODE_DEPS = {
    N_SCALE: (),
    N_BLOCK_PTS: ("source", N_SCALE),
    N_OVERFLOW: ("source", N_BLOCK_PTS),
    N_GAPS: ("source", N_SCALE, N_BLOCK_PTS),
    N_CUE_CHECK: ("source", N_SCALE, N_BLOCK_PTS),
    N_TRACK_TIMELINES: ("source", N_SCALE, N_BLOCK_PTS),
}

RULE_VERSION = "rules:matroska-ts@1"
SIGNED_RULE = ("rule:relative-timecode-signed-16bit@1",
               "Block relative timecode is signed int16; 0xFFFF == -1")
NO_GUESS_RULE = ("rule:lacing-corrupt-no-guess@1",
                 "corrupt lacing yields no frame boundary candidates")
CUE_EVIDENCE_RULE = ("rule:cue-is-index-evidence@1",
                     "Cues index clusters; they are evidence, not time truth")
INDEPENDENT_TRACK_RULE = ("rule:multi-track-independent@1",
                          "each track has its own timebase; never align by "
                          "file order")


@dataclass
class DerivedBundle:
    params: dict
    nodes: dict[str, Any]
    evidence: dict[str, list[dict]] = field(default_factory=dict)
    rules: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"params": self.params, "nodes": self.nodes,
                "evidence": self.evidence, "rules": self.rules}


def _track_map(doc: MatroskaDoc) -> dict[int, dict]:
    return {t.number: t for t in doc.tracks}


def compute(doc: MatroskaDoc, params: Optional[dict] = None,
            only: Optional[set[str]] = None) -> DerivedBundle:
    """Compute (or recompute) derived nodes.

    ``params`` may carry ``timecode_scale_ns`` and per-track
    ``track_timestamp_scale`` overrides produced by a human parameter revision.
    """
    params = dict(params or {})
    tcs_override = params.get("timecode_scale_ns")
    tscale = float(tcs_override if tcs_override is not None
                   else doc.timecode_scale_ns)
    ttrack = params.get("track_timestamp_scale", {})
    tmap = _track_map(doc)
    rules = [RULE_VERSION, SIGNED_RULE[0], NO_GUESS_RULE[0],
             CUE_EVIDENCE_RULE[0], INDEPENDENT_TRACK_RULE[0]]
    bundle = DerivedBundle(
        params={"timecode_scale_ns": tscale,
                "track_timestamp_scale": {
                    str(n): float(ttrack.get(str(n),
                                             t.track_timestamp_scale))
                    for n, t in tmap.items()}},
        nodes={}, evidence={}, rules=rules)

    want = only or set(ALL_NODES)

    if N_SCALE in want:
        bundle.nodes[N_SCALE] = {
            "timecode_scale_ns": q_(tscale, NS, doc.timecode_scale_ns),
            "tracks": {
                str(t.number): {
                    "track_timestamp_scale":
                        q_(float(ttrack.get(str(t.number),
                                           t.track_timestamp_scale)),
                           "dimensionless-ratio", t.track_timestamp_scale),
                    "default_duration_ns": t.default_duration_ns,
                } for t in tmap.values()
            },
        }

    if N_BLOCK_PTS in want:
        bundle.nodes[N_BLOCK_PTS] = _block_pts(doc, tscale, ttrack, tmap,
                                               bundle)
    if N_OVERFLOW in want:
        bundle.nodes[N_OVERFLOW] = _relative_overflow(doc)
    if N_GAPS in want:
        bundle.nodes[N_GAPS] = _cluster_gaps(doc, tscale, ttrack, tmap)
    if N_CUE_CHECK in want:
        bundle.nodes[N_CUE_CHECK] = _cue_check(doc, tscale, ttrack, tmap)
    if N_TRACK_TIMELINES in want:
        bundle.nodes[N_TRACK_TIMELINES] = _track_timelines(
            doc, tscale, ttrack, tmap)
    return bundle


def q_(value, unit, raw):
    from .units import q
    return q(value, unit, raw).to_dict()


def _block_pts(doc, tscale, ttrack, tmap, bundle):
    out = []
    # Vectorised signed int16 interpretation over the whole file.
    raws = np.array([b.relative_ticks_raw for cl in doc.clusters
                     for b in cl.blocks], dtype=">u2").astype(np.int64)
    cl_ticks = np.array([cl.timecode_ticks for cl in doc.clusters
                         for _b in cl.blocks], dtype=np.int64)
    signed = raws.astype(">i2").astype(np.int64)
    abs_ticks = cl_ticks + signed
    idx = 0
    for cl in doc.clusters:
        for b in cl.blocks:
            tts = float(ttrack.get(str(b.track_number),
                                   tmap.get(b.track_number,
                                            type("T", (),
                                                 {"track_timestamp_scale": 1.0}
                                                 )).track_timestamp_scale))
            pts = scaled_ticks_to_ns(int(abs_ticks[idx]), tscale, tts)
            out.append({
                "block_uid": b.uid,
                "cluster_uid": cl.uid,
                "track_number": b.track_number,
                "cluster_ticks": cl.timecode_ticks,
                "relative_ticks_signed": int(signed[idx]),
                "absolute_ticks": int(abs_ticks[idx]),
                "pts_ns": pts,
                "unit": NS,
                "rule": SIGNED_RULE[0],
                "corrupt_lace": b.corrupt,
            })
            idx += 1
    return out


def _relative_overflow(doc):
    """Flag Block relative timecodes whose signed interpretation moves the
    block out of the cluster's expected tick window.

    We keep both readings (signed int16 is adopted; zero-extended uint16 is
    the rejected alternative) so reviewers can see what was not chosen.
    """
    anomalies = []
    for cl in doc.clusters:
        for b in cl.blocks:
            signed = b.relative_ticks_signed
            unsigned = b.relative_ticks_raw
            # Cluster ordering: blocks normally stay >= cluster timecode and
            # before the next cluster. A negative signed value whose unsigned
            # reading would have been large is the classic overflow marker.
            raw_tick = cl.timecode_ticks + unsigned
            signed_tick = cl.timecode_ticks + signed
            candidates = [
                {"interpretation": "signed-int16",
                 "absolute_ticks": signed_tick, "adopted": True,
                 "basis": SIGNED_RULE[0]},
                {"interpretation": "zero-extended-uint16",
                 "absolute_ticks": raw_tick, "adopted": False,
                 "basis": "rejected: Matroska stores signed relative time"},
            ]
            if signed < 0 or unsigned >= 0x8000:
                anomalies.append({
                    "code": "relative_timecode.high-bit",
                    "block_uid": b.uid,
                    "cluster_uid": cl.uid,
                    "raw_unsigned": unsigned,
                    "signed": signed,
                    "signed_absolute_ticks": signed_tick,
                    "unsigned_absolute_ticks": raw_tick,
                    "candidates": candidates,
                    "rule": SIGNED_RULE[0],
                    "severity": "warning" if signed < 0 else "info",
                })
    return anomalies


def _cluster_gaps(doc, tscale, ttrack, tmap):
    ordered = sorted(doc.clusters, key=lambda c: c.timecode_ticks)
    gaps = []
    for prev, nxt in zip(ordered, ordered[1:]):
        last_end = prev.timecode_ticks
        for b in prev.blocks:
            # A corrupt lace keeps one opaque extent; duration unknown.
            end_tick = prev.timecode_ticks + b.relative_ticks_signed
            if b.block_duration_ticks is not None:
                end_tick += b.block_duration_ticks
            last_end = max(last_end, end_tick)
        gap_ticks = nxt.timecode_ticks - last_end
        if gap_ticks > 0:
            gaps.append({
                "code": "cluster.gap",
                "left_cluster_uid": prev.uid,
                "right_cluster_uid": nxt.uid,
                "from_ticks": last_end,
                "to_ticks": nxt.timecode_ticks,
                "gap_ticks": gap_ticks,
                "gap_ns_at_scale": ticks_to_ns(gap_ticks, tscale),
                "unit_ticks": RAW_TICKS,
                "rule": RULE_VERSION,
                "severity": "info",
            })
    return gaps


def _cue_check(doc, tscale, ttrack, tmap):
    """Cue entries are index evidence: resolve their bytes against actual
    clusters/blocks, but never overwrite the observed timeline with them."""
    seg_data = doc.segment_data_pos
    pts_by_track: dict[int, list[dict]] = {}
    for cl in doc.clusters:
        for b in cl.blocks:
            tts = float(ttrack.get(str(b.track_number),
                                   tmap.get(b.track_number,
                                            type("T", (),
                                                 {"track_timestamp_scale": 1.0}
                                                 )).track_timestamp_scale))
            ticks = cl.timecode_ticks + b.relative_ticks_signed
            pts = scaled_ticks_to_ns(ticks, tscale, tts)
            pts_by_track.setdefault(b.track_number, []).append(
                {"block_uid": b.uid, "cluster_uid": cl.uid,
                 "cluster_file_pos": cl.pos,
                 "block_file_pos": b.pos, "ticks": ticks, "pts_ns": pts})
    out = []
    for cue in doc.cues:
        file_pos = (seg_data + cue.cluster_position_segrel
                    if cue.cluster_position_segrel is not None else None)
        actual = next((c for c in doc.clusters if c.pos == file_pos), None)
        observed = pts_by_track.get(cue.track_number, [])
        match_tick = next((o for o in observed
                           if o["cluster_uid"] == (actual.uid if actual
                                                   else None)
                           and o["ticks"] == cue.cue_time_ticks), None)
        status = "consistent"
        if actual is None:
            status = "mispointed"
        elif match_tick is None:
            status = "time-disagrees"
        out.append({
            "cue_uid": cue.uid,
            "track_number": cue.track_number,
            "cue_time_ticks": cue.cue_time_ticks,
            "resolved_cluster_file_pos": file_pos,
            "resolved_cluster_uid": actual.uid if actual else None,
            "status": status,
            "observed_block_uid": match_tick["block_uid"] if match_tick
            else None,
            "rule": CUE_EVIDENCE_RULE[0],
            "severity": "error" if status == "mispointed" else (
                "warning" if status == "time-disagrees" else "info"),
        })
    return out


def _track_timelines(doc, tscale, ttrack, tmap):
    """Independent per-track timelines. Multi-track data is never force-aligned
    by file order: each track is placed on its own timebase."""
    lines: dict[int, list[dict]] = {}
    for cl in doc.clusters:
        for b in cl.blocks:
            tts = float(ttrack.get(str(b.track_number),
                                   tmap.get(b.track_number,
                                            type("T", (),
                                                 {"track_timestamp_scale": 1.0}
                                                 )).track_timestamp_scale))
            ticks = cl.timecode_ticks + b.relative_ticks_signed
            pts = scaled_ticks_to_ns(ticks, tscale, tts)
            lines.setdefault(b.track_number, []).append({
                "block_uid": b.uid,
                "cluster_uid": cl.uid,
                "absolute_ticks": ticks,
                "pts_ns": pts,
                "corrupt_lace": b.corrupt,
                "file_pos": b.pos,
                "keyframe": b.keyframe,
                "discard_padding_ns": b.discard_padding_ns,
            })
    return {
        str(num): {
            "rule": INDEPENDENT_TRACK_RULE[0],
            "track_timestamp_scale": float(ttrack.get(str(num),
                                            tmap[num].track_timestamp_scale))
            if num in tmap else 1.0,
            "points": sorted(points, key=lambda p: p["pts_ns"]),
        } for num, points in lines.items()
    }


def affected_nodes_for_source_change() -> set[str]:
    return set(ALL_NODES) - {N_SCALE}


def affected_nodes_for_param_change(param: str) -> set[str]:
    return {N_BLOCK_PTS, N_GAPS, N_CUE_CHECK, N_TRACK_TIMELINES, N_OVERFLOW}


def explain(node: str, changed_inputs: set[str]) -> str:
    """Why a node is still valid or needs recomputation."""
    deps = set(d for d in NODE_DEPS[node] if d != "source")
    if changed_inputs & deps or "source" in changed_inputs and \
            "source" in NODE_DEPS[node]:
        return f"{node} depends on {sorted(changed_inputs)}; recomputed"
    return f"{node} unchanged: its dependencies {sorted(deps)} were not touched"
