"""版本化存储层(SQLite)。

保证:
- 每次推导产生独立版本;只有完整成功后才切换 current 指针(单事务)。
- 写入中断后重启:要么看到旧版本,要么看到完整新版本(recover 清理 building 残留)。
- 两人从同一版本编辑:后保存者得到可解决的冲突,未选候选永久保留。
- 导出/删除/再导入:身份、顺序、来源关系不变。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

from .derive import compute_nodes

SCHEMA_VERSION = 3

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS versions (
    id TEXT PRIMARY KEY, parent_id TEXT, kind TEXT, label TEXT,
    created_at TEXT, state TEXT);
CREATE TABLE IF NOT EXISTS current (id INTEGER PRIMARY KEY CHECK (id = 1), version_id TEXT);
CREATE TABLE IF NOT EXISTS nodes (
    version_id TEXT, key TEXT, value_json TEXT, fingerprint TEXT, status TEXT,
    PRIMARY KEY (version_id, key));
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY, version_id TEXT, seq INTEGER, type TEXT,
    payload_json TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS conflicts (
    id TEXT PRIMARY KEY, base_version TEXT, version_a TEXT, version_b TEXT,
    status TEXT, resolution TEXT);
"""

MIGRATIONS = {
    2: [
        "ALTER TABLE nodes ADD COLUMN reason TEXT",
        "ALTER TABLE events ADD COLUMN actor TEXT",
        "ALTER TABLE versions ADD COLUMN actor TEXT",
    ],
    3: [
        "CREATE TABLE IF NOT EXISTS sources (version_id TEXT PRIMARY KEY, data BLOB)",
    ],
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class ConflictError(Exception):
    def __init__(self, conflict_id: str, version_id: str):
        self.conflict_id = conflict_id
        self.version_id = version_id
        super().__init__(f"与当前版本冲突,已登记 {conflict_id}")


class Store:
    def __init__(self, path: str = ":memory:"):
        self.path = path
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.lock = threading.RLock()
        self._migrate()
        self.recover()

    def close(self):
        self.db.close()

    # --- schema / 迁移 ---
    def _migrate(self):
        with self.lock, self.db:
            row = self.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='meta'"
            ).fetchone()
            if not row:
                self.db.executescript(SCHEMA_V1)
                self.db.execute(
                    "INSERT INTO meta(key, value) VALUES('schema_version', '1')")
            version = int(self.db.execute(
                "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0])
            while version < SCHEMA_VERSION:
                version += 1
                for stmt in MIGRATIONS[version]:
                    try:
                        self.db.execute(stmt)
                    except sqlite3.OperationalError:
                        pass  # 列已存在
                self.db.execute(
                    "UPDATE meta SET value=? WHERE key='schema_version'",
                    (str(version),))

    def recover(self):
        """启动恢复:清理 building 状态的残留版本(写入中断)。"""
        with self.lock, self.db:
            stale = [r[0] for r in self.db.execute(
                "SELECT id FROM versions WHERE state='building'").fetchall()]
            for vid in stale:
                self.db.execute("DELETE FROM nodes WHERE version_id=?", (vid,))
                self.db.execute("DELETE FROM events WHERE version_id=?", (vid,))
                self.db.execute("DELETE FROM sources WHERE version_id=?", (vid,))
                self.db.execute("DELETE FROM versions WHERE id=?", (vid,))

    # --- 查询 ---
    def current_version(self) -> str | None:
        row = self.db.execute("SELECT version_id FROM current WHERE id=1").fetchone()
        return row[0] if row else None

    def get_source(self, vid: str) -> bytes | None:
        row = self.db.execute(
            "SELECT data FROM sources WHERE version_id=?", (vid,)).fetchone()
        return bytes(row[0]) if row else None

    def get_version(self, vid: str) -> dict | None:
        row = self.db.execute("SELECT * FROM versions WHERE id=?", (vid,)).fetchone()
        return dict(row) if row else None

    def list_versions(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM versions WHERE state='complete' ORDER BY created_at, id"
        ).fetchall()
        current = self.current_version()
        out = []
        for r in rows:
            d = dict(r)
            d["is_current"] = d["id"] == current
            out.append(d)
        return out

    def get_nodes(self, vid: str) -> dict[str, dict]:
        rows = self.db.execute(
            "SELECT * FROM nodes WHERE version_id=? ORDER BY key", (vid,)).fetchall()
        return {r["key"]: {
            "key": r["key"],
            "value": json.loads(r["value_json"]),
            "fingerprint": r["fingerprint"],
            "status": r["status"],
            "reason": r["reason"],
        } for r in rows}

    def get_events(self, vid: str | None = None) -> list[dict]:
        sql = "SELECT * FROM events"
        args: tuple = ()
        if vid:
            sql += " WHERE version_id=?"
            args = (vid,)
        sql += " ORDER BY created_at, seq"
        return [dict(r) | {"payload": json.loads(r["payload_json"])}
                for r in self.db.execute(sql, args).fetchall()]

    def list_conflicts(self) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM conflicts ORDER BY id").fetchall()]

    # --- 写入 ---
    def save_version(self, parent_id: str | None, kind: str, label: str,
                     actor: str, data: bytes, params: dict,
                     events: list[dict] | None = None,
                     base_version: str | None = None,
                     _crash_after_insert: bool = False) -> dict:
        """保存新版本。base_version 与当前版本不一致时登记冲突(可解决)。"""
        with self.lock:
            current = self.current_version()
            if base_version is not None and current is not None \
                    and base_version != current:
                # 冲突路径:版本仍完整保存为候选,但不切换 current
                vid = self._write_version(parent_id, kind, label + " [未选候选]",
                                          actor, data, params, events,
                                          make_current=False)
                cid = _uid("cft")
                with self.db:
                    self.db.execute(
                        "INSERT INTO conflicts VALUES(?,?,?,?,?,?)",
                        (cid, base_version, current, vid, "open", None))
                raise ConflictError(cid, vid)
            vid = self._write_version(parent_id, kind, label, actor, data, params,
                                      events, _crash_after_insert)
            return {"version_id": vid, "status": "committed"}

    def _write_version(self, parent_id, kind, label, actor, data, params,
                       events, _crash_after_insert=False,
                       make_current=True) -> str:
        new_nodes = compute_nodes(data, params)
        prev_nodes = self.get_nodes(parent_id) if parent_id else {}
        vid = _uid("ver")
        now = _now()
        try:
            with self.db:  # 单事务:要么完整新版本,要么回滚到旧版本
                self.db.execute(
                    "INSERT INTO versions VALUES(?,?,?,?,?,?,?)",
                    (vid, parent_id, kind, label, now, "building", actor))
                self.db.execute("INSERT INTO sources VALUES(?,?)", (vid, data))
                for key, node in new_nodes.items():
                    prev = prev_nodes.get(key)
                    if prev and prev["fingerprint"] == node["fingerprint"]:
                        status, reason = "reused", "输入未变,结果仍有效(值指纹一致)"
                    else:
                        changed = [k for k in node["inputs"]
                                   if k not in prev_nodes
                                   or prev_nodes[k]["fingerprint"]
                                   != new_nodes[k]["fingerprint"]]
                        status = "computed"
                        reason = ("重算:输入变化 " + ",".join(changed)) if changed \
                            else "重算:新节点或配方变化"
                    self.db.execute(
                        "INSERT INTO nodes VALUES(?,?,?,?,?,?)",
                        (vid, key, json.dumps(node["value"], ensure_ascii=False),
                         node["fingerprint"], status, reason))
                for seq, ev in enumerate(events or []):
                    self.db.execute(
                        "INSERT INTO events VALUES(?,?,?,?,?,?,?)",
                        (_uid("evt"), vid, seq, ev.get("type", "note"),
                         json.dumps(ev.get("payload", ev), ensure_ascii=False),
                         now, actor))
                if _crash_after_insert:
                    raise RuntimeError("simulated crash mid-write")
                self.db.execute("UPDATE versions SET state='complete' WHERE id=?",
                                (vid,))
                if make_current:
                    self.db.execute(
                        "INSERT INTO current(id, version_id) VALUES(1,?) "
                        "ON CONFLICT(id) DO UPDATE SET version_id=excluded.version_id",
                        (vid,))
        except RuntimeError:
            raise
        return vid

    def _prev_fp(self, vid: str, key: str) -> str | None:
        row = self.db.execute(
            "SELECT fingerprint FROM nodes WHERE version_id=? AND key=?",
            (vid, key)).fetchone()
        return row[0] if row else None

    def resolve_conflict(self, conflict_id: str, choose: str, actor: str) -> dict:
        """choose: 'a'(保持当前) 或 'b'(切换到候选)。未选一方永久保留。"""
        with self.lock, self.db:
            row = self.db.execute(
                "SELECT * FROM conflicts WHERE id=?", (conflict_id,)).fetchone()
            if not row:
                raise KeyError(conflict_id)
            if row["status"] != "open":
                raise ValueError("冲突已解决")
            chosen = row["version_b"] if choose == "b" else row["version_a"]
            if choose == "b":
                self.db.execute(
                    "UPDATE current SET version_id=? WHERE id=1", (chosen,))
            self.db.execute(
                "UPDATE conflicts SET status='resolved', resolution=? WHERE id=?",
                (f"chosen={chosen};by={actor};loser_kept", conflict_id))
            return {"chosen": chosen, "conflict_id": conflict_id}

    # --- 导出 / 导入 ---
    def export_case(self) -> dict:
        """确定性导出:身份、顺序、来源关系完整保留。"""
        tables = {}
        for table, order in (("versions", "created_at, id"),
                             ("nodes", "version_id, key"),
                             ("events", "created_at, seq, id"),
                             ("conflicts", "id"),
                             ("sources", "version_id")):
            rows = self.db.execute(f"SELECT * FROM {table} ORDER BY {order}").fetchall()
            tables[table] = [dict(r) for r in rows]
        for row in tables["sources"]:
            row["data"] = row["data"].hex()
        return {
            "format": "matroska-timecode-court/1",
            "schema_version": SCHEMA_VERSION,
            "current": self.current_version(),
            "tables": tables,
        }

    def import_case(self, payload: dict):
        if payload.get("format") != "matroska-timecode-court/1":
            raise ValueError("未知导出格式")
        with self.lock, self.db:
            for table in ("nodes", "events", "conflicts", "versions", "current",
                          "sources"):
                self.db.execute(f"DELETE FROM {table}")
            for v in payload["tables"]["versions"]:
                self.db.execute(
                    "INSERT INTO versions VALUES(?,?,?,?,?,?,?)",
                    (v["id"], v["parent_id"], v["kind"], v["label"],
                     v["created_at"], v["state"], v.get("actor")))
            for n in payload["tables"]["nodes"]:
                self.db.execute(
                    "INSERT INTO nodes VALUES(?,?,?,?,?,?)",
                    (n["version_id"], n["key"], n["value_json"],
                     n["fingerprint"], n["status"], n.get("reason")))
            for e in payload["tables"]["events"]:
                self.db.execute(
                    "INSERT INTO events VALUES(?,?,?,?,?,?,?)",
                    (e["id"], e["version_id"], e["seq"], e["type"],
                     e["payload_json"], e["created_at"], e.get("actor")))
            for c in payload["tables"]["conflicts"]:
                self.db.execute(
                    "INSERT INTO conflicts VALUES(?,?,?,?,?,?)",
                    (c["id"], c["base_version"], c["version_a"],
                     c["version_b"], c["status"], c["resolution"]))
            for src in payload["tables"].get("sources", []):
                self.db.execute("INSERT INTO sources VALUES(?,?)",
                                (src["version_id"], bytes.fromhex(src["data"])))
            if payload.get("current"):
                self.db.execute(
                    "INSERT INTO current(id, version_id) VALUES(1,?)",
                    (payload["current"],))
