"""依赖图传播：只重算受影响节点，其余标记为复用（可解释"为何仍有效"）。"""
from __future__ import annotations


class Graph:
    def __init__(self):
        self.nodes: dict[str, dict] = {}

    def add(self, key: str, deps: list[str], fn):
        self.nodes[key] = {"deps": list(deps), "fn": fn}

    def children_map(self) -> dict[str, list[str]]:
        children: dict[str, list[str]] = {k: [] for k in self.nodes}
        for key, node in self.nodes.items():
            for dep in node["deps"]:
                children.setdefault(dep, []).append(key)
        return children

    def affected(self, changed: set[str]) -> set[str]:
        """changed 的反向可达闭包（含 changed 自身）。"""
        children = self.children_map()
        seen: set[str] = set()
        stack = list(changed)
        while stack:
            key = stack.pop()
            if key in seen:
                continue
            seen.add(key)
            stack.extend(children.get(key, []))
        return seen

    def topo(self, keys: set[str]) -> list[str]:
        order: list[str] = []
        state: dict[str, int] = {}

        def visit(k: str):
            if state.get(k) == 2:
                return
            if state.get(k) == 1:
                raise ValueError(f"依赖环: {k}")
            state[k] = 1
            for dep in self.nodes[k]["deps"]:
                if dep in keys:
                    visit(dep)
            state[k] = 2
            order.append(k)

        for k in sorted(keys):
            if k in self.nodes:
                visit(k)
        return order

    def run(self, changed: set[str], cache: dict, ctx):
        """重算受影响节点；cache 含上一版本全部节点值。

        返回 (values, computed, reused)。reused 节点即"仍然有效"的结果。
        """
        affected = self.affected(changed)
        computed: set[str] = set()
        values = dict(cache)

        def get(k):
            return values[k]

        for key in self.topo(affected):
            if key not in self.nodes:
                continue
            values[key] = self.nodes[key]["fn"](get, ctx)
            computed.add(key)
        reused = set(cache) & (set(self.nodes) - affected)
        return values, computed, reused
