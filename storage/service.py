"""用例编排服务：导入、派生缓存、事件、分支、参数修订、导出/导入与重放。

版本语义：
- 每次动作产生独立 revision，commit_revision 原子切换 current 指针；
- 人工决定（accept/reject/adjust）是新事件，不覆盖旧事件；
- checkout 任意旧事件 = 按账本重放到该点，恢复当时的依赖版本与结论；
- 派生用依赖图：node_key = hash(规则指纹, 输入哈希)，输入未变且规则未变则
  直接命中，并能说明"为何仍有效/为何需要重算"。
"""
from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path
from typing import Optional

from core import parser as P
from core.facts import extract
from core.domain import derive
from .db import Store, ConflictError, rule_fingerprint, _eid

RULE_VERSION = "rules-v1"
BASELINE_FILE = "baseline.mkv"


def _hash_blob(blob: bytes) -> str:
    return hashlib.sha1(blob).hexdigest()


class CourtService:
    def __init__(self, store: Store):
        self.store = store

    # ------------------------------------------------------------------
    def _parse_and_commit(self, *, file_key: str, fragment_key, blob: bytes,
                          branch: str, kind: str, payload: dict,
                          parent_event=None, actor="reviewer", accepted=True,
                          supersedes=None, expected_parent=None,
                          params=None, label=None) -> dict:
        elements, diags = P.parse_file(blob, file_key)
        return self.store.commit_revision(
            file_key=file_key, fragment_key=fragment_key, blob=blob,
            kind=kind, payload=payload, branch=branch,
            parent_event=parent_event, actor=actor, accepted=accepted,
            supersedes=supersedes, expected_parent=expected_parent,
            rule_version=RULE_VERSION, elements=elements,
            parse_diags=[d.to_dict() for d in diags],
            params=params or {})

    def import_baseline(self, blob: bytes, filename: str = BASELINE_FILE,
                        branch: str = "main") -> dict:
        if self.store.head(branch) is not None:
            raise ValueError(f"分支 {branch} 已有基线；请走派生事件流程")
        file_key = self.store.add_file(blob, filename)
        return self._parse_and_commit(
            file_key=file_key, fragment_key=None, blob=blob, branch=branch,
            kind="import", payload={"filename": filename,
                                    "sha1": _hash_blob(blob),
                                    "byte_length": len(blob)})

    def current_state(self, branch: str = "main") -> dict:
        head = self.store.head(branch)
        if head is None:
            return {"branch": branch, "empty": True}
        return self.state_at_revision(head["revision_id"], branch=branch,
                                      event_id=head["event_id"])

    def state_at_event(self, event_id: str, branch: str = "main") -> dict:
        ev = self.store.event(event_id)
        if ev is None:
            raise KeyError(event_id)
        return self.state_at_revision(ev["revision_id"], branch=branch,
                                      event_id=event_id)

    def state_at_revision(self, revision_id: str, *, branch: str,
                          event_id: Optional[str] = None) -> dict:
        rev = self.store.revision(revision_id)
        if rev is None:
            raise KeyError(revision_id)
        blob = self.store.get_file(rev["file_key"])
        params = json.loads(rev["params_json"])
        elements = self.store.revision_elements(revision_id)
        parse_diags = self.store.revision_parse_diags(revision_id)

        # ---- 依赖图派生（facts -> timeline）----
        inputs_hash = hashlib.sha1(
            rev["file_key"].encode() + b"|" +
            (rev["fragment_key"] or b"-" if isinstance(rev["fragment_key"], bytes)
             else (rev["fragment_key"] or "-").encode()) + b"|" +
            json.dumps(params, sort_keys=True).encode()).hexdigest()

        cached = self.store.derivation_get("timeline", inputs_hash,
                                           rev["rule_version"])
        if cached is not None:
            timeline = json.loads(cached["result_json"])
            node_key = cached["node_key"]
            reused = True
            reuse_reason = ("规则指纹与输入哈希均未变化（"
                            f"node={node_key[:18]}），结论仍有效")
        else:
            els, _ = P.parse_file(blob, rev["file_key"])
            facts = extract(els)
            timeline = derive(facts,
                              scale_override=params.get("scale_override")).to_dict()
            node_key = self.store.derivation_put(
                "timeline", inputs_hash, rev["rule_version"], timeline)
            reused = False
            reuse_reason = "新输入或规则变化，已重算并缓存"

        frag = None
        if rev["fragment_key"]:
            frag = self.store.get_fragment(rev["fragment_key"])
        events = [self._event_dict(e)
                  for e in self.store.events_on(branch)]
        return {
            "branch": branch, "empty": False,
            "head_event_id": self.store.head(branch)["event_id"]
                              if self.store.head(branch) else None,
            "at_event_id": event_id,
            "revision_id": revision_id,
            "file_key": rev["file_key"],
            "fragment": ({"kind": frag["kind"], "label": frag["label"],
                          "params": json.loads(frag["params_json"])}
                         if frag else None),
            "rule_version": rev["rule_version"],
            "rule_fingerprint": rule_fingerprint(),
            "params": params,
            "elements": elements,
            "parse_diags": parse_diags,
            "timeline": timeline,
            "derivation": {"node_key": node_key, "reused": reused,
                           "reason": reuse_reason, "inputs_hash": inputs_hash},
            "events": events,
        }

    def _event_dict(self, row) -> dict:
        return {"event_id": row["event_id"], "revision_id": row["revision_id"],
                "seq": row["seq"], "branch": row["branch"],
                "parent_event": row["parent_event"], "kind": row["kind"],
                "payload": json.loads(row["payload_json"]),
                "actor": row["actor"], "accepted": bool(row["accepted"]),
                "supersedes": row["supersedes"],
                "created_at": row["created_at"]}

    # ------------------------------------------------------------------
    # 动作：变异 / 参数修订 / 人工决定（均产生独立新版本）
    # ------------------------------------------------------------------
    def _head_revision_blob(self, branch):
        head = self.store.head(branch)
        if head is None:
            raise ValueError(f"分支 {branch} 为空，先导入基线")
        rev = self.store.revision(head["revision_id"])
        return rev, self.store.get_file(rev["file_key"])

    def trigger_mutation(self, kind: str, params: Optional[dict] = None,
                         branch: str = "main", actor: str = "reviewer",
                         expected_parent: Optional[str] = None,
                         accepted: bool = True, label: Optional[str] = None):
        from core.mutator import mutate, MUTATION_KINDS
        if kind not in MUTATION_KINDS:
            raise ValueError(kind)
        rev, base_blob = self._head_revision_blob(branch)
        result = mutate(base_blob, kind, params)
        if label:
            result.label = label
        file_key = self.store.add_file(result.blob,
                                       f"{branch}-{kind}.mkv")
        frag_key = self.store.add_fragment(
            rev["file_key"], kind, result.label,
            {**result.params, "changed_ranges": result.changed_ranges,
             "description": result.description}, result.blob)
        return self._parse_and_commit(
            file_key=file_key, fragment_key=frag_key, blob=result.blob,
            branch=branch, kind=f"mutation:{kind}",
            payload={"fragment_key": frag_key, "kind": kind,
                     "changed_ranges": result.changed_ranges,
                     "description": result.description,
                     "params": result.params, "accepted": accepted},
            actor=actor, accepted=accepted, expected_parent=expected_parent)

    def revise_scale(self, new_scale_ns: int, reason: str,
                     branch: str = "main", actor: str = "reviewer",
                     expected_parent: Optional[str] = None):
        """参数修订：不改任何原始字节，只增加 scale_override 参数。

        只影响 timeline 派生节点（facts/elements 完全不变）。"""
        rev, blob = self._head_revision_blob(branch)
        params = {"scale_override": int(new_scale_ns), "reason": reason}
        head = self.store.head(branch)
        return self.store.commit_revision(
            file_key=rev["file_key"], fragment_key=rev["fragment_key"],
            blob=blob, kind="param:scale",
            payload={"new_scale_ns": int(new_scale_ns), "reason": reason},
            branch=branch, actor=actor, expected_parent=expected_parent,
            rule_version=RULE_VERSION, params=params)

    def decide(self, decision: str, subject_event: str,
               note: str = "", branch: str = "main", actor: str = "reviewer",
               expected_parent: Optional[str] = None,
               adjusted_params: Optional[dict] = None):
        """人工裁定 accept / reject / adjust。

        - 不覆盖历史：产生新事件；reject 通过 supersedes 指向被否定事件；
        - 未被采纳的候选以 accepted=0 保留；
        - adjust 携带调整后的参数（如新的 scale）。
        """
        assert decision in ("accept", "reject", "adjust")
        rev, blob = self._head_revision_blob(branch)
        params = dict(json.loads(rev["params_json"]))
        if decision == "adjust" and adjusted_params:
            params.update(adjusted_params)
        accepted = decision != "reject"
        return self.store.commit_revision(
            file_key=rev["file_key"], fragment_key=rev["fragment_key"],
            blob=blob, kind=f"decision:{decision}",
            payload={"subject_event": subject_event, "note": note,
                     "decision": decision,
                     "adjusted_params": adjusted_params or {}},
            branch=branch, actor=actor, accepted=accepted,
            supersedes=subject_event if decision == "reject" else None,
            expected_parent=expected_parent,
            rule_version=RULE_VERSION, params=params)

    # ------------------------------------------------------------------
    def create_branch(self, name: str, from_event: Optional[str] = None):
        """从任意旧事件拉分支（用于比较两个版本后选择继续哪一版）。"""
        if from_event is None:
            head = self.store.head("main")
            if head is None:
                raise ValueError("main 为空")
            from_event = head["event_id"]
        ev = self.store.event(from_event)
        if ev is None:
            raise KeyError(from_event)
        with self.store.transaction() as c:
            exists = c.execute("SELECT 1 FROM branch_heads WHERE branch=?",
                               (name,)).fetchone()
            if exists:
                raise ValueError(f"分支 {name} 已存在")
            c.execute("INSERT INTO branch_heads(branch,event_id,revision_id,"
                      "updated_seq) VALUES(?,?,?,?)",
                      (name, ev["event_id"], ev["revision_id"], ev["seq"]))
            c.execute("INSERT INTO branch_bases(branch,base_event) "
                      "VALUES(?,?)", (name, from_event))
        return {"branch": name, "from_event": from_event}

    def compare(self, event_a: str, event_b: str) -> dict:
        a = self.state_at_event(event_a)
        b = self.state_at_event(event_b)
        ta, tb = a["timeline"], b["timeline"]
        return {
            "a": {"event_id": event_a, "revision_id": a["revision_id"],
                  "fragment": a["fragment"], "params": a["params"],
                  "scale_ns": ta["scale_ns"],
                  "diag_codes": sorted({d["code"] for d
                                        in ta["diagnostics"]}),
                  "frame_count": len(ta["frames"]),
                  "cue_status": {c["cue_key"]: c["status"] for c in ta["cues"]}},
            "b": {"event_id": event_b, "revision_id": b["revision_id"],
                  "fragment": b["fragment"], "params": b["params"],
                  "scale_ns": tb["scale_ns"],
                  "diag_codes": sorted({d["code"] for d
                                        in tb["diagnostics"]}),
                  "frame_count": len(tb["frames"]),
                  "cue_status": {c["cue_key"]: c["status"] for c in tb["cues"]}},
        }

    # ------------------------------------------------------------------
    # 导出 / 导入（删除本地库再导入，身份与顺序必须保持）
    # ------------------------------------------------------------------
    def export_bundle(self) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            files = list(self.store.conn.execute("SELECT * FROM files"))
            frags = list(self.store.conn.execute("SELECT * FROM fragments"))
            revs = list(self.store.conn.execute("SELECT * FROM revisions "
                                                "ORDER BY rowid"))
            events = list(self.store.conn.execute(
                "SELECT * FROM events ORDER BY branch, seq"))
            heads = list(self.store.conn.execute("SELECT * FROM branch_heads"))
            elems = list(self.store.conn.execute("SELECT * FROM elements"))
            pdiags = list(self.store.conn.execute("SELECT * FROM parse_diags"))
            derivs = list(self.store.conn.execute("SELECT * FROM derivations"))
            manifest = {"format": "mtt-bundle", "version": 1,
                        "rule_version": RULE_VERSION,
                        "rule_fingerprint": rule_fingerprint(),
                        "counts": {"files": len(files), "fragments": len(frags),
                                   "revisions": len(revs),
                                   "events": len(events)}}
            z.writestr("manifest.json",
                       json.dumps(manifest, ensure_ascii=False, indent=2))
            z.writestr("events.jsonl",
                       "\n".join(json.dumps(dict(r), ensure_ascii=False,
                                            default=str) for r in events))
            z.writestr("revisions.jsonl",
                       "\n".join(json.dumps(dict(r), ensure_ascii=False,
                                            default=str) for r in revs))
            z.writestr("heads.json",
                       json.dumps([dict(r) for r in heads], ensure_ascii=False))
            z.writestr("files.json",
                       json.dumps([{k: v for k, v in dict(r).items()
                                    if k != "blob"} for r in files],
                                  ensure_ascii=False))
            z.writestr("fragments.json",
                       json.dumps([{k: v for k, v in dict(r).items()
                                    if k != "blob"} for r in frags],
                                  ensure_ascii=False))
            z.writestr("elements.jsonl",
                       "\n".join(json.dumps(dict(r), ensure_ascii=False,
                                            default=str) for r in elems))
            z.writestr("parse_diags.jsonl",
                       "\n".join(json.dumps(dict(r), ensure_ascii=False,
                                            default=str) for r in pdiags))
            z.writestr("derivations.jsonl",
                       "\n".join(json.dumps(dict(r), ensure_ascii=False,
                                            default=str) for r in derivs))
            for r in files:
                z.writestr(f"blobs/files/{r['file_key']}.bin", r["blob"])
            for r in frags:
                z.writestr(f"blobs/fragments/{r['fragment_key']}.bin",
                           r["blob"])
        return buf.getvalue()

    def import_bundle(self, data: bytes) -> dict:
        """导入到当前（应为空）库。所有身份按包内稳定键原样恢复。"""
        z = zipfile.ZipFile(io.BytesIO(data))
        manifest = json.loads(z.read("manifest.json"))
        with self.store.transaction() as c:
            for r in json.loads(z.read("files.json")):
                blob = z.read(f"blobs/files/{r['file_key']}.bin")
                c.execute("INSERT OR REPLACE INTO files VALUES(?,?,?,?,?)",
                          (r["file_key"], r["filename"], blob,
                           r["byte_length"], r["imported_at"]))
            for r in json.loads(z.read("fragments.json")):
                blob = z.read(f"blobs/fragments/{r['fragment_key']}.bin")
                c.execute("INSERT OR REPLACE INTO fragments VALUES(?,?,?,?,?,"
                          "?,?,?)",
                          (r["fragment_key"], r["file_key"], r["kind"],
                           r["label"], r["params_json"], blob,
                           r["byte_length"], r["created_event"]))
            for line in z.read("revisions.jsonl").decode().splitlines():
                r = json.loads(line)
                c.execute("INSERT OR REPLACE INTO revisions VALUES(?,?,?,?,?,"
                          "?,?)",
                          (r["revision_id"], r["branch"], r["file_key"],
                           r["fragment_key"], r["rule_version"],
                           r["params_json"], r["created_at"]))
            for line in z.read("elements.jsonl").decode().splitlines():
                r = json.loads(line)
                c.execute(
                    "INSERT OR REPLACE INTO elements(revision_id,el_key,"
                    "parent_key,id,name,etype,unit,id_start,data_start,raw_end,"
                    "declared_size,unknown_size,inferred_end,depth,value,"
                    "value_raw_hex,children_json) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (r["revision_id"], r["el_key"], r["parent_key"], r["id"],
                     r["name"], r["etype"], r["unit"], r["id_start"],
                     r["data_start"], r["raw_end"], r["declared_size"],
                     r["unknown_size"], r["inferred_end"], r["depth"],
                     r["value"], r["value_raw_hex"], r["children_json"]))
            for line in z.read("parse_diags.jsonl").decode().splitlines():
                r = json.loads(line)
                c.execute("INSERT OR REPLACE INTO parse_diags VALUES(?,?,?,?,"
                          "?,?)",
                          (r["revision_id"], r["code"], r["offset_"],
                           r["span_end"], r["severity"], r["message"]))
            for line in z.read("derivations.jsonl").decode().splitlines():
                r = json.loads(line)
                c.execute("INSERT OR REPLACE INTO derivations VALUES(?,?,?,?,"
                          "?,?,?)",
                          (r["node_key"], r["rule_version"], r["inputs_hash"],
                           r["kind"], r["result_json"], r["status"],
                           r["created_at"]))
            for line in z.read("events.jsonl").decode().splitlines():
                r = json.loads(line)
                c.execute("INSERT OR REPLACE INTO events VALUES(?,?,?,?,?,?,"
                          "?,?,?,?,?)",
                          tuple(r[k] for k in
                                ["event_id", "revision_id", "seq", "branch",
                                 "parent_event", "kind", "payload_json",
                                 "actor", "accepted", "supersedes",
                                 "created_at"]))
            c.execute("DELETE FROM branch_heads")
            for r in json.loads(z.read("heads.json")):
                c.execute("INSERT INTO branch_heads VALUES(?,?,?,?)",
                          (r["branch"], r["event_id"], r["revision_id"],
                           r["updated_seq"]))
        return manifest
