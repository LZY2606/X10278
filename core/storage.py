"""SQLite-backed, event-sourced, versioned storage.

Atomicity guarantee: a derivation is fully written as a *building* version in
one transaction; the current pointer only switches after commit. If a process
dies mid-write, the next opener rolls the building version back / ignores it,
so readers always see either the old complete version or the new complete one.

Concurrency: saves carry the base version they started from; two editors on the
same base produce a 409-style conflict that must be explicitly resolved.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from typing import Optional, Any

from .events import Event, make_event, new_event_id

SCHEMA_VERSION = 2
_LOCK = threading.RLock()


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _migrate(conn: sqlite3.Connection):
    """Create schema or migrate an old (v1) database without data loss."""
    row = conn.execute("PRAGMA user_version").fetchone()
    ver = row[0]
    if ver == 0:
        conn.executescript(_SCHEMA)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.commit()
        return
    if ver == 1:
        # v1 lacked the decisions table; add it and upgrade.
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='decisions'").fetchone()
        if not exists:
            conn.execute(
                "CREATE TABLE decisions ("
                "key TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.commit()
    row = conn.execute("PRAGMA user_version").fetchone()
    if row[0] > SCHEMA_VERSION:
        raise RuntimeError(f"database schema {row[0]} newer than code "
                           f"{SCHEMA_VERSION}")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    source_uid TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL,
    byte_length INTEGER NOT NULL,
    imported_at REAL NOT NULL,
    label TEXT
);
CREATE TABLE IF NOT EXISTS source_revisions (
    source_uid TEXT NOT NULL,
    revision INTEGER NOT NULL,
    data BLOB NOT NULL,
    sha256 TEXT NOT NULL,
    kind TEXT NOT NULL,
    changed_direct_source TEXT,
    created_at REAL NOT NULL,
    PRIMARY KEY (source_uid, revision)
);
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    seq INTEGER NOT NULL,
    branch TEXT NOT NULL,
    base_version INTEGER NOT NULL,
    timestamp REAL NOT NULL,
    actor TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    rule_refs TEXT NOT NULL,
    adopted TEXT NOT NULL,
    rejected TEXT NOT NULL,
    affected_uids TEXT NOT NULL,
    parent_event_id TEXT,
    derived_version INTEGER
);
CREATE TABLE IF NOT EXISTS deriv_versions (
    version INTEGER PRIMARY KEY AUTOINCREMENT,
    source_uid TEXT NOT NULL,
    source_revision INTEGER NOT NULL,
    branch TEXT NOT NULL,
    event_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('building','complete')),
    params TEXT NOT NULL,
    doc_json TEXT,
    derived_json TEXT,
    anomalies_json TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS branch_heads (
    branch TEXT PRIMARY KEY,
    source_uid TEXT NOT NULL,
    source_revision INTEGER NOT NULL,
    current_version INTEGER NOT NULL,
    event_seq INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
    key TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class ConflictError(Exception):
    def __init__(self, message, current_version, incoming_version):
        super().__init__(message)
        self.current_version = current_version
        self.incoming_version = incoming_version


class Store:
    def __init__(self, path: str = ":memory:"):
        self.path = path
        self.conn = _connect(path)
        _migrate(self.conn)
        self._cleanup_building()

    def close(self):
        self.conn.close()

    def _cleanup_building(self):
        with _LOCK:
            # Remove any half-written versions from a crashed process.
            self.conn.execute(
                "DELETE FROM deriv_versions WHERE status='building'")
            self.conn.commit()

    # ---------------------------------------------------------------- sources
    def import_source(self, data: bytes, label: str, kind: str = "import",
                      changed: str | None = None,
                      source_uid: str | None = None) -> tuple[str, int]:
        import time
        sha = hashlib.sha256(data).hexdigest()
        uid = source_uid or f"src:{sha[:16]}"
        with _LOCK:
            exists = self.conn.execute(
                "SELECT 1 FROM sources WHERE source_uid=?", (uid,)).fetchone()
            if exists:
                self.conn.execute(
                    "UPDATE sources SET label=? WHERE source_uid=?",
                    (label, uid))
            else:
                self.conn.execute(
                    "INSERT INTO sources(source_uid,sha256,byte_length,"
                    "imported_at,label) VALUES(?,?,?,?,?)",
                    (uid, sha, len(data), time.time(), label))
            row = self.conn.execute(
                "SELECT COALESCE(MAX(revision),0) FROM source_revisions "
                "WHERE source_uid=?", (uid,)).fetchone()
            rev = row[0] + 1
            self.conn.execute(
                "INSERT INTO source_revisions(source_uid,revision,data,sha256,"
                "kind,changed_direct_source,created_at) VALUES(?,?,?,?,?,?,?)",
                (uid, rev, data, sha, kind, changed, time.time()))
            self.conn.commit()
        return uid, rev

    def get_source_bytes(self, uid: str, revision: int) -> bytes:
        row = self.conn.execute(
            "SELECT data FROM source_revisions WHERE source_uid=? AND "
            "revision=?", (uid, revision)).fetchone()
        if row is None:
            raise KeyError((uid, revision))
        return row["data"]

    def list_sources(self):
        return [dict(r) for r in self.conn.execute(
            "SELECT source_uid,sha256,byte_length,imported_at,label "
            "FROM sources ORDER BY imported_at")]

    def source_revisions(self, uid: str):
        return [
            {"revision": r["revision"], "sha256": r["sha256"],
             "kind": r["kind"],
             "changed_direct_source": r["changed_direct_source"],
             "created_at": r["created_at"]}
            for r in self.conn.execute(
                "SELECT revision,sha256,kind,changed_direct_source,created_at "
                "FROM source_revisions WHERE source_uid=? ORDER BY revision",
                (uid,))]

    # ----------------------------------------------------------------- events
    def _next_seq(self, branch: str) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(seq),0)+1 FROM events WHERE branch=?",
            (branch,)).fetchone()
        return row[0]

    def append_event(self, ev: Event):
        with _LOCK:
            self.conn.execute(
                "INSERT INTO events(id,seq,branch,base_version,timestamp,"
                "actor,kind,payload,rule_refs,adopted,rejected,"
                "affected_uids,parent_event_id,derived_version) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ev.id, ev.seq, ev.branch, ev.base_version, ev.timestamp,
                 ev.actor, ev.kind, json.dumps(ev.payload, ensure_ascii=False),
                 json.dumps(ev.rule_refs), json.dumps(ev.adopted),
                 json.dumps(ev.rejected), json.dumps(ev.affected_uids),
                 ev.parent_event_id, ev.derived_version))
            self.conn.commit()

    def list_events(self, branch: Optional[str] = None) -> list[dict]:
        if branch is None:
            rows = self.conn.execute(
                "SELECT * FROM events ORDER BY seq, id").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM events WHERE branch=? ORDER BY seq",
                (branch,)).fetchall()
        return [self._event_row(r) for r in rows]

    def get_event(self, event_id: str) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM events WHERE id=?",
                                (event_id,)).fetchone()
        return self._event_row(row) if row else None

    @staticmethod
    def _event_row(r) -> dict:
        return {
            "id": r["id"], "seq": r["seq"], "branch": r["branch"],
            "base_version": r["base_version"],
            "timestamp": r["timestamp"], "actor": r["actor"],
            "kind": r["kind"], "payload": json.loads(r["payload"]),
            "rule_refs": json.loads(r["rule_refs"]),
            "adopted": json.loads(r["adopted"]),
            "rejected": json.loads(r["rejected"]),
            "affected_uids": json.loads(r["affected_uids"]),
            "parent_event_id": r["parent_event_id"],
            "derived_version": r["derived_version"],
        }

    # -------------------------------------------------------------- versions
    def head(self, branch: str = "main") -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM branch_heads WHERE branch=?",
                                (branch,)).fetchone()
        return dict(row) if row else None

    def save_version(self, *, source_uid: str, source_revision: int,
                     branch: str, event: Event, params: dict,
                     doc: dict, derived: dict, anomalies: list[dict],
                     base_version: Optional[int], actor="reviewer") -> dict:
        """Atomically persist derivation and move the branch pointer.

        Raises ConflictError when ``base_version`` is stale.
        """
        import time
        with _LOCK:
            cur = self.head(branch)
            current_version = cur["current_version"] if cur else 0
            if base_version is not None and cur is not None and \
                    current_version != base_version:
                raise ConflictError(
                    f"branch {branch} moved to v{current_version}; "
                    f"save was based on v{base_version}",
                    current_version, base_version)
            seq = self._next_seq(branch)
            event.seq = seq
            event.branch = branch
            event.base_version = current_version
            cur2 = self.conn.execute(
                "INSERT INTO deriv_versions(source_uid,source_revision,"
                "branch,event_id,status,params,doc_json,derived_json,"
                "anomalies_json,created_at) VALUES(?,?,?,?,'building',"
                "?,?,?,?,?)",
                (source_uid, source_revision, branch, event.id,
                 json.dumps(params),
                 json.dumps(doc, ensure_ascii=False),
                 json.dumps(derived, ensure_ascii=False),
                 json.dumps(anomalies, ensure_ascii=False), time.time()))
            version = cur2.lastrowid
            self.conn.execute(
                "INSERT INTO events(id,seq,branch,base_version,timestamp,"
                "actor,kind,payload,rule_refs,adopted,rejected,"
                "affected_uids,parent_event_id,derived_version) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (event.id, seq, branch, current_version, event.timestamp,
                 event.actor, event.kind,
                 json.dumps(event.payload, ensure_ascii=False),
                 json.dumps(event.rule_refs),
                 json.dumps(event.adopted),
                 json.dumps(event.rejected),
                 json.dumps(event.affected_uids),
                 event.parent_event_id, version))
            self.conn.execute(
                "UPDATE deriv_versions SET status='complete' WHERE version=?",
                (version,))
            self.conn.execute(
                "INSERT INTO branch_heads(branch,source_uid,source_revision,"
                "current_version,event_seq) VALUES(?,?,?,?,?) "
                "ON CONFLICT(branch) DO UPDATE SET source_uid=excluded.source_uid,"
                "source_revision=excluded.source_revision,"
                "current_version=excluded.current_version,"
                "event_seq=excluded.event_seq",
                (branch, source_uid, source_revision, version, seq))
            self.conn.commit()
            return {"version": version, "seq": seq,
                    "event_id": event.id}

    def get_version(self, version: int) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM deriv_versions WHERE version=? AND status='complete'",
            (version,)).fetchone()
        return self._version_row(row) if row else None

    def get_event_snapshot(self, event_id: str) -> Optional[dict]:
        """Restore the exact dependency versions and visible conclusions at a
        past event; never overwrite history with the latest recomputation."""
        ev = self.get_event(event_id)
        if ev is None:
            return None
        ver = ev.get("derived_version")
        snap = self.get_version(ver) if ver else None
        return {"event": ev, "version": snap}

    def list_versions(self, branch: Optional[str] = None) -> list[dict]:
        if branch:
            rows = self.conn.execute(
                "SELECT version,source_uid,source_revision,branch,event_id,"
                "status,created_at FROM deriv_versions WHERE branch=? AND "
                "status='complete' ORDER BY version", (branch,)).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT version,source_uid,source_revision,branch,event_id,"
                "status,created_at FROM deriv_versions WHERE status='complete' "
                "ORDER BY version").fetchall()
        return [dict(r) for r in rows]

    def list_branches(self):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM branch_heads ORDER BY branch")]

    def create_branch(self, name: str, from_branch: str = "main"):
        with _LOCK:
            src = self.head(from_branch)
            if src is None:
                raise KeyError(from_branch)
            if self.head(name) is not None:
                raise KeyError(f"branch {name} exists")
            self.conn.execute(
                "INSERT INTO branch_heads(branch,source_uid,source_revision,"
                "current_version,event_seq) VALUES(?,?,?,?,?)",
                (name, src["source_uid"], src["source_revision"],
                 src["current_version"], src["event_seq"]))
            self.conn.commit()
        return self.head(name)

    @staticmethod
    def _version_row(r) -> dict:
        return {
            "version": r["version"],
            "source_uid": r["source_uid"],
            "source_revision": r["source_revision"],
            "branch": r["branch"],
            "event_id": r["event_id"],
            "status": r["status"],
            "params": json.loads(r["params"]),
            "doc": json.loads(r["doc_json"]),
            "derived": json.loads(r["derived_json"]),
            "anomalies": json.loads(r["anomalies_json"]),
            "created_at": r["created_at"],
        }

    # ------------------------------------------------------------- decisions
    def put_decision(self, key: str, payload: dict):
        with _LOCK:
            self.conn.execute(
                "INSERT INTO decisions(key,payload) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload",
                (key, json.dumps(payload, ensure_ascii=False)))
            self.conn.commit()

    def get_decision(self, key: str):
        row = self.conn.execute("SELECT payload FROM decisions WHERE key=?",
                                (key,)).fetchone()
        return json.loads(row["payload"]) if row else None

    # crash simulation ------------------------------------------------------
    def simulate_interrupted_write(self, *, source_uid: str,
                                   source_revision: int, branch: str) -> int:
        """Insert a half-finished 'building' version and never move the
        pointer, exactly as a process killed mid-write would leave it."""
        import time
        with _LOCK:
            cur = self.conn.execute(
                "INSERT INTO deriv_versions(source_uid,source_revision,"
                "branch,event_id,status,params,doc_json,derived_json,"
                "anomalies_json,created_at) VALUES(?,?,?,?,'building',"
                "'{}',NULL,NULL,NULL,?)",
                (source_uid, source_revision, branch,
                 f"ev:crashed-{uuid.uuid4().hex[:8]}", time.time()))
            self.conn.commit()
            return cur.lastrowid

    def building_versions(self):
        return [dict(r) for r in self.conn.execute(
            "SELECT version,branch,status FROM deriv_versions "
            "WHERE status!='complete'")]

    # ------------------------------------------------------- export / import
    def export_bundle(self) -> dict:
        """Portable archive: events, versions (with provenance), source bytes,
        decisions and branch pointers. Stable uids are preserved verbatim."""
        def rows(sql, args=()):
            return [dict(r) for r in self.conn.execute(sql, args)]
        return {
            "format": "mkv-timecode-court/v1",
            "schema_version": SCHEMA_VERSION,
            "sources": rows("SELECT * FROM sources"),
            "source_revisions": [
                {**{k: v for k, v in r.items() if k != "data"},
                 "data_hex": r["data"].hex()}
                for r in self.conn.execute(
                    "SELECT source_uid,revision,data,sha256,kind,"
                    "changed_direct_source,created_at FROM source_revisions")],
            "events": [self._event_row(r) for r in self.conn.execute(
                "SELECT * FROM events ORDER BY seq, id")],
            "versions": [self._version_row(r) for r in self.conn.execute(
                "SELECT * FROM deriv_versions WHERE status='complete' "
                "ORDER BY version")],
            "branch_heads": rows("SELECT * FROM branch_heads"),
            "decisions": rows("SELECT * FROM decisions"),
        }

    @classmethod
    def import_bundle(cls, path: str, bundle: dict) -> "Store":
        """Load an exported bundle into a fresh database file. Identities,
        ordering and provenance relationships are restored exactly."""
        store = cls(path)
        c = store.conn
        with _LOCK:
            for s in bundle["sources"]:
                c.execute("INSERT INTO sources VALUES(?,?,?,?,?)",
                          (s["source_uid"], s["sha256"], s["byte_length"],
                           s["imported_at"], s["label"]))
            for r in bundle["source_revisions"]:
                c.execute("INSERT INTO source_revisions VALUES(?,?,?,?,?,?,?)",
                          (r["source_uid"], r["revision"],
                           bytes.fromhex(r["data_hex"]), r["sha256"],
                           r["kind"], r["changed_direct_source"],
                           r["created_at"]))
            for ev in bundle["events"]:
                c.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,"
                          "?,?)",
                          (ev["id"], ev["seq"], ev["branch"],
                           ev["base_version"], ev["timestamp"], ev["actor"],
                           ev["kind"], json.dumps(ev["payload"]),
                           json.dumps(ev["rule_refs"]),
                           json.dumps(ev["adopted"]),
                           json.dumps(ev["rejected"]),
                           json.dumps(ev["affected_uids"]),
                           ev["parent_event_id"], ev["derived_version"]))
            # Re-insert with explicit version numbers to preserve identity.
            for v in bundle["versions"]:
                c.execute(
                    "INSERT INTO deriv_versions(version,source_uid,"
                    "source_revision,branch,event_id,status,params,doc_json,"
                    "derived_json,anomalies_json,created_at) "
                    "VALUES(?,?,?,?,?,'complete',?,?,?,?,?)",
                    (v["version"], v["source_uid"], v["source_revision"],
                     v["branch"], v["event_id"],
                     json.dumps(v["params"]),
                     json.dumps(v["doc"], ensure_ascii=False),
                     json.dumps(v["derived"], ensure_ascii=False),
                     json.dumps(v["anomalies"], ensure_ascii=False),
                     v["created_at"]))
            c.execute(
                "UPDATE sqlite_sequence SET seq=("
                "SELECT COALESCE(MAX(version),0) FROM deriv_versions) "
                "WHERE name='deriv_versions'")
            for b in bundle["branch_heads"]:
                c.execute("INSERT INTO branch_heads VALUES(?,?,?,?,?)",
                          (b["branch"], b["source_uid"], b["source_revision"],
                           b["current_version"], b["event_seq"]))
            for d in bundle.get("decisions", []):
                c.execute("INSERT INTO decisions VALUES(?,?)",
                          (d["key"], d["payload"]
                           if isinstance(d["payload"], str)
                           else json.dumps(d["payload"], ensure_ascii=False)))
            c.commit()
        return store
