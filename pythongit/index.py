"""Git index (staging area), version 2.

Layout:
  header: 'DIRC' + version(uint32) + count(uint32)
  entries:
    ctime_s, ctime_n, mtime_s, mtime_n, dev, ino, mode, uid, gid, size  (10 * uint32)
    sha1(20)
    flags(uint16)  -- low 12 bits = path length
    path (NUL terminated, padded so total entry length is multiple of 8)
  extensions ...
  SHA1 trailer over the preceding bytes.
"""
from __future__ import annotations

import hashlib
import os
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .repo import Repository


REG_MODE = 0o100644
EXE_MODE = 0o100755
SYM_MODE = 0o120000


@dataclass
class IndexEntry:
    ctime_s: int = 0
    ctime_n: int = 0
    mtime_s: int = 0
    mtime_n: int = 0
    dev: int = 0
    ino: int = 0
    mode: int = REG_MODE
    uid: int = 0
    gid: int = 0
    size: int = 0
    sha: str = "0" * 40
    flags: int = 0
    path: str = ""

    def mode_str(self) -> str:
        return f"{self.mode:06o}"

    @property
    def stage(self) -> int:
        return (self.flags >> 12) & 0x3

    @stage.setter
    def stage(self, value: int) -> None:
        self.flags = (self.flags & ~0x3000) | ((value & 0x3) << 12)


@dataclass
class Index:
    version: int = 2
    entries: list[IndexEntry] = field(default_factory=list)

    def by_path(self) -> dict[str, IndexEntry]:
        """Stage-0 entries by path (for convenience). Use `entries` for the
        full view when conflict stages are present."""
        return {e.path: e for e in self.entries if e.stage == 0}

    def by_path_all_stages(self) -> dict[str, dict[int, IndexEntry]]:
        out: dict[str, dict[int, IndexEntry]] = {}
        for e in self.entries:
            out.setdefault(e.path, {})[e.stage] = e
        return out

    def has_conflicts(self) -> bool:
        return any(e.stage != 0 for e in self.entries)

    def conflicted_paths(self) -> list[str]:
        return sorted({e.path for e in self.entries if e.stage != 0})

    def remove(self, path: str, *, stage: int | None = None) -> bool:
        kept = []
        removed = False
        for e in self.entries:
            if e.path == path and (stage is None or e.stage == stage):
                removed = True
                continue
            kept.append(e)
        self.entries = kept
        return removed

    def upsert(self, entry: IndexEntry) -> None:
        for i, e in enumerate(self.entries):
            if e.path == entry.path and e.stage == entry.stage:
                self.entries[i] = entry
                return
        self.entries.append(entry)
        self.entries.sort(key=lambda e: (e.path, e.stage))


# ---------------------------------------------------------------------------


def _index_path(repo: Repository) -> Path:
    return repo.gitdir / "index"


def read_index(repo: Repository) -> Index:
    p = _index_path(repo)
    if not p.exists():
        return Index()
    raw = p.read_bytes()
    if raw[:4] != b"DIRC":
        raise ValueError("not a git index (bad signature)")
    version, count = struct.unpack(">II", raw[4:12])
    if version not in (2, 3, 4):
        raise ValueError(f"unsupported index version {version}")
    idx = Index(version=version)
    pos = 12
    for _ in range(count):
        start = pos
        fields = struct.unpack(">10I20sH", raw[pos : pos + 62])
        (cts, ctn, mts, mtn, dev, ino, mode, uid, gid, size, sha_b, flags) = fields
        pos += 62
        name_len = flags & 0x0FFF
        if name_len < 0x0FFF:
            path = raw[pos : pos + name_len].decode("utf-8", errors="replace")
            pos += name_len
        else:
            end = raw.index(b"\0", pos)
            path = raw[pos:end].decode("utf-8", errors="replace")
            pos = end
        # advance past NUL + padding so (pos - start) is multiple of 8
        pos += 1
        while (pos - start) % 8 != 0:
            pos += 1
        idx.entries.append(
            IndexEntry(
                cts, ctn, mts, mtn, dev, ino, mode, uid, gid, size,
                sha_b.hex(), flags, path,
            )
        )
    return idx


def write_index(repo: Repository, idx: Index) -> None:
    buf = bytearray()
    buf += b"DIRC" + struct.pack(">II", 2, len(idx.entries))
    idx.entries.sort(key=lambda e: (e.path, e.stage))
    for e in idx.entries:
        start = len(buf)
        path_bytes = e.path.encode("utf-8")
        flags = (e.flags & 0xF000) | min(len(path_bytes), 0x0FFF)
        buf += struct.pack(
            ">10I20sH",
            e.ctime_s & 0xFFFFFFFF, e.ctime_n & 0xFFFFFFFF,
            e.mtime_s & 0xFFFFFFFF, e.mtime_n & 0xFFFFFFFF,
            e.dev & 0xFFFFFFFF, e.ino & 0xFFFFFFFF,
            e.mode, e.uid & 0xFFFFFFFF, e.gid & 0xFFFFFFFF,
            e.size & 0xFFFFFFFF,
            bytes.fromhex(e.sha), flags,
        )
        buf += path_bytes + b"\0"
        while (len(buf) - start) % 8 != 0:
            buf += b"\0"
    buf += hashlib.sha1(buf).digest()
    p = _index_path(repo)
    tmp = p.with_suffix(".tmp")
    tmp.write_bytes(bytes(buf))
    os.replace(tmp, p)


# ---------------------------------------------------------------------------


def stat_to_entry(path: str, st: os.stat_result, sha: str, mode: int) -> IndexEntry:
    return IndexEntry(
        ctime_s=int(st.st_ctime),
        ctime_n=int((st.st_ctime - int(st.st_ctime)) * 1e9),
        mtime_s=int(st.st_mtime),
        mtime_n=int((st.st_mtime - int(st.st_mtime)) * 1e9),
        dev=getattr(st, "st_dev", 0),
        ino=getattr(st, "st_ino", 0),
        mode=mode,
        uid=getattr(st, "st_uid", 0),
        gid=getattr(st, "st_gid", 0),
        size=st.st_size & 0xFFFFFFFF,
        sha=sha,
        flags=0,
        path=path,
    )
