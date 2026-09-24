"""Versioned SQLite store.

Guarantees:

* every version is immutable; the ``current`` pointer switches only after a
  version is written in full, inside one transaction -- a crash mid-write
  leaves either the old version or a complete new one;
* concurrent saves from the same base produce an explicit, resolvable
  conflict; the losing branch is kept, never overwritten;
* every event is machine-readable and points at the version whose
  derivations were visible when it happened (replay restores that view);
* export/import preserves identities, order and provenance.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid

from .graph import compute_derivations

SCHEMA_VERSION = 2

SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS meta(
  key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS versions(
  version_id TEXT PRIMARY KEY,
  parent_id TEXT,
  label TEXT,
  actor TEXT,
  branch INTEGER DEFAULT 1,
  seq INTEGER,
  created_at TEXT);
CREATE TABLE IF NOT EXISTS objects(
  version_id TEXT, object_id TEXT, kind TEXT, ordinal INTEGER, data TEXT,
  PRIMARY KEY(version_id, object_id));
CREATE TABLE IF NOT EXISTS events(
  event_id TEXT PRIMARY KEY,
  version_id TEXT,
  seq INTEGER,
  kind TEXT,
  actor TEXT,
  payload TEXT,
  created_at TEXT);
CREATE TABLE IF NOT EXISTS derivations(
  version_id TEXT, key TEXT, rule_version TEXT, inputs_hash TEXT,
  input_ids TEXT, status TEXT, reason TEXT, output TEXT,
  PRIMARY KEY(version_id, key));
CREATE TABLE IF NOT EXISTS conflicts(
  conflict_id TEXT PRIMARY KEY,
  base_version TEXT, left_version TEXT, right_version TEXT,
  status TEXT, resolution TEXT, created_at TEXT);
"""


class ConflictError(Exception):
    def __init__(self, conflict_id, base, current, branch_version):
        self.conflict_id = conflict_id
        self.base = base
        self.current = current
        self.branch_version = branch_version
        super().__init__(
            f"save conflict: base {base} is stale, current is {current}; "
            f"attempt saved as branch {branch_version} "
            f"(conflict {conflict_id})")


class InjectedWriteFault(Exception):
    """Test hook: simulates a crash in the middle of a version write."""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


class Store:
    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._open_or_migrate()

    # ------------------------------------------------------------- schema
    def _open_or_migrate(self):
        cur = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='meta'")
        if not cur.fetchone():
            self.conn.executescript(SCHEMA_V2)
            self._set_meta("schema_version", str(SCHEMA_VERSION))
            self._set_meta("event_seq", "0")
            self.conn.commit()
            return
        version = int(self._get_meta("schema_version") or "1")
        if version < 2:
            # v1 -> v2: events gained `actor`, derivations gained `reason`.
            cols = {r[1] for r in self.conn.execute("PRAGMA table_info(events)")}
            if "actor" not in cols:
                self.conn.execute("ALTER TABLE events ADD COLUMN actor TEXT")
            cols = {r[1] for r in
                    self.conn.execute("PRAGMA table_info(derivations)")}
            if "reason" not in cols:
                self.conn.execute(
                    "ALTER TABLE derivations ADD COLUMN reason TEXT")
            self._set_meta("schema_version", "2")
            self.conn.commit()

    def _get_meta(self, key):
        row = self.conn.execute("SELECT value FROM meta WHERE key=?",
                                (key,)).fetchone()
        return row["value"] if row else None

    def _set_meta(self, key, value):
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)))

    # ----------------------------------------------------------- versions
    def current_version_id(self) -> str | None:
        return self._get_meta("current_version")

    def list_versions(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM versions ORDER BY seq").fetchall()
        current = self.current_version_id()
        return [dict(r) | {"is_current": r["version_id"] == current}
                for r in rows]

    def get_objects(self, version_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT object_id, kind, ordinal, data FROM objects "
            "WHERE version_id=? ORDER BY ordinal", (version_id,)).fetchall()
        return [{"object_id": r["object_id"], "kind": r["kind"],
                 "ordinal": r["ordinal"], "data": json.loads(r["data"])}
                for r in rows]

    def get_derivations(self, version_id: str) -> dict[str, dict]:
        rows = self.conn.execute(
            "SELECT * FROM derivations WHERE version_id=?", (version_id,)
        ).fetchall()
        return {r["key"]: {
            "key": r["key"], "version_id": r["version_id"],
            "rule_version": r["rule_version"], "inputs_hash": r["inputs_hash"],
            "input_ids": json.loads(r["input_ids"]), "status": r["status"],
            "reason": r["reason"], "output": json.loads(r["output"]),
        } for r in rows}

    def create_version(self, objects: list[dict], events: list[dict],
                       label: str, actor: str,
                       expected_base: str | None = None,
                       advance_current: bool = True,
                       fail_before_commit: bool = False) -> str:
        """Atomically publish a new immutable version.

        ``expected_base`` implements optimistic concurrency: when given and
        stale, the attempt is still preserved as a branch and a resolvable
        conflict is recorded (ConflictError).
        """
        current = self.current_version_id()
        if expected_base is not None and expected_base != current:
            branch_id = self._write_version(
                objects, events, label + " (conflicting branch)", actor,
                parent=expected_base, advance_current=False)
            conflict_id = f"conflict:{uuid.uuid4().hex[:12]}"
            self.conn.execute(
                "INSERT INTO conflicts VALUES(?,?,?,?,?,?,?)",
                (conflict_id, expected_base, current, branch_id,
                 "open", None, _now()))
            self._append_events(current, [{
                "kind": "conflict.detected", "actor": actor,
                "payload": {"conflict_id": conflict_id, "base": expected_base,
                            "left": current, "right": branch_id,
                            "note": "two users saved from the same version; "
                                    "both branches kept"}}])
            self.conn.commit()
            raise ConflictError(conflict_id, expected_base, current, branch_id)
        return self._write_version(objects, events, label, actor,
                                   parent=current,
                                   advance_current=advance_current,
                                   fail_before_commit=fail_before_commit)

    def _write_version(self, objects, events, label, actor, parent,
                       advance_current, fail_before_commit=False) -> str:
        version_id = f"v{uuid.uuid4().hex[:12]}"
        prev_objects = self.get_objects(parent) if parent else []
        prev_derivs = self.get_derivations(parent) if parent else {}
        derivs = compute_derivations(objects, prev_objects, prev_derivs)
        seq = int(self._get_meta("version_seq") or "0") + 1
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            self.conn.execute(
                "INSERT INTO versions VALUES(?,?,?,?,?,?,?)",
                (version_id, parent, label, actor, 1, seq, _now()))
            for ordinal, obj in enumerate(objects):
                self.conn.execute(
                    "INSERT INTO objects VALUES(?,?,?,?,?)",
                    (version_id, obj["object_id"], obj["kind"], ordinal,
                     json.dumps(obj["data"], sort_keys=True)))
            for key, d in derivs.items():
                self.conn.execute(
                    "INSERT INTO derivations VALUES(?,?,?,?,?,?,?,?)",
                    (version_id, key, d["rule_version"], d["inputs_hash"],
                     json.dumps(d["input_ids"]), d["status"], d["reason"],
                     json.dumps(d["output"], sort_keys=True)))
            if fail_before_commit:
                raise InjectedWriteFault(
                    "simulated crash after data writes, before pointer switch")
            self._set_meta("version_seq", str(seq))
            self._append_events(version_id, events)
            if advance_current:
                self._set_meta("current_version", version_id)
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise
        return version_id

    def _append_events(self, version_id, events):
        seq = int(self._get_meta("event_seq") or "0")
        for event in events:
            seq += 1
            event_id = f"evt:{seq:06d}"
            self.conn.execute(
                "INSERT INTO events VALUES(?,?,?,?,?,?,?)",
                (event_id, version_id, seq, event["kind"],
                 event.get("actor", "system"),
                 json.dumps(event.get("payload", {}), sort_keys=True),
                 _now()))
        self._set_meta("event_seq", str(seq))

    def add_events(self, version_id, events):
        self.conn.execute("BEGIN IMMEDIATE")
        self._append_events(version_id, events)
        self.conn.commit()

    # ------------------------------------------------------------- events
    def list_events(self, version_id=None) -> list[dict]:
        if version_id:
            rows = self.conn.execute(
                "SELECT * FROM events WHERE version_id=? ORDER BY seq",
                (version_id,)).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM events ORDER BY seq").fetchall()
        return [dict(r) | {"payload": json.loads(r["payload"])} for r in rows]

    def get_event(self, event_id) -> dict | None:
        row = self.conn.execute("SELECT * FROM events WHERE event_id=?",
                                (event_id,)).fetchone()
        return dict(row) | {"payload": json.loads(row["payload"])} if row else None

    # ---------------------------------------------------------- conflicts
    def list_conflicts(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM conflicts").fetchall()
        return [dict(r) for r in rows]

    def resolve_conflict(self, conflict_id, choice, actor,
                         note="") -> str:
        conflict = self.conn.execute(
            "SELECT * FROM conflicts WHERE conflict_id=?",
            (conflict_id,)).fetchone()
        if not conflict or conflict["status"] != "open":
            raise KeyError(f"no open conflict {conflict_id}")
        chosen = (conflict["left_version"] if choice == "left"
                  else conflict["right_version"])
        objects = self.get_objects(chosen)
        resolution_events = [{
            "kind": "conflict.resolved", "actor": actor,
            "payload": {"conflict_id": conflict_id, "choice": choice,
                        "chosen_version": chosen, "note": note,
                        "kept_unchosen": (conflict["right_version"]
                                          if choice == "left"
                                          else conflict["left_version"])}}]
        new_version = self._write_version(
            objects, resolution_events,
            f"resolve {conflict_id} -> {choice}", actor,
            parent=self.current_version_id(), advance_current=True)
        self.conn.execute(
            "UPDATE conflicts SET status='resolved', resolution=? "
            "WHERE conflict_id=?",
            (json.dumps({"choice": choice, "chosen": chosen,
                         "new_version": new_version}), conflict_id))
        self.conn.commit()
        return new_version

    # -------------------------------------------------------- export/import
    def export_payload(self) -> dict:
        tables = {}
        for table in ("meta", "versions", "objects", "events",
                      "derivations", "conflicts"):
            rows = self.conn.execute(f"SELECT * FROM {table}").fetchall()
            tables[table] = [dict(r) for r in rows]
        return {"format": "mkv-timecode-court/1",
                "schema_version": SCHEMA_VERSION, "tables": tables}

    def import_payload(self, payload: dict):
        if payload.get("format") != "mkv-timecode-court/1":
            raise ValueError("unrecognized export format")
        if self.current_version_id() is not None:
            raise ValueError("import requires an empty database")
        tables = payload["tables"]
        self.conn.execute("BEGIN IMMEDIATE")
        for row in tables.get("meta", []):
            self.conn.execute("INSERT INTO meta VALUES(?,?)",
                              (row["key"], row["value"]))
        for row in tables.get("versions", []):
            self.conn.execute(
                "INSERT INTO versions VALUES(?,?,?,?,?,?,?)",
                (row["version_id"], row["parent_id"], row["label"],
                 row.get("actor"), row.get("branch", 1), row["seq"],
                 row["created_at"]))
        for row in tables.get("objects", []):
            self.conn.execute(
                "INSERT INTO objects VALUES(?,?,?,?,?)",
                (row["version_id"], row["object_id"], row["kind"],
                 row["ordinal"], row["data"]))
        for row in tables.get("events", []):
            self.conn.execute(
                "INSERT INTO events VALUES(?,?,?,?,?,?,?)",
                (row["event_id"], row["version_id"], row["seq"], row["kind"],
                 row.get("actor"), row["payload"], row["created_at"]))
        for row in tables.get("derivations", []):
            self.conn.execute(
                "INSERT INTO derivations VALUES(?,?,?,?,?,?,?,?)",
                (row["version_id"], row["key"], row["rule_version"],
                 row["inputs_hash"], row["input_ids"], row["status"],
                 row.get("reason"), row["output"]))
        for row in tables.get("conflicts", []):
            self.conn.execute(
                "INSERT INTO conflicts VALUES(?,?,?,?,?,?,?)",
                (row["conflict_id"], row["base_version"], row["left_version"],
                 row["right_version"], row["status"], row["resolution"],
                 row["created_at"]))
        self.conn.commit()

    def close(self):
        self.conn.close()
