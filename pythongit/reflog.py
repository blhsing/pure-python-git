"""Reflog: append-only log of ref updates.

File format (per Documentation/gitformat-reflog):
  <old-sha> SP <new-sha> SP <ident> TAB <message> LF
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional

from .repo import Repository


def _reflog_path(repo: Repository, ref: str) -> Path:
    return repo.gitdir / "logs" / ref


def append(repo: Repository, ref: str, old_sha: str, new_sha: str, message: str, *, ident: Optional[str] = None) -> None:
    if ident is None:
        name, email = repo.user()
        when = int(time.time())
        ident = f"{name} <{email}> {when} +0000"
    p = _reflog_path(repo, ref)
    p.parent.mkdir(parents=True, exist_ok=True)
    line = f"{old_sha} {new_sha} {ident}\t{message}\n"
    with p.open("a", encoding="utf-8") as f:
        f.write(line)


def read(repo: Repository, ref: str) -> list[tuple[str, str, str, str]]:
    """Return list of (old, new, ident, message)."""
    p = _reflog_path(repo, ref)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        head, _, msg = line.partition("\t")
        parts = head.split(" ", 2)
        if len(parts) < 3:
            continue
        out.append((parts[0], parts[1], parts[2], msg))
    return out
