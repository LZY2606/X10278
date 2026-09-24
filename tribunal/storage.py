"""SQLite event-sourced storage with atomic version materialization.

Invariants:
  * every derivation is an independent version row;
  * the branch current pointer moves only inside the same transaction that
    successfully writes the complete view (old version or full new version
    after a crash);
  * saves carry an optimistic-lock base version; two users editing the
    same base get one SaveConflict that resolves into two sibling versions.
"""
from __future__ import annotations

import gzip
import json
import sqlite3
import threading
import time
from dataclasses import dataclass

from .analysis import Document
from .derive import Context, derive

SCHEMA_VERSION = 2
LOCK = threading.RLock()


class SaveConflict(Exception):
    def __init__(self, message: str, base_version: int, current: int):
        super().__init__(message)
        self.base_version = base_version
        self.current = current


@dataclass
class Event:
    event_id: int
    case_id: str
    branch: str
    seq: int
    kind: str
    payload: dict
    parent_event: int | None
    rule_refs: list[str]
    actor: str
    created_at: float
    result_status: str
    affected: list[str]
    input_fragment: dict | None


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def user_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn: sqlite3.Connection) -> None:
    version = user_version(conn)
    if version > SCHEMA_VERSION:
        raise RuntimeError(f"schema v{version} newer than v{SCHEMA_VERSION}")
    if version == 0 and _looks_like_legacy_v1(conn):
        _migrate_v1_to_v2(conn)
        version = SCHEMA_VERSION
    if user_version(conn) < SCHEMA_VERSION:
        _create_schema(conn)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.commit()


def _looks_like_legacy_v1(conn: sqlite3.Connection) -> bool:
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    return {"legacy_facts", "legacy_pointers"} <= tables


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    # Legacy v1 held a mutable facts table; lift it into one imported
    # version without claiming derivations it never recorded.
    with conn:
        conn.execute("CREATE TABLE IF NOT EXISTS cases ("
                     "case_id TEXT PRIMARY KEY, created_at REAL)")
        conn.execute("CREATE TABLE IF NOT EXISTS sources ("
                     "case_id TEXT, branch TEXT, kind TEXT, name TEXT, "
                     "sha256 TEXT, content BLOB, media_type TEXT, "
                     "PRIMARY KEY(case_id, branch, kind, name))")
        conn.execute("CREATE TABLE IF NOT EXISTS events ("
                     "event_id INTEGER PRIMARY KEY AUTOINCREMENT, "
                     "case_id TEXT, branch TEXT, seq INTEGER, kind TEXT, "
                     "payload TEXT, parent_event INTEGER, rule_refs TEXT, "
                     "actor TEXT, created_at REAL, result_status TEXT, "
                     "affected TEXT, input_fragment TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS versions ("
                     "version_id INTEGER PRIMARY KEY AUTOINCREMENT, "
                     "case_id TEXT, branch TEXT, seq INTEGER, "
                     "event_id INTEGER, base_version INTEGER, "
                     "doc_json BLOB, view_json BLOB, created_at REAL)")
        conn.execute("CREATE TABLE IF NOT EXISTS pointers ("
                     "case_id TEXT, branch TEXT, version_id INTEGER, "
                     "PRIMARY KEY(case_id, branch))")
        rows = conn.execute("SELECT case_id, fact_json FROM legacy_facts").fetchall()
        now = time.time()
        for case_id, fact_json in rows:
            conn.execute("INSERT OR IGNORE INTO cases VALUES (?,?)",
                         (case_id, now))
            eid = conn.execute(
                "INSERT INTO events(case_id,branch,seq,kind,payload,"
                "parent_event,rule_refs,actor,created_at,result_status,"
                "affected,input_fragment) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (case_id, "main", 0, "import-legacy",
                 json.dumps({"note": "migrated from legacy v1"}), None,
                 json.dumps([]), "migration", now, "adopted",
                 json.dumps([]), None)).lastrowid
            doc = {"legacy_facts": json.loads(fact_json)}
            vid = conn.execute(
                "INSERT INTO versions(case_id,branch,seq,event_id,"
                "base_version,doc_json,view_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (case_id, "main", 0, eid, None,
                 gzip.compress(json.dumps(doc).encode()),
                 gzip.compress(b"{}"), now)).lastrowid
            conn.execute(
                "INSERT OR REPLACE INTO pointers VALUES(?,?,?)",
                (case_id, "main", vid))
        conn.execute("DROP TABLE legacy_facts")
        conn.execute("DROP TABLE legacy_pointers")


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS cases (
        case_id TEXT PRIMARY KEY,
        created_at REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS sources (
        case_id TEXT NOT NULL,
        branch TEXT NOT NULL,
        kind TEXT NOT NULL,
        name TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        content BLOB,
        media_type TEXT,
        PRIMARY KEY (case_id, branch, kind, name));
    CREATE TABLE IF NOT EXISTS events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        case_id TEXT NOT NULL,
        branch TEXT NOT NULL,
        seq INTEGER NOT NULL,
        kind TEXT NOT NULL,
        payload TEXT NOT NULL,
        parent_event INTEGER,
        rule_refs TEXT NOT NULL,
        actor TEXT NOT NULL,
        created_at REAL NOT NULL,
        result_status TEXT NOT NULL,
        affected TEXT NOT NULL,
        input_fragment TEXT);
    CREATE TABLE IF NOT EXISTS versions (
        version_id INTEGER PRIMARY KEY AUTOINCREMENT,
        case_id TEXT NOT NULL,
        branch TEXT NOT NULL,
        seq INTEGER NOT NULL,
        event_id INTEGER NOT NULL,
        base_version INTEGER,
        doc_json BLOB NOT NULL,
        view_json BLOB NOT NULL,
        created_at REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS pointers (
        case_id TEXT NOT NULL,
        branch TEXT NOT NULL,
        version_id INTEGER NOT NULL,
        PRIMARY KEY (case_id, branch));
    """)


def _gz(data: bytes | str) -> bytes:
    return gzip.compress(data if isinstance(data, bytes) else data.encode())


def _gun(data: bytes) -> str:
    return gzip.decompress(data).decode()


class Store:
    def __init__(self, path: str):
        self.path = path
        self.conn = connect(path)
        migrate(self.conn)

    # ------------------------------------------------------------- reads

    def cases(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT c.case_id, c.created_at, "
            " (SELECT COUNT(*) FROM events e WHERE e.case_id=c.case_id) ev "
            "FROM cases c ORDER BY c.created_at").fetchall()
        out = []
        for r in rows:
            pointers = self.conn.execute(
                "SELECT branch, version_id FROM pointers WHERE case_id=?",
                (r["case_id"],)).fetchall()
            out.append({"case_id": r["case_id"], "created_at": r["created_at"],
                        "event_count": r["ev"],
                        "pointers": {p["branch"]: p["version_id"]
                                     for p in pointers}})
        return out

    def branches(self, case_id: str) -> list[str]:
        return [r[0] for r in self.conn.execute(
            "SELECT DISTINCT branch FROM events WHERE case_id=? ORDER BY branch",
            (case_id,)).fetchall()]

    def current_version_id(self, case_id: str, branch: str) -> int | None:
        row = self.conn.execute(
            "SELECT version_id FROM pointers WHERE case_id=? AND branch=?",
            (case_id, branch)).fetchone()
        return row[0] if row else None

    def version(self, version_id: int) -> dict:
        r = self.conn.execute(
            "SELECT * FROM versions WHERE version_id=?",
            (version_id,)).fetchone()
        return self._version_row(r)

    def _version_row(self, r: sqlite3.Row) -> dict:
        return {"version_id": r["version_id"], "case_id": r["case_id"],
                "branch": r["branch"], "seq": r["seq"],
                "event_id": r["event_id"], "base_version": r["base_version"],
                "doc": json.loads(_gun(r["doc_json"])),
                "view": json.loads(_gun(r["view_json"])),
                "created_at": r["created_at"]}

    def current(self, case_id: str, branch: str) -> dict | None:
        vid = self.current_version_id(case_id, branch)
        return self.version(vid) if vid else None

    def events(self, case_id: str, branch: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM events WHERE case_id=? AND branch=? ORDER BY seq",
            (case_id, branch)).fetchall()
        return [self._event_row(r) for r in rows]

    def _event_row(self, r: sqlite3.Row) -> dict:
        return {"event_id": r["event_id"], "case_id": r["case_id"],
                "branch": r["branch"], "seq": r["seq"], "kind": r["kind"],
                "payload": json.loads(r["payload"]),
                "parent_event": r["parent_event"],
                "rule_refs": json.loads(r["rule_refs"]),
                "actor": r["actor"], "created_at": r["created_at"],
                "result_status": r["result_status"],
                "affected": json.loads(r["affected"]),
                "input_fragment": (json.loads(r["input_fragment"])
                                   if r["input_fragment"] else None)}

    def sources(self, case_id: str, branch: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT kind, name, sha256, media_type, length(content) n "
            "FROM sources WHERE case_id=? AND branch=?",
            (case_id, branch)).fetchall()
        return [dict(r) for r in rows]

    def source_bytes(self, case_id: str, branch: str, kind: str,
                    name: str) -> bytes | None:
        r = self.conn.execute(
            "SELECT content FROM sources WHERE case_id=? AND branch=? "
            "AND kind=? AND name=?", (case_id, branch, kind, name)).fetchone()
        return r[0] if r else None

    # ------------------------------------------------------------ writes

    def _next_seq(self, case_id: str, branch: str) -> int:
        r = self.conn.execute(
            "SELECT COALESCE(MAX(seq), -1) + 1 FROM events "
            "WHERE case_id=? AND branch=?", (case_id, branch)).fetchone()
        return r[0]

    def init_case(self, case_id: str, name: str, mkv: bytes,
                 sidecar: dict, actor: str = "importer") -> int:
        from .analysis import parse_matroska
        with LOCK:
            if self.conn.execute("SELECT 1 FROM cases WHERE case_id=?",
                                (case_id,)).fetchone():
                raise ValueError(f"case {case_id} already exists")
            doc = parse_matroska(mkv, sidecar).as_dict()
            now = time.time()
            with self.conn:
                self.conn.execute("INSERT INTO cases VALUES(?,?)",
                                  (case_id, now))
                self._put_source(case_id, "main", "container", name, mkv,
                                "video/x-matroska")
                self._put_source(case_id, "main", "sidecar",
                                name + ".frames.json",
                                json.dumps(sidecar, ensure_ascii=False,
                                           indent=2).encode(),
                                "application/json")
                seq = 0
                view = derive(_rehydrate(doc), Context(
                    scale_ns=doc["timecode_scale_ns"],
                    source="container"))
                event_payload = {
                    "name": name,
                    "source_sha256": doc["source_sha256"],
                    "byte_length": doc["byte_length"],
                    "summary": _doc_summary(doc)}
                eid = self._insert_event(
                    case_id, "main", seq, "import", event_payload, None,
                    ["R-UNIT-RAW", "R-UNKNOWN-LENGTH"], actor, now,
                    "adopted", _affected_keys({}, view),
                    {"container": len(mkv), "sidecar":
                     len(json.dumps(sidecar))})
                vid = self._insert_version(case_id, "main", seq, eid, None,
                                          doc, view, now)
                self.conn.execute(
                    "INSERT OR REPLACE INTO pointers VALUES(?,?,?)",
                    (case_id, "main", vid))
            return vid

    def _put_source(self, case_id, branch, kind, name, content, media_type):
        import hashlib
        self.conn.execute(
            "INSERT INTO sources(case_id,branch,kind,name,sha256,content,"
            "media_type) VALUES(?,?,?,?,?,?,?)",
            (case_id, branch, kind, name,
             hashlib.sha256(content).hexdigest(),
             content, media_type))

    def _insert_event(self, case_id, branch, seq, kind, payload, parent_event,
                      rule_refs, actor, now, status, affected, fragment):
        cur = self.conn.execute(
            "INSERT INTO events(case_id,branch,seq,kind,payload,parent_event,"
            "rule_refs,actor,created_at,result_status,affected,"
            "input_fragment) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (case_id, branch, seq, kind, json.dumps(payload),
             parent_event, json.dumps(rule_refs), actor, now,
             status, json.dumps(affected),
             json.dumps(fragment) if fragment is not None else None))
        return cur.lastrowid

    def _insert_version(self, case_id, branch, seq, event_id, base, doc,
                         view, now):
        return self.conn.execute(
            "INSERT INTO versions(case_id,branch,seq,event_id,base_version,"
            "doc_json,view_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (case_id, branch, seq, event_id, base,
             _gz(json.dumps(doc)), _gz(json.dumps(view)), now)).lastrowid

    def save_version(self, case_id: str, branch: str, kind: str,
                     payload: dict, rule_refs: list[str], actor: str,
                     doc_mutator, status: str = "adopted",
                     input_fragment: dict | None = None,
                     base_version: int | None = None,
                     crash_after_doc: bool = False) -> int:
        """Generic transaction: event + recompute + version + pointer switch.

        crash_after_doc simulates a crash between the doc snapshot write and
        the view/pointer write; the pointer must still point at the old
        version afterwards.
        """
        from .analysis import document_from_dict
        with LOCK:
            ptr = self.current_version_id(case_id, branch)
            if ptr is None:
                raise KeyError(f"no such branch {case_id}/{branch}")
            if base_version is not None and base_version != ptr:
                raise SaveConflict(
                    f"base version {base_version} is stale; current is {ptr}",
                    base_version, ptr)
            current = self.version(ptr)
            doc = current["doc"]
            doc = json.loads(json.dumps(doc))
            doc_mutator(doc, payload)
            now = time.time()
            seq = self._next_seq(case_id, branch)
            with self.conn:
                eid = self._insert_event(
                    case_id, branch, seq, kind, payload,
                    current["event_id"], rule_refs, actor, now, status,
                    [], input_fragment)
                scale = payload.get("timecode_scale_ns",
                                   doc["timecode_scale_ns"])
                source = (f"decision:{eid}"
                          if kind == "decision" else "container")
                view = derive(document_from_dict(doc),
                             Context(scale_ns=scale, source=source,
                                     decided_event=eid))
                affected = _affected_keys(current["view"], view)
                self.conn.execute(
                    "UPDATE events SET affected=? WHERE event_id=?",
                    (json.dumps(affected), eid))
                vid = self._insert_version(
                    case_id, branch, seq, eid, ptr, doc, view, now)
                if crash_after_doc:
                    self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    raise _CrashSimulated()
                self.conn.execute(
                    "INSERT OR REPLACE INTO pointers VALUES(?,?,?)",
                    (case_id, branch, vid))
            return vid

    def create_branch(self, case_id: str, name: str, from_branch: str,
                      kind: str, payload: dict, rule_refs: list[str],
                      actor: str, doc_mutator,
                      input_fragment: dict | None = None,
                      status: str = "adopted") -> int:
        from .analysis import document_from_dict
        with LOCK:
            if name in self.branches(case_id):
                raise ValueError(f"branch {name} already exists")
            base = self.current(case_id, from_branch)
            doc = json.loads(json.dumps(base["doc"]))
            doc_mutator(doc, payload)
            now = time.time()
            base_seq = self.conn.execute(
                "SELECT COALESCE(MAX(seq),-1) FROM events WHERE case_id=?",
                (case_id,)).fetchone()[0] + 1
            with self.conn:
                eid = self._insert_event(
                    case_id, name, 0, kind, payload, base["event_id"],
                    rule_refs, actor, now, status, [], input_fragment)
                view = derive(document_from_dict(doc), Context(
                    scale_ns=doc["timecode_scale_ns"], source="container"))
                affected = _affected_keys(base["view"], view)
                self.conn.execute(
                    "UPDATE events SET affected=? WHERE event_id=?",
                    (json.dumps(affected), eid))
                for source in self.conn.execute(
                        "SELECT kind,name,content,media_type FROM sources "
                        "WHERE case_id=? AND branch=?",
                        (case_id, from_branch)).fetchall():
                    self._put_source(case_id, name, source["kind"],
                                   source["name"], source["content"],
                                   source["media_type"])
                vid = self._insert_version(
                    case_id, name, 0, eid, base["version_id"], doc, view, now)
                self.conn.execute(
                    "INSERT INTO pointers VALUES(?,?,?)",
                    (case_id, name, vid))
            return vid

    def version_at_event(self, case_id: str, event_id: int) -> int | None:
        r = self.conn.execute(
            "SELECT version_id FROM versions WHERE case_id=? AND event_id=?",
            (case_id, event_id)).fetchone()
        return r["version_id"] if r else None


class _CrashSimulated(Exception):
    pass


def _affected_keys(old_view: dict, new_view: dict) -> list[str]:
    old = {n["key"]: n for n in old_view.get("nodes", [])}
    new = {n["key"]: n for n in new_view.get("nodes", [])}
    keys = sorted((set(old) | set(new)))
    changed = [k for k in keys if old.get(k) != new.get(k)]
    return changed


def _doc_summary(doc: dict) -> dict:
    return {
        "tracks": len(doc["tracks"]),
        "clusters": len(doc["clusters"]),
        "blocks": sum(c["block_count"] for c in doc["clusters"]),
        "cues": len(doc["cues"]),
        "frame_rows": len(doc["frames"]),
        "diagnostics": len(doc["diagnostics"]),
    }


def _rehydrate(doc: dict):
    from .analysis import document_from_dict
    return document_from_dict(doc)

    # ----------------------------------------------------- export/import

    def export_case(self, case_id: str) -> dict:
        events = []
        for branch in self.branches(case_id):
            events += self.events(case_id, branch)
        versions = []
        rows = self.conn.execute(
            "SELECT * FROM versions WHERE case_id=? ORDER BY version_id",
            (case_id,)).fetchall()
        for r in rows:
            versions.append({"version_id": r["version_id"],
                            "branch": r["branch"], "seq": r["seq"],
                            "event_id": r["event_id"],
                            "base_version": r["base_version"],
                            "doc": json.loads(_gun(r["doc_json"])),
                            "view": json.loads(_gun(r["view_json"])),
                            "created_at": r["created_at"]})
        sources = []
        for branch in self.branches(case_id):
            for src in self.sources(case_id, branch):
                content = self.source_bytes(case_id, branch, src["kind"],
                                           src["name"])
                sources.append({"branch": branch, "kind": src["kind"],
                                "name": src["name"], "sha256": src["sha256"],
                                "media_type": src["media_type"],
                                "content_b64": content.hex()
                                if src["media_type"] == "video/x-matroska"
                                else content.decode()})
        pointers = {r["branch"]: r["version_id"] for r in self.conn.execute(
            "SELECT branch, version_id FROM pointers WHERE case_id=?",
            (case_id,)).fetchall()}
        case = self.conn.execute("SELECT * FROM cases WHERE case_id=?",
                                 (case_id,)).fetchone()
        return {
            "format": "tribunal-bundle/1",
            "case_id": case_id,
            "created_at": case["created_at"],
            "events": events,
            "versions": versions,
            "sources": sources,
            "pointers": pointers,
        }

    def import_case(self, bundle: dict, new_case_id: str | None = None) -> str:
        if bundle.get("format") != "tribunal-bundle/1":
            raise ValueError("unsupported bundle format")
        case_id = new_case_id or bundle["case_id"]
        with LOCK:
            if self.conn.execute("SELECT 1 FROM cases WHERE case_id=?",
                                (case_id,)).fetchone():
                raise ValueError(f"case {case_id} already exists")
            with self.conn:
                self.conn.execute("INSERT INTO cases VALUES(?,?)",
                                  (case_id, bundle["created_at"]))
                for src in bundle["sources"]:
                    if src["media_type"] == "video/x-matroska":
                        content = bytes.fromhex(src["content_b64"])
                    else:
                        content = src["content_b64"].encode()
                    self.conn.execute(
                        "INSERT INTO sources VALUES(?,?,?,?,?,?,?)",
                        (case_id, src["branch"], src["kind"], src["name"],
                         src["sha256"], content, src["media_type"]))
                for ev in bundle["events"]:
                    self.conn.execute(
                        "INSERT INTO events(event_id,case_id,branch,seq,kind,"
                        "payload,parent_event,rule_refs,actor,created_at,"
                        "result_status,affected,input_fragment) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (ev["event_id"], case_id, ev["branch"], ev["seq"],
                         ev["kind"], json.dumps(ev["payload"]),
                         ev["parent_event"], json.dumps(ev["rule_refs"]),
                         ev["actor"], ev["created_at"], ev["result_status"],
                         json.dumps(ev["affected"]),
                         json.dumps(ev["input_fragment"])
                         if ev["input_fragment"] is not None else None))
                for v in bundle["versions"]:
                    self.conn.execute(
                        "INSERT INTO versions(version_id,case_id,branch,seq,"
                        "event_id,base_version,doc_json,view_json,created_at)"
                        " VALUES(?,?,?,?,?,?,?,?,?)",
                        (v["version_id"], case_id, v["branch"], v["seq"],
                         v["event_id"], v["base_version"],
                         _gz(json.dumps(v["doc"])),
                         _gz(json.dumps(v["view"])), v["created_at"]))
                for branch, vid in bundle["pointers"].items():
                    self.conn.execute(
                        "INSERT INTO pointers VALUES(?,?,?)",
                        (case_id, branch, vid))
            return case_id

    def close(self) -> None:
        self.conn.close()
