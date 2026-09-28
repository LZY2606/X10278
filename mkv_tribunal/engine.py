"""引擎：导入 Matroska 字节、版本化推导、候选裁定、历史视图。

每次导入 / 改动都生成新快照，只有完整成功后 Store 才切换当前指针。
所有人工动作产生事件；未选候选始终保留，不覆盖历史。
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import asdict
from typing import Any, Callable, Optional

from . import RULE_VERSION, graph
from .domain import (MatroskaFactSet, build_fact_set)
from .parser import parse_document
from .storage import ConflictError, Store


def _facts_to_json(facts: MatroskaFactSet) -> dict[str, Any]:
    return {
        "segment": asdict(facts.segment) if facts.segment else None,
        "tracks": [asdict(t) for t in facts.tracks],
        "clusters": [asdict(c) for c in facts.clusters],
        "blocks": [asdict(b) for b in blocks_list(facts)],
        "frames": [asdict(f) for f in facts.frames],
        "cues": [asdict(c) for c in facts.cues],
        "gaps": [asdict(g) for g in facts.gaps],
        "diagnostics": [asdict(d) for d in facts.diagnostics],
        "warnings": [asdict(d) for d in facts.warnings],
    }


def blocks_list(facts: MatroskaFactSet):
    return facts.blocks


def _count_elements(facts_json: dict) -> int:
    return facts_json.get("_element_count", 0)


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Engine:
    def __init__(self, store: Store):
        self.store = store

    # ---------- 状态构造 ----------
    @staticmethod
    def _initial_state() -> dict[str, Any]:
        return {
            "rule_version": RULE_VERSION,
            "version": 0,
            "sources": {},
            "facts": None,
            "derivations": {},
            "parameters": {"gap_threshold_ns": 1_000_000_000,
                           "cue_tolerance_ticks": 0,
                           "scale_override": None},
            "decisions": [],
            "studies": {},
        }

    def create_project(self, name: str,
                       project_id: str | None = None) -> str:
        pid = self.store.create_project(name, project_id)
        # create_project 已写入空快照；用引擎初始状态覆盖一次
        state = self._initial_state()
        state["project_id"] = pid
        self.store.commit(
            pid, state,
            [{"kind": "project-created", "payload": {"name": name}}],
            parent_commit=self.store.current_commit(pid))
        return pid

    # ---------- 导入 ----------
    def import_bytes(self, pid: str, data: bytes, *, label: str,
                     source_id: str = "source-0",
                     author: str = "reviewer",
                     parent_commit: str | None = None,
                     patches: list[dict] | None = None) -> str:
        state = self.store.state(pid, parent_commit)
        state = self._clone_state(state)
        actual_bytes = self._apply_patches(data, patches or [])
        facts, elements = self._compute(actual_bytes, state["parameters"])
        facts_json = _facts_to_json(facts)
        prop = graph.propagate(["source"])
        state["sources"][source_id] = {
            "source_id": source_id,
            "label": label,
            "byte_length": len(actual_bytes),
            "sha256": _hash_bytes(actual_bytes),
            "bytes_b64": base64.b64encode(actual_bytes).decode("ascii"),
            "patches": patches or [],
        }
        state["facts"] = facts_json
        state["elements"] = self._elements_json(elements)
        state["derivations"] = self._derivations(facts_json, state,
                                                 prop.dirty, [])
        state["last_propagation"] = {
            "changed_inputs": ["source"],
            "recomputed": prop.dirty,
            "kept": prop.kept,
            "explain": prop.explain(),
        }
        state["version"] = state.get("version", 0) + 1
        events = [{
            "kind": "source-imported",
            "payload": {
                "source_id": source_id, "label": label,
                "byte_length": len(actual_bytes),
                "sha256": _hash_bytes(actual_bytes),
                "patches": patches or [],
                "rule_version": RULE_VERSION,
            }}]
        return self.store.commit(pid, state, events, author=author,
                                 parent_commit=parent_commit)

    # ---------- 参数修订 ----------
    def update_parameter(self, pid: str, name: str, value: Any, *,
                         reason: str, author: str = "reviewer",
                         parent_commit: str | None = None) -> str:
        state = self._clone_state(self.store.state(pid, parent_commit))
        state["parameters"][name] = value
        data = self._source_bytes(state)
        changed = [graph.input_for_parameter(name)]
        prop = graph.propagate(changed)
        facts, _ = self._compute(data, state["parameters"])
        facts_json = _facts_to_json(facts)
        state["facts"] = facts_json
        state["derivations"] = self._derivations(facts_json, state,
                                                 prop.dirty, set())
        state["last_propagation"] = {
            "changed_inputs": changed, "recomputed": prop.dirty,
            "kept": prop.kept, "explain": prop.explain()}
        state["version"] += 1
        events = [{"kind": "parameter-revised",
                   "payload": {"name": name, "value": value,
                               "reason": reason,
                               "recomputed": prop.dirty,
                               "kept": prop.kept}}]
        return self.store.commit(pid, state, events, author=author,
                                 parent_commit=parent_commit)

    # ---------- 人工裁定 ----------
    def decide_candidate(self, pid: str, block_id: str,
                         candidate_index: int, *, accepted: bool,
                         rationale: str, author: str = "reviewer",
                         parent_commit: str | None = None) -> str:
        """接受/否定一个分帧候选。未选候选保留，确认不可逆覆盖是禁止的。"""
        state = self._clone_state(self.store.state(pid, parent_commit))
        block = self._find_block(state, block_id)
        if block is None:
            raise KeyError(block_id)
        candidates = block["frame_candidates"]
        if not 0 <= candidate_index < len(candidates):
            raise ValueError("候选序号越界: %d" % candidate_index)
        decision = {
            "decision_id": "dec-%s" % hashlib.sha1(
                (block_id + str(candidate_index) + rationale + author
                 ).encode()).hexdigest()[:12],
            "subject": block_id,
            "type": "lacing-candidate",
            "candidate_index": candidate_index,
            "candidate_sizes": candidates[candidate_index],
            "accepted": accepted,
            "rationale": rationale,
            "author": author,
            "unselected_preserved": True,
            "basis_units": "frame-bytes / segment-ns",
        }
        state["decisions"].append(decision)
        if accepted:
            block["selected_candidate"] = candidate_index
            block["lacing_definitive"] = True
        state["version"] += 1
        events = [{"kind": "decision-recorded", "payload": decision}]
        return self.store.commit(pid, state, events, author=author,
                                 parent_commit=parent_commit)

    def decide_cue(self, pid: str, cue_id: str, verdict: str, *,
                   rationale: str, author: str = "reviewer",
                   parent_commit: str | None = None) -> str:
        """对 cue 的裁定：accepted-index-evidence（仅作索引）或 rejected。
        cue 永远不能被改写成时间真值。"""
        if verdict not in ("accepted-index-evidence", "rejected",
                           "needs-investigation"):
            raise ValueError(verdict)
        state = self._clone_state(self.store.state(pid, parent_commit))
        cue = self._find_cue(state, cue_id)
        decision = {
            "decision_id": "dec-%s" % hashlib.sha1(
                (cue_id + verdict + rationale + author).encode()
            ).hexdigest()[:12],
            "subject": cue_id,
            "type": "cue-verdict",
            "verdict": verdict,
            "rationale": rationale,
            "author": author,
            "time_truth_changed": False,
            "unselected_preserved": True,
            "basis_units": "ticks / segment-ns / segment-relative-byte",
        }
        state["decisions"].append(decision)
        if cue is not None and verdict != "needs-investigation":
            cue["adjudication"] = verdict
        state["version"] += 1
        return self.store.commit(pid, state,
                                 [{"kind": "decision-recorded",
                                   "payload": decision}],
                                 author=author, parent_commit=parent_commit)

    def note_multitrack_alignment(self, pid: str, *, rationale: str,
                                  author: str = "reviewer",
                                  parent_commit: str | None = None) -> str:
        state = self._clone_state(self.store.state(pid, parent_commit))
        decision = {
            "decision_id": "dec-%s" % hashlib.sha1(
                ("multitrack" + rationale + author).encode()
            ).hexdigest()[:12],
            "subject": "timeline:all-tracks",
            "type": "multitrack-alignment-policy",
            "policy": "align-by-segment-timecode; never-by-file-order",
            "rationale": rationale,
            "author": author,
            "unselected_preserved": True,
            "basis_units": "segment-ns with per-track legacy basis annotation",
        }
        state["decisions"].append(decision)
        state["version"] += 1
        return self.store.commit(pid, state,
                                 [{"kind": "decision-recorded",
                                   "payload": decision}],
                                 author=author, parent_commit=parent_commit)

    # ---------- 冲突 ----------
    def resolve_conflict(self, pid: str, stale_state: dict,
                         stale_events: list[dict], *, resolution: str,
                         author: str = "reviewer") -> str:
        """以当前状态为基底重放被拒绝方意图。resolution 说明如何取舍，
        被拒内容完整保存在 payload.rejected 中。"""
        current = self._clone_state(self.store.state(pid))
        current["decisions"].append({
            "decision_id": "dec-conflict-%s" % hashlib.sha1(
                resolution.encode()).hexdigest()[:8],
            "subject": "concurrent-edit",
            "type": "conflict-resolution",
            "resolution": resolution,
            "rejected_intent_events": stale_events,
            "author": author,
            "unselected_preserved": True,
            "basis_units": "event-ids",
        })
        current["version"] += 1
        return self.store.commit(pid, current, [{
            "kind": "conflict-resolved",
            "payload": {"resolution": resolution,
                        "rejected_event_count": len(stale_events),
                        "rejected": stale_events}}], author=author)

    # ---------- 历史视图 ----------
    def state_at_event(self, pid: str, event_id: str) -> dict:
        commit_id = self.store.commit_of_event(pid, event_id)
        return self.store.state(pid, commit_id)

    # ---------- 计算与工具 ----------
    @staticmethod
    def _apply_patches(data: bytes, patches: list[dict]) -> bytes:
        raw = bytearray(data)
        for patch in patches:
            offset = patch["offset"]
            new = bytes.fromhex(patch["new_hex"])
            if "old_hex" in patch:
                old = bytes.fromhex(patch["old_hex"])
                if bytes(raw[offset:offset + len(old)]) != old:
                    raise ValueError(
                        "补丁前置值不匹配 @%d：期望 %s，实际 %s"
                        % (offset, patch["old_hex"],
                           bytes(raw[offset:offset + len(old)]).hex()))
            if "end" in patch and patch["end"] != offset + len(new):
                raise ValueError("补丁长度必须保持不变（固定偏移取证）")
            raw[offset:offset + len(new)] = new
        return bytes(raw)

    @staticmethod
    def _compute(data: bytes, parameters: dict):
        doc = parse_document(data)
        facts = build_fact_set(
            doc.elements, doc.diagnostics,
            gap_threshold_ns=parameters.get("gap_threshold_ns",
                                            1_000_000_000))
        return facts, doc.elements

    @staticmethod
    def _clone_state(state: dict) -> dict:
        import json
        return json.loads(json.dumps(state))

    @staticmethod
    def _source_bytes(state: dict) -> bytes:
        source = next(iter(state.get("sources", {}).values()), None)
        if source is None:
            raise ValueError("还没有导入源数据")
        import base64 as b64
        return b64.b64decode(source["bytes_b64"])

    @staticmethod
    def _elements_json(elements) -> list[dict]:
        def walk(el, depth):
            value = el.value
            if isinstance(value, bytes):
                value = {"__binary__": True, "hex": value[:32].hex(),
                         "length": len(value)}
            return {
                "stable_id": "el@%d" % el.offset,
                "id": "0x%X" % el.element_id,
                "name": el.name,
                "type": el.type,
                "depth": depth,
                "offset": el.offset,
                "id_width": el.id_width,
                "length_width": el.length_width,
                "data_offset": el.data_offset,
                "declared_length": el.declared_length,
                "effective_end": el.effective_end,
                "unknown": el.unknown,
                "value": value,
                "diagnostics": [asdict(d) for d in el.diagnostics],
                "children": [walk(c, depth + 1) for c in el.children],
            }
        return [walk(e, 0) for e in elements]

    def _derivations(self, facts_json: dict, state: dict, dirty: list[str],
                     scope: set[str],
                     previous: dict | None = None) -> dict:
        derivations: dict[str, Any] = {}
        prev = previous or state.get("derivations", {})
        for node in graph.NODES:
            if node in dirty:
                derivations[node] = {
                    "node": node,
                    "rule_version": RULE_VERSION,
                    "status": "recomputed",
                    "scope": sorted(scope) if scope else "full",
                    "summary": self._summarize(node, facts_json,
                                                state["parameters"]),
                }
            elif node in prev:
                kept = dict(prev[node])
                kept["status"] = "kept-valid"
                kept["why_kept"] = "无被改输入到达该节点"
                derivations[node] = kept
            else:
                derivations[node] = {
                    "node": node, "rule_version": RULE_VERSION,
                    "status": "recomputed",
                    "summary": self._summarize(node, facts_json,
                                                state["parameters"])}
        return derivations

    @staticmethod
    def _summarize(node: str, facts: dict, params: dict) -> dict:
        if node == "ebml-tree":
            return {"element_count": _count_elements(facts)}
        if node == "facts":
            seg = facts.get("segment")
            return {"timecode_scale_ns": seg["timecode_scale_raw"]
                    if seg else None,
                    "tracks": len(facts["tracks"]),
                    "clusters": len(facts["clusters"]),
                    "blocks": len(facts["blocks"]),
                    "cues": len(facts["cues"])}
        if node == "timelines":
            return {"frames": len(facts["frames"])}
        if node == "lacing":
            damaged = [b["block_id"] for b in facts["blocks"]
                       if b["damaged"]]
            ambiguous = [b["block_id"] for b in facts["blocks"]
                         if len(b["candidate_counts"]) > 1]
            return {"damaged": damaged, "ambiguous": ambiguous}
        if node == "cues":
            return {"statuses": {s: [c["cue_id"] for c in facts["cues"]
                                     if c["status"] == s]
                                 for s in ("ok", "mispoint",
                                           "missing-position",
                                           "unresolvable")},
                    "policy": "cue 是索引证据，不是时间真值",
                    "tolerance_ticks": params.get("cue_tolerance_ticks", 0)}
        if node == "gaps":
            return {"gaps": facts["gaps"],
                    "threshold_ns": params.get("gap_threshold_ns",
                                               1_000_000_000)}
        if node == "track-timebases":
            return {"legacy": [t["track_id"] for t in facts["tracks"]
                               if t["legacy_timebase"]],
                    "policy": "多轨按 segment 时码对齐，不按文件顺序"}
        return {}

    @staticmethod
    def _find_block(state: dict, block_id: str) -> Optional[dict]:
        for block in state.get("facts", {}).get("blocks", []):
            if block["block_id"] == block_id:
                return block
        return None

    @staticmethod
    def _find_cue(state: dict, cue_id: str) -> Optional[dict]:
        for cue in state.get("facts", {}).get("cues", []):
            if cue["cue_id"] == cue_id:
                return cue
        return None

    def patch_source(self, pid: str, data: bytes, patches: list[dict], *,
                     reason: str, source_id: str = "source-0",
                     author: str = "reviewer",
                     parent_commit: str | None = None) -> str:
        """只改动一个直接来源（同长度字节补丁），重算受影响部分。"""
        state = self.store.state(pid, parent_commit)
        state = self._clone_state(state)
        actual = self._apply_patches(data, patches)
        facts, elements = self._compute(actual, state["parameters"])
        facts_json = _facts_to_json(facts)
        changed = ["source"]
        # 补丁若带有 local-scope 标注，只影响对应节点；否则整源传播。
        scope = {p.get("scope") for p in patches if p.get("scope")}
        prop = graph.propagate([s for s in changed])
        prev_derivations = state.get("derivations", {})
        state["sources"][source_id] = {
            **state["sources"].get(source_id, {}),
            "source_id": source_id,
            "byte_length": len(actual),
            "sha256": _hash_bytes(actual),
            "bytes_b64": base64.b64encode(actual).decode("ascii"),
            "patches": state["sources"].get(source_id, {}).get("patches", [])
                       + patches,
        }
        state["facts"] = facts_json
        state["elements"] = self._elements_json(elements)
        state["derivations"] = self._derivations(
            facts_json, state, prop.dirty, scope, prev_derivations)
        state["last_propagation"] = {
            "changed_inputs": changed, "recomputed": prop.dirty,
            "kept": prop.kept, "scoped": sorted(s for s in scope if s),
            "explain": prop.explain(),
        }
        state["version"] += 1
        events = [{"kind": "source-patched",
                   "payload": {"reason": reason, "patches": patches,
                               "sha256": _hash_bytes(actual)}}]
        return self.store.commit(pid, state, events, author=author,
                                 parent_commit=parent_commit)
