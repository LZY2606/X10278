"""Dependency-graph derivation and adjudication.

Hard semantics (RULE_SEMANTICS):
  * relative block timecodes are interpreted as *signed* fixed-width integers;
    the unsigned reading is kept only as an un-adopted alternative candidate.
  * corrupted lacing -> no frame boundaries are guessed (zero fabricated
    frames); the block time still exists but frame detail is 'corrupted'.
  * cues are index *evidence*, never the source of time truth.
  * tracks are derived independently and never force-aligned by file order.
  * when more than one legal interpretation remains, every legal candidate is
    retained; nothing collapses ambiguity to a single answer.
"""
from __future__ import annotations

from typing import Any, Optional

from . import RULESET_VERSION
from .models import (DerivedNode, Candidate, oid, U_TICK, U_S16_TICK, U_NS,
                     U_SEG_BYTE, U_BYTE, DEFAULT_TIMESCALE_NS)

RULE_SEMANTICS = [
    "relative_timecode_signed_fixed_width",
    "lacing_corrupt_no_guessed_boundaries",
    "cue_is_index_evidence_not_time_truth",
    "multitrack_no_file_order_alignment",
    "retain_all_legal_candidates_when_ambiguous",
]


def _scale(facts) -> int:
    return facts.timescale["scale"]["value"] or DEFAULT_TIMESCALE_NS


def node_id(kind: str, subject_oid: str) -> str:
    return oid("n", kind, subject_oid)


def derive_all(facts, params: Optional[dict] = None,
               track_overrides: Optional[dict] = None) -> dict[str, DerivedNode]:
    params = params or {}
    track_overrides = track_overrides or {}
    scale = _scale(facts)
    nodes: dict[str, DerivedNode] = {}
    _derive_clusters(facts, scale, nodes)
    _derive_blocks(facts, scale, nodes, track_overrides)
    _derive_cues(facts, scale, nodes)
    _derive_timelines(facts, scale, nodes, track_overrides)
    return nodes


# ---- clusters ---------------------------------------------------------------
def _derive_clusters(facts, scale, nodes):
    for c in facts.clusters:
        tc = c["timecode"]["value"]
        nid = node_id("cluster_time", c["oid"])
        inputs = [c["oid"]]
        if facts.timescale["element_oid"]:
            inputs.append(facts.timescale["element_oid"])
        values = {
            "start_ns": None if tc is None else tc * scale,
            "start_tick": tc,
            "unit": U_NS,
            "raw_start": c["timecode"],
            "segment_position": c["segment_position"],
            "unknown_length": c["unknown_length"],
            "byte_range": c["raw_range"],
        }
        status = "unconvertible" if tc is None else "ok"
        nodes[nid] = DerivedNode(
            node_id=nid, kind="cluster_time", subject_oid=c["oid"],
            inputs=inputs, rule_version=RULESET_VERSION, values=values,
            status=status,
            note="missing cluster timecode; left unconverted"
            if status == "unconvertible" else "")


# ---- blocks -----------------------------------------------------------------
def _derive_blocks(facts, scale, nodes, track_overrides):
    ctime = {n.subject_oid: n for n in nodes.values()
             if n.kind == "cluster_time"}
    for b in facts.blocks:
        nid = node_id("block_time", b["oid"])
        inputs = [b["oid"], b["cluster_oid"]]
        cluster_node = ctime.get(b["cluster_oid"])
        if not b.get("framed"):
            nodes[nid] = DerivedNode(
                node_id=nid, kind="block_time", subject_oid=b["oid"],
                inputs=inputs, rule_version=RULESET_VERSION,
                values={"start_ns": None, "unit": U_NS,
                        "byte_range": b["raw_range"]},
                status="corrupted", note="block header unframeable")
            continue
        rel_signed = b["relative_timecode"]["signed"]["value"]
        rel_unsigned = b["relative_timecode"]["unsigned"]["value"]
        cluster_tc = (b["cluster_timecode"]["value"]
                      if b["cluster_timecode"] else None)
        signed_candidate = Candidate(
            candidate_id=oid("cand", b["oid"], "signed"),
            node_id=nid, label="signed 16-bit relative timecode (Matroska)",
            values={"relative_tick": rel_signed,
                    "relative_unit": U_S16_TICK},
            basis=[b["oid"], RULE_SEMANTICS[0]], status="accepted")
        unsigned_candidate = Candidate(
            candidate_id=oid("cand", b["oid"], "unsigned"),
            node_id=nid, label="unsigned 16-bit reading (raw evidence only)",
            values={"relative_tick": rel_unsigned,
                    "relative_unit": "u16_tick"},
            basis=[b["oid"]],
            status="alternative" if rel_unsigned != rel_signed
            else "rejected")
        start_ns = None
        if cluster_tc is not None:
            start_ns = (cluster_tc + rel_signed) * scale
        discard_ns = None
        if b.get("discard_padding"):
            discard_ns = b["discard_padding"]["value"] * scale
        duration_ns = None
        if b.get("block_duration"):
            duration_ns = b["block_duration"]["value"] * scale
        values = {
            "start_ns": start_ns, "start_tick": (None if cluster_tc is None
                                                 else cluster_tc + rel_signed),
            "unit": U_NS, "relative_signed_tick": rel_signed,
            "relative_unsigned_tick": rel_unsigned,
            "discard_padding_ns": discard_ns,
            "discard_padding_raw": b.get("discard_padding"),
            "block_duration_ns": duration_ns,
            "track_oid": b.get("track_oid"),
            "keyframe": b.get("keyframe"),
            "lacing_mode": (b["lacing"] or {}).get("mode")
            if b.get("lacing") else None,
            "frame_count": (b["lacing"] or {}).get("count")
            if b.get("lacing") else 0,
            "byte_range": b["raw_range"],
            "payload_range": b["payload_range"],
        }
        status = "ok"
        note = ""
        if cluster_tc is None:
            status = "unconvertible"
            note = "cluster timecode missing; block start left unconverted"
        if b.get("lacing_corrupt"):
            status = "corrupted"
            note = ("lacing corrupted; frame boundaries not guessed: "
                    + b["lacing_corrupt"])
        nodes[nid] = DerivedNode(
            node_id=nid, kind="block_time", subject_oid=b["oid"],
            inputs=inputs, rule_version=RULESET_VERSION, values=values,
            candidates=[signed_candidate.to_dict(),
                        unsigned_candidate.to_dict()],
            status=status, note=note)


# ---- cues: index evidence only ---------------------------------------------
def _derive_cues(facts, scale, nodes):
    ctime = {n.subject_oid: n for n in nodes.values()
             if n.kind == "cluster_time"}
    for cue in facts.cues:
        nid = node_id("cue_resolution", cue["oid"])
        target_node = ctime.get(cue["points_to_cluster_oid"])
        declared_ns = cue["cue_time"]["value"]
        declared_ns = None if declared_ns is None else declared_ns * scale
        target_start_ns = (target_node.values["start_ns"]
                           if target_node else None)
        agrees = (target_node is not None and declared_ns is not None
                  and target_start_ns is not None
                  and abs(declared_ns - target_start_ns) < 1)
        values = {
            "declared_cue_time_ns": declared_ns,
            "unit": U_NS,
            "cluster_position": cue["cluster_position"],
            "relative_position": cue["relative_position"],
            "absolute_position": cue["absolute_position"],
            "target_cluster_oid": cue["points_to_cluster_oid"],
            "target_cluster_start_ns": target_start_ns,
            "matches_cluster_time": agrees,
            "byte_range": cue["raw_range"],
            "track_oid": cue.get("track_oid"),
        }
        if target_node is None:
            status, note = "corrupted", "cue points where no cluster begins"
        elif not agrees:
            status = "ambiguous"
            note = ("CueTime disagrees with target cluster timecode; cue is "
                    "index evidence, not time truth; both values retained")
        else:
            status, note = "ok", "cue index consistent with cluster evidence"
        nodes[nid] = DerivedNode(
            node_id=nid, kind="cue_resolution", subject_oid=cue["oid"],
            inputs=[cue["oid"], cue["points_to_cluster_oid"]]
            if cue["points_to_cluster_oid"] else [cue["oid"]],
            rule_version=RULESET_VERSION, values=values, status=status,
            note=note)


# ---- per-track timelines (never cross-align by file order) -----------------
def _derive_timelines(facts, scale, nodes, track_overrides):
    blocks_by_track: dict[str, list] = {}
    node_by_block = {n.subject_oid: n for n in nodes.values()
                     if n.kind == "block_time"}
    for b in facts.blocks:
        if b.get("track_oid"):
            blocks_by_track.setdefault(b["track_oid"], []).append(b)

    for track in facts.tracks:
        toid = track["oid"]
        nid = node_id("track_timeline", toid)
        default_duration = None
        override = track_overrides.get(str(track["number"]["value"]))
        if override and override.get("default_duration_ns"):
            default_duration = override["default_duration_ns"]
        elif track.get("default_duration"):
            default_duration = track["default_duration"]["value"]
        blocks = sorted(blocks_by_track.get(toid, []),
                        key=lambda b: (b["cluster_index"], b["seq"]))
        entries = []
        prev_end_ns = None
        prev_ref = None
        gaps = []
        for b in blocks:
            bn = node_by_block[b["oid"]]
            start_ns = bn.values["start_ns"]
            frame_count = bn.values["frame_count"]
            if (bn.values.get("lacing_mode") and frame_count > 1
                    and default_duration):
                step = default_duration
            else:
                step = default_duration
            frame_times = []
            if start_ns is None:
                frame_times = []
            elif bn.status == "corrupted":
                # lacing corrupt: block start exists, but frame boundaries are
                # deliberately not synthesised.
                frame_times = [start_ns]
            elif frame_count > 1 and step:
                frame_times = [start_ns + i * step for i in range(frame_count)]
            else:
                frame_times = [start_ns]
            end_ns = None
            if frame_times and step:
                end_ns = frame_times[-1] + step
            entries.append({
                "block_oid": b["oid"],
                "start_ns": start_ns,
                "frame_times_ns": frame_times,
                "frame_count_known": bn.status != "corrupted",
                "status": bn.status,
                "byte_range": b["raw_range"],
                "cluster_oid": b["cluster_oid"],
                "relative_signed_tick": bn.values["relative_signed_tick"]
                if b.get("framed") else None,
                "discard_padding_ns": bn.values.get("discard_padding_ns"),
            })
            if (prev_end_ns is not None and start_ns is not None
                    and start_ns > prev_end_ns):
                gap_id = oid("gap", toid, prev_ref, b["oid"])
                gap = {
                    "oid": gap_id,
                    "track_oid": toid,
                    "from_block_oid": prev_ref,
                    "to_block_oid": b["oid"],
                    "gap_start_ns": prev_end_ns,
                    "gap_end_ns": start_ns,
                    "gap_duration_ns": start_ns - prev_end_ns,
                    "unit": U_NS,
                    "kind": "track_gap",
                }
                gaps.append(gap)
                facts_gap_node = DerivedNode(
                    node_id=node_id("gap", gap_id), kind="gap",
                    subject_oid=gap_id,
                    inputs=[prev_ref, b["oid"]],
                    rule_version=RULESET_VERSION, values=gap, status="ok",
                    note="discontinuity on this track (not filled in)")
                nodes[facts_gap_node.node_id] = facts_gap_node
            if start_ns is not None:
                prev_end_ns = end_ns if end_ns is not None else start_ns
                prev_ref = b["oid"]

        # cluster coverage gap (a cluster with no block for this track, sitting
        # between clusters that do) is also reported but never interpolated.
        present_clusters = {b["cluster_oid"] for b in blocks}
        ordered = [c for c in facts.clusters]
        seen_any = False
        for c in ordered:
            has = c["oid"] in present_clusters
            if has:
                seen_any = True
            elif seen_any and any(cc["oid"] in present_clusters
                                  for cc in ordered[ordered.index(c) + 1:]):
                gap_id = oid("clustergap", toid, c["oid"])
                cn = node_by_block  # noop
                gap = {
                    "oid": gap_id, "track_oid": toid,
                    "missing_cluster_oid": c["oid"],
                    "cluster_start_ns": (c["timecode"]["value"] * scale
                                         if c["timecode"]["value"] is not None
                                         else None),
                    "unit": U_NS, "kind": "mid_cluster_gap",
                    "byte_range": c["raw_range"],
                }
                gaps.append(gap)
                nodes[node_id("gap", gap_id)] = DerivedNode(
                    node_id=node_id("gap", gap_id), kind="gap",
                    subject_oid=gap_id, inputs=[c["oid"], toid],
                    rule_version=RULESET_VERSION, values=gap, status="ok",
                    note="cluster contributes no block to this track")

        values = {
            "track_oid": toid,
            "number": track["number"]["value"],
            "type": track["type"]["value"],
            "default_duration_ns": default_duration,
            "time_base": "track_independent",
            "entries": entries,
            "gaps": gaps,
            "byte_range": track["raw_range"],
        }
        status = "ok"
        if any(e["status"] == "corrupted" for e in entries):
            status = "corrupted"
        nodes[nid] = DerivedNode(
            node_id=nid, kind="track_timeline", subject_oid=toid,
            inputs=[toid, facts.timescale["element_oid"]
                    or "timescale:default"],
            rule_version=RULESET_VERSION, values=values, status=status,
            note="timeline derived per-track; no file-order alignment")
