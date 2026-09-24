"""Offline case pack: "Matroska 时码庭" (Matroska timecode court).

Builds a deterministic synthetic Matroska file that exercises every edge the
review workflow must survive:
- unknown-size cluster (extends to segment end)
- relative timecode near the signed 16-bit limits (overflow candidates)
- corrupt Xiph lacing and corrupt fixed lacing (no frame guessing)
- BlockGroup DiscardPadding (raw value + ns unit preserved)
- a mis-pointing cue (index evidence, not time truth)
- divergent track timebases (no file-order alignment)
- a mid-file gap of unparseable bytes where a cluster is missing

Also drives the scripted narrative: initial import, one parameter revision,
one local source amendment, and one conflicting pair of human decisions.
"""
from __future__ import annotations

import os
import struct

from . import derive, rules

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "..", "fixtures",
                            "matroska_timecode_court.mkv")


# ---- EBML encoding helpers ----

def _eid(value: int) -> bytes:
    return value.to_bytes((value.bit_length() + 7) // 8, "big")


def _size(value: int) -> bytes:
    for length in (1, 2, 3, 4, 5, 6, 7, 8):
        if value < (1 << (7 * length)) - 1:
            return ((1 << (7 * length)) | value).to_bytes(length, "big")
    raise ValueError(value)


def E(eid: int, data: bytes) -> bytes:
    return _eid(eid) + _size(len(data)) + data


def U(eid: int, data: bytes) -> bytes:
    """Element with unknown (reserved all-ones) size."""
    return _eid(eid) + b"\xff" + data


def uint(eid: int, value: int) -> bytes:
    return E(eid, value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big"))


def sint(eid: int, value: int) -> bytes:
    length = 1
    while not (-(1 << (8 * length - 1)) <= value < (1 << (8 * length - 1))):
        length += 1
    return E(eid, value.to_bytes(length, "big", signed=True))


def f32(eid: int, value: float) -> bytes:
    return E(eid, struct.pack(">f", value))


def s(eid: int, text: str) -> bytes:
    return E(eid, text.encode())


def simple_block(track: int, rel: int, flags: int, body: bytes) -> bytes:
    return E(0xA3, _size(track) + rel.to_bytes(2, "big", signed=True)
             + bytes([flags]) + body)


def build_fixture() -> bytes:
    """Construct the synthetic Matroska case file (deterministic)."""
    head = E(0x1A45DFA3,
             uint(0x4286, 1) + uint(0x42F7, 1) + uint(0x42F2, 4)
             + uint(0x42F3, 8) + s(0x4282, "matroska")
             + uint(0x4287, 4) + uint(0x4285, 2))
    info = E(0x1549A966, uint(0x2AD7B1, 1_000_000))

    track1 = E(0xAE,
               uint(0xD7, 1) + uint(0x73C5, 11) + uint(0x83, 1)
               + s(0x86, "V_UNCOMPRESSED")
               + E(0xE0, uint(0xB0, 640) + uint(0xBA, 480)))
    track2 = E(0xAE,
               uint(0xD7, 2) + uint(0x73C5, 22) + uint(0x83, 2)
               + s(0x86, "A_PCM/INT/LIT") + f32(0x23314F, 2.0)
               + E(0xE1, f32(0xB5, 48000.0) + uint(0x9F, 2)))
    tracks = E(0x1654AE6B, track1 + track2)

    # Cluster A @ 0 ticks: normal blocks, Xiph lacing, fixed lacing,
    # and a BlockGroup carrying DiscardPadding.
    a_b1 = simple_block(1, 0, 0x80, b"V-frame-0")
    a_b2 = simple_block(1, 40, 0x80 | 0x02,
                        bytes([2, 4, 4]) + b"AAAA" + b"BBBB" + b"CCCC")
    a_b3 = simple_block(2, 0, 0x80 | 0x04,
                        bytes([1]) + b"aaaaaaaa" + b"bbbbbbbb")
    group_block = E(0xA1, _size(1) + (80).to_bytes(2, "big", signed=True)
                    + b"\x00" + b"V-frame-80")
    group = E(0xA0, group_block + uint(0x9B, 40)
              + sint(0x75A2, 7_500_000))
    cluster_a = E(0x1F43B675, uint(0xE7, 0) + a_b1 + a_b2 + a_b3 + group)

    gap = b"\x00" * 24  # mid-file cluster gap: unparseable bytes

    # Cluster B @ 1000 ticks: signed-limit blocks + corrupt lacings.
    b_b1 = simple_block(1, -32768, 0x80, b"PRE")          # signed minimum
    b_b2 = simple_block(1, 32767, 0x80, b"OVER")          # near max
    b_b3 = simple_block(2, 20, 0x80 | 0x02,
                        bytes([2, 255, 3]) + b"zzzzzz")   # corrupt Xiph
    b_b4 = simple_block(1, 120, 0x80 | 0x04,
                        bytes([1]) + b"1234567")           # corrupt fixed
    cluster_b = E(0x1F43B675,
                  uint(0xE7, 1000) + b_b1 + b_b2 + b_b3 + b_b4)

    # Cluster C @ 2000 ticks: UNKNOWN size, must be the last segment child.
    cluster_c = U(0x1F43B675,
                  uint(0xE7, 2000)
                  + simple_block(2, 0, 0x80, b"aud-c0")
                  + simple_block(1, 0, 0x80, b"vid-c0"))

    # Cues precede the clusters; CueClusterPosition is relative to the
    # segment data start, so iterate until sizes stabilize.
    cues = b""
    prefix = info + tracks
    tail = cluster_a + gap + cluster_b + cluster_c
    for _ in range(6):
        seg_header = _eid(0x18538067) + _size(len(prefix + cues + tail))
        base = len(head) + len(seg_header)
        rel_a = len(prefix + cues)
        rel_b = rel_a + len(cluster_a) + len(gap)
        rel_c = rel_b + len(cluster_b)
        mispoint = rel_a + 10  # points INSIDE cluster A -> mis-point
        new_cues = E(0x1C53BB6B,
                     E(0xBB, uint(0xB3, 0)
                       + E(0xB7, uint(0xF7, 1)
                           + uint(0xF1, rel_a)))
                     + E(0xBB, uint(0xB3, 1000)
                       + E(0xB7, uint(0xF7, 1)
                           + uint(0xF1, mispoint)))
                     + E(0xBB, uint(0xB3, 2000)
                       + E(0xB7, uint(0xF7, 1)
                           + uint(0xF1, rel_c))))
        if new_cues == cues:
            break
        cues = new_cues
    return head + _eid(0x18538067) + _size(len(prefix + cues + tail)) \
        + prefix + cues + tail


def ensure_fixture(path: str | None = None) -> str:
    path = path or FIXTURE_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = build_fixture()
    if not os.path.exists(path) or open(path, "rb").read() != data:
        with open(path, "wb") as f:
            f.write(data)
    return path


def fixture_bytes() -> bytes:
    return build_fixture()


def _rule_events() -> list[dict]:
    return [{"kind": "rule.applied",
             "payload": {"rule_id": r["rule_id"], "statement": r["statement"]},
             "actor": "system"} for r in rules.RULE_STATEMENTS]


def import_case(store, actor: str = "media-team") -> dict:
    """Initial import: raw baseline is preserved before any judgment."""
    data = fixture_bytes()
    overrides = {
        "src:raw": derive.make_source(data, "matroska_timecode_court.mkv"),
        "params": dict(rules.DEFAULT_PARAMS),
        "decisions": {"selections": {}},
    }
    events = [{
        "kind": "case.imported",
        "payload": {
            "name": "matroska_timecode_court",
            "statement": "EBML element offsets, unknown sizes, track params, "
                         "cluster/block timecodes, discard padding, cues and "
                         "raw byte ranges are preserved in the baseline",
            "sha256": overrides["src:raw"]["sha256"],
        },
        "actor": actor,
    }] + _rule_events()
    result = derive.derive(store, None, "v1 初始导入: EBML/时码基线",
                           overrides, events, actor=actor)
    vid = result["version"]
    for anomaly in store.version_nodes(vid)["anomalies"]["payload"]["anomalies"]:
        store.append_event(vid, "anomaly.detected", {
            "anomaly_id": anomaly["id"],
            "object": anomaly["object"],
            "kind": anomaly["kind"],
            "detail": anomaly["detail"],
            "policy": anomaly.get("policy"),
            "rule_id": anomaly.get("rule_id"),
        }, actor=actor)
    return result


def revise_params(store, actor: str = "reviewer-A") -> dict:
    """Parameter revision: TimecodeScale 1ms -> 2ms. Only normalization
    nodes depend on it, so parse/raw nodes stay valid."""
    cur = store.current_version()
    old = store.version_nodes(cur)["params"]["payload"]
    new = dict(old)
    new["timecode_scale_ns"] = 2_000_000
    new["revision_reason"] = ("审查裁定: 头部声明的 TimecodeScale 与采集时钟不符, "
                              "按 2ms 基准重新规范化派生层")
    events = [{
        "kind": "param.revise",
        "payload": {
            "param": "timecode_scale_ns",
            "old": {"value": old["timecode_scale_ns"], "unit": "ns/tick"},
            "new": {"value": 2_000_000, "unit": "ns/tick"},
            "statement": "raw TimecodeScale element is untouched; only the "
                         "normalized derived layer changes",
        },
        "actor": actor,
    }]
    return derive.derive(store, cur, "v2 参数修订: TimecodeScale 1ms→2ms",
                         {"params": new}, events, actor=actor,
                         expected_parent=cur)


def amend_source(store, actor: str = "reviewer-A") -> dict:
    """Local source amendment: repair only the corrupt Xiph block bytes
    (same length, so file offsets do not shift)."""
    cur = store.current_version()
    nodes = store.version_nodes(cur)
    target = None
    for anomaly in nodes["anomalies"]["payload"]["anomalies"]:
        if anomaly["kind"] == "lacing_corrupt" and anomaly.get("lacing") == "xiph":
            target = anomaly["object"]
            break
    if target is None:
        raise RuntimeError("no corrupt Xiph block found to amend")
    raw_key = f"{target}:raw"
    old = nodes[raw_key]["payload"]
    body = bytes([2, 2, 2]) + b"zzzzzz"  # 3 frames of 2 bytes; valid Xiph
    new_raw = bytes.fromhex(old["header_hex"]) + body
    payload = rules.parse_block_header(new_raw)
    payload.update({
        "key": target,
        "kind": old["kind"],
        "raw_hex": new_raw.hex(),
        "byte_range": old["byte_range"],
        "discard_padding": old.get("discard_padding"),
        "block_duration": old.get("block_duration"),
        "amended": True,
        "original_hex": old["raw_hex"],
        "amendment_event": "source.amend",
    })
    events = [{
        "kind": "source.amend",
        "payload": {
            "object": target,
            "byte_range": old["byte_range"],
            "original_hex": old["raw_hex"],
            "amended_hex": new_raw.hex(),
            "statement": "only this block's raw node changes; parse:ebml and "
                         "unrelated block nodes stay valid",
        },
        "actor": actor,
    }]
    return derive.derive(store, cur, f"v3 源数据修订: 修复 {target} 的 Xiph lacing",
                         {raw_key: payload}, events, actor=actor,
                         expected_parent=cur)


def conflicting_decisions(store, actor_a: str = "reviewer-A",
                          actor_b: str = "reviewer-B") -> dict:
    """Two reviewers accept different candidates for the mis-pointing cue
    from the same base version -> conflict; resolve in favour of A, keep B."""
    base = store.current_version()
    base_decisions = store.version_nodes(base)["decisions"]["payload"]

    def branch(actor, cluster_id, label):
        decisions = {"selections": dict(base_decisions["selections"])}
        decisions["selections"]["cue:1"] = cluster_id
        events = [{
            "kind": "decision.accept",
            "payload": {
                "object": "cue:1",
                "accepted_candidate": cluster_id,
                "statement": "cue is index evidence, not time truth; the other "
                             "legal candidate is retained unselected",
                "actor": actor,
            },
            "actor": actor,
        }, {
            "kind": "decision.reject",
            "payload": {
                "object": "cue:1",
                "rejected_candidate": (
                    "cluster:1" if cluster_id == "cluster:0" else "cluster:0"),
                "statement": "rejection is recorded but the candidate is never "
                             "deleted, so the decision is reversible",
            },
            "actor": actor,
        }]
        return derive.derive(store, base, label, {"decisions": decisions},
                             events, actor=actor, expected_parent=base)

    ra = branch(actor_a, "cluster:0",
                "v4 决定 A: cue:1 → cluster:0 (前一簇)")
    rb = branch(actor_b, "cluster:1",
                "v5 决定 B: cue:1 → cluster:1 (后一簇)")
    conflict_id = rb.get("conflict_id")
    resolved = None
    if conflict_id is not None:
        resolved = store.resolve_conflict(conflict_id, "a", actor=actor_a)
    return {"base": base, "branch_a": ra, "branch_b": rb,
            "conflict_id": conflict_id, "resolved": resolved}


def run_demo(store) -> dict:
    """Execute the full scripted path (skips steps already present)."""
    log = []
    labels = {v["label"] for v in store.list_versions()}
    if not any(lbl.startswith("v1 ") for lbl in labels):
        log.append({"step": "import", "result": import_case(store)})
    if not any(lbl.startswith("v2 ") for lbl in labels):
        log.append({"step": "param_revision", "result": revise_params(store)})
    if not any(lbl.startswith("v3 ") for lbl in labels):
        log.append({"step": "source_amendment", "result": amend_source(store)})
    open_conflicts = [c for c in store.list_conflicts()
                      if c["status"] == "open"]
    resolved_conflicts = [c for c in store.list_conflicts()
                          if c["status"] == "resolved"]
    if not open_conflicts and not resolved_conflicts:
        log.append({"step": "conflicting_decisions",
                    "result": conflicting_decisions(store)})
    return {"steps": log,
            "statement": "replay passes through unknown size, relative "
                         "timecode overflow, corrupt lacing, discard padding, "
                         "cue mis-point, track timebase divergence and a "
                         "mid-file cluster gap"}
