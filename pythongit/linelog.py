"""Line-level history (``git log -L`` / ``git show -L``).

This is a port of the relevant parts of git's line-log.c / line-range.c plus the
line-range diff callback from diff.c (2.54.0).  It traces a set of line ranges of
one or more files backwards through history, reporting each commit that changed
those lines together with the scoped unified-diff hunk.

The two public entry points are :func:`parse_args`, which parses the ``-L``
option strings against the starting commit, and :func:`run`, which walks history
and emits output via injected rendering callbacks.
"""

from __future__ import annotations

from typing import Callable, Optional

from . import objects as objs
from . import diff as diff_mod


# ---------------------------------------------------------------------------
# range sets  (line-log.c)
#
# A range set is a sorted list of non-empty, non-overlapping half-open
# ``[start, end)`` integer intervals (0-based, like line-log.c's internal form).
# ---------------------------------------------------------------------------


Range = tuple[int, int]


def _append(rs: list[Range], a: int, b: int) -> None:
    if a < b:
        rs.append((a, b))


def sort_and_merge(rs: list[Range]) -> list[Range]:
    out: list[Range] = []
    for a, b in sorted(rs):
        if a == b:
            continue
        if out and a <= out[-1][1]:
            if out[-1][1] < b:
                out[-1] = (out[-1][0], b)
        else:
            out.append((a, b))
    return out


def _union(a: list[Range], b: list[Range]) -> list[Range]:
    """Union of two canonical range sets (line-log.c range_set_union)."""
    out: list[Range] = []
    i = j = 0
    while i < len(a) or j < len(b):
        if i < len(a) and j < len(b):
            if a[i][0] < b[j][0]:
                nr = a[i]; i += 1
            elif a[i][0] > b[j][0]:
                nr = b[j]; j += 1
            elif a[i][1] < b[j][1]:
                nr = a[i]; i += 1
            else:
                nr = b[j]; j += 1
        elif i < len(a):
            nr = a[i]; i += 1
        else:
            nr = b[j]; j += 1
        s, e = nr
        if s == e:
            continue
        if not out or out[-1][1] < s:
            out.append((s, e))
        elif out[-1][1] < e:
            out[-1] = (out[-1][0], e)
    return out


def _difference(a: list[Range], b: list[Range]) -> list[Range]:
    """out = a \\ b (line-log.c range_set_difference)."""
    out: list[Range] = []
    j = 0
    for start, end in a:
        while start < end:
            while j < len(b) and start >= b[j][1]:
                j += 1
            if j >= len(b) or end <= b[j][0]:
                _append(out, start, end)
                break
            if start >= b[j][0]:
                start = b[j][1]
            elif end > b[j][0]:
                if start < b[j][0]:
                    _append(out, start, b[j][0])
                start = b[j][1]
    return out


def _ranges_overlap(a: Range, b: Range) -> bool:
    return not (a[1] <= b[0] or b[1] <= a[0])


def _filter_touched(diff_parent: list[Range], diff_target: list[Range],
                    rs: list[Range]) -> tuple[list[Range], list[Range]]:
    """Hunks of the diff whose target side overlaps an interesting range."""
    out_parent: list[Range] = []
    out_target: list[Range] = []
    j = 0
    for i in range(len(diff_target)):
        while diff_target[i][0] >= rs[j][1]:
            j += 1
            if j == len(rs):
                return out_parent, out_target
        if _ranges_overlap(diff_target[i], rs[j]):
            out_parent.append((diff_parent[i][0], diff_parent[i][1]))
            out_target.append((diff_target[i][0], diff_target[i][1]))
    return out_parent, out_target


def _shift_diff(rs: list[Range], diff_parent: list[Range],
                diff_target: list[Range]) -> list[Range]:
    out: list[Range] = []
    j = 0
    offset = 0
    for s, e in rs:
        while j < len(diff_target) and s >= diff_target[j][0]:
            offset += ((diff_parent[j][1] - diff_parent[j][0])
                       - (diff_target[j][1] - diff_target[j][0]))
            j += 1
        _append(out, s + offset, e + offset)
    return out


def map_across_diff(rs: list[Range], diff_parent: list[Range],
                    diff_target: list[Range]) -> tuple[list[Range], list[Range], list[Range]]:
    """Map the interesting ranges ``rs`` backwards across one diff.

    Returns ``(new_ranges, touched_parent, touched_target)`` mirroring
    line-log.c range_set_map_across_diff.
    """
    tp, tt = _filter_touched(diff_parent, diff_target, rs)
    tmp1 = _difference(rs, tt)
    tmp2 = _shift_diff(tmp1, diff_parent, diff_target)
    out = _union(tmp2, tp)
    return out, tp, tt


# ---------------------------------------------------------------------------
# collect_diff: parent/target range pairs for one file pair  (ctxlen 0)
# ---------------------------------------------------------------------------


def collect_diff(parent_text: str, target_text: str) -> tuple[list[Range], list[Range]]:
    """Return (parent_ranges, target_ranges): the 0-based half-open line spans of
    each change atom between the two blobs, matching git's ``collect_diff`` with
    ``ctxlen = interhunkctxlen = 0`` (one hunk per change atom)."""
    a = parent_text.splitlines()
    b = target_text.splitlines()
    ops = diff_mod.diff_lines(a, b)
    parent: list[Range] = []
    target: list[Range] = []
    ai = bi = 0
    i = 0
    n = len(ops)
    while i < n:
        if ops[i][0] == "eq":
            ai += 1
            bi += 1
            i += 1
            continue
        a_start, b_start = ai, bi
        while i < n and ops[i][0] != "eq":
            if ops[i][0] == "del":
                ai += 1
            else:
                bi += 1
            i += 1
        # collect_diff_cb appends both sides per change atom, keeping the parent
        # and target arrays index-aligned (zero-length spans included).
        parent.append((a_start, ai))
        target.append((b_start, bi))
    return parent, target


# ---------------------------------------------------------------------------
# -L argument parsing  (line-range.c)
# ---------------------------------------------------------------------------


class LParseError(Exception):
    """Raised with git's exact ``fatal:`` text (sans the ``fatal: `` prefix)."""


def _is_funcname_match(line: str) -> bool:
    """Default find-function predicate (line-range.c match_funcname)."""
    if not line:
        return False
    c = line[0]
    return c.isalpha() or c == "_" or c == "$"


def _parse_loc(spec: str, lines: list[str], nlines: int, begin: int,
               want_ret: bool) -> tuple[str, Optional[int]]:
    """Port of line-range.c parse_loc.  Returns (remainder, value-or-None).

    ``begin`` follows the C contract: negative when parsing the start anchor
    (abs value = base line), positive when parsing the end.  ``want_ret`` is the
    C ``ret`` non-NULL test (False = skip_range_arg scan).
    """
    # "+N" / "-N" relative forms (only when begin >= 1)
    if 1 <= begin and spec[:1] in ("+", "-"):
        num, term = _strtol(spec, 1)
        if term != 1:
            rest = spec[term:]
            if not want_ret:
                return rest, None
            if num == 0:
                raise LParseError("-L invalid empty range")
            if spec[0] == "-":
                num = -num
            if num > 0:
                ret = begin + num - 2
            elif num == 0:
                ret = begin
            else:
                ret = begin + num if begin + num > 0 else 1
            return rest, ret
        return spec, None

    num, term = _strtol(spec, 0)
    if term != 0:
        rest = spec[term:]
        if want_ret:
            if num <= 0:
                raise LParseError(f"-L invalid line number: {num}")
            return rest, num
        return rest, None

    if begin < 0:
        if spec[:1] != "^":
            begin = -begin
        else:
            begin = 1
            spec = spec[1:]

    if spec[:1] != "/":
        return spec, None

    # regexp of form /.../
    t = 1
    while t < len(spec) and spec[t] != "/":
        if spec[t] == "\\":
            t += 1
        t += 1
    if t >= len(spec) or spec[t] != "/":
        return spec, None

    if not want_ret:
        return spec[t + 1:], None

    pattern = spec[1:t]
    human_begin = begin - 1  # 0-based search start
    import re as _re
    try:
        rx = _re.compile(pattern)
    except _re.error as exc:
        raise LParseError(
            f"-L parameter '{pattern}' starting at line {begin}: {exc}")
    # search nth_line(begin..) for first line whose match lands at/after begin
    line_no = human_begin
    found = None
    while line_no < nlines:
        m = rx.search(lines[line_no])
        if m is not None:
            found = line_no
            break
        line_no += 1
    if found is None:
        # mirror regexec REG_NOMATCH -> regerror "No match"
        raise LParseError(
            f"-L parameter '{pattern}' starting at line {begin}: No match")
    return spec[t + 1:], found + 1


def _strtol(s: str, off: int) -> tuple[int, int]:
    """Mimic C strtol over decimal: return (value, end-index). end==off when no
    digits were consumed."""
    i = off
    sign = 1
    if i < len(s) and s[i] in "+-":
        # strtol consumes a leading sign, but parse_loc only calls _strtol at
        # off>0 after stripping +/-, or at off=0 where a sign would already have
        # been handled by the +/- branch; still, be faithful.
        if s[i] == "-":
            sign = -1
        i += 1
    start = i
    while i < len(s) and s[i].isdigit():
        i += 1
    if i == start:
        return 0, off
    return sign * int(s[start:i]), i


def _parse_range_funcname(arg: str, lines: list[str], nlines: int, anchor: int,
                          want_begin: bool) -> tuple[Optional[str], Optional[int], Optional[int]]:
    """Port of line-range.c parse_range_funcname.  Returns (remainder, begin,
    end).  ``want_begin`` False = skip_range_arg scan (begin NULL)."""
    if arg[:1] == "^":
        anchor = 1
        arg = arg[1:]
    assert arg[:1] == ":"
    t = 1
    while t < len(arg) and arg[t] != ":":
        if arg[t] == "\\" and t + 1 < len(arg):
            t += 1
        t += 1
    if t == 1:
        return None, None, None
    if not want_begin:
        return arg[t:], None, None

    pattern = arg[1:t]
    import re as _re
    anchor0 = anchor - 1
    try:
        rx = _re.compile(pattern)
    except _re.error as exc:
        raise LParseError(f"-L parameter '{pattern}': {exc}")

    # find_funcname_matching_regexp over the text starting at line anchor0
    begin = None
    ln = anchor0
    while ln < nlines:
        if rx.search(lines[ln]) is not None and _is_funcname_match(lines[ln]):
            begin = ln
            break
        ln += 1
    if begin is None:
        raise LParseError(
            f"-L parameter '{pattern}' starting at line {anchor0 + 1}: no match")
    if begin >= nlines:
        raise LParseError(f"-L parameter '{pattern}' matches at EOF")
    end = begin + 1
    while end < nlines:
        if _is_funcname_match(lines[end]):
            break
        end += 1
    # compensate for 1-based numbering
    return arg[t:], begin + 1, end


def skip_range_arg(arg: str) -> Optional[str]:
    """Port of line-range.c skip_range_arg (scan-only: returns the part after the
    range spec, i.e. the ``:file`` portion, or None)."""
    if arg[:1] == ":" or (arg[:1] == "^" and arg[1:2] == ":"):
        rest, _b, _e = _parse_range_funcname(arg, [], 0, 0, want_begin=False)
        return rest
    rest, _ = _parse_loc(arg, [], 0, -1, want_ret=False)
    if rest[:1] == ",":
        rest, _ = _parse_loc(rest[1:], [], 0, 0, want_ret=False)
    return rest


def parse_range_arg(arg: str, lines: list[str], nlines: int,
                    anchor: int) -> tuple[int, int]:
    """Port of line-range.c parse_range_arg.  Returns (begin, end) with the C
    semantics (begin/end are 1-based, or 0 meaning "unspecified")."""
    if anchor < 1:
        anchor = 1
    if anchor > nlines:
        anchor = nlines + 1

    if arg[:1] == ":" or (arg[:1] == "^" and arg[1:2] == ":"):
        rest, begin, end = _parse_range_funcname(arg, lines, nlines, anchor,
                                                 want_begin=True)
        if rest is None or rest != "":
            raise _Malformed(arg)
        return begin or 0, end or 0

    rest, begin = _parse_loc(arg, lines, nlines, -anchor, want_ret=True)
    begin = begin or 0
    end = 0
    if rest[:1] == ",":
        rest, end = _parse_loc(rest[1:], lines, nlines, begin + 1, want_ret=True)
        end = end or 0
    if rest != "":
        raise _Malformed(arg)
    if begin and end and end < begin:
        begin, end = end, begin
    return begin, end


class _Malformed(Exception):
    def __init__(self, arg: str):
        self.arg = arg


# ---------------------------------------------------------------------------
# line-range diff callback  (diff.c: line_range_line_fn / flush_rhunk)
#
# Given the FULL unified diff of one file pair (computed with ctxlen inflated to
# max(range span, context)) and the post-image range set, emit only the @@ hunks
# scoped to the ranges, with synthetic headers and the captured funcname.
# ---------------------------------------------------------------------------


class _LineRangeState:
    __slots__ = ("ranges", "cur_range", "lno_post", "lno_pre", "func",
                 "rhunk", "rhunk_old_begin", "rhunk_old_count",
                 "rhunk_new_begin", "rhunk_new_count", "rhunk_active",
                 "rhunk_has_changes", "pending_rm", "pending_rm_count",
                 "pending_rm_pre_begin", "out")

    def __init__(self, ranges: list[Range], out: list[str]):
        self.ranges = ranges
        self.cur_range = 0
        self.lno_post = 0
        self.lno_pre = 0
        self.func = ""
        self.rhunk: list[str] = []
        self.rhunk_old_begin = 0
        self.rhunk_old_count = 0
        self.rhunk_new_begin = 0
        self.rhunk_new_count = 0
        self.rhunk_active = False
        self.rhunk_has_changes = False
        self.pending_rm: list[str] = []
        self.pending_rm_count = 0
        self.pending_rm_pre_begin = 0
        self.out = out


def _discard_pending_rm(s: _LineRangeState) -> None:
    s.pending_rm = []
    s.pending_rm_count = 0


def _flush_rhunk(s: _LineRangeState) -> None:
    if not s.rhunk_active:
        return
    if s.pending_rm_count:
        s.rhunk.extend(s.pending_rm)
        s.rhunk_old_count += s.pending_rm_count
        s.rhunk_has_changes = True
        _discard_pending_rm(s)
    if not s.rhunk_has_changes:
        s.rhunk_active = False
        s.rhunk = []
        return
    hdr = (f"@@ -{s.rhunk_old_begin},{s.rhunk_old_count} "
           f"+{s.rhunk_new_begin},{s.rhunk_new_count} @@")
    if s.func:
        hdr += " " + s.func
    s.out.append(hdr)
    s.out.extend(s.rhunk)
    s.rhunk_active = False
    s.rhunk = []


def _hunk_header(s: _LineRangeState, old_begin: int, new_begin: int,
                 func: str) -> None:
    s.lno_post = new_begin
    s.lno_pre = old_begin
    if func:
        # diff.c truncates to sizeof(func)==80 bytes; our headings are short.
        s.func = func[:80]
    else:
        s.func = ""


def _line(s: _LineRangeState, line: str) -> None:
    """Port of diff.c line_range_line_fn.  ``line`` includes its leading
    +/-/space marker but no trailing newline (we re-add on emit)."""
    c = line[:1]
    if c == "-":
        if not s.pending_rm_count:
            s.pending_rm_pre_begin = s.lno_pre
        s.lno_pre += 1
        s.pending_rm.append(line)
        s.pending_rm_count += 1
        return

    if c == "\\":
        if s.pending_rm_count:
            s.pending_rm.append(line)
        elif s.rhunk_active:
            s.rhunk.append(line)
        return

    # c is '+' or ' '
    lno_0 = s.lno_post - 1
    cur_pre = s.lno_pre
    s.lno_post += 1
    if c == " ":
        s.lno_pre += 1

    while (s.cur_range < len(s.ranges)
           and lno_0 >= s.ranges[s.cur_range][1]):
        if s.rhunk_active:
            _flush_rhunk(s)
        _discard_pending_rm(s)
        s.cur_range += 1

    if s.cur_range >= len(s.ranges):
        _discard_pending_rm(s)
        return

    cur = s.ranges[s.cur_range]
    if lno_0 < cur[0]:
        _discard_pending_rm(s)
        return

    if not s.rhunk_active:
        s.rhunk_active = True
        s.rhunk_has_changes = False
        s.rhunk_new_begin = lno_0 + 1
        s.rhunk_old_begin = (s.pending_rm_pre_begin
                             if s.pending_rm_count else cur_pre)
        s.rhunk_old_count = 0
        s.rhunk_new_count = 0
        s.rhunk = []

    if s.pending_rm_count:
        s.rhunk.extend(s.pending_rm)
        s.rhunk_old_count += s.pending_rm_count
        s.rhunk_has_changes = True
        _discard_pending_rm(s)

    s.rhunk.append(line)
    s.rhunk_new_count += 1
    if c == "+":
        s.rhunk_has_changes = True
    else:
        s.rhunk_old_count += 1


def emit_scoped_hunks(parent_text: str, target_text: str,
                      ranges: list[Range], context: int) -> list[str]:
    """Produce the scoped unified-diff hunk lines (no file header) for one file
    pair restricted to ``ranges`` (post-image, 0-based half-open).

    Mirrors diff.c builtin_diff's ``line_ranges`` branch: inflate ctxlen to the
    widest range span, run a normal unified diff, then clip to the ranges.
    """
    a = parent_text.splitlines()
    b = target_text.splitlines()
    max_span = 0
    for st, en in ranges:
        if en - st > max_span:
            max_span = en - st
    ctx = max(context, max_span)
    body = diff_mod.format_hunks(
        a, b, ctx,
        a_no_newline=bool(parent_text) and not parent_text.endswith("\n"),
        b_no_newline=bool(target_text) and not target_text.endswith("\n"),
    )
    out: list[str] = []
    s = _LineRangeState(ranges, out)
    for ln in body:
        if ln.startswith("@@"):
            ob, nb, func = _parse_at_header(ln)
            _hunk_header(s, ob, nb, func)
        else:
            _line(s, ln)
    _flush_rhunk(s)
    return out


def _parse_at_header(line: str) -> tuple[int, int, str]:
    """Parse ``@@ -a,b +c,d @@ func`` -> (a, c, func)."""
    # line: @@ -<old> +<new> @@[ func]
    after = line[3:]  # strip "@@ "
    body, _, rest = after.partition(" @@")
    func = rest[1:] if rest.startswith(" ") else ""
    old_part, _, new_part = body.partition(" ")
    old_begin = int(old_part[1:].split(",", 1)[0]) if old_part else 0
    new_begin = int(new_part[1:].split(",", 1)[0]) if new_part else 0
    return old_begin, new_begin, func


# ---------------------------------------------------------------------------
# argument parsing entry point  (line-log.c parse_lines)
# ---------------------------------------------------------------------------


class FileRanges:
    """One file's tracked ranges plus the diff pair recorded for output."""
    __slots__ = ("path", "ranges", "pair", "ranges_at_target")

    def __init__(self, path: str, ranges: list[Range]):
        self.path = path
        self.ranges = ranges
        # pair: (one_path, one_text, two_text) of the diff that this commit is
        # responsible for, or None.
        self.pair: Optional[tuple[str, str, str]] = None
        # The post-image (target) ranges at the time this commit was processed,
        # used to scope the output hunks.
        self.ranges_at_target: list[Range] = list(ranges)


def merge_file_ranges(a: Optional[list], b: list) -> list:
    """Union two path-sorted FileRanges lists (line-log.c line_log_data_merge)."""
    if a is None:
        return b
    out: list[FileRanges] = []
    i = j = 0
    while i < len(a) or j < len(b):
        if i >= len(a):
            cmp = 1
        elif j >= len(b):
            cmp = -1
        else:
            cmp = (a[i].path > b[j].path) - (a[i].path < b[j].path)
        if cmp < 0:
            out.append(FileRanges(a[i].path, list(a[i].ranges))); i += 1
        elif cmp == 0:
            out.append(FileRanges(a[i].path, _union(a[i].ranges, b[j].ranges)))
            i += 1; j += 1
        else:
            out.append(FileRanges(b[j].path, list(b[j].ranges))); j += 1
    return out


def _read_blob_text(repo, sha: Optional[str]) -> str:
    if sha is None:
        return ""
    return objs.read_object(repo, sha)[1].decode("utf-8", errors="replace")


def parse_args(repo, start_tree: str, l_args: list[str],
               read_path_text: Callable[[str, str], Optional[str]]):
    """Parse the ``-L`` option strings against the starting commit's tree.

    ``read_path_text(tree, path)`` returns the blob text at ``path`` in ``tree``
    or None if absent.  Returns a list of :class:`FileRanges` sorted by path
    (matching line-log.c's path-sorted linked list).

    Raises :class:`LParseError` (git ``fatal:`` text, no prefix).
    """
    by_path: dict[str, list[Range]] = {}
    order: list[str] = []
    for raw in l_args:
        name_part = skip_range_arg(raw)
        if name_part is None or name_part[:1] != ":" or len(name_part) < 2:
            raise LParseError(
                "-L argument not 'start,end:file' or ':funcname:file': " + raw)
        range_part = raw[:len(raw) - len(name_part)]
        path = name_part[1:]

        text = read_path_text(start_tree, path)
        if text is None:
            raise LParseError(f"There is no path {path} in the commit")
        lines = text.splitlines()
        # fill_line_ends: number of lines (a trailing newline does not add one).
        nlines = len(lines)

        existing = by_path.get(path)
        if existing:
            anchor = existing[-1][1] + 1
        else:
            anchor = 1

        try:
            begin, end = parse_range_arg(range_part, lines, nlines, anchor)
        except _Malformed:
            raise LParseError(f"malformed -L argument '{range_part}'")
        if (not nlines and (begin or end)) or nlines < begin:
            raise LParseError(f"file {path} has only {nlines} lines")
        if begin < 1:
            begin = 1
        if end < 1 or nlines < end:
            end = nlines
        begin -= 1  # 0-based half-open [begin, end)
        if path not in by_path:
            by_path[path] = []
            order.append(path)
        by_path[path].append((begin, end))

    result = []
    for path in sorted(by_path):
        result.append(FileRanges(path, sort_and_merge(by_path[path])))
    return result


# ---------------------------------------------------------------------------
# history walk + output  (line-log.c process_ranges_* / line_log_print)
# ---------------------------------------------------------------------------


def process_commit(repo, ranges: list[FileRanges], commit_tree: str,
                   parent_tree: Optional[str]) -> tuple[bool, list[FileRanges]]:
    """Process one ordinary commit: diff each tracked file against the (first)
    parent, record the responsible diff in each FileRanges.pair, and compute the
    parent's tracked ranges.

    Returns (changed, parent_ranges).
    """
    changed = False
    parent_ranges: list[FileRanges] = []
    for fr in ranges:
        if not fr.ranges:
            parent_ranges.append(FileRanges(fr.path, []))
            continue
        target_text = _read_blob_text(repo, _tree_blob(repo, commit_tree, fr.path))
        parent_path, parent_blob = _locate_parent(repo, parent_tree, fr.path,
                                                  commit_tree)
        parent_text = _read_blob_text(repo, parent_blob)

        diff_parent, diff_target = collect_diff(parent_text, target_text)
        new_ranges, tp, _tt = map_across_diff(fr.ranges, diff_parent, diff_target)

        pr = FileRanges(parent_path, new_ranges)
        parent_ranges.append(pr)

        if tp:  # this commit is responsible for a change in-range
            changed = True
            fr.pair = (parent_path, parent_text, target_text)
            fr.ranges_at_target = list(fr.ranges)
    return changed, parent_ranges


def _process_against_parent(repo, ranges: list[FileRanges], commit_tree: str,
                            parent_tree: Optional[str]) -> tuple[bool, list[FileRanges]]:
    """Diff each tracked file against one parent tree, returning (changed,
    candidate_parent_ranges) WITHOUT recording output pairs (used per merge
    parent).  ``changed`` is True iff any tracked range was touched."""
    changed = False
    cand: list[FileRanges] = []
    for fr in ranges:
        if not fr.ranges:
            cand.append(FileRanges(fr.path, []))
            continue
        target_text = _read_blob_text(repo, _tree_blob(repo, commit_tree, fr.path))
        parent_path, parent_blob = _locate_parent(repo, parent_tree, fr.path,
                                                  commit_tree)
        parent_text = _read_blob_text(repo, parent_blob)
        diff_parent, diff_target = collect_diff(parent_text, target_text)
        new_ranges, tp, _tt = map_across_diff(fr.ranges, diff_parent, diff_target)
        cand.append(FileRanges(parent_path, new_ranges))
        if tp:
            changed = True
    return changed, cand


def process_merge_commit(repo, ranges: list[FileRanges], commit_tree: str,
                         parent_trees: list[tuple[str, Optional[str]]]):
    """Port of line-log.c process_ranges_merge_commit.

    ``parent_trees`` is a list of (parent_sha, parent_tree).  Returns
    ``(shown, propagate)`` where ``shown`` is True iff the merge commit should be
    displayed (with an empty diff), and ``propagate`` is a list of
    ``(parent_sha, parent_ranges)`` pairs to add to those parents.
    """
    cands: list[tuple[str, list[FileRanges]]] = []
    for psha, ptree in parent_trees:
        changed, cand = _process_against_parent(repo, ranges, commit_tree, ptree)
        if not changed:
            # This parent takes all the blame: follow only it, hide the merge.
            return False, [(psha, cand)]
        cands.append((psha, cand))
    # No single parent took the blame: show the merge, follow all parents.
    return True, cands


def _tree_blob(repo, tree: Optional[str], path: str) -> Optional[str]:
    from . import workdir as _wd
    if tree is None:
        return None
    e = _wd.tree_path_entry(repo, tree, path)
    if e is None or e.is_dir() or e.is_gitlink():
        return None
    return e.sha


def _locate_parent(repo, parent_tree: Optional[str], path: str,
                   commit_tree: str) -> tuple[str, Optional[str]]:
    """Return (parent_path, parent_blob_sha).  Renames are followed when the path
    is absent from the parent but present (by content) under another name."""
    blob = _tree_blob(repo, parent_tree, path)
    if blob is not None or parent_tree is None:
        return path, blob
    # Path absent in parent: look for a rename source whose content best matches.
    src = _find_rename_source(repo, parent_tree, commit_tree, path)
    if src is not None:
        return src, _tree_blob(repo, parent_tree, src)
    return path, None


def _find_rename_source(repo, parent_tree: str, commit_tree: str,
                        path: str) -> Optional[str]:
    """Best-effort rename detection (exact + similarity), mirroring git's
    default ``diff.renames`` (50% similarity)."""
    from . import workdir as _wd
    target_sha = _tree_blob(repo, commit_tree, path)
    if target_sha is None:
        return None
    from . import workdir as _wd
    parent_map = {p: (m, s) for p, m, s in _wd.iter_tree_files(repo, parent_tree)}
    commit_paths = {p for p, _m, _s in _wd.iter_tree_files(repo, commit_tree)}
    # Candidates: deleted in commit (present in parent, absent in commit).
    candidates = [p for p in parent_map if p not in commit_paths]
    # exact content match first
    for p in candidates:
        if parent_map[p][1] == target_sha:
            return p
    # similarity fallback
    target_text = _read_blob_text(repo, target_sha)
    best = None
    best_score = 0.0
    for p in candidates:
        ptext = _read_blob_text(repo, parent_map[p][1])
        score = _similarity(ptext, target_text)
        if score > best_score:
            best_score = score
            best = p
    if best is not None and best_score >= 0.5:
        return best
    return None


def _similarity(a: str, b: str) -> float:
    al = a.splitlines()
    bl = b.splitlines()
    if not al and not bl:
        return 1.0
    common = 0
    for op in diff_mod.diff_lines(al, bl):
        if op[0] == "eq":
            common += 1
    denom = max(len(al), len(bl))
    return common / denom if denom else 0.0
