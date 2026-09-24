"""Domain extraction: raw Matroska facts with units and coordinates.

This layer never normalises across units and never silently drops a value.
Every quantity is a structured record carrying its original representation,
its unit / coordinate basis and the source element path that produced it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Optional

from . import catalog as C
from .block import BlockInfo, LaceError, parse_block
from .parser import Element, flatten


def stable_id(prefix: str, *parts: object) -> str:
    payload = "|".join(str(p) for p in parts)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


def quantity(value, unit: str, *, raw=None, raw_width: int | None = None,
             signed: bool | None = None) -> dict:
    return {
        "value": value,
        "unit": unit,
        "raw": value if raw is None else raw,
        "raw_width_bytes": raw_width,
        "signed": signed,
    }


@dataclass
class ParsedSource:
    file_name: str
    sha256: str
    size: int
    elements: list[Element]
    parse_diagnostics: list[dict] = field(default_factory=list)


def _src(element: Element, sha: str, note: str = "") -> dict:
    return {
        "element": catalog.elem_name(element.element_id),
        "element_id": element.element_id,
        "path": list(element.path),
        "range": {
            "id_start": element.start,
            "size_pos": element.size_pos,
            "data_start": element.data_start,
            "data_end": element.data_end,
            "unknown_size": element.unknown_size,
            "declared_size": element.declared_size,
        },
        "source_sha256": sha,
        "note": note,
    }


def extract(parsed: ParsedSource) -> dict:
    sha = parsed.sha256
    elems = flatten(parsed.elements)
    seg = next((e for e in elems if e.element_id == C.SEGMENT), None)
    unknown_lengths = [
        {
            "id": stable_id("unklen", sha, e.start),
            "element": catalog.elem_name(e.element_id),
            "range": {"id_start": e.start, "size_pos": e.size_pos,
                      "data_start": e.data_start, "data_end": e.data_end},
        }
        for e in elems if e.unknown_size
    ]

    scale_elem = next((e for e in elems if e.element_id == C.TIMESTAMP_SCALE), None)
    scale_ns = scale_elem.value if scale_elem is not None else 1_000_000

    tracks = _extract_tracks(parsed, scale_ns)
    track_by_number = {t["number"]: t for t in tracks}
    clusters, blocks, damage = _extract_clusters(parsed, track_by_number, scale_ns)
    cues, cue_damage = _extract_cues(parsed, seg, clusters)
    damage.extend(cue_damage)

    return {
        "file": parsed.file_name,
        "sha256": sha,
        "size": parsed.size,
        "segment": (
            {
                "id": stable_id("seg", sha),
                "unknown_size": seg.unknown_size,
                "range": {
                    "id_start": seg.start,
                    "size_pos": seg.size_pos,
                    "data_start": seg.data_start,
                    "data_end": seg.data_end,
                },
            }
            if seg is not None else None
        ),
        "timecode_scale": {
            "id": stable_id("scale", sha),
            "scale": quantity(scale_ns, "ns/tick", raw=scale_ns),
            "source": _src(scale_elem, sha) if scale_elem is not None else None,
        },
        "unknown_lengths": unknown_lengths,
        "tracks": tracks,
        "clusters": clusters,
        "blocks": blocks,
        "cues": cues,
        "damage": damage,
        "elements": [e.to_json() for e in parsed.elements],
    }


def _extract_tracks(parsed: ParsedSource, scale_ns: int) -> list[dict]:
    sha = parsed.sha256
    out = []
    for entry in (e for e in flatten(parsed.elements) if e.element_id == C.TRACK_ENTRY):
        child = {c.element_id: c for c in entry.children}
        number = child[C.TRACK_NUMBER].value
        track = {
            "id": stable_id("track", sha, number),
            "number": number,
            "uid": child[C.TRACK_UID].value if C.TRACK_UID in child else None,
            "type": child[C.TRACK_TYPE].value if C.TRACK_TYPE in child else None,
            "codec": child[C.CODEC_ID].value if C.CODEC_ID in child else None,
            "name": child[C.NAME].value if C.NAME in child else None,
            "default_flag": child[C.FLAG_DEFAULT].value if C.FLAG_DEFAULT in child else 1,
            "timebase": {
                # The Matroska segment timecode scale is shared.  Per-track
                # divergence is a parameter annotation, not a raw field.
                "scale_ns_per_tick": quantity(
                    child[C.TRACK_TIMECODE_SCALE].value * scale_ns
                    if C.TRACK_TIMECODE_SCALE in child else scale_ns,
                    "ns/tick",
                ),
                "source": _src(entry, sha),
            },
            "default_duration": (
                quantity(child[C.DEFAULT_DURATION].value, "ns")
                if C.DEFAULT_DURATION in child else None
            ),
            "source": _src(entry, sha),
        }
        out.append(track)
    return out


def _extract_clusters(parsed: ParsedSource, tracks: dict, scale_ns: int):
    sha = parsed.sha256
    elems = flatten(parsed.elements)
    clusters = []
    blocks = []
    damage = []
    cluster_index = 0
    for cluster in (e for e in elems if e.element_id == C.CLUSTER):
        tc_elem = next((c for c in cluster.children
                        if c.element_id == C.CLUSTER_TIMESTAMP), None)
        cluster_rec = {
            "id": stable_id("cluster", sha, cluster.start),
            "index": cluster_index,
            "timecode": quantity(
                tc_elem.value if tc_elem else None, "tick",
                raw=tc_elem.value if tc_elem else None, signed=True,
                raw_width=(tc_elem.data_end - tc_elem.data_start) if tc_elem else None,
            ),
            "range": {
                "id_start": cluster.start,
                "data_start": cluster.data_start,
                "data_end": cluster.data_end,
                "unknown_size": cluster.unknown_size,
            },
            "source": _src(cluster, sha),
            "block_ids": [],
        }
        cluster_index += 1
        block_seq = 0
        for child in cluster.children:
            group = child if child.element_id == C.BLOCK_GROUP else None
            block_elem = None
            if child.element_id in (C.BLOCK, C.SIMPLE_BLOCK):
                block_elem = child
            elif group is not None:
                block_elem = next(
                    (g for g in group.children if g.element_id == C.BLOCK), None
                )
            if block_elem is None:
                continue
            raw = bytes.fromhex(block_elem.data_hex)
            block_rec = _block_record(
                raw, block_elem, cluster_rec, group, sha, block_seq
            )
            blocks.append(block_rec)
            cluster_rec["block_ids"].append(block_rec["id"])
            if block_rec["lace_status"] == "corrupt":
                damage.append({
                    "id": stable_id("dmg", sha, block_elem.start, "lace"),
                    "kind": "lacing_corrupt",
                    "message": block_rec["lace_error"],
                    "affected": [block_rec["id"]],
                    "byte_range": {
                        "data_start": block_elem.data_start + block_rec["payload_start"],
                        "data_end": block_elem.data_end,
                    },
                })
            block_seq += 1
        clusters.append(cluster_rec)
    return clusters, blocks, damage


def _block_record(raw: bytes, block_elem: Element, cluster_rec: dict,
                  group: Optional[Element], sha: str, seq: int) -> dict:
    info: BlockInfo = parse_block(raw)
    discard = None
    group_children = {}
    if group is not None:
        group_children = {g.element_id: g for g in group.children}
        dp = group_children.get(C.DISCARD_PADDING)
        if dp is not None:
            discard = {
                "id": stable_id("discard", sha, dp.start),
                "ns": quantity(dp.value, "ns", raw=dp.value, signed=True,
                               raw_width=dp.data_end - dp.data_start),
                "source": _src(dp, sha),
            }
    frames = [
        {
            "index": i,
            "block_relative_range": {"start": a, "end": b},
            "absolute_range": {
                "data_start": block_elem.data_start + a,
                "data_end": block_elem.data_start + b,
            },
        }
        for i, (a, b) in enumerate(info.frame_ranges)
    ]
    return {
        "id": stable_id("block", sha, block_elem.start),
        "cluster_id": cluster_rec["id"],
        "cluster_ticks": cluster_rec["timecode"]["value"],
        "sequence": seq,
        "track_number": info.track_number,
        "simple": block_elem.element_id == C.SIMPLE_BLOCK,
        "relative_timecode": quantity(
            info.relative_ticks_signed, "signed tick",
            raw=info.relative_ticks_raw, signed=True,
            raw_width=info.tc_field_width,
        ),
        "flags": info.flags,
        "lacing": info.lacing,
        "keyframe": info.keyframe,
        "frame_count": info.frame_count,
        "frames": frames if info.lace_status == "ok" else [],
        "frame_boundaries_unknown": info.lace_status == "corrupt",
        "lace_status": info.lace_status,
        "lace_error": info.lace_error,
        "payload_start": info.payload_start,
        "discard_padding": discard,
        "byte_range": {
            "id_start": block_elem.start,
            "data_start": block_elem.data_start,
            "data_end": block_elem.data_end,
        },
        "source": _src(block_elem, sha),
    }


def _extract_cues(parsed: ParsedSource, seg: Optional[Element], clusters: list[dict]):
    sha = parsed.sha256
    cues = []
    damage = []
    elems = flatten(parsed.elements)
    cluster_starts = sorted(c["range"]["id_start"] for c in clusters)
    seg_data_start = seg.data_start if seg is not None else 0
    for point in (e for e in elems if e.element_id == C.CUE_POINT):
        pc = {c.element_id: c for c in point.children}
        positions = [c for c in point.children if c.element_id == C.CUE_TRACK_POSITIONS]
        for pos in positions:
            pcc = {c.element_id: c for c in pos.children}
            rel_pos = pcc[C.CUE_CLUSTER_POSITION].value
            absolute = seg_data_start + rel_pos
            target_cluster = next(
                (c for c in clusters if c["range"]["id_start"] == absolute), None
            )
            cue = {
                "id": stable_id("cue", sha, point.start, pcc[C.CUE_TRACK].value),
                "cue_time": quantity(pc[C.CUE_TIME].value, "tick", raw=pc[C.CUE_TIME].value),
                "track": pcc[C.CUE_TRACK].value,
                "cluster_position": quantity(rel_pos, "byte (segment-relative)"),
                "absolute_file_offset": absolute,
                "resolves_to_cluster_id": target_cluster["id"] if target_cluster else None,
                "status": "resolved" if target_cluster else "mispoint",
                "source": _src(point, sha),
            }
            if not target_cluster:
                damage.append({
                    "id": stable_id("dmg", sha, point.start, "cue"),
                    "kind": "cue_mispoint",
                    "message": (
                        f"CueClusterPosition {rel_pos} (file offset {absolute}) "
                        "does not land on a cluster element start"
                    ),
                    "affected": [cue["id"]],
                    "byte_range": {
                        "data_start": pcc[C.CUE_CLUSTER_POSITION].data_start,
                        "data_end": pcc[C.CUE_CLUSTER_POSITION].data_end,
                    },
                })
            cues.append(cue)
    return cues, damage
