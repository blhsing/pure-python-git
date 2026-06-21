"""Git's content conversion (the convert_to_git / "clean" path).

This implements the BUILT-IN conversions git applies when storing a blob: the
gitattributes-driven CRLF->LF text normalization (the ``text``/``eol``/``crlf``
attributes plus ``core.autocrlf``).  It mirrors convert.c closely enough to be
byte-exact for ``git hash-object`` (which never reads the index, so the
``has_crlf_in_index`` short-circuit never fires there).

External clean filters (``filter.<drv>.clean``), working-tree-encoding and the
``ident`` attribute are intentionally NOT handled here; callers that encounter
them must fall back / reject so we never accept-and-diverge.
"""
from __future__ import annotations

import re
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
    """Raised when a conversion cannot be performed byte-exact (e.g. an
    external clean filter or working-tree-encoding is configured)."""


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
# public entry point


def convert_to_git(
    repo: Optional[Repository],
    path: str,
    data: bytes,
    write_object: bool,
) -> tuple[bytes, Optional[str]]:
    """Apply git's clean conversion for ``path`` to ``data``.

    Returns (converted_bytes, warning_message_or_None).  Raises ConvertError if
    the path's attributes request a conversion we cannot perform byte-exact (an
    external clean filter or a working-tree-encoding).

    ``write_object`` mirrors INDEX_WRITE_OBJECT: only then does git enable the
    safe-crlf round-trip check (global_conv_flags_eol).
    """
    attrs = _check_attrs(repo, path)

    # External clean filter: we cannot guarantee byte-exact output.
    filt = attrs.get("filter", None)
    if isinstance(filt, str) and filt:
        drv_clean = _config_get(repo, "filter.%s.clean" % filt)
        drv_process = _config_get(repo, "filter.%s.process" % filt)
        if drv_clean or drv_process:
            raise ConvertError("filter:%s" % filt)

    # working-tree-encoding requires iconv; defer.
    enc = attrs.get("working-tree-encoding", None)
    if isinstance(enc, str) and enc and enc.lower() not in ("utf-8", "utf8"):
        raise ConvertError("encoding:%s" % enc)

    crlf_action = _resolve_crlf_action(repo, attrs)
    conv_flags = {}
    if write_object:
        conv_flags["safecrlf"] = _safecrlf(repo)
    else:
        conv_flags["safecrlf"] = "false"
    data, warning = _crlf_to_git(repo, path, data, crlf_action, conv_flags)

    # ident_to_git runs last in the pipeline: "$Id: <sha> $" -> "$Id$".
    data = _ident_to_git(data, attrs.get("ident") is True)
    return data, warning
