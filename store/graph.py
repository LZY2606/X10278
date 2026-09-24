"""Dependency-graph propagation for derivations.

A derivation is recomputed only when its dependency fingerprint (the
projected objects it actually reads, hashed) changes or the rule version
moves. Otherwise the previous output is carried over with an explicit
reason, so the system can always explain why a result is still valid.
"""
from __future__ import annotations

from mkv.rules import (RULE_VERSION, dependency_projection, derivation_keys,
                       inputs_fingerprint, run_derivation)


def _projection_map(key, objects):
    return {oid: proj for oid, proj in dependency_projection(key, objects)}


def compute_derivations(objects: list[dict], prev_objects: list[dict],
                        prev_derivations: dict[str, dict]) -> dict[str, dict]:
    """Return {key: derivation_row} for a new version."""
    out: dict[str, dict] = {}
    for key in derivation_keys(objects):
        fingerprint, dep_ids = inputs_fingerprint(key, objects)
        prev = prev_derivations.get(key)
        if (prev and prev["rule_version"] == RULE_VERSION
                and prev["inputs_hash"] == fingerprint):
            out[key] = {
                "key": key, "rule_version": RULE_VERSION,
                "inputs_hash": fingerprint, "input_ids": dep_ids,
                "status": "kept",
                "reason": (f"inputs unchanged since {prev['version_id']} "
                           f"(hash {fingerprint[:12]}…)"),
                "output": prev["output"],
            }
            continue
        if prev:
            old_proj = _projection_map(key, prev_objects)
            new_proj = _projection_map(key, objects)
            changed = sorted(oid for oid in set(old_proj) | set(new_proj)
                             if old_proj.get(oid) != new_proj.get(oid))
            reason = ("recomputed: changed inputs "
                      + (", ".join(changed) if changed else "(set reshaped)"))
        else:
            reason = "computed: no previous derivation"
        out[key] = {
            "key": key, "rule_version": RULE_VERSION,
            "inputs_hash": fingerprint, "input_ids": dep_ids,
            "status": "recomputed", "reason": reason,
            "output": run_derivation(key, objects),
        }
    return out
