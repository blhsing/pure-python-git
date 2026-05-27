"""Merge-base computation and three-way merge.

Algorithm mirrors git/commit-reach.c paint_down_to_common: BFS from both
sides with PARENT1/PARENT2 flags; a commit reachable from both is a
candidate, and its ancestors are marked STALE so they don't become bases
themselves.
"""
from __future__ import annotations

import heapq
from dataclasses import dataclass
from typing import Optional

from . import objects as objs
from .repo import Repository


PARENT1 = 1
PARENT2 = 2
STALE = 4
RESULT = 8


def _commit_time(repo: Repository, sha: str) -> int:
    t, data = objs.read_object(repo, sha)
    if t != "commit":
        return 0
    c = objs.parse_commit(data)
    parts = c.committer.rsplit(" ", 2)
    try:
        return int(parts[-2])
    except (ValueError, IndexError):
        return 0


def _parents(repo: Repository, sha: str) -> list[str]:
    t, data = objs.read_object(repo, sha)
    if t != "commit":
        return []
    return objs.parse_commit(data).parents


def merge_bases(repo: Repository, a: str, b: str) -> list[str]:
    if a == b:
        return [a]
    flags: dict[str, int] = {a: PARENT1, b: PARENT2}
    # max-heap by commit time via negative
    pq: list[tuple[int, str]] = []
    heapq.heappush(pq, (-_commit_time(repo, a), a))
    heapq.heappush(pq, (-_commit_time(repo, b), b))
    result: list[str] = []
    while pq:
        # check if any non-stale remain with both flags possible
        if all((flags[s] & STALE) for _, s in pq):
            break
        _, sha = heapq.heappop(pq)
        f = flags.get(sha, 0)
        if (f & (PARENT1 | PARENT2)) == (PARENT1 | PARENT2):
            if not (f & RESULT):
                f |= RESULT
                result.append(sha)
            f |= STALE
        flags[sha] = f
        carry = f & (PARENT1 | PARENT2 | STALE)
        for p in _parents(repo, sha):
            pf = flags.get(p, 0)
            if (pf & carry) == carry:
                continue
            flags[p] = pf | carry
            heapq.heappush(pq, (-_commit_time(repo, p), p))
    # filter out stale results
    return [s for s in result if not (flags.get(s, 0) & STALE) or (flags.get(s, 0) & RESULT)]


def is_ancestor(repo: Repository, ancestor: str, descendant: str) -> bool:
    if ancestor == descendant:
        return True
    bases = merge_bases(repo, ancestor, descendant)
    return ancestor in bases


# ---------------------------------------------------------------------------
# three-way blob merge (line-based)


def _split(text: bytes) -> list[bytes]:
    if not text:
        return []
    return text.splitlines(keepends=True)


def merge_blob(base: bytes, ours: bytes, theirs: bytes) -> tuple[bytes, bool]:
    """Return (merged, had_conflict). Uses a simple LCS-based 3-way merge."""
    if ours == theirs:
        return ours, False
    if base == ours:
        return theirs, False
    if base == theirs:
        return ours, False

    a = _split(base)
    o = _split(ours)
    t = _split(theirs)

    # Diff base->ours and base->theirs as edits, then merge by hunks.
    from .diff import diff_lines
    ops_o = diff_lines(a, o)
    ops_t = diff_lines(a, t)

    # Build maps from base index -> aligned lines on each side.
    def align(ops, side):
        out = [None] * (len(a) + 1)  # for each base position, list of side lines aligned
        groups: list[tuple[int, int, list[bytes]]] = []  # (base_start, base_end, side_lines)
        cur_start = 0
        cur_end = 0
        cur_lines: list[bytes] = []
        changed = False
        i = 0
        while i < len(ops):
            kind, ai, bi = ops[i]
            if kind == "eq":
                if changed:
                    groups.append((cur_start, cur_end, cur_lines))
                    changed = False
                cur_lines = []
                cur_start = ai + 1
                cur_end = ai + 1
                i += 1
                continue
            # collect contiguous non-eq
            grp_start = ai if kind == "del" else cur_end
            grp_end = grp_start
            grp_lines: list[bytes] = []
            while i < len(ops) and ops[i][0] != "eq":
                k, aii, bii = ops[i]
                if k == "del":
                    grp_end = aii + 1
                elif k == "ins":
                    grp_lines.append(side[bii])
                i += 1
            if grp_end < grp_start:
                grp_end = grp_start
            # find base range
            base_start = grp_start
            base_end = grp_end
            groups.append((base_start, base_end, grp_lines))
        return groups

    g_o = align(ops_o, o)
    g_t = align(ops_t, t)

    # Walk base linearly merging changes. For overlapping ranges that disagree, emit conflict.
    out = bytearray()
    pos = 0
    i_o = i_t = 0
    conflict = False
    while pos <= len(a):
        # Find next change that starts at >= pos on either side
        next_o = g_o[i_o] if i_o < len(g_o) else None
        next_t = g_t[i_t] if i_t < len(g_t) else None
        # advance any group whose range ended before pos (shouldn't happen, but safe)
        if next_o and next_o[1] < pos:
            i_o += 1
            continue
        if next_t and next_t[1] < pos:
            i_t += 1
            continue
        # next event position
        no_start = next_o[0] if next_o else len(a) + 1
        nt_start = next_t[0] if next_t else len(a) + 1
        ev = min(no_start, nt_start)
        # emit untouched base lines [pos:ev]
        out += b"".join(a[pos:ev])
        pos = ev
        if pos > len(a):
            break
        # gather all overlapping groups
        o_grp = next_o if next_o and next_o[0] == pos else None
        t_grp = next_t if next_t and next_t[0] == pos else None
        # Expand overlap until both sides converge
        if o_grp and t_grp:
            o_end = o_grp[1]
            t_end = t_grp[1]
            o_lines = list(o_grp[2])
            t_lines = list(t_grp[2])
            i_o += 1
            i_t += 1
            # consume any further groups overlapping the union range
            while True:
                changed = False
                while i_o < len(g_o) and g_o[i_o][0] < max(o_end, t_end):
                    o_end = max(o_end, g_o[i_o][1])
                    o_lines.extend(g_o[i_o][2])
                    i_o += 1
                    changed = True
                while i_t < len(g_t) and g_t[i_t][0] < max(o_end, t_end):
                    t_end = max(t_end, g_t[i_t][1])
                    t_lines.extend(g_t[i_t][2])
                    i_t += 1
                    changed = True
                if not changed:
                    break
            end = max(o_end, t_end)
            if o_lines == t_lines:
                out += b"".join(o_lines)
            else:
                conflict = True
                out += b"<<<<<<< ours\n"
                out += b"".join(o_lines)
                out += b"=======\n"
                out += b"".join(t_lines)
                out += b">>>>>>> theirs\n"
            pos = end
        elif o_grp:
            out += b"".join(o_grp[2])
            pos = o_grp[1]
            i_o += 1
        elif t_grp:
            out += b"".join(t_grp[2])
            pos = t_grp[1]
            i_t += 1
        else:
            break
    return bytes(out), conflict
