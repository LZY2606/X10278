"""SQLite 版本化存储。

- 每次推导产生独立 version；只有完整写入成功后才切换 current 指针（单事务）。
- 写入中断 => 事务回滚 => 重启后看到旧版本（SQLite rollback journal 保证）。
- 所有领域记录带稳定身份；导出/导入为字节级往返，身份不变。
- schema 迁移通过 user_version 管理。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .ids import canon

SCHEMA_VERSION = 2

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY, version_no INTEGER, seq INTEGER, kind TEXT,
  subject TEXT, detail TEXT, actor TEXT, prev_event_id TEXT);
CREATE TABLE IF NOT EXISTS sources (
  id TEXT PRIMARY KEY, logical_id TEXT, sha256 TEXT, bytes BLOB,
  label TEXT, created_event TEXT);
CREATE TABLE IF NOT EXISTS versions (
  id TEXT PRIMARY KEY, no INTEGER UNIQUE, parent_id TEXT, parent_no INTEGER,
  event_ids TEXT, created_by TEXT);
CREATE TABLE IF NOT EXISTS records (
  id TEXT PRIMARY KEY, version_no INTEGER, kind TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS results (
  version_no INTEGER, node_key TEXT, value TEXT, deps TEXT,
  PRIMARY KEY (version_no, node_key));
CREATE TABLE IF NOT EXISTS edges (
  parent TEXT, child TEXT, PRIMARY KEY (parent, child));
CREATE TABLE IF NOT EXISTS current (version_no INTEGER);
"""

_MIGRATIONS = {
    2: ["ALTER TABLE versions ADD COLUMN note TEXT"],
}


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self):
        cur = self.conn.execute("PRAGMA user_version")
        version = cur.fetchone()[0]
        if version == 0:
            self.conn.executescript(_SCHEMA_V1)
            version = 1
        while version < SCHEMA_VERSION:
            version += 1
            for stmt in _MIGRATIONS[version]:
                self.conn.execute(stmt)
            self.conn.execute(f"PRAGMA user_version={version}")
        self.conn.commit()

    def close(self):
        self.conn.close()

    @contextmanager
    def tx(self):
        """单事务：任何异常 => 回滚 => current 指针不变。"""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise

    # ---------- 写入 ----------
    def write_version(self, *, version: dict, events: list[dict],
                      records: list[dict], results: list[dict],
                      edges: list[tuple[str, str]], sources: list[dict],
                      set_current: bool = True):
        """原子写入一个完整版本并切换 current。"""
        with self.tx() as conn:
            for s in sources:
                conn.execute(
                    "INSERT OR IGNORE INTO sources VALUES (?,?,?,?,?,?)",
                    (s["id"], s["logical_id"], s["sha256"], s["bytes"],
                     s["label"], s["created_event"]))
            for e in events:
                conn.execute(
                    "INSERT INTO events VALUES (?,?,?,?,?,?,?,?)",
                    (e["id"], e["version_no"], e["seq"], e["kind"],
                     e["subject"], canon(e.get("detail", {})), e.get("actor"),
                     e.get("prev_event_id")))
            conn.execute(
                "INSERT INTO versions VALUES (?,?,?,?,?,?,?)",
                (version["id"], version["no"], version.get("parent_id"),
                 version.get("parent_no"), canon(version.get("event_ids", [])),
                 version.get("created_by"), version.get("note")))
            for r in records:
                conn.execute("INSERT OR REPLACE INTO records VALUES (?,?,?,?)",
                             (r["id"], version["no"], r["kind"], canon(r["data"])))
            for res in results:
                conn.execute("INSERT OR REPLACE INTO results VALUES (?,?,?,?)",
                             (version["no"], res["node_key"],
                              canon(res["value"]), canon(res.get("deps", []))))
            for parent, child in edges:
                conn.execute("INSERT OR IGNORE INTO edges VALUES (?,?)",
                             (parent, child))
            if set_current:
                conn.execute("DELETE FROM current")
                conn.execute("INSERT INTO current VALUES (?)", (version["no"],))

    # ---------- 读取 ----------
    def current_version_no(self) -> int | None:
        row = self.conn.execute("SELECT version_no FROM current").fetchone()
        return row[0] if row else None

    def get_version(self, no: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM versions WHERE no=?", (no,)).fetchone()
        if not row:
            return None
        return {"id": row["id"], "no": row["no"], "parent_id": row["parent_id"],
                "parent_no": row["parent_no"],
                "event_ids": json.loads(row["event_ids"]),
                "created_by": row["created_by"], "note": row["note"]}

    def list_versions(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM versions ORDER BY no").fetchall()
        return [{"id": r["id"], "no": r["no"], "parent_no": r["parent_no"],
                 "note": r["note"], "created_by": r["created_by"]} for r in rows]

    def events_of(self, version_no: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM events WHERE version_no=? ORDER BY seq",
            (version_no,)).fetchall()
        return [dict(r) | {"detail": json.loads(r["detail"])} for r in rows]

    def all_events(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM events ORDER BY version_no, seq").fetchall()
        return [dict(r) | {"detail": json.loads(r["detail"])} for r in rows]

    def records_of(self, version_no: int, kind: str | None = None) -> list[dict]:
        if kind:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE version_no=? AND kind=?",
                (version_no, kind)).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE version_no=?", (version_no,)).fetchall()
        return [{"id": r["id"], "kind": r["kind"], "data": json.loads(r["data"])}
                for r in rows]

    def results_of(self, version_no: int) -> dict[str, dict]:
        rows = self.conn.execute(
            "SELECT * FROM results WHERE version_no=?", (version_no,)).fetchall()
        return {r["node_key"]: {"value": json.loads(r["value"]),
                                "deps": json.loads(r["deps"])} for r in rows}

    def edges(self) -> list[list[str]]:
        rows = self.conn.execute("SELECT * FROM edges").fetchall()
        return [[r["parent"], r["child"]] for r in rows]

    def get_source(self, source_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        if not row:
            return None
        return {"id": row["id"], "logical_id": row["logical_id"],
                "sha256": row["sha256"], "bytes": row["bytes"],
                "label": row["label"], "created_event": row["created_event"]}

    def list_sources(self) -> list[dict]:
        rows = self.conn.execute("SELECT id, logical_id, sha256, label FROM sources").fetchall()
        return [dict(r) for r in rows]

    # ---------- 导出 / 导入 ----------
    def export_bundle(self) -> dict:
        sources = []
        for row in self.conn.execute("SELECT * FROM sources").fetchall():
            sources.append({"id": row["id"], "logical_id": row["logical_id"],
                            "sha256": row["sha256"], "bytes_hex": bytes(row["bytes"]).hex(),
                            "label": row["label"], "created_event": row["created_event"]})
        return {
            "format": "mkv-timecourt-bundle",
            "schema_version": SCHEMA_VERSION,
            "current": self.current_version_no(),
            "versions": self.list_versions_full(),
            "events": self.all_events(),
            "records": [dict(id=r["id"], version_no=vn, kind=r["kind"], data=r["data"])
                        for vn in self._version_nos() for r in self.records_of(vn)],
            "results": [{"version_no": vn, "node_key": k, **v}
                        for vn in self._version_nos()
                        for k, v in self.results_of(vn).items()],
            "edges": self.edges(),
            "sources": sources,
        }

    def _version_nos(self) -> list[int]:
        return [r[0] for r in self.conn.execute("SELECT no FROM versions ORDER BY no")]

    def list_versions_full(self) -> list[dict]:
        return [self.get_version(n) for n in self._version_nos()]

    def import_bundle(self, bundle: dict, *, replace: bool = False):
        if bundle.get("format") != "mkv-timecourt-bundle":
            raise ValueError("不是 mkv-timecourt 档案包")
        if self._version_nos() and not replace:
            raise ValueError("数据库非空：导入需要空库或 replace=True")
        with self.tx() as conn:
            if replace:
                for t in ("events", "sources", "versions", "records", "results",
                          "edges", "current"):
                    conn.execute(f"DELETE FROM {t}")
            for s in bundle["sources"]:
                conn.execute("INSERT INTO sources VALUES (?,?,?,?,?,?)",
                             (s["id"], s["logical_id"], s["sha256"],
                              bytes.fromhex(s["bytes_hex"]), s["label"],
                              s["created_event"]))
            for e in bundle["events"]:
                conn.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?)",
                             (e["id"], e["version_no"], e["seq"], e["kind"],
                              e["subject"], canon(e["detail"]), e.get("actor"),
                              e.get("prev_event_id")))
            for v in bundle["versions"]:
                conn.execute("INSERT INTO versions VALUES (?,?,?,?,?,?,?)",
                             (v["id"], v["no"], v.get("parent_id"),
                              v.get("parent_no"), canon(v.get("event_ids", [])),
                              v.get("created_by"), v.get("note")))
            for r in bundle["records"]:
                conn.execute("INSERT INTO records VALUES (?,?,?,?)",
                             (r["id"], r["version_no"], r["kind"], canon(r["data"])))
            for res in bundle["results"]:
                conn.execute("INSERT INTO results VALUES (?,?,?,?)",
                             (res["version_no"], res["node_key"],
                              canon(res["value"]), canon(res.get("deps", []))))
            for parent, child in bundle["edges"]:
                conn.execute("INSERT INTO edges VALUES (?,?)", (parent, child))
            if bundle.get("current") is not None:
                conn.execute("INSERT INTO current VALUES (?)", (bundle["current"],))
