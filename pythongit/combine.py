"""Combined diff of unmerged index entries (``diff-files -c`` / ``--cc``).

Byte-exact port of git's ``combine-diff.c`` for the worktree-vs-multiple-stages
case driven by ``builtin/diff-files.c`` / ``diff-lib.c:run_diff_files``.

When the index has unmerged entries (stages #2/#3 from a conflict), the combined
diff compares the worktree result against the >=2 parent stages, emitting the
``diff --cc``/``diff --combined`` header and ``@@@``-style multi-column hunks.
``--cc`` (dense) additionally elides hunks whose result matches one parent.

Only the algorithm needed by diff-files is ported (num_parent is always 2 here,
but the code is written generically for N parents as in the C original).
"""

from __future__ import annotations

import os
from typing import Optional

from . import objects as objs
from .repo import Repository


# --- lline / sline data structures (combine-diff.c:109-137) ------------------

class _Lline:
    __slots__ = ("line", "parent_map", "next", "prev")

    def __init__(self, line: bytes, parent_map: int):
        self.line = line              # bytes WITHOUT trailing newline
        self.parent_map = parent_map  # bitmask of parents this line was lost from
        self.next: Optional[_Lline] = None
        self.prev: Optional[_Lline] = None


class _Sline:
    __slots__ = ("lost", "lenlost", "plost_head", "plost_tail", "plost_len",
                 "bol", "flag", "p_lno")

    def __init__(self):
        self.lost: Optional[_Lline] = None       # coalesced lost lines
        self.lenlost = 0
        self.plost_head: Optional[_Lline] = None  # per-parent uncoalesced
        self.plost_tail: Optional[_Lline] = None
        self.plost_len = 0
        self.bol: Optional[bytes] = None          # result line text (no newline)
        self.flag = 0
        self.p_lno: Optional[list[int]] = None    # per-parent line numbers


# --- blob access (combine-diff.c:grab_blob) ----------------------------------

def _grab_blob(repo: Repository, oid: Optional[str], mode: int) -> bytes:
    """Return the content used as a combined-diff operand for ``oid``/``mode``."""
    if mode and (mode & 0o170000) == 0o160000:  # S_ISGITLINK
        return b"Subproject commit %s\n" % (oid or "0" * 40).encode()
    if oid is None or oid == "0" * 40:
        return b""
    try:
        typ, data = objs.read_object(repo, oid)
    except (KeyError, ValueError):
        return b""
    return data


# --- coalesce_lines (combine-diff.c:182) -------------------------------------

_XDF_WHITESPACE_FLAGS = 0  # diff-files passes xdl_opts; default 0 here.


def _match_string_spaces(l1: bytes, l2: bytes, flags: int) -> bool:
    # With no whitespace flags this is a plain equality (the common case).
    return l1 == l2


def _coalesce_lines(base_head, lenbase, new_head, lennew, parent, flags):
    """LCS-coalesce the ``new`` lost-line list into ``base``; returns (head, len).

    Faithful port of coalesce_lines: matched lines get ``parent`` OR'd into their
    parent_map, unmatched new lines are spliced into base preserving order.
    """
    if new_head is None:
        return base_head, lenbase
    if base_head is None:
        return new_head, lennew

    # Materialize linked lists into arrays for the DP, keeping node identity.
    base = []
    n = base_head
    while n is not None:
        base.append(n)
        n = n.next
    new = []
    n = new_head
    while n is not None:
        new.append(n)
        n = n.next
    origbaselen = len(base)

    lcs = [[0] * (lennew + 1) for _ in range(origbaselen + 1)]
    # directions: 0=MATCH 1=BASE 2=NEW
    MATCH, BASE, NEW = 0, 1, 2
    directions = [[BASE] * (lennew + 1) for _ in range(origbaselen + 1)]
    for j in range(1, lennew + 1):
        directions[0][j] = NEW
    for i in range(1, origbaselen + 1):
        for j in range(1, lennew + 1):
            if _match_string_spaces(base[i - 1].line, new[j - 1].line, flags):
                lcs[i][j] = lcs[i - 1][j - 1] + 1
                directions[i][j] = MATCH
            elif lcs[i][j - 1] >= lcs[i - 1][j]:
                lcs[i][j] = lcs[i][j - 1]
                directions[i][j] = NEW
            else:
                lcs[i][j] = lcs[i - 1][j]
                directions[i][j] = BASE

    # Rebuild base as a linked list mutated in place per the C backtrack.
    # We track the list head and splice nodes; use the array nodes' next/prev.
    base_list_head = base_head
    bi = origbaselen
    nj = lennew
    baseend = base[-1] if base else None
    newend = new[-1] if new else None
    while bi != 0 or nj != 0:
        d = directions[bi][nj]
        if d == MATCH:
            baseend.parent_map |= (1 << parent)
            baseend = baseend.prev
            newend = newend.prev
            bi -= 1
            nj -= 1
        elif d == NEW:
            lline = newend
            # remove lline from new list
            if lline.prev:
                lline.prev.next = lline.next
            else:
                pass  # head of new list; new list head no longer tracked
            if lline.next:
                lline.next.prev = lline.prev
            newend = lline.prev
            nj -= 1
            # add lline to base list after baseend
            if baseend is not None:
                lline.next = baseend.next
                lline.prev = baseend
                if lline.prev:
                    lline.prev.next = lline
            else:
                lline.next = base_list_head
                lline.prev = None
                base_list_head = lline
            lenbase += 1
            if lline.next:
                lline.next.prev = lline
        else:  # BASE
            baseend = baseend.prev
            bi -= 1

    return base_list_head, lenbase


# --- per-parent diff into slines (combine-diff.c:combine_diff) ----------------

def _append_lost(sline: _Sline, n: int, line: bytes) -> None:
    if line.endswith(b"\n"):
        line = line[:-1]
    ll = _Lline(line, 1 << n)
    ll.prev = sline.plost_tail
    if ll.prev:
        ll.prev.next = ll
    else:
        sline.plost_head = ll
    sline.plost_tail = ll
    sline.plost_len += 1


def _combine_diff(repo, parent_oid, parent_mode, result_lines, sline, cnt, n,
                  num_parent, result_deleted, flags):
    """Diff one parent against the result, recording '+'/'-' info into sline."""
    nmask = 1 << n
    if result_deleted:
        return

    from .diff import diff_lines
    parent_blob = _grab_blob(repo, parent_oid, parent_mode)
    parent_lines = _split_lines(parent_blob)

    ops = diff_lines(parent_lines, result_lines)

    # Group ops into maximal non-equal runs (xdiff context=0 hunks). All '-'
    # lines of a run hang on the result-line bucket at the run's start; the j-th
    # '+' line marks sline[r0+j] (consume_hunk/consume_line semantics).
    rno = 0           # 0-based result line index
    i = 0
    nops = len(ops)
    while i < nops:
        kind = ops[i][0]
        if kind == "eq":
            rno += 1
            i += 1
            continue
        # start of a change run
        r0 = rno
        bucket = sline[r0]
        ins_idx = r0
        while i < nops and ops[i][0] != "eq":
            k, ai, bi = ops[i]
            if k == "del":
                _append_lost(bucket, n, parent_lines[ai] + b"\n")
            else:  # ins
                sline[ins_idx].flag |= nmask
                ins_idx += 1
                rno += 1
            i += 1

    # Assign per-parent line numbers (combine-diff.c:462-486).
    p_lno = 1
    for lno in range(0, cnt + 1):
        sline[lno].p_lno[n] = p_lno
        # coalesce new lines for this parent
        if sline[lno].plost_head is not None:
            sline[lno].lost, sline[lno].lenlost = _coalesce_lines(
                sline[lno].lost, sline[lno].lenlost,
                sline[lno].plost_head, sline[lno].plost_len, n, flags)
            sline[lno].plost_head = None
            sline[lno].plost_tail = None
            sline[lno].plost_len = 0
        ll = sline[lno].lost
        while ll is not None:
            if ll.parent_map & nmask:
                p_lno += 1
            ll = ll.next
        if lno < cnt and not (sline[lno].flag & nmask):
            p_lno += 1
    sline[cnt + 1].p_lno[n] = p_lno  # trailer (combine-diff.c:486)


# --- hunk marking / densify (combine-diff.c:make_hunks etc.) ------------------

_context = 3
_combine_marker = "@"


def _interesting(sline: _Sline, all_mask: int) -> bool:
    return bool((sline.flag & all_mask) or sline.lost)


def _adjust_hunk_tail(sline, all_mask, hunk_begin, i):
    if (hunk_begin + 1 <= i) and not (sline[i - 1].flag & all_mask):
        i -= 1
    return i


def _find_next(sline, mark, i, cnt, look_for_uninteresting):
    while i <= cnt:
        if (not (sline[i].flag & mark)) if look_for_uninteresting else (sline[i].flag & mark):
            return i
        i += 1
    return i


def _give_context(sline, cnt, num_parent):
    all_mask = (1 << num_parent) - 1
    mark = 1 << num_parent
    no_pre_delete = 2 << num_parent

    i = _find_next(sline, mark, 0, cnt, 0)
    if cnt < i:
        return 0

    while i <= cnt:
        j = (i - _context) if (_context < i) else 0
        # paint a few lines before the first interesting line
        while j < i:
            if not (sline[j].flag & mark):
                sline[j].flag |= no_pre_delete
            sline[j].flag |= mark
            j += 1
        while True:
            j = _find_next(sline, mark, i, cnt, 1)
            if cnt < j:
                # the rest are all interesting
                return 1
            k = _find_next(sline, mark, j, cnt, 0)
            j = _adjust_hunk_tail(sline, all_mask, i, j)
            if k < j + _context:
                while j < k:
                    sline[j].flag |= mark
                    j += 1
                i = k
                continue  # goto again
            i = k
            k = (j + _context) if (j + _context < cnt + 1) else (cnt + 1)
            while j < k:
                sline[j].flag |= mark
                j += 1
            break
    return 1


def _make_hunks(sline, cnt, num_parent, dense):
    all_mask = (1 << num_parent) - 1
    mark = 1 << num_parent
    for i in range(0, cnt + 1):
        if _interesting(sline[i], all_mask):
            sline[i].flag |= mark
        else:
            sline[i].flag &= ~mark
    if not dense:
        return _give_context(sline, cnt, num_parent)

    i = 0
    while i <= cnt:
        while i <= cnt and not (sline[i].flag & mark):
            i += 1
        if cnt < i:
            break
        hunk_begin = i
        j = i + 1
        while j <= cnt:
            if not (sline[j].flag & mark):
                la = _adjust_hunk_tail(sline, all_mask, hunk_begin, j)
                la = (la + _context) if (la + _context < cnt + 1) else (cnt + 1)
                contin = 0
                while la and j <= la - 1:
                    la -= 1
                    if sline[la].flag & mark:
                        contin = 1
                        break
                if not contin:
                    break
                j = la
            j += 1
        hunk_end = j

        same_diff = 0
        has_interesting = 0
        jj = i
        while jj < hunk_end and not has_interesting:
            this_diff = sline[jj].flag & all_mask
            if this_diff:
                if not same_diff:
                    same_diff = this_diff
                elif same_diff != this_diff:
                    has_interesting = 1
                    break
            ll = sline[jj].lost
            while ll is not None and not has_interesting:
                this_diff = ll.parent_map
                if not same_diff:
                    same_diff = this_diff
                elif same_diff != this_diff:
                    has_interesting = 1
                ll = ll.next
            jj += 1

        if not has_interesting and same_diff != all_mask:
            for jj in range(hunk_begin, hunk_end):
                sline[jj].flag &= ~mark
        i = hunk_end

    return _give_context(sline, cnt, num_parent)


# --- output (combine-diff.c:dump_sline / show_combined_header) ---------------

def _hunk_comment_line(bol: Optional[bytes]) -> bool:
    if not bol:
        return False
    ch = bol[0]
    return (65 <= ch <= 90) or (97 <= ch <= 122) or ch == ord("_") or ch == ord("$")


def _show_line_to_eol(out, line: bytes) -> None:
    saw_cr = bool(line) and line[-1:] == b"\r"
    body = line[:-1] if saw_cr else line
    out.write(body)
    if saw_cr:
        out.write(b"\r")
    out.write(b"\n")


def _u64(x: int) -> int:
    """Emulate C unsigned long (%lu) wraparound for hunk counts that can
    underflow (e.g. --unified=0 on an empty result: rlines goes negative)."""
    return x & 0xFFFFFFFFFFFFFFFF


def _show_parent_lno(out, sline, l0, l1, n, null_context):
    a = sline[l0].p_lno[n]
    b = sline[l1].p_lno[n]
    out.write(b" -%d,%d" % (_u64(a), _u64(b - a - null_context)))


def _dump_sline(out, sline, cnt, num_parent, result_deleted):
    mark = 1 << num_parent
    no_pre_delete = 2 << num_parent
    lno = 0
    if result_deleted:
        return
    while True:
        hunk_comment = None
        null_context = 0
        while lno <= cnt and not (sline[lno].flag & mark):
            if _hunk_comment_line(sline[lno].bol):
                hunk_comment = sline[lno].bol
            lno += 1
        if cnt < lno:
            break
        hunk_end = lno + 1
        while hunk_end <= cnt:
            if not (sline[hunk_end].flag & mark):
                break
            hunk_end += 1
        rlines = hunk_end - lno
        if cnt < hunk_end:
            rlines -= 1
        if not _context:
            for jj in range(lno, hunk_end):
                if not (sline[jj].flag & (mark - 1)):
                    null_context += 1
            rlines -= null_context

        out.write(b"@" * (num_parent + 1))
        for i in range(num_parent):
            _show_parent_lno(out, sline, lno, hunk_end, i, null_context)
        out.write(b" +%d,%d " % (_u64(lno + 1), _u64(rlines)))
        out.write(b"@" * (num_parent + 1))

        if hunk_comment is not None:
            comment_end = 0
            for i in range(40):
                if i >= len(hunk_comment):
                    break
                ch = hunk_comment[i]
                if ch == 0 or ch == ord("\n"):
                    break
                if not _isspace(ch):
                    comment_end = i
            if comment_end:
                out.write(b" ")
            for i in range(comment_end):
                out.write(hunk_comment[i:i + 1])
        out.write(b"\n")

        while lno < hunk_end:
            sl = sline[lno]
            lno += 1
            ll = None if (sl.flag & no_pre_delete) else sl.lost
            while ll is not None:
                row = bytearray()
                for j in range(num_parent):
                    row += b"-" if (ll.parent_map & (1 << j)) else b" "
                out.write(bytes(row))
                _show_line_to_eol(out, ll.line)
                ll = ll.next
            if cnt < lno:
                break
            p_mask = 1
            if not (sl.flag & (mark - 1)):
                if not _context:
                    continue
                # context line
            row = bytearray()
            for j in range(num_parent):
                row += b"+" if (p_mask & sl.flag) else b" "
                p_mask <<= 1
            out.write(bytes(row))
            _show_line_to_eol(out, sl.bol if sl.bol is not None else b"")


def _isspace(ch: int) -> bool:
    return ch in (0x20, 0x09, 0x0a, 0x0b, 0x0c, 0x0d)


def _show_combined_header(out, path, parent_oids, parent_modes, parent_status,
                          result_oid, result_mode, num_parent, dense,
                          abbrev, mode_differs, show_file_header,
                          combined_all_paths=False):
    a_prefix = "a/"
    b_prefix = "b/"
    out.write(b"diff --cc " if dense else b"diff --combined ")
    out.write(_quote_path(path))
    out.write(b"\n")
    out.write(b"index ")
    for i in range(num_parent):
        if i:
            out.write(b",")
        out.write(_abbrev_oid(parent_oids[i], abbrev))
    out.write(b"..")
    out.write(_abbrev_oid(result_oid, abbrev))
    out.write(b"\n")

    added = 0
    deleted = 0
    if mode_differs:
        deleted = 0 if result_mode else 1
        added = 0 if deleted else 1
        i = 0
        while added and i < num_parent:
            if parent_status[i] != "A":
                added = 0
            i += 1
        if added:
            out.write(b"new file mode %06o" % result_mode)
        else:
            if deleted:
                out.write(b"deleted file ")
            out.write(b"mode ")
            for i in range(num_parent):
                if i:
                    out.write(b",")
                out.write(b"%06o" % parent_modes[i])
            if result_mode:
                out.write(b"..%06o" % result_mode)
        out.write(b"\n")

    if not show_file_header:
        return

    # With --combined-all-paths a "---" line is emitted per parent (each using
    # that parent's path; ADDED -> /dev/null); otherwise a single "---" line is
    # emitted with "added" meaning every parent lacked the file (combine-diff.c).
    if combined_all_paths:
        for i in range(num_parent):
            if parent_status[i] == "A":
                out.write(b"--- /dev/null\n")
            else:
                out.write(b"--- " + _quote_path(a_prefix + path) + b"\n")
    elif added:
        out.write(b"--- /dev/null\n")
    else:
        out.write(b"--- " + _quote_path(a_prefix + path) + b"\n")
    if deleted:
        out.write(b"+++ /dev/null\n")
    else:
        out.write(b"+++ " + _quote_path(b_prefix + path) + b"\n")


def _abbrev_oid(oid: Optional[str], abbrev: int) -> bytes:
    o = oid or ("0" * 40)
    return o[:abbrev].encode()


def _quote_path(path: str) -> bytes:
    """C-style path quoting matching quote_two_c_style / write_name_quoted for
    the unproblematic ASCII case (no quoting); falls back to git's quoting for
    bytes needing escapes."""
    raw = path.encode("utf-8", "surrogateescape")
    needs = any(b < 0x20 or b == 0x7f or b in (0x22, 0x5c) or b >= 0x80 for b in raw)
    if not needs:
        return raw
    out = bytearray(b'"')
    for b in raw:
        if b == 0x22:
            out += b'\\"'
        elif b == 0x5c:
            out += b"\\\\"
        elif b == 0x07:
            out += b"\\a"
        elif b == 0x08:
            out += b"\\b"
        elif b == 0x0c:
            out += b"\\f"
        elif b == 0x0a:
            out += b"\\n"
        elif b == 0x0d:
            out += b"\\r"
        elif b == 0x09:
            out += b"\\t"
        elif b == 0x0b:
            out += b"\\v"
        elif b < 0x20 or b >= 0x80:
            out += b"\\%03o" % b
        else:
            out.append(b)
    out += b'"'
    return bytes(out)


# --- line splitting (matches combine-diff.c result-splitting) ----------------

def _split_lines(data: bytes) -> list[bytes]:
    """Split into lines WITHOUT trailing newlines; a final line lacking a newline
    is still its own entry (mirrors sline construction in show_patch_diff)."""
    if not data:
        return []
    lines = data.split(b"\n")
    if data.endswith(b"\n"):
        lines = lines[:-1]
    return lines


# --- top-level: build + show one combined path (show_patch_diff) -------------

def show_patch_diff_files(out, repo, path, parent_oids, parent_modes,
                          result_content, result_present, dense, abbrev,
                          flags=0, context=3, combined_all_paths=False):
    """Emit a combined PATCH diff for one unmerged path (working-tree file).

    parent_oids/parent_modes: lists (len num_parent) for stages #2.. (ours first).
    result_content: worktree bytes (None if file is missing -> deleted).
    """
    global _context
    _context = context
    num_parent = len(parent_oids)
    result_deleted = 0
    if result_present:
        result = result_content
        result_mode = _canon_mode_from_content(repo, path)
    else:
        result_deleted = 1
        result = b""
        result_mode = 0

    parent_status = ["M"] * num_parent

    mode_differs = 0
    for i in range(num_parent):
        if parent_modes[i] != result_mode:
            mode_differs = 1
            break

    # binary detection
    is_binary = _buffer_is_binary(result)
    if not is_binary:
        for i in range(num_parent):
            if _buffer_is_binary(_grab_blob(repo, parent_oids[i], parent_modes[i])):
                is_binary = 1
                break
    if is_binary:
        _show_combined_header(out, path, parent_oids, parent_modes, parent_status,
                              None, result_mode, num_parent, dense, abbrev,
                              mode_differs, 0, combined_all_paths)
        out.write(b"Binary files differ\n")
        return

    result_lines = _split_lines(result)
    cnt = len(result_lines)

    sline = [_Sline() for _ in range(cnt + 2)]
    for idx in range(cnt):
        sline[idx].bol = result_lines[idx]
    for s in sline:
        s.p_lno = [0] * num_parent

    for i in range(num_parent):
        reused = False
        for j in range(i):
            if parent_oids[i] == parent_oids[j] and parent_oids[i] is not None:
                _reuse_combine_diff(sline, cnt, i, j)
                reused = True
                break
        if not reused:
            _combine_diff(repo, parent_oids[i], parent_modes[i], result_lines,
                          sline, cnt, i, num_parent, result_deleted, flags)

    show_hunks = _make_hunks(sline, cnt, num_parent, dense)

    if show_hunks or mode_differs or True:  # working_tree_file is always true
        _show_combined_header(out, path, parent_oids, parent_modes, parent_status,
                              None, result_mode, num_parent, dense, abbrev,
                              mode_differs, 1, combined_all_paths)
        _dump_sline(out, sline, cnt, num_parent, result_deleted)


def _reuse_combine_diff(sline, cnt, i, j):
    imask = 1 << i
    jmask = 1 << j
    for lno in range(0, cnt + 1):
        sl = sline[lno]
        sl.p_lno[i] = sl.p_lno[j]
        ll = sl.lost
        while ll is not None:
            if ll.parent_map & jmask:
                ll.parent_map |= imask
            ll = ll.next
        if sl.flag & jmask:
            sl.flag |= imask
    # the overall size of the file (sline[cnt+1] trailer) (combine-diff.c:900)
    sline[cnt + 1].p_lno[i] = sline[cnt + 1].p_lno[j]


def _buffer_is_binary(data: bytes) -> bool:
    return b"\x00" in data[:8000]


def _canon_mode_from_content(repo: Repository, path: str) -> int:
    full = repo.path / path
    try:
        st = os.lstat(full)
    except OSError:
        return 0
    import stat as _stat
    if _stat.S_ISLNK(st.st_mode):
        return 0o120000
    if st.st_mode & 0o111:
        return 0o100755
    return 0o100644


def show_raw_combined(out, path, parent_oids, parent_modes, parent_status,
                      result_oid, result_mode, num_parent, abbrev,
                      fmt="raw", line_term=b"\n", combined_all_paths=False):
    """Combined raw output (show_raw_diff). ``fmt`` selects the layout:
      - "raw":        ':'*N + modes + oids + status + [per-parent paths] + path
      - "name-status": status + [per-parent paths] + path
      - "name":        [per-parent paths] + path

    With --combined-all-paths each parent's path is written before the result
    path (write_name_quoted per parent)."""
    inter = b"\t" if line_term == b"\n" else b"\0"
    if fmt == "raw":
        out.write(b":" * num_parent)
        for i in range(num_parent):
            out.write(b"%06o " % parent_modes[i])
        out.write(b"%06o" % result_mode)
        for i in range(num_parent):
            out.write(b" " + _abbrev_oid(parent_oids[i], abbrev))
        out.write(b" " + _abbrev_oid(result_oid, abbrev) + b" ")
    if fmt in ("raw", "name-status"):
        for i in range(num_parent):
            out.write(parent_status[i].encode())
        out.write(inter)
    name = _quote_path(path) if line_term == b"\n" else path.encode()
    if combined_all_paths:
        for i in range(num_parent):
            out.write(name + inter)
    out.write(name)
    out.write(line_term)
