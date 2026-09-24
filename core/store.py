"""版本化存储层（SQLite）。

语义：
- 每次推导/裁定产生独立版本；只有完整成功后才切换 current 指针。
- 写入中断 -> 事务回滚 -> 只能看到旧版本或完整新版本。
- 两个使用者基于同一版本保存时，后到者收到 ConflictError，
  可作为分支保存（save_branch），由人工选择继续哪一版，未选分支保留。
- 导出/删除数据库/导入后，对象身份、顺序与来源关系保持不变。
"""
from __future__ import annotations

import json
import sqlite3
import time

SCHEMA_VERSION = 2

SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS versions (
    id TEXT PRIMARY KEY, parent_id TEXT, seq INTEGER UNIQUE,
    note TEXT, actor TEXT, created_at REAL
);
CREATE TABLE IF NOT EXISTS objects (
    oid TEXT, version_id TEXT, ord INTEGER, kind TEXT, name TEXT,
    payload TEXT, reason TEXT,
    PRIMARY KEY (oid, version_id)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id TEXT, seq INTEGER, type TEXT, actor TEXT,
    payload TEXT, created_at REAL
);
CREATE TABLE IF NOT EXISTS blobs (id TEXT PRIMARY KEY, data BLOB);
CREATE TABLE IF NOT EXISTS current (id INTEGER PRIMARY KEY CHECK (id = 1), version_id TEXT);
"""


class ConflictError(Exception):
    def __init__(self, current: str, base: str):
        super().__init__(f"版本冲突：base={base} 但当前已是 {current}")
        self.current = current
        self.base = base


class Store:
    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self._migrate()

    def _migrate(self):
        row = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
        if row is None:
            self.conn.executescript(SCHEMA_V2)
            self.conn.execute("INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                              (str(SCHEMA_VERSION),))
            self.conn.commit()
            return
        version = int(self.conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0])
        if version < 2:
            # v1 -> v2：objects 增加 reason 列，events 增加 actor 列
            cols = [r[1] for r in self.conn.execute("PRAGMA table_info(objects)")]
            if "reason" not in cols:
                self.conn.execute("ALTER TABLE objects ADD COLUMN reason TEXT")
            cols = [r[1] for r in self.conn.execute("PRAGMA table_info(events)")]
            if "actor" not in cols:
                self.conn.execute("ALTER TABLE events ADD COLUMN actor TEXT DEFAULT 'system'")
            self.conn.execute("UPDATE meta SET value=? WHERE key='schema_version'",
                              (str(SCHEMA_VERSION),))
            self.conn.commit()

    # ---------- 读取 ----------
    def current(self) -> str | None:
        row = self.conn.execute("SELECT version_id FROM current WHERE id=1").fetchone()
        return row[0] if row else None

    def list_versions(self):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM versions ORDER BY seq")]

    def get_version(self, vid: str):
        row = self.conn.execute("SELECT * FROM versions WHERE id=?", (vid,)).fetchone()
        return dict(row) if row else None

    def version_objects(self, vid: str) -> dict:
        """按写入顺序返回 {oid: record}，保证顺序稳定。"""
        out = {}
        for r in self.conn.execute(
                "SELECT * FROM objects WHERE version_id=? ORDER BY ord", (vid,)):
            out[r["oid"]] = {
                "kind": r["kind"], "name": r["name"],
                "payload": json.loads(r["payload"]), "reason": r["reason"],
            }
        return out

    def current_objects(self) -> dict:
        cur = self.current()
        return self.version_objects(cur) if cur else {}

    def list_events(self):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM events ORDER BY id")]

    def get_event(self, event_id: int):
        row = self.conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        return dict(row) if row else None

    def get_blob(self, sha: str) -> bytes:
        row = self.conn.execute("SELECT data FROM blobs WHERE id=?", (sha,)).fetchone()
        if row is None:
            raise KeyError(f"blob 不存在: {sha}")
        return row[0]

    # ---------- 写入 ----------
    def _next_seq(self) -> int:
        row = self.conn.execute("SELECT MAX(seq) FROM versions").fetchone()
        return (row[0] or 0) + 1

    def save_version(self, base_id, objects: dict, events: list, note: str,
                     actor: str = "system", blobs: dict | None = None,
                     branch: bool = False) -> str:
        """原子地保存新版本。branch=True 时不移动 current 指针。"""
        cur = self.current()
        if not branch and base_id != cur:
            raise ConflictError(cur, base_id)
        seq = self._next_seq()
        vid = f"v{seq:04d}"
        now = time.time()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                "INSERT INTO versions(id, parent_id, seq, note, actor, created_at)"
                " VALUES(?,?,?,?,?,?)",
                (vid, base_id, seq, note, actor, now))
            for order, (oid, rec) in enumerate(objects.items()):
                self.conn.execute(
                    "INSERT INTO objects(oid, version_id, ord, kind, name, payload, reason)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (oid, vid, order, rec["kind"], rec["name"],
                     json.dumps(rec["payload"], ensure_ascii=False, sort_keys=True),
                     rec.get("reason")))
            for order, ev in enumerate(events):
                self.conn.execute(
                    "INSERT INTO events(version_id, seq, type, actor, payload, created_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (vid, order, ev["type"], ev.get("actor", actor),
                     json.dumps(ev.get("payload", {}), ensure_ascii=False), now))
            for sha, data in (blobs or {}).items():
                self.conn.execute("INSERT OR IGNORE INTO blobs(id, data) VALUES(?,?)",
                                  (sha, sqlite3.Binary(data)))
            if not branch:
                self.conn.execute(
                    "INSERT INTO current(id, version_id) VALUES(1, ?)"
                    " ON CONFLICT(id) DO UPDATE SET version_id=excluded.version_id",
                    (vid,))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return vid

    def choose_branch(self, vid: str, actor: str = "system"):
        """显式选择继续某一版本（保留未选分支）。"""
        if self.get_version(vid) is None:
            raise KeyError(vid)
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                "INSERT INTO current(id, version_id) VALUES(1, ?)"
                " ON CONFLICT(id) DO UPDATE SET version_id=excluded.version_id", (vid,))
            self.conn.execute(
                "INSERT INTO events(version_id, seq, type, actor, payload, created_at)"
                " VALUES(?,?,?,?,?,?)",
                (vid, 0, "branch.chosen", actor,
                 json.dumps({"chosen": vid}, ensure_ascii=False), time.time()))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def restore_to_event(self, event_id: int, actor: str = "system") -> str:
        """回到任意旧事件：恢复当时的依赖版本与可见结论（指针回移，历史不被覆盖）。"""
        ev = self.get_event(event_id)
        if ev is None:
            raise KeyError(event_id)
        vid = ev["version_id"]
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                "INSERT INTO current(id, version_id) VALUES(1, ?)"
                " ON CONFLICT(id) DO UPDATE SET version_id=excluded.version_id", (vid,))
            self.conn.execute(
                "INSERT INTO events(version_id, seq, type, actor, payload, created_at)"
                " VALUES(?,?,?,?,?,?)",
                (vid, 0, "restored", actor,
                 json.dumps({"restored_event": event_id, "version": vid},
                            ensure_ascii=False), time.time()))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return vid

    # ---------- 导出 / 导入 ----------
    def export_json(self) -> dict:
        return {
            "meta": {r["key"]: r["value"] for r in self.conn.execute("SELECT * FROM meta")},
            "versions": [dict(r) for r in self.conn.execute(
                "SELECT * FROM versions ORDER BY seq")],
            "objects": [dict(r) for r in self.conn.execute(
                "SELECT * FROM objects ORDER BY version_id, ord")],
            "events": [dict(r) for r in self.conn.execute(
                "SELECT * FROM events ORDER BY id")],
            "blobs": {r["id"]: r["data"].hex() for r in self.conn.execute(
                "SELECT * FROM blobs ORDER BY id")},
            "current": self.current(),
        }

    def import_json(self, data: dict):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            for table in ("objects", "events", "versions", "blobs", "current", "meta"):
                self.conn.execute(f"DELETE FROM {table}")
            for key, value in data["meta"].items():
                self.conn.execute("INSERT INTO meta(key, value) VALUES(?,?)", (key, value))
            for v in data["versions"]:
                self.conn.execute(
                    "INSERT INTO versions(id, parent_id, seq, note, actor, created_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (v["id"], v["parent_id"], v["seq"], v["note"], v["actor"],
                     v["created_at"]))
            for o in data["objects"]:
                self.conn.execute(
                    "INSERT INTO objects(oid, version_id, ord, kind, name, payload, reason)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (o["oid"], o["version_id"], o["ord"], o["kind"], o["name"],
                     o["payload"], o["reason"]))
            for e in data["events"]:
                self.conn.execute(
                    "INSERT INTO events(id, version_id, seq, type, actor, payload,"
                    " created_at) VALUES(?,?,?,?,?,?,?)",
                    (e["id"], e["version_id"], e["seq"], e["type"], e["actor"],
                     e["payload"], e["created_at"]))
            for sha, hexdata in data["blobs"].items():
                self.conn.execute("INSERT INTO blobs(id, data) VALUES(?,?)",
                                  (sha, sqlite3.Binary(bytes.fromhex(hexdata))))
            if data.get("current"):
                self.conn.execute("INSERT INTO current(id, version_id) VALUES(1, ?)",
                                  (data["current"],))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
