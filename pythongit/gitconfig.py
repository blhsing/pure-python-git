"""A reader for Git's configuration file format.

Python's :mod:`configparser` cannot represent Git config faithfully: Git has
``[section "subsection"]`` headers, case-insensitive section/variable names with
case-sensitive subsections, multi-valued variables, and its own value escaping.
This module parses the real format and applies Git's file precedence (system,
global, local, then ``-c`` parameters) so ``config`` and ``var`` can match C Git.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator, Optional

from .repo import Repository


class ConfigError(Exception):
    pass


def _parse_text(text: str) -> list[tuple[str, str]]:
    """Parse one config file's text into ordered ``(full_key, value)`` pairs.

    ``full_key`` is ``section.key`` or ``section.subsection.key`` with the
    section and key lowercased and the subsection left as written.
    """
    pairs: list[tuple[str, str]] = []
    section: Optional[str] = None
    subsection: Optional[str] = None
    i = 0
    n = len(text)
    while i < n:
        # Start of a logical line; skip leading horizontal whitespace.
        while i < n and text[i] in " \t":
            i += 1
        if i >= n:
            break
        ch = text[i]
        if ch in "\r\n":
            i += 1
            continue
        if ch in "#;":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if ch == "[":
            i += 1
            header_start = i
            while i < n and text[i] != "]" and text[i] != "\n":
                i += 1
            header = text[header_start:i]
            if i < n and text[i] == "]":
                i += 1
            section, subsection = _parse_header(header)
            continue
        # variable line: name [= value]
        name_start = i
        while i < n and (text[i].isalnum() or text[i] == "-"):
            i += 1
        name = text[name_start:i].lower()
        # skip whitespace before '=' or EOL/comment
        while i < n and text[i] in " \t":
            i += 1
        if i < n and text[i] == "=":
            i += 1
            value, i = _read_value(text, i)
        else:
            # boolean true; consume rest of line / inline comment
            value = "true"
            while i < n and text[i] != "\n":
                i += 1
        if name and section is not None:
            full = f"{section}.{subsection}.{name}" if subsection is not None else f"{section}.{name}"
            pairs.append((full, value))
    return pairs


def _parse_header(header: str) -> tuple[str, Optional[str]]:
    header = header.strip()
    if '"' in header:
        sect, _, rest = header.partition('"')
        sub = rest.rsplit('"', 1)[0]
        # Unescape \\ and \" inside the subsection.
        sub = sub.replace('\\\\', '\\').replace('\\"', '"')
        return sect.strip().lower(), sub
    if "." in header:  # deprecated [section.subsection] form
        sect, _, sub = header.partition(".")
        return sect.strip().lower(), sub.strip()
    return header.lower(), None


def _read_value(text: str, i: int) -> tuple[str, int]:
    n = len(text)
    out: list[str] = []
    in_quotes = False
    # Leading whitespace after '=' is not part of the value.
    while i < n and text[i] in " \t":
        i += 1
    trailing_ws = 0
    while i < n:
        c = text[i]
        if c == "\\":
            nxt = text[i + 1] if i + 1 < n else ""
            if nxt == "\n":  # line continuation
                i += 2
                continue
            mapping = {"n": "\n", "t": "\t", "b": "\b", "\\": "\\", '"': '"'}
            if nxt in mapping:
                out.append(mapping[nxt])
                trailing_ws = 0
                i += 2
                continue
            out.append(nxt)
            trailing_ws = 0
            i += 2
            continue
        if c == '"':
            in_quotes = not in_quotes
            i += 1
            continue
        if not in_quotes and c == "\n":
            break
        if not in_quotes and c in "#;":
            while i < n and text[i] != "\n":
                i += 1
            break
        if not in_quotes and c in " \t":
            out.append(c)
            trailing_ws += 1
            i += 1
            continue
        out.append(c)
        trailing_ws = 0
        i += 1
    if trailing_ws:
        del out[len(out) - trailing_ws:]
    return "".join(out), i


def _parameters_pairs() -> list[tuple[str, str]]:
    """Pairs from ``GIT_CONFIG_PARAMETERS`` (set by ``git -c``)."""
    raw = os.environ.get("GIT_CONFIG_PARAMETERS", "")
    pairs: list[tuple[str, str]] = []
    for chunk in raw.split("\n"):
        chunk = chunk.strip()
        if not chunk:
            continue
        # Each item is either 'key=value' or a single-quoted 'key=value'.
        if chunk.startswith("'") and chunk.endswith("'") and len(chunk) >= 2:
            chunk = chunk[1:-1].replace("'\\''", "'")
        key, sep, value = chunk.partition("=")
        key = key.strip()
        if not sep:
            pairs.append((_normalize_key(key), "true"))
        else:
            pairs.append((_normalize_key(key), value))
    return pairs


def _normalize_key(name: str) -> str:
    section, _, rest = name.partition(".")
    sub, dot, key = rest.rpartition(".")
    if dot:
        return f"{section.lower()}.{sub}.{key.lower()}"
    return f"{section.lower()}.{rest.lower()}"


def _config_files(repo: Optional[Repository]) -> list[Path]:
    files: list[Path] = []
    if not os.environ.get("GIT_CONFIG_NOSYSTEM"):
        system = os.environ.get("GIT_CONFIG_SYSTEM")
        files.append(Path(system) if system else Path("/etc/gitconfig"))
    git_global = os.environ.get("GIT_CONFIG_GLOBAL")
    if git_global:
        files.append(Path(git_global))
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
        files.append(Path(xdg) / "git" / "config")
        files.append(Path(os.path.expanduser("~")) / ".gitconfig")
    if repo is not None:
        files.append(repo.gitdir / "config")
    return files


def _scope_of(path: Path, repo: Optional[Repository]) -> str:
    if repo is not None and path == repo.gitdir / "config":
        return "local"
    if not os.environ.get("GIT_CONFIG_NOSYSTEM"):
        system = os.environ.get("GIT_CONFIG_SYSTEM")
        if path == (Path(system) if system else Path("/etc/gitconfig")):
            return "system"
    return "global"


def list_all(repo: Optional[Repository]) -> list[tuple[str, str]]:
    """All ``(full_key, value)`` pairs across system/global/local and ``-c``."""
    pairs: list[tuple[str, str]] = []
    for path in _config_files(repo):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        pairs.extend(_parse_text(text))
    pairs.extend(_parameters_pairs())
    return pairs


def list_all_scoped(repo: Optional[Repository]):
    """Like :func:`list_all` but yields ``(scope, origin_path, key, value)``.

    ``origin_path`` is the config file path (or ``None`` for ``-c`` params),
    ``scope`` is one of ``system``/``global``/``local``/``command``.
    """
    out: list[tuple[str, Optional[Path], str, str]] = []
    for path in _config_files(repo):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        scope = _scope_of(path, repo)
        for key, value in _parse_text(text):
            out.append((scope, path, key, value))
    for key, value in _parameters_pairs():
        out.append(("command", None, key, value))
    return out


def get_all(repo: Optional[Repository], name: str) -> list[str]:
    key = _normalize_key(name)
    return [value for full, value in list_all(repo) if full == key]


def get(repo: Optional[Repository], name: str) -> Optional[str]:
    values = get_all(repo, name)
    return values[-1] if values else None


def apply_insteadof(repo: Optional[Repository], url: str, *, push: bool = False) -> str:
    """Rewrite ``url`` per ``url.<base>.insteadOf`` (and pushInsteadOf).

    Mirrors remote.c:alias_url(): among all ``url.<base>.insteadOf = <prefix>``
    entries whose ``<prefix>`` is a prefix of ``url``, the longest-matching
    prefix wins; the URL's matching prefix is replaced by ``<base>`` (the
    subsection). When ``push`` is set, pushInsteadOf is consulted first (and the
    URL is only rewritten if a push rule matches; remote.c falls back to plain
    insteadOf for the fetch URL separately, so callers decide the order)."""
    subkey = "pushinsteadof" if push else "insteadof"
    longest_base: Optional[str] = None
    longest_prefix = ""
    for full, value in list_all(repo):
        if not full.startswith("url.") or not full.endswith("." + subkey):
            continue
        base = full[len("url."):-len("." + subkey)]
        if url.startswith(value) and (longest_base is None or len(value) > len(longest_prefix)):
            longest_base = base
            longest_prefix = value
    if longest_base is None:
        return url
    return longest_base + url[len(longest_prefix):]


def _header_for(section: str, subsection: Optional[str]) -> str:
    if subsection is None:
        return f"[{section}]"
    escaped = subsection.replace("\\", "\\\\").replace('"', '\\"')
    return f'[{section} "{escaped}"]'


def _format_value(value: str) -> str:
    if value == "":
        return ""
    needs_quote = value != value.strip() or any(c in value for c in '#;"\n\t')
    body = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t")
    return f'"{body}"' if needs_quote else body


def _line_sections(lines: list[str]) -> list[tuple[Optional[str], Optional[str]]]:
    """For each line, the (section, subsection) in effect on that line."""
    out: list[tuple[Optional[str], Optional[str]]] = []
    section: Optional[str] = None
    subsection: Optional[str] = None
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            header = stripped[1:].split("]", 1)[0]
            section, subsection = _parse_header(header)
        out.append((section, subsection))
    return out


def _line_defines_key(line: str, key: str) -> bool:
    stripped = line.strip()
    if not stripped or stripped[0] in "[#;":
        return False
    name = stripped.split("=", 1)[0].strip()
    return name.lower() == key.lower()


def _emit_kv(key: str, value: str) -> str:
    return f"\t{key} = {_format_value(value)}\n" if value != "" else f"\t{key} =\n"


def write_value(
    path: Path,
    section: str,
    subsection: Optional[str],
    key: str,
    value: str,
    *,
    mode: str = "set",
) -> None:
    """Set/add/replace a variable in ``path`` using Git's on-disk format.

    ``mode`` is ``set`` (replace a lone value, append if absent), ``add``
    (always append), or ``replace_all`` (drop existing values, write one).
    """
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = text.splitlines(keepends=True)
    owners = _line_sections(lines)
    target = (section.lower(), subsection)
    match_idx = [
        i for i, line in enumerate(lines)
        if (owners[i][0], owners[i][1]) == target and _line_defines_key(line, key)
    ]
    if mode == "replace_all" and match_idx:
        for i in reversed(match_idx[1:]):
            del lines[i]
            del owners[i]
        lines[match_idx[0]] = _emit_kv(key, value)
        path.write_text("".join(lines), encoding="utf-8")
        return
    if mode == "set" and match_idx:
        lines[match_idx[0]] = _emit_kv(key, value)
        path.write_text("".join(lines), encoding="utf-8")
        return
    # add mode, or set/replace_all with no existing value: insert into section.
    section_last = max(
        (i for i, owner in enumerate(owners) if owner == target),
        default=None,
    )
    if section_last is not None:
        insert_at = section_last + 1
        lines.insert(insert_at, _emit_kv(key, value))
    else:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(f"{_header_for(section, subsection)}\n")
        lines.append(_emit_kv(key, value))
    path.write_text("".join(lines), encoding="utf-8")


def unset_value(path: Path, section: str, subsection: Optional[str], key: str, *, all_values: bool) -> int:
    if not path.exists():
        return 5
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    owners = _line_sections(lines)
    target = (section.lower(), subsection)
    match_idx = [
        i for i, line in enumerate(lines)
        if (owners[i][0], owners[i][1]) == target and _line_defines_key(line, key)
    ]
    if not match_idx:
        return 5
    if not all_values and len(match_idx) > 1:
        return 5
    for i in reversed(match_idx):
        del lines[i]
    path.write_text("".join(lines), encoding="utf-8")
    return 0


def remove_section(path: Path, section: str, subsection: Optional[str]) -> bool:
    """Drop an entire ``[section "subsection"]`` block from ``path``."""
    if not path.exists():
        return False
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    target = (section.lower(), subsection)
    keep: list[str] = []
    removed = False
    in_target = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            header = stripped[1:].split("]", 1)[0]
            in_target = _parse_header(header) == target
            if in_target:
                removed = True
                continue
        if in_target:
            continue
        keep.append(line)
    if removed:
        path.write_text("".join(keep), encoding="utf-8")
    return removed


def split_key(name: str) -> Optional[tuple[str, Optional[str], str]]:
    """Split ``section[.subsection].key``; None if it has no section."""
    if "." not in name:
        return None
    section, _, rest = name.partition(".")
    if not section:
        return None
    sub, dot, key = rest.rpartition(".")
    if not key:
        return None
    if dot:
        return section.lower(), sub, key.lower()
    return section.lower(), None, key.lower()


def rename_section(path: Path, old: str, new: Optional[str]) -> int:
    """Rename ``[old]`` to ``[new]`` (or remove it when ``new`` is None).

    ``old`` and ``new`` are full section names (``section`` or
    ``section.subsection``).  Returns the number of section headers renamed,
    or a negative value on a write/parse error.  Mirrors C Git's
    git_config_rename_section_in_file: matching headers are rewritten in place,
    and when removing, the section's variable lines are dropped too.
    """
    def _parse(spec: str) -> tuple[str, Optional[str]]:
        s, dot, sub = spec.partition(".")
        return s.lower(), (sub if dot else None)

    old_sec, old_sub = _parse(old)
    new_spec = _parse(new) if new is not None else None
    if not path.exists():
        return 0
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    out: list[str] = []
    found = 0
    in_target = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            header = stripped[1:].split("]", 1)[0]
            sec, sub = _parse_header(header)
            in_target = (sec, sub) == (old_sec, old_sub)
            if in_target:
                found += 1
                if new_spec is None:
                    # remove-section: drop the header line entirely.
                    continue
                # rename-section: rewrite the header, preserving any trailing
                # content after the ']' on the same line.
                trailing = ""
                bracket = line.find("]")
                if bracket != -1:
                    trailing = line[bracket + 1:]
                out.append(_header_for(new_spec[0], new_spec[1]) + trailing
                           if trailing.strip("\r\n ")
                           else _header_for(new_spec[0], new_spec[1]) + "\n")
                continue
        elif in_target and new_spec is None:
            # remove-section: drop variable lines belonging to the section.
            continue
        out.append(line)
    if found:
        path.write_text("".join(out), encoding="utf-8")
    return found


# --- value type normalization / formatting (config.c) --------------------

class ConfigValueError(Exception):
    """Raised when a typed config value fails validation."""
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _get_unit_factor(end: str) -> int:
    if end == "":
        return 1
    low = end.lower()
    if low == "k":
        return 1024
    if low == "m":
        return 1024 * 1024
    if low == "g":
        return 1024 * 1024 * 1024
    return 0


_INT_MAX = (1 << 63) - 1


def parse_signed(value: str) -> Optional[tuple[int, str]]:
    """Port of git_parse_signed: parse a (possibly unit-suffixed) integer.

    Returns ``(result, error)`` where exactly one element is meaningful:
    ``error`` is ``""`` on success, ``"invalid unit"`` for a bad/garbage
    suffix, or ``"out of range"`` for overflow.  Returns ``None`` only when
    the value is empty.
    """
    if value is None or value == "":
        return None
    # strtoimax(value, &end, base=0): leading whitespace, optional sign, then
    # decimal / 0x-hex / 0-octal digits.  `end` is the first unconsumed char.
    s = value
    n = len(s)
    i = 0
    while i < n and s[i] in " \t\n\r\f\v":
        i += 1
    sign = 1
    if i < n and s[i] in "+-":
        if s[i] == "-":
            sign = -1
        i += 1
    base = 10
    num_start = i
    if i < n and s[i] == "0":
        if i + 1 < n and s[i + 1] in "xX":
            base = 16
            i += 2
            num_start = i
        else:
            base = 8
            # leading 0 is itself a consumed octal digit
    digset = ("0123456789abcdefABCDEF" if base == 16
              else "01234567" if base == 8 else "0123456789")
    j = i
    while j < n and s[j] in digset:
        j += 1
    if j == num_start:
        # No digits at all after the prefix -> end==value (EINVAL).
        return (0, "invalid unit")
    body = s[num_start:j]
    try:
        val = int(body, base) * sign
    except ValueError:
        return (0, "invalid unit")
    end = s[j:]
    factor = _get_unit_factor(end)
    if factor == 0:
        return (0, "invalid unit")
    # overflow check against signed 64-bit max
    if (val < 0 and (-_INT_MAX - 1) // factor > val) or \
       (val > 0 and _INT_MAX // factor < val):
        return (0, "out of range")
    return (val * factor, "")


def parse_maybe_bool_text(value: Optional[str]) -> int:
    """git_parse_maybe_bool_text: 1=true, 0=false, -1=not a bool."""
    if value is None:
        return 1
    if value == "":
        return 0
    low = value.lower()
    if low in ("true", "yes", "on"):
        return 1
    if low in ("false", "no", "off"):
        return 0
    return -1


def parse_maybe_bool(value: Optional[str]) -> int:
    v = parse_maybe_bool_text(value)
    if v >= 0:
        return v
    parsed = parse_signed(value or "")
    if parsed is not None and parsed[1] == "":
        return 1 if parsed[0] else 0
    return -1


def config_int64(key: str, value: str) -> int:
    """git_config_int64: parse or die with the 'bad numeric config value'."""
    parsed = parse_signed(value if value is not None else "")
    if parsed is None or parsed[1] != "":
        reason = parsed[1] if parsed is not None else "invalid unit"
        raise ConfigValueError(
            f"bad numeric config value '{value}' for '{key}': {reason}")
    return parsed[0]


def config_bool(key: str, value: Optional[str]) -> bool:
    """git_config_bool: bool text, else nonzero int, else die."""
    v = parse_maybe_bool_text(value)
    if v >= 0:
        return bool(v)
    parsed = parse_signed(value or "")
    if parsed is not None and parsed[1] == "":
        return bool(parsed[0])
    raise ConfigValueError(
        f"bad boolean config value '{value}' for '{key}'")


def config_bool_or_int(key: str, value: str) -> tuple[int, bool]:
    """git_config_bool_or_int: (value, is_bool)."""
    v = parse_maybe_bool_text(value)
    if v >= 0:
        return v, True
    return config_int64(key, value), False


# --- color parsing (color.c color_parse) ---------------------------------

_COLOR_NAMES = ("black", "red", "green", "yellow",
                "blue", "magenta", "cyan", "white")
_COLOR_ATTRS = {
    "bold": (1, 22), "dim": (2, 22), "italic": (3, 23), "ul": (4, 24),
    "blink": (5, 25), "reverse": (7, 27), "strike": (9, 29),
}


def _parse_one_color(word: str):
    """Return a ('normal'|'ansi'|'256'|'rgb', payload) tuple or None."""
    low = word.lower()
    if low == "normal":
        return ("normal", None)
    if len(word) in (7, 4) and word[0] == "#":
        per = 2 if len(word) == 7 else 1
        body = word[1:]
        try:
            comps = []
            for k in range(3):
                seg = body[k * per:k * per + per]
                comps.append(int(seg[0] + seg[-1], 16))
            return ("rgb", tuple(comps))
        except ValueError:
            return None
    # ANSI named
    name = low
    offset = 30
    if name == "default":
        return ("ansi", 9 + 30)
    if name.startswith("bright"):
        offset = 90
        name = name[6:]
    for i, cn in enumerate(_COLOR_NAMES):
        if name == cn:
            return ("ansi", i + offset)
    # literal 256-color number: strtol(name, &end, 10) must consume all of word
    body = word
    sign = ""
    if body[:1] in "+-":
        sign, body = word[0], word[1:]
    if not body or not body.isdigit():
        return None
    val = int(word, 10)
    if val < -1:
        return None
    if val < 0:
        return ("normal", None)
    if val < 8:
        return ("ansi", val + 30)
    if val < 16:
        return ("ansi", val - 8 + 90)
    if val < 256:
        return ("256", val)
    return None


def _color_output(c, background: bool) -> str:
    offset = 10 if background else 0
    kind, payload = c
    if kind in ("normal",):
        return ""
    if kind == "ansi":
        return str(payload + offset)
    if kind == "256":
        return f"{38 + offset};5;{payload}"
    if kind == "rgb":
        r, g, b = payload
        return f"{38 + offset};2;{r};{g};{b}"
    return ""


def _color_empty(c) -> bool:
    return c is None or c[0] in ("unspecified", "normal")


def color_parse(value: str) -> Optional[str]:
    """color_parse_quietly: ANSI escape for ``value`` or None if invalid."""
    ptr = value
    # leading whitespace
    ptr = ptr.lstrip()
    if not ptr.strip():
        return ""
    words = ptr.split()
    has_reset = False
    attr = 0
    fg = None
    bg = None
    for word in words:
        if word.lower() == "reset":
            has_reset = True
            continue
        c = _parse_one_color(word)
        if c is not None:
            if fg is None:
                fg = c
                continue
            if bg is None:
                bg = c
                continue
            return None
        low = word.lower()
        negate = False
        nm = low
        if nm.startswith("no"):
            nm = nm[2:]
            if nm.startswith("-"):
                nm = nm[1:]
            negate = True
        if nm in _COLOR_ATTRS:
            val = _COLOR_ATTRS[nm][1] if negate else _COLOR_ATTRS[nm][0]
            attr |= (1 << val)
        else:
            return None
    if not (has_reset or attr or not _color_empty(fg) or not _color_empty(bg)):
        return ""
    parts: list[str] = []
    if has_reset:
        parts.append("")
    for i in range(32):
        if attr & (1 << i):
            parts.append(str(i))
    if not _color_empty(fg):
        parts.append(_color_output(fg, False))
    if not _color_empty(bg):
        parts.append(_color_output(bg, True))
    return "\033[" + ";".join(parts) + "m"
