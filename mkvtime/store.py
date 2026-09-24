"""SQLite-backed versioned store.

Guarantees:
  * every derivation is an independent, immutable version;
  * the current pointer switches only after a version is fully written
    (single transaction) -- a crash mid-write leaves either the old
    version or the complete new one;
  * two users editing the same base version get a resolvable conflict;
  * events are machine-readable and carry stable identities;
  * export/import round-trips preserve identity, order and provenance.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid

SCHEMA_VERSION = 2

SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS versions (
    id TEXT PRIMARY KEY,
    seq INTEGER NOT NULL,
    parent_id TEXT,
    base_id TEXT,          -- base the editor started from (for conflicts)
    status TEXT NOT NULL,  -- building | complete | aborted
    message TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS artifacts (
    version_id TEXT NOT NULL,
    key TEXT NOT NULL,
    json TEXT NOT NULL,
    PRIMARY KEY (version_id, key)
);
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    seq INTEGER NOT NULL,
    version_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS pointer (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version_id TEXT NOT NULL
);
"""


class Conflict(Exception):
    def __init__(self, base_id: str, current_id: str):
        self.base_id = base_id
        self.current_id = current_id
        super().__init__(f"base {base_id} is not current {current_id}")


class Store:
    def __init__(self, path: str):
        self.path = path
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self._migrate()

    def close(self):
        self.db.close()

    # -- schema / migration -------------------------------------------------
    def _meta(self, key, default=None):
        try:
            row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        except sqlite3.OperationalError:
            return default
        return row["value"] if row else default

    def _migrate(self):
        version = int(self._meta("schema_version", "0") or "0")
        if version == 0:
            self.db.executescript(SCHEMA_V2)
            self._set_meta("schema_version", str(SCHEMA_VERSION))
            self.db.commit()
        elif version == 1:
            # v1 -> v2: versions gained base_id; events gained seq
            self.db.execute("ALTER TABLE versions ADD COLUMN base_id TEXT")
            self.db.execute("ALTER TABLE events ADD COLUMN seq INTEGER")
            rows = self.db.execute(
                "SELECT id FROM events ORDER BY created_at, id").fetchall()
            for i, r in enumerate(rows):
                self.db.execute("UPDATE events SET seq=? WHERE id=?", (i, r["id"]))
            self._set_meta("schema_version", str(SCHEMA_VERSION))
            self.db.commit()

    def _set_meta(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                        (key, value))

    # -- versions ------------------------------------------------------------
    def current(self) -> str | None:
        row = self.db.execute("SELECT version_id FROM pointer WHERE id=1").fetchone()
        return row["version_id"] if row else None

    def _next_seq(self, table="versions") -> int:
        row = self.db.execute(f"SELECT COALESCE(MAX(seq),-1)+1 AS s FROM {table}").fetchone()
        return row["s"]

    def begin_version(self, parent_id: str | None, message: str,
                      base_id: str | None = None) -> str:
        vid = f"ver:{uuid.uuid4().hex[:12]}"
        self.db.execute(
            "INSERT INTO versions(id,seq,parent_id,base_id,status,message,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (vid, self._next_seq(), parent_id, base_id, "building", message,
             time.time()))
        return vid

    def put_artifact(self, version_id: str, key: str, obj):
        self.db.execute(
            "INSERT OR REPLACE INTO artifacts(version_id,key,json) VALUES(?,?,?)",
            (version_id, key, json.dumps(obj, sort_keys=True)))

    def add_event(self, version_id: str, kind: str, payload: dict,
                  event_id: str | None = None) -> str:
        eid = event_id or f"ev:{uuid.uuid4().hex[:12]}"
        self.db.execute(
            "INSERT INTO events(id,seq,version_id,kind,payload,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (eid, self._next_seq("events"), version_id, kind,
             json.dumps(payload, sort_keys=True), time.time()))
        return eid

    def commit(self, version_id: str, fail_before_pointer: bool = False):
        """Atomically mark the version complete and move the pointer.
        `fail_before_pointer` exists to simulate a crash between the two
        steps for tests: the transaction rolls back and the old current
        version remains visible."""
        try:
            with self.db:
                self.db.execute("UPDATE versions SET status='complete' WHERE id=?",
                                (version_id,))
                if fail_before_pointer:
                    raise RuntimeError("simulated crash before pointer switch")
                self.db.execute(
                    "INSERT OR REPLACE INTO pointer(id,version_id) VALUES(1,?)",
                    (version_id,))
        except RuntimeError:
            self.db.execute("UPDATE versions SET status='aborted' WHERE id=?",
                            (version_id,))
            self.db.commit()
            raise

    def abort(self, version_id: str):
        self.db.execute("UPDATE versions SET status='aborted' WHERE id=?",
                        (version_id,))
        self.db.commit()

    # -- high level save with conflict detection ------------------------------
    def save_version(self, base_id: str | None, message: str,
                     artifacts: dict, events: list[tuple[str, dict]],
                     allow_behind: bool = False) -> str:
        current = self.current()
        if not allow_behind and current is not None and base_id != current:
            raise Conflict(base_id, current)
        vid = self.begin_version(current if base_id is None else base_id,
                                 message, base_id=base_id)
        # parent chain follows the base the author started from
        self.db.execute("UPDATE versions SET parent_id=? WHERE id=?",
                        (base_id, vid))
        for key, obj in artifacts.items():
            self.put_artifact(vid, key, obj)
        for kind, payload in events:
            self.add_event(vid, kind, payload)
        self.commit(vid)
        return vid

    def resolve_conflict(self, losing_base: str, message: str,
                         artifacts: dict, events: list[tuple[str, dict]]) -> str:
        """Create a resolution version on top of the current pointer while
        recording the losing base in the events (nothing is overwritten)."""
        current = self.current()
        vid = self.begin_version(current, message, base_id=losing_base)
        for key, obj in artifacts.items():
            self.put_artifact(vid, key, obj)
        self.add_event(vid, "conflict_resolution",
                       {"losing_base": losing_base, "winning_parent": current,
                        "note": "both branches kept; reviewer chose this one"})
        for kind, payload in events:
            self.add_event(vid, kind, payload)
        self.commit(vid)
        return vid

    # -- reads -----------------------------------------------------------------
    def get_version(self, vid: str) -> dict | None:
        row = self.db.execute("SELECT * FROM versions WHERE id=?", (vid,)).fetchone()
        return dict(row) if row else None

    def list_versions(self) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM versions WHERE status='complete' ORDER BY seq")]

    def get_artifact(self, vid: str, key: str):
        row = self.db.execute(
            "SELECT json FROM artifacts WHERE version_id=? AND key=?",
            (vid, key)).fetchone()
        return json.loads(row["json"]) if row else None

    def artifact_keys(self, vid: str) -> list[str]:
        return [r["key"] for r in self.db.execute(
            "SELECT key FROM artifacts WHERE version_id=? ORDER BY key", (vid,))]

    def list_events(self, version_id: str | None = None) -> list[dict]:
        if version_id:
            rows = self.db.execute(
                "SELECT * FROM events WHERE version_id=? ORDER BY seq",
                (version_id,)).fetchall()
        else:
            rows = self.db.execute("SELECT * FROM events ORDER BY seq").fetchall()
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    def get_event(self, eid: str) -> dict | None:
        row = self.db.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
        return {**dict(row), "payload": json.loads(row["payload"])} if row else None

    def version_at_event(self, eid: str) -> str | None:
        """The version whose conclusions were visible when `eid` was the
        latest event: its own version if it is complete, else the nearest
        preceding complete version."""
        ev = self.get_event(eid)
        if ev is None:
            return None
        v = self.get_version(ev["version_id"])
        if v and v["status"] == "complete":
            return v["id"]
        row = self.db.execute(
            "SELECT id FROM versions WHERE status='complete' AND seq<=("
            " SELECT seq FROM versions WHERE id=?) ORDER BY seq DESC LIMIT 1",
            (ev["version_id"],)).fetchone()
        return row["id"] if row else None

    # -- export / import ---------------------------------------------------------
    def export(self) -> dict:
        return {
            "format": "mkvtime-archive",
            "schema_version": SCHEMA_VERSION,
            "versions": [dict(r) for r in self.db.execute(
                "SELECT * FROM versions ORDER BY seq")],
            "artifacts": [dict(r) for r in self.db.execute(
                "SELECT * FROM artifacts ORDER BY version_id, key")],
            "events": [dict(r) for r in self.db.execute(
                "SELECT * FROM events ORDER BY seq")],
            "pointer": self.current(),
        }

    def import_archive(self, archive: dict):
        """Load an exported archive into this (empty) store, preserving all
        identities, ordering (seq) and provenance links."""
        assert archive["format"] == "mkvtime-archive"
        with self.db:
            for v in archive["versions"]:
                self.db.execute(
                    "INSERT INTO versions(id,seq,parent_id,base_id,status,message,"
                    "created_at) VALUES(?,?,?,?,?,?,?)",
                    (v["id"], v["seq"], v["parent_id"], v.get("base_id"),
                     v["status"], v["message"], v["created_at"]))
            for a in archive["artifacts"]:
                self.db.execute(
                    "INSERT INTO artifacts(version_id,key,json) VALUES(?,?,?)",
                    (a["version_id"], a["key"], a["json"]))
            for e in archive["events"]:
                self.db.execute(
                    "INSERT INTO events(id,seq,version_id,kind,payload,created_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (e["id"], e["seq"], e["version_id"], e["kind"], e["payload"],
                     e["created_at"]))
            if archive["pointer"]:
                self.db.execute(
                    "INSERT OR REPLACE INTO pointer(id,version_id) VALUES(1,?)",
                    (1, archive["pointer"]))
