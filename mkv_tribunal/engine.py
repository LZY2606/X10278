"""Dependency-graph propagation for derived values.

Each derived value is a node key.  A node declares:
  * ``inputs(doc)``   -> raw input fingerprints it reads
  * ``compute(ctx)``  -> JSON-able value
  * ``deps(doc)``     -> other node keys it depends on

When a new raw document arrives the engine computes fingerprints for every
node; a node is **recomputed only if its own inputs changed or a dependency
was recomputed to a different value**.  A propagation report states for each
node whether it was ``recomputed`` or ``still-valid`` and why.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class Node:
    key: str
    inputs: Callable[[dict], Any]
    compute: Callable[[dict, dict], Any]
    deps: Callable[[dict], list[str]]


class Graph:
    def __init__(self) -> None:
        self.nodes: dict[str, Node] = {}

    def register(self, key: str, inputs, compute, deps=None) -> None:
        self.nodes[key] = Node(key, inputs, compute, deps or (lambda doc: []))

    def evaluate(self, doc: dict, previous: dict,
                 decisions: dict | None = None) -> tuple[dict, list[dict]]:
        """Return (values_by_key, propagation report)."""
        decisions = decisions or {}
        ctx = {"doc": doc, "values": {}, "decisions": decisions}
        report: list[dict] = []
        memo_fingerprint: dict[str, str] = {}

        def fp(key: str) -> str:
            if key in memo_fingerprint:
                return memo_fingerprint[key]
            node = self.nodes[key]
            parts = [json.dumps(node.inputs(doc), sort_keys=True,
                                default=str)]
            for dep in node.deps(doc):
                parts.append(f"{dep}={fp(dep)}")
            h = hashlib.sha1("|".join(parts).encode()).hexdigest()
            memo_fingerprint[key] = h
            return h

        state: dict[str, str] = {}

        def run(key: str, stack: tuple[str, ...]) -> None:
            if key in state:
                return
            if key in stack:
                raise RuntimeError(f"dependency cycle at {key}")
            node = self.nodes[key]
            for dep in node.deps(doc):
                run(dep, stack + (key,))
            fingerprint = fp(key)
            prev = previous.get(key)
            input_changed = prev is None or prev.get("fingerprint") != fingerprint
            value = node.compute(doc, ctx["values"])
            ctx["values"][key] = value
            if prev is not None and not input_changed:
                status = "still-valid"
                reason = "raw inputs and dependency fingerprints unchanged"
                value = prev["value"]
                ctx["values"][key] = value
            elif prev is not None:
                dep_changes = [
                    d for d in node.deps(doc)
                    if previous.get(d, {}).get("value") != ctx["values"].get(d)
                ]
                own = [d for d in node.deps(doc) if d not in dep_changes]
                if dep_changes:
                    reason = f"dependency changed: {', '.join(dep_changes)}"
                    if own or json.dumps(node.inputs(doc), sort_keys=True,
                                         default=str) != json.dumps(
                            previous.get(key, {}).get("inputs"), sort_keys=True,
                            default=str):
                        reason += "; own raw inputs also changed"
                else:
                    reason = "own raw inputs changed"
                status = "recomputed"
            else:
                status = "new"
                reason = "no previous value"
            state[key] = fingerprint
            report.append({
                "key": key, "status": status, "reason": reason,
                "fingerprint": fingerprint,
            })

        for key in sorted(self.nodes):
            run(key, ())
        values = {
            k: {"value": v, "fingerprint": state[k]}
            for k, v in ctx["values"].items()
        }
        return values, report
