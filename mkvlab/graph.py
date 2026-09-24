"""依赖图传播：源/规则变更时只标记并重算受影响对象，并给出有效性理由。"""
from __future__ import annotations

from typing import Optional

from . import engine


COLLECTIONS = ["tracks", "clusters", "cues", "gaps", "anomalies"]


def _index_objects(derived: dict) -> dict[str, tuple[str, dict]]:
    idx = {}
    for coll in COLLECTIONS:
        for obj in derived.get(coll, []):
            idx[obj["uid"]] = (coll, obj)
    # blocks/frames 嵌在 clusters 内，依赖图里它们是独立节点
    for cl in derived.get("clusters", []):
        for blk in cl["blocks"]:
            idx[blk["uid"]] = ("blocks", blk)
            for fr in blk.get("frames", []):
                idx[fr["uid"]] = ("frames", fr)
    return idx


def _overlaps(ranges, target):
    a, b = target
    for r in ranges:
        ra, rb = r[0], r[1]
        if ra < b and a < rb:
            return True
    return False


def affected_set(prev: dict, changed_ranges: list[tuple[int, int]],
                 changed_rule_keys: list[str], changed_track_numbers=None) -> set:
    """根据依赖边计算受影响节点（含传递：cluster->block->frame, timescale->全部）。"""
    affected: set[str] = set()
    deps = prev.get("dependencies", {})
    changed_track_numbers = changed_track_numbers or []
    for uid, edges in deps.items():
        for edge in edges:
            hit = False
            if edge.startswith("offset:"):
                off = int(edge.split(":", 1)[1])
                if any(a <= off < b for a, b in changed_ranges):
                    hit = True
            elif edge.startswith("timescale:"):
                if "rule:timecode_scale" in changed_rule_keys:
                    hit = True
            elif edge.startswith("rule:"):
                if edge in changed_rule_keys:
                    hit = True
            elif edge.startswith("track:"):
                if edge == "track:missing":
                    continue
                # track 参数变化 -> 该 track 的 block
                pass
            if hit:
                affected.add(uid)
    # 轨道参数变化
    track_uids = {t["uid"]: t["number"] for t in prev.get("tracks", [])}
    for uid, num in track_uids.items():
        if str(num) in [str(x) for x in changed_track_numbers]:
            affected.add(uid)
    # 传递：block 属于 cluster；frame 属于 block；cue/timeline 依赖 timescale
    closure = set(affected)
    changed = True
    while changed:
        changed = False
        for uid, edges in deps.items():
            if uid in closure:
                continue
            if any(e in closure for e in edges if not e.startswith(("offset:",
                                                                    "rule:",
                                                                    "timescale:",
                                                                    "track:"))):
                closure.add(uid)
                changed = True
    # timescale 变化波及全部 block/cue/timeline
    if "rule:timecode_scale" in changed_rule_keys:
        for uid, edges in deps.items():
            if any(e.startswith("timescale:") for e in edges):
                closure.add(uid)
    return closure


def propagate(prev: dict, fresh: dict, changed_ranges=None,
              changed_rule_keys=None, changed_track_numbers=None) -> dict:
    """比较新旧推导，输出 graph_report：每节点 unchanged/recomputed/added/removed
    以及理由。fresh 中仅受影响集合内的对象允许变化；集合外若发生变化说明依赖边缺失，
    报告 unexpected。
    """
    changed_ranges = changed_ranges or []
    changed_rule_keys = changed_rule_keys or []
    affected = affected_set(prev, changed_ranges, changed_rule_keys,
                            changed_track_numbers)
    old_idx = _index_objects(prev)
    new_idx = _index_objects(fresh)
    report = {}
    for uid, (coll, obj) in new_idx.items():
        if uid not in old_idx:
            report[uid] = {"status": "added",
                           "reasons": [f"新对象（集合 {coll}）"]}
            continue
        old_obj = old_idx[uid][1]
        if _fingerprint(old_obj) == _fingerprint(obj):
            report[uid] = {"status": "unchanged",
                           "reasons": ["依赖未变化，结果仍有效"]}
        else:
            if uid in affected:
                reasons = _why(prev, uid, changed_ranges, changed_rule_keys,
                               changed_track_numbers)
                report[uid] = {"status": "recomputed", "reasons": reasons}
            else:
                report[uid] = {"status": "unexpected_change",
                               "reasons": ["对象发生变化但不在受影响集合内："
                                           "依赖边缺失，需要补图"]}
    for uid, (coll, obj) in old_idx.items():
        if uid not in new_idx:
            report[uid] = {"status": "removed",
                           "reasons": [f"源对象消失（集合 {coll}）"]
                                     if uid in affected else
                                     ["对象消失但不在受影响集合内"]}
    fresh["graph_report"] = report
    fresh["affected"] = sorted(affected)
    return fresh


def _fingerprint(obj) -> str:
    import hashlib
    import json
    return hashlib.sha1(json.dumps(obj, sort_keys=True,
                                   ensure_ascii=False).encode()).hexdigest()


def _why(prev, uid, ranges, rules, track_nums):
    reasons = []
    for edge in prev.get("dependencies", {}).get(uid, []):
        if edge.startswith("offset:"):
            off = int(edge.split(":", 1)[1])
            hit = [r for r in ranges if r[0] <= off < r[1]]
            if hit:
                reasons.append(f"源字节区间 {hit[0]} 覆盖其元素偏移 {off}")
        elif edge.startswith("rule:") and edge in rules:
            reasons.append(f"规则/参数 {edge} 被修订")
        elif edge.startswith("timescale:") and "rule:timecode_scale" in rules:
            reasons.append("TimecodeScale 参数修订")
    if not reasons:
        reasons.append("其上游依赖被标记为受影响（图传递）")
    return reasons
