"""SQLite event-sourced storage with atomic version pointers.

Guarantees:
  * Every derivation/event append writes a full version; the branch head
    pointer only moves inside the same commit => readers see either the old
    version or the complete new one, never a half-written state.
  * Pending staged rows (``staging_*``) are reconciled on startup: a
    ``prepared`` txn whose switch commit never lands is rolled back to the old
    version (modelling an interrupted writer).
  * Two editors starting from the same parent version diverge; the second
    submit raises :class:`Conflict`, resolvable via ``resolve_version``.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from typing import Any

SCHEMA_VERSION = 2


SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS projects(
  project_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS versions(
  project_id TEXT NOT NULL,
  version_id TEXT PRIMARY KEY,
  parent_id TEXT,
  branch TEXT NOT NULL,
  seq INTEGER NOT NULL,
  kind TEXT NOT NULL,
  label TEXT NOT NULL,
  created_at REAL NOT NULL,
  author TEXT NOT NULL,
  doc_json TEXT NOT NULL,
  derived_json TEXT NOT NULL,
  report_json TEXT NOT NULL,
  rules_version TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'complete',
  UNIQUE(project_id, branch, seq)
);

CREATE TABLE IF NOT EXISTS heads(
  project_id TEXT NOT NULL,
  branch TEXT NOT NULL,
  version_id TEXT NOT NULL,
  PRIMARY KEY(project_id, branch)
);

CREATE TABLE IF NOT EXISTS events(
  event_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  version_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  branch TEXT NOT NULL,
  etype TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at REAL NOT NULL,
  author TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS branch_parent(
  project_id TEXT NOT NULL, branch TEXT NOT NULL,
  parent_version_id TEXT, PRIMARY KEY(project_id, branch)
);

-- interrupted-write staging area
CREATE TABLE IF NOT EXISTS staging(
  txn_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  created_at REAL NOT NULL,
  state TEXT NOT NULL,
  payload_json TEXT NOT NULL
);
"""


class Conflict(Exception):
    def __init__(self, msg: str, *, head_id: str, parent_id: str):
        super().__init__(msg)
        self.head_id = head_id
        self.parent_id = parent_id


class Store:
    def __init__(self, path: str = ":memory:"):
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._init()

    def _init(self) -> None:
        with self._tx() as cur:
            cur.executescript(SCHEMA)
            row = cur.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if row is None:
                cur.execute("INSERT INTO meta VALUES('schema_version', ?)",
                            (str(SCHEMA_VERSION),))
            else:
                self._migrate(cur, int(row["value"]))
        self.recover_pending()

    def _migrate(self, cur, from_v: int) -> None:
        if from_v < 2:
            # v1 kept only a single 'current' version and no events table.
            cols = {r["name"] for r in cur.execute("PRAGMA table_info(versions)")}
            if "rules_version" not in cols:
                cur.execute("ALTER TABLE versions ADD COLUMN rules_version TEXT DEFAULT 'rules-v1'")
            if "status" not in cols:
                cur.execute("ALTER TABLE versions ADD COLUMN status TEXT DEFAULT 'complete'")
            cur.execute("UPDATE meta SET value='2' WHERE key='schema_version'")

    @contextmanager
    def _tx(self):
        cur = self.conn.cursor()
        try:
            yield cur
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        finally:
            cur.close()

    # ----- crash recovery ------------------------------------------------
    def recover_pending(self) -> list[str]:
        """Roll back any staged txn left in 'prepared' state by a dead writer."""
        recovered = []
        rows = self.conn.execute(
            "SELECT txn_id, state FROM staging").fetchall()
        for r in rows:
            if r["state"] in ("prepared", "writing"):
                with self._tx() as cur:
                    cur.execute("DELETE FROM staging WHERE txn_id=?",
                                (r["txn_id"],))
                recovered.append(r["txn_id"])
        return recovered

    # ----- projects ------------------------------------------------------
    def create_project(self, project_id: str, name: str) -> None:
        with self._tx() as cur:
            cur.execute(
                "INSERT OR IGNORE INTO projects VALUES(?,?,?)",
                (project_id, name, time.time()))

    def list_projects(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM projects ORDER BY created_at")]

    # ----- read ----------------------------------------------------------
    def head_id(self, project_id: str, branch: str = "main") -> str | None:
        r = self.conn.execute(
            "SELECT version_id FROM heads WHERE project_id=? AND branch=?",
            (project_id, branch)).fetchone()
        return r["version_id"] if r else None

    def branches(self, project_id: str) -> list[dict]:
        rows = self.conn.execute("""
            SELECT h.branch, h.version_id, v.seq, v.label, v.kind, v.created_at
            FROM heads h JOIN versions v ON v.version_id=h.version_id
            WHERE h.project_id=? ORDER BY v.created_at""", (project_id,)).fetchall()
        return [dict(r) for r in rows]

    def get_version(self, version_id: str) -> dict:
        r = self.conn.execute(
            "SELECT * FROM versions WHERE version_id=?", (version_id,)).fetchone()
        if not r:
            raise KeyError(version_id)
        d = dict(r)
        d["doc"] = json.loads(d.pop("doc_json"))
        d["derived"] = json.loads(d.pop("derived_json"))
        d["report"] = json.loads(d.pop("report_json"))
        return d

    def get_doc(self, project_id: str, branch: str = "main") -> dict:
        vid = self.head_id(project_id, branch)
        return self.get_version(vid)["doc"] if vid else {}

    def history(self, project_id: str, branch: str | None = None) -> list[dict]:
        q = ("SELECT version_id,parent_id,branch,seq,kind,label,created_at,"
             "author,rules_version FROM versions WHERE project_id=?")
        args: list = [project_id]
        if branch:
            q += " AND branch=?"
            args.append(branch)
        q += " ORDER BY seq"
        return [dict(r) for r in self.conn.execute(q, args)]

    def events(self, project_id: str, version_id: str | None = None) -> list[dict]:
        q = "SELECT * FROM events WHERE project_id=?"
        args: list = [project_id]
        if version_id:
            q += " AND version_id=?"
            args.append(version_id)
        q += " ORDER BY seq, created_at"
        out = []
        for r in self.conn.execute(q, args):
            d = dict(r)
            d["payload"] = json.loads(d.pop("payload_json"))
            out.append(d)
        return out

    # ----- write / commit ------------------------------------------------
    def next_seq(self, project_id: str, branch: str) -> int:
        r = self.conn.execute(
            "SELECT COALESCE(MAX(seq),0)+1 AS n FROM versions "
            "WHERE project_id=? AND branch=?", (project_id, branch)).fetchone()
        return r["n"]

    def prepare_version(self, txn_id: str, project_id: str, payload: dict) -> None:
        """Stage a full new version without moving any pointer."""
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO staging VALUES(?,?,?,?,?)",
                (txn_id, project_id, time.time(), "prepared",
                 json.dumps(payload)))

    def crash_after_prepare(self, txn_id: str) -> None:
        """Test hook: leave a prepared txn staged, as if the writer died."""
        self.conn.commit()

    def commit_prepared(self, txn_id: str, *, expected_parent: str | None,
                        allow_conflict: bool = False) -> dict:
        row = self.conn.execute("SELECT * FROM staging WHERE txn_id=?",
                                (txn_id,)).fetchone()
        if not row:
            raise KeyError(f"no staging txn {txn_id}")
        payload = json.loads(row["payload_json"])
        project_id = row["project_id"]
        branch = payload["branch"]
        head = self.head_id(project_id, branch)
        if head != expected_parent and not allow_conflict:
            raise Conflict(
                f"branch {branch} moved: parent {expected_parent} != head {head}",
                head_id=head, parent_id=expected_parent)
        seq = self.next_seq(project_id, branch)
        return self._install(payload, project_id, seq, txn_id)

    def commit_version(self, project_id: str, payload: dict,
                       *, expected_parent: str | None = None) -> dict:
        """Atomically write version rows and switch the head pointer."""
        branch = payload["branch"]
        head = self.head_id(project_id, branch)
        if expected_parent is not None and head != expected_parent:
            raise Conflict(
                f"branch {branch} moved: expected parent {expected_parent}, "
                f"head is {head}", head_id=head, parent_id=expected_parent)
        seq = self.next_seq(project_id, branch)
        return self._install(payload, project_id, seq, None)

    def _install(self, payload: dict, project_id: str, seq: int,
                 txn_id: str | None) -> dict:
        branch = payload["branch"]
        version_id = payload["version_id"]
        with self._tx() as cur:
            cur.execute(
                "INSERT OR IGNORE INTO branch_parent VALUES(?,?,?)",
                (project_id, branch, payload.get("parent_id")))
            cur.execute("""INSERT INTO versions
                (project_id,version_id,parent_id,branch,seq,kind,label,
                 created_at,author,doc_json,derived_json,report_json,
                 rules_version,status)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (project_id, version_id, payload.get("parent_id"), branch, seq,
                 payload["kind"], payload["label"], time.time(),
                 payload.get("author", "reviewer"),
                 json.dumps(payload["doc"], ensure_ascii=False),
                 json.dumps(payload["derived"], ensure_ascii=False),
                 json.dumps(payload["report"], ensure_ascii=False),
                 payload.get("rules_version", "rules-v1"), "complete"))
            ev = payload["event"]
            cur.execute("""INSERT INTO events
                (event_id,project_id,version_id,seq,branch,etype,
                 payload_json,created_at,author)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (ev["event_id"], project_id, version_id, seq, branch,
                 ev["etype"], json.dumps(ev["payload"], ensure_ascii=False),
                 time.time(), payload.get("author", "reviewer")))
            cur.execute("""INSERT INTO heads(project_id,branch,version_id)
                VALUES(?,?,?) ON CONFLICT(project_id,branch)
                DO UPDATE SET version_id=excluded.version_id""",
                (project_id, branch, version_id))
            if txn_id:
                cur.execute("DELETE FROM staging WHERE txn_id=?", (txn_id,))
        return {"version_id": version_id, "seq": seq, "branch": branch}

    def new_branch(self, project_id: str, name: str,
                   from_version_id: str) -> dict:
        v = self.get_version(from_version_id)
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO heads(project_id,branch,version_id) VALUES(?,?,?)",
                (project_id, name, from_version_id))
            cur.execute(
                "INSERT OR REPLACE INTO branch_parent VALUES(?,?,?)",
                (project_id, name, from_version_id))
        return {"branch": name, "head": from_version_id,
                "from_seq": v["seq"]}

    def resolve_version(self, project_id: str, payload: dict,
                        *, winner_id: str, loser_id: str) -> dict:
        """Record a conflict resolution as a merge-style decision version."""
        branch = payload["branch"]
        head = self.head_id(project_id, branch)
        seq = self.next_seq(project_id, branch)
        payload = dict(payload)
        payload.setdefault("parent_id", head)
        payload.setdefault("kind", "resolution")
        payload["event"]["payload"].update({
            "resolution": {"winner": winner_id, "loser": loser_id,
                           "kept_loser_candidates": True}})
        return self._install(payload, project_id, seq, None)
