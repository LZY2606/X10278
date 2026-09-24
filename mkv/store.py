"""版本化存储层 (SQLite)。

语义:
- 每次推导/裁定产生独立版本; 版本、节点、事件在同一事务内写入,
  只有完整成功后才移动 head 指针 -> 写入中断后重启, 要么看到旧版本,
  要么看到完整新版本。
- 节点身份 (node id) 跨版本稳定; 同一 id 在不同版本可携带不同 payload。
- 从同一版本出发的两个提交产生冲突: 第二个提交被拒绝(ConflictError),
  可显式落为分支, 之后用解决事件选择继续哪一版; 未选分支保留。
- schema_version 记录于 meta 表, 打开旧库时自动迁移。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import hashlib

SCHEMA_VERSION = 2

SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS versions (
    id TEXT PRIMARY KEY, parent TEXT, label TEXT, created REAL);
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT, version_id TEXT, kind TEXT, status TEXT,
    payload TEXT, deps TEXT, provenance TEXT,
    PRIMARY KEY (id, version_id));
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT UNIQUE, version_id TEXT, kind TEXT, payload TEXT, created REAL);
CREATE TABLE IF NOT EXISTS heads (name TEXT PRIMARY KEY, version_id TEXT);
CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY, kind TEXT, blob BLOB, sha TEXT);
"""


class ConflictError(Exception):
    def __init__(self, head: str, base: str):
        self.head, self.base = head, base
        super().__init__(f"head 已前进: 当前 {head}, 提交基于 {base}")


class Store:
    def __init__(self, path: str):
        self.path = path
        first = not os.path.exists(path) or os.path.getsize(path) == 0
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        if first:
            self.db.executescript(SCHEMA_V2)
            self.db.execute("INSERT OR REPLACE INTO meta VALUES('schema_version',?)",
                            (str(SCHEMA_VERSION),))
            self.db.commit()
        else:
            self._migrate()

    def _migrate(self):
        self.db.executescript(SCHEMA_V2)
        row = self.db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        ver = int(row["value"]) if row else 1
        if ver < 2:
            # v1 -> v2: nodes 表增加 provenance 列
            cols = [r["name"] for r in self.db.execute("PRAGMA table_info(nodes)")]
            if "provenance" not in cols:
                self.db.execute("ALTER TABLE nodes ADD COLUMN provenance TEXT DEFAULT '{}'")
            self.db.execute("INSERT OR REPLACE INTO meta VALUES('schema_version','2')")
            self.db.commit()

    def close(self):
        self.db.close()

    # ---------- 读取 ----------
    def head(self, name: str = "main") -> str | None:
        row = self.db.execute("SELECT version_id FROM heads WHERE name=?", (name,)).fetchone()
        return row["version_id"] if row else None

    def heads(self) -> dict:
        return {r["name"]: r["version_id"] for r in self.db.execute("SELECT * FROM heads")}

    def versions(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM versions ORDER BY created, id")]

    def nodes_at(self, version_id: str) -> dict[str, dict]:
        out = {}
        for r in self.db.execute("SELECT * FROM nodes WHERE version_id=? ORDER BY id",
                                 (version_id,)):
            out[r["id"]] = {"id": r["id"], "kind": r["kind"], "status": r["status"],
                            "payload": json.loads(r["payload"]),
                            "deps": json.loads(r["deps"]),
                            "provenance": json.loads(r["provenance"] or "{}")}
        return out

    def events(self, upto_seq: int | None = None) -> list[dict]:
        sql = "SELECT * FROM events"
        args: tuple = ()
        if upto_seq is not None:
            sql += " WHERE seq<=?"
            args = (upto_seq,)
        sql += " ORDER BY seq"
        return [{"seq": r["seq"], "id": r["id"], "version_id": r["version_id"],
                 "kind": r["kind"], "payload": json.loads(r["payload"]),
                 "created": r["created"]}
                for r in self.db.execute(sql, args)]

    def event_by_seq(self, seq: int) -> dict | None:
        for e in self.events():
            if e["seq"] == seq:
                return e
        return None

    def put_artifact(self, aid: str, kind: str, blob: bytes):
        sha = hashlib.sha256(blob).hexdigest()
        self.db.execute("INSERT OR REPLACE INTO artifacts VALUES(?,?,?,?)",
                        (aid, kind, blob, sha))
        self.db.commit()
        return sha

    def get_artifact(self, aid: str) -> bytes | None:
        r = self.db.execute("SELECT blob FROM artifacts WHERE id=?", (aid,)).fetchone()
        return r["blob"] if r else None

    def artifacts(self) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT id, kind, sha, length(blob) AS size FROM artifacts ORDER BY id")]

    # ---------- 写入 ----------
    def _next_id(self, table: str, prefix: str) -> str:
        if table == "versions":
            n = len(self.versions()) + 1
        else:
            n = len(self.events()) + 1
        return f"{prefix}{n:06d}"

    def commit(self, base: str | None, label: str,
               nodes: dict[str, dict], events: list[dict],
               head: str = "main", branch: bool = False) -> str:
        """原子提交一个版本。base 与当前 head 不一致时:
        branch=False -> 抛 ConflictError; branch=True -> 落到 heads['branch:<label>']。
        """
        cur_head = self.head(head)
        if cur_head != base:
            if not branch:
                raise ConflictError(cur_head, base)
            head = f"branch:{label}"
        vid = self._next_id("versions", "v")
        now = time.time()
        try:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute("INSERT INTO versions VALUES(?,?,?,?)",
                            (vid, base, label, now))
            for nid, node in nodes.items():
                self.db.execute(
                    "INSERT INTO nodes VALUES(?,?,?,?,?,?,?)",
                    (nid, vid, node["kind"], node.get("status", "ok"),
                     json.dumps(node["payload"], ensure_ascii=False, sort_keys=True),
                     json.dumps(node.get("deps", []), ensure_ascii=False),
                     json.dumps(node.get("provenance", {}), ensure_ascii=False)))
            for ev in events:
                eid = ev.get("id") or self._next_id("events", "e")
                self.db.execute(
                    "INSERT INTO events(id,version_id,kind,payload,created) VALUES(?,?,?,?,?)",
                    (eid, vid, ev["kind"],
                     json.dumps(ev["payload"], ensure_ascii=False, sort_keys=True), now))
            # 只有全部写入成功后才移动 head 指针
            self.db.execute("INSERT OR REPLACE INTO heads VALUES(?,?)", (head, vid))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return vid

    def resolve_conflict(self, chosen: str, discarded: str, reason: str,
                         head: str = "main") -> str:
        """记录冲突解决事件(挂在被选版本上)并把 head 指向它; 未选版本保留。"""
        eid = self._next_id("events", "e")
        try:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute(
                "INSERT INTO events(id,version_id,kind,payload,created) VALUES(?,?,?,?,?)",
                (eid, chosen, "conflict_resolution",
                 json.dumps({"chosen": chosen, "discarded": discarded,
                             "reason": reason,
                             "rule": "unselected_candidate_kept"},
                            ensure_ascii=False), time.time()))
            self.db.execute("INSERT OR REPLACE INTO heads VALUES(?,?)", (head, chosen))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return eid
