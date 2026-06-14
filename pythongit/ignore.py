"""gitignore parsing."""
from __future__ import annotations

import os
import re
from pathlib import Path


class IgnoreRule:
    __slots__ = ("pattern", "negate", "dir_only", "pathname", "base", "regex")

    def __init__(self, raw: str, base: str):
        self.negate = False
        self.dir_only = False
        self.pathname = False
        self.base = base.strip("/")
        s = raw
        if s.startswith("!"):
            self.negate = True
            s = s[1:]
        elif s.startswith("\\!") or s.startswith("\\#"):
            s = s[1:]
        if s.endswith("/"):
            self.dir_only = True
            s = s[:-1]
        if s.startswith("/"):
            self.pathname = True
            s = s[1:]
        if "/" in s:
            self.pathname = True
        self.pattern = s
        self.regex = re.compile("^" + _wildmatch_to_regex(s) + "$")

    def match(self, rel: str, is_dir: bool) -> bool:
        sub = self._relative_path(rel)
        if not sub:
            return False
        if self._match_path(sub, is_dir):
            return True

        # A pattern that matches a directory also applies to everything below
        # that directory.  This is what makes "build/" ignore "build/out.o".
        parts = sub.split("/")
        for i in range(1, len(parts)):
            parent = "/".join(parts[:i])
            if self._match_path(parent, True):
                return True
        return False

    def _relative_path(self, rel: str) -> str | None:
        if self.base:
            if rel == self.base:
                return None
            prefix = self.base + "/"
            if not rel.startswith(prefix):
                return None
            return rel[len(prefix) :]
        return rel

    def _match_path(self, sub: str, is_dir: bool) -> bool:
        if self.dir_only and not is_dir:
            return False
        if self.pathname:
            return self.regex.match(sub) is not None
        if "/" in sub:
            sub = sub.rsplit("/", 1)[-1]
        return self.regex.match(sub) is not None


class IgnoreSet:
    def __init__(self) -> None:
        self.rules: list[IgnoreRule] = []

    def add_file(self, gitignore_path: Path, base_rel: str) -> None:
        if not gitignore_path.exists() or gitignore_path.is_symlink():
            return
        for line in gitignore_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = _trim_trailing_spaces(line)
            if not line or line.startswith("#"):
                continue
            self.rules.append(IgnoreRule(line, base_rel))

    def is_ignored(self, rel_path: str, is_dir: bool = False) -> bool:
        rel_path, is_dir = _normalize_path(rel_path, is_dir)
        if not rel_path:
            return False
        parts = rel_path.split("/")
        for i in range(1, len(parts)):
            if self._last_match("/".join(parts[:i]), True):
                return True
        return self._last_match(rel_path, is_dir)

    def _last_match(self, rel_path: str, is_dir: bool) -> bool:
        ignored = False
        for r in self.rules:
            if r.match(rel_path, is_dir):
                ignored = not r.negate
        return ignored


def load(repo_path: Path) -> IgnoreSet:
    s = IgnoreSet()
    # Lower-precedence excludes are loaded first; later matches override them.
    s.add_file(repo_path / ".git" / "info" / "exclude", "")
    s.add_file(repo_path / ".gitignore", "")
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d != ".git"]
        if ".gitignore" in files:
            rel_root = os.path.relpath(root, repo_path).replace(os.sep, "/")
            if rel_root == ".":
                continue
            s.add_file(Path(root) / ".gitignore", rel_root)
    return s


def _normalize_path(path: str, is_dir: bool) -> tuple[str, bool]:
    path = path.replace(os.sep, "/")
    if path.endswith("/"):
        is_dir = True
        path = path.rstrip("/")
    parts = [p for p in path.split("/") if p and p != "."]
    return "/".join(parts), is_dir


def _trim_trailing_spaces(line: str) -> str:
    last_space: int | None = None
    i = 0
    while i < len(line):
        c = line[i]
        if c == " ":
            if last_space is None:
                last_space = i
        elif c == "\\":
            i += 1
            if i >= len(line):
                return line
            last_space = None
        else:
            last_space = None
        i += 1
    if last_space is not None:
        return line[:last_space]
    return line


def _wildmatch_to_regex(pattern: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "*":
            start = i
            while i < len(pattern) and pattern[i] == "*":
                i += 1
            count = i - start
            prev_is_sep = start == 0 or pattern[start - 1] == "/"
            next_is_sep = i == len(pattern) or pattern[i] == "/"
            if count >= 2 and prev_is_sep and next_is_sep:
                if i < len(pattern) and pattern[i] == "/":
                    out.append("(?:.*/)?")
                    i += 1
                else:
                    out.append(".*")
            else:
                out.append("[^/]*")
            continue
        if c == "?":
            out.append("[^/]")
        elif c == "[":
            token, end = _translate_char_class(pattern, i)
            out.append(token)
            i = end
            continue
        elif c == "\\":
            i += 1
            if i >= len(pattern):
                return r"(?!)"
            out.append(re.escape(pattern[i]))
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)


def _translate_char_class(pattern: str, start: int) -> tuple[str, int]:
    i = start + 1
    if i >= len(pattern):
        return r"\[", start + 1

    negate = False
    if pattern[i] in ("!", "^"):
        negate = True
        i += 1
    if i < len(pattern) and pattern[i] == "]":
        i += 1

    body = ""
    while i < len(pattern) and pattern[i] != "]":
        if pattern[i] == "\\" and i + 1 < len(pattern):
            i += 1
            body += re.escape(pattern[i])
        else:
            body += pattern[i]
        i += 1
    if i >= len(pattern):
        return r"\[", start + 1

    body = body.replace("\\", r"\\")
    if negate:
        token = f"(?:(?!/)[^{body}])"
    else:
        token = f"(?:(?!/)[{body}])"
    return token, i + 1
