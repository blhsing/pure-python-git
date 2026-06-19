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


_NO_NEWLINE = "\\ No newline at end of file"


def _group_hunks(ops: list[tuple[str, int, int]], context: int) -> list[tuple[int, int]]:
    """Return (start, end) op-index spans for each hunk, with up to ``context``
    lines of surrounding context, merging hunks <= 2*context equal lines apart."""
    change_idx = [i for i, op in enumerate(ops) if op[0] != "eq"]
    if not change_idx:
        return []
    spans: list[tuple[int, int]] = []
    i = 0
    while i < len(change_idx):
        j = i
        while j + 1 < len(change_idx) and change_idx[j + 1] - change_idx[j] - 1 <= 2 * context:
            j += 1
        start = max(0, change_idx[i] - context)
        end = min(len(ops), change_idx[j] + 1 + context)
        spans.append((start, end))
        i = j + 1
    return spans


def _funcname_heading(a: list[str], scan_from: int) -> str:
    """C Git's default hunk 'function' heading: the nearest preceding line whose
    first character is a letter, '_' or '$' (xdiff's default find-function)."""
    j = scan_from
    while j >= 0:
        line = a[j]
        if line and (line[0].isalpha() or line[0] in "_$"):
            return line.rstrip()
        j -= 1
    return ""


def _hunk_range(start: int, count: int) -> str:
    if count == 1:
        return str(start)
    return f"{start},{count}"


def format_hunks(
    a: list[str],
    b: list[str],
    context: int = 3,
    *,
    a_no_newline: bool = False,
    b_no_newline: bool = False,
) -> list[str]:
    """Render git-style unified hunks (``@@`` headers + body) for two line
    lists, including ``\\ No newline at end of file`` markers."""
    ops = diff_lines(a, b)
    out: list[str] = []
    last_a = len(a) - 1
    last_b = len(b) - 1
    # Prefix counts of a/b lines consumed before each op, for zero-length range
    # numbering (insertions/deletions show the adjacent old/new line number).
    a_cons = [0] * (len(ops) + 1)
    b_cons = [0] * (len(ops) + 1)
    for i, (k, _ai, _bi) in enumerate(ops):
        a_cons[i + 1] = a_cons[i] + (1 if k in ("eq", "del") else 0)
        b_cons[i + 1] = b_cons[i] + (1 if k in ("eq", "ins") else 0)
    for start, end in _group_hunks(ops, context):
        hunk = ops[start:end]
        a_idx = [ai for k, ai, _ in hunk if k in ("eq", "del")]
        b_idx = [bi for k, _, bi in hunk if k in ("eq", "ins")]
        a_count = len(a_idx)
        b_count = len(b_idx)
        a_start = a_idx[0] + 1 if a_idx else a_cons[start]
        b_start = b_idx[0] + 1 if b_idx else b_cons[start]
        # git appends the nearest preceding "function" line as a section heading.
        first_a = a_idx[0] if a_idx else a_cons[start]
        heading = _funcname_heading(a, first_a - 1)
        hdr = f"@@ -{_hunk_range(a_start, a_count)} +{_hunk_range(b_start, b_count)} @@"
        if heading:
            hdr += " " + heading
        out.append(hdr)
        for kind, ai, bi in hunk:
            if kind == "eq":
                out.append(" " + a[ai])
                if a_no_newline and ai == last_a:
                    out.append(_NO_NEWLINE)
            elif kind == "del":
                out.append("-" + a[ai])
                if a_no_newline and ai == last_a:
                    out.append(_NO_NEWLINE)
            else:
                out.append("+" + b[bi])
                if b_no_newline and bi == last_b:
                    out.append(_NO_NEWLINE)
    return out


# ---------------------------------------------------------------------------
# word-diff (git diff --word-diff[=plain]) — port of diff.c:diff_words_show


def _split_words(text: str) -> list[tuple[int, int]]:
    """Tokenize ``text`` into (begin, end) offsets of words — maximal runs of
    non-whitespace, exactly as git's whitespace word-splitter (newlines, being
    whitespace, bound words)."""
    res: list[tuple[int, int]] = []
    i = 0
    n = len(text)
    while i < n:
        while i < n and text[i].isspace():
            i += 1
        if i >= n:
            break
        j = i + 1
        while j < n and not text[j].isspace():
            j += 1
        res.append((i, j))
        i = j
    return res


def _ww(text: str, prefix: str, suffix: str) -> str:
    """Mirror diff.c:fn_out_diff_words_write_helper for plain mode (newline
    style "\\n"): wrap each non-empty newline-delimited segment with the
    prefix/suffix, leaving the newlines themselves bare."""
    out: list[str] = []
    parts = text.split("\n")
    for idx, seg in enumerate(parts):
        if seg != "":
            out.append(prefix + seg + suffix)
        if idx < len(parts) - 1:
            out.append("\n")
    return "".join(out)


def _word_render(minus_text: str, plus_text: str) -> str:
    """Render one hunk's word-level diff. Common text (and all whitespace) comes
    from the plus side; removed words are wrapped [-..-], added words {+..+}."""
    if not plus_text:
        return _ww(minus_text, "[-", "-]")
    wm = _split_words(minus_text)
    wp = _split_words(plus_text)
    ops = diff_lines([minus_text[b:e] for b, e in wm],
                     [plus_text[b:e] for b, e in wp])
    out: list[str] = []
    current_plus = 0
    mi = pi = 0
    i = 0
    n = len(ops)
    while i < n:
        if ops[i][0] == "eq":
            mi += 1
            pi += 1
            i += 1
            continue
        del_idx: list[int] = []
        ins_idx: list[int] = []
        while i < n and ops[i][0] != "eq":
            if ops[i][0] == "del":
                del_idx.append(mi)
                mi += 1
            else:
                ins_idx.append(pi)
                pi += 1
            i += 1
        minus_end_off = minus_text[wm[del_idx[-1]][1]:wm[del_idx[-1]][1] + 1] if del_idx else ""
        if ins_idx:
            plus_begin = wp[ins_idx[0]][0]
            plus_end = wp[ins_idx[-1]][1]
            del_lo = wm[del_idx[0]][0] if del_idx else 0
            del_hi = wm[del_idx[-1]][1] if del_idx else 0
        else:
            # Pure deletion. A *whole-line* deletion (the removed words are
            # bounded by newlines on both sides in the minus side) takes its
            # trailing newline and consumes the next plus word's leading newline
            # as context, so it lands on its own line — matching git. An in-line
            # deletion keeps the removed words on the surviving line.
            d0 = wm[del_idx[0]][0]
            dn = wm[del_idx[-1]][1]
            before = minus_text[d0 - 1] if d0 > 0 else "\n"
            after = minus_text[dn] if dn < len(minus_text) else "\n"
            del_lo, del_hi = d0, dn
            if before == "\n" and after == "\n":
                del_hi = dn + 1  # include the deleted line's newline
                plus_begin = plus_end = (wp[pi][0] if pi < len(wp) else len(plus_text))
            else:
                plus_begin = plus_end = (wp[pi - 1][1] if pi > 0 else 0)
        out.append(_ww(plus_text[current_plus:plus_begin], "", ""))
        if del_idx:
            out.append(_ww(minus_text[del_lo:del_hi], "[-", "-]"))
        if ins_idx:
            out.append(_ww(plus_text[wp[ins_idx[0]][0]:wp[ins_idx[-1]][1]], "{+", "+}"))
        current_plus = plus_end
    out.append(_ww(plus_text[current_plus:], "", ""))
    return "".join(out)


def word_diff_hunks(a: list[str], b: list[str], context: int = 3) -> str:
    """Return the git ``--word-diff=plain`` body (``@@`` headers + word-diffed
    content) for two line lists."""
    ops = diff_lines(a, b)
    a_cons = [0] * (len(ops) + 1)
    b_cons = [0] * (len(ops) + 1)
    for i, (k, _ai, _bi) in enumerate(ops):
        a_cons[i + 1] = a_cons[i] + (1 if k in ("eq", "del") else 0)
        b_cons[i + 1] = b_cons[i] + (1 if k in ("eq", "ins") else 0)
    chunks: list[str] = []
    for start, end in _group_hunks(ops, context):
        hunk = ops[start:end]
        a_idx = [ai for k, ai, _ in hunk if k in ("eq", "del")]
        b_idx = [bi for k, _, bi in hunk if k in ("eq", "ins")]
        a_count = len(a_idx)
        b_count = len(b_idx)
        a_start = a_idx[0] + 1 if a_idx else a_cons[start]
        b_start = b_idx[0] + 1 if b_idx else b_cons[start]
        first_a = a_idx[0] if a_idx else a_cons[start]
        heading = _funcname_heading(a, first_a - 1)
        hdr = f"@@ -{_hunk_range(a_start, a_count)} +{_hunk_range(b_start, b_count)} @@"
        if heading:
            hdr += " " + heading
        minus_text = "".join(a[ai] + "\n" for k, ai, _bi in hunk if k in ("eq", "del"))
        plus_text = "".join(b[bi] + "\n" for k, _ai, bi in hunk if k in ("eq", "ins"))
        chunks.append(hdr + "\n" + _word_render(minus_text, plus_text))
    return "".join(chunks)


def unified_diff(a_text: str, b_text: str, a_label: str = "a", b_label: str = "b", context: int = 3) -> str:
    a = a_text.splitlines()
    b = b_text.splitlines()
    if a == b:
        return ""
    body = format_hunks(
        a, b, context,
        a_no_newline=bool(a_text) and not a_text.endswith("\n"),
        b_no_newline=bool(b_text) and not b_text.endswith("\n"),
    )
    if not body:
        return ""
    return "\n".join([f"--- {a_label}", f"+++ {b_label}", *body]) + "\n"
