"""End-to-end review workflow over the versioned store.

One narrative, not disconnected buttons: import -> verify -> trigger boundary
mutants -> inspect adopted/rejected candidates -> decide -> replay.
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict
from typing import Optional

from . import ebml
from .model import extract, MatroskaDoc
from .derive import compute, ALL_NODES, NODE_DEPS, affected_nodes_for_source_change
from .events import make_event
from .storage import Store, ConflictError

RULE_REFS = [
    "rule:relative-timecode-signed-16bit@1",
    "rule:lacing-corrupt-no-guess@1",
    "rule:cue-is-index-evidence@1",
    "rule:multi-track-independent@1",
]


def parse_doc(data: bytes, source_uid: str) -> MatroskaDoc:
    roots = ebml.parse_document(data)
    return extract(data, roots, source_uid)


def source_uid_for(data: bytes) -> str:
    return f"src:{hashlib.sha256(data).hexdigest()[:16]}"


class Workflow:
    def __init__(self, store: Store):
        self.store = store

    def import_bytes(self, data: bytes, label: str = "initial import",
                     branch: str = "main", kind: str = "import",
                     changed: str | None = None,
                     source_uid: str | None = None,
                     params: dict | None = None,
                     base_version: int | None = None,
                     actor: str = "reviewer") -> dict:
        uid = source_uid or source_uid_for(data)
        uid, rev = self.store.import_source(data, label, kind=kind,
                                            changed=changed, source_uid=uid)
        doc = parse_doc(data, uid)
        bundle = compute(doc, params)
        ev = make_event(
            "source.import" if kind == "import" else "source.patch",
            {"label": label, "kind": kind,
             "changed_direct_source": changed,
             "recomputed_nodes": sorted(affected_nodes_for_source_change()),
             "params": bundle.params},
            actor=actor, rule_refs=RULE_REFS,
            adopted=[{"what": "signed-int16 relative timecode",
                      "rule": RULE_REFS[0]},
                     {"what": "no frame guess on corrupt lace",
                      "rule": RULE_REFS[1]},
                     {"what": "cue as index evidence only",
                      "rule": RULE_REFS[2]},
                     {"what": "independent per-track timebases",
                      "rule": RULE_REFS[3]}],
            rejected=[{"what": "zero-extended uint16 timecode",
                       "reason": "Matroska relative time is signed"}],
            affected_uids=[t.uid for t in doc.tracks] +
                          [c.uid for c in doc.clusters])
        head = self.store.head(branch)
        if head is None:
            bv = None
        else:
            bv = base_version if base_version is not None \
                else head["current_version"]
        saved = self.store.save_version(
            source_uid=uid, source_revision=rev, branch=branch, event=ev,
            params=bundle.params, doc=doc.to_dict(),
            derived=bundle.to_dict(),
            anomalies=[a.to_dict() for a in doc.anomalies] +
                      bundle.nodes["relative_overflow"] +
                      bundle.nodes["cluster_gaps"] +
                      bundle.nodes["cue_check"],
            base_version=bv if head else None, actor=actor)
        return {"source_uid": uid, "revision": rev, **saved}

    # --------------------------------------------------------- current state
    def current(self, branch: str = "main") -> Optional[dict]:
        head = self.store.head(branch)
        if not head:
            return None
        snap = self.store.get_version(head["current_version"])
        data = self.store.get_source_bytes(head["source_uid"],
                                           head["source_revision"])
        return {"head": head, "snapshot": snap, "data_len": len(data)}

    # --------------------------------------------------- parameter revision
    def revise_params(self, params: dict, reason: str,
                      branch: str = "main", actor: str = "reviewer",
                      base_version: int | None = None) -> dict:
        head = self.store.head(branch)
        if not head:
            raise RuntimeError("no imported source on branch")
        data = self.store.get_source_bytes(head["source_uid"],
                                           head["source_revision"])
        doc = parse_doc(data, head["source_uid"])
        merged = dict(self.store.get_version(
            head["current_version"])["params"])
        merged.update(params)
        bundle = compute(doc, merged,
                         only=set(ALL_NODES) - {"scale"})
        ev = make_event(
            "params.revise",
            {"reason": reason, "changes": params, "merged_params": merged,
             "recomputed_nodes": sorted(set(ALL_NODES) - {"scale"}),
             "explanation": "scale node untouched; time-dependent nodes "
                            "recomputed via dependency graph"},
            actor=actor, rule_refs=RULE_REFS)
        bv = base_version if base_version is not None else \
            head["current_version"]
        saved = self.store.save_version(
            source_uid=head["source_uid"],
            source_revision=head["source_revision"], branch=branch,
            event=ev, params=merged, doc=doc.to_dict(),
            derived=bundle.to_dict(),
            anomalies=[a.to_dict() for a in doc.anomalies] +
                      bundle.nodes["relative_overflow"] +
                      bundle.nodes["cluster_gaps"] +
                      bundle.nodes["cue_check"],
            base_version=bv, actor=actor)
        return saved

    # -------------------------------------------------------- local recompute
    def recompute_node(self, node: str, branch: str = "main") -> dict:
        """Recompute one derived node and explain why the rest stays valid."""
        head = self.store.head(branch)
        snap = self.store.get_version(head["current_version"])
        data = self.store.get_source_bytes(head["source_uid"],
                                           head["source_revision"])
        doc = parse_doc(data, head["source_uid"])
        bundle = compute(doc, snap["params"], only={node})
        explanations = {n: ("recomputed" if n == node else
                            f"kept: {node} only was requested")
                        for n in ALL_NODES}
        return {"node": node, "result": bundle.nodes[node],
                "explanations": explanations}

    # ------------------------------------------------------------- decisions
    def decide(self, scope: str, target_uid: str, choice: str,
               candidates: list[dict], rationale: str,
               branch: str = "main", actor: str = "reviewer",
               base_version: int | None = None,
               create_new_version: bool = True) -> dict:
        """Record an accept/reject. Unchosen candidates are retained, never
        overwritten; the decision is a new event with provenance."""
        head = self.store.head(branch)
        if not head:
            raise RuntimeError("no imported source")
        adopted = [c for c in candidates if c.get("choice") == choice]
        rejected = [c for c in candidates if c.get("choice") != choice]
        key = f"decision:{scope}:{target_uid}"
        payload = {"scope": scope, "target_uid": target_uid,
                   "choice": choice, "rationale": rationale,
                   "candidates": candidates, "actor": actor}
        self.store.put_decision(key, payload)
        ev = make_event(
            "decision.record", payload, actor=actor, rule_refs=RULE_REFS,
            adopted=adopted, rejected=rejected,
            affected_uids=[target_uid],
            parent_event_id=None)
        if create_new_version:
            data = self.store.get_source_bytes(head["source_uid"],
                                               head["source_revision"])
            doc = parse_doc(data, head["source_uid"])
            bundle = compute(doc, self.store.get_version(
                head["current_version"])["params"])
            bv = base_version if base_version is not None else \
                head["current_version"]
            saved = self.store.save_version(
                source_uid=head["source_uid"],
                source_revision=head["source_revision"], branch=branch,
                event=ev, params=bundle.params, doc=doc.to_dict(),
                derived=bundle.to_dict(),
                anomalies=[a.to_dict() for a in doc.anomalies] +
                          bundle.nodes["relative_overflow"] +
                          bundle.nodes["cluster_gaps"] +
                          bundle.nodes["cue_check"],
                base_version=bv, actor=actor)
            return {"decision_key": key, **saved}
        self.store.append_event(ev)
        return {"decision_key": key, "event_id": ev.id}

    def resolve_conflict(self, resolution: str, theirs_event: dict,
                         branch: str = "main", actor: str = "reviewer") -> dict:
        """Two editors on the same base: rebases onto current head and records
        an explicit resolution event. ``resolution`` is 'keep-ours',
        'take-theirs' or 'merge'."""
        head = self.store.head(branch)
        if resolution == "take-theirs":
            return self.revise_params(
                theirs_event.get("payload", {}).get("changes", {}),
                f"conflict resolved: take theirs ({resolution})",
                branch=branch, actor=actor,
                base_version=head["current_version"])
        ev = make_event(
            "conflict.resolve",
            {"resolution": resolution, "theirs": theirs_event},
            actor=actor, rule_refs=RULE_REFS)
        self.store.append_event(ev)
        return {"resolution": resolution, "event_id": ev.id}

    # --------------------------------------------------------------- history
    def at_event(self, event_id: str) -> Optional[dict]:
        return self.store.get_event_snapshot(event_id)
