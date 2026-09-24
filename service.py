"""Application service: review workflow, branching decisions and provenance."""
from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass

from domain import ids
from domain.extract import extract_bytes
from domain.derive import build_graph, affected_targets
from storage.db import Storage, ConflictError

RULE_VERSION = "rules-1"


class Service:
    def __init__(self, db_path: str = ":memory:"):
        if db_path == ":memory:":
            self.storage = Storage.__new__(Storage)
            self.storage.path = db_path
            self.storage.conn = _mem_connect()
            self.storage.fault = None
            from storage.db import init_db
            init_db(self.storage.conn)
        else:
            self.storage = Storage(db_path)
        self._bootstrap_rules()

    def _bootstrap_rules(self):
        with self.storage.conn:
            self.storage.conn.execute(
                "INSERT OR IGNORE INTO rules(rule_version,rules_json,created_at)"
                " VALUES(?,?,?)",
                (RULE_VERSION, json.dumps({
                    "signed_relative_timecode": True,
                    "no_guess_lacing_boundaries": True,
                    "cue_is_index_evidence": True,
                    "no_cross_track_file_order_alignment": True,
                    "keep_all_legal_candidates": True,
                }), 0))

    # ---- state ----------------------------------------------------------
    def state(self) -> dict:
        cur = self.storage.current_version()
        if cur is None:
            return {"current_version": None, "versions": [], "events": []}
        return {
            "current_version": cur,
            "version": self.storage.get_version(cur),
            "document": self.storage.get_document(cur),
            "nodes": self.storage.get_nodes(cur),
            "affected": self.storage.get_affected(cur),
            "versions": self.storage.list_versions(),
            "events": self.storage.list_events(),
        }

    def state_at(self, version_id: str) -> dict:
        return {
            "current_version": self.storage.current_version(),
            "version": self.storage.get_version(version_id),
            "document": self.storage.get_document(version_id),
            "nodes": self.storage.get_nodes(version_id),
            "affected": self.storage.get_affected(version_id),
            "events": self.storage.list_events(),
            "versions": self.storage.list_versions(),
        }

    def _next_seq(self) -> int:
        rows = self.storage.list_versions()
        return (max((r["seq"] for r in rows), default=0)) + 1

    def _params_at(self, version_id: str | None) -> dict:
        if not version_id:
            return {}
        doc = self.storage.get_document(version_id)
        return {"timecode_scale": doc["timecode_scale"]} if doc else {}

    # ---- import ---------------------------------------------------------
    def import_bytes(self, buf: bytes, filename: str = "input.mkv",
                     actor: str = "importer",
                     file_id: str | None = None,
                     base_version: str | None = None,
                     forced_file_id: str | None = None) -> str:
        fid = forced_file_id or file_id or ids.digest("file", buf[:64],
                                                      filename, len(buf))
        doc = extract_bytes(fid, buf)
        doc_dict = doc.to_dict()
        version = ids.version_id(base_version, "import", self._next_seq())
        nodes = build_graph(version, doc_dict, RULE_VERSION).serialize()
        event_id = ids.digest("evt", version, "import")
        self.storage.publish(
            version_id=version, parent_version_id=base_version,
            base_version_id=base_version, seq=self._peek_seq(version),
            label="initial import", event_id=event_id, kind="import",
            actor=actor, payload={"filename": filename, "file_id": fid,
                                  "byte_length": len(buf)},
            rule_version=RULE_VERSION, doc=doc_dict, nodes=nodes,
            affected={"new": [n["id"] for n in nodes], "recomputed": [],
                      "still_valid": []}, blob=buf)
        return version

    def _peek_seq(self, version_id) -> int:
        return self._next_seq()

    # ---- decisions / parameter revision --------------------------------
    def decide(self, node_id_or_target: str, action: str, actor: str = "reviewer",
               choice_index: int | None = None, note: str = "",
               base_version: str | None = None, base_assumed: str | None = None,
               branch_label: str | None = None) -> str:
        """Record accept / reject / select-candidate as a NEW immutable version.

        Unselected candidates are retained; history is never overwritten.
        """
        base = base_version or self.storage.current_version()
        if base_assumed is not None and base != base_assumed:
            raise ConflictError(f"optimistic conflict: base moved {base_assumed} -> {base}")
        # A branch based on a version that is no longer current must conflict.
        current = self.storage.current_version()
        if base != current:
            raise ConflictError(
                f"non-current base {base[:12]}; current is {current[:12]}")
        doc = self.storage.get_document(base)
        old_nodes = self.storage.get_nodes(base)
        nodes = copy.deepcopy(old_nodes)
        # Allow cross-version addressing by current-version node id as well.
        cur_nodes = self.storage.get_nodes(self.storage.current_version())
        alias = None
        if not any(n["id"] == node_id_or_target or
                   n["target"] == node_id_or_target for n in nodes):
            alias = next((n for n in cur_nodes
                          if n["id"] == node_id_or_target), None)
        lookup = node_id_or_target
        if alias is not None and any(
                n["kind"] == alias["kind"] and n["target"] == alias["target"]
                for n in nodes):
            lookup = alias["target"]
        target_node = next((n for n in nodes
                            if n["id"] == lookup or n["target"] == lookup), None)
        if target_node is None:
            raise KeyError(node_id_or_target)
        if action == "accept":
            target_node["status"] = "valid"
            if choice_index is not None and target_node["candidates"]:
                target_node["accepted_index"] = choice_index
        elif action == "reject":
            target_node["status"] = "rejected"
        elif action == "select":
            target_node["accepted_index"] = choice_index
            target_node["status"] = "valid"
        else:
            raise ValueError(action)
        target_node["reason"] = (target_node["reason"] +
                                 f" | decision:{action} by {actor}: {note}").strip(" |")

        seq = self._next_seq()
        version = ids.version_id(base, f"decision-{action}", seq)
        event_id = ids.digest("evt", version, action)
        payload = {"node": target_node["id"], "action": action,
                   "choice_index": choice_index, "note": note,
                   "actor": actor}
        affected = affected_targets(old_nodes, nodes)
        self.storage.publish(
            version_id=version, parent_version_id=base,
            base_version_id=base, seq=seq,
            label=branch_label or f"{action} {target_node['kind']}",
            event_id=event_id, kind=f"decision.{action}", actor=actor,
            payload=payload, rule_version=RULE_VERSION, doc=doc, nodes=nodes,
            affected=affected)
        return version

    def revise_parameter(self, key: str, value, actor: str = "reviewer",
                         note: str = "", base_version: str | None = None,
                         base_assumed: str | None = None) -> str:
        """A parameter revision triggers dependency-graph propagation."""
        base = base_version or self.storage.current_version()
        if base_assumed is not None and base != base_assumed:
            raise ConflictError("optimistic conflict on parameter revision")
        if base != self.storage.current_version():
            raise ConflictError("parameter revision based on stale version")
        doc = self.storage.get_document(base)
        old_nodes = self.storage.get_nodes(base)
        new_doc = copy.deepcopy(doc)
        if key == "timecode_scale":
            new_doc["timecode_scale"] = int(value)
            new_doc["timecode_scale_present"] = True
        else:
            new_doc.setdefault("params", {})[key] = value
        seq = self._next_seq()
        version = ids.version_id(base, f"param-{key}", seq)
        nodes = build_graph(version, new_doc, RULE_VERSION,
                            {"timecode_scale": new_doc["timecode_scale"]}
                            ).serialize()
        affected = affected_targets(old_nodes, nodes)
        event_id = ids.digest("evt", version, key)
        self.storage.publish(
            version_id=version, parent_version_id=base, base_version_id=base,
            seq=seq, label=f"param {key}={value}", event_id=event_id,
            kind="param.revision", actor=actor,
            payload={"key": key, "value": value, "note": note},
            rule_version=RULE_VERSION, doc=new_doc, nodes=nodes,
            affected=affected)
        return version

    # ---- source mutation (local-dependency change) ---------------------
    def reimport_mutated(self, buf: bytes, mutation_kind: str,
                         actor: str = "experiment",
                         base_version: str | None = None,
                         filename: str = "mutated.mkv") -> str:
        base = base_version or self.storage.current_version()
        fid = ids.digest("file", buf[:64], filename, len(buf), mutation_kind)
        doc = extract_bytes(fid, buf).to_dict()
        seq = self._next_seq()
        version = ids.version_id(base, f"mutation-{mutation_kind}", seq)
        old_doc = self.storage.get_document(base)
        old_nodes = self.storage.get_nodes(base)
        nodes = build_graph(version, doc, RULE_VERSION).serialize()
        # affected relative to prior source: annotate propagation
        affected = _cross_document_affected(old_doc, doc, old_nodes, nodes)
        event_id = ids.digest("evt", version, mutation_kind)
        self.storage.publish(
            version_id=version, parent_version_id=base, base_version_id=base,
            seq=seq, label=f"source mutation: {mutation_kind}",
            event_id=event_id, kind=f"source.mutation.{mutation_kind}",
            actor=actor, payload={"mutation": mutation_kind,
                                  "filename": filename},
            rule_version=RULE_VERSION, doc=doc, nodes=nodes, affected=affected,
            blob=buf)
        return version

    def checkout(self, version_id: str) -> None:
        self.storage.checkout(version_id)

    # ---- export / import bundle ----------------------------------------
    def export_bundle(self) -> dict:
        state = self.state()
        events = self.storage.list_events()
        versions = self.storage.list_versions()
        docs = {}
        nodes = {}
        affected = {}
        blobs = {}
        for v in versions:
            vid = v["version_id"]
            d = self.storage.get_document(vid)
            if d:
                docs[vid] = d
                row = self.storage.conn.execute(
                    "SELECT blob FROM documents WHERE version_id=?",
                    (vid,)).fetchone()
                if row and row["blob"] is not None:
                    blobs[vid] = bytes(row["blob"]).hex()
            nodes[vid] = self.storage.get_nodes(vid)
            affected[vid] = self.storage.get_affected(vid)
        return {
            "format": "matroska-timecode-court-bundle/1",
            "rule_version": RULE_VERSION,
            "current_version": state["current_version"],
            "versions": versions, "events": events,
            "documents": docs, "nodes": nodes, "affected": affected,
            "blobs": blobs,
            "identity_order": [v["version_id"] for v in versions],
        }

    def import_bundle(self, bundle: dict, replace: bool = True) -> str:
        if replace:
            self._wipe()
        s = self.storage
        # deterministic insert order by seq preserves id/order/provenance
        for rv, rj in (("rules", None),):
            pass
        inserted = 0
        for v in sorted(bundle["versions"], key=lambda x: x["seq"]):
            vid = v["version_id"]
            with s.conn:
                s.conn.execute(
                    "INSERT OR IGNORE INTO versions(version_id,"
                    "parent_version_id,base_version_id,seq,label,created_at,"
                    "current) VALUES(?,?,?,?,?,?,?)",
                    (vid, v["parent_version_id"], v["base_version_id"],
                     v["seq"], v["label"], v["created_at"], 0))
            doc = bundle["documents"].get(vid)
            blob = bytes.fromhex(bundle["blobs"][vid]) if vid in bundle.get(
                "blobs", {}) else None
            if doc is not None:
                ev = next((e for e in bundle["events"]
                           if e["version_id"] == vid), {})
                with s.conn:
                    s.conn.execute(
                        "INSERT OR REPLACE INTO documents(doc_id,version_id,"
                        "filename,byte_length,doc_json,blob) VALUES(?,?,?,?,?,?)",
                        (doc["file_id"], vid, ev.get("payload") and
                         json.loads(ev["payload"]).get("filename", "input.mkv")
                         or "input.mkv", doc["byte_length"],
                         json.dumps(doc, default=str), blob))
            for n in bundle["nodes"].get(vid, []):
                with s.conn:
                    s.conn.execute(
                        "INSERT OR REPLACE INTO derivations(node_id,version_id,"
                        "kind,target,node_json) VALUES(?,?,?,?,?)",
                        (n["id"], vid, n["kind"], n["target"],
                         json.dumps(n, default=str)))
            with s.conn:
                s.conn.execute(
                    "INSERT OR REPLACE INTO affected(version_id,report_json)"
                    " VALUES(?,?)", (vid, json.dumps(
                        bundle["affected"].get(vid, {}), default=str)))
            inserted += 1
        for ev in bundle["events"]:
            with s.conn:
                s.conn.execute(
                    "INSERT OR REPLACE INTO events(event_id,seq,version_id,"
                    "base_version_id,kind,actor,payload,rule_version,"
                    "created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (ev["event_id"], ev["seq"], ev["version_id"],
                     ev["base_version_id"], ev["kind"], ev["actor"],
                     ev["payload"], ev["rule_version"], ev["created_at"]))
        self.checkout(bundle["current_version"])
        return bundle["current_version"]

    def _wipe(self):
        with self.storage.conn:
            for t in ("events", "versions", "documents", "derivations",
                      "affected"):
                self.storage.conn.execute(f"DELETE FROM {t}")


_MEM_SEQ = 0


def _mem_connect():
    import sqlite3
    global _MEM_SEQ
    _MEM_SEQ += 1
    conn = sqlite3.connect(
        f"file:mtcc{_MEM_SEQ}?mode=memory&cache=shared", uri=True,
        timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def _cross_document_affected(old_doc, new_doc, old_nodes, new_nodes):
    """Explain locality: only nodes whose coordinate source changed recompute."""
    old_clusters = {c["start"]: c for c in old_doc.get("clusters", [])}
    changed_coords = set()
    new_starts = set()
    for c in new_doc.get("clusters", []):
        new_starts.add(c["start"])
        oc = old_clusters.get(c["start"])
        if oc is None or oc["timecode_raw"] != c["timecode_raw"] or \
                oc["end"] != c["end"]:
            changed_coords.add(c["id"])
    return {
        "changed_source_clusters": list(changed_coords),
        "note": "only derived nodes depending on changed coordinates recompute; "
                "other track timelines remain valid",
        **affected_targets(old_nodes, new_nodes),
    }
