"""Machine-readable event vocabulary.

Every event is one JSON object with a stable ``event_id``, ``etype`` and
structured ``payload``.  Rule judgements are emitted as ``rule_assertion``
events so the audit page can render *why* a value was accepted/rejected and
history can reconstruct the exact rule version.
"""
from __future__ import annotations

import hashlib


def _eid(parts) -> str:
    return "ev-" + hashlib.sha1(
        "|".join(str(p) for p in parts).encode()).hexdigest()[:14]


def import_event(doc: dict) -> dict:
    return {
        "event_id": _eid(["import", doc["file_identity"], doc["sha1"]]),
        "etype": "import",
        "payload": {
            "title": "导入 Matroska/WebM 结构与帧摘要",
            "file_identity": doc["file_identity"],
            "filename": doc["filename"], "sha1": doc["sha1"],
            "byte_length": doc["byte_length"],
            "preserved": [
                "ebml-element-offsets", "unknown-length", "track-parameters",
                "cluster-block-timecodes", "discard-padding", "cues",
                "raw-byte-ranges", "raw-representations"],
            "counts": {
                "elements": len(doc["elements"]),
                "tracks": len(doc["tracks"]),
                "clusters": len(doc["clusters"]),
                "blocks": len(doc["blocks"]), "cues": len(doc["cues"])},
        },
    }


def parameter_event(scale_before, scale_after, rationale: str) -> dict:
    return {
        "event_id": _eid(["param", scale_before, scale_after, rationale[:24]]),
        "etype": "parameter-revision",
        "payload": {
            "title": "TimecodeScale 参数修订",
            "timecode_scale_raw_before": scale_before,
            "timecode_scale_raw_after": scale_after,
            "unit": "ns/tick",
            "rationale": rationale,
            "raw_layer_unchanged": True,
            "note": "原始 TimecodeScale 字节保留；仅派生层切换有效时基",
        },
    }


def source_patch_event(name: str, byte_range, affected, rationale: str) -> dict:
    return {
        "event_id": _eid(["patch", name, byte_range]),
        "etype": "source-mutation",
        "payload": {
            "title": f"局部源数据改动：{name}",
            "direct_source": name, "byte_range": list(byte_range),
            "expected_affected": affected,
            "rationale": rationale,
        },
    }


def rule_assertion(rule_id: str, subject_identity: str, accepted: list,
                   rejected: list, rationale: str, byte_range=None) -> dict:
    return {
        "event_id": _eid(["rule", rule_id, subject_identity, str(accepted)[:40]]),
        "etype": "rule_assertion",
        "payload": {
            "rule": rule_id, "subject_identity": subject_identity,
            "accepted": accepted, "rejected": rejected,
            "rationale": rationale, "byte_range": byte_range,
        },
    }


def decision_event(decision: str, subject_identity: str, chosen, rejected,
                   reason: str) -> dict:
    return {
        "event_id": _eid(["decision", decision, subject_identity, str(chosen)[:40]]),
        "etype": "adjudication",
        "payload": {
            "decision": decision, "subject_identity": subject_identity,
            "chosen": chosen, "rejected_candidates_kept": rejected,
            "reason": reason,
        },
    }


def mutation_event(scenario: dict) -> dict:
    return {
        "event_id": _eid(["counterex", scenario["code"], scenario["byte_range"]]),
        "etype": "counterexample",
        "payload": scenario,
    }


def conflict_event(subject: str, option_a, option_b, resolution: str | None) -> dict:
    return {
        "event_id": _eid(["conflict", subject]),
        "etype": "conflict",
        "payload": {
            "subject_identity": subject, "option_a": option_a,
            "option_b": option_b, "resolution": resolution,
            "note": "两个候选均保留；解决不删除未选分支",
        },
    }
