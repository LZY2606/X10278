"""稳定身份生成。

所有领域对象的身份都是内容寻址的短哈希（sha1 截断），
保证：同一来源字节 + 同一规则版本 => 同一身份；
导出/删除数据库/再导入后身份不变。
"""
from __future__ import annotations

import hashlib
import json


def canon(obj) -> str:
    """规范化 JSON：键排序、紧凑分隔、UTF-8。用于哈希与存储。"""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def h(*parts) -> str:
    """对若干部分做 sha1，取前 16 个十六进制字符作为稳定短身份。"""
    digest = hashlib.sha1()
    for part in parts:
        if isinstance(part, (dict, list)):
            part = canon(part)
        if isinstance(part, str):
            part = part.encode("utf-8")
        digest.update(part)
        digest.update(b"\x00")
    return digest.hexdigest()[:16]


def element_id(source_sha: str, offset: int) -> str:
    return h("element", source_sha, str(offset))


def block_id(source_sha: str, offset: int) -> str:
    return h("block", source_sha, str(offset))


def frame_id(block_id_: str, index: int) -> str:
    return h("frame", block_id_, str(index))


def cue_id(source_sha: str, offset: int) -> str:
    return h("cue", source_sha, str(offset))


def derived_id(node_key: str, version_no: int) -> str:
    return h("derived", node_key, str(version_no))


def event_id(version_no: int, seq: int, kind: str, subject: str) -> str:
    return h("event", str(version_no), str(seq), kind, subject)


def version_id(parent_version_id: str | None, event_ids: list[str]) -> str:
    return h("version", parent_version_id or "root", *event_ids)


def record_id(kind: str, *parts) -> str:
    return h("record", kind, *[str(p) for p in parts])
