"""依赖图推导引擎。

每个派生对象声明依赖（可精确到另一对象的切片）。重算时只更新
受影响部分，并为每个对象记录原因：为何重算 / 为何仍然有效。
"""
from __future__ import annotations

import hashlib
import json


def hash_payload(payload) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest()


class Deriver:
    """一个派生对象（或一族对象）的生成规则。"""

    def __init__(self, kind, oids, deps, compute):
        self.kind = kind
        self._oids = oids
        self._deps = deps
        self._compute = compute

    def oids(self, objs):
        if callable(self._oids):
            return list(self._oids(objs))
        return [self._oids]

    def deps(self, oid, objs):
        return self._deps(oid, objs)

    def compute(self, oid, objs, ctx):
        return self._compute(oid, objs, ctx)


def _slice_hash(objs, slicers, dep_oid, slice_key):
    rec = objs.get(dep_oid)
    if rec is None:
        return None
    payload = rec["payload"]
    if slice_key is None:
        return hash_payload(payload)
    slicer = slicers.get(rec["kind"])
    if slicer is None:
        return hash_payload(payload)
    return hash_payload(slicer(payload, slice_key))


def derive(prev_objs, input_objs, derivers, slicers, ctx):
    """根据输入对象计算全部派生对象，返回 (objects, reasons)。"""
    objs = dict(input_objs)
    reasons = {oid: "input" for oid in input_objs}
    for deriver in derivers:
        for oid in deriver.oids(objs):
            deps = deriver.deps(oid, objs)
            changed = []
            for dep_oid, slice_key in deps:
                before = _slice_hash(prev_objs, slicers, dep_oid, slice_key)
                after = _slice_hash(objs, slicers, dep_oid, slice_key)
                if before != after:
                    label = dep_oid + ("#" + str(slice_key) if slice_key else "")
                    changed.append(label)
            if oid not in prev_objs:
                payload = deriver.compute(oid, objs, ctx)
                reason = "computed: 新对象"
            elif changed:
                payload = deriver.compute(oid, objs, ctx)
                reason = "recomputed: 依赖变化 -> " + ", ".join(changed)
            else:
                payload = prev_objs[oid]["payload"]
                reason = "valid: 依赖未变化，结果仍然有效"
            objs[oid] = {"kind": deriver.kind, "name": oid, "payload": payload}
            reasons[oid] = reason
    return objs, reasons
