"""Derived domain layer: time mapping, lacing frame candidates, cue checks.

Judgement invariants (mirrored as machine-readable ``rule_assertion`` events):

R1 relative-timecode-signed : relative block timecodes are interpreted with a
   signed, fixed 16-bit width; the unsigned reading is recorded but rejected.
R2 lacing-no-guess           : corrupted lacing yields no frame boundaries and
   naive even/guessed splits are retained only as *rejected* candidates.
R3 cue-is-evidence           : Cue points are index evidence; they never define
   frame time truth. A cue that misses the real block is flagged, never used
   to move the timeline.
R4 no-file-order-alignment   : tracks each keep their own time base; frames are
   ordered by time, never by file interleaving order.
R5 keep-all-candidates       : when frame time cannot be uniquely decided, all
   legal candidates survive for adjudication.
"""
from __future__ import annotations

import numpy as np

from .units import Q

SCALE_DEFAULT_NS = 1_000_000


# ---------------------------------------------------------------------------
# Node builders
# ---------------------------------------------------------------------------
def register(graph) -> None:
    graph.register(
        "timebase",
        inputs=_timebase_inputs,
        compute=lambda doc, vals: _timebase(doc),
    )
    graph.register(
        "tracks",
        inputs=lambda doc: doc.get("tracks", []),
        compute=lambda doc, vals: _tracks(doc, vals["timebase"]),
        deps=lambda doc: ["timebase"],
    )
    graph.register(
        "clusters",
        inputs=lambda doc: [
            {"id": c["identity"], "tc": c["timecode_raw"],
             "off": c["element_offset"], "unk": c["unknown_length"]}
            for c in doc.get("clusters", [])
        ],
        compute=lambda doc, vals: _clusters(doc, vals["timebase"]),
        deps=lambda doc: ["timebase"],
    )
    graph.register(
        "blocks",
        inputs=lambda doc: [_block_fp(b) for b in doc.get("blocks", [])],
        compute=lambda doc, vals: _blocks(doc, vals["timebase"], vals["clusters"]),
        deps=lambda doc: ["timebase", "clusters"],
    )
    graph.register(
        "frames",
        inputs=lambda doc: [_block_fp(b) for b in doc.get("blocks", [])],
        compute=lambda doc, vals: _frames(doc, vals["timebase"], vals["blocks"],
                                          vals["tracks"]),
        deps=lambda doc: ["timebase", "blocks", "tracks"],
    )
    graph.register(
        "cues",
        inputs=lambda doc: doc.get("cues", []) + [
            {"seg_start": doc.get("segment_data_start")}],
        compute=lambda doc, vals: _cues(doc, vals["timebase"], vals["clusters"],
                                        vals["blocks"]),
        deps=lambda doc: ["timebase", "clusters", "blocks"],
    )
    graph.register(
        "diagnostics",
        inputs=lambda doc: doc.get("parse_diagnostics", []),
        compute=lambda doc, vals: _diagnostics(doc, vals),
        deps=lambda doc: ["timebase", "tracks", "clusters", "blocks", "frames",
                          "cues"],
    )
    graph.register(
        "timeline",
        inputs=lambda doc: {"n_tracks": len(doc.get("tracks", [])),
                            "n_blocks": len(doc.get("blocks", []))},
        compute=lambda doc, vals: _timeline(vals["frames"], vals["tracks"]),
        deps=lambda doc: ["frames", "tracks"],
    )
    graph.register(
        "gaps",
        inputs=lambda doc: [c["timecode_raw"] for c in doc.get("clusters", [])],
        compute=lambda doc, vals: _gaps(doc, vals["timebase"], vals["clusters"],
                                        vals["blocks"]),
        deps=lambda doc: ["timebase", "clusters", "blocks"],
    )


def _block_fp(b: dict) -> dict:
    return {
        "id": b["identity"], "track": b["track_number"], "rel": b["rel_tc"],
        "lacing": b["lacing"], "frames": b["frames"],
        "errors": b["block_errors"], "duration": b.get("block_duration_raw"),
        "discard": b.get("discard_padding_raw"),
        "cluster": b["cluster_identity"], "raw": b["raw_range"],
        "payload": b["payload_range"], "kind": b["kind"],
        "flags": b["flags"],
    }


def _timebase_inputs(doc: dict) -> dict:
    return {"timecode_scale_raw": doc.get("timecode_scale_raw"),
            "segment_unknown": doc.get("segment_unknown_length")}


def _timebase(doc: dict) -> dict:
    raw = doc.get("timecode_scale_raw")
    if raw is None or raw <= 0:
        diag = None if raw is None else {
            "code": "invalid-timecode-scale", "raw": raw,
            "message": "non-positive TimecodeScale kept raw; default not forced",
        }
        return {
            "timecode_scale_raw": raw,
            "timecode_scale_ns": None,
            "scale_valid": raw is not None and raw > 0,
            "diagnostic": diag,
        }
    return {
        "timecode_scale_raw": raw,
        "timecode_scale_ns": raw,
        "scale_valid": True,
        "diagnostic": None,
    }


def _tracks(doc: dict, timebase: dict) -> list:
    out = []
    for t in doc.get("tracks", []):
        period_ns = t.get("default_duration_ns")
        out.append({
            "identity": t["identity"], "number": t["number"], "uid": t.get("uid"),
            "type": t["type"], "codec_id": t["codec_id"], "name": t.get("name"),
            "frame_period": (
                Q(period_ns, "ns", "TrackEntry/DefaultDuration").as_dict()
                if period_ns is not None else None),
            "flag_lacing": t.get("flag_lacing"),
            "video": t.get("video", {}), "audio": t.get("audio", {}),
            "byte_range": t["byte_range"],
        })
    return out


def _clusters(doc: dict, timebase: dict) -> dict:
    scale = timebase.get("timecode_scale_ns")
    out = []
    for c in doc.get("clusters", []):
        raw = c["timecode_raw"]
        ns = (raw * scale) if raw is not None and scale else None
        out.append({
            "identity": c["identity"], "timecode_raw": raw,
            "start_ns": ns,
            "start": Q(ns, "ns", "ClusterTimecode*TimecodeScale").as_dict()
            if ns is not None else None,
            "element_offset": c["element_offset"],
            "payload_range": c["payload_range"],
            "unknown_length": c["unknown_length"],
            "blocks": c.get("blocks", []),
        })
    return {"order": out, "by_id": {c["identity"]: c for c in out}}


def _blocks(doc: dict, timebase: dict, clusters: dict) -> list:
    scale = timebase.get("timecode_scale_ns")
    cbyid = clusters["by_id"]
    out = []
    for b in doc.get("blocks", []):
        c = cbyid.get(b["cluster_identity"], {})
        ctc = c.get("timecode_raw")
        abs_scale_units = (ctc + b["rel_tc"]) if ctc is not None else None
        abs_ns = abs_scale_units * scale if abs_scale_units is not None and scale else None
        unsigned16 = b["rel_tc"] % 65536
        unsigned_ns = (
            (ctc + unsigned16) * scale
            if ctc is not None and scale else None)
        duration_ns = (
            b["block_duration_raw"] * scale
            if b.get("block_duration_raw") is not None and scale else None)
        out.append({
            "identity": b["identity"], "cluster": b["cluster_identity"],
            "kind": b["kind"], "track_number": b["track_number"],
            "rel_tc": Q(b["rel_tc"], "scale-unit",
                        "signed16(relative timecode)").as_dict(),
            "rel_tc_raw_hex": b["rel_tc_raw"],
            "absolute_ns": abs_ns,
            "absolute": Q(abs_ns, "ns",
                          "(ClusterTimecode + signed16(relTC))*TimecodeScale"
                          ).as_dict() if abs_ns is not None else None,
            "unsigned_alt": {
                "rel_tc_unsigned16": unsigned16,
                "absolute_ns": unsigned_ns,
                "accepted": False,
                "rule": "R1",
                "reason": "relative timecode uses signed 16-bit width",
            },
            "block_duration_ns": duration_ns,
            "discard_padding": (
                Q(b["discard_padding_raw"], "ns",
                  "BlockGroup/DiscardPadding").as_dict()
                if b.get("discard_padding_raw") is not None else None),
            "lacing": b["lacing"], "frame_count": b["frame_count"],
            "frames_raw": b["frames"], "block_errors": b["block_errors"],
            "flags": b["flags"], "keyframe": b["keyframe"],
            "discardable": b["discardable"],
            "raw_range": b["raw_range"], "payload_range": b["payload_range"],
            "negative": abs_ns is not None and abs_ns < 0,
        })
    return out


def _frames(doc: dict, timebase: dict, blocks: list, tracks: list) -> list:
    tby_num = {t["number"]: t for t in tracks}
    period_by_num = {
        n: (t["frame_period"]["value"] if t.get("frame_period") else None)
        for n, t in tby_num.items()}
    out: list = []
    for b in blocks:
        base_ns = b["absolute_ns"]
        period = period_by_num.get(b["track_number"])
        count = b["frame_count"]
        corrupt = bool(b["block_errors"]) or b["frames_raw"] is None
        if corrupt:
            # R2: never guess frame boundaries.  Emit one anchored frame marker
            # plus a rejected naive even-split candidate set.
            payload_len = b["payload_range"][1] - b["payload_range"][0]
            out.append({
                "identity": f"{b['identity']}:f0", "block": b["identity"],
                "track_number": b["track_number"], "index": 0,
                "anchored_ns": base_ns,
                "byte_range": [b["payload_range"][0], b["payload_range"][1]],
                "status": "corrupt-lacing", "selected": None,
                "candidates": [{
                    "label": "unparseable (no guessed boundaries)",
                    "times_ns": [base_ns] if base_ns is not None else None,
                    "accepted": True, "rule": "R2",
                }, {
                    "label": f"naive even split into {count} frames",
                    "times_ns": None, "accepted": False, "rule": "R2",
                    "reason": "lacing metadata inconsistent; guessing forbidden",
                }],
            })
            continue
        times = _candidate_times(base_ns, count, period, b)
        selected_idx = 0 if times else None
        for i, (off, size) in enumerate(b["frames_raw"]):
            out.append({
                "identity": f"{b['identity']}:f{i}",
                "block": b["identity"], "track_number": b["track_number"],
                "index": i, "anchored_ns": base_ns,
                "offset_in_block": off, "size": size,
                "byte_range": [b["payload_range"][0] + off,
                               b["payload_range"][0] + off + size],
                "status": "unique" if len(times) == 1 else "ambiguous",
                "selected": times[selected_idx][i] if times else None,
                "candidates": [
                    {"label": lbl, "times_ns": t,
                     "accepted": idx == selected_idx,
                     "rule": "R5" if len(times) > 1 else None}
                    for idx, (t, lbl) in enumerate(times)
                ],
                "discard_padding_ns": (
                    b["discard_padding"]["value"]
                    if i == count - 1 and b.get("discard_padding") else None),
            })
    return out


def _candidate_times(base_ns, count, period_ns, block) -> list:
    """Return legal per-frame time candidate lists ``[(times, label), ...]``.

    Only the first frame is anchored by the block timecode.  Subsequent frame
    times need extra evidence; every legal interpretation is returned rather
    than collapsing to one answer (R5).
    """
    if base_ns is None or count <= 1:
        return [([base_ns], "anchored")] if count >= 1 else []
    cands = []
    if period_ns:
        cands.append(([base_ns + i * period_ns for i in range(count)],
                      f"even DefaultDuration {int(period_ns)} ns"))
    if block.get("block_duration_ns"):
        span = block["block_duration_ns"]
        cands.append(([base_ns + i * span / count for i in range(count)],
                      "even across BlockDuration"))
    if not cands:
        # Nothing gives later-frame times: anchored first frame, unknown rest.
        cands.append(([base_ns] + [None] * (count - 1),
                      "first anchored; rest undetermined"))
    return cands


def _cues(doc: dict, timebase: dict, clusters: dict, blocks: list) -> list:
    scale = timebase.get("timecode_scale_ns")
    seg_data = doc.get("segment_data_start")
    blocks_by_cluster: dict[str, list] = {}
    for b in blocks:
        blocks_by_cluster.setdefault(b["cluster"], []).append(b)
    out = []
    for cue in doc.get("cues", []):
        cue_ns = cue["cue_time_raw"] * scale if scale else None
        target_abs_off = (seg_data + cue["cluster_pos"]) if seg_data is not None else None
        target_cluster = None
        mismatch = None
        for c in clusters["order"]:
            if c["element_offset"] == target_abs_off:
                target_cluster = c["identity"]
                break
        if target_cluster is None and clusters["order"]:
            nearest = min(
                clusters["order"],
                key=lambda c: abs(c["element_offset"] - (target_abs_off or -1)))
            mismatch = {
                "code": "cue-mispointing",
                "claimed_cluster_offset": target_abs_off,
                "nearest_cluster_offset": nearest["element_offset"],
                "nearest_cluster": nearest["identity"],
                "delta_bytes": abs(nearest["element_offset"]
                                   - (target_abs_off or -1)),
                "rule": "R3",
            }
        block_match = None
        rel = cue.get("rel_pos")
        if rel is not None and seg_data is not None:
            want_off = seg_data + cue["cluster_pos"] + rel
            block_match = any(
                b["raw_range"][0] == want_off
                for b in blocks_by_cluster.get(target_cluster, []))
        out.append({
            "identity": cue["identity"], "track_number": cue["track_number"],
            "cue_time_raw": cue["cue_time_raw"],
            "cue_time": Q(cue_ns, "ns",
                          "CueTime*TimecodeScale (index evidence only)"
                          ).as_dict() if cue_ns is not None else None,
            "target_cluster_identity": target_cluster,
            "target_cluster_offset": target_abs_off,
            "relative_position_raw": rel,
            "block_match": block_match,
            "mismatch": mismatch,
            "raw_range": cue["raw_range"],
        })
    return out


def _diagnostics(doc: dict, vals: dict) -> list:
    out: list = []
    if vals["timebase"].get("diagnostic"):
        out.append(vals["timebase"]["diagnostic"])
    if doc.get("segment_unknown_length"):
        out.append({
            "code": "unknown-length-segment", "rule": "R6",
            "identity": "segment",
            "message": "Segment has unknown length; end inferred at EOF",
            "byte_range": doc.get("segment_range"),
        })
    for u in doc.get("unknown_length_elements", []):
        out.append({
            "code": "unknown-length-element", "rule": "R6",
            "identity": u["identity"], "id_name": u["id_name"],
            "offset": u["offset"],
            "message": f"{u['id_name']} at {u['offset']} uses unknown length",
        })
    for c in vals["clusters"]["order"]:
        if c["unknown_length"]:
            out.append({
                "code": "unknown-length-cluster", "rule": "R6",
                "identity": c["identity"], "offset": c["element_offset"],
                "message": "cluster with unknown length consumes to EOF/parent",
            })
    for b in vals["blocks"]:
        if b["rel_tc_raw_hex"] and b["rel_tc"]["value"] < 0:
            out.append({
                "code": "relative-timecode-overflow", "rule": "R1",
                "identity": b["identity"],
                "raw_hex": b["rel_tc_raw_hex"],
                "signed_value": b["rel_tc"]["value"],
                "unsigned_alternative": b["unsigned_alt"]["rel_tc_unsigned16"],
                "unsigned_absolute_ns": b["unsigned_alt"]["absolute_ns"],
                "accepted": "signed", "rejected": "unsigned",
                "message": "relative timecode top bit set; interpreted signed",
                "byte_range": b["raw_range"],
            })
        if b["negative"]:
            out.append({
                "code": "negative-absolute-time", "rule": "R1",
                "identity": b["identity"], "absolute_ns": b["absolute_ns"],
                "message": "signed relative timecode maps block before cluster",
                "byte_range": b["raw_range"],
            })
        if b["block_errors"]:
            out.append({
                "code": "lacing-corrupt", "rule": "R2",
                "identity": b["identity"], "lacing": b["lacing"],
                "errors": b["block_errors"],
                "message": "lacing metadata inconsistent; frame boundaries withheld",
                "byte_range": b["raw_range"],
            })
        if b.get("discard_padding"):
            frame = next((f for f in vals["frames"]
                          if f["block"] == b["identity"]), None)
            end_ns = b["absolute_ns"]
            if b.get("block_duration_ns") is not None:
                end_ns = b["absolute_ns"] + b["block_duration_ns"]
            dp = b["discard_padding"]["value"]
            kept = end_ns - (b["absolute_ns"] + (end_ns - b["absolute_ns"] - dp))
            if dp > (end_ns - b["absolute_ns"]) and b.get("block_duration_ns"):
                out.append({
                    "code": "discard-padding-overflow",
                    "identity": b["identity"], "discard_padding_ns": dp,
                    "block_duration_ns": b["block_duration_ns"],
                    "message": "DiscardPadding exceeds block duration",
                    "byte_range": b["raw_range"],
                })
            out.append({
                "code": "discard-padding", "identity": b["identity"],
                "discard_padding_ns": dp,
                "presented_end_ns": b["absolute_ns"],
                "message": "DiscardPadding trims encoder delay at block tail",
                "byte_range": b["raw_range"],
            })
    for cue in vals["cues"]:
        if cue["mismatch"]:
            m = dict(cue["mismatch"])
            m.update({"identity": cue["identity"],
                      "message": "CueClusterPosition does not point at a "
                                 "Cluster; cue retained as evidence only",
                      "byte_range": cue["raw_range"]})
            out.append(m)
        elif cue["block_match"] is False:
            out.append({
                "code": "cue-relative-miss", "rule": "R3",
                "identity": cue["identity"],
                "message": "CueRelativePosition does not match a block",
                "byte_range": cue["raw_range"],
            })
    for g in vals["gaps"]:
        out.append(g)
    for d in doc.get("parse_diagnostics", []):
        out.append({"code": "ebml-byte-error", "message": d})
    return out


def _timeline(frames: list, tracks: list) -> dict:
    """Per-track timelines ordered by time (R4), never file order."""
    tby_num = {t["number"]: t for t in tracks}
    lanes: dict[int, list] = {}
    for f in frames:
        n = f["track_number"]
        t = f.get("selected")
        if t is None:
            t = f.get("anchored_ns")
        lanes.setdefault(n, []).append({
            "frame_identity": f["identity"], "time_ns": t,
            "status": f["status"],
            "byte_range": f.get("byte_range"),
            "file_index": len(lanes.get(n, [])),
        })
    lanes_out = {}
    for n, items in lanes.items():
        timed = [x for x in items if x["time_ns"] is not None]
        untimed = [x for x in items if x["time_ns"] is None]
        timed.sort(key=lambda x: x["time_ns"])
        arr = np.array([x["time_ns"] for x in timed], dtype=float)
        if len(arr) > 1:
            deltas = np.diff(arr)
            spacing = {"min_ns": float(deltas.min()),
                       "max_ns": float(deltas.max()),
                       "uniform": bool(np.allclose(deltas, deltas[0]))}
        else:
            spacing = None
        lanes_out[str(n)] = {
            "track_identity": tby_num.get(n, {}).get("identity"),
            "frames": timed + untimed, "spacing": spacing,
        }
    # Cross-track observation (evidence, not forced alignment).
    starts = {n: l["frames"][0]["time_ns"] for n, l in lanes_out.items()
              if l["frames"] and l["frames"][0]["time_ns"] is not None}
    cross = None
    if len(starts) > 1:
        cross = {"track_start_ns": starts,
                 "note": "per-track times reported; no file-order alignment (R4)"}
    return {"lanes": lanes_out, "cross_track": cross}


def _gaps(doc: dict, timebase: dict, clusters: dict, blocks: list) -> list:
    scale = timebase.get("timecode_scale_ns")
    out = []
    order = clusters["order"]
    # expected block coverage within a cluster from default durations
    for i, c in enumerate(order[:-1]):
        nxt = order[i + 1]
        if c["start_ns"] is None or nxt["start_ns"] is None:
            continue
        cblocks = [b for b in blocks if b["cluster"] == c["identity"]
                   and b["absolute_ns"] is not None]
        end_ns = c["start_ns"]
        for b in cblocks:
            e = b["absolute_ns"]
            if b.get("block_duration_ns"):
                e += b["block_duration_ns"]
            end_ns = max(end_ns, e)
        if nxt["start_ns"] > end_ns + (scale or 0):
            out.append({
                "code": "cluster-gap", "identity": f"gap:{c['identity']}",
                "from_cluster": c["identity"], "to_cluster": nxt["identity"],
                "gap": Q(nxt["start_ns"] - end_ns, "ns",
                         "next ClusterTimecode - last block end").as_dict(),
                "message": "no block covers the interval between clusters",
                "byte_range": [c["payload_range"][1],
                               nxt["payload_range"][0]],
            })
        elif nxt["start_ns"] < end_ns:
            out.append({
                "code": "cluster-overlap", "identity": f"ovl:{c['identity']}",
                "from_cluster": c["identity"], "to_cluster": nxt["identity"],
                "overlap_ns": end_ns - nxt["start_ns"],
                "message": "next cluster starts before previous content ends",
                "byte_range": [nxt["element_offset"], nxt["element_offset"]],
            })
    return out


def evaluate_doc(doc: dict, previous: dict | None = None,
                 decisions: dict | None = None):
    from .engine import Graph
    g = Graph()
    register(g)
    return g.evaluate(doc, previous or {}, decisions)
