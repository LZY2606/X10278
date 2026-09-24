"""SQLite 存储：原子版本切换、事件账本、分支、冲突与依赖图缓存。

提交协议（commit_revision）：
  在单个 IMMEDIATE 事务里完成 写入 revisions/elements/derivations/events/
  更新 branch_heads；事务结束前 current 指针不切换。
  进程在提交中途崩溃时，未提交事务由 SQLite 回滚（重启只看到旧版本或新版本）。
  crash hook（env MTT_CRASH_AFTER=rows）可在测试中制造行级写入中断。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
SCHEMA_VERSION = 1

_RULE_FP = None


def rule_fingerprint() -> str:
    """对核心规则代码做指纹（规则一变动，派生节点全部换身份）。"""
    global _RULE_FP
    if _RULE_FP is not None:
        return _RULE_FP
    import core.ebml, core.blocks, core.facts, core.domain  # noqa
    h = hashlib.sha1()
    for mod in (core.ebml, core.blocks, core.facts, core.domain):
        p = Path(mod.__file__)
        h.update(p.name.encode())
        h.update(p.read_bytes())
    _RULE_FP = h.hexdigest()
    return _RULE_FP


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _eid(prefix: str, *parts) -> str:
    h = hashlib.sha1()
    for p in parts:
        h.update(p if isinstance(p, bytes) else str(p).encode())
        h.update(b"\0")
    return f"{prefix}_{h.hexdigest()[:16]}"


class ConflictError(Exception):
    """两位使用者从同一版本编辑：可解决的冲突（需要选择合并策略）。"""


class Store:
    def __init__(self, path: str = ":memory:"):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False,
                                    isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self._crash_after = int(os.environ.get("MTT_CRASH_AFTER", "0") or 0)
        self._rows_written = 0
        with self.lock:
            self._init_schema()

    # ------------------------------------------------------------------
    def _init_schema(self):
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        ddl = SCHEMA_PATH.read_text()
        self.conn.executescript(ddl)
        cur = self.conn.execute("SELECT MAX(version) v FROM schema_migrations")
        v = cur.fetchone()["v"]
        if v is None:
            self.conn.execute(
                "INSERT INTO schema_migrations(version) VALUES (?)",
                (SCHEMA_VERSION,))
        elif v < SCHEMA_VERSION:
            self._migrate(v)

    def _migrate(self, from_v: int):
        """离线案例包要求的旧版本迁移钩子（当前 v1，预留）。"""
        for v in range(from_v + 1, SCHEMA_VERSION + 1):
            self.conn.execute(
                "INSERT INTO schema_migrations(version) VALUES (?)", (v,))

    @contextmanager
    def transaction(self):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    def _maybe_crash(self, n: int = 1):
        self._rows_written += n
        if self._crash_after and self._rows_written >= self._crash_after:
            self._crash_after = 0
            os._exit(7)  # 模拟进程被杀：不清理、不提交

    # ------------------------------------------------------------------
    # 文件与片段
    # ------------------------------------------------------------------
    def add_file(self, blob: bytes, filename: str) -> str:
        key = _eid("file", blob)
        with self.transaction() as c:
            c.execute("INSERT OR IGNORE INTO files(file_key,filename,blob,"
                      "byte_length) VALUES(?,?,?,?)",
                      (key, filename, blob, len(blob)))
            self._maybe_crash()
        return key

    def get_file(self, key: str) -> bytes:
        cur = self.conn.execute("SELECT blob FROM files WHERE file_key=?", (key,))
        r = cur.fetchone()
        if not r:
            raise KeyError(key)
        return r["blob"]

    def add_fragment(self, file_key: str, kind: str, label: str,
                     params: dict, blob: bytes) -> str:
        key = _eid("frag", file_key, kind, json.dumps(params, sort_keys=True),
                   blob)
        with self.transaction() as c:
            c.execute("INSERT OR IGNORE INTO fragments(fragment_key,file_key,"
                      "kind,label,params_json,blob,byte_length) "
                      "VALUES(?,?,?,?,?,?,?)",
                      (key, file_key, kind, label,
                       json.dumps(params, ensure_ascii=False), blob, len(blob)))
        return key

    def get_fragment(self, key: str) -> dict:
        r = self.conn.execute("SELECT * FROM fragments WHERE fragment_key=?",
                              (key,)).fetchone()
        return dict(r) if r else None

    # ------------------------------------------------------------------
    # 分支
    # ------------------------------------------------------------------
    def head(self, branch: str = "main") -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM branch_heads WHERE branch=?", (branch,)).fetchone()

    def list_branches(self) -> list[str]:
        return [r["branch"] for r in self.conn.execute(
            "SELECT branch FROM branch_heads ORDER BY branch")]

    def event(self, event_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM events WHERE event_id=?",
                                 (event_id,)).fetchone()

    def revision(self, revision_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM revisions WHERE revision_id=?",
                                 (revision_id,)).fetchone()

    def events_on(self, branch: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM events WHERE branch=? ORDER BY seq", (branch,)))

    # ------------------------------------------------------------------
    # 提交：原子版本切换 + 并发冲突
    # ------------------------------------------------------------------
    def commit_revision(self, *, file_key: str, fragment_key: Optional[str],
                        blob: bytes, kind: str, payload: dict,
                        branch: str = "main", parent_event: Optional[str] = None,
                        actor: str = "reviewer", accepted: bool = True,
                        supersedes: Optional[str] = None,
                        expected_parent: Optional[str] = None,
                        rule_version: str = "rules-v1",
                        elements=None, parse_diags=None,
                        params: Optional[dict] = None) -> dict:
        """原子提交一个新事件+新版本。

        expected_parent：乐观并发控制。两个使用者从同一 head 提交时，第二个
        会收到 ConflictError，可通过重试或显式三向合并解决。
        """
        params = params or {}
        with self.transaction() as c:
            head = c.execute("SELECT event_id FROM branch_heads WHERE branch=?",
                             (branch,)).fetchone()
            cur_parent = head["event_id"] if head else None
            if expected_parent is not None and expected_parent != cur_parent:
                raise ConflictError(
                    f"分支 {branch} 已从 {expected_parent} 前进到 "
                    f"{cur_parent}；请基于最新版本合并后重试")
            if parent_event is None:
                parent_event = cur_parent
            seqrow = c.execute(
                "SELECT COALESCE(MAX(seq),0)+1 s FROM events WHERE branch=?",
                (branch,)).fetchone()
            seq = seqrow["s"]
            revision_id = _eid("rev", file_key, fragment_key or b"-",
                               json.dumps(params, sort_keys=True),
                               rule_version, seq, branch, kind)
            event_id = _eid("evt", revision_id, kind, json.dumps(payload,
                            sort_keys=True, ensure_ascii=False), actor,
                            str(accepted))
            c.execute("INSERT INTO revisions(revision_id,branch,file_key,"
                      "fragment_key,rule_version,params_json) "
                      "VALUES(?,?,?,?,?,?)",
                      (revision_id, branch, file_key, fragment_key,
                       rule_version, json.dumps(params, ensure_ascii=False)))
            self._maybe_crash()
            if elements is not None:
                self._insert_elements(c, revision_id, elements)
            if parse_diags is not None:
                c.executemany(
                    "INSERT INTO parse_diags(revision_id,code,offset_,span_end,"
                    "severity,message) VALUES(?,?,?,?,?,?)",
                    [(revision_id, d["code"], d["offset"], d.get("span_end"),
                      d["severity"], d["message"]) for d in parse_diags])
            c.execute("INSERT INTO events(event_id,revision_id,seq,branch,"
                      "parent_event,kind,payload_json,actor,accepted,"
                      "supersedes) VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (event_id, revision_id, seq, branch, parent_event, kind,
                       json.dumps(payload, ensure_ascii=False), actor,
                       1 if accepted else 0, supersedes))
            self._maybe_crash()
            c.execute("INSERT INTO branch_heads(branch,event_id,revision_id,"
                      "updated_seq) VALUES(?,?,?,?) "
                      "ON CONFLICT(branch) DO UPDATE SET "
                      "event_id=excluded.event_id,"
                      "revision_id=excluded.revision_id,"
                      "updated_seq=excluded.updated_seq",
                      (branch, event_id, revision_id, seq))
            self._maybe_crash()
        return {"event_id": event_id, "revision_id": revision_id,
                "seq": seq, "branch": branch, "parent_event": parent_event}

    def _insert_elements(self, c, revision_id, elements):
        rows = []
        for e in elements:
            rows.append((revision_id, e.key, e.parent_key, e.id, e.name,
                         e.etype, e.unit, e.id_start, e.data_start, e.raw_end,
                         e.declared_size, 1 if e.unknown_size else 0,
                         1 if e.inferred_end else 0, e.depth,
                         None if e.value is None else json.dumps(e.value,
                             ensure_ascii=False, default=str),
                         e.value_raw_hex, json.dumps(e.children)))
        c.executemany(
            "INSERT INTO elements(revision_id,el_key,parent_key,id,name,etype,"
            "unit,id_start,data_start,raw_end,declared_size,unknown_size,"
            "inferred_end,depth,value,value_raw_hex,children_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        self._maybe_crash(len(rows))

    # ------------------------------------------------------------------
    # 派生缓存（依赖图节点，内容寻址）
    # ------------------------------------------------------------------
    def derivation_get(self, kind: str, inputs_hash: str,
                       rule_version: str) -> Optional[dict]:
        fp = rule_fingerprint()
        key = _eid("node", kind, inputs_hash, fp, rule_version)
        r = self.conn.execute(
            "SELECT * FROM derivations WHERE node_key=? AND status='valid'",
            (key,)).fetchone()
        if not r:
            return None
        return {"node_key": key, "result_json": r["result_json"],
                "rule_version": r["rule_version"], "inputs_hash": inputs_hash}

    def derivation_put(self, kind: str, inputs_hash: str, rule_version: str,
                       result: dict) -> str:
        fp = rule_fingerprint()
        key = _eid("node", kind, inputs_hash, fp, rule_version)
        with self.transaction() as c:
            c.execute("INSERT OR IGNORE INTO derivations(node_key,rule_version,"
                      "inputs_hash,kind,result_json) VALUES(?,?,?,?,?)",
                      (key, rule_version, inputs_hash, kind,
                       json.dumps(result, ensure_ascii=False, default=str)))
        return key

    def invalidate_for_rule_change(self):
        with self.transaction() as c:
            c.execute("UPDATE derivations SET status='stale'")

    # ------------------------------------------------------------------
    def revision_elements(self, revision_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM elements WHERE revision_id=? ORDER BY id_start,depth",
            (revision_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["children"] = json.loads(d.pop("children_json"))
            v = d.pop("value")
            d["value"] = json.loads(v) if v is not None else None
            out.append(d)
        return out

    def revision_parse_diags(self, revision_id: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT code,offset_ AS offset,span_end,severity,message "
            "FROM parse_diags WHERE revision_id=?", (revision_id,))]

    def close(self):
        self.conn.close()
