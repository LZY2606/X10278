"""SQLite 版本化存储。

- 每次推导产生独立版本，写入分两个阶段：先落 status='building' 的版本行，
  再在单个事务里写入对象/事件并把 current 指针切到 'complete' 的新版本；
  写入中断后重启，要么看到旧版本，要么看到完整新版本。
- 从同一版本出发的两份编辑产生可解决的冲突（ConflictError 携带两个头）。
- 导出/导入保持 uid、顺序与来源关系。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time

SCHEMA_VERSION = 2

SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS versions(
  id TEXT PRIMARY KEY, parent TEXT, seq INTEGER, label TEXT,
  status TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS objects(
  version_id TEXT, uid TEXT, kind TEXT, json TEXT, provenance TEXT,
  PRIMARY KEY(version_id, uid));
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, version_id TEXT, seq INTEGER,
  type TEXT, payload TEXT, created_at TEXT);
"""


class ConflictError(Exception):
    def __init__(self, current: str, base: str):
        self.current = current
        self.base = base
        super().__init__(f"版本冲突：当前指针 {current}，提交基于 {base}")


class SimulatedCrash(Exception):
    """测试用：模拟写入中断。"""


class Store:
    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self._init_or_migrate()

    def _init_or_migrate(self):
        has_meta = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
        if has_meta is None:
            self.conn.executescript(SCHEMA_V2)
            self.conn.execute("INSERT INTO meta VALUES('schema_version', ?)",
                              (str(SCHEMA_VERSION),))
            self.conn.execute("INSERT INTO meta VALUES('current_version', '')")
            self.conn.commit()
            return
        ver = int(self.conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0])
        if ver < 2:
            # v1 -> v2：objects 增加 provenance 列
            self.conn.execute("ALTER TABLE objects ADD COLUMN provenance TEXT")
            self.conn.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
            self.conn.commit()

    def close(self):
        self.conn.close()

    # ---------- 读 ----------
    def current_version(self):
        r = self.conn.execute(
            "SELECT value FROM meta WHERE key='current_version'").fetchone()
        return r[0] or None

    def list_versions(self, include_building: bool = False):
        q = "SELECT * FROM versions"
        if not include_building:
            q += " WHERE status='complete'"
        return [dict(r) for r in self.conn.execute(q + " ORDER BY seq")]

    def read_version(self, vid: str) -> dict:
        rows = self.conn.execute(
            "SELECT uid, kind, json, provenance FROM objects WHERE version_id=? ORDER BY uid",
            (vid,))
        out = {}
        for r in rows:
            obj = json.loads(r["json"])
            obj.setdefault("uid", r["uid"])
            obj.setdefault("kind", r["kind"])
            if r["provenance"]:
                obj["provenance"] = json.loads(r["provenance"])
            out[r["uid"]] = obj
        return out

    def events(self, version_id: str | None = None):
        if version_id:
            rows = self.conn.execute(
                "SELECT * FROM events WHERE version_id=? ORDER BY seq", (version_id,))
        else:
            rows = self.conn.execute("SELECT * FROM events ORDER BY id")
        return [dict(r, payload=json.loads(r["payload"])) for r in rows]

    def get_event(self, event_id: int):
        r = self.conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        return dict(r, payload=json.loads(r["payload"])) if r else None

    # ---------- 写 ----------
    def write_version(self, parent: str | None, label: str, objects: dict,
                      events: list[dict], allow_non_head: bool = False,
                      _fail_in_phase2: bool = False) -> str:
        with self.lock:
            cur = self.current_version() or ""
            parent = parent or ""
            if parent != cur and not allow_non_head:
                raise ConflictError(cur or None, parent or None)
            seq = self.conn.execute(
                "SELECT COALESCE(MAX(seq),0) FROM versions").fetchone()[0] + 1
            vid = f"v{seq}"
            now = time.strftime("%Y-%m-%dT%H:%M:%S")
            # 阶段 1：持久化 building 标记（独立事务）
            self.conn.execute(
                "INSERT INTO versions(id,parent,seq,label,status,created_at)"
                " VALUES(?,?,?,?,'building',?)", (vid, parent, seq, label, now))
            self.conn.commit()
            # 阶段 2：对象 + 事件 + 状态完成 + 指针切换，单事务原子完成
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                for uid, obj in objects.items():
                    prov = obj.get("provenance")
                    self.conn.execute(
                        "INSERT INTO objects VALUES(?,?,?,?,?)",
                        (vid, uid, obj.get("kind"),
                         json.dumps(obj, ensure_ascii=False, sort_keys=True),
                         json.dumps(prov, ensure_ascii=False) if prov else None))
                for i, ev in enumerate(events):
                    self.conn.execute(
                        "INSERT INTO events(id,version_id,seq,type,payload,created_at)"
                        " VALUES(?,?,?,?,?,?)",
                        (ev.get("id"), vid, i, ev["type"],
                         json.dumps(ev.get("payload", {}), ensure_ascii=False,
                                    sort_keys=True), now))
                if _fail_in_phase2:
                    raise SimulatedCrash("阶段 2 写入中断")
                self.conn.execute(
                    "UPDATE versions SET status='complete' WHERE id=?", (vid,))
                r = self.conn.execute(
                    "UPDATE meta SET value=? WHERE key='current_version' AND value=?",
                    (vid, cur))
                if r.rowcount != 1:
                    raise ConflictError(cur or None, parent or None)
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
            return vid

    # ---------- 导出 / 导入 ----------
    def export_json(self) -> dict:
        versions = self.list_versions()
        return {
            "schema_version": SCHEMA_VERSION,
            "current": self.current_version(),
            "versions": versions,
            "objects": {v["id"]: self.read_version(v["id"]) for v in versions},
            "events": self.events(),
        }

    def import_json(self, dump: dict):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                for t in ("objects", "events", "versions"):
                    self.conn.execute(f"DELETE FROM {t}")
                for v in dump["versions"]:
                    self.conn.execute(
                        "INSERT INTO versions(id,parent,seq,label,status,created_at)"
                        " VALUES(?,?,?,?,?,?)",
                        (v["id"], v["parent"], v["seq"], v["label"], v["status"],
                         v["created_at"]))
                for vid, objects in dump["objects"].items():
                    for uid, obj in objects.items():
                        prov = obj.get("provenance")
                        self.conn.execute(
                            "INSERT INTO objects VALUES(?,?,?,?,?)",
                            (vid, uid, obj.get("kind"),
                             json.dumps(obj, ensure_ascii=False, sort_keys=True),
                             json.dumps(prov, ensure_ascii=False) if prov else None))
                for ev in dump["events"]:
                    self.conn.execute(
                        "INSERT INTO events(id,version_id,seq,type,payload,created_at)"
                        " VALUES(?,?,?,?,?,?)",
                        (ev["id"], ev["version_id"], ev["seq"], ev["type"],
                         json.dumps(ev["payload"], ensure_ascii=False, sort_keys=True),
                         ev["created_at"]))
                self.conn.execute(
                    "UPDATE meta SET value=? WHERE key='current_version'",
                    (dump["current"],))
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def reset_file(self):
        """删除本地数据库内容（保留 schema），用于导出后重导入演示。"""
        with self.lock:
            for t in ("objects", "events", "versions"):
                self.conn.execute(f"DELETE FROM {t}")
            self.conn.execute("UPDATE meta SET value='' WHERE key='current_version'")
            self.conn.commit()
