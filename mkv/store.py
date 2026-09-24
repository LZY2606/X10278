"""Versioned SQLite store.

Guarantees:
- Every derivation produces an independent version; the `current` pointer is
  switched only inside the same transaction that marks the version complete.
  A crash mid-write leaves either the old version or a complete new version.
- Two users saving from the same base version produce a resolvable conflict;
  both branches are kept, nothing is overwritten.
- Schema migrations are explicit (schema_version in `meta`).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone

SCHEMA_VERSION = 2

SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS versions(
    id INTEGER PRIMARY KEY,
    parent_id INTEGER,
    label TEXT,
    status TEXT,
    actor TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS nodes(
    version_id INTEGER,
    key TEXT,
    payload TEXT,
    inputs TEXT,
    digest TEXT,
    explanation TEXT,
    PRIMARY KEY(version_id, key)
);
CREATE TABLE IF NOT EXISTS events(
    id INTEGER PRIMARY KEY,
    version_id INTEGER,
    kind TEXT,
    payload TEXT,
    actor TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS pointer(
    id INTEGER PRIMARY KEY CHECK(id = 1),
    version_id INTEGER
);
CREATE TABLE IF NOT EXISTS conflicts(
    id INTEGER PRIMARY KEY,
    base_version INTEGER,
    a_version INTEGER,
    b_version INTEGER,
    status TEXT,
    resolution TEXT,
    created_at TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest_payload(payload) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class Store:
    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.RLock()
        self._staged_nodes: dict[int, list] = {}
        self._staged_events: dict[int, list] = {}
        self._migrate()
        self._cleanup_interrupted()

    def _migrate(self):
        with self._lock, self.conn:
            have_meta = self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='meta'"
            ).fetchone()
            if not have_meta:
                self.conn.executescript(SCHEMA_V2)
                self.conn.execute(
                    "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),))
                self.conn.execute(
                    "INSERT OR IGNORE INTO pointer(id, version_id) VALUES(1, NULL)")
                return
            row = self.conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'").fetchone()
            version = int(row["value"]) if row else 1
            if version == 1:
                # v1 -> v2: nodes gain explanation, events gain actor
                self.conn.execute(
                    "ALTER TABLE nodes ADD COLUMN explanation TEXT NOT NULL DEFAULT ''")
                self.conn.execute(
                    "ALTER TABLE events ADD COLUMN actor TEXT NOT NULL DEFAULT 'system'")
                self.conn.execute(
                    "UPDATE meta SET value=? WHERE key='schema_version'",
                    (str(SCHEMA_VERSION),))
            elif version != SCHEMA_VERSION:
                raise RuntimeError(f"unsupported schema version {version}")

    def _cleanup_interrupted(self):
        """Versions left in `building` state by a crash are marked aborted."""
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE versions SET status='aborted' WHERE status='building'")

    def begin_version(self, parent_id: int | None, label: str, actor: str) -> int:
        with self._lock, self.conn:
            cur = self.conn.execute(
                "INSERT INTO versions(parent_id, label, status, actor, created_at)"
                " VALUES(?,?,?,?,?)",
                (parent_id, label, "building", actor, _now()))
            vid = cur.lastrowid
        self._staged_nodes[vid] = []
        self._staged_events[vid] = []
        return vid

    def put_node(self, vid: int, key: str, payload, inputs: list[str],
                 explanation: str):
        self._staged_nodes.setdefault(vid, []).append((
            vid, key, json.dumps(payload), json.dumps(inputs),
            digest_payload(payload), explanation))

    def stage_event(self, vid: int, kind: str, payload, actor: str = "system"):
        self._staged_events.setdefault(vid, []).append((
            vid, kind, json.dumps(payload), actor, _now()))

    def commit_version(self, vid: int, expected_parent: int | None = None) -> dict:
        """Atomically persist nodes+events and switch the pointer.

        If `expected_parent` is given and the pointer has moved on, the version
        is still kept (complete) but a conflict is raised instead of switching.
        """
        with self._lock, self.conn:
            cur = self.current_version()
            self.conn.execute(
                "UPDATE versions SET status='complete' WHERE id=?", (vid,))
            self.conn.executemany(
                "INSERT INTO nodes(version_id, key, payload, inputs, digest,"
                " explanation) VALUES(?,?,?,?,?,?)",
                self._staged_nodes.pop(vid, []))
            self.conn.executemany(
                "INSERT INTO events(version_id, kind, payload, actor, created_at)"
                " VALUES(?,?,?,?,?)",
                self._staged_events.pop(vid, []))
            if expected_parent is not None and cur != expected_parent:
                row = self.conn.execute(
                    "INSERT INTO conflicts(base_version, a_version, b_version,"
                    " status, created_at) VALUES(?,?,?,?,?)",
                    (expected_parent, cur, vid, "open", _now()))
                cid = row.lastrowid
                self.append_event(
                    vid, "conflict.raised",
                    {"conflict_id": cid, "base_version": expected_parent,
                     "current_branch": cur, "incoming_branch": vid,
                     "statement": "two users saved from the same base version; "
                                  "both branches are kept and must be resolved"},
                    actor="system")
                return {"status": "conflict", "conflict_id": cid,
                        "version": vid, "current": cur}
            self.conn.execute("UPDATE pointer SET version_id=? WHERE id=1", (vid,))
            return {"status": "committed", "version": vid}

    def abort_version(self, vid: int):
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE versions SET status='aborted' WHERE id=?", (vid,))
        self._staged_nodes.pop(vid, None)
        self._staged_events.pop(vid, None)

    def current_version(self) -> int | None:
        row = self.conn.execute(
            "SELECT version_id FROM pointer WHERE id=1").fetchone()
        return row["version_id"] if row else None

    def checkout(self, vid: int, actor: str = "reviewer") -> dict:
        """Restore a historical version: its dependency versions and visible
        conclusions become current again (no recomputation, no overwrite)."""
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT id FROM versions WHERE id=? AND status='complete'",
                (vid,)).fetchone()
            if not row:
                raise KeyError(f"version {vid} not found or not complete")
            self.conn.execute("UPDATE pointer SET version_id=? WHERE id=1", (vid,))
            self.append_event(vid, "view.checkout",
                              {"restored_version": vid,
                               "statement": "historical dependency versions and "
                                            "visible conclusions restored"},
                              actor=actor)
        return {"status": "checked_out", "version": vid}

    # ---- queries ----

    def version_nodes(self, vid: int) -> dict:
        rows = self.conn.execute(
            "SELECT key, payload, inputs, digest, explanation FROM nodes"
            " WHERE version_id=? ORDER BY key", (vid,)).fetchall()
        return {r["key"]: {
            "payload": json.loads(r["payload"]),
            "inputs": json.loads(r["inputs"]),
            "digest": r["digest"],
            "explanation": r["explanation"],
        } for r in rows}

    def list_versions(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, parent_id, label, status, actor, created_at"
            " FROM versions ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def list_events(self, vid: int | None = None) -> list[dict]:
        sql = ("SELECT id, version_id, kind, payload, actor, created_at"
               " FROM events")
        args: tuple = ()
        if vid is not None:
            sql += " WHERE version_id=?"
            args = (vid,)
        sql += " ORDER BY id"
        return [{**dict(r), "payload": json.loads(r["payload"]),
                 "event_id": f"evt:{r['id']}"}
                for r in self.conn.execute(sql, args).fetchall()]

    def append_event(self, vid: int, kind: str, payload, actor: str = "system"):
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO events(version_id, kind, payload, actor, created_at)"
                " VALUES(?,?,?,?,?)", (vid, kind, json.dumps(payload), actor,
                                       _now()))

    def list_conflicts(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM conflicts ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def resolve_conflict(self, cid: int, choice: str,
                         actor: str = "reviewer") -> dict:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM conflicts WHERE id=?", (cid,)).fetchone()
            if not row:
                raise KeyError(f"conflict {cid} not found")
            if row["status"] != "open":
                raise ValueError(f"conflict {cid} already resolved")
            chosen = row["a_version"] if choice == "a" else row["b_version"]
            kept = row["b_version"] if choice == "a" else row["a_version"]
            self.conn.execute(
                "UPDATE conflicts SET status='resolved', resolution=? WHERE id=?",
                (f"continue:{choice}", cid))
            self.conn.execute(
                "UPDATE pointer SET version_id=? WHERE id=1", (chosen,))
            self.append_event(
                chosen, "conflict.resolved",
                {"conflict_id": cid, "continued_branch": chosen,
                 "kept_branch": kept,
                 "statement": "chosen branch continues; the unselected branch is "
                              "kept and remains queryable"},
                actor=actor)
        return {"status": "resolved", "continued": chosen, "kept": kept}

    # ---- export / import / wipe ----

    def export_dump(self) -> dict:
        tables = {}
        for table in ("meta", "versions", "nodes", "events", "pointer",
                      "conflicts"):
            rows = self.conn.execute(f"SELECT * FROM {table}").fetchall()
            tables[table] = [dict(r) for r in rows]
        return {"format": "matroska-timecode-court/1", "tables": tables}

    def wipe(self):
        with self._lock, self.conn:
            for table in ("nodes", "events", "conflicts", "pointer",
                          "versions", "meta"):
                self.conn.execute(f"DELETE FROM {table}")

    def import_dump(self, dump: dict):
        """Rebuild the database from an export. Identities (ids, digests,
        ordering, provenance) are preserved verbatim."""
        if dump.get("format") != "matroska-timecode-court/1":
            raise ValueError("unknown dump format")
        tables = dump["tables"]
        with self._lock, self.conn:
            self.wipe()
            for row in tables.get("meta", []):
                self.conn.execute(
                    "INSERT INTO meta(key, value) VALUES(?,?)",
                    (row["key"], row["value"]))
            for row in tables.get("versions", []):
                self.conn.execute(
                    "INSERT INTO versions(id, parent_id, label, status, actor,"
                    " created_at) VALUES(?,?,?,?,?,?)",
                    (row["id"], row["parent_id"], row["label"], row["status"],
                     row.get("actor", "system"), row["created_at"]))
            for row in tables.get("nodes", []):
                self.conn.execute(
                    "INSERT INTO nodes(version_id, key, payload, inputs, digest,"
                    " explanation) VALUES(?,?,?,?,?,?)",
                    (row["version_id"], row["key"], row["payload"],
                     row["inputs"], row["digest"], row.get("explanation", "")))
            for row in tables.get("events", []):
                self.conn.execute(
                    "INSERT INTO events(id, version_id, kind, payload, actor,"
                    " created_at) VALUES(?,?,?,?,?,?)",
                    (row["id"], row["version_id"], row["kind"], row["payload"],
                     row.get("actor", "system"), row["created_at"]))
            ptr = tables.get("pointer", [])
            if ptr:
                self.conn.execute(
                    "INSERT INTO pointer(id, version_id) VALUES(1, ?)",
                    (ptr[0]["version_id"],))
            else:
                self.conn.execute(
                    "INSERT INTO pointer(id, version_id) VALUES(1, NULL)")
            for row in tables.get("conflicts", []):
                self.conn.execute(
                    "INSERT INTO conflicts(id, base_version, a_version,"
                    " b_version, status, resolution, created_at)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (row["id"], row["base_version"], row["a_version"],
                     row["b_version"], row["status"], row.get("resolution"),
                     row["created_at"]))

    def full_state(self) -> dict:
        vid = self.current_version()
        nodes = self.version_nodes(vid) if vid is not None else {}
        return {
            "current_version": vid,
            "versions": self.list_versions(),
            "nodes": nodes,
            "events": self.list_events(),
            "conflicts": self.list_conflicts(),
            "anomalies": (nodes.get("anomalies", {}).get("payload", {})
                          .get("anomalies", [])),
        }

    def close(self):
        self.conn.close()
