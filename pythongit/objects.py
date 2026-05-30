"""Git object storage: blob, tree, commit, tag.

Loose objects are stored zlib-compressed under .git/objects/xx/yyyy...
Pack objects are read transparently by `read_object` via the pack module.
"""
from __future__ import annotations

import hashlib
import os
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from .repo import Repository


# ---------------------------------------------------------------------------
# low-level loose object I/O


def _loose_path(repo: Repository, sha: str) -> Path:
    return repo.gitdir / "objects" / sha[:2] / sha[2:]


def hash_bytes(obj_type: str, data: bytes, repo: Optional[Repository] = None) -> tuple[str, bytes]:
    """Return (object id, serialized) for a raw object payload."""
    header = f"{obj_type} {len(data)}".encode() + b"\0"
    full = header + data
    if repo is not None:
        return repo.hash_hex(full), full
    return hashlib.sha1(full).hexdigest(), full


def write_object(repo: Repository, obj_type: str, data: bytes) -> str:
    sha, full = hash_bytes(obj_type, data, repo)
    p = _loose_path(repo, sha)
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_bytes(zlib.compress(full))
        os.replace(tmp, p)
        try:
            from . import loose as _loose

            _loose.clear_cache(repo)
        except Exception:
            pass
    return sha


def read_object(repo: Repository, sha: str) -> tuple[str, bytes]:
    """Return (type, payload). Looks in loose objects then packs."""
    p = _loose_path(repo, sha)
    if p.exists():
        raw = zlib.decompress(p.read_bytes())
        nul = raw.index(b"\0")
        header = raw[:nul].decode()
        obj_type, _, size = header.partition(" ")
        data = raw[nul + 1 :]
        if int(size) != len(data):
            raise ValueError(f"object {sha} size mismatch")
        return obj_type, data
    # try packs
    from . import pack as _pack
    res = _pack.find_in_packs(repo, sha)
    if res is None:
        raise KeyError(sha)
    return res


def object_exists(repo: Repository, sha: str) -> bool:
    if _loose_path(repo, sha).exists():
        return True
    from . import pack as _pack
    return _pack.find_in_packs(repo, sha) is not None


# ---------------------------------------------------------------------------
# tree


@dataclass
class TreeEntry:
    mode: str  # e.g. "100644", "100755", "120000", "40000"
    name: str
    sha: str

    def is_dir(self) -> bool:
        return self.mode in ("40000", "040000")

    def is_gitlink(self) -> bool:
        return self.mode == "160000"


def parse_tree(data: bytes, hash_len: int = 20) -> list[TreeEntry]:
    entries: list[TreeEntry] = []
    i = 0
    while i < len(data):
        sp = data.index(b" ", i)
        mode = data[i:sp].decode()
        nul = data.index(b"\0", sp)
        name = data[sp + 1 : nul].decode("utf-8", errors="replace")
        sha = data[nul + 1 : nul + 1 + hash_len].hex()
        entries.append(TreeEntry(mode, name, sha))
        i = nul + 1 + hash_len
    return entries


def encode_tree(entries: Iterable[TreeEntry]) -> bytes:
    # Git sorts tree entries by name with directories effectively having a
    # trailing slash for comparison purposes.
    def key(e: TreeEntry) -> bytes:
        suffix = b"/" if e.is_dir() else b""
        return e.name.encode("utf-8") + suffix

    out = bytearray()
    for e in sorted(entries, key=key):
        out += e.mode.lstrip("0").encode() or b"0"
        out += b" " + e.name.encode("utf-8") + b"\0" + bytes.fromhex(e.sha)
    return bytes(out)


# ---------------------------------------------------------------------------
# commit / tag


@dataclass
class Commit:
    tree: str
    parents: list[str] = field(default_factory=list)
    author: str = ""
    committer: str = ""
    message: str = ""

    def encode(self) -> bytes:
        lines = [f"tree {self.tree}"]
        for p in self.parents:
            lines.append(f"parent {p}")
        lines.append(f"author {self.author}")
        lines.append(f"committer {self.committer}")
        header = "\n".join(lines) + "\n\n"
        return header.encode("utf-8") + self.message.encode("utf-8")


def parse_commit(data: bytes) -> Commit:
    text = data.decode("utf-8", errors="replace")
    head, _, msg = text.partition("\n\n")
    c = Commit(tree="", message=msg)
    for line in head.splitlines():
        if not line:
            continue
        key, _, val = line.partition(" ")
        if key == "tree":
            c.tree = val
        elif key == "parent":
            c.parents.append(val)
        elif key == "author":
            c.author = val
        elif key == "committer":
            c.committer = val
    return c


def format_signature(name: str, email: str, *, when: Optional[int] = None, tz_minutes: int = 0) -> str:
    import time
    if when is None:
        when = int(time.time())
    sign = "+" if tz_minutes >= 0 else "-"
    tz_abs = abs(tz_minutes)
    return f"{name} <{email}> {when} {sign}{tz_abs // 60:02d}{tz_abs % 60:02d}"
