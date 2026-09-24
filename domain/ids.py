"""Stable, deterministic identities for domain objects.

Identities are derived from immutable content/coordinates so that export ->
delete -> import reconstructs exactly the same id, order and provenance.
"""
from __future__ import annotations

import hashlib
from typing import Any, Mapping, Sequence


def _stable_repr(value: Any) -> str:
    if isinstance(value, Mapping):
        return "{" + ",".join(f"{k}:{_stable_repr(value[k])}" for k in sorted(value)) + "}"
    if isinstance(value, (list, tuple, Sequence)) and not isinstance(value, (str, bytes)):
        return "[" + ",".join(_stable_repr(v) for v in value) + "]"
    if isinstance(value, bytes):
        return "b" + value.hex()
    return repr(value)


def digest(prefix: str, *parts: Any) -> str:
    payload = "\x1f".join(_stable_repr(p) for p in parts)
    h = hashlib.sha256(payload.encode("utf-8", "surrogatepass")).hexdigest()
    return f"{prefix}_{h[:16]}"


def element_id(file_id: str, parent_path: str, index: int, eid: str, start: int) -> str:
    return digest("el", file_id, parent_path, index, eid, start)


def track_id(file_id: str, track_number: int, start: int) -> str:
    return digest("tr", file_id, track_number, start)


def cluster_id(file_id: str, start: int) -> str:
    return digest("cl", file_id, start)


def block_id(file_id: str, start: int) -> str:
    return digest("bk", file_id, start)


def frame_id(file_id: str, block_start: int, index: int) -> str:
    return digest("fr", file_id, block_start, index)


def cue_id(file_id: str, start: int) -> str:
    return digest("cue", file_id, start)


def node_id(version: str, kind: str, target: str) -> str:
    return digest("nd", version, kind, target)


def version_id(parent: str | None, event: str, seq: int) -> str:
    return digest("ver", parent or "ROOT", event, seq)
