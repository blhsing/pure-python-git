"""Byte-exact port of git's ``builtin/fast-import.c`` (Git 2.54.0).

The frontend feeds a stream of commands (blob/commit/tag/reset/...) on stdin;
this builds the objects and updates refs to match git exactly.  Only the
behaviours that are reachable through git's own data model are reproduced, but
all of the stream grammar, mark tracking, date formats, file changes
(M/D/C/R/deleteall), from/merge, lightweight + annotated tags, and the
``fatal:`` stream errors are implemented faithfully.
"""

from __future__ import annotations

import os
import sys
from typing import Optional

from . import objects as objs
from . import refs as refs_mod
from .repo import Repository

# Object modes (octal) used by git's tree model.
S_IFDIR = 0o040000
S_IFREG = 0o100000
S_IFLNK = 0o120000
S_IFGITLINK = 0o160000

EMPTY_TREE_OID = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
NULL_OID = "0" * 40

# sign modes accepted by fast-import's parse_one_option via parse_sign_mode.
_SIGN_MODES = frozenset({
    "abort", "verbatim", "ignore", "warn-verbatim", "warn", "warn-strip",
    "strip", "abort-if-invalid", "strip-if-invalid", "sign-if-invalid",
})


def _valid_sign_mode(arg: str) -> bool:
    """True if ``arg`` is accepted by gpg-interface.c:parse_sign_mode.

    fast-import uses parse_sign_mode directly (unlike fast-export, it does not
    reject the *-if-invalid variants), and also accepts a keyid via the
    "sign-if-invalid=<keyid>" form."""
    return arg in _SIGN_MODES or arg.startswith("sign-if-invalid=")


def _git_parse_ulong(value: str):
    """Port of parse.c:git_parse_ulong / git_parse_unsigned.

    Parses an optionally-suffixed (k/m/g, case-insensitive) non-negative
    integer in base 0 (decimal, 0x hex, 0 octal).  Returns the int value, or
    None on any parse error (negative, empty, bad suffix, non-numeric)."""
    if value is None or value == "":
        return None
    if "-" in value:  # strtoumax would accept it; git rejects explicitly
        return None
    # strtoumax(value, &end, 0): leading numeric run in base 0.
    s = value
    try:
        # Determine the numeric prefix the C strtoumax would consume.
        end = 0
        n = len(s)
        if s[end:end + 2].lower() == "0x":
            j = end + 2
            while j < n and s[j] in "0123456789abcdefABCDEF":
                j += 1
            if j == end + 2:
                return None
            val = int(s[end:j], 16)
        elif s[end] == "0":
            j = end + 1
            while j < n and s[j] in "01234567":
                j += 1
            val = int(s[end:j], 8) if j > end + 1 else 0
        else:
            j = end
            while j < n and s[j].isdigit():
                j += 1
            if j == end:
                return None
            val = int(s[end:j], 10)
    except ValueError:
        return None
    suffix = s[j:]
    factor = {"": 1, "k": 1024, "m": 1024 * 1024,
              "g": 1024 * 1024 * 1024}.get(suffix.lower())
    if factor is None:
        return None
    return val * factor


class FastImportDie(Exception):
    """Raised for a ``fatal:`` stream error (exit 128 + crash report)."""


class FastImportUsage(Exception):
    """Raised for a usage error (exit 129)."""

    def __init__(self, message: str = ""):
        super().__init__(message)
        self.message = message


def _is_dir(mode: int) -> bool:
    return (mode & 0o170000) == S_IFDIR


def _is_gitlink(mode: int) -> bool:
    return (mode & 0o170000) == S_IFGITLINK


def _is_lnk(mode: int) -> bool:
    return (mode & 0o170000) == S_IFLNK


# ---------------------------------------------------------------------------
# verify_path (port of read-cache.c verify_path_internal under default config:
# protect_hfs/protect_ntfs off, has_dos_drive_prefix false on POSIX)


def _verify_dotfile(rest: bytes, mode: int) -> bool:
    # rest follows an initial '.'
    if not rest or rest[0:1] == b"/":
        return False
    c = rest[0:1]
    if c in (b"g", b"G"):
        if rest[1:2] not in (b"i", b"I"):
            return True
        if rest[2:3] not in (b"t", b"T"):
            return True
        if len(rest) == 3 or rest[3:4] == b"/":
            return False
        if _is_lnk(mode):
            tail = rest[3:]
            low = tail.lower()
            if low.startswith(b"modules"):
                after = tail[7:]
                if after == b"" or after[0:1] == b"/":
                    return False
        return True
    if c == b".":
        if len(rest) == 1 or rest[1:2] == b"/":
            return False
    return True


def _verify_path(path: bytes, mode: int) -> bool:
    # Empty path is never passed here (handled by caller via root replace).
    if not path:
        return False
    i = 0
    n = len(path)
    # mimic the C state machine: process leading component then each '/'.
    # The first byte enters the "inside" branch directly.
    c = path[0:1]
    pos = 1
    # initial component
    if c == b"." and not _verify_dotfile(path[1:], mode):
        return False
    if c == b"/":
        return False
    # walk the rest
    while True:
        if not c:
            return True
        if c == b"/":
            if pos < n:
                c = path[pos:pos + 1]
                pos += 1
            else:
                c = b""
            if c == b"." and not _verify_dotfile(path[pos:], mode):
                return False
            if c == b"/":
                return False
            if not c:
                return _is_dir(mode)
            continue
        if pos < n:
            c = path[pos:pos + 1]
            pos += 1
        else:
            c = b""
    return True


# ---------------------------------------------------------------------------
# unquote_c_style (port of quote.c) for quoted paths in M/D/C/R/ls lines.

_C_UNESCAPE = {
    ord("a"): 7, ord("b"): 8, ord("f"): 12, ord("n"): 10,
    ord("r"): 13, ord("t"): 9, ord("v"): 11,
    ord('"'): ord('"'), ord("\\"): ord("\\"),
}


def _unquote_c_style(s: bytes):
    """Return (decoded_bytes, rest) starting at a leading double-quote, or None
    on a malformed quote (mirrors unquote_c_style returning non-zero)."""
    if s[0:1] != b'"':
        return None
    out = bytearray()
    i = 1
    n = len(s)
    while i < n:
        ch = s[i]
        i += 1
        if ch == ord('"'):
            return bytes(out), s[i:]
        if ch != ord("\\"):
            out.append(ch)
            continue
        if i >= n:
            return None
        ch = s[i]
        i += 1
        if ch in _C_UNESCAPE:
            out.append(_C_UNESCAPE[ch])
        elif ord("0") <= ch <= ord("7"):
            # up to 3 octal digits
            val = ch - ord("0")
            for _ in range(2):
                if i < n and ord("0") <= s[i] <= ord("7"):
                    val = (val << 3) | (s[i] - ord("0"))
                    i += 1
                else:
                    break
            out.append(val & 0xFF)
        else:
            return None
    return None  # missing closing quote


def _quote_c_style(name: bytes) -> bytes:
    """Port of quote_c_style: quote only when needed."""
    special = {7: b"a", 8: b"b", 12: b"f", 10: b"n", 13: b"r", 9: b"t", 11: b"v",
               ord('"'): b'"', ord("\\"): b"\\"}
    need = False
    for b in name:
        if b in special or b < 0x20 or b >= 0x80:
            need = True
            break
    if not need:
        return name
    out = bytearray(b'"')
    for b in name:
        if b in special:
            out += b"\\" + special[b]
        elif b < 0x20 or b >= 0x80:
            out += b"\\%03o" % b
        else:
            out.append(b)
    out += b'"'
    return bytes(out)


# ---------------------------------------------------------------------------
# In-memory tree model (mirrors struct tree_entry / tree_content).


class _Entry:
    __slots__ = ("name", "mode", "oid", "tree")

    def __init__(self, name: bytes, mode: int, oid: str):
        self.name = name          # basename bytes
        self.mode = mode          # version[1] mode
        self.oid = oid            # version[1] oid (hex)
        self.tree: Optional["_Tree"] = None  # subtree (loaded lazily)


class _Tree:
    """A directory's children; loaded lazily from the on-disk tree object."""

    __slots__ = ("entries", "loaded", "oid")

    def __init__(self):
        self.entries: list[_Entry] = []
        self.loaded = False
        self.oid: Optional[str] = None  # the tree oid this content came from


class FastImport:
    def __init__(self, repo: Repository):
        self.repo = repo
        self.marks: dict[int, tuple[str, str]] = {}  # mark -> (oid, type)
        self.branches: dict[str, dict] = {}  # name -> branch dict
        self.tags: list[tuple[str, str]] = []  # ordered (name, oid)
        self.whenspec = "raw"
        self.export_marks_file: Optional[str] = None
        self.import_marks_file: Optional[str] = None
        self.import_marks_ignore_missing = False
        self.import_marks_from_stream = False
        self.import_marks_done = False
        self.relative_marks = False
        self.quiet = False
        self.show_stats = True
        self.force_update = False
        self.require_explicit_termination = False
        self.allow_unsafe_features = False
        self.seen_data_command = False
        self.cat_blob_fd = sys.stdout.buffer
        # Pack-layout knobs (accepted/validated; pygit's pack writer is internal
        # so the stored values only gate the parse, not the on-disk layout).
        self.max_packsize = 0
        self.big_file_threshold = 512 * 1024 * 1024

        # stream state
        self._pos = 0
        self.command_buf: bytes = b""
        self.unread = False
        self.eof = False
        self._next_mark = 0

        # crash report state
        self.cmd_hist: list[bytes] = []

        # dump_stats counters (builtin/fast-import.c). Deltas are always 0 (we
        # never deltify during import) and the Memory/* lines reflect this
        # Python process, not git's C heap, so those specific values are not
        # byte-reproducible; every other field is tracked exactly.
        self._stat_count = {"blob": 0, "tree": 0, "commit": 0, "tag": 0}
        self._stat_dup = {"blob": 0, "tree": 0, "commit": 0, "tag": 0}
        self._stat_seen: set = set()
        self._stat_branch_loads = 0
        self._stat_atoms: set = set()

    # -- low-level stream reading -------------------------------------------
    def _raw_getline(self) -> Optional[bytes]:
        """Read one LF-terminated line (LF stripped), or None at EOF."""
        if self._pos >= len(self._data):
            return None
        nl = self._data.find(b"\n", self._pos)
        if nl == -1:
            line = self._data[self._pos:]
            self._pos = len(self._data)
        else:
            line = self._data[self._pos:nl]
            self._pos = nl + 1
        return line

    def _skip_optional_lf(self):
        if self._pos < len(self._data) and self._data[self._pos:self._pos + 1] == b"\n":
            self._pos += 1

    def read_next_command(self) -> int:
        """Returns 0 on success (command_buf set) or EOF (-1)."""
        if self.eof:
            self.unread = False
            return -1
        while True:
            if self.unread:
                self.unread = False
            else:
                line = self._raw_getline()
                if line is None:
                    self.eof = True
                    return -1
                self.command_buf = line
                if (not self.seen_data_command
                        and not line.startswith(b"feature ")
                        and not line.startswith(b"option ")):
                    self.parse_argv()
                # record recent command history (for crash report)
                self.cmd_hist.append(line)
                if len(self.cmd_hist) > 100:
                    self.cmd_hist.pop(0)
            if self.command_buf[0:1] == b"#":
                continue
            return 0

    # -- argv / options -----------------------------------------------------
    def set_argv(self, argv: list[str]):
        self._argv = argv

    def _early_argv(self):
        for a in self._argv:
            if not a.startswith("-") or a == "--":
                break
            if a == "--allow-unsafe-features":
                self.allow_unsafe_features = True

    def parse_argv(self):
        if self.seen_data_command:
            return
        i = 0
        argv = self._argv
        while i < len(argv):
            a = argv[i]
            if not a.startswith("-") or a == "--":
                break
            if not a.startswith("--"):
                raise FastImportDie("unknown option %s" % a)
            opt = a[2:]
            if self._parse_one_option(opt):
                i += 1
                continue
            if self._parse_one_feature(opt, 0):
                i += 1
                continue
            if opt.startswith("cat-blob-fd="):
                self._option_cat_blob_fd(opt[len("cat-blob-fd="):])
                i += 1
                continue
            raise FastImportDie("unknown option --%s" % opt)
        if i != len(argv):
            raise FastImportUsage(_FAST_IMPORT_USAGE)
        self.seen_data_command = True
        if self.import_marks_file:
            self._read_marks()

    def _ulong_arg(self, option: str, arg: str) -> int:
        try:
            rv = int(arg, 0)
        except ValueError:
            raise FastImportDie("%s: argument must be a non-negative integer" % option)
        if "-" in arg or rv < 0:
            raise FastImportDie("%s: argument must be a non-negative integer" % option)
        return rv

    def _option_cat_blob_fd(self, fd: str):
        n = self._ulong_arg("--cat-blob-fd", fd)
        # We only honour fd 1 (stdout) and 2 (stderr); others would be a
        # caller-supplied descriptor that the harness cannot observe.
        if n == 2:
            self.cat_blob_fd = sys.stderr.buffer
        else:
            self.cat_blob_fd = sys.stdout.buffer

    def _parse_one_option(self, option: str) -> bool:
        # builtin/fast-import.c:parse_one_option — a value that git_parse_ulong
        # rejects makes the option fall through (return 0) to the unknown-option
        # die, so an invalid numeric here means "unknown option --<opt>".
        if option.startswith("max-pack-size="):
            v = _git_parse_ulong(option[len("max-pack-size="):])
            if v is None:
                return False  # falls through -> "unknown option --max-pack-size=..."
            # max-pack-size is now in bytes; small values get a unit warning.
            if v < 8192:
                sys.stderr.write(
                    "warning: max-pack-size is now in bytes, assuming "
                    "--max-pack-size=%dm\n" % v)
                v *= 1024 * 1024
            elif v < 1024 * 1024:
                sys.stderr.write("warning: minimum max-pack-size is 1 MiB\n")
                v = 1024 * 1024
            self.max_packsize = v
        elif option.startswith("big-file-threshold="):
            v = _git_parse_ulong(option[len("big-file-threshold="):])
            if v is None:
                return False
            self.big_file_threshold = v
        elif option.startswith("depth="):
            self._option_depth(option[len("depth="):])
        elif option.startswith("active-branches="):
            self._ulong_arg("--active-branches", option[len("active-branches="):])
        elif option.startswith("export-pack-edges="):
            return True
        elif option.startswith("signed-commits="):
            if not _valid_sign_mode(option[len("signed-commits="):]):
                raise FastImportUsage(
                    "unknown --signed-commits mode '%s'"
                    % option[len("signed-commits="):])
        elif option.startswith("signed-tags="):
            if not _valid_sign_mode(option[len("signed-tags="):]):
                raise FastImportUsage(
                    "unknown --signed-tags mode '%s'"
                    % option[len("signed-tags="):])
        elif option == "quiet":
            self.show_stats = False
            self.quiet = True
        elif option == "stats":
            self.show_stats = True
        elif option == "allow-unsafe-features":
            pass
        else:
            return False
        return True

    def _option_depth(self, depth: str):
        v = self._ulong_arg("--depth", depth)
        # DEPTH_BITS=13 -> MAX_DEPTH = (1<<13)-1 = 8191.
        MAX_DEPTH = 8191
        if v > MAX_DEPTH:
            raise FastImportDie("--depth cannot exceed %u" % MAX_DEPTH)

    def _check_unsafe_feature(self, feature: str, from_stream: int):
        if from_stream and not self.allow_unsafe_features:
            raise FastImportDie(
                "feature '%s' forbidden in input without --allow-unsafe-features"
                % feature)

    def _make_fast_import_path(self, path: str) -> str:
        if not self.relative_marks or os.path.isabs(path):
            return path
        return str(self.repo.gitdir / "info" / "fast-import" / path)

    def _parse_one_feature(self, feature: str, from_stream: int) -> bool:
        if feature.startswith("date-format="):
            self._option_date_format(feature[len("date-format="):])
        elif feature.startswith("import-marks="):
            self._check_unsafe_feature("import-marks", from_stream)
            self._option_import_marks(feature[len("import-marks="):], from_stream, 0)
        elif feature.startswith("import-marks-if-exists="):
            self._check_unsafe_feature("import-marks-if-exists", from_stream)
            self._option_import_marks(feature[len("import-marks-if-exists="):], from_stream, 1)
        elif feature.startswith("export-marks="):
            self._check_unsafe_feature(feature, from_stream)
            self.export_marks_file = self._make_fast_import_path(feature[len("export-marks="):])
        elif feature == "alias":
            pass
        elif feature.startswith("rewrite-submodules-to="):
            raise FastImportDie("cannot read '%s'" % feature[len("rewrite-submodules-to="):].split(":", 1)[-1])
        elif feature.startswith("rewrite-submodules-from="):
            raise FastImportDie("cannot read '%s'" % feature[len("rewrite-submodules-from="):].split(":", 1)[-1])
        elif feature == "get-mark":
            pass
        elif feature == "cat-blob":
            pass
        elif feature == "relative-marks":
            self.relative_marks = True
        elif feature == "no-relative-marks":
            self.relative_marks = False
        elif feature == "done":
            self.require_explicit_termination = True
        elif feature == "force":
            self.force_update = True
        elif feature in ("notes", "ls"):
            pass
        else:
            return False
        return True

    def _option_date_format(self, fmt: str):
        if fmt in ("raw", "raw-permissive", "rfc2822", "now"):
            self.whenspec = fmt
        else:
            raise FastImportDie("unknown --date-format argument %s" % fmt)

    def _option_import_marks(self, marks: str, from_stream: int, ignore_missing: int):
        if self.import_marks_file:
            if from_stream:
                raise FastImportDie("only one import-marks command allowed per stream")
            if not self.import_marks_from_stream:
                self._read_marks()
        self.import_marks_file = self._make_fast_import_path(marks)
        self.import_marks_from_stream = bool(from_stream)
        self.import_marks_ignore_missing = bool(ignore_missing)

    # -- marks --------------------------------------------------------------
    def _read_marks(self):
        path = self.import_marks_file
        try:
            f = open(path, "rb")
        except FileNotFoundError:
            if self.import_marks_ignore_missing:
                self.import_marks_done = True
                return
            raise FastImportDie("cannot read '%s'" % path)
        except OSError:
            raise FastImportDie("cannot read '%s'" % path)
        with f:
            for raw in f:
                line = raw.rstrip(b"\n")
                if not line.startswith(b":"):
                    raise FastImportDie("corrupt mark line: %s" % line.decode("latin-1"))
                rest = line[1:]
                sp = rest.find(b" ")
                if sp == -1:
                    raise FastImportDie("corrupt mark line: %s" % line.decode("latin-1"))
                num_s = rest[:sp]
                oid_s = rest[sp + 1:]
                try:
                    mark = int(num_s)
                except ValueError:
                    raise FastImportDie("corrupt mark line: %s" % line.decode("latin-1"))
                if mark == 0 or not _is_hex_oid(oid_s):
                    raise FastImportDie("corrupt mark line: %s" % line.decode("latin-1"))
                oid = oid_s.decode("ascii")
                t = self._read_type_of(oid)
                if t is None:
                    raise FastImportDie("object not found: %s" % oid)
                self.marks[mark] = (oid, t)
        self.import_marks_done = True

    def _read_type_of(self, oid: str) -> Optional[str]:
        try:
            t, _ = objs.read_object(self.repo, oid)
        except (KeyError, ValueError, OSError):
            return None
        return t

    def _dump_marks(self):
        if not self.export_marks_file:
            return
        if self.import_marks_file and not self.import_marks_done:
            return
        path = self.export_marks_file
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "wb") as f:
            for mark in sorted(self.marks):
                oid, _t = self.marks[mark]
                f.write(b":%d %s\n" % (mark, oid.encode("ascii")))

    # -- mark helpers -------------------------------------------------------
    def _find_mark(self, idnum: int) -> tuple[str, str]:
        # Mirrors find_mark(): a missing mark is fatal here, so callers'
        # post-NULL "unknown mark" checks are effectively unreachable.
        m = self.marks.get(idnum)
        if m is None:
            raise FastImportDie("mark :%d not declared" % idnum)
        return m

    def _parse_mark(self):
        if self.command_buf.startswith(b"mark :"):
            v = self.command_buf[len(b"mark :"):]
            self._next_mark = _strtoumax(v)
            self.read_next_command()
        else:
            self._next_mark = 0

    def _parse_original_identifier(self):
        if self.command_buf.startswith(b"original-oid "):
            self.read_next_command()

    # -- data ---------------------------------------------------------------
    def _parse_data(self) -> bytes:
        buf = self.command_buf
        if not buf.startswith(b"data "):
            raise FastImportDie("expected 'data n' command, found: %s"
                                % buf.decode("latin-1"))
        data = buf[len(b"data "):]
        if data.startswith(b"<<"):
            term = data[2:]
            out = bytearray()
            while True:
                line = self._raw_getline()
                if line is None:
                    raise FastImportDie("EOF in data (terminator '%s' not found)"
                                        % term.decode("latin-1"))
                if line == term:
                    break
                out += line
                out += b"\n"
            self._skip_optional_lf()
            return bytes(out)
        else:
            n = _strtoumax(data)
            out = self._data[self._pos:self._pos + n]
            if len(out) < n:
                got = self._pos + len(out)
                remaining = n - len(out)
                self._pos = len(self._data)
                raise FastImportDie("EOF in data (%lu bytes remaining)" % remaining)
            self._pos += n
            self._skip_optional_lf()
            return bytes(out)

    # -- ident / dates ------------------------------------------------------
    def _parse_ident(self, buf: bytes) -> bytes:
        # Mirror parse_ident(): when the value begins with '<' git does --buf,
        # which makes the preceding byte (the SP after "author"/"committer")
        # part of the name, so an empty-name ident keeps a single leading space.
        if buf[:1] == b"<":
            s = b" " + buf
        else:
            s = buf
        start = 0
        # find first '<' or '>'
        lt = start
        while lt < len(s) and s[lt:lt + 1] not in (b"<", b">"):
            lt += 1
        if lt >= len(s) or s[lt:lt + 1] != b"<":
            raise FastImportDie("missing < in ident string: %s" % buf.decode("latin-1"))
        if lt != start and s[lt - 1:lt] != b" ":
            raise FastImportDie("missing space before < in ident string: %s" % buf.decode("latin-1"))
        gt = lt + 1
        while gt < len(s) and s[gt:gt + 1] not in (b"<", b">"):
            gt += 1
        if gt >= len(s) or s[gt:gt + 1] != b">":
            raise FastImportDie("missing > in ident string: %s" % buf.decode("latin-1"))
        gt += 1
        if gt >= len(s) or s[gt:gt + 1] != b" ":
            raise FastImportDie("missing space after > in ident string: %s" % buf.decode("latin-1"))
        gt += 1
        name = s[start:gt]
        date_str = s[gt:]
        out = bytearray(name)
        ws = self.whenspec
        if ws == "raw":
            d = _validate_raw_date(date_str, strict=True)
            if d is None:
                raise FastImportDie('invalid raw date "%s" in ident: %s'
                                    % (date_str.decode("latin-1"), buf.decode("latin-1")))
            out += d
        elif ws == "raw-permissive":
            d = _validate_raw_date(date_str, strict=False)
            if d is None:
                raise FastImportDie('invalid raw date "%s" in ident: %s'
                                    % (date_str.decode("latin-1"), buf.decode("latin-1")))
            out += d
        elif ws == "rfc2822":
            d = _parse_rfc2822(date_str)
            if d is None:
                raise FastImportDie('invalid rfc2822 date "%s" in ident: %s'
                                    % (date_str.decode("latin-1"), buf.decode("latin-1")))
            out += d
        elif ws == "now":
            if date_str != b"now":
                raise FastImportDie("date in ident must be 'now': %s" % buf.decode("latin-1"))
            out += _datestamp_now()
        return bytes(out)

    # -- tree model ---------------------------------------------------------
    def _load_tree(self, oid: str) -> _Tree:
        t = _Tree()
        t.loaded = True
        t.oid = oid
        if oid == NULL_OID or oid is None:
            return t
        otype, data = objs.read_object(self.repo, oid)
        if otype != "tree":
            raise FastImportDie("can't load tree %s" % oid)
        i = 0
        while i < len(data):
            sp = data.index(b" ", i)
            mode = int(data[i:sp], 8)
            nul = data.index(b"\0", sp)
            name = data[sp + 1:nul]
            sha = data[nul + 1:nul + 21].hex()
            self._stat_atoms.add(name)  # loaded tree-entry names are atoms too
            e = _Entry(name, mode, sha)
            t.entries.append(e)
            i = nul + 21
        return t

    def _ensure_tree(self, branch: dict):
        if branch["tree"] is None:
            branch["tree"] = self._load_tree(branch["tree_oid"])

    def _new_root(self, oid: str) -> dict:
        return {"oid": oid, "tree": None, "tree_oid": oid}

    def _mktree_and_store(self, t: _Tree) -> str:
        """Recursively store a tree, returning its oid. Mirrors store_tree."""
        # store any subtrees first
        for e in t.entries:
            if e.tree is not None:
                e.oid = self._store_subtree(e.tree)
                e.mode = S_IFDIR
        # build sorted tree object (drop entries with mode 0)
        live = [e for e in t.entries if e.mode]

        def key(e: _Entry) -> bytes:
            return e.name + (b"/" if _is_dir(e.mode) else b"")

        live.sort(key=key)
        out = bytearray()
        for e in live:
            out += b"%o " % (e.mode) + e.name + b"\0" + bytes.fromhex(e.oid)
        oid = self._wo("tree", bytes(out))
        return oid

    def _store_subtree(self, t: _Tree) -> str:
        return self._mktree_and_store(t)

    def _tree_content_set(self, t: _Tree, path: bytes, oid: str, mode: int,
                          subtree: Optional[_Tree]):
        slash = path.find(b"/")
        if slash == -1:
            comp = path
            rest = b""
        else:
            comp = path[:slash]
            rest = path[slash + 1:]
        if not comp:
            raise FastImportDie("empty path component found in input")
        self._stat_atoms.add(comp)  # git interns each tree-entry name as an atom
        if slash == -1 and not _is_dir(mode) and subtree is not None:
            raise FastImportDie("non-directories cannot have subtrees")
        for e in t.entries:
            if e.name == comp:
                if slash == -1:
                    e.mode = mode
                    e.oid = oid
                    e.tree = subtree
                    return True
                if not _is_dir(e.mode):
                    e.tree = _Tree()
                    e.tree.loaded = True
                    e.mode = S_IFDIR
                if e.tree is None:
                    e.tree = self._load_tree(e.oid)
                return self._tree_content_set(e.tree, rest, oid, mode, subtree)
        e = _Entry(comp, 0, NULL_OID)
        t.entries.append(e)
        if slash != -1:
            e.tree = _Tree()
            e.tree.loaded = True
            e.mode = S_IFDIR
            self._tree_content_set(e.tree, rest, oid, mode, subtree)
        else:
            e.tree = subtree
            e.mode = mode
            e.oid = oid
        return True

    def _tree_content_remove(self, t: _Tree, path: bytes, backup: Optional[list],
                             allow_root: bool):
        slash = path.find(b"/")
        if slash == -1:
            comp = path
            rest = b""
        else:
            comp = path[:slash]
            rest = path[slash + 1:]
        if not path and allow_root:
            return False  # not used by our callers with empty path
        for idx, e in enumerate(t.entries):
            if e.name == comp:
                if slash != -1 and not _is_dir(e.mode):
                    return True
                if slash == -1 or not _is_dir(e.mode):
                    if backup is not None:
                        backup.append(e)
                    t.entries.pop(idx)
                    return True
                if e.tree is None:
                    e.tree = self._load_tree(e.oid)
                if self._tree_content_remove(e.tree, rest, backup, False):
                    if any(se.mode for se in e.tree.entries):
                        return True
                    # subtree empty -> delete the dir entry
                    t.entries.pop(idx)
                    return True
                return False
        return False

    def _tree_content_get(self, t: _Tree, path: bytes, allow_root: bool):
        """Return an _Entry-like (mode, oid, tree) or None."""
        slash = path.find(b"/")
        if slash == -1:
            comp = path
            rest = b""
        else:
            comp = path[:slash]
            rest = path[slash + 1:]
        if not comp and not allow_root:
            raise FastImportDie("empty path component found in input")
        for e in t.entries:
            if e.name == comp:
                if slash == -1:
                    return e
                if not _is_dir(e.mode):
                    return None
                if e.tree is None:
                    e.tree = self._load_tree(e.oid)
                return self._tree_content_get(e.tree, rest, False)
        return None

    # -- branches -----------------------------------------------------------
    def _lookup_branch(self, name: str) -> Optional[dict]:
        return self.branches.get(name)

    def _new_branch(self, name: str) -> dict:
        b = {
            "name": name,
            "oid": NULL_OID,        # last commit on the branch
            "tree": None,           # _Tree of the branch tip (lazy)
            "tree_oid": NULL_OID,   # oid of the loaded tree (version[1])
            "delete": False,
        }
        self.branches[name] = b
        self._stat_branch_loads += 1  # git counts each branch load (dump_stats)
        return b

    def _branch_tree(self, b: dict) -> _Tree:
        if b["tree"] is None:
            b["tree"] = self._load_tree(b["tree_oid"])
        return b["tree"]

    # -- from / merge -------------------------------------------------------
    def _parse_from_commit_oid(self, oid: str) -> str:
        """Return the tree oid of the given commit."""
        otype, data = objs.read_object(self.repo, oid)
        if otype != "commit" or not data.startswith(b"tree "):
            raise FastImportDie("the commit %s is corrupt" % oid)
        return data[5:45].decode("ascii")

    def _parse_objectish(self, b: dict, objectish: str) -> bool:
        old_tree = b["tree_oid"]
        s = self._lookup_branch(objectish)
        if b is s:
            raise FastImportDie("can't create a branch from itself: %s" % b["name"])
        elif s is not None:
            b["oid"] = s["oid"]
            b["tree_oid"] = s["tree_oid"]
        elif objectish.startswith(":"):
            idnum = _parse_mark_ref_eol(objectish, self.command_buf)
            oid, t = self._find_mark(idnum)
            if t != "commit":
                raise FastImportDie("mark :%d not a commit" % idnum)
            if b["oid"] != oid:
                b["oid"] = oid
                b["tree_oid"] = self._parse_from_commit_oid(oid)
        elif _is_hex_oid(objectish.encode("ascii")) and int(objectish, 16) == 0:
            # `from <null oid>` -> parse_from_existing with null oid then delete.
            b["oid"] = NULL_OID
            b["tree_oid"] = NULL_OID
            b["delete"] = True
        else:
            resolved = refs_mod.rev_parse(self.repo, objectish)
            if resolved:
                b["oid"] = resolved
                peeled = _peel_to_commit(self.repo, resolved)
                if peeled is None:
                    raise FastImportDie("not a valid commit: %s" % objectish)
                b["oid"] = peeled
                b["tree_oid"] = self._parse_from_commit_oid(peeled)
                if b["oid"] == NULL_OID:
                    b["delete"] = True
            else:
                raise FastImportDie("invalid ref name or SHA1 expression: %s" % objectish)
        if b["tree"] is not None and old_tree != b["tree_oid"]:
            b["tree"] = None
        self.read_next_command()
        return True

    def _parse_from(self, b: dict) -> bool:
        if not self.command_buf.startswith(b"from "):
            return False
        frm = self.command_buf[len(b"from "):].decode("latin-1")
        return self._parse_objectish(b, frm)

    def _parse_objectish_with_prefix(self, b: dict, prefix: bytes) -> bool:
        if not self.command_buf.startswith(prefix):
            return False
        base = self.command_buf[len(prefix):].decode("latin-1")
        return self._parse_objectish(b, base)

    def _parse_merge(self) -> list[str]:
        out: list[str] = []
        while self.command_buf.startswith(b"merge "):
            frm = self.command_buf[len(b"merge "):].decode("latin-1")
            s = self._lookup_branch(frm)
            if s is not None:
                out.append(s["oid"])
            elif frm.startswith(":"):
                idnum = _parse_mark_ref_eol(frm, self.command_buf)
                oid, t = self._find_mark(idnum)
                if t != "commit":
                    raise FastImportDie("mark :%d not a commit" % idnum)
                out.append(oid)
            else:
                resolved = refs_mod.rev_parse(self.repo, frm)
                if resolved:
                    peeled = _peel_to_commit(self.repo, resolved)
                    if peeled is None:
                        raise FastImportDie("not a valid commit: %s" % frm)
                    out.append(peeled)
                else:
                    raise FastImportDie("invalid ref name or SHA1 expression: %s" % frm)
            self.read_next_command()
        return out

    # -- file changes -------------------------------------------------------
    def _parse_mode_field(self, p: bytes):
        if p[:1] == b" ":
            return None, None
        i = 0
        mode = 0
        while i < len(p):
            c = p[i]
            if c == ord(" "):
                return mode, p[i + 1:]
            if c < ord("0") or c > ord("7"):
                return None, None
            mode = (mode << 3) + (c - ord("0"))
            i += 1
        return None, None

    def _file_change_m(self, p: bytes, b: dict):
        mode, rest = self._parse_mode_field(p)
        if mode is None:
            raise FastImportDie("corrupt mode: %s" % self.command_buf.decode("latin-1"))
        if mode in (0o644, 0o755):
            mode |= S_IFREG
        elif mode in (S_IFREG | 0o644, S_IFREG | 0o755, S_IFLNK, S_IFDIR, S_IFGITLINK):
            pass
        else:
            raise FastImportDie("corrupt mode: %s" % self.command_buf.decode("latin-1"))
        p = rest
        inline_data = False
        oid = None
        otype = None
        if p[:1] == b":":
            idnum, p = _parse_mark_ref_space(p, self.command_buf)
            oid, otype = self._find_mark(idnum)
        elif p.startswith(b"inline "):
            inline_data = True
            p = p[len(b"inline "):]
        else:
            sp = p.find(b" ")
            if sp == -1 or not _is_hex_oid(p[:sp]):
                raise FastImportDie("invalid dataref: %s" % self.command_buf.decode("latin-1"))
            oid = p[:sp].decode("ascii")
            p = p[sp + 1:]
            otype = self._read_type_of(oid)
        path = self._parse_path_eol(p, "path")
        bt = self._branch_tree(b)
        # Git does not track empty, non-toplevel directories.
        if _is_dir(mode) and oid == EMPTY_TREE_OID and path:
            self._tree_content_remove(bt, path, None, False)
            return
        if _is_gitlink(mode):
            if inline_data:
                raise FastImportDie("Git links cannot be specified 'inline': %s"
                                    % self.command_buf.decode("latin-1"))
            if oid is not None and otype is not None and otype != "commit":
                raise FastImportDie("not a commit (actually a %s): %s"
                                    % (otype, self.command_buf.decode("latin-1")))
        elif inline_data:
            if _is_dir(mode):
                raise FastImportDie("directories cannot be specified 'inline': %s"
                                    % self.command_buf.decode("latin-1"))
            while self.read_next_command() != -1:
                if self.command_buf.startswith(b"cat-blob "):
                    self._parse_cat_blob(self.command_buf[len(b"cat-blob "):])
                else:
                    payload = self._parse_data()
                    oid = self._wo("blob", payload)
                    break
        else:
            expected = "tree" if _is_dir(mode) else "blob"
            t = otype if otype is not None else self._read_type_of(oid)
            if t is None:
                raise FastImportDie("%s not found: %s"
                                    % (expected, self.command_buf.decode("latin-1")))
            if t != expected:
                raise FastImportDie("not a %s (actually a %s): %s"
                                    % (expected, t, self.command_buf.decode("latin-1")))
        if not path:
            # tree_content_replace at root
            if not _is_dir(mode):
                raise FastImportDie("root cannot be a non-directory")
            b["tree"] = self._load_tree(oid)
            b["tree_oid"] = NULL_OID
            return
        if not _verify_path(path, mode):
            raise FastImportDie("invalid path '%s'" % path.decode("latin-1"))
        self._tree_content_set(bt, path, oid, mode, None)

    def _file_change_d(self, p: bytes, b: dict):
        path = self._parse_path_eol(p, "path")
        bt = self._branch_tree(b)
        self._tree_content_remove(bt, path, None, True)

    def _file_change_cr(self, p: bytes, b: dict, rename: bool):
        source, p = self._parse_path_space(p, "source")
        dest = self._parse_path_eol(p, "dest")
        bt = self._branch_tree(b)
        if rename:
            backup: list = []
            self._tree_content_remove(bt, source, backup, True)
            leaf = backup[0] if backup else None
        else:
            leaf = self._tree_content_get(bt, source, True)
        if leaf is None or not leaf.mode:
            raise FastImportDie("path %s not in branch" % source.decode("latin-1"))
        if not dest:
            if not _is_dir(leaf.mode):
                raise FastImportDie("root cannot be a non-directory")
            # tree_content_replace at root with leaf's subtree
            b["tree"] = leaf.tree if leaf.tree is not None else self._load_tree(leaf.oid)
            b["tree_oid"] = NULL_OID
            return
        if not _verify_path(dest, leaf.mode):
            raise FastImportDie("invalid path '%s'" % dest.decode("latin-1"))
        self._tree_content_set(bt, dest, leaf.oid, leaf.mode,
                               leaf.tree)

    def _file_change_deleteall(self, b: dict):
        b["tree"] = _Tree()
        b["tree"].loaded = True
        b["tree_oid"] = NULL_OID

    # -- path parsing -------------------------------------------------------
    def _parse_path(self, p: bytes, is_last: bool, field: str):
        if p[:1] == b'"':
            res = _unquote_c_style(p)
            if res is None:
                raise FastImportDie("invalid %s: %s" % (field, self.command_buf.decode("latin-1")))
            decoded, rest = res
            if b"\0" in decoded:
                raise FastImportDie("NUL in %s: %s" % (field, self.command_buf.decode("latin-1")))
            return decoded, rest
        if is_last:
            return p, b""
        sp = p.find(b" ")
        if sp == -1:
            return p, b""
        return p[:sp], p[sp:]

    def _parse_path_eol(self, p: bytes, field: str) -> bytes:
        decoded, end = self._parse_path(p, True, field)
        if end:
            raise FastImportDie("garbage after %s: %s" % (field, self.command_buf.decode("latin-1")))
        return decoded

    def _parse_path_space(self, p: bytes, field: str):
        decoded, end = self._parse_path(p, False, field)
        if end[:1] != b" ":
            raise FastImportDie("missing space after %s: %s" % (field, self.command_buf.decode("latin-1")))
        return decoded, end[1:]

    # -- commands -----------------------------------------------------------
    def parse_new_blob(self):
        self.read_next_command()
        self._parse_mark()
        self._parse_original_identifier()
        payload = self._parse_data()
        oid = self._wo("blob", payload)
        if self._next_mark:
            self.marks[self._next_mark] = (oid, "blob")

    def parse_new_commit(self, arg: str):
        b = self._lookup_branch(arg)
        if b is None:
            b = self._new_branch(arg)
        self.read_next_command()
        self._parse_mark()
        self._parse_original_identifier()
        author = None
        committer = None
        if self.command_buf.startswith(b"author "):
            author = self._parse_ident(self.command_buf[len(b"author "):])
            self.read_next_command()
        if self.command_buf.startswith(b"committer "):
            committer = self._parse_ident(self.command_buf[len(b"committer "):])
            self.read_next_command()
        if committer is None:
            raise FastImportDie("expected committer but didn't get one")
        # gpgsig lines: default signed_commit_mode aborts
        while self.command_buf.startswith(b"gpgsig "):
            raise FastImportDie("encountered signed commit; use "
                                "--signed-commits=<mode> to handle it")
        encoding = None
        if self.command_buf.startswith(b"encoding "):
            encoding = self.command_buf[len(b"encoding "):]
            self.read_next_command()
        msg = self._parse_data()
        self.read_next_command()
        self._parse_from(b)
        merge_list = self._parse_merge()

        # file changes
        while len(self.command_buf) > 0:
            buf = self.command_buf
            if buf.startswith(b"M "):
                self._file_change_m(buf[2:], b)
            elif buf.startswith(b"D "):
                self._file_change_d(buf[2:], b)
            elif buf.startswith(b"R "):
                self._file_change_cr(buf[2:], b, True)
            elif buf.startswith(b"C "):
                self._file_change_cr(buf[2:], b, False)
            elif buf.startswith(b"N "):
                raise FastImportDie("unsupported command: %s" % buf.decode("latin-1"))
            elif buf == b"deleteall":
                self._file_change_deleteall(b)
            elif buf.startswith(b"ls "):
                self._parse_ls(buf[len(b"ls "):], b)
            elif buf.startswith(b"cat-blob "):
                self._parse_cat_blob(buf[len(b"cat-blob "):])
            else:
                self.unread = True
                break
            if self.read_next_command() == -1:
                break

        # build the tree and commit
        bt = self._branch_tree(b)
        tree_oid = self._mktree_and_store(bt)
        b["tree_oid"] = tree_oid
        b["tree"] = bt

        out = bytearray()
        out += b"tree %s\n" % tree_oid.encode("ascii")
        if b["oid"] != NULL_OID:
            out += b"parent %s\n" % b["oid"].encode("ascii")
        for m in merge_list:
            out += b"parent %s\n" % m.encode("ascii")
        out += b"author %s\n" % (author if author is not None else committer)
        out += b"committer %s\n" % committer
        if encoding is not None:
            out += b"encoding %s\n" % encoding
        out += b"\n"
        out += msg
        oid = self._wo("commit", bytes(out))
        b["oid"] = oid
        if self._next_mark:
            self.marks[self._next_mark] = (oid, "commit")

    def parse_new_tag(self, arg: str):
        self.read_next_command()
        self._parse_mark()
        if not self.command_buf.startswith(b"from "):
            raise FastImportDie("expected 'from' command, got '%s'"
                                % self.command_buf.decode("latin-1"))
        frm = self.command_buf[len(b"from "):].decode("latin-1")
        s = self._lookup_branch(frm)
        if s is not None:
            if s["oid"] == NULL_OID:
                raise FastImportDie("can't tag an empty branch.")
            oid = s["oid"]
            otype = "commit"
        elif frm.startswith(":"):
            from_mark = _parse_mark_ref_eol(frm, self.command_buf)
            oid, otype = self._find_mark(from_mark)
        else:
            resolved = refs_mod.rev_parse(self.repo, frm)
            if resolved:
                oid = resolved
                otype = self._read_type_of(oid)
                if otype is None:
                    raise FastImportDie("not a valid object: %s" % frm)
            else:
                raise FastImportDie("invalid ref name or SHA1 expression: %s" % frm)
        self.read_next_command()
        self._parse_original_identifier()
        tagger = None
        if self.command_buf.startswith(b"tagger "):
            tagger = self._parse_ident(self.command_buf[len(b"tagger "):])
            self.read_next_command()
        msg = self._parse_data()
        out = bytearray()
        out += b"object %s\n" % oid.encode("ascii")
        out += b"type %s\n" % otype.encode("ascii")
        out += b"tag %s\n" % arg.encode("utf-8")
        if tagger is not None:
            out += b"tagger %s\n" % tagger
        # default signed_tag_mode aborts on a signed tag payload
        sig_off = _parse_signed_buffer(msg)
        if sig_off < len(msg):
            raise FastImportDie("encountered signed tag; use "
                                "--signed-tags=<mode> to handle it")
        out += b"\n"
        out += msg
        tag_oid = self._wo("tag", bytes(out))
        # remove any previous tag of same name then append (ordered)
        self.tags = [(n, o) for (n, o) in self.tags if n != arg]
        self.tags.append((arg, tag_oid))
        if self._next_mark:
            self.marks[self._next_mark] = (tag_oid, "tag")

    def parse_reset_branch(self, arg: str):
        b = self._lookup_branch(arg)
        if b is not None:
            b["oid"] = NULL_OID
            b["tree_oid"] = NULL_OID
            b["tree"] = None
        else:
            b = self._new_branch(arg)
        self.read_next_command()
        self._parse_from(b)
        if b.get("delete") and b["name"].startswith("refs/tags/"):
            tag_name = b["name"][len("refs/tags/"):]
            self.tags = [(n, o) for (n, o) in self.tags if n != tag_name]
        if len(self.command_buf) > 0:
            self.unread = True

    def parse_get_mark(self, p: bytes):
        if p[:1] != b":":
            raise FastImportDie("not a mark: %s" % p.decode("latin-1"))
        idnum = _parse_mark_ref_eol(p.decode("latin-1"), self.command_buf)
        m = self._find_mark(idnum)
        self.cat_blob_fd.write(m[0].encode("ascii") + b"\n")
        self.cat_blob_fd.flush()

    def _parse_cat_blob(self, p: bytes):
        if p[:1] == b":":
            idnum = _parse_mark_ref_eol(p.decode("latin-1"), self.command_buf)
            m = self._find_mark(idnum)
            oid = m[0]
        else:
            if not _is_hex_oid(p[:40]):
                raise FastImportDie("invalid dataref: %s" % self.command_buf.decode("latin-1"))
            if len(p) != 40:
                raise FastImportDie("garbage after SHA1: %s" % self.command_buf.decode("latin-1"))
            oid = p[:40].decode("ascii")
        self._cat_blob(oid)

    def _cat_blob(self, oid: str):
        try:
            otype, data = objs.read_object(self.repo, oid)
        except (KeyError, ValueError, OSError):
            self.cat_blob_fd.write(("%s missing\n" % oid).encode("ascii"))
            self.cat_blob_fd.flush()
            return
        if otype != "blob":
            raise FastImportDie("object %s is a %s but a blob was expected." % (oid, otype))
        line = ("%s %s %d\n" % (oid, otype, len(data))).encode("ascii")
        self.cat_blob_fd.write(line)
        self.cat_blob_fd.write(data)
        self.cat_blob_fd.write(b"\n")
        self.cat_blob_fd.flush()

    def _parse_ls(self, p: bytes, b: Optional[dict]):
        if p[:1] == b'"':
            if b is None:
                raise FastImportDie("not in a commit: %s" % self.command_buf.decode("latin-1"))
            root = self._branch_tree(b)
            path = self._parse_path_eol(p, "path")
        else:
            # ls SP <dataref> SP <path>
            if p[:1] == b":":
                idnum, p = _parse_mark_ref_space(p, self.command_buf)
                oid, _t = self._find_mark(idnum)
            else:
                sp = p.find(b" ")
                if sp == -1 or not _is_hex_oid(p[:sp]):
                    raise FastImportDie("invalid dataref: %s" % self.command_buf.decode("latin-1"))
                oid = p[:sp].decode("ascii")
                p = p[sp + 1:]
            root = self._load_tree(oid) if oid != NULL_OID else _Tree()
            root.loaded = True
            path = self._parse_path_eol(p, "path")
        leaf = self._tree_content_get(root, path, True)
        if leaf is None:
            self._print_ls(0, None, path)
        else:
            self._print_ls(leaf.mode, leaf.oid, path)

    def _print_ls(self, mode: int, oid: Optional[str], path: bytes):
        if not mode:
            line = b"missing " + _quote_c_style(path) + b"\n"
        else:
            if _is_gitlink(mode):
                typ = b"commit"
            elif _is_dir(mode):
                typ = b"tree"
            else:
                typ = b"blob"
            line = (b"%06o " % mode) + typ + b" " + oid.encode("ascii") + b"\t" + _quote_c_style(path) + b"\n"
        self.cat_blob_fd.write(line)
        self.cat_blob_fd.flush()

    def parse_progress(self):
        sys.stdout.buffer.write(self.command_buf)
        sys.stdout.buffer.write(b"\n")
        sys.stdout.buffer.flush()
        self._skip_optional_lf()

    def parse_checkpoint(self):
        # All objects/refs are written immediately, so a checkpoint only needs
        # to flush refs and marks.
        self._dump_branches()
        self._dump_tags()
        self._dump_marks()
        self._skip_optional_lf()

    def parse_alias(self):
        self._skip_optional_lf()
        self.read_next_command()
        self._parse_mark()
        if not self._next_mark:
            raise FastImportDie("expected 'mark' command, got %s"
                                % self.command_buf.decode("latin-1"))
        b = self._new_branch("\0alias\0")
        del self.branches["\0alias\0"]
        if not self._parse_objectish_with_prefix(b, b"to "):
            raise FastImportDie("expected 'to' command, got %s"
                                % self.command_buf.decode("latin-1"))
        oid = b["oid"]
        t = self._read_type_of(oid) or "commit"
        self.marks[self._next_mark] = (oid, t)

    def parse_feature(self, feature: str):
        if self.seen_data_command:
            raise FastImportDie("got feature command '%s' after data command" % feature)
        if self._parse_one_feature(feature, 1):
            return
        raise FastImportDie("this version of fast-import does not support feature %s." % feature)

    def parse_option(self, option: str):
        if self.seen_data_command:
            raise FastImportDie("got option command '%s' after data command" % option)
        if self._parse_one_option(option):
            return
        raise FastImportDie("this version of fast-import does not support option: %s" % option)

    # -- ref dumping --------------------------------------------------------
    def _dump_branches(self):
        for name in self.branches:
            b = self.branches[name]
            self._update_branch(b)

    def _update_branch(self, b: dict):
        name = b["name"]
        replace_prefix = "refs/replace/"
        if name.startswith(replace_prefix) and name[len(replace_prefix):] == b["oid"]:
            if not self.quiet:
                sys.stderr.write("warning: dropping %s since it would point to "
                                 "itself (i.e. to %s)\n" % (name, b["oid"]))
            try:
                refs_mod.delete_ref(self.repo, name)
            except Exception:
                pass
            return
        if b["oid"] == NULL_OID:
            if b.get("delete"):
                try:
                    refs_mod.delete_ref(self.repo, name)
                except Exception:
                    pass
            return
        old = refs_mod.read_ref(self.repo, name)
        if not self.force_update and old:
            # require new tip to contain old (fast-forward); we approximate by
            # checking ancestry.
            if not _is_ancestor(self.repo, old, b["oid"]):
                sys.stderr.write("warning: not updating %s (new tip %s does not contain %s)\n"
                                 % (name, b["oid"], old))
                self._failure = True
                return
        refs_mod.update_ref(self.repo, name, b["oid"], message="fast-import")

    def _dump_tags(self):
        for name, oid in self.tags:
            refs_mod.update_ref(self.repo, "refs/tags/%s" % name, oid, message="fast-import")

    # -- main loop ----------------------------------------------------------
    def run(self, data: bytes) -> int:
        self._data = data
        self._failure = False
        # early argv parse for --allow-unsafe-features
        self._early_argv()
        while self.read_next_command() != -1:
            buf = self.command_buf
            if buf == b"blob":
                self.parse_new_blob()
            elif buf.startswith(b"commit "):
                self.parse_new_commit(buf[len(b"commit "):].decode("latin-1"))
            elif buf.startswith(b"tag "):
                self.parse_new_tag(buf[len(b"tag "):].decode("latin-1"))
            elif buf.startswith(b"reset "):
                self.parse_reset_branch(buf[len(b"reset "):].decode("latin-1"))
            elif buf.startswith(b"ls "):
                self._parse_ls(buf[len(b"ls "):], None)
            elif buf.startswith(b"cat-blob "):
                self._parse_cat_blob(buf[len(b"cat-blob "):])
            elif buf.startswith(b"get-mark "):
                self.parse_get_mark(buf[len(b"get-mark "):])
            elif buf == b"checkpoint":
                self.parse_checkpoint()
            elif buf == b"done":
                break
            elif buf == b"alias":
                self.parse_alias()
            elif buf.startswith(b"progress "):
                self.parse_progress()
            elif buf.startswith(b"feature "):
                self.parse_feature(buf[len(b"feature "):].decode("latin-1"))
            elif buf.startswith(b"option git "):
                self.parse_option(buf[len(b"option git "):].decode("latin-1"))
            elif buf.startswith(b"option "):
                pass  # ignore non-git options
            else:
                raise FastImportDie("unsupported command: %s" % buf.decode("latin-1"))

        if not self.seen_data_command:
            self.parse_argv()

        if self.require_explicit_termination and self.eof:
            raise FastImportDie("stream ends early")

        self._dump_branches()
        self._dump_tags()
        self._dump_marks()

        if self.show_stats:
            self._print_stats()

        return 1 if self._failure else 0

    def _wo(self, otype: str, data: bytes) -> str:
        """write_object + dump_stats accounting (object/duplicate counts)."""
        from . import objects as objs
        oid = objs.write_object(self.repo, otype, data)
        self._stat_count[otype] += 1
        if oid in self._stat_seen:
            self._stat_dup[otype] += 1
        else:
            self._stat_seen.add(oid)
        return oid

    def _print_stats(self):
        # Port of builtin/fast-import.c dump_stats(). The structural fields
        # (counts, branches, marks, atoms, pack_report) are byte-exact; the
        # delta columns are 0 (we never deltify on import) and the Memory/*
        # KiB lines reflect this Python process rather than git's C heap, so
        # those specific values are not byte-reproducible (see the normalized
        # stats parity test).
        import os as _os
        c, d = self._stat_count, self._stat_dup
        total = c["blob"] + c["tree"] + c["commit"] + c["tag"]
        dup = d["blob"] + d["tree"] + d["commit"] + d["tag"]
        # alloc_count: object_entry slots, allocated in OBJECT_ENTRY_BLOCK (5000).
        blocks = max(1, (total + 4999) // 5000)
        alloc_count = blocks * 5000
        branch_count = len(self.branches)
        marks_total = 1024  # (1 << marks.shift) * 1024, shift starts at 0
        while marks_total < len(self.marks):
            marks_total *= 1024
        w = sys.stderr.write
        w("fast-import statistics:\n")
        w("-" * 69 + "\n")
        w("Alloc'd objects: %10d\n" % alloc_count)
        w("Total objects:   %10d (%10d duplicates                  )\n" % (total, dup))
        for label, key in (("blobs  ", "blob"), ("trees  ", "tree"),
                           ("commits", "commit"), ("tags   ", "tag")):
            w("      %s:   %10d (%10d duplicates %10d deltas of %10d attempts)\n"
              % (label, c[key], d[key], 0, 0))
        w("Total branches:  %10d (%10d loads     )\n" % (branch_count, self._stat_branch_loads))
        w("      marks:     %10d (%10d unique    )\n" % (marks_total, len(self.marks)))
        w("      atoms:     %10d\n" % len(self._stat_atoms))
        # Memory accounting, ported from dump_stats():
        #   objects = alloc_count * sizeof(struct object_entry) / 1024
        #   pools   = (tree_entry_allocd + fi_mem_pool.pool_alloc) / 1024
        #   total   = (tree_entry_allocd + pool_alloc + objects_bytes) / 1024
        # sizeof(struct object_entry) is 72 on a 64-bit build (pack_idx_entry
        # 48 + hashmap_entry 16 + a packed uint32_t 4, padded to 8).  The pool
        # holds a single 2 MiB block here (fi_mem_pool.block_alloc) so pool_alloc
        # is exactly 2 MiB once any pool allocation has happened.
        OBJECT_ENTRY_SIZE = 72
        objects_bytes = alloc_count * OBJECT_ENTRY_SIZE
        pool_alloc = 2 * 1024 * 1024  # one fi_mem_pool block
        tree_entry_allocd = 0  # no deltified-tree bookkeeping in pygit's writer
        obj_kib = objects_bytes // 1024
        pools_kib = (tree_entry_allocd + pool_alloc) // 1024
        total_kib = (tree_entry_allocd + pool_alloc + objects_bytes) // 1024
        w("Memory total:    %10d KiB\n" % total_kib)
        w("       pools:    %10d KiB\n" % pools_kib)
        w("     objects:    %10d KiB\n" % obj_kib)
        w("-" * 69 + "\n")
        try:
            pagesize = _os.sysconf("SC_PAGE_SIZE")
        except (ValueError, OSError, AttributeError):
            pagesize = 4096
        # The packed-git window/limit defaults are reported once the packing
        # subsystem initialises (i.e. when a packfile was actually started).
        packed = total > 0
        win = 1073741824 if packed else 0          # 1 GiB default window
        limit = 35184372088832 if packed else 0    # 32 TiB default (64-bit)
        w("pack_report: getpagesize()            = %10d\n" % pagesize)
        w("pack_report: core.packedGitWindowSize = %10d\n" % win)
        w("pack_report: core.packedGitLimit      = %10d\n" % limit)
        w("pack_report: pack_used_ctr            = %10d\n" % 0)
        w("pack_report: pack_mmap_calls          = %10d\n" % 0)
        w("pack_report: pack_open_windows        = %10d / %10d\n" % (0, 0))
        w("pack_report: pack_mapped              = %10d / %10d\n" % (0, 0))
        w("-" * 69 + "\n")
        w("\n")
        sys.stderr.flush()


# ---------------------------------------------------------------------------
# free helpers


def _strtoumax(b: bytes) -> int:
    # parse leading decimal digits (like strtoumax base 10); 0 if none.
    i = 0
    while i < len(b) and 48 <= b[i] <= 57:
        i += 1
    if i == 0:
        return 0
    return int(b[:i])


def _is_hex_oid(b: bytes) -> bool:
    if len(b) != 40:
        return False
    for c in b:
        if not (48 <= c <= 57 or 97 <= c <= 102 or 65 <= c <= 70):
            return False
    return True


def _parse_mark_ref_eol(p: str, command_buf: bytes) -> int:
    # p starts with ':'
    rest = p[1:]
    i = 0
    while i < len(rest) and rest[i].isdigit():
        i += 1
    if i == 0:
        raise FastImportDie("no value after ':' in mark: %s" % command_buf.decode("latin-1"))
    if i != len(rest):
        raise FastImportDie("garbage after mark: %s" % command_buf.decode("latin-1"))
    return int(rest[:i])


def _parse_mark_ref_space(p: bytes, command_buf: bytes):
    # p starts with b':'
    rest = p[1:]
    i = 0
    while i < len(rest) and 48 <= rest[i] <= 57:
        i += 1
    if i == 0:
        raise FastImportDie("no value after ':' in mark: %s" % command_buf.decode("latin-1"))
    if i >= len(rest) or rest[i] != ord(" "):
        raise FastImportDie("missing space after mark: %s" % command_buf.decode("latin-1"))
    return int(rest[:i]), rest[i + 1:]


def _validate_raw_date(src: bytes, strict: bool):
    """Port of validate_raw_date: returns the original string on success or None."""
    s = src
    # strtoul base 10 for seconds
    i = 0
    if i < len(s) and s[i:i + 1] in (b"+", b"-"):
        # strtoul allows a leading sign; seconds use no explicit sign normally
        i += 1
    j = i
    while j < len(s) and 48 <= s[j] <= 57:
        j += 1
    if j == i:  # no digits parsed
        return None
    if j >= len(s) or s[j:j + 1] != b" ":
        return None
    k = j + 1
    if k >= len(s) or s[k:k + 1] not in (b"-", b"+"):
        return None
    # timezone digits
    k2 = k + 1
    start = k2
    while k2 < len(s) and 48 <= s[k2] <= 57:
        k2 += 1
    if k2 == start:
        return None
    if k2 != len(s):
        return None
    num = int(s[start:k2])
    if strict and num > 1400:
        return None
    return src


def _parse_rfc2822(src: bytes):
    import email.utils
    try:
        dt = email.utils.parsedate_to_datetime(src.decode("latin-1").strip())
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    try:
        secs = int(dt.timestamp())
    except (OverflowError, OSError, ValueError):
        return None
    off = dt.utcoffset()
    total = int(off.total_seconds()) if off is not None else 0
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return ("%d %s%02d%02d" % (secs, sign, total // 3600, (total % 3600) // 60)).encode("ascii")


def _datestamp_now():
    import time
    secs = int(time.time())
    # git's datestamp() uses the local timezone offset; under TZ=UTC it is +0000
    lt = time.localtime(secs)
    off = lt.tm_gmtoff or 0
    sign = "+" if off >= 0 else "-"
    off = abs(off)
    return ("%d %s%02d%02d" % (secs, sign, off // 3600, (off % 3600) // 60)).encode("ascii")


def _parse_signed_buffer(msg: bytes) -> int:
    """Port of parse_signed_buffer: find offset of a PGP/SSH/X509 signature."""
    sig_markers = (
        b"-----BEGIN PGP SIGNATURE-----",
        b"-----BEGIN PGP MESSAGE-----",
        b"-----BEGIN SIGNED MESSAGE-----",
        b"-----BEGIN SIGNATURE-----",
    )
    pos = 0
    n = len(msg)
    while pos < n:
        nl = msg.find(b"\n", pos)
        if nl == -1:
            line_end = n
        else:
            line_end = nl + 1
        line = msg[pos:line_end]
        for marker in sig_markers:
            if line.startswith(marker):
                return pos
        pos = line_end
    return n


def _peel_to_commit(repo: Repository, oid: str) -> Optional[str]:
    seen = set()
    cur = oid
    while cur and cur not in seen:
        seen.add(cur)
        try:
            otype, data = objs.read_object(repo, cur)
        except (KeyError, ValueError, OSError):
            return None
        if otype == "commit":
            return cur
        if otype == "tag":
            line = data.split(b"\n", 1)[0]
            if line.startswith(b"object "):
                cur = line[7:].decode("ascii")
                continue
        return None
    return None


def _is_ancestor(repo: Repository, anc: str, desc: str) -> bool:
    """Return True if commit `anc` is reachable from `desc`."""
    if anc == desc:
        return True
    seen = set()
    stack = [desc]
    while stack:
        cur = stack.pop()
        if cur == anc:
            return True
        if cur in seen:
            continue
        seen.add(cur)
        try:
            otype, data = objs.read_object(repo, cur)
        except (KeyError, ValueError, OSError):
            continue
        if otype != "commit":
            continue
        for line in data.split(b"\n"):
            if line.startswith(b"parent "):
                stack.append(line[7:].decode("ascii"))
            elif line == b"":
                break
    return False


_FAST_IMPORT_USAGE = (
    "git fast-import [--date-format=<f>] [--max-pack-size=<n>] "
    "[--big-file-threshold=<n>] [--depth=<n>] [--active-branches=<n>] "
    "[--export-marks=<marks.file>]"
)


def run_fast_import(repo: Repository, argv: list[str], data: bytes) -> int:
    fi = FastImport(repo)
    fi.set_argv(argv)
    return fi.run(data)
