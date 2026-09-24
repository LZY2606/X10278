"""Derivation engine for the Matroska timecode review.

Design rules (see repository case archive):

* Relative block timecodes are signed at their fixed 16-bit bit width.
* Damaged lacing never yields guessed frame boundaries.
* Cues are indexing evidence, never the time truth: when cue and cluster
  disagree both are retained and an anomaly is raised.
* Multi-track timelines are never force-aligned by file order.
* When a value is not uniquely decidable, every legal candidate is kept.

Every result is a node in a dependency graph keyed by a stable identity, so a
recompute only refreshes nodes whose inputs changed and can explain why any
other node is still valid.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

import numpy as np

from .units import MS, NS, scale_ticks_to_ms, ms_to_ns


ENGINE_VERSION = "engine-2026.09.1"


@dataclass
class Node:
    node_id: str
    kind: str
    deps: list[str]
    value: dict
    engine_version: str = ENGINE_VERSION

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "kind": self.kind,
            "deps": list(self.deps),
            "value": self.value,
            "engine_version": self.engine_version,
        }


@dataclass
class Anomaly:
    code: str
    severity: str
    subject: str
    message: str
    byte_range: list[int] | None = None
    candidates: list = field(default_factory=list)
    basis: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity,
            "subject": self.subject,
            "message": self.message,
            "byte_range": self.byte_range,
            "candidates": list(self.candidates),
            "basis": self.basis,
        }


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def resolve_timecode_scale(facts: dict, overrides: dict | None = None) -> Node:
    overrides = overrides or {}
    raw = facts.get("info", {}).get("timecode_scale") or {}
    wire_value = raw.get("value")
    source = "wire"
    if "timecode_scale_ns" in overrides:
        value = int(overrides["timecode_scale_ns"])
        source = "reviewer-override"
    elif wire_value:
        value = int(wire_value)
    else:
        value = 1_000_000
        source = "spec-default"
    value_dict = {
        "value": value,
        "unit": NS,
        "wire_value": wire_value,
        "source": source,
        "diagnostic": None if wire_value else "TimecodeScale absent; default used and flagged",
    }
    return Node(
        node_id="derived:timecode_scale",
        kind="timecode_scale",
        deps=["fact:info.timecode_scale"],
        value=value_dict,
    )


def derive_blocks(facts: dict, scale_node: Node) -> tuple[list[Node], list[Anomaly]]:
    """Absolute block time candidates.

    The relative timecode is signed 16-bit (range -32768..32767).  Values near
    the positive boundary are noted as overflow-suspect because the encoder may
    have meant their wrapped counterpart, but both readings are retained as
    legal candidates rather than collapsed to one answer.
    """
    scale_ns = scale_node.value["value"]
    nodes: list[Node] = []
    anomalies: list[Anomaly] = []
    for block in facts.get("blocks", []):
        node_id = f"derived:block-time:{block['stable_id']}"
        relative = block["relative_timecode"]["value"]
        cluster_ticks = block.get("cluster_timecode_ticks")
        raw_range = block["raw_range"]
        candidates_ticks: list[int] = []
        if relative is None or cluster_ticks is None:
            readings = []
            anomalies.append(
                Anomaly(
                    code="block:timecode-unreadable",
                    severity="error",
                    subject=block["stable_id"],
                    message="block or cluster timecode missing; no unique time",
                    byte_range=raw_range,
                    candidates=[],
                    basis={"relative": relative, "cluster_ticks": cluster_ticks},
                )
            )
        else:
            primary = cluster_ticks + relative
            readings = [primary]
            candidates_ticks = [primary]
            if relative >= 0x6000:
                wrapped = cluster_ticks + (relative - 0x10000)
                readings.append(wrapped)
                candidates_ticks.append(wrapped)
                anomalies.append(
                    Anomaly(
                        code="relative-timecode:overflow-suspect",
                        severity="warning",
                        subject=block["stable_id"],
                        message=(
                            "relative timecode near signed16 positive boundary; "
                            "wrapped negative reading retained as legal candidate"
                        ),
                        byte_range=raw_range,
                        candidates=readings,
                        basis={"relative_signed16": relative, "cluster_ticks": cluster_ticks},
                    )
                )
            if primary < 0:
                anomalies.append(
                    Anomaly(
                        code="block:negative-time",
                        severity="warning",
                        subject=block["stable_id"],
                        message="cluster + signed relative timecode precedes segment zero",
                        byte_range=raw_range,
                        candidates=[primary],
                    )
                )

        discard_ns = None
        group = block.get("block_group")
        if group and group.get("discard_padding_ticks") is not None:
            # DiscardPadding is stored directly in nanoseconds.
            discard_ns = int(group["discard_padding_ticks"])
            if discard_ns < 0:
                anomalies.append(
                    Anomaly(
                        code="discard-padding:negative-trailing",
                        severity="info",
                        subject=block["stable_id"],
                        message="negative DiscardPadding marks encoder delay at block start",
                        byte_range=group.get("discard_raw_range"),
                        candidates=[discard_ns],
                    )
                )

        lacing = block.get("lacing")
        if lacing and lacing.get("damaged"):
            anomalies.append(
                Anomaly(
                    code="lacing:damaged",
                    severity="error",
                    subject=block["stable_id"],
                    message="lacing damaged; frame boundaries after damage point are not guessed",
                    byte_range=raw_range,
                    candidates=[frame["index"] for frame in lacing["frames"] if frame["proven"]],
                    basis={"codes": lacing["codes"], "damage_offset": lacing["damage_offset"]},
                )
            )

        if block.get("parse_error"):
            anomalies.append(
                Anomaly(
                    code="block:parse-error",
                    severity="error",
                    subject=block["stable_id"],
                    message=block["parse_error"],
                    byte_range=raw_range,
                )
            )

        nodes.append(
            Node(
                node_id=node_id,
                kind="block_time",
                deps=["fact:block:" + block["stable_id"], "derived:timecode_scale"],
                value={
                    "track_number": block["track_number"],
                    "relative_signed16_ticks": relative,
                    "cluster_ticks": cluster_ticks,
                    "absolute_tick_candidates": candidates_ticks,
                    "absolute_ms_candidates": [
                        scale_ticks_to_ms(tick, scale_ns) for tick in candidates_ticks
                    ],
                    "unit_ms": MS,
                    "discard_padding_ns": discard_ns,
                    "presentation_start_ms": (
                        scale_ticks_to_ms(candidates_ticks[0], scale_ns)
                        + (discard_ns or 0) / 1_000_000.0
                        if candidates_ticks
                        else None
                    ),
                    "lacing_damaged": bool(lacing and lacing.get("damaged")),
                    "lacing": lacing,
                    "raw_range": raw_range,
                },
            )
        )
    return nodes, anomalies


def derive_cues(facts: dict, scale_node: Node, cluster_nodes: list[Node]) -> tuple[list[Node], list[Anomaly]]:
    scale_ns = scale_node.value["value"]
    clusters_by_offset = {
        cluster["header_start"]: cluster for cluster in facts.get("clusters", [])
    }
    nodes: list[Node] = []
    anomalies: list[Anomaly] = []
    for cue in facts.get("cues", []):
        time_ticks = cue["cue_time_ticks"]["value"]
        cue_ms = scale_ticks_to_ms(time_ticks, scale_ns) if time_ticks is not None else None
        target_offset = None
        target_cluster = None
        absolute = cue.get("absolute_cluster_byte")
        if absolute and absolute.get("value") is not None:
            target_offset = absolute["value"]
            target_cluster = clusters_by_offset.get(target_offset)

        cluster_ms = None
        if target_cluster is not None:
            cluster_ms = scale_ticks_to_ms(
                target_cluster["timecode_ticks"]["value"], scale_ns
            ) if target_cluster["timecode_ticks"]["value"] is not None else None

        mismatch = None
        if cue_ms is not None and cluster_ms is not None and abs(cue_ms - cluster_ms) > 0.5:
            mismatch = abs(cue_ms - cluster_ms)
            anomalies.append(
                Anomaly(
                    code="cue:mispointing",
                    severity="warning",
                    subject=cue["stable_id"],
                    message=(
                        "CueTime does not match the referenced cluster timecode; "
                        "cue retained as index evidence, cluster time retained as time truth"
                    ),
                    byte_range=cue["raw_range"],
                    candidates=[cue_ms, cluster_ms],
                    basis={
                        "cue_ms": cue_ms,
                        "cluster_ms": cluster_ms,
                        "absolute_cluster_byte": target_offset,
                    },
                )
            )
        if target_cluster is None and target_offset is not None:
            anomalies.append(
                Anomaly(
                    code="cue:dangling-pointer",
                    severity="error",
                    subject=cue["stable_id"],
                    message="CueClusterPosition points outside any parsed cluster",
                    byte_range=cue["raw_range"],
                    candidates=[target_offset],
                )
            )

        node_id = f"derived:cue-resolve:{cue['stable_id']}"
        deps = ["fact:cue:" + cue["stable_id"], "derived:timecode_scale"]
        if target_cluster is not None:
            deps.append(f"derived:cluster-interval:{target_cluster['cluster_index']}")
        nodes.append(
            Node(
                node_id=node_id,
                kind="cue_resolution",
                deps=deps,
                value={
                    "track_number": cue["track_number"],
                    "cue_time_ms": cue_ms,
                    "target_cluster_index": target_cluster["cluster_index"] if target_cluster else None,
                    "target_byte": target_offset,
                    "target_cluster_time_ms": cluster_ms,
                    "mismatch_ms": mismatch,
                    "raw_range": cue["raw_range"],
                },
            )
        )
    return nodes, anomalies


def derive_track_timelines(
    facts: dict, scale_node: Node, block_nodes: list[Node]
) -> tuple[list[Node], list[Anomaly]]:
    """One lane per track number.

    Lanes are never aligned by file order: each entry keeps its own derived
    time candidates and source block.
    """
    block_index = {block["stable_id"]: block for block in facts.get("blocks", [])}
    tracks = {track["track_number"]: track for track in facts.get("tracks", [])}
    lanes: dict[int, list[dict]] = {}
    for node in block_nodes:
        block = block_index[node.node_id.replace("derived:block-time:", "")]
        track_number = node.value["track_number"]
        lanes.setdefault(track_number, []).append(
            {
                "block_id": block["stable_id"],
                "time_ms_candidates": node.value["absolute_ms_candidates"],
                "presentation_start_ms": node.value["presentation_start_ms"],
                "keyframe": block["keyframe"],
                "lacing_damaged": node.value["lacing_damaged"],
                "frame_count": (node.value["lacing"] or {}).get("frame_count"),
                "byte_range": node.value["raw_range"],
            }
        )

    nodes: list[Node] = []
    anomalies: list[Anomaly] = []
    for track_number, entries in lanes.items():
        times = np.array(
            [entry["time_ms_candidates"][0] for entry in entries if entry["time_ms_candidates"]],
            dtype=float,
        )
        deltas = np.diff(np.sort(times)) if times.size >= 2 else np.array([], dtype=float)
        track = tracks.get(track_number, {})
        expected_delta = None
        default_duration = track.get("default_duration") or {}
        if default_duration.get("value"):
            expected_delta = default_duration["value"] / 1_000_000.0
        gaps = []
        if deltas.size:
            if expected_delta:
                gap_mask = deltas > expected_delta * 2.5
            else:
                median = float(np.median(deltas))
                gap_mask = deltas > median * 5
            gaps = [float(value) for value in deltas[gap_mask]]
        if gaps:
            anomalies.append(
                Anomaly(
                    code="track:timeline-gap",
                    severity="warning",
                    subject=f"track-{track_number}",
                    message="per-track presentation gaps detected (lanes not file-order aligned)",
                    candidates=gaps,
                    basis={"expected_delta_ms": expected_delta},
                )
            )
        nodes.append(
            Node(
                node_id=f"derived:timeline:track-{track_number}",
                kind="track_timeline",
                deps=["fact:track:" + str(track_number)]
                + [f"derived:block-time:{entry['block_id']}" for entry in entries],
                value={
                    "track_number": track_number,
                    "timebase_kind": track.get("timebase_kind"),
                    "entries": entries,
                    "gap_ms_candidates": gaps,
                },
            )
        )

    declared = set(tracks.keys())
    seen = set(lanes.keys())
    for track_number in declared - seen:
        anomalies.append(
            Anomaly(
                code="track:no-blocks",
                severity="info",
                subject=f"track-{track_number}",
                message="track declared in Track headers but no blocks found",
            )
        )
    for track_number in seen - declared:
        anomalies.append(
            Anomaly(
                code="track:block-without-header",
                severity="error",
                subject=f"track-{track_number}",
                message="blocks reference a track number absent from Track headers",
            )
        )
    return nodes, anomalies


def derive_clusters(facts: dict, scale_node: Node, block_nodes: list[Node]) -> tuple[list[Node], list[Anomaly]]:
    scale_ns = scale_node.value["value"]
    by_cluster: dict[int, list[Node]] = {}
    for node in block_nodes:
        cluster_header = next(
            block["cluster_header_start"]
            for block in facts["blocks"]
            if f"derived:block-time:block@{block['header_start']}" == node.node_id
        )
        by_cluster.setdefault(cluster_header, []).append(node)

    nodes: list[Node] = []
    anomalies: list[Anomaly] = []
    cluster_records = facts.get("clusters", [])
    starts_ms = []
    for cluster in cluster_records:
        ticks = cluster["timecode_ticks"]["value"]
        start_ms = scale_ticks_to_ms(ticks, scale_ns) if ticks is not None else None
        starts_ms.append(start_ms)
        children = by_cluster.get(cluster["header_start"], [])
        end_candidates = [
            candidate
            for child in children
            for candidate in child.value["absolute_ms_candidates"]
        ]
        node = Node(
            node_id=f"derived:cluster-interval:{cluster['cluster_index']}",
            kind="cluster_interval",
            deps=[
                f"fact:cluster:{cluster['cluster_index']}",
                "derived:timecode_scale",
            ]
            + [child.node_id for child in children],
            value={
                "cluster_index": cluster["cluster_index"],
                "start_ms": start_ms,
                "end_ms_hint": max(end_candidates) if end_candidates else start_ms,
                "unknown_length": cluster["unknown_length"],
                "truncated": cluster["truncated"],
                "byte_range": cluster["raw_range"],
                "segment_relative_position": cluster["segment_relative_position"],
            },
        )
        nodes.append(node)
        if cluster["unknown_length"]:
            anomalies.append(
                Anomaly(
                    code="cluster:unknown-length",
                    severity="info",
                    subject=f"cluster#{cluster['cluster_index']}",
                    message="cluster uses unknown length; interval end recovered from children",
                    byte_range=cluster["raw_range"],
                )
            )
        if cluster["truncated"]:
            anomalies.append(
                Anomaly(
                    code="cluster:truncated",
                    severity="error",
                    subject=f"cluster#{cluster['cluster_index']}",
                    message="cluster payload runs past file end",
                    byte_range=cluster["raw_range"],
                )
            )

    # Mid-stream gaps: compare consecutive cluster starts (NumPy).  Gaps are
    # computed in cluster-time terms, not from byte adjacency.
    if len(starts_ms) >= 2:
        array = np.array([value for value in starts_ms if value is not None], dtype=float)
        if array.size >= 2:
            deltas = np.diff(array)
            median = float(np.median(deltas)) if deltas.size else 0.0
            ordered = [cluster for cluster, value in zip(cluster_records, starts_ms) if value is not None]
            for index, delta in enumerate(deltas):
                if median > 0 and delta > max(median * 5, median + 2000.0):
                    anomalies.append(
                        Anomaly(
                            code="cluster:midstream-gap",
                            severity="warning",
                            subject=f"cluster#{ordered[index + 1]['cluster_index']}",
                            message=(
                                f"cluster start gap {float(delta):.3f} ms far exceeds "
                                f"local spacing {median:.3f} ms"
                            ),
                            byte_range=ordered[index + 1]["raw_range"],
                            candidates=[float(delta), median],
                            basis={"delta_ms": float(delta), "median_ms": median},
                        )
                    )
    return nodes, anomalies
