"""Derived layer: dependency-graph propagation over a parsed document.

Rules enforced here (each emits a machine-readable event):

* ``relative_timecode_signed`` -- block relative timecodes are read as
  signed 16-bit big-endian; overflow/negative results keep *all* legal
  candidate interpretations instead of collapsing to one answer.
* ``lacing_corrupt_no_frame_guess`` -- corrupt lacing means the frame
  boundaries are unknown; the payload stays opaque.
* ``cue_is_index_evidence`` -- cues are index evidence, never time
  truth; a mispointing cue is an anomaly, not an override.
* ``multitrack_no_file_order_alignment`` -- tracks are aligned by
  absolute time only, never by file order.

Every node records an input hash.  A node whose inputs are unchanged
since the parent version is reused verbatim and says why; a changed
node says which inputs forced the recompute.
"""
from __future__ import annotations

import hashlib
import json

import numpy as np

INT16_MIN, INT16_MAX = -32768, 32767


def _hash(obj) -> str:
    blob = json.dumps(obj, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def _tc_to_ns(tc: int, scale: int) -> int:
    return int(tc) * int(scale)


class Derivation:
    def __init__(self, doc: dict, decisions: dict | None = None,
                 prev_nodes: dict | None = None):
        self.doc = doc
        self.decisions = decisions or {}
        self.prev = prev_nodes or {}
        self.nodes: dict[str, dict] = {}
        self.events: list[dict] = []
        self.scale = doc["timecode_scale"]["value"]

    # ---------------------------------------------------------- plumbing
    def node(self, key, depends, input_obj, compute):
        ih = _hash(input_obj)
        prev = self.prev.get(key)
        if prev and prev.get("input_hash") == ih:
            self.nodes[key] = {
                "key": key, "depends": depends, "input_hash": ih,
                "status": "reused",
                "reason": f"inputs unchanged (hash {ih}); result still valid",
                "result": prev["result"],
                "events": prev.get("events", []),
            }
            self.events.extend(prev.get("events", []))
            return prev["result"]
        result, events = compute()
        if prev:
            reason = f"inputs changed ({prev.get('input_hash')} -> {ih}); recomputed"
        else:
            reason = "no prior version; computed"
        self.nodes[key] = {"key": key, "depends": depends, "input_hash": ih,
                           "status": "computed", "reason": reason,
                           "result": result, "events": events}
        self.events.extend(events)
        return result

    def event(self, rule, kind, subject, detail, extra=None):
        ev = {"rule": rule, "kind": kind, "subject": subject,
              "detail": detail}
        if extra:
            ev.update(extra)
        return ev

    # ------------------------------------------------------------- graph
    def run(self):
        doc = self.doc
        self.node("structure", [], {"elements": doc["elements"],
                                    "diagnostics": doc["diagnostics"]},
                  self._structure)
        tracks = self.node("tracks", ["structure"],
                           {"tracks": doc["tracks"], "scale": self.scale},
                           self._tracks)
        block_results = {}
        for cluster in doc["clusters"]:
            uid = cluster["element"]
            block_results[uid] = self.node(
                f"blocks:{uid}", ["structure"], {"cluster": cluster},
                lambda c=cluster: self._blocks(c))
        time_results = {}
        for cluster in doc["clusters"]:
            uid = cluster["element"]
            time_results[uid] = self.node(
                f"block_times:{uid}", [f"blocks:{uid}", "tracks"],
                {"blocks": block_results[uid], "scale": self.scale,
                 "cluster_tc": (cluster["timecode"] or {}).get("value"),
                 "decisions": self.decisions},
                lambda c=cluster, b=block_results[uid]: self._block_times(c, b))
        self.node("timelines", ["tracks"] + [f"block_times:{u}" for u in time_results],
                  {"times": time_results, "tracks": doc["tracks"]},
                  lambda: self._timelines(time_results, tracks))
        self.node("cues", ["timelines"],
                  {"cues": doc["cues"], "clusters": doc["clusters"],
                   "times": time_results},
                  lambda: self._cues(time_results))
        self.node("anomalies", ["timelines", "cues"],
                  {"events": self.events, "decisions": self.decisions,
                   "clusters": doc["clusters"], "times": time_results},
                  lambda: self._anomalies(time_results))
        return self

    # ---------------------------------------------------------- compute
    def _structure(self):
        doc = self.doc
        unknown = [e for e in doc["elements"] if e["unknown_size"]]
        events = []
        for e in unknown:
            events.append(self.event(
                "unknown_length_inferred_extent", "unknown_length", e["uid"],
                f"{e['name']} uses unknown length; extent inferred from "
                f"parent, raw byte range retained",
                {"byte_range": [e["offset"]["value"], None]}))
        result = {
            "element_count": {"value": len(doc["elements"]), "unit": "count"},
            "unknown_length_elements": [e["uid"] for e in unknown],
            "diagnostics": doc["diagnostics"],
        }
        return result, events

    def _tracks(self):
        events = []
        tracks = []
        for t in self.doc["tracks"]:
            entry = {
                "number": t.get("number", {}).get("value"),
                "uid": t.get("uid", {}).get("value"),
                "name": t.get("name", {}).get("value"),
                "codec": t.get("codec", {}).get("value"),
                "element": t["element"],
                "default_duration": t.get("default_duration"),
            }
            tracks.append(entry)
        durations = {t["number"]: (t["default_duration"] or {}).get("value")
                     for t in tracks}
        distinct = {d for d in durations.values() if d}
        if len(tracks) > 1:
            events.append(self.event(
                "multitrack_no_file_order_alignment", "multitrack_timebases",
                [t["element"] for t in tracks],
                "tracks keep their own timebases; alignment is by absolute "
                "time only, never by file order",
                {"default_durations_ns": durations,
                 "timebases_differ": len(distinct) > 1}))
        return {"tracks": tracks}, events

    def _blocks(self, cluster):
        events = []
        blocks = []
        for b in cluster["blocks"]:
            entry = {
                "element": b["element"],
                "kind": b["kind"],
                "track": (b.get("track_number") or {}).get("value"),
                "rel_timecode": b.get("rel_timecode"),
                "frames": b.get("frames", []),
                "corrupted": b.get("corrupted", False),
                "byte_range": b["byte_range"],
                "discard_padding": (b.get("group") or {}).get("discard_padding"),
                "block_duration": (b.get("group") or {}).get("block_duration"),
                "diagnostics": b.get("diagnostics", []),
            }
            blocks.append(entry)
            for diag in entry["diagnostics"]:
                events.append(self.event(
                    diag.get("rule", "lacing_corrupt_no_frame_guess"),
                    diag["kind"], b["element"], diag["detail"],
                    {"byte_range": [b["byte_range"]["start"]["value"],
                                    b["byte_range"]["end"]["value"]]}))
            if entry["discard_padding"] is not None:
                dp = entry["discard_padding"]
                events.append(self.event(
                    "discard_padding_kept", "discard_padding", b["element"],
                    f"DiscardPadding {dp['value']} ns retained with raw "
                    f"representation; applied only in derived layer",
                    {"value": dp["value"], "unit": "ns", "raw": dp["raw"]}))
        result = {"cluster": cluster["element"],
                  "cluster_timecode": cluster["timecode"],
                  "unknown_size": cluster["unknown_size"],
                  "position": cluster["position"],
                  "blocks": blocks}
        return result, events

    def _block_times(self, cluster, blocks_result):
        events = []
        scale = self.scale
        cluster_tc = (cluster["timecode"] or {}).get("value")
        timed = []
        for b in blocks_result["blocks"]:
            rel = (b.get("rel_timecode") or {}).get("value")
            if cluster_tc is None or rel is None:
                timed.append({**b, "candidates": [], "chosen": None,
                              "anomaly": "missing_timecode"})
                continue
            abs_signed = cluster_tc + rel
            candidates = [{
                "id": "signed",
                "rel_timecode": {"value": rel, "unit": "timecode"},
                "abs_timecode": {"value": abs_signed, "unit": "timecode"},
                "abs_ns": {"value": _tc_to_ns(abs_signed, scale), "unit": "ns"},
                "basis": "cluster_timecode + int16 signed relative",
            }]
            anomaly = None
            if rel < 0 or abs_signed < 0 or rel >= INT16_MAX - 64:
                wrapped_rel = rel + 65536 if rel < 0 else rel - 65536
                candidates.append({
                    "id": "wrapped",
                    "rel_timecode": {"value": wrapped_rel, "unit": "timecode"},
                    "abs_timecode": {"value": cluster_tc + wrapped_rel,
                                     "unit": "timecode"},
                    "abs_ns": {"value": _tc_to_ns(cluster_tc + wrapped_rel, scale),
                               "unit": "ns"},
                    "basis": "int16 modular wrap of relative timecode",
                })
                candidates.append({
                    "id": "unsigned_raw",
                    "rel_timecode": {"value": rel & 0xFFFF, "unit": "timecode"},
                    "abs_timecode": {"value": cluster_tc + (rel & 0xFFFF),
                                     "unit": "timecode"},
                    "abs_ns": {"value": _tc_to_ns(cluster_tc + (rel & 0xFFFF), scale),
                               "unit": "ns"},
                    "basis": "uint16 reading, kept as evidence",
                    "rejected_by": "relative_timecode_signed",
                })
                anomaly = "rel_timecode_overflow"
                events.append(self.event(
                    "relative_timecode_signed", "rel_timecode_overflow",
                    b["element"],
                    f"relative timecode {rel} read as signed int16; "
                    f"{len(candidates)} legal candidates retained, no "
                    f"single answer forced",
                    {"candidates": [c["id"] for c in candidates],
                     "raw": (b.get("rel_timecode") or {}).get("raw")}))
            else:
                events.append(self.event(
                    "relative_timecode_signed", "rel_timecode_ok",
                    b["element"],
                    f"relative timecode {rel} read as signed int16",
                    {"raw": (b.get("rel_timecode") or {}).get("raw")}))
            anomaly_id = f"rel_timecode_overflow:{b['element']}"
            chosen = self.decisions.get(anomaly_id)
            timed.append({**b, "candidates": candidates,
                          "anomaly": anomaly, "anomaly_id": anomaly_id,
                          "chosen": chosen or "signed"})
        return {"cluster": cluster["element"], "blocks": timed}, events

    def _timelines(self, time_results, tracks_result):
        events = []
        per_track: dict[int, list] = {}
        for cluster_uid, res in time_results.items():
            for b in res["blocks"]:
                track = b.get("track")
                if track is None:
                    continue
                chosen = next((c for c in b["candidates"]
                               if c["id"] == b.get("chosen")), None)
                if chosen is None:
                    continue
                per_track.setdefault(track, []).append({
                    "element": b["element"],
                    "cluster": cluster_uid,
                    "abs_timecode": chosen["abs_timecode"],
                    "abs_ns": chosen["abs_ns"],
                    "corrupted": b["corrupted"],
                    "anomaly": b.get("anomaly"),
                    "frames": b["frames"],
                    "discard_padding": b.get("discard_padding"),
                })
        timelines = {}
        for track, frames in per_track.items():
            ordered = sorted(frames, key=lambda f: f["abs_ns"]["value"])
            ns = np.array([f["abs_ns"]["value"] for f in ordered],
                          dtype=np.int64)
            gaps = np.diff(ns) if len(ns) > 1 else np.array([], dtype=np.int64)
            timelines[str(track)] = {
                "frames": ordered,
                "frame_count": {"value": len(ordered), "unit": "frames"},
                "max_gap_ns": {"value": int(gaps.max()) if len(gaps) else 0,
                               "unit": "ns"},
                "sorted_by": "abs_ns (never file order)",
            }
        events.append(self.event(
            "multitrack_no_file_order_alignment", "timelines_built",
            list(time_results.keys()),
            "per-track timelines sorted by absolute time; file order not "
            "used for cross-track alignment",
            {"tracks": sorted(per_track.keys())}))
        return {"timelines": timelines}, events
