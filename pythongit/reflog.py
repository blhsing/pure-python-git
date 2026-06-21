"""Reflog: append-only log of ref updates.

File format (per Documentation/gitformat-reflog):
  <old-sha> SP <new-sha> SP <ident> TAB <message> LF
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from .repo import Repository


def _reflog_path(repo: Repository, ref: str) -> Path:
    return repo.gitdir / "logs" / ref


def append(repo: Repository, ref: str, old_sha: str, new_sha: str, message: str, *, ident: Optional[str] = None) -> None:
    if ident is None:
        # The reflog entry is stamped with the committer identity, honoring
        # GIT_COMMITTER_NAME/EMAIL/DATE and the local timezone, exactly like the
        # signature on the commit the update points to.
        from . import objects as _objs
        ident = _objs.build_signature(repo, "committer")
    p = _reflog_path(repo, ref)
    p.parent.mkdir(parents=True, exist_ok=True)
    line = f"{old_sha} {new_sha} {ident}\t{message}\n"
    with p.open("a", encoding="utf-8") as f:
        f.write(line)


def read(repo: Repository, ref: str) -> list[tuple[str, str, str, str]]:
    """Return list of (old, new, ident, message), oldest-first."""
    from . import reftable as _reftable
    if _reftable.is_reftable(repo.gitdir):
        store = _reftable.RefStore(repo.gitdir, hash_size=repo.hash_len)
        recs = [r for r in store._all_logs()
                if r.refname == ref and r.value_type == _reftable.LOG_UPDATE]
        # reftable stores newest first (reverse_int64 key order); the reflog
        # file is oldest-first, so present in ascending update_index order.
        recs.sort(key=lambda r: r.update_index)
        out = []
        for r in recs:
            # tz_offset is stored as the raw HHMM integer (git's
            # fill_reftable_log_record uses atoi of the HHMM string), so it maps
            # straight back to the "+HHMM" reflog timezone field.
            tz = r.tz_offset
            sign = "+" if tz >= 0 else "-"
            ident = f"{r.name} <{r.email}> {r.time} {sign}{abs(tz):04d}"
            msg = r.message
            if msg.endswith("\n"):
                msg = msg[:-1]
            out.append((r.old_hash.hex(), r.new_hash.hex(), ident, msg))
        return out
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
