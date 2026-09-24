"""Application service: turns imports, parameter revisions, source patches
and human judgments into new immutable versions on top of Store + derive.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import derive
from store import SCHEMA_VERSION, ConflictError, Store


def _commit(store: Store, parent_id, label, kind, event_payload,
            source_bytes, crash_mid=False) -> int:
    parent_payload = None
    if parent_id is not None:
        parent_payload = store.get_version(parent_id)["payload"]
    seq = store.next_event_seq()
    event = {
        "seq": seq,
        "kind": kind,
        "payload": event_payload,
        "base_version_id": parent_id,
    }
    events = store.list_events() + [event]
    payload = derive.build_payload(source_bytes, events, parent_payload)
    return store.commit_version(parent_id, label, payload, event,
                                crash_mid=crash_mid)


def _source_of(store: Store, version_id) -> bytes:
    version = store.get_version(version_id)
    return store.get_source(version["payload"]["source_sha256"])


def import_fixture(store: Store, fixture_path, label="基线导入") -> int:
    data = Path(fixture_path).read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    store.put_source(data, sha)
    return _commit(store, None, label, "import", {
        "fixture": Path(fixture_path).name,
        "sha256": sha,
        "size": len(data),
        "note": "EBML 元素偏移、未知长度、track 参数、cluster/block 时码、"
                "discard padding、cue 和原字节范围均保留",
    }, data)


def revise_params(store: Store, base_version_id, params: dict,
                  note="", reviewer="") -> int:
    _check_base(store, base_version_id)
    source = _source_of(store, base_version_id)
    return _commit(store, base_version_id, "参数修订", "param_revision", {
        "set": params, "note": note, "reviewer": reviewer,
    }, source)


def patch_source(store: Store, base_version_id, offset: int,
                 new_bytes: bytes, note="", reviewer="",
                 crash_mid=False) -> int:
    _check_base(store, base_version_id)
    old = _source_of(store, base_version_id)
    old_slice = old[offset:offset + len(new_bytes)]
    patched = old[:offset] + new_bytes + old[offset + len(new_bytes):]
    sha = hashlib.sha256(patched).hexdigest()
    store.put_source(patched, sha)
    return _commit(store, base_version_id, "源数据改动", "source_patch", {
        "offset": offset,
        "old_hex": old_slice.hex(),
        "new_hex": new_bytes.hex(),
        "sha256": sha,
        "note": note,
        "reviewer": reviewer,
    }, patched, crash_mid=crash_mid)


def judge(store: Store, base_version_id, anomaly_id, decision,
          candidate_id=None, note="", reviewer="", rules=None,
          allow_branch=False) -> int:
    current = store.current_version_id()
    if base_version_id != current and not allow_branch:
        raise ConflictError(current)
    version = store.get_version(base_version_id)
    anomalies = version["payload"]["nodes"]["anomalies"]
    known = {a["id"] for a in anomalies}
    if anomaly_id not in known:
        raise KeyError(f"unknown anomaly {anomaly_id}")
    source = _source_of(store, base_version_id)
    return _commit(store, base_version_id, f"判定 {anomaly_id}", "judgment", {
        "anomaly_id": anomaly_id,
        "decision": decision,
        "candidate_id": candidate_id,
        "note": note,
        "reviewer": reviewer,
        "rules": rules or [],
    }, source)


def select_branch(store: Store, version_id, reviewer="", note="") -> None:
    store.get_version(version_id)  # validates existence
    store.set_current(version_id)
    store.add_pointer_event("branch_select", {
        "selected_version": version_id,
        "reviewer": reviewer,
        "note": note,
        "kept": "未选分支保留在版本库中，可随时比较或切回",
    }, version_id)


def revert_to_event(store: Store, event_id) -> int:
    event = store.get_event(event_id)
    version_id = event["version_id"]
    if version_id is None:
        raise ValueError("event has no associated version")
    store.set_current(version_id)
    store.add_pointer_event("revert", {
        "to_event_id": event_id,
        "restored_version": version_id,
        "note": "恢复当时的依赖版本和可见结论，历史事件不覆盖",
    }, version_id)
    return version_id


def export_all(store: Store) -> dict:
    sources = [{
        "sha256": r["sha256"],
        "data_hex": bytes(r["data"]).hex(),
    } for r in store.conn.execute("SELECT * FROM sources ORDER BY sha256")]
    versions = []
    for r in store.conn.execute("SELECT * FROM versions ORDER BY id"):
        versions.append({
            "id": r["id"], "parent_id": r["parent_id"], "seq": r["seq"],
            "label": r["label"], "status": r["status"],
            "payload": json.loads(r["payload_json"]),
            "created_at": r["created_at"],
        })
    events = store.list_events()
    return {
        "schema_version": SCHEMA_VERSION,
        "sources": sources,
        "versions": versions,
        "events": events,
        "current": store.current_version_id(),
    }


def import_all(store: Store, data: dict) -> None:
    if data.get("schema_version", 0) > SCHEMA_VERSION:
        raise ValueError("export comes from a newer schema")
    store.reset()
    conn = store.conn
    for s in data.get("sources", []):
        conn.execute("INSERT INTO sources(sha256, data) VALUES(?,?)",
                     (s["sha256"], bytes.fromhex(s["data_hex"])))
    for v in data.get("versions", []):
        conn.execute(
            "INSERT INTO versions(id, parent_id, seq, label, status,"
            " payload_json, created_at) VALUES(?,?,?,?,?,?,?)",
            (v["id"], v["parent_id"], v.get("seq"), v["label"], v["status"],
             json.dumps(v["payload"]), v["created_at"]))
    for e in data.get("events", []):
        conn.execute(
            "INSERT INTO events(id, version_id, base_version_id, seq, kind,"
            " payload_json, created_at) VALUES(?,?,?,?,?,?,?)",
            (e["id"], e["version_id"], e.get("base_version_id"), e["seq"],
             e["kind"], json.dumps(e["payload"]), e["created_at"]))
    conn.execute("UPDATE current_version SET version_id=? WHERE id=1",
                 (data.get("current"),))
    conn.commit()


def _check_base(store: Store, base_version_id):
    current = store.current_version_id()
    if base_version_id != current:
        raise ConflictError(current)
