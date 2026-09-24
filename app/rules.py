"""规则引擎：每个派生节点纯函数。返回 (value, events)。

裁定语义（事件 detail.rule 字段）：
- R1: relative timecode 按 int16 有符号位宽解释；异常位型保留候选，不静默修正。
- R2: lacing 损坏不猜帧边界，整块降级为单帧 + 诊断。
- R3: cue 是索引证据而非时间真值；错指保留为未决异常，人工接受与否都会产生事件。
- R4: 多轨不按文件顺序强对齐；每轨独立时间线，回退只在轨内。
- R5: cluster 缺口按相邻关键块间距中位数 * gap_factor（默认 2.0）判定。
- R6: discard padding 单位为 ns（Matroska 规定 DiscardPadding 直接是 ns 有符号值）。
"""
from __future__ import annotations

import hashlib
from statistics import median

from . import ids
from .ebml import parse as ebml_parse
from .matroska import parse_block, parse_cue_point, quantity, tcq, nsq

RULES_VERSION = "1.0.0"


def _evt(kind: str, subject: str, rule: str, **detail) -> dict:
    return {"kind": kind, "subject": id(subject) if False else subject,
            "detail": {"rule": rule, "rules_version": RULES_VERSION, **detail}}


def _walk(node_list):
    for node in node_list:
        yield node
        yield from _walk(node.get("children", []))


def _children(node, name):
    return [c for无法 := None for c in node.get("children", []) if c["name"] == name]
