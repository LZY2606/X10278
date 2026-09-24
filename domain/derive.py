"""Derived layer with a dependency graph and incremental propagation.

Nodes:
  source.*        immutable imported facts
  param.*         reviewable parameters (e.g. timecode scale assumption)
  block_time.*    normalised block times (ns) per block
  frame_time.*    per-frame times (multiple candidates when ambiguous)
  track_timeline.* per-track ordered timeline (not file-order aligned)
  gap.*           mid-stream gaps
  cue_check.*     cue-as-evidence comparisons
  corrupt.*       lacing corruption findings (no guessed boundaries)

Each derived node records why it is still valid or why it was recomputed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import ids
from .time import raw_to_ns, detect_gaps, cue_consistency, NS_PER_MS


@dataclass
class Node:
    id: str
    kind: str
    target: str
    deps: list
    value: Any
    status: str = "valid"          # valid | candidate | rejected | uncertain
    candidates: list = field(default_factory=list)
    reason: str = ""
    source_hash: str = ""
    rule_version: str = ""
    accepted_index: int | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "target": self.target,
            "deps": self.deps, "value": self.value, "status": self.status,
            "candidates": self.candidates, "reason": self.reason,
            "source_hash": self.source_hash, "rule_version": self.rule_version,
            "accepted_index": self.accepted_index,
        }


class DependencyGraph:
    def __init__(self, version: str, rule_version: str):
        self.version = version
        self.rule_version = rule_version
        self.nodes: dict[str, Node] = {}
        self.reverse: dict[str, set[str]] = {}

    def add(self, kind, target, deps, value, reason, status="valid",
            candidates=None, accepted_index=None):
        nid = ids.node_id(self.version, kind, target)
        node = Node(
            id=nid, kind=kind, target=target, deps=list(deps), value=value,
            status=status, candidates=candidates or [], reason=reason,
            rule_version=self.rule_version, accepted_index=accepted_index,
        )
        self.nodes[nid] = node
        for d in deps:
            self.reverse.setdefault(d, set()).add(nid)
        return node

    def dependents(self, source_targets: set[str]) -> set[str]:
        """Return node ids transitively affected by changed source targets."""
        affected: set[str] = set()
        frontier = set()
        for n in self.nodes.values():
            if any(_dep_matches(d, source_targets) for d in n.deps):
                frontier.add(n.id)
        while frontier:
            nid = frontier.pop()
            if nid in affected:
                continue
            affected.add(nid)
            for child in self.reverse.get(nid, ()):  # noqa
                pass
            # reverse edges are keyed by dep node-id or dep token
            for child in self.nodes.values():
                if nid in child.deps and child.id not in affected:
                    frontier.add(child.id)
        return affected

    def serialize(self) -> list[dict]:
        return [n.to_dict() for n in self.nodes.values()]


def _dep_matches(dep: str, targets: set[str]) -> bool:
    return dep in targets or any(dep.endswith(":" + t) for t in targets)


def build_graph(version: str, doc: dict, rule_version: str,
                params: dict | None = None) -> DependencyGraph:
    params = params or {}
    g = DependencyGraph(version, rule_version)
    scale = params.get("timecode_scale", doc["timecode_scale"])
    tracks_by_num = {t["number"]: t for t in doc["tracks"]}

    # Source nodes for tracks and parameters.
    for t in doc["tracks"]:
        g.add("source.track", t["id"], [], t, "imported track parameter")

    block_points_by_track: dict[int, list[dict]] = {}
    block_node_targets = []
    for cl in doc["clusters"]:
        # cluster time
        clq = raw_to_ns(cl["timecode_raw"] or 0, scale)
        g.add("cluster_time", cl["id"],
              ["param:timecode_scale", f"source:cluster:{cl['id']}"],
              {"raw": cl["timecode_raw"], "ns": clq["ns"],
               "unit": "ns", "basis": clq["basis"]},
              "cluster Timecode normalised via TimecodeScale")

        for b in cl["blocks"]:
            tgt = b["id"]
            block_node_targets.append(tgt)
            track = tracks_by_num.get(b["track_number"])
            tts = track["track_timecode_scale"] if track else 1.0
            toff = track["track_offset"] if track else 0
            bq = raw_to_ns(b["rel_tc_signed"], scale, tts, toff)
            # Cluster Timecode and block relative time share the track time
            # base: (cluster + relative) * TimecodeScale * TrackTimecodeScale;
            # TrackOffset shifts only the block presentation time.
            block_ns = ((cl["timecode_raw"] or 0) + b["rel_tc_signed"])                 * scale * tts + toff * scale

            overflow_note = ("relative-timecode at signed-width extreme; "
                             "kept raw and signed interpretations") \
                if b["tc_overflow"] else ""
            value = {
                "block_id": b["id"], "track": b["track_number"],
                "rel_tc_raw": b["rel_tc_raw"], "rel_tc_signed": b["rel_tc_signed"],
                "tc_width_bits": b["tc_width_bits"],
                "cluster_time_raw": cl["timecode_raw"],
                "start_ns": block_ns, "unit": "ns", "basis": bq["basis"],
                "overflow": b["tc_overflow"], "note": overflow_note,
            }
            g.add("block_time", tgt,
                  [ids.node_id(version, "cluster_time", cl["id"]),
                   f"source:track:{track['id']}" if track else "source:track:none"],
                  value,
                  "signed fixed-width relative timecode added to cluster time",
                  status="uncertain" if b["tc_overflow"] else "valid")

            # discard padding
            if b["discard_padding_raw"] is not None:
                g.add("discard_padding", b["id"] + ":discard",
                      [ids.node_id(version, "block_time", tgt)],
                      {"block_id": b["id"], "raw": b["discard_padding_raw"],
                       "unit": "nanoseconds (signed)",
                       "ns": b["discard_padding_raw"],
                       "meaning": "trim presentation start (>=0) / end (<0); "
                                  "evidence retained, not applied to file order"},
                      "DiscardPadding is per-block trim evidence")

            # corrupt lacing -> no frame boundary guessing; candidate spans
            if b["corrupt"]:
                g.add("frame_time", b["id"] + ":frames",
                      [ids.node_id(version, "block_time", tgt)],
                      {"block_id": b["id"], "frame_times": [],
                       "untrusted_span": [b["payload_start"], b["end"]]},
                      "lacing corrupt: frame boundaries not guessed",
                      status="uncertain",
                      candidates=[{
                          "label": "whole-block-untrusted-span",
                          "span": [b["payload_start"], b["end"]],
                          "trusted": False,
                      }])
            else:
                frame_times = []
                default_dur = track["default_duration"] if track else None
                for i, f in enumerate(b["frames"]):
                    start = block_ns
                    end = (start + default_dur) if default_dur else None
                    frame_times.append({
                        "frame_id": f["id"], "index": i,
                        "start_ns": start, "end_ns": end,
                        "duration_basis": "track DefaultDuration(ns)"
                        if default_dur else "unset: end unknown",
                        "trusted": f["trusted"],
                    })
                g.add("frame_time", b["id"] + ":frames",
                      [ids.node_id(version, "block_time", tgt)],
                      {"block_id": b["id"], "frame_times": frame_times},
                      "frame times anchored at block start; DefaultDuration only "
                      "when track supplies it")

            block_points_by_track.setdefault(b["track_number"], []).append({
                "track": b["track_number"],
                "start_ns": None if b["corrupt"] else block_ns,
                "end_ns": (block_ns + (track["default_duration"] or 0))
                if track and not b["corrupt"] else None,
                "cluster_start": cl["start"], "block_id": b["id"],
            })

    # Per-track timelines: grouped per track number, sorted by ns (never file
    # order across tracks).
    for num, points in sorted(block_points_by_track.items()):
        ordered = sorted((p for p in points if p["start_ns"] is not None),
                         key=lambda p: p["start_ns"])
        dropped = [p["block_id"] for p in points if p["start_ns"] is None]
        g.add("track_timeline", f"track-{num}",
              [ids.node_id(version, "block_time", p["block_id"])
               for p in points],
              {"track": num, "points": ordered,
               "excluded_corrupt": dropped,
               "alignment": "per-track ns order; not file-order aligned"},
              "tracks keep independent time bases")

        for gap in detect_gaps(ordered):
            g.add("gap", f"{num}@{gap.cluster_a_start}:{gap.cluster_b_start}",
                  [ids.node_id(version, "track_timeline", f"track-{num}")],
                  gap.to_dict(),
                  "mid-stream cluster gap detected from ns spacing")

    # Cues are evidence only; compare with actual cluster coordinates.
    cluster_by_pos = {c["segment_relative_pos"]: c for c in doc["clusters"]}
    for cue in doc["cues"]:
        target_cluster = cluster_by_pos.get(cue["cluster_position"])
        if target_cluster is None:
            g.add("cue_check", cue["id"], ["source:cue:" + cue["id"]],
                  {"cue_id": cue["id"], "position_match": False,
                   "evidence_only": True,
                   "diagnostic": "cue cluster position resolves to no cluster"},
                  "cue is index evidence; target missing", status="uncertain",
                  candidates=[{
                      "label": "nearest-cluster-by-byte",
                      "cluster_positions": [c["segment_relative_pos"]
                                            for c in doc["clusters"]]}])
            continue
        result = cue_consistency(
            cue["time_raw"], cue["cluster_position"],
            target_cluster["segment_relative_pos"],
            target_cluster["timecode_raw"], scale)
        status = "valid" if result["position_match"] and result["time_match"] \
            else "uncertain"
        candidates = []
        if not result["position_match"]:
            candidates = [{"label": "actual-cluster",
                           "cluster_id": target_cluster["id"],
                           "position": target_cluster["segment_relative_pos"]}]
        g.add("cue_check", cue["id"],
              ["source:cue:" + cue["id"]], result,
              "cue interpreted as index evidence, not time truth",
              status=status, candidates=candidates)

    g.add("param", "timecode_scale", ["source:document"],
          {"raw_present": doc["timecode_scale_present"],
           "value_ns": scale, "unit": "ns-per-tick"},
          "TimecodeScale from source" if doc["timecode_scale_present"]
          else "TimecodeScale absent: default 1,000,000 ns retained as "
               "assumption candidate",
          status="valid" if doc["timecode_scale_present"] else "candidate")
    return g


def _stable_key(node: dict) -> tuple:
    # version-independent identity: kind + target survive recomputation.
    return (node["kind"], node["target"])


def affected_targets(before: list[dict], after: list[dict]) -> dict:
    """Explain which derived nodes stay valid / need recompute.

    Node ids embed the version, so comparison uses stable (kind,target) keys.
    """
    before_map = {_stable_key(n): n for n in before}
    report = {"recomputed": [], "still_valid": [], "new": []}
    for node in after:
        key = _stable_key(node)
        old = before_map.get(key)
        if old is None:
            report["new"].append({"id": node["id"], "kind": node["kind"],
                                  "target": node["target"],
                                  "reason": node["reason"]})
        elif _value_key(old["value"]) != _value_key(node["value"]) or \
                old["rule_version"] != node["rule_version"]:
            report["recomputed"].append({
                "id": node["id"], "kind": node["kind"],
                "target": node["target"], "reason": node["reason"]})
        else:
            report["still_valid"].append({
                "id": node["id"], "kind": node["kind"],
                "target": node["target"],
                "reason": "dependencies unchanged; result still valid"})
    return report


def _value_key(v):
    import json
    return json.dumps(v, sort_keys=True, default=str)
