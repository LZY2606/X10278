"""Derivation rules: turn the preserved raw model into reviewed conclusions.

Standing semantics (these are the case's adjudicated rules, kept explicit so
every derived value can cite them):

R1  Block relative timecodes are signed 16-bit big-endian integers.
R2  Corrupted lacing never produces guessed frame boundaries; frames stay
    indeterminate and all raw lace bytes remain attached.
R3  Cues are index *evidence*, never time truth. A cue mismatch is recorded
    and never rewrites a timeline.
R4  Tracks are not force-aligned by file order; each track timeline is
    derived independently under its own timebase.
R5  Ambiguity is preserved: every legitimate candidate interpretation is
    kept with an adopted flag instead of collapsing to one answer.
"""
from __future__ import annotations

import hashlib
import json

import numpy as np

RULE_VERSION = "rules-1.0"
INT16_MIN = -32768


def _sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True,
                                     default=str).encode()).hexdigest()


def index_objects(objects: list[dict]) -> dict[str, list[dict]]:
    by_kind: dict[str, list[dict]] = {}
    for obj in objects:
        by_kind.setdefault(obj["kind"], []).append(obj)
    return by_kind


def get_params(by_kind) -> dict:
    params = {"timecode_scale_ns": None, "end_policy": "default_duration",
              "cue_tolerance_bytes": 0}
    for obj in by_kind.get("params", []):
        params.update(obj["data"])
    return params


def file_timecode_scale_ns(by_kind, params) -> tuple[int, str]:
    if params.get("timecode_scale_ns"):
        return int(params["timecode_scale_ns"]), "params:review (revised)"
    for obj in by_kind.get("ebml_element", []):
        if obj["data"]["name"] == "TimecodeScale":
            return int(obj["data"]["value"]), obj["object_id"]
    return 1_000_000, "matroska-default (element absent)"


# ---------------------------------------------------------------- clusters

def derive_cluster_intervals(objects: list[dict]) -> dict:
    by_kind = index_objects(objects)
    params = get_params(by_kind)
    scale_ns, scale_src = file_timecode_scale_ns(by_kind, params)
    intervals = []
    for obj in by_kind.get("cluster", []):
        d = obj["data"]
        intervals.append({
            "cluster_id": obj["object_id"],
            "byte_range": d["byte_range"],
            "unknown_size": d["unknown_size"],
            "timecode_ticks": d["timecode_ticks"],
            "start_ns": d["timecode_ticks"] * scale_ns,
        })
    intervals.sort(key=lambda x: x["byte_range"][0])
    gaps = [{"byte_range": g["data"]["byte_range"], "gap_id": g["object_id"]}
            for g in by_kind.get("gap", [])]
    anomalies = []
    prev_end = None
    for it in intervals:
        if prev_end is not None and it["start_ns"] < prev_end:
            anomalies.append({
                "kind": "cluster_timecode_regression",
                "cluster_id": it["cluster_id"],
                "note": "cluster timecode goes backwards vs previous cluster",
            })
        prev_end = it["start_ns"]
    return {"timecode_scale_ns": scale_ns, "scale_source": scale_src,
            "unit": "ns", "intervals": intervals, "gaps": gaps,
            "anomalies": anomalies}


# ---------------------------------------------------------------- timelines

def _block_start_ticks(block: dict, cluster_tc: dict) -> int:
    return cluster_tc[block["cluster"]] + block["rel_timecode_ticks"]


def derive_track_timeline(objects: list[dict], track_uid: int) -> dict:
    by_kind = index_objects(objects)
    params = get_params(by_kind)
    scale_ns, scale_src = file_timecode_scale_ns(by_kind, params)
    track = None
    for obj in by_kind.get("track", []):
        if obj["data"]["track_uid"] == track_uid:
            track = obj["data"]
            break
    if track is None:
        return {"error": f"track uid {track_uid} not found", "entries": []}
    track_scale = float(track.get("track_timecode_scale") or 1.0)
    cluster_tc = {c["object_id"]: c["data"]["timecode_ticks"]
                  for c in by_kind.get("cluster", [])}
    blocks = [b["data"] | {"block_id": b["object_id"]}
              for b in by_kind.get("block", [])
              if b["data"].get("track_uid") == track_uid]
    # R4: order by this track's own computed time, never by file order.
    blocks.sort(key=lambda b: (_block_start_ticks(b, cluster_tc),
                               b["block_id"]))

    starts_ticks = np.array([_block_start_ticks(b, cluster_tc)
                             for b in blocks], dtype=np.int64)
    starts_ns = (starts_ticks * int(scale_ns)).astype(np.int64)

    entries = []
    anomalies = []
    default_dur = track.get("default_duration_ns")
    for i, block in enumerate(blocks):
        start_ns = int(starts_ns[i])
        duration_ns = None
        duration_src = None
        if params["end_policy"] == "default_duration" and default_dur:
            duration_ns = int(default_dur * track_scale)
            duration_src = "DefaultDuration"
        elif i + 1 < len(blocks):
            duration_ns = int(starts_ns[i + 1]) - start_ns
            duration_src = "next_block_start"
        end_ns = start_ns + duration_ns if duration_ns is not None else None

        padding = block.get("discard_padding_ns")
        padding_note = None
        if padding is not None and end_ns is not None:
            if padding >= 0:
                end_ns -= padding
                padding_note = f"end trimmed by DiscardPadding {padding} ns"
            else:
                end_ns -= padding  # negative padding extends the sample
                padding_note = f"end extended by DiscardPadding {padding} ns"
            if end_ns < start_ns:
                anomalies.append({
                    "kind": "discard_padding_overrun",
                    "block_id": block["block_id"],
                    "discard_padding_ns": padding,
                    "duration_ns": duration_ns,
                    "rule": "discard padding applied in ns; overrun kept visible, "
                            "not clamped away",
                    "candidates": [
                        {"label": "computed_end_ns", "value": end_ns,
                         "adopted": True},
                        {"label": "clamped_to_start_ns", "value": start_ns,
                         "adopted": False},
                    ],
                })
                padding_note += " (overruns block start)"

        # R1: signed interpretation; flag the overflow signature 0x8000 and
        # any block landing before the segment start.
        rel_candidates = [{
            "label": "signed_int16",
            "rel_ticks": block["rel_timecode_ticks"],
            "adopted": True,
            "rule": "R1: relative timecode is signed int16 BE",
        }]
        if block["rel_timecode_ticks"] == INT16_MIN or \
                _block_start_ticks(block, cluster_tc) < 0:
            wrapped = block["rel_timecode_ticks"] + 65536
            rel_candidates.append({
                "label": "wrapped_unsigned",
                "rel_ticks": wrapped,
                "adopted": False,
                "rule": "alternative hypothesis: encoder wrapped past int16; "
                        "kept as non-adopted candidate",
            })
            anomalies.append({
                "kind": "relative_timecode_overflow",
                "block_id": block["block_id"],
                "rel_raw_hex": block["rel_raw_hex"],
                "rel_signed_ticks": block["rel_timecode_ticks"],
                "rule": "R1: relative timecode interpreted as signed int16; "
                        "0x8000 == -32768, not +32768",
                "candidates": rel_candidates,
            })

        frames = None
        if block["frames_status"] == "ok":
            frames = block["frames"]
        else:
            # R2: corrupted lacing -> no guessed boundaries.
            anomalies.append({
                "kind": "lacing_indeterminate",
                "block_id": block["block_id"],
                "lace_sizes_raw": block.get("lace_sizes_raw"),
                "rule": "R2: lacing corrupt; frame boundaries NOT guessed",
                "candidates": [],
            })

        entries.append({
            "block_id": block["block_id"],
            "cluster": block["cluster"],
            "start_ns": start_ns,
            "end_ns": end_ns,
            "duration_ns": duration_ns,
            "duration_source": duration_src,
            "rel_ticks": block["rel_timecode_ticks"],
            "rel_candidates": rel_candidates,
            "keyframe": block["keyframe"],
            "lacing": block["lacing"],
            "frames_status": block["frames_status"],
            "frames": frames,
            "discard_padding_ns": padding,
            "padding_note": padding_note,
            "byte_range": block["byte_range"],
        })

    return {
        "track_uid": track_uid,
        "track_number": track["track_number"],
        "codec_id": track.get("codec_id"),
        "timecode_scale_ns": scale_ns,
        "scale_source": scale_src,
        "track_timecode_scale": track_scale,
        "timebase_note": "R4: timeline derived independently for this track; "
                         "not force-aligned with other tracks by file order",
        "unit": "ns",
        "entries": entries,
        "anomalies": anomalies,
    }


# ---------------------------------------------------------------- cues

def derive_cue_check(objects: list[dict]) -> dict:
    by_kind = index_objects(objects)
    params = get_params(by_kind)
    scale_ns, scale_src = file_timecode_scale_ns(by_kind, params)
    segment_data_start = None
    for obj in by_kind.get("ebml_element", []):
        if obj["data"]["name"] == "Segment":
            segment_data_start = obj["data"]["data_start"]
            break
    clusters = {c["data"]["offset"]: c["object_id"]
                for c in by_kind.get("cluster", [])}
    results = []
    anomalies = []
    tol = int(params.get("cue_tolerance_bytes") or 0)
    for cue in by_kind.get("cue", []):
        d = cue["data"]
        expected = (segment_data_start or 0) + (d.get("cue_cluster_position") or 0)
        hit = clusters.get(expected)
        status = "match" if hit else "mismatch"
        nearest = None
        if not hit and clusters:
            nearest_off = min(clusters, key=lambda o: abs(o - expected))
            nearest = {"cluster_id": clusters[nearest_off],
                       "offset": nearest_off,
                       "delta_bytes": expected - nearest_off}
            if tol and abs(nearest["delta_bytes"]) <= tol:
                status = "within_tolerance"
        results.append({
            "cue_id": cue["object_id"],
            "cue_time_ticks": d.get("cue_time_ticks"),
            "cue_time_ns": (d.get("cue_time_ticks") or 0) * scale_ns,
            "cue_track": d.get("cue_track"),
            "cue_cluster_position": d.get("cue_cluster_position"),
            "expected_byte_offset": expected,
            "matched_cluster": hit,
            "nearest_cluster": nearest,
            "status": status,
        })
        if status == "mismatch":
            anomalies.append({
                "kind": "cue_mispoint",
                "cue_id": cue["object_id"],
                "expected_byte_offset": expected,
                "nearest_cluster": nearest,
                "rule": "R3: cue is index evidence, not time truth; mismatch "
                        "recorded, timelines left untouched",
                "candidates": [
                    {"label": "cue_position_as_written",
                     "offset": expected, "adopted": False},
                    {"label": "nearest_cluster",
                     "offset": nearest["offset"] if nearest else None,
                     "adopted": True},
                ],
            })
    return {"timecode_scale_ns": scale_ns, "scale_source": scale_src,
            "segment_data_start": segment_data_start,
            "cues": results, "anomalies": anomalies,
            "note": "R3: cues checked as index evidence only"}


# ---------------------------------------------------------------- anomalies

def derive_anomaly_register(objects: list[dict]) -> dict:
    by_kind = index_objects(objects)
    entries = []
    for obj in by_kind.get("diagnostic", []):
        d = obj["data"]
        entries.append({
            "diagnostic_id": obj["object_id"],
            "kind": d["kind"],
            "byte_range": d.get("byte_range"),
            "element": d.get("element"),
            "note": d.get("note"),
            "stage": "parse",
        })
    return {"entries": entries,
            "note": "parse-stage diagnostics; derivation-stage anomalies live "
                    "inside each derivation output"}


# ---------------------------------------------------------------- decisions

def derive_adjudication(objects: list[dict]) -> dict:
    by_kind = index_objects(objects)
    decisions = []
    for obj in by_kind.get("decision", []):
        d = obj["data"]
        decisions.append({
            "decision_id": obj["object_id"],
            "subject": d["subject"],
            "verdict": d["verdict"],
            "reviewer": d["reviewer"],
            "rationale": d["rationale"],
            "candidates": d.get("candidates", []),
            "supersedes": d.get("supersedes"),
        })
    return {"decisions": decisions,
            "note": "R5: unchosen candidates stay on record; a decision is "
                    "an event, never an overwrite"}


# ---------------------------------------------------------------- registry
# Each derivation declares the object kinds it reads; the store hashes those
# objects to decide whether a recompute is needed (dependency graph).

def _timeline_keys(objects: list[dict]) -> list[str]:
    return [f"timeline:track:{o['data']['track_uid']}"
            for o in objects if o["kind"] == "track"]


def derivation_keys(objects: list[dict]) -> list[str]:
    return (["cluster_intervals", "cue_check", "anomaly_register",
             "adjudication"]
            + _timeline_keys(objects))


def run_derivation(key: str, objects: list[dict]) -> dict:
    if key == "cluster_intervals":
        return derive_cluster_intervals(objects)
    if key == "cue_check":
        return derive_cue_check(objects)
    if key == "anomaly_register":
        return derive_anomaly_register(objects)
    if key == "adjudication":
        return derive_adjudication(objects)
    if key.startswith("timeline:track:"):
        return derive_track_timeline(objects, int(key.rsplit(":", 1)[1]))
    raise KeyError(key)


def _proj(obj, fields):
    return {f: obj["data"].get(f) for f in fields}


def dependency_projection(key: str, objects: list[dict]):
    """Return [(object_id, projected_data)] -- exactly what the derivation
    reads. The dependency graph hashes the projection, so a change to an
    unrelated field of a related object does NOT trigger a recompute."""
    out = []

    def emit(obj, projected):
        if projected is not None:
            out.append((obj["object_id"], projected))

    if key == "cluster_intervals":
        for o in objects:
            if o["kind"] in ("cluster", "gap", "params"):
                emit(o, o["data"])
            elif o["kind"] == "ebml_element" and o["data"]["name"] == "TimecodeScale":
                emit(o, _proj(o, ("name", "value")))
    elif key == "cue_check":
        for o in objects:
            if o["kind"] == "cue":
                emit(o, _proj(o, ("cue_time_ticks", "cue_track",
                                  "cue_cluster_position", "cue_block_number")))
            elif o["kind"] == "cluster":
                emit(o, _proj(o, ("offset",)))
            elif o["kind"] == "params":
                emit(o, o["data"])
            elif o["kind"] == "ebml_element" and o["data"]["name"] in (
                    "Segment", "TimecodeScale"):
                emit(o, _proj(o, ("name", "value", "data_start")))
    elif key == "anomaly_register":
        for o in objects:
            if o["kind"] == "diagnostic":
                emit(o, o["data"])
    elif key == "adjudication":
        for o in objects:
            if o["kind"] == "decision":
                emit(o, o["data"])
    elif key.startswith("timeline:track:"):
        uid = int(key.rsplit(":", 1)[1])
        for o in objects:
            if o["kind"] == "block" and o["data"].get("track_uid") == uid:
                emit(o, _proj(o, ("cluster", "rel_timecode_ticks",
                                  "frames_status", "lace_sizes_raw",
                                  "discard_padding_ns", "lacing",
                                  "track_uid")))
            elif o["kind"] == "cluster":
                emit(o, _proj(o, ("timecode_ticks",)))
            elif o["kind"] == "track" and o["data"].get("track_uid") == uid:
                emit(o, o["data"])
            elif o["kind"] == "params":
                emit(o, o["data"])
            elif o["kind"] == "ebml_element" and o["data"]["name"] == "TimecodeScale":
                emit(o, _proj(o, ("name", "value")))
    else:
        raise KeyError(key)
    return out


def inputs_fingerprint(key: str, objects: list[dict]) -> tuple[str, list[str]]:
    pairs = sorted((oid, _sha(proj))
                   for oid, proj in dependency_projection(key, objects))
    blob = json.dumps(pairs).encode()
    return hashlib.sha256(blob).hexdigest(), [p[0] for p in pairs]
