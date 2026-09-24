"""Derived layer: ticks -> nanoseconds, timelines, cue resolution, gaps.

Every derived node carries:
  * stable key (for the dependency graph),
  * inputs it read,
  * all legal candidates with accepted/rejected status and the rule id
    that decided the matter.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .analysis import Document, ClusterRaw
from .blocks import Block
from .rules import RULES


@dataclass
class Candidate:
    value: object
    status: str             # accepted | rejected
    basis: str
    rule: str | None = None

    def as_dict(self) -> dict:
        return {"value": self.value, "status": self.status,
                "basis": self.basis, "rule": self.rule}


@dataclass
class Node:
    key: str
    kind: str
    inputs: list[str]
    value: dict
    candidates: list[Candidate] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"key": self.key, "kind": self.kind, "inputs": self.inputs,
                "value": self.value,
                "candidates": [c.as_dict() for c in self.candidates]}


@dataclass
class Context:
    scale_ns: int
    source: str            # "container" | "decision:<event_id>"
    decided_event: str | None = None


def _block_media_end_ns(block: Block, start_ns: int, eff_scale: float):
    end_ns = None
    if block.duration_ticks is not None:
        end_ns = start_ns + round(block.duration_ticks * eff_scale)
    else:
        lacing = block.lacing
        if lacing.frames and lacing.frames[-1].size is not None:
            end_ns = start_ns
    return end_ns


def _build_block_nodes(doc: Document, ctx: Context) -> list[Node]:
    nodes: list[Node] = []
    for cluster in doc.clusters:
        for block in cluster.blocks:
            track = doc.track(block.track_number)
            eff_scale = ctx.scale_ns
            basis = f"TimecodeScale={ctx.scale_ns}ns ({ctx.source})"
            if track is not None and track.track_scale is not None:
                eff_scale = ctx.scale_ns * track.track_scale
                basis += f" * TrackTimecodeScale={track.track_scale!r}"
            inputs = [f"raw:block:{block.block_id}",
                      f"raw:cluster:{cluster.cluster_id}",
                      "param:timecode_scale"]
            cands: list[Candidate] = []
            accepted_ns = None
            damaged = block.lacing.damaged
            if cluster.timecode_ticks is not None:
                signed_ticks = cluster.timecode_ticks + block.rel_ticks_signed
                accepted_ns = round(signed_ticks * eff_scale)
                unsigned_rel = int.from_bytes(block.rel_ticks_raw, "big")
                unsigned_ticks = cluster.timecode_ticks + unsigned_rel
                cands.append(Candidate(
                    {"relative_ticks": block.rel_ticks_signed,
                     "absolute_ticks": signed_ticks,
                     "absolute_ns": accepted_ns},
                    "accepted",
                    "relative timecode is the fixed-width signed reading",
                    "R-SIGNED-16"))
                if unsigned_rel != block.rel_ticks_signed:
                    cands.append(Candidate(
                        {"relative_ticks": unsigned_rel,
                         "absolute_ticks": unsigned_ticks,
                         "absolute_ns": round(unsigned_ticks * eff_scale)},
                        "rejected",
                        "unsigned reading of a high-bit-set relative timecode "
                        "would treat in-range negative offsets as overflow",
                        "R-SIGNED-16"))
            media_start_ns = accepted_ns
            discard_ns = block.discard_padding_ns
            media_end_ns = None
            if accepted_ns is not None:
                media_end_ns = accepted_ns
                if block.duration_ticks is not None:
                    media_end_ns = accepted_ns + round(
                        block.duration_ticks * eff_scale)
                if discard_ns:
                    media_end_ns = max(accepted_ns, media_end_ns - discard_ns)
            if damaged:
                cands.append(Candidate(
                    {"frames": "opaque-payload", "frame_count": 1},
                    "accepted",
                    "lacing metadata inconsistent with payload: one opaque "
                    "payload, damaged byte range reported",
                    "R-LACING-NO-GUESS"))
                cands.append(Candidate(
                    {"frames": "inferred-boundaries"},
                    "rejected",
                    "guessed frame boundaries would fabricate evidence",
                    "R-LACING-NO-GUESS"))
            value = {
                "block_id": block.block_id,
                "cluster_id": cluster.cluster_id,
                "track_number": block.track_number,
                "kind": block.kind,
                "cluster_ticks": cluster.timecode_ticks,
                "relative_ticks_raw_hex": block.rel_ticks_raw.hex(),
                "relative_ticks_signed": block.rel_ticks_signed,
                "absolute_ticks": (cluster.timecode_ticks +
                                  block.rel_ticks_signed)
                    if cluster.timecode_ticks is not None else None,
                "time_unit": "nanosecond",
                "time_basis": basis,
                "presentation_ns": accepted_ns,
                "media_start_ns": media_start_ns,
                "media_end_ns": media_end_ns,
                "discard_padding_ns": discard_ns,
                "keyframe": block.keyframe,
                "byte_range": list(block.byte_range),
                "payload_range": list(block.payload_range),
                "lacing": block.lacing.as_dict(),
            }
            nodes.append(Node(f"block:{block.block_id}", "block_time",
                             inputs, value, cands))
    return nodes


def _build_timeline_nodes(doc: Document, block_nodes: list[Node],
                           ctx: Context) -> list[Node]:
    by_track: dict[int, list[Node]] = {}
    for node in block_nodes:
        by_track.setdefault(node.value["track_number"], []).append(node)
    out: list[Node] = []
    for track in doc.tracks:
        nodes = by_track.get(track.track_number, [])
        nodes.sort(key=lambda n: (n.value["presentation_ns"]
                                  if n.value["presentation_ns"] is not None
                                  else 1 << 62, n.value["byte_range"][0]))
        eff = ctx.scale_ns * (track.track_scale or 1.0)
        basis = "per-track own ticks/scale; not interleaved file order"
        out.append(Node(
            f"timeline:track:{track.track_number}", "track_timeline",
            [f"raw:track:{track.track_number}",
             "param:timecode_scale"] +
            [n.key for n in nodes],
            {
                "track_number": track.track_number,
                "track_type": track.type_name,
                "name": track.name,
                "default_duration_ns": track.default_duration_ns,
                "effective_scale_ns": eff,
                "time_unit": "nanosecond",
                "time_basis": basis,
                "blocks": [n.value["block_id"] for n in nodes],
                "starts_ns": [n.value["presentation_ns"] for n in nodes],
            },
            [Candidate("independent track timebase", "accepted", basis,
                       "R-MULTITRACK-NO-FILE-ORDER"),
             Candidate("force-align by file interleaving", "rejected",
                       "tracks have independent timebases and may legitimately "
                       "start at different times",
                       "R-MULTITRACK-NO-FILE-ORDER")]))
    return out


def _build_cue_nodes(doc: Document, block_nodes: list[Node],
                    ctx: Context) -> list[Node]:
    block_index = {n.value["block_id"]: n for n in block_nodes}
    clusters_by_offset = {c.data_offset: c for c in doc.clusters}
    out: list[Node] = []
    for cue in doc.cues:
        target_offset = doc.segment["data_offset"] + cue.cluster_position
        cluster: ClusterRaw | None = clusters_by_offset.get(target_offset)
        inputs = [f"raw:cue:{cue.cue_id}", "raw:segment:offset",
                  "param:timecode_scale"]
        cands: list[Candidate] = []
        value = {
            "cue_id": cue.cue_id,
            "track_number": cue.track_number,
            "cue_time_ticks": cue.cue_time_ticks,
            "cue_time_ns": round(cue.cue_time_ticks * ctx.scale_ns),
            "declared_cluster_position": cue.cluster_position,
            "resolved_segment_offset": target_offset,
            "element_range": cue.element_range,
        }
        if cluster is None:
            value["status"] = "orphan"
            value["resolved_cluster_id"] = None
            value["actual_time_ns"] = None
            cands.append(Candidate("orphan-position", "accepted",
                                  "declared position resolves to no cluster "
                                  "data offset; cue retained as evidence",
                                  "R-CUE-EVIDENCE"))
        else:
            actual_ns = (round(cluster.timecode_ticks * ctx.scale_ns)
                         if cluster.timecode_ticks is not None else None)
            value["status"] = "confirmed"
            value["resolved_cluster_id"] = cluster.cluster_id
            value["actual_time_ns"] = actual_ns
            rel_ok = None
            if cue.relative_position is not None:
                target_abs = target_offset + cue.relative_position
                rel_ok = any(b.byte_range[0] <= target_abs < b.byte_range[1]
                            for c in doc.clusters
                            if c.cluster_id == cluster.cluster_id
                            for b in c.blocks)
            matches = (actual_ns == value["cue_time_ns"])
            if matches and rel_ok is not False:
                cands.append(Candidate("cue-agrees-with-cluster", "accepted",
                                      "cue position resolves to the indexed "
                                      "cluster and times agree",
                                      "R-CUE-EVIDENCE"))
            else:
                value["status"] = "misdirected"
                cands.append(Candidate(
                    {"status": "misdirected", "actual_time_ns": actual_ns},
                    "accepted",
                    "cue contradicts cluster timecode or block range; the "
                    "cluster/block evidence governs the timeline",
                    "R-CUE-EVIDENCE"))
            cands.append(Candidate("trust-cue-time-as-truth", "rejected",
                                  "that would let an index overwrite frame "
                                  "evidence", "R-CUE-EVIDENCE"))
        inputs.append(f"raw:cluster:{cluster.cluster_id}"
                      if cluster else "raw:cluster:<missing>")
        out.append(Node(f"cue:{cue.cue_id}", "cue_resolution", inputs,
                        value, cands))
    return out


def _build_gap_nodes(doc: Document, block_nodes: list[Node],
                    ctx: Context) -> list[Node]:
    starts = {}
    for n in block_nodes:
        cid = n.value["cluster_id"]
        ns = n.value["presentation_ns"]
        if ns is None:
            continue
        end = n.value["media_end_ns"] if n.value["media_end_ns"] is not None else ns
        if cid not in starts or end > starts[cid][1]:
            starts[cid] = (ns, end)
    out: list[Node] = []
    timed = [c for c in doc.clusters if c.timecode_ticks is not None]
    timed.sort(key=lambda c: c.timecode_ticks)
    for prev, nxt in zip(timed, timed[1:]):
        prev_end = starts.get(prev.cluster_id, (None, None))[1]
        next_start = round(nxt.timecode_ticks * ctx.scale_ns)
        if prev_end is None:
            prev_end = round(prev.timecode_ticks * ctx.scale_ns)
        gap = next_start - prev_end
        if gap > 0:
            out.append(Node(
                f"gap:{prev.cluster_id}->{nxt.cluster_id}", "cluster_gap",
                [f"raw:cluster:{prev.cluster_id}",
                 f"raw:cluster:{nxt.cluster_id}", "param:timecode_scale"],
                {"from_cluster": prev.cluster_id,
                 "to_cluster": nxt.cluster_id,
                 "gap_start_ns": prev_end, "gap_end_ns": next_start,
                 "gap_ns": gap, "time_unit": "nanosecond",
                 "byte_range": [
                     (prev.byte_range[1] if prev.byte_range[1] is not None
                      else prev.id_offset), nxt.id_offset]},
                [Candidate("report-gap", "accepted",
                           "non-adjacent cluster timecodes expose a real "
                           "timeline gap; it is not papered over",
                           "R-MULTITRACK-NO-FILE-ORDER")]))
    return out


def derive(doc: Document, ctx: Context) -> dict:
    block_nodes = _build_block_nodes(doc, ctx)
    timeline_nodes = _build_timeline_nodes(doc, block_nodes, ctx)
    cue_nodes = _build_cue_nodes(doc, block_nodes, ctx)
    gap_nodes = _build_gap_nodes(doc, block_nodes, ctx)
    nodes = block_nodes + timeline_nodes + cue_nodes + gap_nodes
    return {
        "scale_ns": ctx.scale_ns,
        "scale_source": ctx.source,
        "decided_event": ctx.decided_event,
        "nodes": [n.as_dict() for n in nodes],
    }


def graph_edges(view: dict) -> dict[str, list[str]]:
    return {n["key"]: n["inputs"] for n in view["nodes"]}
