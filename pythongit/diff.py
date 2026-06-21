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


def _shortest_edit_keyed(a: list, b: list) -> list[dict[int, int]]:
    """Myers shortest-edit over arbitrary comparable keys (used so whitespace-
    insensitive comparison can diff normalized keys while output uses originals).
    """
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


def diff_lines_keyed(akeys: list, bkeys: list) -> list[tuple[str, int, int]]:
    """Like :func:`diff_lines` but diffs on the supplied key sequences, returning
    ops whose indices refer to the original (same-length) line lists."""
    trace = _shortest_edit_keyed(akeys, bkeys)
    ops = list(_backtrack(trace, akeys, bkeys))
    ops.reverse()
    return ops


# --- whitespace normalization (port of xdiff/xutils.c:xdl_recmatch) ----------
# Each flag normalizes a line to a canonical key such that two lines compare
# equal under the flag iff their keys are equal.

def _ws_key(line: str, *, ignore_all: bool, ignore_change: bool,
            ignore_eol: bool, ignore_cr_eol: bool) -> str:
    """Canonical key for a line under the active whitespace-ignore flags. The
    line text here excludes the trailing newline (the diff works on split
    lines). Whitespace chars per xdiff XDL_ISSPACE: space, \\t, \\n, \\v, \\f,
    \\r."""
    if ignore_all:
        # remove all whitespace
        return "".join(c for c in line if not _isspace(c))
    if ignore_change:
        # collapse each run of whitespace to a single space, drop leading/
        # trailing runs entirely (xdiff -b: matching spaces are skipped).
        out = []
        i = 0
        n = len(line)
        while i < n:
            if _isspace(line[i]):
                while i < n and _isspace(line[i]):
                    i += 1
                if i < n:  # internal whitespace -> single space
                    out.append(" ")
            else:
                out.append(line[i])
                i += 1
        return "".join(out)
    if ignore_eol:
        # trailing whitespace ignored
        return line.rstrip(" \t\n\v\f\r")
    if ignore_cr_eol:
        # only a single trailing CR is ignored
        return line[:-1] if line.endswith("\r") else line
    return line


def _isspace(c: str) -> bool:
    return c in " \t\n\v\f\r"


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


def _group_hunks_ic(ops, context: int, inter_hunk_context: int):
    """Like :func:`_group_hunks` but with an explicit inter-hunk context: hunks
    are merged when separated by <= ``2*context + inter_hunk_context`` equal
    lines (xdiff's ``xdl_emit_diff`` interhunkctxlen rule)."""
    change_idx = [i for i, op in enumerate(ops) if op[0] != "eq"]
    if not change_idx:
        return []
    spans = []
    gap = 2 * context + inter_hunk_context
    i = 0
    while i < len(change_idx):
        j = i
        while j + 1 < len(change_idx) and change_idx[j + 1] - change_idx[j] - 1 <= gap:
            j += 1
        start = max(0, change_idx[i] - context)
        end = min(len(ops), change_idx[j] + 1 + context)
        spans.append((start, end))
        i = j + 1
    return spans


def _expand_function_context(a: list[str], spans, ops, a_cons):
    """Port of xdiff XDL_EMIT_FUNCNAMES (-W): grow each hunk's pre-context up to
    the preceding function line and its post-context down to just before the
    next function line."""
    out = []
    for (start, end) in spans:
        # find the first a-line index covered by this hunk
        first_a = None
        for k, ai, _bi in ops[start:end]:
            if k in ("eq", "del"):
                first_a = ai
                break
        if first_a is None:
            first_a = a_cons[start]
        # walk op index back until the op whose a-line is a function line
        s = start
        while s > 0:
            k, ai, _bi = ops[s - 1]
            s -= 1
            if k in ("eq", "del") and ai >= 0 and _is_func_line(a[ai]):
                break
        # extend forward: include context up to (not including) next func line
        e = end
        while e < len(ops):
            k, ai, _bi = ops[e]
            if k in ("eq", "del") and ai >= 0 and _is_func_line(a[ai]):
                break
            e += 1
        out.append((s, e))
    # merge overlapping/adjacent spans produced by expansion
    merged = []
    for sp in out:
        if merged and sp[0] <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], sp[1]))
        else:
            merged.append(sp)
    return merged


def _is_func_line(line: str) -> bool:
    return bool(line) and (line[0].isalpha() or line[0] in "_$")


def format_hunks_ex(
    a: list[str],
    b: list[str],
    context: int = 3,
    *,
    a_no_newline: bool = False,
    b_no_newline: bool = False,
    ignore_all_space: bool = False,
    ignore_space_change: bool = False,
    ignore_space_at_eol: bool = False,
    ignore_cr_at_eol: bool = False,
    ignore_blank_lines: bool = False,
    ignore_regex=None,        # compiled regex (str) or None: -I
    inter_hunk_context: int = 0,
    func_context: bool = False,
    new_ind: str = "+",
    old_ind: str = "-",
    ctx_ind: str = " ",
) -> list[str]:
    """Render unified hunks with the full set of diff-options applied. Returns a
    list of lines (without trailing newlines, like :func:`format_hunks`)."""
    ws = ignore_all_space or ignore_space_change or ignore_space_at_eol or ignore_cr_at_eol
    if ws:
        akeys = [_ws_key(x, ignore_all=ignore_all_space, ignore_change=ignore_space_change,
                         ignore_eol=ignore_space_at_eol, ignore_cr_eol=ignore_cr_at_eol)
                 for x in a]
        bkeys = [_ws_key(x, ignore_all=ignore_all_space, ignore_change=ignore_space_change,
                         ignore_eol=ignore_space_at_eol, ignore_cr_eol=ignore_cr_at_eol)
                 for x in b]
        ops = diff_lines_keyed(akeys, bkeys)
    else:
        ops = diff_lines(a, b)

    out: list[str] = []
    last_a = len(a) - 1
    last_b = len(b) - 1
    a_cons = [0] * (len(ops) + 1)
    b_cons = [0] * (len(ops) + 1)
    for i, (k, _ai, _bi) in enumerate(ops):
        a_cons[i + 1] = a_cons[i] + (1 if k in ("eq", "del") else 0)
        b_cons[i + 1] = b_cons[i] + (1 if k in ("eq", "ins") else 0)

    spans = _group_hunks_ic(ops, context, inter_hunk_context)
    if func_context:
        spans = _expand_function_context(a, spans, ops, a_cons)

    for start, end in spans:
        hunk = ops[start:end]
        # --ignore-blank-lines / -I: drop a hunk if every changed line is blank
        # (ignore_blank_lines) or matches the regex (ignore_regex).
        if ignore_blank_lines or ignore_regex is not None:
            changed = [(k, ai, bi) for k, ai, bi in hunk if k != "eq"]
            if changed and _all_ignorable(changed, a, b, ignore_blank_lines, ignore_regex):
                continue
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
        out.append(hdr)
        for kind, ai, bi in hunk:
            if kind == "eq":
                # xdiff emits context records from xdf2 (the b/new side); under
                # whitespace-ignore the two sides differ and git shows the b text.
                out.append(ctx_ind + b[bi])
                if b_no_newline and bi == last_b:
                    out.append(_NO_NEWLINE)
            elif kind == "del":
                out.append(old_ind + a[ai])
                if a_no_newline and ai == last_a:
                    out.append(_NO_NEWLINE)
            else:
                out.append(new_ind + b[bi])
                if b_no_newline and bi == last_b:
                    out.append(_NO_NEWLINE)
    return out


def _all_ignorable(changed, a, b, ignore_blank_lines, ignore_regex) -> bool:
    """True if every changed line in a hunk is ignorable: blank (for
    --ignore-blank-lines) or matching ``ignore_regex`` (-I)."""
    for k, ai, bi in changed:
        line = a[ai] if k == "del" else b[bi]
        ok = False
        if ignore_blank_lines and line.strip() == "":
            ok = True
        if ignore_regex is not None and ignore_regex.search(line):
            ok = True
        if not ok:
            return False
    return True


# ---------------------------------------------------------------------------
# word-diff (git diff --word-diff[=plain]) — port of diff.c:diff_words_show


def _split_words(text: str, word_regex=None) -> list[tuple[int, int]]:
    """Tokenize ``text`` into (begin, end) offsets of words. Without a regex this
    is git's whitespace word-splitter (maximal non-whitespace runs). With a
    compiled ``word_regex`` it is git's find_word_boundaries: each successive
    regex match is a word; a match is bounded at any embedded newline; an
    empty match advances one char (so it cannot loop)."""
    res: list[tuple[int, int]] = []
    n = len(text)
    if word_regex is None:
        i = 0
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
    i = 0
    while i < n:
        m = word_regex.search(text, i)
        if not m:
            break
        b, e = m.start(), m.end()
        nl = text.find("\n", b, e)
        if nl >= 0:
            e = nl
        if b == e:
            i = b + 1
            continue
        res.append((b, e))
        i = e
    return res


# Word-diff styles: (ctx, old, new) each (prefix, suffix), plus the newline.
# The "color" style wraps removed words red and added words green (diff.c
# diff_words_styles + fn_out_diff_words_write_helper, default diff colors).
_WD_STYLES = {
    "plain": {"ctx": ("", ""), "old": ("[-", "-]"), "new": ("{+", "+}"), "nl": "\n"},
    "porcelain": {"ctx": (" ", "\n"), "old": ("-", "\n"), "new": ("+", "\n"), "nl": "~\n"},
    "color": {"ctx": ("", ""), "old": ("\033[31m", "\033[m"),
              "new": ("\033[32m", "\033[m"), "nl": "\n"},
}


def _ww(text: str, prefix: str, suffix: str, newline: str = "\n") -> str:
    """Mirror diff.c:fn_out_diff_words_write_helper: wrap each non-empty
    newline-delimited segment with the prefix/suffix, emitting ``newline`` for
    each embedded newline."""
    out: list[str] = []
    parts = text.split("\n")
    for idx, seg in enumerate(parts):
        if seg != "":
            out.append(prefix + seg + suffix)
        if idx < len(parts) - 1:
            out.append(newline)
    return "".join(out)


def _word_render(minus_text: str, plus_text: str, style: dict, word_regex=None) -> str:
    """Render one hunk's word-level diff. Common text (and all whitespace) comes
    from the plus side; removed words are wrapped with the old-word style, added
    words with the new-word style."""
    ctx_p, old_p, new_p, nl = style["ctx"], style["old"], style["new"], style["nl"]
    if not plus_text:
        return _ww(minus_text, old_p[0], old_p[1], nl)
    wm = _split_words(minus_text, word_regex)
    wp = _split_words(plus_text, word_regex)
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
        if ins_idx:
            plus_begin = wp[ins_idx[0]][0]
            plus_end = wp[ins_idx[-1]][1]
        else:
            # Pure deletion: the plus position is the end of the previous plus
            # word (orig[plus_first].end with git's POSIX len==0 decrement).
            plus_begin = plus_end = (wp[pi - 1][1] if pi > 0 else 0)
        out.append(_ww(plus_text[current_plus:plus_begin], ctx_p[0], ctx_p[1], nl))
        if del_idx:
            out.append(_ww(minus_text[wm[del_idx[0]][0]:wm[del_idx[-1]][1]], old_p[0], old_p[1], nl))
        if ins_idx:
            out.append(_ww(plus_text[wp[ins_idx[0]][0]:wp[ins_idx[-1]][1]], new_p[0], new_p[1], nl))
        current_plus = plus_end
    out.append(_ww(plus_text[current_plus:], ctx_p[0], ctx_p[1], nl))
    return "".join(out)


def word_diff_hunks(a: list[str], b: list[str], context: int = 3, mode: str = "plain",
                    word_regex=None) -> str:
    """Return the git ``--word-diff`` body (``@@`` headers + word-diffed content)
    for two line lists, in plain/porcelain/color mode. ``word_regex`` (compiled)
    overrides whitespace word-splitting (--word-diff-regex)."""
    style = _WD_STYLES[mode]
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
        # In color mode the fragment header is wrapped in cyan (DIFF_FRAGINFO).
        if mode == "color":
            hdr = "\033[36m" + hdr + "\033[m"
        # git word-diffs only consecutive -/+ line groups; context lines flush
        # the pending group and are emitted verbatim (diff.c:fn_out_consume).
        parts = [hdr + "\n"]
        minus_acc: list[str] = []
        plus_acc: list[str] = []

        def _flush():
            if minus_acc or plus_acc:
                parts.append(_word_render("".join(minus_acc), "".join(plus_acc),
                                          style, word_regex))
                minus_acc.clear()
                plus_acc.clear()

        for k, ai, bi in hunk:
            if k == "eq":
                _flush()
                # Context: plain skips the prefix char; porcelain keeps a leading
                # space and appends a "~" newline marker.
                if mode == "porcelain":
                    parts.append(" " + a[ai] + "\n~\n")
                else:
                    parts.append(a[ai] + "\n")
            elif k == "del":
                minus_acc.append(a[ai] + "\n")
            else:
                plus_acc.append(b[bi] + "\n")
        _flush()
        chunks.append("".join(parts))
    result = "".join(chunks)
    if mode == "color":
        # git wraps each non-empty word-diff output line with a trailing RESET
        # (the line-level context color emit); a line already ending in a colored
        # word keeps its single reset, blank lines stay blank.
        out_lines = []
        for ln in result.split("\n"):
            if ln and not ln.endswith("\033[m"):
                ln = ln + "\033[m"
            out_lines.append(ln)
        result = "\n".join(out_lines)
    return result


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
