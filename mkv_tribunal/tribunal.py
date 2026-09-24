"""High-level tribunal workflow: import, derive, adjudicate, archive."""
from __future__ import annotations

import copy
import hashlib
import json

from . import rules, events as ev
from .model import import_bytes, RawDocument
from .storage import Store, Conflict

RULES_VERSION = "rules-v2-signed-tc-cue-evidence"


class Tribunal:
    def __init__(self, store: Store):
        self.store = store

    # ----- core pipeline -------------------------------------------------
    def import_file(self, project_id: str, name: str, data: bytes,
                    filename: str = "input.mkv", branch: str = "main",
                    author: str = "reviewer-A", label: str = "v0 初始导入",
                    parent_id: str | None = None) -> dict:
        self.store.create_project(project_id, name)
        doc = json.loads(import_bytes(data, filename).to_json())
        return self._commit(project_id, branch, doc,
                            ev.import_event(doc), "import", label,
                            author, parent_id)

    def revise_parameter(self, project_id: str, new_scale: int, reason: str,
                         branch="main", author="reviewer-A") -> dict:
        v = self.store.get_version(self.store.head_id(project_id, branch))
        doc = copy.deepcopy(v["doc"])
        before = doc["timecode_scale_raw"]
        doc["timecode_scale_raw"] = new_scale
        event = ev.parameter_event(before, new_scale, reason)
        return self._commit(project_id, branch, doc, event,
                            "parameter-revision", "v1 TimecodeScale 修订",
                            author, v["version_id"],
                            decisions=v.get("derived", {}).get("decisions"))

    def patch_source(self, project_id: str, data: bytes, name: str,
                     byte_range, affected, reason: str, branch="main",
                     author="reviewer-A", label="局部源数据改动",
                     kind="source-mutation") -> dict:
        v = self.store.get_version(self.store.head_id(project_id, branch))
        doc = json.loads(import_bytes(data, v["doc"]["filename"]).to_json())
        event = ev.source_patch_event(name, byte_range, affected, reason)
        return self._commit(project_id, branch, doc, event, kind, label,
                            author, v["version_id"],
                            decisions=v.get("derived", {}).get("decisions"))

    def adjudicate(self, project_id: str, subject_identity: str, decision: str,
                   chosen, rejected, reason: str, branch="main",
                   author="reviewer-A") -> dict:
        v = self.store.get_version(self.store.head_id(project_id, branch))
        doc = copy.deepcopy(v["doc"])
        decisions = copy.deepcopy(v["derived"].get("decisions", {}))
        decisions[subject_identity] = {"decision": decision, "chosen": chosen}
        event = ev.decision_event(decision, subject_identity, chosen, rejected,
                                  reason)
        return self._commit(project_id, branch, doc, event, "adjudication",
                            f"裁定 {subject_identity}", author,
                            v["version_id"], decisions=decisions)

    # ----- derivation + commit ------------------------------------------
    def _commit(self, project_id, branch, doc, event, kind, label, author,
                parent_id, decisions=None) -> dict:
        parent_id = parent_id if parent_id is not None else self.store.head_id(
            project_id, branch)
        prev_derived = {}
        if parent_id:
            prev = self.store.get_version(parent_id)
            prev_derived = {k: v for k, v in prev["derived"].items()
                            if isinstance(v, dict) and "fingerprint" in v}
        derived, report = rules.evaluate_doc(doc, prev_derived, decisions or {})
        derived["decisions"] = decisions or {}
        version_id = self._vid(branch, event, doc)
        payload = {
            "version_id": version_id, "parent_id": parent_id, "branch": branch,
            "kind": kind, "label": label, "author": author, "doc": doc,
            "derived": derived, "report": report,
            "rules_version": RULES_VERSION, "event": event,
        }
        info = self.store.commit_version(project_id, payload,
                                         expected_parent=parent_id)
        info["event"] = event
        info["report"] = report
        return info

    @staticmethod
    def _vid(branch, event, doc) -> str:
        h = hashlib.sha1(json.dumps(
            [branch, event["event_id"], doc["sha1"]], sort_keys=True
        ).encode()).hexdigest()[:14]
        return f"ver-{h}"

    # ----- snapshot views ------------------------------------------------
    def snapshot(self, project_id: str, version_id: str | None = None,
                 branch: str = "main") -> dict:
        vid = version_id or self.store.head_id(project_id, branch)
        v = self.store.get_version(vid)
        d = v["derived"]
        return {
            "project_id": project_id, "version_id": vid,
            "parent_id": v["parent_id"], "branch": v["branch"], "seq": v["seq"],
            "label": v["label"], "kind": v["kind"], "rules_version": v["rules_version"],
            "created_at": v["created_at"],
            "doc": v["doc"],
            "timebase": d["timebase"]["value"],
            "tracks": d["tracks"]["value"],
            "clusters": d["clusters"]["value"]["order"],
            "blocks": d["blocks"]["value"],
            "frames": d["frames"]["value"],
            "cues": d["cues"]["value"],
            "timeline": d["timeline"]["value"],
            "gaps": d["gaps"]["value"],
            "diagnostics": d["diagnostics"]["value"],
            "report": v["report"],
            "decisions": d.get("decisions", {}),
            "event": self.store.events(project_id, vid),
        }

    # ----- counterexample archive ----------------------------------------
    COUNTEREXAMPLES = [
        ("unknown-length", "未知长度", "mutate_unknown_length_segment", "R6",
         "Segment 改为未知长度；结尾按父容器 EOF 界定，不改动任何子元素"),
        ("relative-overflow", "relative timecode 溢出", "mutate_relative_overflow",
         "R1", "相对时码按有符号 16 位解释；无符号读法保留但拒绝"),
        ("lacing-corrupt", "lacing 损坏", "mutate_lacing_corrupt", "R2",
         "lacing 元数据矛盾时不猜测帧边界，朴素等分作为被拒绝候选"),
        ("discard-padding", "discard padding", "mutate_discard_padding", None,
         "DiscardPadding 超过 BlockDuration，记录溢出诊断"),
        ("cue-mispoint", "cue 错指", "mutate_cue_mispoint", "R3",
         "CueClusterPosition 偏离真实 cluster；cue 仍是索引证据，不移动时间轴"),
        ("track-timebase", "轨道时基差异", "mutate_track_timebase", None,
         "TimecodeScale 改写，所有派生时标重算，原始字节位置不变"),
        ("cluster-gap", "中途 cluster 缺口", "mutate_cluster_gap", None,
         "第三个 ClusterTimecode 前移制造无块覆盖区间"),
    ]

    def build_counterexample_archive(self, project_id: str,
                                     baseline: bytes) -> dict:
        """Baseline -> one branch per single-source mutation.

        Both adopted and rejected results are stored; each finding carries its
        rule_assertion event and affected-object identities.
        """
        from . import mutations as M
        # Fresh baseline project is expected to already contain the import.
        base_ver = self.store.head_id(project_id, "main")
        archive: dict = {
            "project_id": project_id, "baseline_version": base_ver,
            "baseline_sha1": hashlib.sha1(baseline).hexdigest(),
            "cases": [],
        }
        for code, title, fn_name, rule, rationale in self.COUNTEREXAMPLES:
            fn = getattr(M, fn_name)
            mutated, byte_range = fn(baseline)
            branch = f"counter/{code}"
            affected = self._expected_affected(code)
            self.store.new_branch(project_id, branch, base_ver)
            info = self.patch_source(
                project_id, mutated, title, byte_range, affected, rationale,
                branch=branch, author="tribunal-bot",
                label=f"反例：{title}", kind="counterexample")
            snap = self.snapshot(project_id, info["version_id"], branch)
            finding = self._assess(code, snap)
            # Machine-readable judgement event(s).
            if rule:
                event = ev.rule_assertion(
                    rule, finding["subject"], finding["accepted"],
                    finding["rejected"], rationale, byte_range)
                self._append_rule_event(project_id, branch, snap, event,
                                        info["version_id"])
            contained = self._check_containment(snap, affected)
            archive["cases"].append({
                "code": code, "title": title, "rule": rule,
                "branch": branch, "version_id": info["version_id"],
                "byte_range": list(byte_range),
                "input_sha1": hashlib.sha1(mutated).hexdigest(),
                "affected_identities": affected,
                "finding": finding, "containment": contained,
                "adopted": finding["accepted"],
                "not_adopted": finding["rejected"],
                "rationale": rationale,
            })
        return archive

    def _expected_affected(self, code: str) -> list:
        mapping = {
            "unknown-length": ["segment", "e-1f43b675*"],
            "relative-overflow": ["e-a3*"],
            "lacing-corrupt": ["e-a3*:f*"],
            "discard-padding": ["e-a0*"],
            "cue-mispoint": ["e-b7*"],
            "track-timebase": ["*"],
            "cluster-gap": ["e-1f43b675*", "gap:*"],
        }
        return mapping.get(code, [])

    def _assess(self, code: str, snap: dict) -> dict:
        if code == "relative-overflow":
            b = next(x for x in snap["blocks"]
                     if any(d["code"] == "relative-timecode-overflow"
                            for d in snap["diagnostics"]
                            if d.get("identity") == x["identity"]))
            return {"subject": b["identity"],
                    "accepted": [{"interpretation": "signed16",
                                  "rel_tc": b["rel_tc"]["value"],
                                  "absolute_ns": b["absolute_ns"]}],
                    "rejected": [{"interpretation": "unsigned16",
                                  "absolute_ns": b["unsigned_alt"]["absolute_ns"]}]}
        if code == "lacing-corrupt":
            f0 = next(f for f in snap["frames"] if f["status"] == "corrupt-lacing")
            return {"subject": f0["block"],
                    "accepted": [{"frame_boundaries": None,
                                  "policy": "withhold"}],
                    "rejected": [{"frame_boundaries": "naive-even-split",
                                  "policy": "guess"}]}
        if code == "cue-mispoint":
            c = next(c for c in snap["cues"] if c["mismatch"])
            return {"subject": c["identity"],
                    "accepted": [{"usage": "index-evidence",
                                  "target": c["mismatch"]["nearest_cluster"]}],
                    "rejected": [{"usage": "time-truth",
                                  "claimed_offset": c["target_cluster_offset"]}]}
        if code == "cluster-gap":
            g = next(g for g in snap["gaps"] if g["code"] == "cluster-gap")
            return {"subject": g["identity"],
                    "accepted": [{"gap_ns": g["gap"]["value"]}],
                    "rejected": [{"policy": "fill-by-interpolation"}]}
        return {"subject": code, "accepted": [{"status": "diagnostic-recorded"}],
                "rejected": []}

    def _check_containment(self, snap: dict, expected: list) -> dict:
        changed = [r for r in snap["report"] if r["status"] != "still-valid"]
        return {"recomputed_nodes": [r["key"] for r in changed],
                "still_valid_nodes": [r["key"] for r in snap["report"]
                                      if r["status"] == "still-valid"]}

    def _append_rule_event(self, project_id, branch, snap, event,
                           current_version_id) -> None:
        v = self.store.get_version(current_version_id)
        doc = copy.deepcopy(v["doc"])
        new_payload = {
            "version_id": self._vid(branch, event, doc) + "-r",
            "parent_id": current_version_id, "branch": branch,
            "kind": "rule_assertion", "label": "规则裁定事件",
            "author": "tribunal-bot", "doc": doc,
            "derived": v["derived"], "report": [],
            "rules_version": RULES_VERSION, "event": event,
        }
        self.store.commit_version(project_id, new_payload,
                                  expected_parent=current_version_id)
