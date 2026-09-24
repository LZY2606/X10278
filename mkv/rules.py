"""Domain rules for Matroska timecode review.

Pinned semantics (also emitted as machine-readable `rule.applied` events):
- rule:relative-timecode-signed  Block relative timecode is a SIGNED 16-bit
  big-endian integer. Near-overflow values keep a wrapped candidate instead
  of being clamped to a single answer.
- rule:lacing-no-guess           When lacing size data is corrupt, frame
  boundaries are NOT guessed; the payload stays raw and frame count unknown.
- rule:cue-evidence              Cues are index evidence, not time truth.
  A mis-pointing cue keeps every legal cluster candidate.
- rule:no-file-order-alignment   Tracks are aligned by timecode in their own
  timebase, never by file order.

Every domain quantity carries a unit or coordinate basis; raw representations
are preserved and only the derived layer normalizes.
"""
from __future__ import annotations

import numpy as np

from .ebml import ParseError, read_signed_vint, read_vint

LACING_NAMES = {0: "none", 1: "xiph", 2: "fixed", 3: "ebml"}

DEFAULT_PARAMS = {
    "timecode_scale_ns": 1_000_000,
    "overflow_threshold": 30_000,
    "source": "Segment/Info/TimecodeScale",
}

RULE_STATEMENTS = [
    {
        "rule_id": "rule:relative-timecode-signed",
        "statement": "Block relative timecode is interpreted as a signed 16-bit "
                     "big-endian integer; near-overflow values keep a wrapped "
                     "interpretation candidate instead of a single forced answer.",
    },
    {
        "rule_id": "rule:lacing-no-guess",
        "statement": "When lacing size data is corrupt, frame boundaries are not "
                     "guessed; the block payload is preserved raw and the frame "
                     "count is marked unknown.",
    },
    {
        "rule_id": "rule:cue-evidence",
        "statement": "Cues are index evidence, not time truth; a mis-pointing cue "
                     "keeps all legal cluster candidates and never overrides "
                     "block-derived timecodes.",
    },
    {
        "rule_id": "rule:no-file-order-alignment",
        "statement": "Tracks are aligned by timecode in their own timebase, "
                     "never by file order.",
    },
]


def parse_block_header(raw: bytes) -> dict:
    """Parse a Block/SimpleBlock header. Raw bytes are preserved verbatim."""
    track, tlen = read_vint(raw, 0, strip_marker=True)
    rel = int.from_bytes(raw[tlen:tlen + 2], "big", signed=True)
    flags = raw[tlen + 2]
    lacing = (flags & 0x06) >> 1
    return {
        "track": track,
        "relative_timecode": {
            "value": rel,
            "unit": "ticks",
            "interpretation": "signed_int16_be",
        },
        "flags": {
            "keyframe": bool(flags & 0x80),
            "invisible": bool(flags & 0x08),
            "discardable": bool(flags & 0x01),
            "lacing": LACING_NAMES[lacing],
        },
        "lacing": LACING_NAMES[lacing],
        "header_hex": raw[:tlen + 3].hex(),
        "body_hex": raw[tlen + 3:].hex(),
    }


def decode_frames(lacing: str, body: bytes) -> dict:
    """Decode laced frame boundaries. Corrupt lacing -> no guessing."""
    frames: list[dict] = []
    anomalies: list[dict] = []
    if lacing == "none":
        frames = [{"index": 0, "offset": 0, "size": len(body)}]
    elif lacing == "fixed":
        if len(body) < 1:
            anomalies.append(_lacing_anomaly("fixed", "empty laced body"))
        else:
            count = body[0] + 1
            rest = len(body) - 1
            if count < 1 or rest % count != 0:
                anomalies.append(_lacing_anomaly(
                    "fixed",
                    f"{rest} payload bytes not divisible into {count} frames"))
            else:
                size = rest // count
                frames = [{"index": i, "offset": 1 + i * size, "size": size}
                          for i in range(count)]
    elif lacing == "xiph":
        ok = True
        detail = ""
        if len(body) < 1:
            ok, detail = False, "empty laced body"
        else:
            count = body[0] + 1
            sizes: list[int] = []
            pos = 1
            for _ in range(count - 1):
                s = 0
                while True:
                    if pos >= len(body):
                        ok, detail = False, "xiph size bytes overrun body"
                        break
                    b = body[pos]
                    pos += 1
                    s += b
                    if b < 255:
                        break
                if not ok:
                    break
                sizes.append(s)
            if ok:
                remaining = len(body) - pos
                if sum(sizes) > remaining:
                    ok = False
                    detail = (f"xiph sizes {sizes} sum to {sum(sizes)} but only "
                              f"{remaining} payload bytes remain")
                else:
                    sizes.append(remaining - sum(sizes))
                    off = pos
                    for i, s in enumerate(sizes):
                        frames.append({"index": i, "offset": off, "size": s})
                        off += s
        if not ok:
            frames = []
            anomalies.append(_lacing_anomaly("xiph", detail))
    elif lacing == "ebml":
        try:
            if len(body) < 1:
                raise ParseError("empty laced body")
            count = body[0] + 1
            first, ln = read_vint(body, 1, strip_marker=True)
            sizes = [first]
            pos = 1 + ln
            prev = first
            for _ in range(count - 2):
                diff, ln = read_signed_vint(body, pos)
                prev = prev + diff
                if prev < 0:
                    raise ParseError("negative frame size")
                sizes.append(prev)
                pos += ln
            remaining = len(body) - pos
            if sum(sizes) > remaining:
                raise ParseError("ebml lacing sizes exceed body")
            sizes.append(remaining - sum(sizes))
            off = pos
            for i, s in enumerate(sizes):
                frames.append({"index": i, "offset": off, "size": s})
                off += s
        except ParseError as exc:
            frames = []
            anomalies.append(_lacing_anomaly("ebml", str(exc)))
    return {
        "frames": frames,
        "frame_count_known": not anomalies,
        "anomalies": anomalies,
        "policy": "no_frame_boundary_guess",
    }


def _lacing_anomaly(lacing: str, detail: str) -> dict:
    return {
        "kind": "lacing_corrupt",
        "lacing": lacing,
        "detail": detail,
        "policy": "no_frame_boundary_guess",
        "rule_id": "rule:lacing-no-guess",
    }


def block_timecode(cluster_timecode: int, relative: int, params: dict) -> dict:
    """Absolute timecode with signed interpretation + overflow candidates."""
    absolute = cluster_timecode + relative
    candidates = [{
        "label": "signed_int16",
        "relative": relative,
        "absolute_ticks": absolute,
        "selected": True,
    }]
    anomalies: list[dict] = []
    threshold = params.get("overflow_threshold", 30_000)
    if relative >= threshold or relative <= -threshold or absolute < 0:
        wrapped = relative + 65536 if relative < 0 else relative - 65536
        candidates.append({
            "label": "wrapped_uint16",
            "relative": wrapped,
            "absolute_ticks": cluster_timecode + wrapped,
            "selected": False,
        })
        anomalies.append({
            "kind": "relative_timecode_overflow",
            "detail": (f"relative timecode {relative} ticks is near the signed "
                       f"16-bit limit or yields negative absolute timecode "
                       f"{absolute}"),
            "policy": "interpret_signed_keep_candidates",
            "rule_id": "rule:relative-timecode-signed",
        })
    return {
        "cluster_timecode": {"value": cluster_timecode, "unit": "ticks"},
        "absolute": {"value": absolute, "unit": "ticks"},
        "candidates": candidates,
        "anomalies": anomalies,
    }


def build_timeline(track: dict, block_entries: list[dict], params: dict) -> dict:
    """Per-track timeline sorted by timecode (never by file order)."""
    scale_ns = params["timecode_scale_ns"]
    track_scale = track["timecode_scale"]
    frames = []
    for entry in block_entries:
        tc = entry["timecode"]["absolute"]["value"]
        decoded = entry["frames"]
        if decoded["frame_count_known"]:
            n = max(1, len(decoded["frames"]))
        else:
            n = 1  # corrupt lacing: one unknown-extent marker, no guessed frames
        for i in range(n):
            frames.append({
                "ticks": tc,
                "frame_index": i,
                "cluster": entry["cluster"],
                "block": entry["block"],
                "boundaries_known": decoded["frame_count_known"],
            })
    frames.sort(key=lambda f: (f["ticks"], f["cluster"], f["block"], f["frame_index"]))
    ticks = np.array([f["ticks"] for f in frames], dtype=np.int64)
    ns = np.rint(ticks.astype(np.float64) * scale_ns * track_scale).astype(np.int64)
    basis = f"TimecodeScale={scale_ns}ns x TrackTimecodeScale={track_scale}"
    out_frames = []
    for f, t, v in zip(frames, ticks.tolist(), ns.tolist()):
        out_frames.append({
            **f,
            "timecode": {"value": t, "unit": "ticks"},
            "normalized": {"value": v, "unit": "ns", "basis": basis},
        })
    gaps = []
    if len(ticks) > 1:
        diffs = np.diff(ticks)
        positive = diffs[diffs > 0]
        step = int(np.median(positive)) if len(positive) else 0
        if step > 0:
            for i, d in enumerate(diffs.tolist()):
                if d > 3 * step:
                    gaps.append({
                        "start": {"value": int(ticks[i]), "unit": "ticks"},
                        "end": {"value": int(ticks[i + 1]), "unit": "ticks"},
                        "reason": "cluster_gap",
                    })
    return {
        "track": track["number"],
        "track_timecode_scale": track["timecode_scale"],
        "frames": out_frames,
        "gaps": gaps,
        "sorted_by": "timecode",
        "policy": "no_file_order_alignment",
    }


def verify_cues(cues: list[dict], clusters: list[dict], decisions: dict) -> dict:
    """Verify cue cluster positions. Cues are evidence, never time truth."""
    selections = decisions.get("selections", {})
    out = []
    for i, cue in enumerate(cues):
        cue_id = f"cue:{i}"
        target = cue["cluster_position_abs"]
        exact = [c for c in clusters if c["offset"] == target]
        anomalies = []
        if exact:
            candidates = [{"cluster": exact[0]["id"], "offset": exact[0]["offset"],
                           "selected": True}]
            status = "ok"
        else:
            before = [c for c in clusters if c["offset"] < target]
            after = [c for c in clusters if c["offset"] > target]
            candidates = []
            if before:
                candidates.append({"cluster": before[-1]["id"],
                                   "offset": before[-1]["offset"],
                                   "relation": "preceding", "selected": False})
            if after:
                candidates.append({"cluster": after[0]["id"],
                                   "offset": after[0]["offset"],
                                   "relation": "following", "selected": False})
            chosen = selections.get(cue_id)
            if chosen and any(c["cluster"] == chosen for c in candidates):
                for c in candidates:
                    c["selected"] = c["cluster"] == chosen
            elif candidates:
                candidates[0]["selected"] = True  # default: nearest preceding
            status = "mispoint"
            anomalies.append({
                "kind": "cue_mispoint",
                "detail": (f"cue time {cue['time']['value']} ticks points to byte "
                           f"{target} which is not a cluster start"),
                "policy": "cue_is_index_evidence_not_time_truth",
                "rule_id": "rule:cue-evidence",
            })
        out.append({
            "id": cue_id,
            "time": cue["time"],
            "track": cue["track"],
            "cluster_position": cue["cluster_position"],
            "target_abs": target,
            "status": status,
            "candidates": candidates,
            "anomalies": anomalies,
        })
    return {"cues": out, "policy": "cue_is_index_evidence_not_time_truth"}
