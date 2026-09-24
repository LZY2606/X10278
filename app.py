"""Matroska 时码庭 — review workbench for Matroska/EBML multi-track timecodes.

Run:  .venv/bin/uvicorn app:app --host 127.0.0.1 --port 5375
"""
from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from mkv import casepack, derive, rules
from mkv.store import Store

DB_PATH = os.environ.get("MKV_DB", os.path.join("data", "case.db"))
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
os.makedirs("fixtures", exist_ok=True)

casepack.ensure_fixture()
store = Store(DB_PATH)

app = FastAPI(title="Matroska 时码庭")


@app.get("/")
def index():
    return FileResponse(os.path.join("static", "index.html"))


@app.get("/api/state")
def state():
    return store.full_state()


@app.post("/api/import")
def import_case():
    if store.current_version() is not None:
        return {"status": "already_imported",
                "version": store.current_version()}
    return casepack.import_case(store)


@app.post("/api/demo")
def demo():
    return casepack.run_demo(store)


class CheckoutBody(BaseModel):
    version_id: int
    actor: str = "reviewer"


@app.post("/api/checkout")
def checkout(body: CheckoutBody):
    try:
        return store.checkout(body.version_id, actor=body.actor)
    except KeyError as exc:
        raise HTTPException(404, str(exc))


class DecideBody(BaseModel):
    object: str
    candidate: str
    base_version: int
    actor: str = "reviewer"
    rationale: str = ""


@app.post("/api/decide")
def decide(body: DecideBody):
    nodes = store.version_nodes(body.base_version)
    if not nodes:
        raise HTTPException(404, f"version {body.base_version} not found")
    decisions = {"selections": dict(
        nodes["decisions"]["payload"].get("selections", {}))}
    decisions["selections"][body.object] = body.candidate
    events = [{
        "kind": "decision.accept",
        "payload": {"object": body.object,
                    "accepted_candidate": body.candidate,
                    "rationale": body.rationale,
                    "statement": "cue is index evidence, not time truth; "
                                 "unselected candidates are retained"},
        "actor": body.actor,
    }]
    return derive.derive(
        store, body.base_version,
        f"决定: {body.object} → {body.candidate} ({body.actor})",
        {"decisions": decisions}, events, actor=body.actor,
        expected_parent=body.base_version)


class ParamBody(BaseModel):
    timecode_scale_ns: int
    rationale: str = ""
    actor: str = "reviewer"


@app.post("/api/params/revise")
def revise_params(body: ParamBody):
    cur = store.current_version()
    if cur is None:
        raise HTTPException(409, "import the case first")
    old = store.version_nodes(cur)["params"]["payload"]
    new = dict(old)
    new["timecode_scale_ns"] = body.timecode_scale_ns
    new["revision_reason"] = body.rationale
    events = [{
        "kind": "param.revise",
        "payload": {
            "param": "timecode_scale_ns",
            "old": {"value": old["timecode_scale_ns"], "unit": "ns/tick"},
            "new": {"value": body.timecode_scale_ns, "unit": "ns/tick"},
            "rationale": body.rationale,
        },
        "actor": body.actor,
    }]
    return derive.derive(
        store, cur,
        f"参数修订: TimecodeScale → {body.timecode_scale_ns} ns",
        {"params": new}, events, actor=body.actor, expected_parent=cur)


@app.post("/api/source/amend")
def amend_source():
    if store.current_version() is None:
        raise HTTPException(409, "import the case first")
    return casepack.amend_source(store)


class ResolveBody(BaseModel):
    choice: str
    actor: str = "reviewer"


@app.post("/api/conflicts/{cid}/resolve")
def resolve_conflict(cid: int, body: ResolveBody):
    try:
        return store.resolve_conflict(cid, body.choice, actor=body.actor)
    except (KeyError, ValueError) as exc:
        raise HTTPException(409, str(exc))


@app.get("/api/explain")
def explain(key: str):
    vid = store.current_version()
    nodes = store.version_nodes(vid) if vid is not None else {}
    node = nodes.get(key)
    if node is None:
        raise HTTPException(404, f"node {key} not found in v{vid}")
    return {"version": vid, "key": key, "digest": node["digest"],
            "inputs": node["inputs"], "explanation": node["explanation"],
            "payload": node["payload"]}


@app.get("/api/export")
def export_dump():
    return store.export_dump()


@app.post("/api/import_db")
def import_db(dump: dict):
    try:
        store.import_dump(dump)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"status": "imported", "current": store.current_version()}


@app.post("/api/demo_roundtrip")
def demo_roundtrip():
    """Export -> wipe local database -> re-import. Identities, ordering and
    provenance relations must survive unchanged."""
    dump = store.export_dump()
    store.wipe()
    store.import_dump(dump)
    after = store.export_dump()
    same = dump["tables"]["versions"] == after["tables"]["versions"] \
        and dump["tables"]["nodes"] == after["tables"]["nodes"] \
        and dump["tables"]["events"] == after["tables"]["events"]
    return {"status": "roundtrip", "identities_preserved": bool(same),
            "current": store.current_version()}
