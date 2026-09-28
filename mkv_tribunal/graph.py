"""重算依赖图：源数据/参数变化时只传播到受影响的推导节点。

节点：
  ebml-tree        <- source bytes
  facts            <- ebml-tree
  timelines        <- facts + scale
  lacing           <- facts(各 block)
  cues             <- facts + 参数 cue_tolerance_ticks
  gaps             <- facts + 参数 gap_threshold_ns
  track-timebases  <- facts + scale

传播规则在 EDGES 中声明；下游脏节点逐个产生新版本，未受影响节点
保留原版本与“仍有效”原因。
"""

from __future__ import annotations

from dataclasses import dataclass

from . import RULE_VERSION


NODES = (
    "ebml-tree",
    "facts",
    "timelines",
    "lacing",
    "cues",
    "gaps",
    "track-timebases",
)

EDGES: dict[str, frozenset[str]] = {
    "source": frozenset({"ebml-tree"}),
    "ebml-tree": frozenset({"facts"}),
    "facts": frozenset({"timelines", "lacing", "cues", "gaps",
                        "track-timebases"}),
    "param:gap_threshold_ns": frozenset({"gaps"}),
    "param:cue_tolerance_ticks": frozenset({"cues"}),
    "param:scale_override": frozenset({"timelines", "cues", "gaps",
                                       "track-timebases"}),
}


@dataclass
class Propagation:
    changed_inputs: list[str]
    dirty: list[str]
    kept: list[str]

    def explain(self) -> dict[str, list[dict[str, str]]]:
        return {
            "recomputed": [
                {"node": n,
                 "reason": "上游 %s 变化沿依赖图传播到本节点"
                           % ",".join(sorted(
                               _upstream(n, set(self.changed_inputs))))}
                for n in self.dirty],
            "kept": [
                {"node": n,
                 "reason": "无被改输入可到达本节点，依赖哈希与规则版本 "
                           + RULE_VERSION + " 均未变"}
                for n in self.kept],
        }


def _upstream(node: str, changed: set[str]) -> set[str]:
    hits: set[str] = set()
    for source, targets in EDGES.items():
        if node in targets and source in changed:
            hits.add(source)
    return hits


def propagate(changed_inputs: list[str]) -> Propagation:
    dirty: set[str] = set()
    frontier = set(changed_inputs)
    while frontier:
        nxt: set[str] = set()
        for item in frontier:
            for target in EDGES.get(item, frozenset()):
                if target not in dirty:
                    dirty.add(target)
                    nxt.add(target)
        frontier = nxt
    kept = [n for n in NODES if n not in dirty]
    ordered_dirty = [n for n in NODES if n in dirty]
    return Propagation(changed_inputs, ordered_dirty, kept)


def input_for_parameter(name: str) -> str:
    return "param:" + name
