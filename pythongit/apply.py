"""A faithful port of git's ``apply.c`` core machinery.

This implements the parts of ``git apply`` that the CLI wrapper drives:

  * git-style and traditional unified-diff parsing with ``-p<n>`` path strip;
  * the line-image preimage/postimage matcher, including positional offset
    search and ``-C<n>`` context fuzz (reducing leading/trailing context);
  * ``--no-add`` (skip added lines), ``-R`` reverse, no-newline handling;
  * the full ``--3way`` fallback (reconstruct the preimage blob, build the
    postimage by applying to it, three-way merge against the current contents
    using base==preimage, with ``--ours``/``--theirs``/``--union`` favor) and
    conflict-marker output plus index stage 1/2/3 writing;
  * ``-N`` intent-to-add and ``--index``/``--cached`` index updates.

The logic mirrors apply.c line for line where it matters (find_pos,
match_fragment, apply_one_fragment, try_threeway, three_way_merge,
write_out_results) so the observable behaviour is byte-identical to Git 2.54.0.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from . import objects as objs
from . import refs as refs_mod
from . import xdiff

UINT_MAX = 0xFFFFFFFF
EMPTY_BLOB = "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"

LINE_COMMON = 1
LINE_PATCHED = 2


# ---------------------------------------------------------------------------
# data structures (mirror struct fragment / struct patch)


@dataclass
class Fragment:
    oldpos: int = 0
    oldlines: int = 0
    newpos: int = 0
    newlines: int = 0
    leading: int = 0
    trailing: int = 0
    # The raw body lines of the hunk (each retains its leading ' '/'+'/'-'/'\\'
    # marker and a trailing '\n' if present in the patch), as bytes.
    body: list[bytes] = field(default_factory=list)
    rejected: bool = False


@dataclass
class Patch:
    old_name: Optional[str] = None
    new_name: Optional[str] = None
    def_name: Optional[str] = None
    old_mode: int = 0
    new_mode: int = 0
    is_new: int = 0          # tri-state like C: -1 unknown, 0 no, 1 yes
    is_delete: int = 0       # -1 unknown, 0 no, 1 yes
    is_rename: bool = False
    is_copy: bool = False
    is_binary: bool = False
    old_oid_prefix: str = ""
    new_oid_prefix: str = ""
    lines_added: int = 0
    lines_deleted: int = 0
    fragments: list[Fragment] = field(default_factory=list)
    # results filled in during application
    result: Optional[bytes] = None
    conflicted_threeway: bool = False
    direct_to_threeway: bool = False
    threeway_stage: list[Optional[str]] = field(default_factory=lambda: [None, None, None])


class ApplyError(Exception):
    """Raised to abort application with a git-style fatal/usage situation."""

    def __init__(self, message: str, rc: int = 1):
        super().__init__(message)
        self.message = message
        self.rc = rc


# ---------------------------------------------------------------------------
# image model (mirror struct image: a buffer plus a per-line table)


class Image:
    __slots__ = ("lines", "flags")

    def __init__(self, buf: bytes = b""):
        # lines: list[bytes] each ending with '\n' except possibly the last
        self.lines: list[bytes] = _split_lines(buf)
        self.flags: list[int] = [0] * len(self.lines)

    @property
    def line_nr(self) -> int:
        return len(self.lines)

    @property
    def buf(self) -> bytes:
        return b"".join(self.lines)

    def remove_first_line(self) -> None:
        self.lines.pop(0)
        self.flags.pop(0)

    def remove_last_line(self) -> None:
        self.lines.pop()
        self.flags.pop()


def _split_lines(buf: bytes) -> list[bytes]:
    """Split into lines, keeping the terminating '\n' on each (mirrors
    prepare_image: every line is terminated except possibly the last)."""
    out: list[bytes] = []
    i = 0
    n = len(buf)
    while i < n:
        j = buf.find(b"\n", i)
        if j < 0:
            out.append(buf[i:])
            break
        out.append(buf[i : j + 1])
        i = j + 1
    return out


# ---------------------------------------------------------------------------
# path-name extraction with p_value (skip_tree_prefix / git_header_name)


def _skip_tree_prefix(p_value: int, line: str) -> Optional[str]:
    if p_value == 0:
        return None if (line and line[0] == "/") else line
    nslash = p_value
    for i, ch in enumerate(line):
        if ch == "/":
            nslash -= 1
            if nslash <= 0:
                return None if i == 0 else line[i + 1 :]
    return None


def _squash_slash(name: Optional[str]) -> Optional[str]:
    """Collapse runs of '/' (mirror squash_slash)."""
    if name is None:
        return None
    out = []
    prev_slash = False
    for ch in name:
        if ch == "/":
            if prev_slash:
                continue
            prev_slash = True
        else:
            prev_slash = False
        out.append(ch)
    return "".join(out)


def _git_header_name(p_value: int, rest: str) -> Optional[str]:
    """Extract the common name from the text after 'diff --git ' (rest), the
    way git_header_name() does for the unquoted, non-rename case."""
    line = rest
    if line.startswith('"'):
        # quoted first name
        first, second_off = _unquote_c(line)
        if first is None:
            return None
        cp = _skip_tree_prefix(p_value, first)
        if cp is None:
            return None
        first = cp
        second = line[second_off:].lstrip()
        if not second:
            return None
        if second.startswith('"'):
            sp, _ = _unquote_c(second)
            if sp is None:
                return None
            cp2 = _skip_tree_prefix(p_value, sp)
            if cp2 is None or cp2 != first:
                return None
            return first
        cp2 = _skip_tree_prefix(p_value, second)
        if cp2 is None or cp2 != first:
            return None
        return first

    name = _skip_tree_prefix(p_value, line)
    if name is None:
        return None
    # Look for a quote starting the second name.
    qpos = name.find('"')
    if qpos != -1:
        sp, _ = _unquote_c(name[qpos:])
        if sp is None:
            return None
        np = _skip_tree_prefix(p_value, sp)
        if np is None:
            return None
        ln = len(np)
        if ln < qpos and name[:ln] == np and (qpos > ln and name[ln] in " \t"):
            return np
        return None
    # Accept a name only if it shows up twice, exactly the same, separated by
    # a single SP/TAB.
    nl = name.find("\n")
    if nl == -1:
        return None
    line_part = name[:nl]
    for ln in range(len(line_part)):
        ch = line_part[ln]
        if ch in " \t":
            if ln + 1 >= len(line_part):
                return None
            second = _skip_tree_prefix(p_value, line_part[ln + 1 :])
            if second is None:
                continue
            if len(second) == ln and second == line_part[:ln]:
                return line_part[:ln]
    return None


def _unquote_c(s: str) -> tuple[Optional[str], int]:
    """Decode a C-quoted ("...") path. Returns (decoded, index just past the
    closing quote in s) or (None, 0) on error."""
    if not s or s[0] != '"':
        return None, 0
    out = []
    i = 1
    n = len(s)
    while i < n:
        ch = s[i]
        if ch == '"':
            return "".join(out), i + 1
        if ch == "\\":
            i += 1
            if i >= n:
                return None, 0
            e = s[i]
            simple = {"a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r",
                      "t": "\t", "v": "\v", "\\": "\\", '"': '"'}
            if e in simple:
                out.append(simple[e])
                i += 1
            elif e in "0123456789":
                oct_digits = e
                i += 1
                while i < n and len(oct_digits) < 3 and s[i] in "01234567":
                    oct_digits += s[i]
                    i += 1
                out.append(chr(int(oct_digits, 8)))
            else:
                out.append(e)
                i += 1
        else:
            out.append(ch)
            i += 1
    return None, 0


def _diff_timestamp_len(line: str) -> int:
    """Mirror diff_timestamp_len: detect a trailing ISO-ish timestamp preceded
    by a TAB.  We only handle the common 'name\\t<date>' form produced by
    traditional diffs; returns the length of the trailing date portion (incl.
    the preceding whitespace) or 0."""
    # Traditional diffs from GNU diff put a TAB then a date.  git's apply only
    # uses this to trim; absent a TAB there is no date.
    tab = line.rfind("\t")
    if tab < 0:
        return 0
    return len(line) - tab


def _find_name_common(line: str, default: Optional[str], p_value: int,
                      end: Optional[int], terminate: str) -> Optional[str]:
    """Mirror find_name_common(): walk from the start, count p_value slashes,
    honoring a terminator set (space/tab/newline) when end is None."""
    start = None
    if p_value == 0:
        start = 0
    i = 0
    n = len(line) if end is None else end
    pv = p_value
    while i < (len(line) if end is None else end):
        c = line[i]
        if end is None and c.isspace() and c in " \t\n":
            if c == "\n":
                break
            if _name_terminate(c, terminate):
                break
        i += 1
        if c == "/":
            pv -= 1
            if pv == 0:
                start = i
    if start is None:
        return _squash_slash(default)
    seg = line[start:i]
    if not seg:
        return _squash_slash(default)
    if default is not None:
        if len(default) < len(seg) and seg.startswith(default):
            return _squash_slash(default)
    return _squash_slash(seg)


TERM_SPACE = 1
TERM_TAB = 2


def _name_terminate(c: str, terminate: int) -> bool:
    if c == " " and not (terminate & TERM_SPACE):
        return False
    if c == "\t" and not (terminate & TERM_TAB):
        return False
    return True


def _find_name(line: str, default: Optional[str], p_value: int,
               terminate: int) -> Optional[str]:
    if line.startswith('"'):
        name = _git_name_gnu(line, p_value)
        if name is not None:
            return name
    return _find_name_common(line, default, p_value, None, terminate)


def _git_name_gnu(line: str, p_value: int) -> Optional[str]:
    s, _ = _unquote_c(line)
    if s is None:
        return None
    cp = _skip_tree_prefix(p_value, s)
    if cp is None:
        return None
    return _squash_slash(cp)


def _find_name_traditional(line: str, default: Optional[str],
                           p_value: int) -> Optional[str]:
    if line.startswith('"'):
        name = _git_name_gnu(line, p_value)
        if name is not None:
            return name
    nl = line.find("\n")
    seg = line if nl < 0 else line[:nl]
    date_len = _diff_timestamp_len(seg)
    if not date_len:
        return _find_name_common(line, default, p_value, None, TERM_TAB)
    return _find_name_common(line, default, p_value, len(seg) - date_len, 0)


def _is_dev_null(name: str) -> bool:
    # mirror is_dev_null: "/dev/null" possibly followed by tab/space/eol
    if not name.startswith("/dev/null"):
        return False
    rest = name[len("/dev/null"):]
    return rest == "" or rest[0] in " \t\n"


def _guess_p_value(nameline: str, prefix: Optional[str]) -> int:
    if _is_dev_null(nameline):
        return -1
    name = _find_name_traditional(nameline, None, 0)
    if name is None:
        return -1
    cp = name.find("/")
    val = -1
    if cp == -1:
        val = 0
    elif prefix:
        # not supported (no prefix in our hermetic usage); fall through
        val = 0 if "/" not in name else 1
    else:
        val = 1
    return val


# ---------------------------------------------------------------------------
# patch parsing


def parse_patches(text: bytes, p_value_opt: Optional[int],
                  prefix: Optional[str] = None,
                  patch_input_file: Optional[str] = None) -> list[Patch]:
    """Parse all patches from the input.  p_value_opt is the user's -p<n> or
    None (meaning the default of 1, with traditional-diff guessing).

    Raises ApplyError(rc=128) on a git-diff header whose filename cannot be
    resolved, exactly like parse_git_diff_header()."""
    raw = text.decode("utf-8", "surrogateescape")
    lines = raw.splitlines(keepends=True)
    patches: list[Patch] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if line.startswith("diff --git "):
            patch, i = _parse_git_patch(lines, i, p_value_opt, patch_input_file)
            if patch is not None:
                patches.append(patch)
            continue
        if line.startswith("--- ") or line.startswith("+++ "):
            patch, ni = _parse_traditional_patch(lines, i, p_value_opt, prefix)
            if patch is not None:
                patches.append(patch)
                i = ni
                continue
        i += 1
    return patches


def reverse_patches(patches: list[Patch]) -> None:
    """Mirror reverse_patches(): swap the pre/post identities of every patch so
    a forward apply of the swapped patch == reverse apply of the original.

    The hunk body lines keep their '+'/'-' markers; the per-line inversion is
    done later in _build_pre_post (apply_in_reverse), so here we only swap
    metadata and the fragment position/count fields."""
    for p in patches:
        p.old_name, p.new_name = p.new_name, p.old_name
        if p.new_mode or p.is_delete:
            p.old_mode, p.new_mode = p.new_mode, p.old_mode
        p.is_new, p.is_delete = p.is_delete, p.is_new
        p.lines_added, p.lines_deleted = p.lines_deleted, p.lines_added
        p.old_oid_prefix, p.new_oid_prefix = p.new_oid_prefix, p.old_oid_prefix
        for frag in p.fragments:
            # leading/trailing are context-only counts and stay valid under
            # reversal; only positions/counts swap (as in reverse_patches()).
            frag.oldpos, frag.newpos = frag.newpos, frag.oldpos
            frag.oldlines, frag.newlines = frag.newlines, frag.oldlines


def _hdr_missing_filename(p_value: int, has_def: bool, linenr: int,
                          patch_input_file: Optional[str]) -> None:
    n = p_value
    unit = "component" if n == 1 else "components"
    if not has_def:
        if patch_input_file:
            msg = ("git diff header lacks filename information when removing "
                   "%d leading pathname %s at %s:%d" % (n, unit, patch_input_file, linenr))
        else:
            msg = ("git diff header lacks filename information when removing "
                   "%d leading pathname %s (line %d)" % (n, unit, linenr))
    else:
        if patch_input_file:
            msg = "git diff header lacks filename information at %s:%d" % (patch_input_file, linenr)
        else:
            msg = "git diff header lacks filename information (line %d)" % linenr
    raise ApplyError("error: " + msg, rc=128)


def _strip_eol(s: str) -> str:
    return s.rstrip("\n")


def _parse_git_patch(lines: list[str], i: int, p_value_opt: Optional[int],
                     patch_input_file: Optional[str] = None
                     ) -> tuple[Optional[Patch], int]:
    p_value = 1 if p_value_opt is None else p_value_opt
    header = lines[i]
    rest = header[len("diff --git "):]
    patch = Patch(is_new=0, is_delete=0)
    patch.def_name = _git_header_name(p_value, rest)
    # linenr counts patch-file lines 1-based; the header line we just consumed is
    # lines[i], so the next line is at 1-based i+2.
    linenr = i + 2
    i += 1
    n = len(lines)
    # mirror parse_git_diff_header: walk extended headers until "@@ -" (hdrend).
    while i < n:
        line = lines[i]
        body = _strip_eol(line)
        if line.startswith("@@ -"):
            break  # gitdiff_hdrend
        if body.startswith("diff --git "):
            break
        if body.startswith("old mode "):
            patch.old_mode = int(body[len("old mode "):].strip() or "0", 8)
        elif body.startswith("new mode "):
            patch.new_mode = int(body[len("new mode "):].strip() or "0", 8)
        elif body.startswith("deleted file mode "):
            patch.is_delete = 1
            patch.old_mode = int(body[len("deleted file mode "):].strip() or "0", 8)
            patch.old_name = patch.def_name
        elif body.startswith("new file mode "):
            patch.is_new = 1
            patch.new_mode = int(body[len("new file mode "):].strip() or "0", 8)
            patch.new_name = patch.def_name
        elif body.startswith("copy from "):
            patch.is_copy = True
            patch.old_name = _find_name(body[len("copy from "):], None,
                                        p_value - 1 if p_value else 0, 0)
        elif body.startswith("copy to "):
            patch.is_copy = True
            patch.new_name = _find_name(body[len("copy to "):], None,
                                        p_value - 1 if p_value else 0, 0)
        elif body.startswith("rename from ") or body.startswith("rename old "):
            patch.is_rename = True
            patch.old_name = _find_name(body[len("rename from "):], None,
                                        p_value - 1 if p_value else 0, 0)
        elif body.startswith("rename to ") or body.startswith("rename new "):
            patch.is_rename = True
            patch.new_name = _find_name(body[len("rename to "):], None,
                                        p_value - 1 if p_value else 0, 0)
        elif body.startswith("index "):
            spec = body[len("index "):]
            modepart = ""
            if " " in spec:
                spec, modepart = spec.split(" ", 1)
            if ".." in spec:
                patch.old_oid_prefix, patch.new_oid_prefix = spec.split("..", 1)
            if modepart.strip():
                m = int(modepart.strip(), 8)
                if not patch.old_mode:
                    patch.old_mode = m
                if not patch.new_mode:
                    patch.new_mode = m
        elif body.startswith("--- "):
            # gitdiff_oldname: only set when not yet set and patch is not is_new.
            if patch.old_name is None and not patch.is_new and not _is_dev_null(body[4:]):
                patch.old_name = _find_name(body[4:], None, p_value, TERM_TAB)
        elif body.startswith("+++ "):
            if patch.new_name is None and not patch.is_delete and not _is_dev_null(body[4:]):
                patch.new_name = _find_name(body[4:], None, p_value, TERM_TAB)
        elif body.startswith("Binary files") or body.startswith("GIT binary patch"):
            patch.is_binary = True
        i += 1
        linenr += 1

    # parse_git_diff_header "done:" — resolve names or fail.
    if patch.old_name is None and patch.new_name is None:
        if patch.def_name is None:
            _hdr_missing_filename(p_value, False, linenr, patch_input_file)
        patch.old_name = patch.def_name
        patch.new_name = patch.def_name
    if ((patch.new_name is None and patch.is_delete != 1) or
            (patch.old_name is None and patch.is_new != 1)):
        _hdr_missing_filename(p_value, True, linenr, patch_input_file)

    i = _parse_hunks(lines, i, patch)
    _finalize_names(patch, p_value)
    return patch, i


def _parse_traditional_patch(lines: list[str], i: int, p_value_opt: Optional[int],
                             prefix: Optional[str]) -> tuple[Optional[Patch], int]:
    n = len(lines)
    # find '--- ' then '+++ '
    if not lines[i].startswith("--- "):
        return None, i + 1
    minus = _strip_eol(lines[i])[4:]
    if i + 1 >= n or not lines[i + 1].startswith("+++ "):
        return None, i + 1
    plus = _strip_eol(lines[i + 1])[4:]
    if p_value_opt is None:
        p = _guess_p_value(minus, prefix)
        q = _guess_p_value(plus, prefix)
        if p < 0 and 0 <= q:
            p_value = q
        elif q < 0 and 0 <= p:
            p_value = p
        elif p < 0 and q < 0:
            p_value = 1
        else:
            p_value = min(p, q)
    else:
        p_value = p_value_opt
    patch = Patch(is_new=-1, is_delete=-1)
    if _is_dev_null(minus):
        patch.is_new = 1
        patch.old_name = None
    else:
        patch.old_name = _find_name_traditional(minus, None, p_value)
    if _is_dev_null(plus):
        patch.is_delete = 1
        patch.new_name = None
    else:
        patch.new_name = _find_name_traditional(plus, None, p_value)
    i = _parse_hunks(lines, i + 2, patch)
    _finalize_names(patch, p_value)
    return patch, i


def _finalize_names(patch: Patch, p_value: int) -> None:
    if patch.is_new == 1:
        patch.old_name = None
    if patch.is_delete == 1:
        patch.new_name = None
    if patch.old_name is None and patch.new_name is None and patch.def_name:
        patch.old_name = patch.new_name = patch.def_name


def _parse_hunks(lines: list[str], i: int, patch: Patch) -> int:
    n = len(lines)
    while i < n:
        line = lines[i]
        if not line.startswith("@@"):
            if line.startswith("diff --git ") or line.startswith("--- "):
                break
            i += 1
            continue
        frag, i = _parse_one_hunk(lines, i)
        if frag is None:
            break
        patch.fragments.append(frag)
    # recount lines_added/deleted
    for f in patch.fragments:
        for b in f.body:
            if b[:1] == b"+":
                patch.lines_added += 1
            elif b[:1] == b"-":
                patch.lines_deleted += 1
    return i


def _parse_one_hunk(lines: list[str], i: int) -> tuple[Optional[Fragment], int]:
    header = _strip_eol(lines[i])
    # @@ -A,B +C,D @@ optional
    try:
        meta = header.split("@@")[1].strip()
        a_part, b_part = meta.split(" ")[:2]
        a_part = a_part[1:]
        b_part = b_part[1:]
        a_start, _, a_count = a_part.partition(",")
        b_start, _, b_count = b_part.partition(",")
        frag = Fragment(
            oldpos=int(a_start),
            oldlines=int(a_count) if a_count else 1,
            newpos=int(b_start),
            newlines=int(b_count) if b_count else 1,
        )
    except (ValueError, IndexError):
        return None, i + 1
    i += 1
    n = len(lines)
    oldlines = frag.oldlines
    newlines = frag.newlines
    leading = 0
    trailing = 0
    added = deleted = 0
    while i < n:
        if oldlines == 0 and newlines == 0:
            break
        line = lines[i]
        # Normalise a bare empty line to an empty context line (newer GNU diff).
        if line == "":
            tag = " "
            body_bytes = b" "
        elif line == "\n":
            tag = " "
            body_bytes = b" \n"
        else:
            tag = line[0]
            body_bytes = line.encode("utf-8", "surrogateescape")
        if tag not in (" ", "-", "+", "\n"):
            break
        frag.body.append(body_bytes)
        i += 1
        # consume a following "\ No newline at end of file" marker
        if i < n and lines[i][:1] == "\\":
            frag.body.append(lines[i].encode("utf-8", "surrogateescape"))
            i += 1
        if tag in (" ", "\n"):
            oldlines -= 1
            newlines -= 1
            if not deleted and not added:
                leading += 1
            trailing += 1
        elif tag == "-":
            deleted += 1
            oldlines -= 1
            trailing = 0
        elif tag == "+":
            added += 1
            newlines -= 1
            trailing = 0
    frag.leading = leading
    frag.trailing = trailing
    return frag, i


# ---------------------------------------------------------------------------
# matching (find_pos / match_fragment) — see apply.c


@dataclass
class ApplyOpts:
    p_value: Optional[int] = None
    no_add: bool = False
    reverse: bool = False
    unidiff_zero: bool = False
    p_context: int = UINT_MAX
    allow_overlap: bool = False
    threeway: bool = False
    merge_variant: int = 0
    cached: bool = False
    check_index: bool = False
    ita_only: bool = False
    check: bool = False
    verbosity: int = 0  # 0 normal, -1 silent, 1 verbose


def _build_pre_post(frag: Fragment, opts: ApplyOpts) -> tuple[Image, Image, bool, bool]:
    """Mirror the line-collection loop of apply_one_fragment.

    Returns (preimage, postimage, match_beginning, match_end)."""
    pre = Image()
    post = Image()
    pre.lines = []
    pre.flags = []
    post.lines = []
    post.flags = []
    body = frag.body
    k = 0
    nb = len(body)
    while k < nb:
        ln = body[k]
        if not ln:
            k += 1
            continue
        first = chr(ln[0])
        # plen handling: drop the leading marker; if followed by '\ No newline'
        # then the content has no trailing newline
        content = ln[1:]
        next_is_noeol = (k + 1 < nb and body[k + 1][:1] == b"\\")
        if next_is_noeol and content.endswith(b"\n"):
            content = content[:-1]
        if opts.reverse:
            if first == "-":
                first = "+"
            elif first == "+":
                first = "-"
        if first == "\n":
            pre.lines.append(b"\n")
            pre.flags.append(LINE_COMMON)
            post.lines.append(b"\n")
            post.flags.append(LINE_COMMON)
        elif first == " ":
            pre.lines.append(content)
            pre.flags.append(LINE_COMMON)
            post.lines.append(content)
            post.flags.append(LINE_COMMON)
        elif first == "-":
            pre.lines.append(content)
            pre.flags.append(0)
        elif first == "+":
            if opts.no_add:
                k += 1
                continue
            post.lines.append(content)
            post.flags.append(0)
        # '@' '\\' ignored
        k += 1

    match_beginning = (frag.oldpos == 0 or
                       (frag.oldpos == 1 and not opts.unidiff_zero))
    match_end = (not opts.unidiff_zero) and (frag.trailing == 0)
    return pre, post, match_beginning, match_end


def _match_fragment(img: Image, pre: Image, current_lno: int,
                    match_beginning: bool, match_end: bool,
                    opts: ApplyOpts) -> bool:
    pre_nr = pre.line_nr
    if pre_nr + current_lno <= img.line_nr:
        preimage_limit = pre_nr
        if match_end and (pre_nr + current_lno != img.line_nr):
            return False
    else:
        # not removing blanks at eof in our (default) ws mode -> reject
        return False

    if match_beginning and current_lno:
        return False

    # quick line check (hash via direct compare) + LINE_PATCHED guard
    for k in range(preimage_limit):
        if img.flags[current_lno + k] & LINE_PATCHED:
            return False
        if pre.lines[k] != img.lines[current_lno + k]:
            return False
    return True


def _find_pos(img: Image, pre: Image, line: int,
              match_beginning: bool, match_end: bool, opts: ApplyOpts) -> int:
    if (opts.allow_overlap and match_beginning and match_end and
            img.line_nr - pre.line_nr != 0):
        match_beginning = False

    if match_beginning:
        line = 0
    elif match_end:
        line = img.line_nr - pre.line_nr

    if line > img.line_nr:
        line = img.line_nr
    if line < 0:
        # mirror the unsigned wrap: a negative line clamps to img.line_nr
        line = img.line_nr

    backwards_lno = forwards_lno = current_lno = line
    i = 0
    while True:
        if _match_fragment(img, pre, current_lno, match_beginning, match_end, opts):
            return current_lno
        # again:
        while True:
            if backwards_lno == 0 and forwards_lno == img.line_nr:
                return -1
            if i & 1:
                if backwards_lno == 0:
                    i += 1
                    continue
                backwards_lno -= 1
                current_lno = backwards_lno
            else:
                if forwards_lno == img.line_nr:
                    i += 1
                    continue
                forwards_lno += 1
                current_lno = forwards_lno
            break
        i += 1


def _update_image(img: Image, applied_pos: int, pre: Image, post: Image,
                  opts: ApplyOpts) -> None:
    preimage_limit = pre.line_nr
    if preimage_limit > img.line_nr - applied_pos:
        preimage_limit = img.line_nr - applied_pos
    new_lines = (img.lines[:applied_pos] + list(post.lines) +
                 img.lines[applied_pos + preimage_limit:])
    new_flags = (img.flags[:applied_pos] +
                 [(0 if opts.allow_overlap else LINE_PATCHED) | f
                  for f in post.flags] +
                 img.flags[applied_pos + preimage_limit:])
    img.lines = new_lines
    img.flags = new_flags


def _apply_one_fragment(img: Image, frag: Fragment, opts: ApplyOpts,
                        nth: int = 1, quiet: bool = False) -> bool:
    """Return True on success (mirror returns 0/applied_pos>=0)."""
    pre, post, match_beginning, match_end = _build_pre_post(frag, opts)
    leading = frag.leading
    trailing = frag.trailing
    pos = (frag.newpos - 1) if frag.newpos else 0

    while True:
        applied_pos = _find_pos(img, pre, pos, match_beginning, match_end, opts)
        if applied_pos >= 0:
            break
        if leading <= opts.p_context and trailing <= opts.p_context:
            break
        if match_beginning or match_end:
            match_beginning = match_end = False
            continue
        if leading >= trailing:
            pre.remove_first_line()
            post.remove_first_line()
            pos -= 1
            leading -= 1
        if trailing > leading:
            pre.remove_last_line()
            post.remove_last_line()
            trailing -= 1

    if applied_pos >= 0:
        if (not quiet and opts.verbosity > 0 and applied_pos != pos):
            offset = applied_pos - pos
            if opts.reverse:
                offset = -offset
            # ngettext English plural: singular only when n == 1.
            unit = "line" if offset == 1 else "lines"
            _stderr("Hunk #%d succeeded at %d (offset %d %s)."
                    % (nth, applied_pos + 1, offset, unit))
        if ((leading != frag.leading or trailing != frag.trailing)
                and opts.verbosity > -1 and not quiet):
            _stderr("Context reduced to (%d/%d) to apply fragment at %d"
                    % (leading, trailing, applied_pos + 1))
        _update_image(img, applied_pos, pre, post, opts)
        return True
    return False


def _stderr(msg: str) -> None:
    import sys
    sys.stderr.write(msg + "\n")


def _apply_fragments_to_image(img: Image, patch: Patch, opts: ApplyOpts,
                              report_name: str, *, quiet: bool = False) -> bool:
    """Apply all hunks; returns True on full success.  On failure emits the
    git 'patch failed: name:oldpos' error (unless quiet)."""
    nth = 0
    for frag in patch.fragments:
        nth += 1
        if not _apply_one_fragment(img, frag, opts, nth=nth, quiet=quiet):
            if not quiet:
                _stderr("error: patch failed: %s:%d" % (report_name, frag.oldpos))
            return False
    return True


# ---------------------------------------------------------------------------
# high-level driver (mirror apply_patch / check_patch / write_out_results)


def _read_worktree(repo, name: str) -> Optional[bytes]:
    p = repo.path / name
    try:
        return p.read_bytes()
    except FileNotFoundError:
        return None
    except IsADirectoryError:
        return None


def _index_blob(repo, idx, name: str) -> Optional[str]:
    e = idx.by_path().get(name)
    return e.sha if e is not None else None


def _resolve_blob(repo, prefix: str) -> Optional[bytes]:
    """Read a blob given a (possibly abbreviated) object id; None if absent."""
    full = refs_mod.rev_parse(repo, prefix)
    if full is None:
        return None
    try:
        t, data = objs.read_object(repo, full)
    except (KeyError, ValueError):
        return None
    if t != "blob":
        return None
    return data


def _verify_index_match(repo, idx, name: str, content: bytes) -> bool:
    """Approximate ie_match_stat: the worktree file must hash to the index
    blob.  Returns True if it matches."""
    e = idx.by_path().get(name)
    if e is None:
        return False
    sha, _ = objs.hash_bytes("blob", content, repo)
    return sha == e.sha


def _three_way_merge(repo, path: str, base: Optional[str], ours: str,
                     theirs: str, variant: int) -> tuple[bytes, int]:
    """Mirror three_way_merge(): trivial cases first, else ll_merge.

    Returns (result_bytes, status) where status 0 = clean, >0 = conflicts."""
    if base is not None and base == ours:
        return _resolve_to(repo, theirs), 0
    if (base is not None and base == theirs) or ours == theirs:
        return _resolve_to(repo, ours), 0
    base_data = _read_blob(repo, base) if base is not None else b""
    our_data = _read_blob(repo, ours)
    their_data = _read_blob(repo, theirs)
    result, nconf = xdiff.xdl_merge(
        base_data, our_data, their_data,
        level=xdiff.XDL_MERGE_ZEALOUS, style=0, favor=variant,
        flags=0, marker_size=xdiff.DEFAULT_CONFLICT_MARKER_SIZE,
        name1="ours", name2="theirs", ancestor_name="base")
    return result, nconf


def _resolve_to(repo, oid: str) -> bytes:
    return _read_blob(repo, oid)


def _read_blob(repo, oid: str) -> bytes:
    if oid == EMPTY_BLOB:
        return b""
    t, data = objs.read_object(repo, oid)
    return data


def _write_blob(repo, data: bytes) -> str:
    return objs.write_object(repo, "blob", data)


def check_and_apply(repo, patches: list[Patch], opts: ApplyOpts,
                    patch_input_file: str) -> int:
    """Run the check + apply phases. Returns the process rc.

    Mirrors apply_patch(): check_patch_list() builds each patch's in-memory
    result (calling apply_data, which is where the actual hunk matching and the
    3-way fallback happen), then write_out_results() commits them."""
    from .index import read_index, write_index

    update_index = (opts.check_index or opts.ita_only) and (not opts.check)
    idx = read_index(repo) if (opts.check_index or update_index) else None

    # ---- check_patch_list: per-patch check_preimage + apply_data --------------
    for patch in patches:
        if opts.verbosity > 0:
            _say_patch_name("Checking patch %s...", patch)
        rc = _check_one(repo, idx, patch, opts)
        if rc is not None:
            return rc
        if patch.is_delete > 0:
            patch.result = b""
        else:
            if _apply_data(repo, idx, patch, opts) < 0:
                name = patch.old_name if patch.old_name else patch.new_name
                _stderr("error: %s: patch does not apply" % name)
                return 1

    if opts.check:
        return 0

    # ---- write_out_results: phase 0 remove, phase 1 create -------------------
    conflicted: list[str] = []
    errs = False
    for phase in (0, 1):
        for patch in patches:
            r = _write_out_one(repo, idx, patch, opts, phase, conflicted)
            if r < 0:
                return 128
    if conflicted:
        conflicted.sort()
        if opts.verbosity > -1:
            for p in conflicted:
                _stderr("U %s" % p)
        errs = True

    if update_index and idx is not None:
        write_index(repo, idx)

    return 1 if errs else 0


def _say_patch_name(fmt: str, patch: Patch) -> None:
    name = patch.new_name if patch.new_name else patch.old_name
    _stderr(fmt % name)


def _check_to_create(repo, idx, opts: ApplyOpts, new_name: str) -> int:
    """Mirror check_to_create(). Returns 0 ok, or one of the EXISTS_* codes."""
    if opts.check_index and not opts.cached:
        e = idx.by_path().get(new_name) if idx is not None else None
        if e is not None:
            if not e.intent_to_add:
                return EXISTS_IN_INDEX
            if not opts.cached and e.intent_to_add:
                return EXISTS_IN_INDEX_AS_ITA
    if opts.cached:
        return 0
    p = repo.path / new_name
    if p.is_symlink() or p.exists():
        if p.is_dir():
            return 0
        return EXISTS_IN_WORKTREE
    return 0


EXISTS_IN_INDEX = 1
EXISTS_IN_WORKTREE = 2
EXISTS_IN_INDEX_AS_ITA = 3


def _check_one(repo, idx, patch: Patch, opts: ApplyOpts) -> Optional[int]:
    """Mirror check_preimage/check_to_create for a single patch.  Returns an
    rc to abort with, or None to continue."""
    name = patch.old_name
    if patch.is_new > 0 or patch.is_rename or patch.is_copy:
        if patch.new_name is not None:
            err = _check_to_create(repo, idx, opts, patch.new_name)
            if err and opts.threeway:
                patch.direct_to_threeway = True
            elif err == EXISTS_IN_INDEX:
                _stderr("error: %s: already exists in index" % patch.new_name)
                return 1
            elif err == EXISTS_IN_INDEX_AS_ITA:
                _stderr("error: %s: does not match index" % patch.new_name)
                return 1
            elif err == EXISTS_IN_WORKTREE:
                _stderr("error: %s: already exists in working directory" % patch.new_name)
                return 1
    if patch.is_new > 0:
        return None
    if name is None:
        return None
    if opts.check_index or opts.cached:
        if idx is not None and name not in idx.by_path():
            if patch.is_new < 0:
                patch.is_new = 1
                patch.is_delete = 0
                patch.old_name = None
                return None
            _stderr("error: %s: does not exist in index" % name)
            return 1
        if not opts.cached:
            content = _read_worktree(repo, name)
            if content is None:
                _stderr("error: %s: No such file or directory" % name)
                return 1
            if not _verify_index_match(repo, idx, name, content):
                _stderr("error: %s: does not match index" % name)
                return 1
    else:
        content = _read_worktree(repo, name)
        if content is None:
            if patch.is_new < 0:
                patch.is_new = 1
                patch.is_delete = 0
                patch.old_name = None
                return None
            _stderr("error: %s: No such file or directory" % name)
            return 1
    if patch.is_new < 0:
        patch.is_new = 0
    return None


def _load_preimage(repo, idx, patch: Patch, opts: ApplyOpts) -> bytes:
    name = patch.old_name
    if name is None:
        return b""
    if opts.cached or opts.check_index:
        sha = _index_blob(repo, idx, name)
        if sha is None:
            return b""
        return _read_blob(repo, sha)
    data = _read_worktree(repo, name)
    return data if data is not None else b""


def _load_current(repo, idx, patch: Patch, opts: ApplyOpts) -> Optional[bytes]:
    """Mirror load_current(): for an add/add 3-way, 'ours' is the file that
    already exists.  It must be present in the index; otherwise report
    'does not exist in index' and fail (caller then prints the cannot-read
    error)."""
    name = patch.new_name
    sha = _index_blob(repo, idx, name) if idx is not None else None
    if sha is None:
        _stderr("error: %s: does not exist in index" % name)
        return None
    if opts.cached:
        return _read_blob(repo, sha)
    data = _read_worktree(repo, name)
    if data is None:
        # the index entry exists but the worktree file is gone: git would
        # checkout the index version.
        return _read_blob(repo, sha)
    if not _verify_index_match(repo, idx, name, data):
        _stderr("error: %s: does not match index" % name)
        return None
    return data


def _apply_data(repo, idx, patch: Patch, opts: ApplyOpts) -> int:
    """Mirror apply_data(): load preimage, apply (with 3way fallback)."""
    pre_bytes = _load_preimage(repo, idx, patch, opts)
    img = Image(pre_bytes)
    report = patch.old_name if patch.old_name else patch.new_name

    if not opts.threeway or _try_threeway(repo, idx, patch, opts, img) < 0:
        if opts.threeway and not patch.direct_to_threeway and opts.verbosity > -1:
            _stderr("Falling back to direct application...")
        # With direct_to_threeway the 3-way merge is the only path; do not fall
        # back to a normal apply.
        if patch.direct_to_threeway or not _apply_fragments_to_image(img, patch, opts, report):
            return -1
    patch.result = img.buf
    return 0


def _try_threeway(repo, idx, patch: Patch, opts: ApplyOpts, image: Image) -> int:
    """Mirror try_threeway(). Returns 0 on success (sets patch.result via the
    passed image / conflict stages), -1 to fall back."""
    if (patch.is_delete > 0 or
            (patch.is_new and not patch.direct_to_threeway) or
            (patch.is_rename and not patch.lines_added and not patch.lines_deleted)):
        return -1

    # Preimage the patch was prepared for.
    if patch.is_new:
        pre_oid = EMPTY_BLOB
        pre_data = b""
    else:
        pre_data = _resolve_blob(repo, patch.old_oid_prefix)
        if pre_data is None:
            _stderr("error: repository lacks the necessary blob to perform 3-way merge.")
            return -1
        pre_oid = refs_mod.rev_parse(repo, patch.old_oid_prefix)

    if opts.verbosity > -1 and patch.direct_to_threeway:
        _stderr("Performing three-way merge...")

    # Apply the patch to the preimage blob to get the postimage ('theirs').
    tmp = Image(pre_data)
    if not _apply_fragments_to_image(tmp, patch, opts, patch.old_name or patch.new_name, quiet=True):
        return -1
    post_oid = _write_blob(repo, tmp.buf)

    # 'ours' is the current contents (worktree or index).
    if patch.is_new:
        our_data = _load_current(repo, idx, patch, opts)
        if our_data is None:
            _stderr("error: cannot read the current contents of '%s'" % patch.new_name)
            return -1
    else:
        our_data = _load_preimage(repo, idx, patch, opts)
    our_oid = _write_blob(repo, our_data)

    result, status = _three_way_merge(
        repo, patch.new_name, pre_oid, our_oid, post_oid, opts.merge_variant)
    if status < 0:
        if opts.verbosity > -1:
            _stderr("Failed to perform three-way merge...")
        return -1

    image.lines = _split_lines(result)
    image.flags = [0] * len(image.lines)

    if status:
        patch.conflicted_threeway = True
        patch.threeway_stage[0] = None if patch.is_new else pre_oid
        patch.threeway_stage[1] = our_oid
        patch.threeway_stage[2] = post_oid
        if opts.verbosity > -1:
            _stderr("Applied patch to '%s' with conflicts." % patch.new_name)
    else:
        if opts.verbosity > -1:
            _stderr("Applied patch to '%s' cleanly." % patch.new_name)
    return 0


def _write_out_one(repo, idx, patch: Patch, opts: ApplyOpts, phase: int,
                   conflicted: list[str]) -> int:
    if patch.is_delete > 0:
        if phase == 0:
            return _remove_file(repo, idx, patch, opts)
        if phase == 1:
            _write_out_one_reject(opts, patch)
        return 0
    if patch.is_new > 0 or patch.is_copy:
        if phase == 1:
            r = _create_file(repo, idx, patch, opts)
            if r < 0:
                return r
            _write_out_one_reject(opts, patch)
            if patch.conflicted_threeway:
                conflicted.append(patch.new_name)
        return 0
    # modify or rename: remove old, create new
    if phase == 0:
        return _remove_file(repo, idx, patch, opts, rmdir=patch.is_rename)
    if phase == 1:
        r = _create_file(repo, idx, patch, opts)
        if r < 0:
            return r
        _write_out_one_reject(opts, patch)
        if patch.conflicted_threeway:
            conflicted.append(patch.new_name)
    return 0


def _write_out_one_reject(opts: ApplyOpts, patch: Patch) -> None:
    """Without --reject (we never produce .rej here), write_out_one_reject only
    prints the verbose 'Applied patch %s cleanly.' notice."""
    if opts.verbosity > 0:
        _say_patch_name("Applied patch %s cleanly.", patch)


def _remove_file(repo, idx, patch: Patch, opts: ApplyOpts, rmdir: bool = True) -> int:
    update_index = (opts.check_index or opts.ita_only) and not opts.check
    if update_index and not opts.ita_only and idx is not None:
        idx.remove(patch.old_name)
    if not opts.cached:
        p = repo.path / patch.old_name
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        if rmdir:
            try:
                p.parent.rmdir()
            except OSError:
                pass
    return 0


def _create_file(repo, idx, patch: Patch, opts: ApplyOpts) -> int:
    from .index import IndexEntry

    path = patch.new_name
    mode = patch.new_mode if patch.new_mode else 0o100644
    buf = patch.result if patch.result is not None else b""

    if not opts.cached:
        p = repo.path / path
        p.parent.mkdir(parents=True, exist_ok=True)
        if mode & 0o170000 == 0o120000:
            # symlink
            try:
                if p.exists() or p.is_symlink():
                    p.unlink()
            except OSError:
                pass
            os_symlink(buf, p)
        else:
            p.write_bytes(buf)
            _set_exec(p, bool(mode & 0o111))

    update_index = (opts.check_index or opts.ita_only) and not opts.check
    if patch.conflicted_threeway:
        return _add_conflicted_stages(repo, idx, patch, opts)
    if opts.check_index or (opts.ita_only and patch.is_new > 0):
        return _add_index_file(repo, idx, patch, opts, path, mode, buf)
    return 0


def os_symlink(buf: bytes, p) -> None:
    import os as _os
    target = buf.decode("utf-8", "surrogateescape")
    _os.symlink(target, p)


def _set_exec(p, executable: bool) -> None:
    import os as _os
    import stat as _stat
    st = _os.stat(p)
    if executable:
        _os.chmod(p, st.st_mode | 0o111)


def _add_index_file(repo, idx, patch: Patch, opts: ApplyOpts, path: str,
                    mode: int, buf: bytes) -> int:
    from .index import IndexEntry
    import os as _os

    if idx is None:
        return 0
    idx.remove(path)
    entry = IndexEntry()
    entry.path = path
    entry.mode = mode
    entry.stage = 0
    if opts.ita_only:
        entry.intent_to_add = True
        entry.sha = EMPTY_BLOB
        entry.flags |= 0x4000  # CE_EXTENDED so the v3 writer emits the flags
    else:
        entry.sha = _write_blob(repo, buf)
        if not opts.cached:
            try:
                st = _os.stat(repo.path / path)
                entry.ctime_s = int(st.st_ctime)
                entry.ctime_n = getattr(st, "st_ctime_ns", 0) % 1_000_000_000
                entry.mtime_s = int(st.st_mtime)
                entry.mtime_n = getattr(st, "st_mtime_ns", 0) % 1_000_000_000
                entry.dev = st.st_dev
                entry.ino = st.st_ino
                entry.uid = st.st_uid
                entry.gid = st.st_gid
                entry.size = st.st_size
            except OSError:
                pass
    idx.upsert(entry)
    return 0


def _add_conflicted_stages(repo, idx, patch: Patch, opts: ApplyOpts) -> int:
    from .index import IndexEntry

    update_index = (opts.check_index or opts.ita_only) and not opts.check
    if not update_index or idx is None:
        return 0
    mode = patch.new_mode if patch.new_mode else 0o100644
    idx.remove(patch.new_name)
    for stage in (1, 2, 3):
        oid = patch.threeway_stage[stage - 1]
        if oid is None:
            continue
        entry = IndexEntry()
        entry.path = patch.new_name
        entry.mode = mode
        entry.stage = stage
        entry.sha = oid
        idx.upsert(entry)
    return 0
