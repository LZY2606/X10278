"""SQLite-backed versioned store.

Guarantees:
- Every derivation produces an independent immutable version row.
- The current pointer is switched only inside the same transaction that marks
  the version complete; after an interrupted write a restart sees either the
  old version or a complete new one (incomplete rows are rolled back, and any
  torn 'building' rows are cleaned on open).
- Two users editing from the same base version produce a resolvable conflict
  (optimistic check on base_version_id); both branches are kept.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

SCHEMA_VERSION = 2

DDL_V2 = [
    "CREATE TABLE IF NOT EXISTS schema_meta(version INTEGER)",
    """CREATE TABLE IF NOT EXISTS versions(
        id INTEGER PRIMARY KEY,
        parent_id INTEGER,
        seq INTEGER,
        label TEXT,
        status TEXT,
        payload_json TEXT,
        created_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS current_version(
        id INTEGER PRIMARY KEY CHECK(id=1), version_id INTEGER)""",
    """CREATE TABLE IF NOT EXISTS events(
        id INTEGER PRIMARY KEY,
        version_id INTEGER,
        base_version_id INTEGER,
        seq INTEGER,
        kind TEXT,
        payload_json TEXT,
        created_at TEXT)""",
    "CREATE TABLE IF NOT EXISTS sources(sha256 TEXT PRIMARY KEY, data BLOB)",
]


class ConflictError(Exception):
    def __init__(self, current_version):
        super().__init__(
            f"base version is stale; current version is {current_version}")
        self.current_version = current_version


class SimulatedCrash(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: str):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self._migrate()
        self._recover()

    def close(self):
        self.conn.close()

    # ---- schema ----

    def _migrate(self):
        cur = self.conn
        cur.execute(
            "CREATE TABLE IF NOT EXISTS schema_meta(version INTEGER)")
        row = cur.execute("SELECT version FROM schema_meta").fetchone()
        if row is None:
            for stmt in DDL_V2:
                cur.execute(stmt)
            cur.execute("INSERT INTO schema_meta(version) VALUES(?)",
                        (SCHEMA_VERSION,))
            cur.execute(
                "INSERT OR IGNORE INTO current_version(id, version_id)"
                " VALUES(1, NULL)")
            self.conn.commit()
            return
        version = row[0]
        if version < 2:
            # v1 -> v2: versions.seq and events.base_version_id were added.
            cols = [r[1] for r in cur.execute("PRAGMA table_info(versions)")]
            if "seq" not in cols:
                cur.execute("ALTER TABLE versions ADD COLUMN seq INTEGER")
            cols = [r[1] for r in cur.execute("PRAGMA table_info(events)")]
            if "base_version_id" not in cols:
                cur.execute(
                    "ALTER TABLE events ADD COLUMN base_version_id INTEGER")
            cur.execute("UPDATE schema_meta SET version=?", (SCHEMA_VERSION,))
        for stmt in DDL_V2:
            cur.execute(stmt)
        cur.execute(
            "INSERT OR IGNORE INTO current_version(id, version_id)"
            " VALUES(1, NULL)")
        self.conn.commit()

    def _recover(self):
        self.conn.execute("DELETE FROM versions WHERE status != 'complete'")
        self.conn.commit()

    # ---- sources ----

    def put_source(self, data: bytes, sha256: str) -> str:
        self.conn.execute(
            "INSERT OR IGNORE INTO sources(sha256, data) VALUES(?, ?)",
            (sha256, data))
        self.conn.commit()
        return sha256

    def get_source(self, sha256: str) -> bytes:
        row = self.conn.execute(
            "SELECT data FROM sources WHERE sha256=?", (sha256,)).fetchone()
        if row is None:
            raise KeyError(f"unknown source {sha256}")
        return bytes(row[0])

    # ---- versions ----

    def current_version_id(self):
        row = self.conn.execute(
            "SELECT version_id FROM current_version WHERE id=1").fetchone()
        return row[0] if row else None

    def set_current(self, version_id):
        self.conn.execute(
            "UPDATE current_version SET version_id=? WHERE id=1",
            (version_id,))
        self.conn.commit()

    def get_version(self, version_id) -> dict:
        row = self.conn.execute(
            "SELECT * FROM versions WHERE id=?", (version_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown version {version_id}")
        return {
            "id": row["id"],
            "parent_id": row["parent_id"],
            "seq": row["seq"],
            "label": row["label"],
            "status": row["status"],
            "created_at": row["created_at"],
            "payload": json.loads(row["payload_json"]),
        }

    def list_versions(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, parent_id, seq, label, status, created_at"
            " FROM versions ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def next_event_seq(self) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS s FROM events").fetchone()
        return row["s"]

    def commit_version(self, parent_id, label, payload, event,
                       crash_mid=False) -> int:
        """Insert version + event + pointer switch atomically."""
        conn = self.conn
        seq = event["seq"]
        try:
            conn.execute("BEGIN")
            cur = conn.execute(
                "INSERT INTO versions(parent_id, seq, label, status,"
                " payload_json, created_at) VALUES(?,?,?,?,?,?)",
                (parent_id, seq, label, "building",
                 json.dumps(payload), _now()))
            version_id = cur.lastrowid
            if crash_mid:
                raise SimulatedCrash("write interrupted mid-transaction")
            conn.execute("UPDATE versions SET status='complete' WHERE id=?",
                         (version_id,))
            conn.execute(
                "INSERT INTO events(version_id, base_version_id, seq, kind,"
                " payload_json, created_at) VALUES(?,?,?,?,?,?)",
                (version_id, event.get("base_version_id"), seq,
                 event["kind"], json.dumps(event["payload"]), _now()))
            conn.execute(
                "UPDATE current_version SET version_id=? WHERE id=1",
                (version_id,))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return version_id

    def add_pointer_event(self, kind, payload, version_id) -> int:
        seq = self.next_event_seq()
        self.conn.execute(
            "INSERT INTO events(version_id, base_version_id, seq, kind,"
            " payload_json, created_at) VALUES(?,?,?,?,?,?)",
            (version_id, None, seq, kind, json.dumps(payload), _now()))
        self.conn.commit()
        return seq

    # ---- events ----

    def list_events(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM events ORDER BY seq").fetchall()
        return [{
            "id": r["id"],
            "version_id": r["version_id"],
            "base_version_id": r["base_version_id"],
            "seq": r["seq"],
            "kind": r["kind"],
            "payload": json.loads(r["payload_json"]),
            "created_at": r["created_at"],
        } for r in rows]

    def get_event(self, event_id) -> dict:
        row = self.conn.execute(
            "SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown event {event_id}")
        return {
            "id": row["id"],
            "version_id": row["version_id"],
            "base_version_id": row["base_version_id"],
            "seq": row["seq"],
            "kind": row["kind"],
            "payload": json.loads(row["payload_json"]),
            "created_at": row["created_at"],
        }

    # ---- bulk ----

    def reset(self):
        for table in ("events", "versions", "sources"):
            self.conn.execute(f"DELETE FROM {table}")
        self.conn.execute(
            "UPDATE current_version SET version_id=NULL WHERE id=1")
        self.conn.commit()
