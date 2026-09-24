"""SQLite storage: event sourcing + atomic version pointers.

Guarantees:
* every derivation is written to an immutable version row;
* the current pointer moves only after the full version is committed;
* a crash mid-write leaves either the old version or the complete new one
  (single transaction, pointer last);
* two editors starting from the same base version conflict and must resolve.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager

SCHEMA_VERSION = 4

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  event_id TEXT PRIMARY KEY,
  seq INTEGER NOT NULL,
  version_id TEXT NOT NULL,
  base_version_id TEXT,
  kind TEXT NOT NULL,
  actor TEXT NOT NULL,
  payload TEXT NOT NULL,
  rule_version TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS versions (
  version_id TEXT PRIMARY KEY,
  parent_version_id TEXT,
  base_version_id TEXT,
  seq INTEGER NOT NULL,
  label TEXT NOT NULL,
  created_at REAL NOT NULL,
  current INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS documents (
  doc_id TEXT NOT NULL,
  version_id TEXT NOT NULL,
  filename TEXT NOT NULL,
  byte_length INTEGER NOT NULL,
  doc_json TEXT NOT NULL,
  blob BLOB,
  PRIMARY KEY (version_id, doc_id)
);
CREATE TABLE IF NOT EXISTS derivations (
  node_id TEXT NOT NULL,
  version_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  target TEXT NOT NULL,
  node_json TEXT NOT NULL,
  PRIMARY KEY (version_id, node_id)
);
CREATE TABLE IF NOT EXISTS rules (
  rule_version TEXT PRIMARY KEY,
  parent_rule_version TEXT,
  rules_json TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS affected (
  version_id TEXT PRIMARY KEY,
  report_json TEXT NOT NULL
);
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    with conn:
        conn.executescript(SCHEMA)
        cur = conn.execute("SELECT value FROM meta WHERE key='schema_version'")
        row = cur.fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO meta(key,value) VALUES('schema_version',?)",
                (str(SCHEMA_VERSION),))
        else:
            _migrate(conn, int(row[0]))


def _migrate(conn: sqlite3.Connection, from_v: int) -> None:
    # Migration path: older snapshots are upgraded in place, recording history.
    if from_v < SCHEMA_VERSION:
        conn.execute(
            "INSERT OR IGNORE INTO meta(key,value) VALUES('migration',?)",
            (json.dumps({"from": from_v, "to": SCHEMA_VERSION,
                         "at": time.time()}),))
        conn.execute(
            "UPDATE meta SET value=? WHERE key='schema_version'",
            (str(SCHEMA_VERSION),))


class ConflictError(Exception):
    pass


class Storage:
    def __init__(self, path: str):
        self.path = path
        self.conn = connect(path)
        init_db(self.conn)
        self.fault = None  # test hook: raise after writes, before commit

    # ---- reads ----------------------------------------------------------
    def current_version(self) -> str | None:
        row = self.conn.execute(
            "SELECT version_id FROM versions WHERE current=1 "
            "ORDER BY seq DESC LIMIT 1").fetchone()
        return row["version_id"] if row else None

    def get_version(self, version_id: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM versions WHERE version_id=?", (version_id,)).fetchone()
        return dict(row) if row else None

    def list_versions(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM versions ORDER BY seq")]

    def get_document(self, version_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM documents WHERE version_id=?", (version_id,)).fetchone()
        if not row:
            return None
        d = json.loads(row["doc_json"])
        return d

    def get_nodes(self, version_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT node_json FROM derivations WHERE version_id=?",
            (version_id,)).fetchall()
        return [json.loads(r["node_json"]) for r in rows]

    def get_event(self, event_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
        return dict(row) if row else None

    def list_events(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM events ORDER BY seq")]

    def get_rules(self, rule_version: str) -> dict:
        row = self.conn.execute(
            "SELECT rules_json FROM rules WHERE rule_version=?",
            (rule_version,)).fetchone()
        return json.loads(row["rules_json"]) if row else {}

    def get_affected(self, version_id: str) -> dict:
        row = self.conn.execute(
            "SELECT report_json FROM affected WHERE version_id=?",
            (version_id,)).fetchone()
        return json.loads(row["report_json"]) if row else {}

    # ---- atomic write ---------------------------------------------------
    def publish(self, *, version_id, parent_version_id, base_version_id, seq,
                label, event_id, kind, actor, payload, rule_version,
                doc, nodes, affected, blob=None) -> str:
        """Commit a complete new version then flip current pointer atomically."""
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Optimistic concurrency: base must still be the current version.
            cur = conn.execute(
                "SELECT version_id FROM versions WHERE current=1 "
                "ORDER BY seq DESC LIMIT 1").fetchone()
            current = cur["version_id"] if cur else None
            if base_version_id is not None and current is not None \
                    and base_version_id != current:
                raise ConflictError(
                    f"base {base_version_id} is stale; current is {current}")
            if conn.execute("SELECT 1 FROM versions WHERE version_id=?",
                            (version_id,)).fetchone():
                raise ConflictError(f"version {version_id} already exists")

            conn.execute(
                "INSERT INTO versions(version_id,parent_version_id,"
                "base_version_id,seq,label,created_at,current) "
                "VALUES(?,?,?,?,?,? ,0)",
                (version_id, parent_version_id, base_version_id, seq, label,
                 time.time()))
            conn.execute(
                "INSERT INTO events(event_id,seq,version_id,base_version_id,"
                "kind,actor,payload,rule_version,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (event_id, seq, version_id, base_version_id, kind, actor,
                 json.dumps(payload, default=str), rule_version, time.time()))
            if doc is not None:
                conn.execute(
                    "INSERT INTO documents(doc_id,version_id,filename,"
                    "byte_length,doc_json,blob) VALUES(?,?,?,?,?,?)",
                    (doc["file_id"], version_id, payload.get("filename",
                     "input.mkv"), doc["byte_length"],
                     json.dumps(doc, default=str), blob))
            for n in nodes:
                conn.execute(
                    "INSERT INTO derivations(node_id,version_id,kind,target,"
                    "node_json) VALUES(?,?,?,?,?)",
                    (n["id"], version_id, n["kind"], n["target"],
                     json.dumps(n, default=str)))
            conn.execute(
                "INSERT INTO affected(version_id,report_json) VALUES(?,?)",
                (version_id, json.dumps(affected, default=str)))
            conn.execute(
                "INSERT OR IGNORE INTO rules(rule_version,rules_json,created_at)"
                " VALUES(?,?,?)",
                (rule_version, json.dumps(self._ruleset(rule_version)),
                 time.time()))

            if self.fault is not None:
                # Simulate interrupted writer: abort before pointer moves.
                err = self.fault
                self.fault = None
                conn.execute("ROLLBACK")
                raise err

            conn.execute("UPDATE versions SET current=0 WHERE current=1")
            conn.execute("UPDATE versions SET current=1 WHERE version_id=?",
                         (version_id,))
            conn.execute("COMMIT")
        except ConflictError:
            if conn.execute("SELECT 1 FROM sqlite_master").fetchone():
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
            raise
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
        return version_id

    def checkout(self, version_id: str) -> None:
        """Move the current pointer to an existing complete version."""
        with self.conn:
            if not self.conn.execute(
                    "SELECT 1 FROM versions WHERE version_id=?",
                    (version_id,)).fetchone():
                raise KeyError(version_id)
            self.conn.execute("UPDATE versions SET current=0 WHERE current=1")
            self.conn.execute(
                "UPDATE versions SET current=1 WHERE version_id=?",
                (version_id,))

    def _ruleset(self, rule_version: str) -> dict:
        return {
            "signed_relative_timecode": True,
            "no_guess_lacing_boundaries": True,
            "cue_is_index_evidence": True,
            "no_cross_track_file_order_alignment": True,
            "keep_all_legal_candidates": True,
        }
