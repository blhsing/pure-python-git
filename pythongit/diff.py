"""Myers diff (O(ND)) and unified diff formatting."""
from __future__ import annotations

from typing import Iterable


def _shortest_edit(a: list, b: list) -> list[tuple[int, int]]:
    n, m = len(a), len(b)
    max_d = n + m
    if max_d == 0:
        return []
    v: dict[int, int] = {1: 0}
    trace: list[dict[int, int]] = []
    for d in range(max_d + 1):
        trace.append(dict(v))
        for k in range(-d, d + 1, 2):
            if k == -d or (k != d and v.get(k - 1, -1) < v.get(k + 1, -1)):
                x = v.get(k + 1, 0)
            else:
                x = v.get(k - 1, 0) + 1
            y = x - k
            while x < n and y < m and a[x] == b[y]:
                x += 1
                y += 1
            v[k] = x
            if x >= n and y >= m:
                return trace
    return trace


def _backtrack(trace: list[dict[int, int]], a: list, b: list):
    x, y = len(a), len(b)
    for d in range(len(trace) - 1, -1, -1):
        v = trace[d]
        k = x - y
        if k == -d or (k != d and v.get(k - 1, -1) < v.get(k + 1, -1)):
            prev_k = k + 1
        else:
            prev_k = k - 1
        prev_x = v.get(prev_k, 0)
        prev_y = prev_x - prev_k
        while x > prev_x and y > prev_y:
            yield ("eq", x - 1, y - 1)
            x -= 1
            y -= 1
        if d > 0:
            if x == prev_x:
                yield ("ins", -1, prev_y)
            else:
                yield ("del", prev_x, -1)
        x, y = prev_x, prev_y


def diff_lines(a: list[str], b: list[str]) -> list[tuple[str, int, int]]:
    trace = _shortest_edit(a, b)
    ops = list(_backtrack(trace, a, b))
    ops.reverse()
    return ops


def unified_diff(a_text: str, b_text: str, a_label: str = "a", b_label: str = "b", context: int = 3) -> str:
    a = a_text.splitlines()
    b = b_text.splitlines()
    ops = diff_lines(a, b)
    if not ops:
        return ""
    if not any(k != "eq" for k, *_ in ops):
        return ""

    # group into hunks
    hunks: list[list[tuple[str, int, int]]] = []
    cur: list[tuple[str, int, int]] = []
    last_change = -1
    for i, op in enumerate(ops):
        if op[0] != "eq":
            if not cur:
                start = max(0, i - context)
                cur = list(ops[start:i])
            cur.append(op)
            last_change = i
        else:
            if cur and i - last_change <= context:
                cur.append(op)
            elif cur and i - last_change == context + 1:
                cur.append(op)
                hunks.append(cur)
                cur = []
    if cur:
        # add trailing context
        end = min(len(ops), last_change + 1 + context)
        cur.extend(ops[last_change + 1 : end])
        hunks.append(cur)

    out = [f"--- {a_label}", f"+++ {b_label}"]
    for h in hunks:
        a_start = next((x for k, x, _ in h if k in ("eq", "del")), 0)
        b_start = next((y for k, _, y in h if k in ("eq", "ins")), 0)
        a_count = sum(1 for k, *_ in h if k in ("eq", "del"))
        b_count = sum(1 for k, *_ in h if k in ("eq", "ins"))
        out.append(f"@@ -{a_start + 1},{a_count} +{b_start + 1},{b_count} @@")
        for kind, ai, bi in h:
            if kind == "eq":
                out.append(" " + a[ai])
            elif kind == "del":
                out.append("-" + a[ai])
            else:
                out.append("+" + b[bi])
    return "\n".join(out) + "\n"
