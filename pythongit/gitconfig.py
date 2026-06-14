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


def get_all(repo: Optional[Repository], name: str) -> list[str]:
    key = _normalize_key(name)
    return [value for full, value in list_all(repo) if full == key]


def get(repo: Optional[Repository], name: str) -> Optional[str]:
    values = get_all(repo, name)
    return values[-1] if values else None


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
