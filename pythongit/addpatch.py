"""Interactive hunk-selection machinery shared by add/stage/commit/reset/stash.

This is a byte-for-byte port of git's add-patch.c (the hunk splitter and the
y/n/q/a/d/s/e/K/J/k/j/g/?/p/P/</> command loop and prompts) and the relevant
parts of add-interactive.c (the `-i` main menu).

The diff that drives the loop is produced by invoking pygit's own diff plumbing
(``diff-files`` / ``diff-index`` / ``diff-tree``) in-process so the rendered diff
text is identical to what the real git would feed to add-patch; selected hunks
are applied by invoking pygit's ``apply`` the same way git's add-patch shells out
to ``git apply``.
"""

from __future__ import annotations

import io
import re
import sys
from dataclasses import dataclass, field
from typing import List, Optional


# ---------------------------------------------------------------------------
# Patch modes (ported from the patch_mode_* tables in add-patch.c)
# ---------------------------------------------------------------------------

# prompt_mode index constants
PROMPT_MODE_CHANGE = 0
PROMPT_DELETION = 1
PROMPT_ADDITION = 2
PROMPT_HUNK = 3


@dataclass
class PatchMode:
    diff_cmd: List[str]
    apply_args: List[str]
    apply_check_args: List[str]
    prompt_mode: List[str]
    help_patch_text: str
    is_reverse: bool = False
    index_only: bool = False
    apply_for_checkout: bool = False
    edit_hunk_hint: str = ""


_STAGE_HELP = (
    "y - stage this hunk\n"
    "n - do not stage this hunk\n"
    "q - quit; do not stage this hunk or any of the remaining ones\n"
    "a - stage this hunk and all later hunks in the file\n"
    "d - do not stage this hunk or any of the later hunks in the file\n"
)

PATCH_MODE_ADD = PatchMode(
    diff_cmd=["diff-files"],
    apply_args=["--cached"],
    apply_check_args=["--cached"],
    prompt_mode=[
        "Stage mode change%s [y,n,q,a,d%s,?]? ",
        "Stage deletion%s [y,n,q,a,d%s,?]? ",
        "Stage addition%s [y,n,q,a,d%s,?]? ",
        "Stage this hunk%s [y,n,q,a,d%s,?]? ",
    ],
    edit_hunk_hint=("If the patch applies cleanly, the edited hunk "
                    "will immediately be marked for staging."),
    help_patch_text=_STAGE_HELP,
)

PATCH_MODE_STASH = PatchMode(
    diff_cmd=["diff-index", "HEAD"],
    apply_args=["--cached"],
    apply_check_args=["--cached"],
    prompt_mode=[
        "Stash mode change%s [y,n,q,a,d%s,?]? ",
        "Stash deletion%s [y,n,q,a,d%s,?]? ",
        "Stash addition%s [y,n,q,a,d%s,?]? ",
        "Stash this hunk%s [y,n,q,a,d%s,?]? ",
    ],
    edit_hunk_hint=("If the patch applies cleanly, the edited hunk "
                    "will immediately be marked for stashing."),
    help_patch_text=(
        "y - stash this hunk\n"
        "n - do not stash this hunk\n"
        "q - quit; do not stash this hunk or any of the remaining ones\n"
        "a - stash this hunk and all later hunks in the file\n"
        "d - do not stash this hunk or any of the later hunks in the file\n"
    ),
)

PATCH_MODE_RESET_HEAD = PatchMode(
    diff_cmd=["diff-index", "--cached"],
    apply_args=["-R", "--cached"],
    apply_check_args=["-R", "--cached"],
    is_reverse=True,
    index_only=True,
    prompt_mode=[
        "Unstage mode change%s [y,n,q,a,d%s,?]? ",
        "Unstage deletion%s [y,n,q,a,d%s,?]? ",
        "Unstage addition%s [y,n,q,a,d%s,?]? ",
        "Unstage this hunk%s [y,n,q,a,d%s,?]? ",
    ],
    edit_hunk_hint=("If the patch applies cleanly, the edited hunk "
                    "will immediately be marked for unstaging."),
    help_patch_text=(
        "y - unstage this hunk\n"
        "n - do not unstage this hunk\n"
        "q - quit; do not unstage this hunk or any of the remaining ones\n"
        "a - unstage this hunk and all later hunks in the file\n"
        "d - do not unstage this hunk or any of the later hunks in the file\n"
    ),
)

PATCH_MODE_CHECKOUT_INDEX = PatchMode(
    diff_cmd=["diff-files"],
    apply_args=["-R"],
    apply_check_args=["-R"],
    is_reverse=True,
    prompt_mode=[
        "Discard mode change from worktree%s [y,n,q,a,d%s,?]? ",
        "Discard deletion from worktree%s [y,n,q,a,d%s,?]? ",
        "Discard addition from worktree%s [y,n,q,a,d%s,?]? ",
        "Discard this hunk from worktree%s [y,n,q,a,d%s,?]? ",
    ],
    edit_hunk_hint=("If the patch applies cleanly, the edited hunk "
                    "will immediately be marked for discarding."),
    help_patch_text=(
        "y - discard this hunk from worktree\n"
        "n - do not discard this hunk from worktree\n"
        "q - quit; do not discard this hunk or any of the remaining ones\n"
        "a - discard this hunk and all later hunks in the file\n"
        "d - do not discard this hunk or any of the later hunks in the file\n"
    ),
)

PATCH_MODE_RESET_NOTHEAD = PatchMode(
    diff_cmd=["diff-index", "-R", "--cached"],
    apply_args=["--cached"],
    apply_check_args=["--cached"],
    index_only=True,
    prompt_mode=[
        "Apply mode change to index%s [y,n,q,a,d%s,?]? ",
        "Apply deletion to index%s [y,n,q,a,d%s,?]? ",
        "Apply addition to index%s [y,n,q,a,d%s,?]? ",
        "Apply this hunk to index%s [y,n,q,a,d%s,?]? ",
    ],
    edit_hunk_hint=("If the patch applies cleanly, the edited hunk "
                    "will immediately be marked for applying."),
    help_patch_text=(
        "y - apply this hunk to index\n"
        "n - do not apply this hunk to index\n"
        "q - quit; do not apply this hunk or any of the remaining ones\n"
        "a - apply this hunk and all later hunks in the file\n"
        "d - do not apply this hunk or any of the later hunks in the file\n"
    ),
)


# ---------------------------------------------------------------------------
# Hunk model (ported from struct hunk / struct hunk_header)
# ---------------------------------------------------------------------------

UNDECIDED_HUNK = 0
SKIP_HUNK = 1
USE_HUNK = 2


@dataclass
class HunkHeader:
    old_offset: int = 0
    old_count: int = 0
    new_offset: int = 0
    new_count: int = 0
    extra_start: int = 0
    extra_end: int = 0


@dataclass
class Hunk:
    start: int = 0
    end: int = 0
    splittable_into: int = 0
    delta: int = 0
    use: int = UNDECIDED_HUNK
    header: HunkHeader = field(default_factory=HunkHeader)


@dataclass
class FileDiff:
    head: Hunk = field(default_factory=Hunk)
    hunk: List[Hunk] = field(default_factory=list)
    deleted: bool = False
    added: bool = False
    mode_change: bool = False
    binary: bool = False


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

DISPLAY_HUNKS_LINES = 20
SUMMARY_HEADER_WIDTH = 20
SUMMARY_LINE_WIDTH = 80

_HELP_PATCH_REMAINDER = (
    "j - go to the next undecided hunk, roll over at the bottom\n"
    "J - go to the next hunk, roll over at the bottom\n"
    "k - go to the previous undecided hunk, roll over at the top\n"
    "K - go to the previous hunk, roll over at the top\n"
    "g - select a hunk to go to\n"
    "/ - search for a hunk matching the given regex\n"
    "s - split the current hunk into smaller hunks\n"
    "e - manually edit the current hunk\n"
    "p - print the current hunk\n"
    "P - print the current hunk using the pager\n"
    "> - go to the next file, roll over at the bottom\n"
    "< - go to the previous file, roll over at the top\n"
    "? - print help\n"
    "HUNKS SUMMARY - Hunks: %d, USE: %d, SKIP: %d\n"
)


class AddPState:
    def __init__(self, repo, mode: PatchMode, revision: Optional[str],
                 context: int = -1, interhunkcontext: int = -1,
                 auto_advance: bool = True):
        self.repo = repo
        self.mode = mode
        self.revision = revision
        self.context = context
        self.interhunkcontext = interhunkcontext
        self.auto_advance = auto_advance
        self.plain = ""           # the parsed (uncolored) diff text
        self.file_diff: List[FileDiff] = []
        self.answer = ""
        # input/output streams (overridable for tests)
        self.stdin = sys.stdin
        self.stdout = sys.stdout


# ---------------------------------------------------------------------------
# In-process plumbing helpers (run pygit diff/apply with captured streams)
# ---------------------------------------------------------------------------

def _run_capture(func, argv: List[str]) -> str:
    """Run a cli command function capturing its stdout; return the text."""
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        # cli._print / _err write to sys.stdout / sys.stderr which we redirect.
        func(argv)
    finally:
        sys.stdout = old
    return buf.getvalue()


def _run_apply(repo, patch_text: str, args: List[str], *,
               quiet_stderr: bool = False) -> int:
    """Feed *patch_text* to pygit apply with *args*; return its rc.

    git's add-patch shells out to ``git apply`` with the child's stderr
    inherited, so apply's own diagnostics (``error: patch failed: ...``) appear
    on stderr — we let them through by default.  ``quiet_stderr`` mirrors the few
    call sites that pipe stderr to /dev/null (apply_for_checkout's checks)."""
    from . import cli
    old_stdin = sys.stdin
    old_stdout = sys.stdout
    old_stderr = sys.stderr
    sys.stdin = _StdinShim(patch_text)
    sys.stdout = io.StringIO()
    if quiet_stderr:
        sys.stderr = io.StringIO()
    try:
        rc = cli.cmd_apply(args)
    finally:
        sys.stdin = old_stdin
        sys.stdout = old_stdout
        sys.stderr = old_stderr
    return rc


class _StdinShim:
    def __init__(self, text: str):
        self._text = text
        self.buffer = io.BytesIO(text.encode("utf-8", errors="surrogateescape"))

    def read(self):
        return self._text


# ---------------------------------------------------------------------------
# Diff generation (ported from parse_diff's strvec construction)
# ---------------------------------------------------------------------------

def _generate_diff(s: AddPState, pathspec: List[str]) -> str:
    """Produce the plain (uncolored) diff that drives the loop.

    git's add-patch shells out to ``diff-files`` / ``diff-index`` with the mode's
    fixed arguments.  We map each mode onto pygit's byte-exact diff drivers:

      - ADD            -> ``diff-files`` (worktree vs index)
      - RESET HEAD     -> ``diff --cached HEAD``    (index vs HEAD)
      - RESET !HEAD    -> ``diff -R --cached <rev>`` (index vs <rev>, reversed)
      - STASH          -> ``diff HEAD``             (worktree vs HEAD)
    """
    from . import cli
    cmd = s.mode.diff_cmd[0]
    extra = list(s.mode.diff_cmd[1:])  # HEAD / --cached / -R, mode-specific

    if cmd == "diff-files":
        args: List[str] = []
        if s.context != -1:
            args.append("-U%d" % s.context)
        if s.interhunkcontext != -1:
            args.append("--inter-hunk-context=%d" % s.interhunkcontext)
        args.append("-p")
        args.append("--")
        args.extend(pathspec)
        return _run_capture(cli.cmd_diff_files, args)

    # diff-index modes go through cmd_diff (supports --cached/-R/-U/paths).
    args = []
    if s.context != -1:
        args.append("-U%d" % s.context)
    if s.interhunkcontext != -1:
        args.append("--inter-hunk-context=%d" % s.interhunkcontext)
    if "-R" in extra:
        args.append("-R")
    if "--cached" in extra:
        args.append("--cached")
    # The revision: HEAD literal in diff_cmd, or s.revision for the nothead modes.
    rev = None
    for tok in extra:
        if tok not in ("-R", "--cached"):
            rev = tok
            break
    if s.revision and rev == "HEAD":
        rev = s.revision
    elif s.revision and rev is None:
        rev = s.revision
    if rev:
        args.append(rev)
    args.append("--")
    args.extend(pathspec)
    return _run_capture(cli.cmd_diff, args)


# ---------------------------------------------------------------------------
# Parsing (ported from parse_diff / parse_hunk_header)
# ---------------------------------------------------------------------------

def _find_next_line(buf: str, offset: int) -> int:
    nl = buf.find("\n", offset)
    if nl < 0:
        return len(buf)
    return nl + 1


def _normalize_marker(buf: str, pos: int) -> str:
    c = buf[pos] if pos < len(buf) else ""
    if c == "\n" or (c == "\r" and pos + 1 < len(buf) and buf[pos + 1] == "\n"):
        return " "
    return c


_RANGE_RE = re.compile(r"(\d+)(?:,(\d+))?")


def _parse_range(text: str, pos: int):
    """Return (offset, count, new_pos) or None on failure."""
    m = re.match(r"(\d+)", text[pos:])
    if not m:
        return None
    offset = int(m.group(1))
    pos += m.end()
    if pos >= len(text) or text[pos] != ",":
        return offset, 1, pos
    m2 = re.match(r"(\d+)", text[pos + 1:])
    if not m2:
        return None
    count = int(m2.group(1))
    pos = pos + 1 + m2.end()
    return offset, count, pos


def _parse_hunk_header(s: AddPState, hunk: Hunk) -> int:
    plain = s.plain
    line_start = hunk.start
    eol = plain.find("\n", line_start)
    if eol < 0:
        eol = len(plain)
    p = line_start
    if not plain.startswith("@@ -", p):
        return -1
    p += 4
    r = _parse_range(plain, p)
    if r is None:
        return -1
    hunk.header.old_offset, hunk.header.old_count, p = r
    if not plain.startswith(" +", p):
        return -1
    p += 2
    r = _parse_range(plain, p)
    if r is None:
        return -1
    hunk.header.new_offset, hunk.header.new_count, p = r
    if not plain.startswith(" @@", p):
        return -1
    p += 3
    # hunk->start advances past the header line
    hunk.start = eol + (1 if (eol < len(plain) and plain[eol] == "\n") else 0)
    hunk.header.extra_start = p
    hunk.header.extra_end = hunk.start
    return 0


def _is_octal(text: str) -> bool:
    if not text:
        return False
    return all("0" <= c <= "7" for c in text)


def _complete_file(marker: str, hunk: Optional[Hunk]) -> None:
    if hunk is not None and marker in ("-", "+"):
        hunk.splittable_into += 1


def parse_diff(s: AddPState, pathspec: List[str]) -> int:
    plain = _generate_diff(s, pathspec)
    s.plain = plain
    if not plain:
        return 0
    if not plain.endswith("\n"):
        plain += "\n"
        s.plain = plain

    s.file_diff = []
    file_diff: Optional[FileDiff] = None
    hunk: Optional[Hunk] = None
    marker = ""
    p = 0
    pend = len(plain)
    while p != pend:
        eol = plain.find("\n", p)
        if eol < 0:
            eol = pend
        deleted = None
        mode_change = None
        ch = _normalize_marker(plain, p)

        if plain.startswith("diff ", p) or plain.startswith("* Unmerged path ", p):
            _complete_file(marker, hunk)
            file_diff = FileDiff()
            s.file_diff.append(file_diff)
            hunk = file_diff.head
            hunk.start = p
            marker = ""
        elif p == 0:
            raise RuntimeError("diff starts with unexpected line")
        elif file_diff.deleted:
            pass  # keep the rest of the file in a single "hunk"
        elif plain.startswith("@@ ", p) or (
                hunk is file_diff.head and plain.startswith("deleted file", p)):
            if plain.startswith("deleted file", p):
                deleted = "deleted file"
            if marker in ("-", "+"):
                hunk.splittable_into += 1
            new_hunk = Hunk()
            file_diff.hunk.append(new_hunk)
            hunk = new_hunk
            hunk.start = p
            if deleted:
                file_diff.deleted = True
            elif _parse_hunk_header(s, hunk) < 0:
                return -1
            marker = ch
        elif hunk is file_diff.head and plain.startswith("new file", p):
            file_diff.added = True
        elif (hunk is file_diff.head and plain.startswith("old mode ", p)
              and _is_octal(plain[p + len("old mode "):eol])):
            mode_change = plain[p + len("old mode "):eol]
            file_diff.mode_change = True
            ph = Hunk()
            file_diff.hunk.append(ph)
            ph.start = p
        elif (hunk is file_diff.head and plain.startswith("new mode ", p)
              and _is_octal(plain[p + len("new mode "):eol])):
            # Extend the mode-change pseudo-hunk to include the "new mode" line.
            mode_change = plain[p + len("new mode "):eol]
        elif hunk is file_diff.head and plain.startswith("Binary files ", p):
            file_diff.binary = True

        if (marker in ("-", "+")) and ch == " ":
            hunk.splittable_into += 1
        if marker and ch != "\\":
            marker = ch

        p = pend if eol == pend else eol + 1
        hunk.end = p

        if mode_change is not None:
            file_diff.hunk[0].end = hunk.end

    _complete_file(marker, hunk)
    return 0


# ---------------------------------------------------------------------------
# Rendering (ported from render_hunk / render_diff_header)
# ---------------------------------------------------------------------------

def _render_hunk(s: AddPState, hunk: Hunk, delta: int) -> str:
    out = []
    header = hunk.header
    if header.old_offset != 0 or header.new_offset != 0:
        old_offset = header.old_offset
        new_offset = header.new_offset
        extra = s.plain[header.extra_start:header.extra_end]
        if s.mode.is_reverse:
            old_offset -= delta
        else:
            new_offset += delta
        line = "@@ -%d" % old_offset
        if header.old_count != 1:
            line += ",%d" % header.old_count
        line += " +%d" % new_offset
        if header.new_count != 1:
            line += ",%d" % header.new_count
        line += " @@"
        out.append(line)
        if extra:
            out.append(extra)
        else:
            out.append("\n")
    out.append(s.plain[hunk.start:hunk.end])
    return "".join(out)


def _render_diff_header(s: AddPState, file_diff: FileDiff) -> str:
    skip_mode_change = file_diff.mode_change and file_diff.hunk[0].use != USE_HUNK
    head = file_diff.head
    if not skip_mode_change:
        return _render_hunk(s, head, 0)
    first = file_diff.hunk[0]
    p = s.plain
    return (p[head.start:first.start] + p[first.end:head.end])


# ---------------------------------------------------------------------------
# Hunk merging (ported from merge_hunks) and patch reassembly
# ---------------------------------------------------------------------------

def _merge_hunks(s: AddPState, file_diff: FileDiff, hunk_index: int,
                 use_all: bool):
    """Return (merged_hunk_or_None, new_index). Mirrors merge_hunks()."""
    i = hunk_index
    hunk = file_diff.hunk[i]
    if (not use_all) and hunk.use != USE_HUNK:
        return None, i
    merged = Hunk(start=hunk.start, end=hunk.end,
                  splittable_into=hunk.splittable_into, delta=hunk.delta,
                  use=hunk.use,
                  header=HunkHeader(hunk.header.old_offset, hunk.header.old_count,
                                    hunk.header.new_offset, hunk.header.new_count,
                                    hunk.header.extra_start, hunk.header.extra_end))
    header = merged.header
    while i + 1 < len(file_diff.hunk):
        i += 1
        nh = file_diff.hunk[i]
        nxt = nh.header
        if ((not use_all) and nh.use != USE_HUNK) or \
           header.new_offset >= nxt.new_offset + merged.delta or \
           header.new_offset + header.new_count < nxt.new_offset + merged.delta:
            i -= 1
            break
        if merged.start < nh.start and merged.end > nh.start:
            merged.end = nh.end
            delta = 0
        else:
            # An edited hunk was appended to s.plain; coalesce by overlap.
            plain = s.plain
            overlapping_line_count = (header.new_offset + header.new_count
                                      - merged.delta - nxt.new_offset)
            overlap_end = nh.start
            overlap_start = overlap_end
            for _j in range(overlapping_line_count):
                overlap_next = _find_next_line(plain, overlap_end)
                if _normalize_marker(plain, overlap_end) != " ":
                    return None, hunk_index  # error: expected context line
                overlap_start = overlap_end
                overlap_end = overlap_next
            length = overlap_end - overlap_start
            if length > merged.end - merged.start or \
               plain[merged.end - length:merged.end] != plain[overlap_start:overlap_end]:
                return None, hunk_index  # hunks do not overlap
            if merged.end != len(s.plain):
                start = len(s.plain)
                s.plain += plain[merged.start:merged.end]
                plain = s.plain
                merged.start = start
                merged.end = len(s.plain)
            s.plain += plain[overlap_end:nh.end]
            merged.end = len(s.plain)
            merged.splittable_into += nh.splittable_into
            delta = merged.delta
            merged.delta += nh.delta
        header.old_count = nxt.old_offset + nxt.old_count - header.old_offset
        header.new_count = nxt.new_offset + delta + nxt.new_count - header.new_offset
    if i == hunk_index:
        return None, i
    return merged, i


def reassemble_patch(s: AddPState, file_diff: FileDiff, use_all: bool) -> str:
    out = [_render_diff_header(s, file_diff)]
    save_len = len(s.plain)
    delta = 0
    i = 1 if file_diff.mode_change else 0
    while i < len(file_diff.hunk):
        hunk = file_diff.hunk[i]
        if (not use_all) and hunk.use != USE_HUNK:
            delta += hunk.header.old_count - hunk.header.new_count
        else:
            merged, new_i = _merge_hunks(s, file_diff, i, use_all)
            if merged is not None:
                hunk = merged
                i = new_i
            out.append(_render_hunk(s, hunk, delta))
            s.plain = s.plain[:save_len]
            delta += hunk.delta
        i += 1
    return "".join(out)


# ---------------------------------------------------------------------------
# Hunk splitting (ported from split_hunk)
# ---------------------------------------------------------------------------

def split_hunk(s: AddPState, file_diff: FileDiff, hunk_index: int) -> int:
    hunk = file_diff.hunk[hunk_index]
    first = True
    if hunk.splittable_into < 2:
        return 0
    splittable_into = hunk.splittable_into
    end = hunk.end
    remaining = HunkHeader(hunk.header.old_offset, hunk.header.old_count,
                           hunk.header.new_offset, hunk.header.new_count,
                           hunk.header.extra_start, hunk.header.extra_end)

    # Insert (splittable_into - 1) fresh hunks right after hunk_index.
    new_hunks = [Hunk() for _ in range(splittable_into - 1)]
    file_diff.hunk[hunk_index + 1:hunk_index + 1] = new_hunks
    hunk = file_diff.hunk[hunk_index]
    hunk.splittable_into = 1
    hunk.use = UNDECIDED_HUNK
    header = hunk.header
    header.old_count = header.new_count = 0
    # the extra (function context) text stays attached to the first split hunk
    # via its extra_start/extra_end; the others inherit offset 0 headers.

    current = hunk.start
    marker = ""
    context_line_count = 0
    cur_pos = hunk_index

    while splittable_into > 1:
        ch = _normalize_marker(s.plain, current)
        if not ch:
            raise RuntimeError("buffer overrun while splitting hunks")

        if marker in ("-", "+") and ch == " ":
            first = False
            file_diff.hunk[cur_pos + 1].start = current
            context_line_count = 0

        if marker != " " or (ch != "-" and ch != "+"):
            # next_hunk_line
            if ch == "\\":
                ch = marker if marker else " "
            if ch == " ":
                context_line_count += 1
            elif ch == "-":
                header.old_count += 1
            elif ch == "+":
                header.new_count += 1
            else:
                raise RuntimeError("unhandled diff marker: %r" % ch)
            marker = ch
            current = _find_next_line(s.plain, current)
            continue

        # start of a new hunk (a context line shared with the previous one)
        if first:
            header.old_count = context_line_count
            header.new_count = context_line_count
            context_line_count = 0
            first = False
            # goto next_hunk_line for this same char
            ch2 = ch
            if ch2 == " ":
                context_line_count += 1
            elif ch2 == "-":
                header.old_count += 1
            elif ch2 == "+":
                header.new_count += 1
            marker = ch2
            current = _find_next_line(s.plain, current)
            continue

        remaining.old_offset += header.old_count
        remaining.old_count -= header.old_count
        remaining.new_offset += header.new_count
        remaining.new_count -= header.new_count

        nh = file_diff.hunk[cur_pos + 1]
        nh.header.old_offset = header.old_offset + header.old_count
        nh.header.new_offset = header.new_offset + header.new_count

        header.old_count += context_line_count
        header.new_count += context_line_count

        hunk.end = current

        cur_pos += 1
        hunk = file_diff.hunk[cur_pos]
        hunk.splittable_into = 1
        hunk.use = UNDECIDED_HUNK
        header = hunk.header
        header.old_count = header.new_count = context_line_count
        context_line_count = 0
        splittable_into -= 1
        marker = ch

    header.old_count = remaining.old_count
    header.new_count = remaining.new_count
    hunk.end = end
    return 0


# ---------------------------------------------------------------------------
# Editing (ported from edit_hunk_manually / edit_hunk_loop / recount)
# ---------------------------------------------------------------------------

# default comment char
_COMMENT = "#"


def _edit_hunk_manually(s: AddPState, hunk: Hunk, edit_fn) -> int:
    buf = []
    buf.append(_commented("Manual hunk edit mode -- see bottom for a quick guide.\n"))
    buf.append(_render_hunk(s, hunk, 0))
    rm_ctx = "+" if s.mode.is_reverse else "-"
    rm_del = "-" if s.mode.is_reverse else "+"
    buf.append(_commented(
        "---\n"
        "To remove '%s' lines, make them ' ' lines (context).\n"
        "To remove '%s' lines, delete them.\n"
        "Lines starting with %s will be removed.\n" % (rm_ctx, rm_del, _COMMENT)))
    buf.append(_commented(s.mode.edit_hunk_hint + "\n"))
    buf.append(_commented(
        "If it does not apply cleanly, you will be given an opportunity to\n"
        "edit again.  If all lines of the hunk are removed, then the edit is\n"
        "aborted and the hunk is left unchanged.\n"))
    text = "".join(buf)
    edited = edit_fn(text)
    if edited is None:
        return -1
    # strip out commented lines
    hunk.start = len(s.plain)
    i = 0
    out = []
    while i < len(edited):
        nxt = _find_next_line(edited, i)
        if not edited.startswith(_COMMENT, i):
            out.append(edited[i:nxt])
        i = nxt
    s.plain += "".join(out)
    hunk.end = len(s.plain)
    if hunk.end == hunk.start:
        return 0  # aborted: everything deleted
    if s.plain[hunk.start:hunk.start + 1] == "@" and _parse_hunk_header(s, hunk) < 0:
        return -1
    return 1


def _commented(text: str) -> str:
    out = []
    for line in text.splitlines(keepends=True):
        if line == "\n" or line == "":
            out.append(_COMMENT + line)
        else:
            out.append(_COMMENT + " " + line)
    return "".join(out)


def _recount_edited_hunk(s: AddPState, hunk: Hunk,
                         orig_old: int, orig_new: int) -> int:
    header = hunk.header
    hunk.splittable_into = 0
    header.old_count = header.new_count = 0
    marker = " "
    i = hunk.start
    while i < hunk.end:
        ch = _normalize_marker(s.plain, i)
        if ch == "-":
            header.old_count += 1
            if marker == " ":
                hunk.splittable_into += 1
            marker = ch
        elif ch == "+":
            header.new_count += 1
            if marker == " ":
                hunk.splittable_into += 1
            marker = ch
        elif ch == " ":
            header.old_count += 1
            header.new_count += 1
            marker = ch
        i = _find_next_line(s.plain, i)
    return orig_old - orig_new - header.old_count + header.new_count


def _run_apply_check(s: AddPState, file_diff: FileDiff) -> int:
    patch = reassemble_patch(s, file_diff, True)
    # pygit cmd_apply argv excludes the leading "apply".
    rc = _run_apply(s.repo, patch, ["--check"] + s.mode.apply_check_args)
    if rc != 0:
        # run_apply_check(): error() prints "error: 'git apply --cached' failed".
        sys.stderr.write("error: 'git apply --cached' failed\n")
        return -1
    return 0


def _edit_hunk_loop(s: AddPState, file_diff: FileDiff, hunk: Hunk, edit_fn) -> int:
    plain_len = len(s.plain)
    backup = Hunk(hunk.start, hunk.end, hunk.splittable_into, hunk.delta,
                  hunk.use,
                  HunkHeader(hunk.header.old_offset, hunk.header.old_count,
                             hunk.header.new_offset, hunk.header.new_count,
                             hunk.header.extra_start, hunk.header.extra_end))
    while True:
        res = _edit_hunk_manually(s, hunk, edit_fn)
        if res == 0:
            _restore_hunk(hunk, backup)
            return -1
        if res > 0:
            hunk.delta += _recount_edited_hunk(s, hunk, backup.header.old_count,
                                               backup.header.new_count)
            if _run_apply_check(s, file_diff) == 0:
                return 0
        s.plain = s.plain[:plain_len]
        _restore_hunk(hunk, backup)
        if _prompt_yesno(s, 'Your edited hunk does not apply. Edit again '
                            '(saying "no" discards!) [y/n]? ') < 1:
            return -1


def _restore_hunk(hunk: Hunk, backup: Hunk) -> None:
    hunk.start = backup.start
    hunk.end = backup.end
    hunk.splittable_into = backup.splittable_into
    hunk.delta = backup.delta
    hunk.use = backup.use
    hunk.header = HunkHeader(backup.header.old_offset, backup.header.old_count,
                             backup.header.new_offset, backup.header.new_count,
                             backup.header.extra_start, backup.header.extra_end)


# ---------------------------------------------------------------------------
# Prompt helpers
# ---------------------------------------------------------------------------

def _readline(s: AddPState) -> Optional[str]:
    line = s.stdin.readline()
    if line == "":
        return None
    # strip a single trailing newline (and a preceding \r)
    if line.endswith("\n"):
        line = line[:-1]
    if line.endswith("\r"):
        line = line[:-1]
    return line


def _prompt_yesno(s: AddPState, prompt: str) -> int:
    while True:
        s.stdout.write(prompt)
        s.stdout.flush()
        ans = _readline(s)
        if ans is None:
            return -1
        if not ans:
            continue
        c = ans[0].lower()
        if c == "n":
            return 0
        if c == "y":
            return 1


def _err(s: AddPState, fmt: str, *args) -> None:
    s.stdout.write((fmt % args if args else fmt) + "\n")


# ---------------------------------------------------------------------------
# Summaries (ported from summarize_hunk / display_hunks)
# ---------------------------------------------------------------------------

def _summarize_hunk(s: AddPState, hunk: Hunk) -> str:
    header = hunk.header
    out = " -%d,%d +%d,%d " % (header.old_offset, header.old_count,
                               header.new_offset, header.new_count)
    if len(out) < SUMMARY_HEADER_WIDTH:
        out += " " * (SUMMARY_HEADER_WIDTH - len(out))
    plain = s.plain
    i = hunk.start
    while i < hunk.end:
        if plain[i] != " ":
            break
        i = _find_next_line(plain, i)
    if i < hunk.end:
        out += plain[i:_find_next_line(plain, i)]
    if len(out) > SUMMARY_LINE_WIDTH:
        out = out[:SUMMARY_LINE_WIDTH]
    if not out.endswith("\n"):
        out += "\n"
    return out


def _display_hunks(s: AddPState, file_diff: FileDiff, start_index: int) -> int:
    end_index = start_index + DISPLAY_HUNKS_LINES
    if end_index > len(file_diff.hunk):
        end_index = len(file_diff.hunk)
    i = start_index
    while i < end_index:
        hunk = file_diff.hunk[i]
        i += 1
        prefix = "+" if hunk.use == USE_HUNK else ("-" if hunk.use == SKIP_HUNK else " ")
        s.stdout.write("%c%2d: " % (prefix, i) + _summarize_hunk(s, hunk))
    return end_index


# ---------------------------------------------------------------------------
# The interactive per-file loop (ported from patch_update_file)
# ---------------------------------------------------------------------------

def _dec_mod(a: int, m: int) -> int:
    return a - 1 if a > 0 else m - 1


def _inc_mod(a: int, m: int) -> int:
    return a + 1 if a < m - 1 else 0


def _get_first_undecided(file_diff: FileDiff):
    for i, h in enumerate(file_diff.hunk):
        if h.use == UNDECIDED_HUNK:
            return i
    return None


ADD_P_DISALLOW_EDIT = 1 << 0


def patch_update_file(s: AddPState, idx: int, flags: int, edit_fn) -> int:
    file_diff = s.file_diff[idx]
    patch_update_resp = idx
    hunk_nr = len(file_diff.hunk)

    if hunk_nr == 0 and not file_diff.added:
        return patch_update_resp + 1

    out = _render_diff_header(s, file_diff)
    s.stdout.write(out)

    hunk_index = 0
    rendered_hunk_index = -1
    all_decided = False

    while True:
        hunk_nr = len(file_diff.hunk)
        if hunk_index >= hunk_nr:
            hunk_index = 0
        hunk = file_diff.hunk[hunk_index] if hunk_nr else file_diff.head
        undecided_previous = -1
        undecided_next = -1

        if hunk_nr:
            i = _dec_mod(hunk_index, hunk_nr)
            while i != hunk_index:
                if file_diff.hunk[i].use == UNDECIDED_HUNK:
                    undecided_previous = i
                    break
                i = _dec_mod(i, hunk_nr)
            i = _inc_mod(hunk_index, hunk_nr)
            while i != hunk_index:
                if file_diff.hunk[i].use == UNDECIDED_HUNK:
                    undecided_next = i
                    break
                i = _inc_mod(i, hunk_nr)

        if undecided_previous < 0 and undecided_next < 0 and hunk.use != UNDECIDED_HUNK:
            if not s.auto_advance:
                all_decided = True
            else:
                patch_update_resp += 1
                break

        permitted_buf = []  # the ",x" suffix string shown in the prompt
        ALLOW_PREV_HUNK = ALLOW_PREV_UNDEC = ALLOW_NEXT_HUNK = False
        ALLOW_NEXT_UNDEC = ALLOW_SEARCH = ALLOW_SPLIT = ALLOW_EDIT = False
        ALLOW_PREV_FILE = ALLOW_NEXT_FILE = False

        if hunk_nr:
            if rendered_hunk_index != hunk_index:
                s.stdout.write(_render_hunk(s, hunk, 0))
                rendered_hunk_index = hunk_index

            if undecided_previous >= 0:
                ALLOW_PREV_UNDEC = True
                permitted_buf.append(",k")
            if hunk_nr > 1:
                ALLOW_PREV_HUNK = True
                permitted_buf.append(",K")
            if undecided_next >= 0:
                ALLOW_NEXT_UNDEC = True
                permitted_buf.append(",j")
            if hunk_nr > 1:
                ALLOW_NEXT_HUNK = True
                permitted_buf.append(",J")
            if hunk_nr > 1:
                ALLOW_SEARCH = True
                permitted_buf.append(",g,/")
            if hunk.splittable_into > 1:
                ALLOW_SPLIT = True
                permitted_buf.append(",s")
            if (not (flags & ADD_P_DISALLOW_EDIT)
                    and hunk_index + 1 > (1 if file_diff.mode_change else 0)
                    and not file_diff.deleted):
                ALLOW_EDIT = True
                permitted_buf.append(",e")
            if not s.auto_advance and len(s.file_diff) > 1:
                ALLOW_NEXT_FILE = True
                permitted_buf.append(",>")
            if not s.auto_advance and len(s.file_diff) > 1:
                ALLOW_PREV_FILE = True
                permitted_buf.append(",<")
            permitted_buf.append(",p,P")
        permitted = "".join(permitted_buf)

        if file_diff.deleted:
            prompt_mode_type = PROMPT_DELETION
        elif file_diff.added:
            prompt_mode_type = PROMPT_ADDITION
        elif file_diff.mode_change and not hunk_index:
            prompt_mode_type = PROMPT_MODE_CHANGE
        else:
            prompt_mode_type = PROMPT_HUNK

        s.stdout.write("(%d/%d) " % (hunk_index + 1, hunk_nr if hunk_nr else 1))
        if hunk.use != UNDECIDED_HUNK:
            was = " (was: y)" if hunk.use == USE_HUNK else " (was: n)"
        else:
            was = ""
        s.stdout.write(s.mode.prompt_mode[prompt_mode_type] % (was, permitted))
        s.stdout.flush()

        ans = _readline(s)
        if ans is None:
            patch_update_resp = len(s.file_diff)
            break
        s.answer = ans
        if not ans:
            continue
        ch = ans[0].lower()

        if len(ans) != 1 and ch not in ("g", "/"):
            _err(s, "Only one letter is expected, got '%s'", ans)
            continue

        if ch == "y":
            hunk.use = USE_HUNK
            hunk_index = hunk_nr if undecided_next < 0 else undecided_next
        elif ch == "n":
            hunk.use = SKIP_HUNK
            hunk_index = hunk_nr if undecided_next < 0 else undecided_next
        elif ch == "a":
            if hunk_nr:
                while hunk_index < hunk_nr:
                    hh = file_diff.hunk[hunk_index]
                    if hh.use == UNDECIDED_HUNK:
                        hh.use = USE_HUNK
                    hunk_index += 1
                first_undec = _get_first_undecided(file_diff)
                hunk_index = first_undec if first_undec is not None else 0
            elif hunk.use == UNDECIDED_HUNK:
                hunk.use = USE_HUNK
        elif ch == "d":
            if hunk_nr:
                while hunk_index < hunk_nr:
                    hh = file_diff.hunk[hunk_index]
                    if hh.use == UNDECIDED_HUNK:
                        hh.use = SKIP_HUNK
                    hunk_index += 1
                first_undec = _get_first_undecided(file_diff)
                hunk_index = first_undec if first_undec is not None else 0
            elif hunk.use == UNDECIDED_HUNK:
                hunk.use = SKIP_HUNK
        elif ch == "q":
            patch_update_resp = len(s.file_diff)
            break
        elif not s.auto_advance and ans[0] == ">":
            if ALLOW_NEXT_FILE:
                if patch_update_resp == len(s.file_diff) - 1:
                    patch_update_resp = 0
                else:
                    patch_update_resp += 1
                break
            else:
                _err(s, "No next file")
                continue
        elif not s.auto_advance and ans[0] == "<":
            if ALLOW_PREV_FILE:
                if patch_update_resp == 0:
                    patch_update_resp = len(s.file_diff) - 1
                else:
                    patch_update_resp -= 1
                break
            else:
                _err(s, "No previous file")
                continue
        elif ans[0] == "K":
            if ALLOW_PREV_HUNK:
                hunk_index = _dec_mod(hunk_index, hunk_nr)
            else:
                _err(s, "No other hunk")
        elif ans[0] == "J":
            if ALLOW_NEXT_HUNK:
                hunk_index += 1
            else:
                _err(s, "No other hunk")
        elif ans[0] == "k":
            if ALLOW_PREV_UNDEC:
                hunk_index = undecided_previous
            else:
                _err(s, "No other undecided hunk")
        elif ans[0] == "j":
            if ALLOW_NEXT_UNDEC:
                hunk_index = undecided_next
            else:
                _err(s, "No other undecided hunk")
        elif ans[0] == "g":
            if not ALLOW_SEARCH:
                _err(s, "No other hunks to goto")
                continue
            arg = ans[1:].strip()
            i = hunk_index - DISPLAY_HUNKS_LINES // 2
            mc = 1 if file_diff.mode_change else 0
            if i < mc:
                i = mc
            while arg == "":
                i = _display_hunks(s, file_diff, i)
                if i < len(file_diff.hunk):
                    s.stdout.write("go to which hunk (<ret> to see more)? ")
                else:
                    s.stdout.write("go to which hunk? ")
                s.stdout.flush()
                line = _readline(s)
                if line is None:
                    arg = None
                    break
                arg = line.strip()
            if arg is None:
                break
            try:
                response = int(arg)
                ok = True
            except ValueError:
                ok = False
            if not ok or _has_trailing(arg):
                _err(s, "Invalid number: '%s'", arg)
            elif 0 < response <= len(file_diff.hunk):
                hunk_index = response - 1
            else:
                n = len(file_diff.hunk)
                if n == 1:
                    _err(s, "Sorry, only %d hunk available.", n)
                else:
                    _err(s, "Sorry, only %d hunks available.", n)
        elif ans[0] == "/":
            if not ALLOW_SEARCH:
                _err(s, "No other hunks to search")
                continue
            arg = ans[1:].strip()
            if arg == "":
                s.stdout.write("search for regex? ")
                s.stdout.flush()
                line = _readline(s)
                if line is None:
                    break
                arg = line.strip()
                if arg == "":
                    continue
            try:
                regex = re.compile(arg)
            except re.error as exc:
                _err(s, "Malformed search regexp %s: %s", arg, str(exc))
                continue
            i = hunk_index
            found = True
            while True:
                buf = _render_hunk(s, file_diff.hunk[i], 0)
                if regex.search(buf):
                    break
                i += 1
                if i == len(file_diff.hunk):
                    i = 0
                if i != hunk_index:
                    continue
                _err(s, "No hunk matches the given pattern")
                break
            hunk_index = i
        elif ans[0] == "s":
            splittable_into = hunk.splittable_into
            if not ALLOW_SPLIT:
                _err(s, "Sorry, cannot split this hunk")
            elif split_hunk(s, file_diff, file_diff.hunk.index(hunk)) == 0:
                s.stdout.write("Split into %d hunks.\n" % splittable_into)
                rendered_hunk_index = -1
        elif ans[0] == "e":
            if not ALLOW_EDIT:
                _err(s, "Sorry, cannot edit this hunk")
            elif _edit_hunk_loop(s, file_diff, hunk, edit_fn) >= 0:
                hunk.use = USE_HUNK
                hunk_index = hunk_nr if undecided_next < 0 else undecided_next
        elif ch == "p":
            rendered_hunk_index = -1
        elif ans[0] == "?":
            s.stdout.write(s.mode.help_patch_text)
            for line in _HELP_PATCH_REMAINDER.split("\n"):
                if line == "":
                    continue
                lstart = line[0]
                if all_decided and line.startswith("HUNKS SUMMARY"):
                    total = len(file_diff.hunk)
                    used = sum(1 for h in file_diff.hunk if h.use == USE_HUNK)
                    skipped = sum(1 for h in file_diff.hunk if h.use == SKIP_HUNK)
                    s.stdout.write((line % (total, used, skipped)) + "\n")
                    continue
                if lstart != "?" and lstart not in permitted:
                    continue
                s.stdout.write(line + "\n")
        else:
            _err(s, "Unknown command '%s' (use '?' for help)", ans)

    if s.auto_advance:
        apply_patch(s, file_diff)

    s.stdout.write("\n")
    return patch_update_resp


def _has_trailing(arg: str) -> bool:
    """True if arg has trailing characters after an integer (strtoul *pend)."""
    m = re.match(r"\s*[+-]?\d+", arg)
    if not m:
        return True
    return arg[m.end():] != ""


# ---------------------------------------------------------------------------
# Applying (ported from apply_patch / apply_for_checkout)
# ---------------------------------------------------------------------------

def apply_patch(s: AddPState, file_diff: FileDiff) -> None:
    any_use = any(h.use == USE_HUNK for h in file_diff.hunk)
    if not any_use and not (not file_diff.hunk and file_diff.head.use == USE_HUNK):
        return
    patch = reassemble_patch(s, file_diff, False)
    if s.mode.apply_for_checkout:
        _apply_for_checkout(s, patch, s.mode.is_reverse)
    else:
        rc = _run_apply(s.repo, patch, list(s.mode.apply_args))
        if rc != 0:
            # apply_patch(): error() prints "error: 'git apply' failed".
            sys.stderr.write("error: 'git apply' failed\n")


def _apply_for_checkout(s: AddPState, patch: str, is_reverse: bool) -> None:
    reverse = ["-R"] if is_reverse else []
    applies_index = _run_apply(s.repo, patch, ["--cached", "--check"] + reverse) == 0
    applies_worktree = _run_apply(s.repo, patch, ["--check"] + reverse) == 0
    if applies_worktree and applies_index:
        _run_apply(s.repo, patch, ["--cached"] + reverse)
        _run_apply(s.repo, patch, reverse)
        return
    if not applies_index:
        _err(s, "The selected hunks do not apply to the index!")
        if _prompt_yesno(s, "Apply them to the worktree anyway? ") > 0:
            _run_apply(s.repo, patch, reverse)
            return
        _err(s, "Nothing was applied.\n")
    else:
        s.stdout.write(patch)


# ---------------------------------------------------------------------------
# Top-level driver (ported from run_add_p_common / run_add_p)
# ---------------------------------------------------------------------------

def run_add_p_common(s: AddPState, pathspec: List[str], flags: int, edit_fn) -> int:
    if parse_diff(s, pathspec) < 0:
        return -1
    binary_count = 0
    i = 0
    while i < len(s.file_diff):
        fd = s.file_diff[i]
        if fd.binary and not fd.hunk:
            binary_count += 1
            i += 1
            continue
        i = patch_update_file(s, i, flags, edit_fn)
        if i == len(s.file_diff):
            break
    if not s.auto_advance:
        for fd in s.file_diff:
            apply_patch(s, fd)
    if len(s.file_diff) == 0:
        _err(s, "No changes.")
    elif binary_count == len(s.file_diff):
        _err(s, "Only binary files changed.")
    return 0


def run_add_p(repo, mode_kind: str, revision: Optional[str],
              pathspec: List[str], *, context: int = -1,
              interhunkcontext: int = -1, auto_advance: bool = True,
              flags: int = 0, edit_fn=None,
              stdin=None, stdout=None) -> int:
    """Entry point. *mode_kind* is one of: add, stash, reset.

    Returns 0 on success, non-zero on failure (matching run_add_p semantics:
    the caller turns this into a process rc).
    """
    if mode_kind == "stash":
        mode = PATCH_MODE_STASH
    elif mode_kind == "reset":
        if not revision or revision == "HEAD":
            mode = PATCH_MODE_RESET_HEAD
        else:
            mode = PATCH_MODE_RESET_NOTHEAD
    elif mode_kind == "checkout":
        # `git checkout -p` with no tree-ish: discard worktree hunks
        # (patch_mode_checkout_index).
        mode = PATCH_MODE_CHECKOUT_INDEX
    else:
        mode = PATCH_MODE_ADD

    s = AddPState(repo, mode, revision, context, interhunkcontext, auto_advance)
    if stdin is not None:
        s.stdin = stdin
    if stdout is not None:
        s.stdout = stdout
    if edit_fn is None:
        edit_fn = _default_edit_fn(repo)
    return run_add_p_common(s, pathspec, flags, edit_fn)


def _default_edit_fn(repo):
    """Return a callable that edits text via core.editor and returns the result.

    Mirrors strbuf_edit_interactively(): write to a temp file under .git,
    launch the editor, read it back.  Returns None if the editor fails.
    """
    def edit(text: str) -> Optional[str]:
        import subprocess
        from . import cli
        editor = _editor_command(repo)
        path = repo.gitdir / "addp-hunk-edit.diff"
        path.write_text(text, encoding="utf-8")
        try:
            rc = subprocess.call("%s %s" % (editor, _shell_quote(str(path))),
                                 shell=True)
        except OSError:
            return None
        if rc != 0:
            return None
        try:
            return path.read_text(encoding="utf-8")
        finally:
            try:
                path.unlink()
            except OSError:
                pass
    return edit


def _editor_command(repo) -> str:
    # git_editor() precedence: $GIT_EDITOR, core.editor, $VISUAL, $EDITOR, vi.
    import os
    from . import gitconfig
    val = os.environ.get("GIT_EDITOR")
    if not val:
        try:
            val = gitconfig.get(repo, "core.editor")
        except Exception:
            val = None
    if not val:
        val = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not val:
        val = "vi"
    return val


def _shell_quote(p: str) -> str:
    import shlex
    return shlex.quote(p)


# ===========================================================================
# The `-i` interactive menu (ported from add-interactive.c)
# ===========================================================================

NO_FILTER = 0
WORKTREE_ONLY = 1
INDEX_ONLY = 2


class _AddIFile:
    __slots__ = ("name", "prefix_length",
                 "idx_seen", "idx_add", "idx_del", "idx_binary",
                 "wt_seen", "wt_add", "wt_del", "wt_binary")

    def __init__(self, name):
        self.name = name
        self.prefix_length = 0
        self.idx_seen = False
        self.idx_add = 0
        self.idx_del = 0
        self.idx_binary = False
        self.wt_seen = False
        self.wt_add = 0
        self.wt_del = 0
        self.wt_binary = False


def _is_valid_prefix(prefix: str, prefix_len: int) -> bool:
    if not prefix_len or not prefix:
        return False
    # separators
    for ch in " \t\r\n,":
        pos = prefix.find(ch)
        if 0 <= pos < prefix_len:
            return False
    if prefix[0] == "-":
        return False
    if prefix[0].isdigit():
        return False
    if prefix_len == 1 and prefix[0] in ("*", "?"):
        return False
    return True


def _extend_prefix_length(item, other_string: str, max_length: int) -> None:
    cur = item.prefix_length
    if not cur or item.name[:cur] != other_string[:cur]:
        return
    while True:
        c = item.name[cur] if cur < len(item.name) else ""
        cur += 1
        if not c or cur > max_length or ord(c) > 127:
            item.prefix_length = 0
            return
        oc = other_string[cur - 1] if cur - 1 < len(other_string) else ""
        if c != oc:
            item.prefix_length = cur
            return
        item.prefix_length = cur


def _find_unique_prefixes(items: list, min_length: int = 1, max_length: int = 4) -> None:
    """Port of find_unique_prefixes(): set each item.prefix_length."""
    order = sorted(range(len(items)), key=lambda i: items[i].name)
    sorted_items = [items[i] for i in order]
    for i, item in enumerate(sorted_items):
        item.prefix_length = 0
        ln = 0
        while ln < min_length:
            c = item.name[ln] if ln < len(item.name) else ""
            ln += 1
            if not c or ord(c) > 127:
                ln = 0
                break
        item.prefix_length = ln
        if i > 0:
            _extend_prefix_length(item, sorted_items[i - 1].name, max_length)
        if i + 1 < len(sorted_items):
            _extend_prefix_length(item, sorted_items[i + 1].name, max_length)


def _find_unique(string: str, items: list) -> int:
    """Port of find_unique(): resolve a string to a single item index (or -1)."""
    names = [it.name for it in items]
    order = sorted(range(len(items)), key=lambda i: items[i].name)
    sorted_names = [names[i] for i in order]
    # binary search insert index
    import bisect
    idx = bisect.bisect_left(sorted_names, string)
    exact = idx < len(sorted_names) and sorted_names[idx] == string
    if exact:
        return order[idx]
    if idx > 0 and sorted_names[idx - 1].startswith(string):
        return -1
    if idx + 1 < len(sorted_names) and sorted_names[idx + 1].startswith(string):
        return -1
    if idx < len(sorted_names) and sorted_names[idx].startswith(string):
        return order[idx]
    return -1


def _highlight(s, item, color, reset) -> str:
    if item.prefix_length > 0 and _is_valid_prefix(item.name, item.prefix_length):
        if color:
            return "%s%s%s%s" % (color, item.name[:item.prefix_length], reset,
                                 item.name[item.prefix_length:])
        return "[%s]%s" % (item.name[:item.prefix_length],
                           item.name[item.prefix_length:])
    return item.name


def _render_adddel_idx(item) -> str:
    if item.idx_binary:
        return "binary"
    if item.idx_seen:
        return "+%d/-%d" % (item.idx_add, item.idx_del)
    return "unchanged"


def _render_adddel_wt(item) -> str:
    if item.wt_binary:
        return "binary"
    if item.wt_seen:
        return "+%d/-%d" % (item.wt_add, item.wt_del)
    return "nothing"


def _count_addgel(repo, a_mode, a_sha, b_mode, b_sha):
    """Return (add, del, is_binary) for the change a -> b (None side = absent)."""
    from . import objects as objs
    from . import diff as diff_mod

    def blob(sha):
        if sha is None:
            return b""
        try:
            return objs.read_object(repo, sha)[1]
        except KeyError:
            return b""

    da = blob(a_sha)
    db = blob(b_sha)
    if b"\0" in da or b"\0" in db:
        return 0, 0, True
    al = da.decode("utf-8", "replace").splitlines()
    bl = db.decode("utf-8", "replace").splitlines()
    add = dele = 0
    for op in diff_mod.diff_lines(al, bl):
        if op[0] == "ins":
            add += 1
        elif op[0] == "del":
            dele += 1
    return add, dele, False


def _count_worktree(repo, a_mode, a_sha, path):
    """Counts for index/tree blob a -> current worktree content of path."""
    from . import objects as objs
    from . import diff as diff_mod
    full = repo.path / path
    if not (full.exists() or full.is_symlink()):
        # deletion
        da = b""
        if a_sha is not None:
            try:
                da = objs.read_object(repo, a_sha)[1]
            except KeyError:
                da = b""
        n = len(da.decode("utf-8", "replace").splitlines())
        if b"\0" in da:
            return 0, 0, True
        return 0, n, False
    import os as _os
    if full.is_symlink():
        db = _os.readlink(full).encode("utf-8")
    else:
        db = full.read_bytes()
    da = b""
    if a_sha is not None:
        try:
            da = objs.read_object(repo, a_sha)[1]
        except KeyError:
            da = b""
    if b"\0" in da or b"\0" in db:
        return 0, 0, True
    al = da.decode("utf-8", "replace").splitlines()
    bl = db.decode("utf-8", "replace").splitlines()
    add = dele = 0
    for op in diff_mod.diff_lines(al, bl):
        if op[0] == "ins":
            add += 1
        elif op[0] == "del":
            dele += 1
    return add, dele, False


def _get_modified_files(repo, mode_filter, pathspec):
    """Port of get_modified_files(): return a sorted list of _AddIFile.

    Computes both the index (HEAD vs index) and worktree (index vs worktree)
    add/del stats.  The filter selects which appear:
      NO_FILTER      - all changed paths
      WORKTREE_ONLY  - paths with worktree changes
      INDEX_ONLY     - paths with index changes
    """
    from . import objects as objs
    from . import refs as refs_mod
    from . import workdir
    from .index import read_index

    head_sha = refs_mod.rev_parse(repo, "HEAD")
    head_tree = None
    if head_sha:
        t, data = objs.read_object(repo, head_sha)
        head_tree = objs.parse_commit(data).tree if t == "commit" else head_sha
    head_map = {}
    if head_tree:
        for p, m, sh in workdir.iter_tree_files(repo, head_tree):
            head_map[p] = (m, sh)
    idx = read_index(repo).by_path()

    files = {}
    order_seen = []

    def get(name):
        if name not in files:
            f = _AddIFile(name)
            files[name] = f
            order_seen.append(name)
        return files[name]

    def ps_match(name):
        if not pathspec:
            return True
        for w in pathspec:
            ww = w.rstrip("/")
            if name == ww or name.startswith(ww + "/"):
                return True
        return False

    # index changes: HEAD vs index
    index_changed = []
    for p in sorted(set(head_map) | set(idx)):
        if not ps_match(p):
            continue
        a = head_map.get(p)
        b = (idx[p].mode_str(), idx[p].sha) if p in idx else None
        if a == b:
            continue
        if a and b and a[0] == b[0] and a[1] == b[1]:
            continue
        add, dele, biny = _count_addgel(repo, a[0] if a else None, a[1] if a else None,
                                        b[0] if b else None, b[1] if b else None)
        index_changed.append((p, add, dele, biny))

    # worktree changes: index vs worktree
    wt_changed = []
    for p in sorted(idx):
        if not ps_match(p):
            continue
        e = idx[p]
        add, dele, biny = _count_worktree(repo, e.mode_str(), e.sha, p)
        full = repo.path / p
        if not (full.exists() or full.is_symlink()):
            wt_changed.append((p, add, dele, biny))
        else:
            import os as _os
            if full.is_symlink():
                db = _os.readlink(full).encode("utf-8")
            else:
                db = full.read_bytes()
            wsha, _ = objs.hash_bytes("blob", db, repo)
            if wsha != e.sha:
                wt_changed.append((p, add, dele, biny))

    # populate; skip_unseen = filter && i (the second pass only skips for the
    # filtered modes; NO_FILTER never skips).  INDEX_ONLY iterates index first.
    if mode_filter == INDEX_ONLY:
        passes = [("idx", index_changed, False),
                  ("wt", wt_changed, mode_filter != NO_FILTER)]
    else:
        passes = [("wt", wt_changed, False),
                  ("idx", index_changed, mode_filter != NO_FILTER)]

    for kind, changed, skip_unseen in passes:
        for (p, add, dele, biny) in changed:
            if skip_unseen and p not in files:
                continue
            f = get(p)
            if kind == "idx":
                f.idx_seen = True
                f.idx_add = add
                f.idx_del = dele
                f.idx_binary = biny
            else:
                f.wt_seen = True
                f.wt_add = add
                f.wt_del = dele
                f.wt_binary = biny

    result = [files[n] for n in files]
    result.sort(key=lambda f: f.name)
    return result


class AddIState:
    def __init__(self, repo, context=-1, interhunkcontext=-1, auto_advance=True):
        self.repo = repo
        self.context = context
        self.interhunkcontext = interhunkcontext
        self.auto_advance = auto_advance
        self.stdin = sys.stdin
        self.stdout = sys.stdout


_COMMAND_LIST = ["status", "update", "revert", "add untracked",
                 "patch", "diff", "quit", "help"]


def _ai_readline(s):
    line = s.stdin.readline()
    if line == "":
        return None
    if line.endswith("\n"):
        line = line[:-1]
    if line.endswith("\r"):
        line = line[:-1]
    return line


def _list_files(s, items, selected, header, columns, only_names):
    if not items:
        return
    if header:
        s.stdout.write(header + "\n")
    last_lf = True
    for i, item in enumerate(items):
        sel = "*" if (selected and selected[i]) else " "
        hl = _highlight(item.name, item, None, None)
        if only_names:
            s.stdout.write("%c%2d: %s" % (sel, i + 1, hl))
        else:
            idx_s = _render_adddel_idx(item)
            wt_s = _render_adddel_wt(item)
            s.stdout.write("%c%2d: %12s %12s %s" % (sel, i + 1, idx_s, wt_s, hl))
        if columns and ((i + 1) % columns):
            s.stdout.write("\t")
            last_lf = False
        else:
            s.stdout.write("\n")
            last_lf = True
    if not last_lf:
        s.stdout.write("\n")


def _list_commands(s, items, header, columns):
    if header:
        s.stdout.write(header + "\n")
    last_lf = True
    for i, item in enumerate(items):
        if not item.prefix_length or not _is_valid_prefix(item.name, item.prefix_length):
            s.stdout.write(" %2d: %s" % (i + 1, item.name))
        else:
            s.stdout.write(" %2d: [%s]%s" % (i + 1, item.name[:item.prefix_length],
                                             item.name[item.prefix_length:]))
        if columns and ((i + 1) % columns):
            s.stdout.write("\t")
            last_lf = False
        else:
            s.stdout.write("\n")
            last_lf = True
    if not last_lf:
        s.stdout.write("\n")


_FILE_PROMPT_HELP = (
    "Prompt help:\n"
    "1          - select a single item\n"
    "3-5        - select a range of items\n"
    "2-3,6-9    - select multiple ranges\n"
    "foo        - select item based on unique prefix\n"
    "-...       - unselect specified items\n"
    "*          - choose all items\n"
    "           - (empty) finish selecting\n"
)

_CMD_PROMPT_HELP = (
    "Prompt help:\n"
    "1          - select a numbered item\n"
    "foo        - select item based on unique prefix\n"
    "           - (empty) select nothing\n"
)


def _list_and_choose(s, items, prompt, *, singleton, immediate,
                     print_help, columns, header, only_names, is_command):
    selected = None if singleton else [False] * len(items)
    res = -1 if singleton else 0
    _find_unique_prefixes(items)
    while True:
        if is_command:
            _list_commands(s, items, header, columns)
        else:
            _list_files(s, items, selected, header, columns, only_names)
        s.stdout.write(prompt)
        s.stdout.write("> " if singleton else ">> ")
        s.stdout.flush()
        line = _ai_readline(s)
        if line is None:
            s.stdout.write("\n")
            if immediate:
                res = -2  # LIST_AND_CHOOSE_QUIT
            break
        if line == "":
            break
        if line == "?":
            s.stdout.write(print_help)
            continue
        p = line
        pos = 0
        L = len(p)
        broke = False
        while pos < L or pos == 0:
            # find separator span
            start = pos
            while pos < L and p[pos] not in " \t\r\n,":
                pos += 1
            sep = pos - start
            tok = p[start:pos]
            # skip separators
            while pos < L and p[pos] in " \t\r\n,":
                pos += 1
            if sep == 0:
                if pos >= L:
                    break
                continue
            choose = 1
            t = tok
            if t[0] == "-":
                choose = 0
                t = t[1:]
            frm = -1
            to = -1
            if t == "*":
                frm = 0
                to = len(items)
            elif t and t[0].isdigit():
                m = re.match(r"(\d+)", t)
                num = int(m.group(1))
                rest = t[m.end():]
                frm = num - 1
                if rest == "":
                    to = frm + 1
                elif rest[0] == "-":
                    rest2 = rest[1:]
                    if rest2 and rest2[0].isdigit():
                        m2 = re.match(r"(\d+)", rest2)
                        to = int(m2.group(1))
                        if rest2[m2.end():] != "":
                            frm = -1
                    else:
                        to = len(items)
                        if rest2 != "":
                            frm = -1
                else:
                    frm = -1
            if frm < 0:
                frm = _find_unique(t, items)
                if frm >= 0:
                    to = frm + 1
            if frm < 0 or frm >= len(items) or (singleton and frm + 1 != to):
                s.stdout.write("")  # error goes to stderr
                sys.stderr.write("Huh (%s)?\n" % t)
                broke = True
                break
            if singleton:
                res = frm
                broke = True
                break
            if to > len(items):
                to = len(items)
            while frm < to:
                if selected[frm] != choose:
                    selected[frm] = choose
                    res += 1 if choose else -1
                frm += 1
            if pos >= L:
                break
        if (immediate and res != -1) or line == "*":
            break
    return res, selected


def _run_status(s, files_box, pathspec):
    files = _get_modified_files(s.repo, NO_FILTER, pathspec)
    files_box[0] = files
    # run_status() lists without find_unique_prefixes(), so no prefix highlight.
    _list_files(s, files, None, _ai_header(), 0, False)
    s.stdout.write("\n")
    return 0


def _ai_header():
    return "     %12s %12s %s" % ("staged", "unstaged", "path")


def _run_update(s, files_box, pathspec):
    from .index import read_index, write_index, IndexEntry, stat_to_entry
    files = _get_modified_files(s.repo, WORKTREE_ONLY, pathspec)
    files_box[0] = files
    if not files:
        s.stdout.write("\n")
        return 0
    count, selected = _list_and_choose(
        s, files, "Update", singleton=False, immediate=False,
        print_help=_FILE_PROMPT_HELP, columns=0, header=_ai_header(),
        only_names=False, is_command=False)
    if count <= 0:
        s.stdout.write("\n")
        return 0
    idx = read_index(s.repo)
    bp = idx.by_path()
    import os as _os
    for i, f in enumerate(files):
        if not selected[i]:
            continue
        full = s.repo.path / f.name
        if not (full.exists() or full.is_symlink()):
            idx.remove(f.name)
        else:
            from . import workdir as _wd
            data = _wd._blob_data(full)
            from . import objects as objs
            sha = objs.write_object(s.repo, "blob", data)
            st = _os.lstat(full)
            idx.upsert(stat_to_entry(f.name, st, sha, _wd._mode_for(full)))
    write_index(s.repo, idx)
    s.stdout.write("updated %d path%s\n" % (count, "" if count == 1 else "s"))
    s.stdout.write("\n")
    return 0


def _run_revert(s, files_box, pathspec):
    from .index import read_index, write_index, IndexEntry, REG_MODE
    from . import objects as objs
    from . import refs as refs_mod
    from . import workdir
    files = _get_modified_files(s.repo, INDEX_ONLY, pathspec)
    files_box[0] = files
    if not files:
        s.stdout.write("\n")
        return 0
    count, selected = _list_and_choose(
        s, files, "Revert", singleton=False, immediate=False,
        print_help=_FILE_PROMPT_HELP, columns=0, header=_ai_header(),
        only_names=False, is_command=False)
    if count <= 0:
        s.stdout.write("\n")
        return 0
    head_sha = refs_mod.rev_parse(s.repo, "HEAD")
    head_map = {}
    if head_sha:
        t, data = objs.read_object(s.repo, head_sha)
        head_tree = objs.parse_commit(data).tree if t == "commit" else head_sha
        for p, m, sh in workdir.iter_tree_files(s.repo, head_tree):
            head_map[p] = (m, sh)
    idx = read_index(s.repo)
    for i, f in enumerate(files):
        if not selected[i]:
            continue
        if f.name in head_map:
            m, sh = head_map[f.name]
            idx.upsert(IndexEntry(mode=int(m, 8), sha=sh, path=f.name))
        else:
            idx.remove(f.name)
    write_index(s.repo, idx)
    s.stdout.write("reverted %d path%s\n" % (count, "" if count == 1 else "s"))
    s.stdout.write("\n")
    return 0


def _run_add_untracked(s, files_box, pathspec):
    from .index import read_index, write_index, stat_to_entry
    from . import workdir
    from . import objects as objs
    import os as _os
    # untracked files
    idx = read_index(s.repo)
    tracked = set(idx.by_path())
    names = []
    for rel in workdir.iter_worktree(s.repo):
        if rel in tracked:
            continue
        if pathspec:
            if not any(rel == w.rstrip("/") or rel.startswith(w.rstrip("/") + "/")
                       for w in pathspec):
                continue
        if workdir._ignored(rel):
            continue
        names.append(rel)
    names.sort()
    files = [_AddIFile(n) for n in names]
    files_box[0] = files
    if not files:
        s.stdout.write("No untracked files.\n")
        s.stdout.write("\n")
        return 0
    count, selected = _list_and_choose(
        s, files, "Add untracked", singleton=False, immediate=False,
        print_help=_FILE_PROMPT_HELP, columns=0, header=_ai_header(),
        only_names=True, is_command=False)
    if count <= 0:
        s.stdout.write("\n")
        return 0
    idx = read_index(s.repo)
    for i, f in enumerate(files):
        if not selected[i]:
            continue
        full = s.repo.path / f.name
        data = workdir._blob_data(full)
        sha = objs.write_object(s.repo, "blob", data)
        st = _os.lstat(full)
        idx.upsert(stat_to_entry(f.name, st, sha, workdir._mode_for(full)))
    write_index(s.repo, idx)
    s.stdout.write("added %d path%s\n" % (count, "" if count == 1 else "s"))
    s.stdout.write("\n")
    return 0


def _run_patch(s, files_box, pathspec):
    files = _get_modified_files(s.repo, WORKTREE_ONLY, pathspec)
    binary_count = sum(1 for f in files if f.idx_binary or f.wt_binary)
    # drop binary / unmerged (run_patch's filtering block)
    kept = [f for f in files if not (f.idx_binary or f.wt_binary)]
    files = kept
    files_box[0] = files
    if not files:
        if binary_count:
            sys.stderr.write("Only binary files changed.\n")
        else:
            sys.stderr.write("No changes.\n")
        return 0
    count, selected = _list_and_choose(
        s, files, "Patch update", singleton=False, immediate=False,
        print_help=_FILE_PROMPT_HELP, columns=0, header=_ai_header(),
        only_names=False, is_command=False)
    if count > 0:
        sel_paths = [files[i].name for i in range(len(files)) if selected[i]]
        rc = run_add_p(s.repo, "add", None, sel_paths,
                       context=s.context, interhunkcontext=s.interhunkcontext,
                       auto_advance=s.auto_advance,
                       stdin=s.stdin, stdout=s.stdout)
        return rc
    return 0


def _run_diff(s, files_box, pathspec):
    from . import refs as refs_mod
    files = _get_modified_files(s.repo, INDEX_ONLY, pathspec)
    files_box[0] = files
    if not files:
        s.stdout.write("\n")
        return 0
    count, selected = _list_and_choose(
        s, files, "Review diff", singleton=False, immediate=True,
        print_help=_FILE_PROMPT_HELP, columns=0, header=_ai_header(),
        only_names=False, is_command=False)
    if count > 0:
        from . import cli
        sel_paths = [files[i].name for i in range(len(files)) if selected[i]]
        head_sha = refs_mod.rev_parse(s.repo, "HEAD")
        args = ["-p", "--cached"]
        if s.context != -1:
            args.append("--unified=%d" % s.context)
        if s.interhunkcontext != -1:
            args.append("--inter-hunk-context=%d" % s.interhunkcontext)
        rev = head_sha if head_sha else "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
        args += [rev, "--"] + sel_paths
        out = _run_capture(cli.cmd_diff, args)
        s.stdout.write(out)
    s.stdout.write("\n")
    return 0


def _run_help(s, files_box, pathspec):
    s.stdout.write("status        - show paths with changes\n")
    s.stdout.write("update        - add working tree state to the staged set of changes\n")
    s.stdout.write("revert        - revert staged set of changes back to the HEAD version\n")
    s.stdout.write("patch         - pick hunks and update selectively\n")
    s.stdout.write("diff          - view diff between HEAD and index\n")
    s.stdout.write("add untracked - add contents of untracked files to the staged set of changes\n")
    return 0


_AI_COMMANDS = {
    "status": _run_status,
    "update": _run_update,
    "revert": _run_revert,
    "add untracked": _run_add_untracked,
    "patch": _run_patch,
    "diff": _run_diff,
    "quit": None,
    "help": _run_help,
}


def run_add_i(repo, pathspec, *, context=-1, interhunkcontext=-1,
              auto_advance=True, stdin=None, stdout=None) -> int:
    s = AddIState(repo, context, interhunkcontext, auto_advance)
    if stdin is not None:
        s.stdin = stdin
    if stdout is not None:
        s.stdout = stdout

    commands = [_AddIFile(name) for name in _COMMAND_LIST]

    files_box = [[]]
    _run_status(s, files_box, pathspec)

    while True:
        idx, _sel = _list_and_choose(
            s, commands, "What now", singleton=True, immediate=True,
            print_help=_CMD_PROMPT_HELP, columns=4, header="*** Commands ***",
            only_names=False, is_command=True)
        if idx == -2:  # QUIT (EOF)
            s.stdout.write("Bye.\n")
            return 0
        if idx < 0 or idx >= len(commands):
            cmd = None
        else:
            cmd = _AI_COMMANDS[commands[idx].name]
        if cmd is None:
            s.stdout.write("Bye.\n")
            return 0
        cmd(s, files_box, pathspec)
