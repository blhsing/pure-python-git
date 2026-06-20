"""Mailmap parsing and identity remapping — a port of git's mailmap.c.

A ``.mailmap`` file rewrites author/committer identities. Four line forms are
supported (``name1``/``email1`` are the *proper* identity, ``email2``/``name2``
identify the commit identity to rewrite)::

    Proper Name <proper@email>                              # remap name by email
    <proper@email> <commit@email>                           # remap email by email
    Proper Name <proper@email> <commit@email>               # remap name+email by email
    Proper Name <proper@email> Commit Name <commit@email>   # remap by (email, name)

Lookups are keyed on the commit email (case-insensitively); a per-email map of
commit names provides the more specific form-4 matches.
"""
from __future__ import annotations

from typing import Optional

from .repo import Repository


def _parse_name_and_email(buf: str, allow_empty_email: bool):
    """Return (name, email, rest) for the leading ``Name <email>`` of ``buf``.

    ``name`` is whitespace-trimmed (None if empty); ``rest`` is the text after
    the closing ``>`` (None if nothing follows). Returns (None, None, None) when
    there is no well-formed ``<email>``."""
    left = buf.find("<")
    if left < 0:
        return None, None, None
    right = buf.find(">", left + 1)
    if right < 0:
        return None, None, None
    if not allow_empty_email and left + 1 == right:
        return None, None, None
    name = buf[:left].strip() or None
    email = buf[left + 1:right]
    rest = buf[right + 1:]
    return name, email, (rest if rest else None)


class Mailmap:
    def __init__(self) -> None:
        # commit-email(lower) -> (proper_name|None, proper_email|None)
        self.simple: dict[str, tuple[Optional[str], Optional[str]]] = {}
        # commit-email(lower) -> { commit-name(lower) -> (proper_name, proper_email) }
        self.complex: dict[str, dict[str, tuple[Optional[str], Optional[str]]]] = {}

    @property
    def empty(self) -> bool:
        return not self.simple and not self.complex

    def _add_line(self, line: str) -> None:
        if not line or line[0] == "#":
            return
        name1, email1, rest = _parse_name_and_email(line, False)
        name2 = email2 = None
        if rest is not None:
            name2, email2, _ = _parse_name_and_email(rest, True)
        if not email1:
            return
        commit_email = (email2 if email2 else email1).lower()
        if name2:
            self.complex.setdefault(commit_email, {})[name2.lower()] = (name1, email1)
        else:
            self.simple[commit_email] = (name1, email1)

    def resolve(self, name: str, email: str) -> tuple[str, str]:
        """Map (name, email) through the mailmap, returning the proper identity
        (or the input unchanged when there is no matching entry)."""
        key = email.lower()
        result: Optional[tuple[Optional[str], Optional[str]]] = None
        sub = self.complex.get(key)
        if sub is not None:
            result = sub.get(name.lower())
            if result is None:
                result = self.simple.get(key)  # fall back to the email default
        else:
            result = self.simple.get(key)
        if result is None:
            return name, email
        proper_name, proper_email = result
        return (proper_name or name), (proper_email or email)


def load(repo: Repository) -> Mailmap:
    """Read the repository's ``.mailmap`` (top-level working-tree file)."""
    mm = Mailmap()
    path = repo.path / ".mailmap"
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            mm._add_line(line)
    return mm
