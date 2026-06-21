"""Git's content conversion (the convert_to_git / "clean" path).

This implements the conversions git applies when storing a blob, in the exact
order convert.c's ``convert_to_git()`` runs them:

  1. ``apply_filter`` (external ``filter.<drv>.clean`` / ``filter.<drv>.process``)
  2. ``encode_to_git`` (``working-tree-encoding`` re-encoding to UTF-8)
  3. ``crlf_to_git`` (the ``text``/``eol``/``crlf`` attributes + ``core.autocrlf``)
  4. ``ident_to_git`` (the ``ident`` attribute, ``$Id: <sha> $`` -> ``$Id$``)

It mirrors convert.c closely enough to be byte-exact for ``git hash-object``
(which never reads the index, so the ``has_crlf_in_index`` short-circuit never
fires there).
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Optional

from .repo import Repository

# crlf_action values (mirror enum convert_crlf_action in convert.h)
CRLF_UNDEFINED = "undefined"
CRLF_BINARY = "binary"
CRLF_TEXT = "text"
CRLF_TEXT_INPUT = "text_input"
CRLF_TEXT_CRLF = "text_crlf"
CRLF_AUTO = "auto"
CRLF_AUTO_INPUT = "auto_input"
CRLF_AUTO_CRLF = "auto_crlf"

# eol attribute values
EOL_UNSET = "unset"
EOL_LF = "lf"
EOL_CRLF = "crlf"

# autocrlf config values
AUTO_CRLF_FALSE = "false"
AUTO_CRLF_TRUE = "true"
AUTO_CRLF_INPUT = "input"


class ConvertError(Exception):
    """Retained for callers that still distinguish a conversion they cannot
    perform.  The clean-filter and working-tree-encoding paths no longer raise
    it (they are implemented), but it remains the base of ``ConvertDie``."""


class ConvertDie(ConvertError):
    """Raised when convert_to_git must ``die()`` with a ``fatal:`` message.

    Carries the exact message text (without the ``fatal: `` prefix).  This is
    the porting of every ``die(...)`` reachable from ``convert_to_git`` for the
    clean-filter and working-tree-encoding paths.  ``advice`` holds any
    ``hint:`` line that git emits *before* the fatal message (BOM advice)."""

    def __init__(self, message: str, advice: Optional[str] = None):
        super().__init__(message)
        self.advice = advice


class ConvertWarn(Exception):
    """Carries the ``error:``/``hint:`` lines git prints when a conversion
    fails but is allowed to fall through (no ``-w``, or a non-required filter).

    ``messages`` is the ordered list of full lines (already including their
    ``error: ``/``hint: ``/``warning: `` prefix)."""

    def __init__(self, messages: list[str]):
        super().__init__("; ".join(messages))
        self.messages = messages


class SafeCrlfDie(Exception):
    """Raised when core.safecrlf=true and the round-trip would lose EOLs.

    Carries git's exact ``fatal:`` message text (without the prefix)."""


# ---------------------------------------------------------------------------
# gitattributes resolution (subset: text/eol/crlf/filter/working-tree-encoding)


class _AttrRule:
    __slots__ = ("nodir", "regex", "attrs")

    def __init__(self, pattern: str, attrs: dict):
        s = pattern
        # Leading "/" anchors at the attributes-file directory (root here).
        if s.startswith("/"):
            s = s[1:]
            nodir = False
        else:
            nodir = "/" not in s.rstrip("/")
        s = s.rstrip("/")
        from .ignore import _wildmatch_to_regex

        self.nodir = nodir
        self.regex = re.compile("^" + _wildmatch_to_regex(s) + "$")
        self.attrs = attrs

    def matches(self, path: str) -> bool:
        if self.nodir:
            base = path.rsplit("/", 1)[-1]
            return self.regex.match(base) is not None
        return self.regex.match(path) is not None


def _parse_attr_tokens(tokens: list[str]) -> dict:
    attrs: dict[str, object] = {}
    for tok in tokens:
        if tok.startswith("-"):
            attrs[tok[1:]] = False
        elif tok.startswith("!"):
            attrs[tok[1:]] = None  # unspecified / unset to UNKNOWN
        elif "=" in tok:
            k, _, v = tok.partition("=")
            attrs[k] = v
        else:
            attrs[tok] = True
    return attrs


def _load_attr_rules(repo: Optional[Repository]) -> list[_AttrRule]:
    """Parse the top-level .gitattributes plus $GIT_DIR/info/attributes.

    Returns rules in file order; later matches win (callers scan in reverse).
    """
    rules: list[_AttrRule] = []
    files: list[Path] = []
    if repo is not None:
        # info/attributes is consulted last in git's stack (highest priority);
        # .gitattributes at the worktree root is lower.  We append info last so
        # a reverse scan finds it first.
        ga = repo.path / ".gitattributes"
        if ga.is_file():
            files.append(ga)
        info = repo.gitdir / "info" / "attributes"
        if info.is_file():
            files.append(info)
    for f in files:
        try:
            text = f.read_bytes().decode("utf-8", "replace")
        except OSError:
            continue
        for line in text.splitlines():
            stripped = line.lstrip(" \t")
            if not stripped or stripped.startswith("#"):
                continue
            parts = stripped.split()
            pattern = parts[0]
            if pattern.startswith("!"):
                # Negative patterns are ignored in gitattributes.
                continue
            try:
                rules.append(_AttrRule(pattern, _parse_attr_tokens(parts[1:])))
            except re.error:
                continue
    return rules


def _check_attrs(repo: Optional[Repository], path: str) -> dict:
    """Resolve the conversion-relevant attributes for ``path``.

    Mirrors git's fill(): scan rules in reverse, first to set an attr wins.
    Returns a dict with keys among text/eol/crlf/filter/working-tree-encoding.
    """
    wanted = ("crlf", "ident", "filter", "eol", "text", "working-tree-encoding")
    out: dict[str, object] = {}
    rules = _load_attr_rules(repo)
    for rule in reversed(rules):
        if all(k in out for k in wanted):
            break
        if not rule.matches(path):
            continue
        for k, v in rule.attrs.items():
            if k in wanted and k not in out:
                out[k] = v
    return out


# ---------------------------------------------------------------------------
# config


def _autocrlf(repo: Optional[Repository]) -> str:
    val = _config_get(repo, "core.autocrlf")
    if val is None:
        return AUTO_CRLF_FALSE
    low = val.strip().lower()
    if low == "input":
        return AUTO_CRLF_INPUT
    if low in ("true", "yes", "on", "1"):
        return AUTO_CRLF_TRUE
    return AUTO_CRLF_FALSE


def _core_eol(repo: Optional[Repository]) -> str:
    val = _config_get(repo, "core.eol")
    if val is None:
        return "unset"
    low = val.strip().lower()
    if low == "crlf":
        return "crlf"
    if low == "lf":
        return "lf"
    return "unset"


def _safecrlf(repo: Optional[Repository]) -> str:
    """Return 'warn', 'die', or 'false' for core.safecrlf (default 'warn')."""
    val = _config_get(repo, "core.safecrlf")
    if val is None:
        return "warn"
    low = val.strip().lower()
    if low == "warn":
        return "warn"
    if low in ("true", "yes", "on", "1"):
        return "die"
    return "false"


def _config_get(repo: Optional[Repository], key: str) -> Optional[str]:
    try:
        from . import gitconfig

        return gitconfig.get(repo, key)
    except Exception:
        return None


# native EOL on the platforms we target is LF (POSIX); git's EOL_NATIVE.
_EOL_NATIVE = EOL_LF


def _text_eol_is_crlf(repo: Optional[Repository]) -> bool:
    ac = _autocrlf(repo)
    if ac == AUTO_CRLF_TRUE:
        return True
    if ac == AUTO_CRLF_INPUT:
        return False
    ce = _core_eol(repo)
    if ce == "crlf":
        return True
    if ce == "unset" and _EOL_NATIVE == EOL_CRLF:
        return True
    return False


def _output_eol(crlf_action: str, repo: Optional[Repository]) -> str:
    if crlf_action == CRLF_BINARY:
        return EOL_UNSET
    if crlf_action == CRLF_TEXT_CRLF:
        return EOL_CRLF
    if crlf_action == CRLF_TEXT_INPUT:
        return EOL_LF
    if crlf_action in (CRLF_UNDEFINED, CRLF_AUTO_CRLF):
        return EOL_CRLF
    if crlf_action == CRLF_AUTO_INPUT:
        return EOL_LF
    if crlf_action in (CRLF_TEXT, CRLF_AUTO):
        return EOL_CRLF if _text_eol_is_crlf(repo) else EOL_LF
    return EOL_UNSET


# ---------------------------------------------------------------------------
# text statistics (gather_stats / convert_is_binary)


class _Stat:
    __slots__ = ("nul", "lonecr", "lonelf", "crlf", "printable", "nonprintable")

    def __init__(self) -> None:
        self.nul = self.lonecr = self.lonelf = self.crlf = 0
        self.printable = self.nonprintable = 0


def _gather_stats(buf: bytes) -> _Stat:
    st = _Stat()
    size = len(buf)
    i = 0
    while i < size:
        c = buf[i]
        if c == 0x0D:  # '\r'
            if i + 1 < size and buf[i + 1] == 0x0A:
                st.crlf += 1
                i += 1
            else:
                st.lonecr += 1
            i += 1
            continue
        if c == 0x0A:  # '\n'
            st.lonelf += 1
            i += 1
            continue
        if c == 127:
            st.nonprintable += 1
        elif c < 32:
            if c in (0x08, 0x09, 0x1B, 0x0C):  # BS, HT, ESC, FF
                st.printable += 1
            else:
                if c == 0:
                    st.nul += 1
                st.nonprintable += 1
        else:
            st.printable += 1
        i += 1
    if size >= 1 and buf[size - 1] == 0x1A:  # trailing EOF (^Z)
        st.nonprintable -= 1
    return st


def _is_binary(st: _Stat) -> bool:
    if st.lonecr:
        return True
    if st.nul:
        return True
    if (st.printable >> 7) < st.nonprintable:
        return True
    return False


def _will_convert_lf_to_crlf(st: _Stat, crlf_action: str, repo) -> bool:
    if _output_eol(crlf_action, repo) != EOL_CRLF:
        return False
    if not st.lonelf:
        return False
    if crlf_action in (CRLF_AUTO, CRLF_AUTO_INPUT, CRLF_AUTO_CRLF):
        if st.lonecr or st.crlf:
            return False
        if _is_binary(st):
            return False
    return True


# ---------------------------------------------------------------------------
# attribute -> crlf_action resolution (convert_attrs)


def _resolve_crlf_action(repo: Optional[Repository], attrs: dict) -> str:
    # git_path_check_crlf on the "text" attr, then fall back to "crlf".
    def check_crlf(value):
        if value is True:
            return CRLF_TEXT
        if value is False:
            return CRLF_BINARY
        if value is None:  # unset/unspecified -> UNDEFINED
            return CRLF_UNDEFINED
        if value == "input":
            return CRLF_TEXT_INPUT
        if value == "auto":
            return CRLF_AUTO
        return CRLF_UNDEFINED

    text_val = attrs.get("text", None)
    crlf_val = attrs.get("crlf", None)
    action = check_crlf(text_val)
    if action == CRLF_UNDEFINED:
        action = check_crlf(crlf_val)

    if action != CRLF_BINARY:
        eol_val = attrs.get("eol", None)
        eol_attr = EOL_UNSET
        if eol_val == "lf":
            eol_attr = EOL_LF
        elif eol_val == "crlf":
            eol_attr = EOL_CRLF
        if action == CRLF_AUTO and eol_attr == EOL_LF:
            action = CRLF_AUTO_INPUT
        elif action == CRLF_AUTO and eol_attr == EOL_CRLF:
            action = CRLF_AUTO_CRLF
        elif eol_attr == EOL_LF:
            action = CRLF_TEXT_INPUT
        elif eol_attr == EOL_CRLF:
            action = CRLF_TEXT_CRLF

    if action == CRLF_TEXT:
        action = CRLF_TEXT_CRLF if _text_eol_is_crlf(repo) else CRLF_TEXT_INPUT

    ac = _autocrlf(repo)
    if action == CRLF_UNDEFINED and ac == AUTO_CRLF_FALSE:
        action = CRLF_BINARY
    if action == CRLF_UNDEFINED and ac == AUTO_CRLF_TRUE:
        action = CRLF_AUTO_CRLF
    if action == CRLF_UNDEFINED and ac == AUTO_CRLF_INPUT:
        action = CRLF_AUTO_INPUT
    return action


# ---------------------------------------------------------------------------
# crlf_to_git


def _crlf_to_git(repo, path, src: bytes, crlf_action: str, conv_flags: dict):
    """Return (converted_bytes, warning_or_None).

    conv_flags carries safecrlf ('warn'/'die'/'false').  The index check is
    omitted: hash-object never loads the index, so has_crlf_in_index() is 0.
    """
    if crlf_action == CRLF_BINARY or (src is not None and not len(src)):
        return src, None

    stats = _gather_stats(src)
    convert_crlf_into_lf = bool(stats.crlf)

    if crlf_action in (CRLF_AUTO, CRLF_AUTO_INPUT, CRLF_AUTO_CRLF):
        if _is_binary(stats):
            return src, None
        # has_crlf_in_index() is always 0 for hash-object (no index loaded).

    warning = None
    safe = conv_flags.get("safecrlf", "false")
    rndtrp_warn = safe == "warn"
    rndtrp_die = safe == "die"
    if rndtrp_warn or (rndtrp_die and len(src)):
        new_crlf = stats.crlf
        new_lonelf = stats.lonelf
        if convert_crlf_into_lf:
            new_lonelf += new_crlf
            new_crlf = 0
        # simulate checkout
        tmp = _Stat()
        tmp.nul = stats.nul
        tmp.lonecr = stats.lonecr
        tmp.lonelf = new_lonelf
        tmp.crlf = new_crlf
        tmp.printable = stats.printable
        tmp.nonprintable = stats.nonprintable
        if _will_convert_lf_to_crlf(tmp, crlf_action, repo):
            new_crlf += new_lonelf
            new_lonelf = 0
        # check_global_conv_flags_eol(old=stats, new=(new_crlf,new_lonelf))
        if stats.crlf and not new_crlf:
            if rndtrp_die:
                raise SafeCrlfDie("CRLF would be replaced by LF in %s" % path)
            warning = (
                "warning: in the working copy of '%s', CRLF will be "
                "replaced by LF the next time Git touches it" % path
            )
        elif stats.lonelf and not new_lonelf:
            if rndtrp_die:
                raise SafeCrlfDie("LF would be replaced by CRLF in %s" % path)
            warning = (
                "warning: in the working copy of '%s', LF will be "
                "replaced by CRLF the next time Git touches it" % path
            )

    if not convert_crlf_into_lf:
        return src, warning

    if crlf_action in (CRLF_AUTO, CRLF_AUTO_INPUT, CRLF_AUTO_CRLF):
        # We rejected lone CR already; strip every CR.
        dst = src.replace(b"\r", b"")
    else:
        # Strip CR only when immediately followed by LF.
        dst = re.sub(b"\r(?=\n)", b"", src)
    return dst, warning


# ---------------------------------------------------------------------------
# ident_to_git  ("$Id: <sha> $" -> "$Id$")


def _ident_to_git(src: bytes, ident: bool) -> bytes:
    if not ident:
        return src
    out = bytearray()
    i = 0
    n = len(src)
    while True:
        dollar = src.find(b"$", i)
        if dollar < 0:
            break
        out += src[i : dollar + 1]
        rest_start = dollar + 1
        remaining = n - rest_start
        if remaining > 3 and src[rest_start : rest_start + 3] == b"Id:":
            close = src.find(b"$", rest_start + 3)
            if close < 0:
                # No closing dollar; emit remainder verbatim and stop.
                i = rest_start
                break
            if src.find(b"\n", rest_start + 3, close) >= 0:
                # Line break before the next dollar; leave "Id:" in place.
                i = rest_start
                continue
            out += b"Id$"
            i = close + 1
        else:
            i = rest_start
    out += src[i:]
    return bytes(out)


# ---------------------------------------------------------------------------
# apply_filter:  external filter.<drv>.clean / filter.<drv>.process drivers
#
# Ports convert.c apply_filter / apply_single_file_filter / filter_buffer_or_fd
# (the single-file clean path) and apply_multi_file_filter (the long-running
# ".process" protocol).  hash-object only ever drives the CAP_CLEAN direction.


def _sq_quote(src: str) -> str:
    """Port of quote.c sq_quote_buf(): wrap in single quotes, replace each
    ``'`` with ``'\\''`` and each ``!`` with ``'\\!'``."""
    out = ["'"]
    i = 0
    n = len(src)
    while i < n:
        # copy a run of "safe" chars (everything except ' and !)
        j = i
        while j < n and src[j] not in "'!":
            j += 1
        out.append(src[i:j])
        i = j
        while i < n and src[i] in "'!":
            out.append("'\\")
            out.append(src[i])
            out.append("'")
            i += 1
    out.append("'")
    return "".join(out)


def _expand_filter_cmd(cmd: str, path: str) -> str:
    """Port of filter_buffer_or_fd's %-expansion: ``%%`` -> ``%``,
    ``%f`` -> the sq-quoted path, any other ``%x`` -> a literal ``%`` (with
    ``x`` copied normally on the next step), and a trailing ``%`` -> ``%``."""
    out = []
    fmt = cmd
    while True:
        # strbuf_expand_step: copy up to the next '%', then consume it.
        idx = fmt.find("%")
        if idx < 0:
            out.append(fmt)
            break
        out.append(fmt[:idx])
        fmt = fmt[idx + 1 :]
        if fmt.startswith("%"):
            out.append("%")
            fmt = fmt[1:]
        elif fmt.startswith("f"):
            out.append(_sq_quote(path))
            fmt = fmt[1:]
        else:
            out.append("%")
    return "".join(out)


def _shell_argv(cmd: str) -> list:
    """Mirror run-command.c prepare_shell_cmd for a single command string.

    git execs ``["/bin/sh", "-c", cmd, cmd]`` when the command contains shell
    metacharacters, so the shell's ``$0`` (used in its own diagnostics) is the
    full command rather than the bare shell name.  We always take the shell
    path here because external filter / process commands are arbitrary."""
    return ["/bin/sh", "-c", cmd, cmd]


class _FilterDriver:
    __slots__ = ("name", "clean", "smudge", "process", "required")

    def __init__(self, repo, name: str):
        self.name = name
        self.clean = _config_get(repo, "filter.%s.clean" % name)
        self.smudge = _config_get(repo, "filter.%s.smudge" % name)
        self.process = _config_get(repo, "filter.%s.process" % name)
        req = _config_get(repo, "filter.%s.required" % name)
        self.required = _filter_config_bool(req)


def _filter_config_bool(value: Optional[str]) -> bool:
    """Port of git_config_bool() for filter.<drv>.required (a missing key is
    False; a valueless key is stored by our parser as ``"true"``)."""
    if value is None:
        return False
    v = value.lower()
    if value == "":
        return False
    if v in ("true", "yes", "on"):
        return True
    if v in ("false", "no", "off"):
        return False
    try:
        return int(value, 10) != 0
    except ValueError:
        # git_config_bool would die; treat as false (unreachable for valid cfg)
        return False


def _resolve_filter_driver(repo, attrs: dict) -> Optional[_FilterDriver]:
    """Port of git_path_check_convert(): the ``filter`` attribute names a
    driver; only a real string value selects one (true/false/unset do not)."""
    val = attrs.get("filter", None)
    if not isinstance(val, str) or not val:
        return None
    drv = _FilterDriver(repo, val)
    # convert.c only registers a driver when at least one of its commands is
    # configured (read_convert_config), but git_path_check_convert matches by
    # name regardless; a driver with no clean/smudge/process simply yields a
    # no-op apply_filter (returns 0).  We still return it so the "required but
    # did nothing" die path fires identically.
    return drv


def _apply_single_file_filter(path: str, src: bytes, cmd: str) -> Optional[bytes]:
    """Port of apply_single_file_filter + filter_buffer_or_fd.

    Returns the filtered bytes, or None if the filter failed (matching
    apply_filter returning 0).  Emits git's exact ``error:`` lines via the
    ConvertWarn channel collected by the caller; here we just print directly
    to match the streaming order git uses (errors appear before any later
    fatal)."""
    expanded = _expand_filter_cmd(cmd, path)
    try:
        proc = subprocess.Popen(
            _shell_argv(expanded),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,
        )
    except OSError:
        _emit_error("cannot fork to run external filter '%s'" % cmd)
        return None

    out = bytearray()
    write_err = False
    # Feed stdin while draining stdout, mirroring the async writer in git so a
    # filter that emits before consuming all input cannot deadlock.
    import threading

    def _reader():
        while True:
            chunk = proc.stdout.read(65536)
            if not chunk:
                break
            out.extend(chunk)

    t = threading.Thread(target=_reader)
    t.start()
    try:
        proc.stdin.write(src)
    except BrokenPipeError:
        # EPIPE: git treats this as not-an-error for the write itself.
        pass
    except OSError:
        write_err = True
    try:
        proc.stdin.close()
    except OSError:
        write_err = True
    t.join()
    proc.stdout.close()
    status = proc.wait()

    if write_err:
        _emit_error("cannot feed the input to external filter '%s'" % cmd)
    if status:
        # filter_buffer_or_fd: "external filter '%s' failed %d"
        _emit_error("external filter '%s' failed %d" % (cmd, status))

    err = write_err or bool(status)
    if err:
        # apply_single_file_filter: "external filter '%s' failed"
        _emit_error("external filter '%s' failed" % cmd)
        return None
    return bytes(out)


def _emit_error(msg: str) -> None:
    import sys

    sys.stderr.write("error: " + msg + "\n")


# --- long-running ".process" protocol (apply_multi_file_filter) ------------
#
# A pkt-line protocol over the child's stdin/stdout:
#   handshake:  git-filter-client / version=2 / flush ; capability=clean / flush
#   per blob:   command=clean / pathname=<p> / flush ; <content packets> / flush
#               -> status=success / flush ; <content> / flush ; status=success
#
# We keep one process per command string alive for the life of the call (git
# keeps it for the whole process; a single hash-object invocation only filters
# what it is told to, so per-call caching is byte-identical for our harness).

_LARGE_PACKET_DATA_MAX = 65520 - 4

_process_cache: dict = {}


class _ProcessFilterError(Exception):
    pass


def _pkt_write(f, data: Optional[bytes]) -> None:
    if data is None:
        f.write(b"0000")  # flush packet
    else:
        f.write(b"%04x" % (len(data) + 4) + data)


def _pkt_read(f) -> Optional[bytes]:
    """Read one pkt-line.  Returns b'' for a flush packet, None at EOF, or the
    payload bytes otherwise."""
    hdr = f.read(4)
    if len(hdr) < 4:
        return None
    n = int(hdr, 16)
    if n == 0:
        return b""  # flush
    if n < 4:
        raise _ProcessFilterError("bad packet length")
    return f.read(n - 4)


def _read_status(out) -> Optional[str]:
    """subprocess_read_status: read status= lines until a flush; last wins."""
    status = None
    while True:
        line = _pkt_read(out)
        if line is None:
            return status
        if line == b"":
            return status
        text = line.decode("utf-8", "replace")
        if text.startswith("status="):
            status = text[len("status=") :].rstrip("\n")


def _start_process_filter(cmd: str):
    """subprocess_start + start_multi_file_filter_fn handshake."""
    proc = subprocess.Popen(
        _shell_argv(cmd), stdin=subprocess.PIPE, stdout=subprocess.PIPE
    )
    pin, pout = proc.stdin, proc.stdout
    # handshake_version: send client id + supported versions {2,0} + flush.
    _pkt_write(pin, b"git-filter-client\n")
    _pkt_write(pin, b"version=2\n")
    _pkt_write(pin, None)
    pin.flush()
    # read welcome + version + flush
    line = _pkt_read(pout)
    if line != b"git-filter-server\n":
        raise _ProcessFilterError("bad welcome")
    line = _pkt_read(pout)
    if line is None or not line.startswith(b"version="):
        raise _ProcessFilterError("bad version")
    line = _pkt_read(pout)
    if line != b"":
        raise _ProcessFilterError("expected flush after version")
    # handshake_capabilities: announce clean/smudge/delay + flush, read back.
    _pkt_write(pin, b"capability=clean\n")
    _pkt_write(pin, b"capability=smudge\n")
    _pkt_write(pin, b"capability=delay\n")
    _pkt_write(pin, None)
    pin.flush()
    caps = set()
    while True:
        line = _pkt_read(pout)
        if line is None or line == b"":
            break
        text = line.decode("utf-8", "replace")
        if text.startswith("capability="):
            caps.add(text[len("capability=") :].rstrip("\n"))
    return proc, caps


def _apply_multi_file_filter(path: str, src: bytes, cmd: str) -> Optional[bytes]:
    """Port of apply_multi_file_filter for the CAP_CLEAN direction.

    Returns the filtered bytes, or None if the filter did not produce output
    (apply_filter returns 0): the supported-capability gate failed, or the
    protocol returned a non-"success" status."""
    entry = _process_cache.get(cmd)
    if entry is None:
        try:
            proc, caps = _start_process_filter(cmd)
        except (_ProcessFilterError, OSError):
            _emit_error("cannot fork to run subprocess '%s'" % cmd)
            return None
        entry = {"proc": proc, "caps": caps}
        _process_cache[cmd] = entry
    proc = entry["proc"]
    caps = entry["caps"]

    # if !(supported & wanted) -> apply_filter returns 0 (no filtering).
    if "clean" not in caps:
        return None

    pin, pout = proc.stdin, proc.stdout
    try:
        _pkt_write(pin, b"command=clean\n")
        _pkt_write(pin, ("pathname=%s\n" % path).encode("utf-8"))
        _pkt_write(pin, None)  # flush metadata
        # write_packetized_from_buf_no_flush: split into <=LARGE_PACKET_DATA_MAX
        off = 0
        n = len(src)
        while off < n:
            chunk = src[off : off + _LARGE_PACKET_DATA_MAX]
            _pkt_write(pin, chunk)
            off += len(chunk)
        _pkt_write(pin, None)  # flush content
        pin.flush()
    except (BrokenPipeError, OSError):
        _handle_process_error(cmd, "")
        return None

    status = _read_status(pout)
    if status != "success":
        _handle_process_error(cmd, status)
        return None

    # read content packets until flush
    out = bytearray()
    while True:
        line = _pkt_read(pout)
        if line is None:
            _handle_process_error(cmd, "")
            return None
        if line == b"":
            break
        out.extend(line)

    status = _read_status(pout)
    if status != "success":
        _handle_process_error(cmd, status)
        return None
    return bytes(out)


def _handle_process_error(cmd: str, status: Optional[str]) -> None:
    """Port of handle_filter_error: 'error' is silent (file-level problem);
    'abort' disables the capability silently; anything else is a protocol
    failure that prints an error and tears the process down."""
    if status == "error":
        return
    if status == "abort":
        # drop the cached entry so a later blob would restart (unobservable
        # within a single hash-object since each path filters once).
        _process_cache.pop(cmd, None)
        return
    _emit_error("external filter '%s' failed" % cmd)
    entry = _process_cache.pop(cmd, None)
    if entry is not None:
        try:
            entry["proc"].kill()
        except OSError:
            pass


def _apply_filter(repo, path: str, src: bytes, drv: Optional[_FilterDriver]):
    """Port of apply_filter for CAP_CLEAN.

    Returns (new_src_or_None, applied) where ``applied`` mirrors the int return
    of apply_filter (1 if the filter ran and produced output, else 0).  ``cmd``
    selection: prefer .process; otherwise use .clean."""
    if drv is None:
        return None, False
    cmd = None
    if not drv.process and drv.clean:
        cmd = drv.clean
    if cmd:
        out = _apply_single_file_filter(path, src, cmd)
    elif drv.process:
        out = _apply_multi_file_filter(path, src, drv.process)
    else:
        return None, False
    if out is None:
        return None, False
    return out, True


# ---------------------------------------------------------------------------
# encode_to_git:  working-tree-encoding=<enc>  (re-encode worktree -> UTF-8)
#
# Ports convert.c encode_to_git + validate_encoding + check_roundtrip, and the
# utf8.c BOM helpers (has_prohibited_utf_bom / is_missing_required_utf_bom).

_UTF16_BE_BOM = b"\xfe\xff"
_UTF16_LE_BOM = b"\xff\xfe"
_UTF32_BE_BOM = b"\x00\x00\xfe\xff"
_UTF32_LE_BOM = b"\xff\xfe\x00\x00"
_DEFAULT_ENCODING = "UTF-8"


def _skip_iprefix(s: str, prefix: str) -> Optional[str]:
    """If ``s`` starts with ``prefix`` case-insensitively, return the rest."""
    if s[: len(prefix)].lower() == prefix.lower():
        return s[len(prefix) :]
    return None


def _same_utf_encoding(a: str, b: str) -> bool:
    """Port of utf8.c same_utf_encoding(): both must start with 'utf' (case
    insensitive), then an optional '-' is skipped on each, then compared
    case-insensitively.  E.g. UTF-16BE == UTF16BE."""
    ra = _skip_iprefix(a, "utf")
    rb = _skip_iprefix(b, "utf")
    if ra is None or rb is None:
        return False
    if ra.startswith("-"):
        ra = ra[1:]
    if rb.startswith("-"):
        rb = rb[1:]
    return ra.lower() == rb.lower()


def _same_encoding(a: Optional[str], b: Optional[str]) -> bool:
    if a is None:
        a = "UTF-8"
    if b is None:
        b = "UTF-8"
    if _same_utf_encoding(a, b):
        return True
    return a.lower() == b.lower()


def _has_bom_prefix(data: bytes, bom: bytes) -> bool:
    return len(data) >= len(bom) and data[: len(bom)] == bom


def _has_prohibited_utf_bom(enc: str, data: bytes) -> bool:
    return (
        (_same_utf_encoding("UTF-16BE", enc) or _same_utf_encoding("UTF-16LE", enc))
        and (
            _has_bom_prefix(data, _UTF16_BE_BOM)
            or _has_bom_prefix(data, _UTF16_LE_BOM)
        )
    ) or (
        (_same_utf_encoding("UTF-32BE", enc) or _same_utf_encoding("UTF-32LE", enc))
        and (
            _has_bom_prefix(data, _UTF32_BE_BOM)
            or _has_bom_prefix(data, _UTF32_LE_BOM)
        )
    )


def _is_missing_required_utf_bom(enc: str, data: bytes) -> bool:
    return (
        _same_utf_encoding(enc, "UTF-16")
        and not (
            _has_bom_prefix(data, _UTF16_BE_BOM)
            or _has_bom_prefix(data, _UTF16_LE_BOM)
        )
    ) or (
        _same_utf_encoding(enc, "UTF-32")
        and not (
            _has_bom_prefix(data, _UTF32_BE_BOM)
            or _has_bom_prefix(data, _UTF32_LE_BOM)
        )
    )


def _validate_encoding(path: str, enc: str, data: bytes, die_on_error: bool):
    """Port of convert.c validate_encoding.  On a BOM violation, either raises
    ConvertDie (die_on_error) or ConvertWarn (error+hint), the latter telling
    the caller to leave the content unmodified."""
    stripped = _skip_iprefix(enc, "UTF")
    if stripped is None:
        return  # only UTF?? encodings are BOM-checked
    if stripped.startswith("-"):
        stripped = stripped[1:]

    if _has_prohibited_utf_bom(enc, data):
        # advise uses UTF-<stripped without trailing "BE"/"LE">
        advise = (
            "The file '%s' contains a byte order mark (BOM). Please use "
            "UTF-%s as working-tree-encoding." % (path, stripped[: len(stripped) - 2])
        )
        msg = "BOM is prohibited in '%s' if encoded as %s" % (path, enc)
        if die_on_error:
            raise ConvertDie(msg, advice="hint: " + advise)
        raise ConvertWarn(["hint: " + advise, "error: " + msg])

    if _is_missing_required_utf_bom(enc, data):
        advise = (
            "The file '%s' is missing a byte order mark (BOM). Please use "
            "UTF-%sBE or UTF-%sLE (depending on the byte order) as "
            "working-tree-encoding." % (path, stripped, stripped)
        )
        msg = "BOM is required in '%s' if encoded as %s" % (path, enc)
        if die_on_error:
            raise ConvertDie(msg, advice="hint: " + advise)
        raise ConvertWarn(["hint: " + advise, "error: " + msg])


def _reencode(data: bytes, from_enc: str, to_enc: str) -> Optional[bytes]:
    """Mirror reencode_string_len for the cases hash-object needs.

    Decoding the worktree content from ``from_enc`` and producing ``to_enc``
    (always UTF-8 for encode_to_git).  Returns None on any codec failure
    (iconv_open failing / a conversion error -> reencode_string_len NULL)."""
    import codecs

    # reencode_string_len: "UTF-16LE-BOM" reads as plain "UTF-16".
    if _same_utf_encoding("UTF-16LE-BOM", from_enc):
        from_enc = "UTF-16"
    try:
        codecs.lookup(from_enc)
        codecs.lookup(to_enc)
    except LookupError:
        # iconv_open() would fail -> reencode_string_len returns NULL.
        return None
    try:
        text = data.decode(from_enc)
        return text.encode(to_enc)
    except (UnicodeDecodeError, UnicodeEncodeError, ValueError):
        return None


def _check_roundtrip_enc(repo, enc: str) -> bool:
    """Port of check_roundtrip(): is ``enc`` listed in
    core.checkRoundtripEncoding (default "SHIFT-JIS")?  The list is comma/space
    separated and matched case-insensitively on word boundaries."""
    encoding = _config_get(repo, "core.checkRoundtripEncoding")
    if encoding is None:
        encoding = "SHIFT-JIS"
    low = encoding.lower()
    target = enc.lower()
    idx = low.find(target)
    if idx < 0:
        return False
    elen = len(encoding)
    nxt = idx + len(enc)
    before_ok = idx == 0 or (encoding[idx - 1].isspace() or encoding[idx - 1] == ",")
    after_ok = nxt == elen or (
        nxt < elen and (encoding[nxt].isspace() or encoding[nxt] == ",")
    )
    return before_ok and after_ok


def _encode_to_git(repo, path: str, src: bytes, enc: Optional[str], die_on_error: bool):
    """Port of convert.c encode_to_git.

    Returns (new_src_or_None, applied).  ``applied`` is True only when the
    content was re-encoded.  Raises ConvertDie / ConvertWarn exactly where git
    would die() / error()."""
    if not enc or not src:
        return None, False

    # validate_encoding: BOM rules.  Raises ConvertDie (die_on_error) or
    # ConvertWarn; on ConvertWarn the caller leaves content unmodified, which
    # matches "return 0" after validate_encoding errors.
    _validate_encoding(path, enc, src, die_on_error)

    dst = _reencode(src, enc, _DEFAULT_ENCODING)
    if dst is None:
        msg = "failed to encode '%s' from %s to %s" % (path, enc, _DEFAULT_ENCODING)
        if die_on_error:
            raise ConvertDie(msg)
        raise ConvertWarn(["error: " + msg])

    # Round-trip check (only when writing to git and enc is configured for it).
    if die_on_error and _check_roundtrip_enc(repo, enc):
        re_src = _reencode(dst, _DEFAULT_ENCODING, enc)
        if re_src is None or re_src != src:
            raise ConvertDie(
                "encoding '%s' from %s to %s and back is not the same"
                % (path, enc, _DEFAULT_ENCODING)
            )
    return dst, True


# ---------------------------------------------------------------------------
# public entry point


def _check_encoding_attr(attrs: dict) -> Optional[str]:
    """Port of git_path_check_encoding(): the working-tree-encoding attribute.

    Returns the encoding name, or None when unset / empty / equal to the
    default UTF-8.  A true/false value die()s ("true/false are no valid
    working-tree-encodings")."""
    val = attrs.get("working-tree-encoding", None)
    if val is None or val is False:
        # ATTR_UNSET maps to None here (the "!attr" / "-attr" forms) and an
        # absent attr is also None; both mean "no encoding".
        return None
    if val is True:
        raise ConvertDie("true/false are no valid working-tree-encodings")
    if not isinstance(val, str) or not val:
        return None
    # Don't encode to the default encoding.
    if _same_encoding(val, _DEFAULT_ENCODING):
        return None
    return val


def convert_to_git(
    repo: Optional[Repository],
    path: str,
    data: bytes,
    write_object: bool,
) -> tuple[bytes, Optional[str]]:
    """Apply git's clean conversion for ``path`` to ``data``, in convert.c's
    convert_to_git() order: apply_filter -> encode_to_git -> crlf_to_git ->
    ident_to_git.

    Returns (converted_bytes, warning_message_or_None).

    Raises ConvertDie (the caller prints ``fatal: <message>``, rc 128) for any
    die() reachable here.  Non-fatal filter / encoding failures print their
    ``error:``/``hint:`` lines directly (matching git's streaming order) and
    leave the content unmodified for that stage.

    ``write_object`` mirrors INDEX_WRITE_OBJECT: only then does git enable the
    safe-crlf round-trip check (global_conv_flags_eol) and the
    working-tree-encoding die_on_error behaviour (CONV_WRITE_OBJECT)."""
    import sys

    attrs = _check_attrs(repo, path)

    # 1. apply_filter: external filter.<drv>.clean / filter.<drv>.process.
    drv = _resolve_filter_driver(repo, attrs)
    new_data, applied = _apply_filter(repo, path, data, drv)
    if applied:
        data = new_data
    elif drv is not None and drv.required:
        # convert.c: if (!ret && ca.drv && ca.drv->required) die(...)
        raise ConvertDie("%s: clean filter '%s' failed" % (path, drv.name))

    # 2. encode_to_git: working-tree-encoding=<enc> -> UTF-8.
    enc = _check_encoding_attr(attrs)
    if enc is not None:
        try:
            new_data, applied = _encode_to_git(repo, path, data, enc, write_object)
        except ConvertWarn as warn:
            # error()/hint() then leave content unmodified (return 0).
            for line in warn.messages:
                sys.stderr.write(line + "\n")
        else:
            if applied:
                data = new_data

    # 3. crlf_to_git: text/eol/crlf attributes + core.autocrlf.
    crlf_action = _resolve_crlf_action(repo, attrs)
    conv_flags = {}
    if write_object:
        conv_flags["safecrlf"] = _safecrlf(repo)
    else:
        conv_flags["safecrlf"] = "false"
    data, warning = _crlf_to_git(repo, path, data, crlf_action, conv_flags)

    # 4. ident_to_git runs last: "$Id: <sha> $" -> "$Id$".
    data = _ident_to_git(data, attrs.get("ident") is True)
    return data, warning
