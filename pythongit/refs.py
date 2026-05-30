"""Reference resolution and update.

Refs may live as loose files (.git/refs/...) or in packed-refs.
HEAD may be a symref ("ref: refs/heads/main") or a detached sha.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from .repo import Repository


SHA_LEN = 40


def _is_sha(s: str, hex_len: int = SHA_LEN) -> bool:
    return len(s) == hex_len and all(c in "0123456789abcdef" for c in s.lower())


def read_packed_refs(repo: Repository) -> dict[str, str]:
    f = repo.gitdir / "packed-refs"
    out: dict[str, str] = {}
    if not f.exists():
        return out
    for line in f.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or line.startswith("^"):
            continue
        sha, _, name = line.partition(" ")
        out[name] = sha
    return out


def read_ref(repo: Repository, name: str) -> Optional[str]:
    """Return the SHA the ref ultimately points at, following symrefs."""
    seen: set[str] = set()
    cur = name
    while True:
        if cur in seen:
            return None
        seen.add(cur)
        p = repo.gitdir / cur
        if p.exists():
            txt = p.read_text(encoding="utf-8").strip()
        else:
            packed = read_packed_refs(repo)
            if cur in packed:
                return packed[cur]
            return None
        if txt.startswith("ref: "):
            cur = txt[5:].strip()
            continue
        return txt if _is_sha(txt, repo.hex_len) else None


def update_ref(repo: Repository, name: str, sha: str, *, message: str = "") -> None:
    if not _is_sha(sha, repo.hex_len):
        raise ValueError(f"not a sha: {sha}")
    old = read_ref(repo, name) or repo.null_oid()
    p = repo.gitdir / name
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(sha + "\n", encoding="utf-8")
    os.replace(tmp, p)
    # reflog
    if name.startswith("refs/heads/") or name == "HEAD" or name.startswith("refs/remotes/") or name == "refs/stash":
        from . import reflog as _reflog
        _reflog.append(repo, name, old, sha, message or f"update: {name}")
        # also log HEAD if it points at this ref
        try:
            sym = (repo.gitdir / "HEAD").read_text(encoding="utf-8").strip()
            if sym == f"ref: {name}":
                _reflog.append(repo, "HEAD", old, sha, message or f"update: {name}")
        except FileNotFoundError:
            pass


def delete_ref(repo: Repository, name: str) -> None:
    p = repo.gitdir / name
    if p.exists():
        p.unlink()


def read_head(repo: Repository) -> tuple[Optional[str], Optional[str]]:
    """Return (symbolic_ref_or_None, sha_or_None)."""
    p = repo.gitdir / "HEAD"
    if not p.exists():
        return None, None
    txt = p.read_text(encoding="utf-8").strip()
    if txt.startswith("ref: "):
        ref = txt[5:].strip()
        return ref, read_ref(repo, ref)
    return None, txt if _is_sha(txt, repo.hex_len) else None


def set_head(repo: Repository, target: str) -> None:
    p = repo.gitdir / "HEAD"
    if target.startswith("refs/"):
        p.write_text(f"ref: {target}\n", encoding="utf-8")
    elif _is_sha(target, repo.hex_len):
        p.write_text(target + "\n", encoding="utf-8")
    else:
        # branch shorthand
        p.write_text(f"ref: refs/heads/{target}\n", encoding="utf-8")


def list_branches(repo: Repository) -> list[str]:
    root = repo.gitdir / "refs" / "heads"
    found: set[str] = set()
    if root.exists():
        for f in root.rglob("*"):
            if f.is_file():
                found.add(str(f.relative_to(root)).replace(os.sep, "/"))
    for ref in read_packed_refs(repo):
        if ref.startswith("refs/heads/"):
            found.add(ref[len("refs/heads/") :])
    return sorted(found)


def list_tags(repo: Repository) -> list[str]:
    root = repo.gitdir / "refs" / "tags"
    found: set[str] = set()
    if root.exists():
        for f in root.rglob("*"):
            if f.is_file():
                found.add(str(f.relative_to(root)).replace(os.sep, "/"))
    for ref in read_packed_refs(repo):
        if ref.startswith("refs/tags/"):
            found.add(ref[len("refs/tags/") :])
    return sorted(found)


def rev_parse(repo: Repository, name: str) -> Optional[str]:
    """Resolve a rev-ish to a full SHA.

    Accepts: full sha, abbreviated sha (>=4), HEAD, branch, tag,
    refs/heads/x, refs/tags/x, refs/remotes/x.
    """
    name = name.strip()
    if name == "HEAD":
        _, sha = read_head(repo)
        return sha
    for candidate in (
        name,
        f"refs/heads/{name}",
        f"refs/tags/{name}",
        f"refs/remotes/{name}",
    ):
        sha = read_ref(repo, candidate)
        if sha:
            return sha
    # raw / abbreviated sha
    low = name.lower()
    if _is_sha(low, repo.hex_len):
        return low
    if 4 <= len(low) <= repo.hex_len and all(c in "0123456789abcdef" for c in low):
        # search loose objects through the persistent loose-object cache
        from . import loose as _loose
        loose_match = _loose.resolve_short(repo, low)
        if loose_match is None:
            return None
        if loose_match:
            return loose_match
        # search packs
        from . import pack as _pack
        m = _pack.resolve_short(repo, low)
        if m:
            return m
    return None
