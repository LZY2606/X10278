-- Matroska 时码庭存储模型
-- 设计要点：
-- 1) files/elements 保存原始字节事实（偏移/未知长度/原字节范围），永不被
--    派生层或人工决定覆盖；
-- 2) events 是只追加账本，每次输入、推导、参数修订、人工决定都成为事件；
-- 3) derivations 是依赖图节点缓存（内容寻址），规则版本 + 输入指纹决定节点；
-- 4) revisions 是每次提交的完整快照指针，commit 原子切换 current_revision；
-- 5) branch_heads / branch_bases 支撑分支与回到任意旧事件的重放。

PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS files (
    file_key      TEXT PRIMARY KEY,   -- sha1(原始字节)
    filename      TEXT NOT NULL,
    blob          BLOB NOT NULL,
    byte_length   INTEGER NOT NULL,
    imported_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS fragments (
    fragment_key  TEXT PRIMARY KEY,   -- sha1(源 file_key + kind + 参数)
    file_key      TEXT NOT NULL REFERENCES files(file_key),
    kind          TEXT NOT NULL,      -- unknown_len / rel_tc_overflow /
                                      -- lace_corrupt / discard_padding /
                                      -- cue_mispoint / timebase_diff / cluster_gap
    label         TEXT NOT NULL,
    params_json   TEXT NOT NULL,
    blob          BLOB NOT NULL,
    byte_length   INTEGER NOT NULL,
    created_event TEXT
);

CREATE TABLE IF NOT EXISTS elements (
    revision_id   TEXT NOT NULL REFERENCES revisions(revision_id),
    el_key        TEXT NOT NULL,
    parent_key    TEXT,
    id            INTEGER NOT NULL,
    name          TEXT NOT NULL,
    etype         TEXT NOT NULL,
    unit          TEXT,
    id_start      INTEGER NOT NULL,
    data_start    INTEGER NOT NULL,
    raw_end       INTEGER NOT NULL,
    declared_size INTEGER,
    unknown_size  INTEGER NOT NULL,
    inferred_end  INTEGER NOT NULL,
    depth         INTEGER NOT NULL,
    value         TEXT,
    value_raw_hex TEXT,
    children_json TEXT NOT NULL,
    PRIMARY KEY (revision_id, el_key)
);

CREATE TABLE IF NOT EXISTS parse_diags (
    revision_id TEXT NOT NULL REFERENCES revisions(revision_id),
    code TEXT NOT NULL, offset_ INTEGER NOT NULL,
    span_end INTEGER, severity TEXT NOT NULL, message TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rules (
    rule_version TEXT PRIMARY KEY,
    description  TEXT NOT NULL,
    fingerprint  TEXT NOT NULL,       -- sha1(规则代码内容)
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS derivations (
    node_key      TEXT NOT NULL,      -- 内容寻址：规则指纹+依赖指纹
    rule_version  TEXT NOT NULL,
    inputs_hash   TEXT NOT NULL,
    kind          TEXT NOT NULL,      -- facts / timeline
    result_json   TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'valid', -- valid / stale
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (node_key)
);

CREATE TABLE IF NOT EXISTS events (
    event_id      TEXT PRIMARY KEY,
    revision_id   TEXT NOT NULL UNIQUE REFERENCES revisions(revision_id),
    seq           INTEGER NOT NULL,
    branch        TEXT NOT NULL,
    parent_event  TEXT,
    kind          TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    actor         TEXT NOT NULL DEFAULT 'reviewer',
    accepted      INTEGER NOT NULL DEFAULT 1,  -- 被采纳/未采纳都保留
    supersedes    TEXT,                      -- 否决事件指向的事件
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS revisions (
    revision_id   TEXT PRIMARY KEY,
    branch        TEXT NOT NULL,
    file_key      TEXT NOT NULL,
    fragment_key  TEXT REFERENCES fragments(fragment_key),
    rule_version  TEXT NOT NULL,
    params_json   TEXT NOT NULL,      -- scale_override 等
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS branch_heads (
    branch        TEXT PRIMARY KEY,
    event_id      TEXT NOT NULL,
    revision_id   TEXT NOT NULL,
    updated_seq   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS branch_bases (
    branch        TEXT NOT NULL,
    base_event    TEXT NOT NULL,
    PRIMARY KEY (branch, base_event)
);

CREATE INDEX IF NOT EXISTS idx_events_branch ON events(branch, seq);
CREATE INDEX IF NOT EXISTS idx_elements_rev ON elements(revision_id);
