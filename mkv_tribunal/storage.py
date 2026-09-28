"""SQLite 存储层：事件溯源 + 快照 + 原子指针切换。

写路径：所有改动进一个事务 -> 写 events/snapshot -> 最后一行更新
meta.current_commit。崩溃在指针更新前则重启只看到旧版本；指针更新成功
则全部内容已落盘（单事务原子）。

并发：每个提交必须带 parent_commit；与当前指针不一致时抛 ConflictError，
调用方拉取对方决定后用 resolve_conflict 合并，合并本身也是一次提交，
被拒绝的内容作为 decision 事件保留。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

CURRENT_SCHEMA_VERSION = 2


class ConflictError(Exception):
    def __init__(self, current_commit: str, your_parent: str):
        super().__init__("父提交 %s 已过期，当前为 %s"
                         % (your_parent, current_commit))
        self.current_commit = current_commit
        self.your_parent = your_parent


def new_id() -> str:
    return uuid.uuid4().hex


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    commit_id TEXT NOT NULL,
    parent_commit_id TEXT,
    author TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(project_id, seq)
);
CREATE TABLE IF NOT EXISTS snapshots (
    commit_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    parent_commit_id TEXT,
    created_at TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    state_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_commit ON events(commit_id);
CREATE INDEX IF NOT EXISTS idx_snap_project ON snapshots(project_id);
"""


def hash_state(state: dict) -> str:
    blob = json.dumps(state, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


class Store:
    def __init__(self, path: str):
        self.path = path
        first_time = not os.path.exists(path)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(_SCHEMA)
        self._init_meta()
        self._migrate()

    def close(self) -> None:
        self.conn.close()

    def _init_meta(self) -> None:
        cur = self.conn.execute("SELECT value FROM meta WHERE key='schema_version'")
        row = cur.fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO meta(key,value) VALUES('schema_version',?)",
                (str(CURRENT_SCHEMA_VERSION),))
            self.conn.commit()

    def _migrate(self) -> None:
        cur = self.conn.execute("SELECT value FROM meta WHERE key='schema_version'")
        version = int(cur.fetchone()[0])
        if version == 1:
            # v1 -> v2：v1 的决策载荷缺少 basis_units 字段，保持历史不动，
            # 在状态上打 migrated 标记，新版本读取时补默认基准。
            self.conn.execute(
                "INSERT OR REPLACE INTO meta(key,value) "
                "VALUES('migration_note',?)",
                ("v1->v2: decision basis_units 默认 ns/ticks 原位保留",))
            self.conn.execute(
                "UPDATE meta SET value='2' WHERE key='schema_version'")
            self.conn.commit()
        elif version > CURRENT_SCHEMA_VERSION:
            raise RuntimeError("数据库 schema 版本 %d 比软件新" % version)

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ---- 项目 / 指针 ----
    def create_project(self, name: str,
                       project_id: str | None = None) -> str:
        pid = project_id or new_id()
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO projects(project_id,name,created_at) "
                "VALUES(?,?,?)", (pid, name, _now()))
            empty = {"version": 0, "sources": {}, "derivations": {},
                     "parameters": {"gap_threshold_ns": 1_000_000_000},
                     "decisions": [], "studies": {}, "migration": None}
            commit_id = new_id()
            conn.execute(
                "INSERT INTO snapshots(commit_id,project_id,parent_commit_id,"
                "created_at,state_hash,state_json) VALUES(?,?,?,?,?,?)",
                (commit_id, pid, None, _now(), hash_state(empty),
                 json.dumps(empty, ensure_ascii=False)))
            conn.execute(
                "INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                ("current_commit:" + pid, commit_id))
        return pid

    def current_commit(self, project_id: str) -> str:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key=?",
            ("current_commit:" + project_id,)).fetchone()
        if row is None:
            raise KeyError("项目不存在: %s" % project_id)
        return row[0]

    def state(self, project_id: str,
              commit_id: str | None = None) -> dict[str, Any]:
        commit_id = commit_id or self.current_commit(project_id)
        row = self.conn.execute(
            "SELECT state_json,state_hash FROM snapshots WHERE commit_id=?",
            (commit_id,)).fetchone()
        if row is None:
            raise KeyError("提交不存在: %s" % commit_id)
        state = json.loads(row["state_json"])
        if hash_state(state) != row["state_hash"]:
            raise RuntimeError("快照哈希不匹配，存储可能损坏: %s" % commit_id)
        return state

    def commit(self, project_id: str, state: dict, events: list[dict],
               *, author: str = "reviewer",
               parent_commit: str | None = None,
               crash_before_pointer: bool = False) -> str:
        """原子提交。events: [{kind, payload, event_id?}]。"""
        head = self.current_commit(project_id)
        parent_commit = parent_commit or head
        if parent_commit != head:
            raise ConflictError(head, parent_commit)
        commit_id = new_id()
        seq_row = self.conn.execute(
            "SELECT COALESCE(MAX(seq),0) AS m FROM events WHERE project_id=?",
            (project_id,)).fetchone()
        next_seq = seq_row["m"] + 1
        try:
            # 故意崩溃点：全部内容写入后、指针切换前 rollback。
            with self._tx() as conn:
                for i, ev in enumerate(events):
                    ev_id = ev.get("event_id") or new_id()
                    conn.execute(
                        "INSERT INTO events(event_id,project_id,seq,commit_id,"
                        "parent_commit_id,author,kind,payload_json,created_at)"
                        " VALUES(?,?,?,?,?,?,?,?,?)",
                        (ev_id, project_id, next_seq + i, commit_id,
                         parent_commit, author, ev["kind"],
                         json.dumps(ev.get("payload", {}), ensure_ascii=False),
                         _now()))
                state_hash = hash_state(state)
                conn.execute(
                    "INSERT INTO snapshots(commit_id,project_id,"
                    "parent_commit_id,created_at,state_hash,state_json)"
                    " VALUES(?,?,?,?,?,?)",
                    (commit_id, project_id, parent_commit, _now(),
                     state_hash,
                     json.dumps(state, ensure_ascii=False)))
                if crash_before_pointer:
                    raise RuntimeError("模拟写入中断：指针切换前崩溃")
                conn.execute(
                    "INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                    ("current_commit:" + project_id, commit_id))
        except RuntimeError as exc:
            if "模拟写入中断" in str(exc):
                return commit_id  # 调用方据此判断：指针未动
            raise
        return commit_id

    def history(self, project_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT event_id,seq,commit_id,parent_commit_id,author,kind,"
            "payload_json,created_at FROM events WHERE project_id=? "
            "ORDER BY seq", (project_id,)).fetchall()
        return [
            {"event_id": r["event_id"], "seq": r["seq"],
             "commit_id": r["commit_id"],
             "parent_commit_id": r["parent_commit_id"],
             "author": r["author"], "kind": r["kind"],
             "payload": json.loads(r["payload_json"]),
             "created_at": r["created_at"]}
            for r in rows]

    def list_projects(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(
            "SELECT project_id,name,created_at FROM projects").fetchall()]

    def event(self, project_id: str, event_id: str) -> dict[str, Any]:
        for ev in self.history(project_id):
            if ev["event_id"] == event_id:
                return ev
        raise KeyError(event_id)

    def commit_of_event(self, project_id: str, event_id: str) -> str:
        return self.event(project_id, event_id)["commit_id"]

    # ---- 导出 / 导入 ----
    def export_project(self, project_id: str) -> dict[str, Any]:
        project = self.conn.execute(
            "SELECT project_id,name,created_at FROM projects WHERE project_id=?",
            (project_id,)).fetchone()
        if project is None:
            raise KeyError(project_id)
        snapshots = [dict(r) for r in self.conn.execute(
            "SELECT commit_id,project_id,parent_commit_id,created_at,"
            "state_hash,state_json FROM snapshots WHERE project_id=? "
            "ORDER BY rowid", (project_id,)).fetchall()]
        events = self.history(project_id)
        return {
            "format": "mkv-tribunal-archive/1",
            "project": dict(project),
            "current_commit": self.current_commit(project_id),
            "snapshots": snapshots,
            "events": events,
        }

    def import_project(self, archive: dict[str, Any],
                       *, verify_only: bool = False) -> str:
        if archive.get("format") != "mkv-tribunal-archive/1":
            raise ValueError("档案格式不受支持: %r" % archive.get("format"))
        project = archive["project"]
        pid = project["project_id"]
        events = archive["events"]
        for i, ev in enumerate(events, start=1):
            if ev["seq"] != i:
                raise ValueError("事件 seq 断裂：位置 %d 为 %d" % (i, ev["seq"]))
        chain_ok = True
        for snap in archive["snapshots"]:
            state = json.loads(snap["state_json"])
            if hash_state(state) != snap["state_hash"]:
                chain_ok = False
        if not chain_ok:
            raise ValueError("快照状态哈希校验失败，拒绝导入")
        if verify_only:
            return pid
        existing = self.conn.execute(
            "SELECT 1 FROM projects WHERE project_id=?", (pid,)).fetchone()
        if existing is not None:
            raise ValueError("项目 %s 已存在（导出/删除/再导入请使用空库或新库）"
                             % pid)
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO projects(project_id,name,created_at) "
                "VALUES(?,?,?)",
                (pid, project["name"], project["created_at"]))
            for snap in archive["snapshots"]:
                conn.execute(
                    "INSERT INTO snapshots(commit_id,project_id,"
                    "parent_commit_id,created_at,state_hash,state_json)"
                    " VALUES(?,?,?,?,?,?)",
                    (snap["commit_id"], pid, snap["parent_commit_id"],
                     snap["created_at"], snap["state_hash"],
                     snap["state_json"]))
            for ev in events:
                conn.execute(
                    "INSERT INTO events(event_id,project_id,seq,commit_id,"
                    "parent_commit_id,author,kind,payload_json,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (ev["event_id"], pid, ev["seq"], ev["commit_id"],
                     ev["parent_commit_id"], ev["author"], ev["kind"],
                     json.dumps(ev["payload"], ensure_ascii=False),
                     ev["created_at"]))
            conn.execute(
                "INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                ("current_commit:" + pid, archive["current_commit"]))
        return pid
