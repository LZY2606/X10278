"""Dependency-graph propagation: recompute only affected nodes.

A node is valid in a version iff its rule version is current AND every input
oid maps to the same content fingerprint it was derived from. When a source
fact changes, we walk the reverse edges; unaffected nodes are explained as
still-valid rather than recomputed.
"""
from __future__ import annotations

import hashlib
from typing import Any

from . import RULESET_VERSION
from .engine import derive_all
from .models import DerivedNode


def fingerprint(obj: Any) -> str:
    payload = repr(sorted(_flatten(obj))).encode("utf-8")
    return hashlib.blake2b(payload, digest_size=8).hexdigest()


def _flatten(obj):
    if isinstance(obj, dict):
        for k in sorted(obj):
            yield ("k", k)
            yield from _flatten(obj[k])
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            yield ("i", i)
            yield from _flatten(v)
    else:
        yield ("v", obj)


def build_input_fingerprints(facts) -> dict[str, str]:
    """Map every source fact oid/element oid to a content fingerprint."""
    fps: dict[str, str] = {}
    bank = facts.to_dict()
    for collection in ("elements", "unknown_lengths", "tracks", "clusters",
                       "blocks", "frames", "cues", "diagnostics"):
        for item in bank[collection]:
            if isinstance(item, dict) and "oid" in item:
                fps[item["oid"]] = fingerprint(item)
            if isinstance(item, dict) and "element_oid" in item:
                fps[item["element_oid"]] = fingerprint(item)
    if facts.timescale and facts.timescale.get("element_oid"):
        fps[facts.timescale["element_oid"]] = fingerprint(facts.timescale)
    return fps


def derive_versioned(facts, params=None, track_overrides=None):
    """Return (nodes, fingerprints) attaching input fingerprints to nodes."""
    nodes = derive_all(facts, params, track_overrides)
    source_fps = build_input_fingerprints(facts)
    for node in nodes.values():
        node.values = dict(node.values)
        node.values["_input_fp"] = {
            i: source_fps.get(i, fingerprint(i)) for i in node.inputs}
        node.values["_rule_version"] = node.rule_version
    return nodes, source_fps


def reverse_graph(nodes) -> dict[str, set[str]]:
    rev: dict[str, set[str]] = {}
    for nid, node in nodes.items():
        for inp in node.inputs:
            rev.setdefault(inp, set()).add(nid)
    return rev


def affected_set(changed_inputs: set[str], nodes) -> set[str]:
    rev = reverse_graph(nodes)
    seen: set[str] = set()
    stack = list(changed_inputs)
    while stack:
        cur = stack.pop()
        for downstream in rev.get(cur, ()):
            if downstream not in seen:
                seen.add(downstream)
                stack.append(downstream)
    return seen


def explain_recompute(old_nodes, new_nodes, changed_inputs: set[str]):
    """Produce per-node validity explanations across two derivations."""
    affected = affected_set(changed_inputs, old_nodes)
    report = {"recomputed": [], "still_valid": [], "new": [], "removed": []}
    for nid, node in new_nodes.items():
        if nid not in old_nodes:
            report["new"].append(nid)
        elif nid in affected:
            report["recomputed"].append(nid)
        else:
            report["still_valid"].append(nid)
    for nid in old_nodes:
        if nid not in new_nodes:
            report["removed"].append(nid)
    return report


def still_valid_reasons(node: DerivedNode, current_fps,
                        current_rule=RULESET_VERSION) -> list[str]:
    reasons = []
    if node.rule_version != current_rule:
        reasons.append(f"rule version changed {node.rule_version} -> "
                       f"{current_rule}")
    stored = node.values.get("_input_fp", {})
    for inp, fp in stored.items():
        if current_fps.get(inp) != fp:
            reasons.append(f"input {inp} changed")
    return reasons
