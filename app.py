"""Matroska 时码庭 —— FastAPI 入口。

演示: .venv/bin/uvicorn app:app --host 127.0.0.1 --port 5375
"""
from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from app.casepkg import seed_case
from app.store import Store, ConflictError

DB_PATH = os.environ.get("CASE_DB", "case.db")
STATIC = os.path.join(os.path.dirname(__file__), "app", "static")

store = Store(DB_PATH)
if store.current_version() is None:
    seed_case(store)

app = FastAPI(title="Matroska 时码庭")


def _snapshot(vid: str) -> dict:
    version = store.get_version(vid)
    if not version:
        raise HTTPException(404, f"版本不存在: {vid}")
    return {"version": version, "nodes": store.get_nodes(vid),
            "events": store.get_events(vid)}


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


@app.get("/api/state")
def state():
    current = store.current_version()
    snap = _snapshot(current) if current else {"version": None, "nodes": {}, "events": []}
    return {
        "current": current,
        "versions": store.list_versions(),
        "conflicts": store.list_conflicts(),
        **snap,
    }


@app.get("/api/versions/{vid}")
def version_detail(vid: str):
    return _snapshot(vid)


@app.get("/api/events/{eid}")
def event_detail(eid: str):
    """回到任意旧事件:恢复当时的依赖版本与可见结论(不拿最新结果覆盖历史)。"""
    for ev in store.get_events():
        if ev["id"] == eid:
            return {"event": ev, **_snapshot(ev["version_id"])}
    raise HTTPException(404, f"事件不存在: {eid}")


class DecisionIn(BaseModel):
    base_version: str
    actor: str = "reviewer"
    action: str               # accept | reject | adjust
    label: str = ""
    params: dict | None = None


@app.post("/api/decisions")
def decide(inp: DecisionIn):
    base = store.get_version(inp.base_version)
    if not base:
        raise HTTPException(404, "base_version 不存在")
    data = store.get_source(inp.base_version)
    parent_params = store.get_nodes(inp.base_version)["params:effective"]["value"]
    params = dict(parent_params)
    if inp.action == "adjust" and inp.params:
        params.update(inp.params)
    label = inp.label or f"决定:{inp.action}"
    events = [{"type": "decision",
               "payload": {"action": inp.action, "actor": inp.actor,
                           "params_delta": inp.params or {},
                           "basis": "页面可见路径:EBML树/cluster区间/轨道时间线/cue指向/损坏字节范围"}}]
    try:
        r = store.save_version(inp.base_version, "decision", label, inp.actor,
                               data, params, events, base_version=inp.base_version)
        return {"status": "committed", **r}
    except ConflictError as exc:
        return JSONResponse({"status": "conflict", "conflict_id": exc.conflict_id,
                             "candidate_version": exc.version_id}, status_code=409)


class ResolveIn(BaseModel):
    choose: str               # 'a' 保持当前 | 'b' 切换到候选
    actor: str = "lead-reviewer"


@app.post("/api/conflicts/{cid}/resolve")
def resolve(cid: str, inp: ResolveIn):
    try:
        return store.resolve_conflict(cid, inp.choose, inp.actor)
    except KeyError:
        raise HTTPException(404, "冲突不存在")
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/export")
def export():
    return store.export_case()


@app.post("/api/import")
def import_case(payload: dict):
    store.import_case(payload)
    return {"status": "imported", "current": store.current_version()}


@app.post("/api/reset")
def reset():
    global store
    store.close()
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(DB_PATH + suffix)
        except FileNotFoundError:
            pass
    store = Store(DB_PATH)
    info = seed_case(store)
    return {"status": "reseeded", **info}


@app.get("/api/sample-path")
def sample_path():
    """90 秒样例路径:来源 → 派生 → 异常 → 分支 → 重放。"""
    current = store.current_version()
    nodes = store.get_nodes(current)
    versions = store.list_versions()
    summary = nodes["diagnostics:summary"]["value"]["by_kind"]
    steps = [
        {"step": 1, "view": "ebml-tree", "title": "来源:EBML 树与原字节范围",
         "detail": "每个元素带文件偏移;未知长度元素有标记;损坏范围保留原字节",
         "node": "parse:ebml"},
        {"step": 2, "view": "clusters", "title": "派生:cluster 区间与 block 时码",
         "detail": "relative timecode 按有符号 int16 解释;单位 tick→ns 在派生层规范化",
         "node": "cluster:1"},
        {"step": 3, "view": "timeline", "title": "多轨时间线(不按文件顺序强对齐)",
         "detail": "Track 2 的 TrackTimecodeScale=0.5,时基差异在派生层处理",
         "node": "track:2:timeline"},
        {"step": 4, "view": "anomalies", "title": "异常:lacing 损坏不猜帧边界",
         "detail": f"当前诊断: {list(summary)}",
         "node": "diagnostics:summary"},
        {"step": 5, "view": "cues", "title": "cue 是索引证据而不是时间真值",
         "detail": "错指 cue 只产生诊断,时间线不被改写",
         "node": "cues:validation"},
        {"step": 6, "view": "branches", "title": "分支与冲突:未选候选保留",
         "detail": f"共 {len(versions)} 个版本;冲突决定的两方都可回看",
         "node": None},
        {"step": 7, "view": "replay", "title": "重放:七类边界情形的判定事件",
         "detail": "未知长度/有符号时码/lacing损坏/discard padding/cue错指/时基差异/cluster缺口",
         "node": None},
    ]
    return {"steps": steps, "versions": versions,
            "events": store.get_events()}
