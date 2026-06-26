"""Local (file path / file://) git transport for fetch and push.

Git's local transport connects to ``upload-pack`` / ``receive-pack`` running in
the other repository.  Because both ends live on this filesystem, we model the
exact observable behaviour (ref negotiation + object transfer + ref updates on
both sides) by operating directly on the two :class:`Repository` object stores.

The on-disk effect the parity harness checks is the refs/packed-refs of both
repositories, plus stdout/stderr.  This module provides the negotiation and the
object copy; the byte-exact status/display lines are produced by the callers in
``cli.py`` (mirroring transport.c / builtin/fetch.c / builtin/push.c).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from .repo import Repository
from . import objects as objs
from . import refs as refs_mod


# ---------------------------------------------------------------------------
# Remote URL handling
# ---------------------------------------------------------------------------

def is_local_url(url: str) -> bool:
    """Whether ``url`` names a local repository (path or ``file://``)."""
    if url.startswith("file://"):
        return True
    if "://" in url:
        return False
    if url.startswith("git@") or (":" in url and "/" not in url.split(":", 1)[0]
                                  and not url[1:2] == ":"):
        # scp-style host:path (no leading slash before colon) — not local
        # (a Windows drive "C:\..." is local but we don't target that here)
        return False
    return True


def local_path(url: str) -> str:
    """Return the filesystem path for a local remote URL."""
    if url.startswith("file://"):
        return url[len("file://"):]
    return url


def open_remote(url: str) -> Repository:
    """Open the bare/working repository referenced by a local URL."""
    return Repository.discover(local_path(url))


# ---------------------------------------------------------------------------
# Ref advertisement
# ---------------------------------------------------------------------------

def advertised_refs(remote: Repository) -> "dict[str, str]":
    """Refs the remote upload-pack/receive-pack would advertise.

    Returns full-refname -> hex-oid for all refs under ``refs/`` (heads, tags,
    remotes, ...).  ``HEAD`` is handled separately by callers.
    """
    out: dict[str, str] = {}
    for name, sha in refs_mod.iter_all_refs(remote).items():
        out[name] = sha
    return out


def remote_head_target(remote: Repository) -> Optional[str]:
    """The symref target of the remote's HEAD (e.g. ``refs/heads/main``)."""
    return refs_mod.read_symbolic(remote, "HEAD")


# ---------------------------------------------------------------------------
# Object transfer
# ---------------------------------------------------------------------------

def _reachable_objects(repo: Repository, tips: list[str],
                       stop: set[str]) -> list[str]:
    """All objects reachable from ``tips`` but not reachable from ``stop``.

    ``stop`` is a set of commit/tag oids whose closure is already present on the
    other side; we walk it first to prime ``seen`` so those objects (and their
    trees/blobs) are not re-sent.
    """
    seen: set[str] = set()
    # Prime with the closure of the stop set so shared history is excluded.
    stack = list(stop)
    while stack:
        sha = stack.pop()
        if sha in seen:
            continue
        seen.add(sha)
        try:
            t, data = objs.read_object(repo, sha)
        except KeyError:
            continue
        _push_children(repo, t, data, stack)

    out: list[str] = []
    stack = list(tips)
    pending: set[str] = set()
    while stack:
        sha = stack.pop()
        if sha in seen or sha in pending:
            continue
        try:
            t, data = objs.read_object(repo, sha)
        except KeyError:
            continue
        pending.add(sha)
        out.append(sha)
        _push_children(repo, t, data, stack)
    return out


def _push_children(repo: Repository, t: str, data: bytes, stack: list[str]) -> None:
    if t == "commit":
        c = objs.parse_commit(data)
        stack.append(c.tree)
        stack.extend(c.parents)
    elif t == "tree":
        for e in objs.parse_tree(data, repo.hash_len):
            stack.append(e.sha)
    elif t == "tag":
        tgt = refs_mod._tag_target(data)
        if tgt:
            stack.append(tgt)


def copy_objects(src: Repository, dst: Repository, tips: list[str],
                 have: set[str]) -> int:
    """Copy all objects reachable from ``tips`` (minus ``have`` closure) from
    ``src`` into ``dst``.  Returns the number of objects written.

    Objects are written as loose objects in the destination.  ``have`` is the
    set of oids already present at the destination that bound the walk.
    """
    # Bound the walk by what the destination already has reachable from its refs.
    shas = _reachable_objects(src, tips, have)
    count = 0
    for sha in shas:
        if objs.object_exists(dst, sha):
            continue
        t, data = objs.read_object(src, sha)
        objs.write_object(dst, t, data)
        count += 1
    return count


def destination_have(repo: Repository) -> set[str]:
    """Commit/tag tips already present in ``repo`` to bound object walks."""
    have: set[str] = set()
    for name, sha in refs_mod.iter_all_refs(repo).items():
        have.add(sha)
    return have


# ---------------------------------------------------------------------------
# Refspec parsing (refspec.c parse_refspec / match)
# ---------------------------------------------------------------------------

class Refspec:
    __slots__ = ("force", "src", "dst", "pattern", "matching", "negative",
                 "delete", "raw", "exact_sha1")

    def __init__(self, raw: str):
        self.raw = raw
        self.force = False
        self.negative = False
        self.matching = False   # the lone ":" (push matching refs)
        self.delete = False
        self.pattern = False
        self.exact_sha1 = False
        self.src: Optional[str] = None
        self.dst: Optional[str] = None
        s = raw
        if s.startswith("^"):
            self.negative = True
            self.src = s[1:]
            return
        if s.startswith("+"):
            self.force = True
            s = s[1:]
        if ":" in s:
            src, dst = s.split(":", 1)
            self.src = src
            self.dst = dst
            if src == "" and dst == "":
                self.matching = True
        else:
            self.src = s
            self.dst = None
        if self.src is not None and "*" in self.src:
            self.pattern = True


def parse_refspecs(specs: list[str]) -> list[Refspec]:
    return [Refspec(s) for s in specs]

