"""gitignore parsing.

Each directory may have its own .gitignore. Patterns:
  - lines starting with # are comments
  - leading ! negates
  - trailing / matches directories only
  - leading / anchors to the .gitignore's directory
  - ** matches any number of path components
"""
from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path
from typing import Optional


class IgnoreRule:
    __slots__ = ("pattern", "negate", "dir_only", "anchored", "base", "regex")

    def __init__(self, raw: str, base: str):
        self.negate = False
        self.dir_only = False
        self.anchored = False
        self.base = base
        s = raw
        if s.startswith("!"):
            self.negate = True
            s = s[1:]
        if s.endswith("/"):
            self.dir_only = True
            s = s[:-1]
        if s.startswith("/"):
            self.anchored = True
            s = s[1:]
        self.pattern = s
        # Translate glob to regex anchored to base
        regex = ""
        i = 0
        while i < len(s):
            c = s[i]
            if c == "*" and i + 1 < len(s) and s[i + 1] == "*":
                # **
                regex += ".*"
                i += 2
                if i < len(s) and s[i] == "/":
                    i += 1
            elif c == "*":
                regex += "[^/]*"
                i += 1
            elif c == "?":
                regex += "[^/]"
                i += 1
            elif c in ".(){}+|^$\\":
                regex += "\\" + c
                i += 1
            elif c == "[":
                end = s.find("]", i)
                if end == -1:
                    regex += "\\["
                    i += 1
                else:
                    regex += s[i : end + 1]
                    i = end + 1
            else:
                regex += c
                i += 1
        self.regex = re.compile("^" + regex + "$")

    def match(self, rel: str, is_dir: bool) -> bool:
        if self.dir_only and not is_dir:
            return False
        target = rel
        if self.anchored:
            if self.base and not target.startswith(self.base + "/") and target != self.base:
                return False
            sub = target[len(self.base) + 1 :] if self.base else target
            return self.regex.match(sub) is not None
        # match against any suffix
        if self.regex.match(target):
            return True
        for i, ch in enumerate(target):
            if ch == "/" and self.regex.match(target[i + 1 :]):
                return True
        # also match against final component
        last = target.rsplit("/", 1)[-1]
        return self.regex.match(last) is not None


class IgnoreSet:
    def __init__(self) -> None:
        self.rules: list[IgnoreRule] = []

    def add_file(self, gitignore_path: Path, base_rel: str) -> None:
        if not gitignore_path.exists():
            return
        for line in gitignore_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.rstrip()
            if not line or line.startswith("#"):
                continue
            self.rules.append(IgnoreRule(line, base_rel))

    def is_ignored(self, rel_path: str, is_dir: bool = False) -> bool:
        ignored = False
        for r in self.rules:
            if r.match(rel_path, is_dir):
                ignored = not r.negate
        return ignored


def load(repo_path: Path) -> IgnoreSet:
    s = IgnoreSet()
    # Root .gitignore + .git/info/exclude
    s.add_file(repo_path / ".gitignore", "")
    s.add_file(repo_path / ".git" / "info" / "exclude", "")
    # Walk for nested .gitignore files
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d != ".git"]
        if ".gitignore" in files:
            rel_root = os.path.relpath(root, repo_path).replace(os.sep, "/")
            if rel_root == ".":
                continue
            s.add_file(Path(root) / ".gitignore", rel_root)
    return s
