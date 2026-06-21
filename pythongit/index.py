"""Git index (staging area), version 2.

Layout:
  header: 'DIRC' + version(uint32) + count(uint32)
  entries:
    ctime_s, ctime_n, mtime_s, mtime_n, dev, ino, mode, uid, gid, size  (10 * uint32)
    object id (20 bytes for SHA-1, 32 bytes for SHA-256)
    flags(uint16)  -- low 12 bits = path length
    path (NUL terminated, padded so total entry length is multiple of 8)
  extensions ...
  object-format hash trailer over the preceding bytes.
"""
from __future__ import annotations

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
    # On-disk extended (v3) flags: CE_INTENT_TO_ADD 0x2000, CE_SKIP_WORKTREE 0x4000.
    extended_flags: int = 0

    def mode_str(self) -> str:
        return f"{self.mode:06o}"

    @property
    def stage(self) -> int:
        return (self.flags >> 12) & 0x3

    @stage.setter
    def stage(self, value: int) -> None:
        self.flags = (self.flags & ~0x3000) | ((value & 0x3) << 12)

    @property
    def intent_to_add(self) -> bool:
        return bool(self.extended_flags & 0x2000)

    @intent_to_add.setter
    def intent_to_add(self, value: bool) -> None:
        self.extended_flags = (self.extended_flags | 0x2000) if value else (self.extended_flags & ~0x2000)

    @property
    def skip_worktree(self) -> bool:
        return bool(self.extended_flags & 0x4000)

    @skip_worktree.setter
    def skip_worktree(self, value: bool) -> None:
        self.extended_flags = (self.extended_flags | 0x4000) if value else (self.extended_flags & ~0x4000)


@dataclass
class Index:
    version: int = 2
    entries: list[IndexEntry] = field(default_factory=list)
    # Resolve-undo (REUC) extension: path -> {stage: (mode, sha)} for stages
    # 1/2/3 that were present before a conflict was resolved. mode 0 means the
    # stage was absent. Mirrors C git's resolve_undo string-list keyed by path.
    resolve_undo: dict[str, dict[int, tuple[int, str]]] = field(default_factory=dict)

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

    def record_resolve_undo(self, entry: IndexEntry) -> None:
        """Record a conflicted (stage 1/2/3) entry into resolve-undo before it
        is dropped by a resolution. Stage-0 entries are ignored, matching C
        git's record_resolve_undo()."""
        stage = entry.stage
        if not stage:
            return
        rec = self.resolve_undo.setdefault(entry.path, {1: (0, ""), 2: (0, ""), 3: (0, "")})
        rec[stage] = (entry.mode, entry.sha)


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
        hash_len = repo.hash_len
        head_len = 40 + hash_len + 2
        fields = struct.unpack(
            f">10I{hash_len}sH",
            raw[pos : pos + head_len],
        )
        (cts, ctn, mts, mtn, dev, ino, mode, uid, gid, size, sha_b, flags) = fields
        pos += head_len
        # CE_EXTENDED (v3+): a second 16-bit flags word precedes the path.
        ext_flags = 0
        if flags & 0x4000:
            ext_flags = struct.unpack(">H", raw[pos:pos + 2])[0]
            pos += 2
            flags &= ~0x4000  # CE_EXTENDED is a serialization detail, not stored
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
                sha_b.hex(), flags, path, ext_flags,
            )
        )
    # Extensions follow the entries, terminated by the hash trailer. Each is a
    # 4-byte signature + 4-byte big-endian size + payload. We understand REUC
    # (resolve-undo); any other (optional, capital-letter) extension is skipped.
    trailer = len(raw) - repo.hash_len
    while pos + 8 <= trailer:
        sig = raw[pos:pos + 4]
        sz = struct.unpack(">I", raw[pos + 4:pos + 8])[0]
        body = raw[pos + 8:pos + 8 + sz]
        pos += 8 + sz
        if sig == b"REUC":
            idx.resolve_undo = _read_reuc(body, repo.hash_len)
    return idx


def _read_reuc(data: bytes, rawsz: int) -> dict[str, dict[int, tuple[int, str]]]:
    """Parse the REUC payload into {path: {stage: (mode, sha)}}."""
    out: dict[str, dict[int, tuple[int, str]]] = {}
    i = 0
    n = len(data)
    while i < n:
        end = data.index(b"\0", i)
        path = data[i:end].decode("utf-8", errors="replace")
        i = end + 1
        modes = [0, 0, 0]
        for s in range(3):
            mend = data.index(b"\0", i)
            modes[s] = int(data[i:mend] or b"0", 8)
            i = mend + 1
        rec = {1: (0, ""), 2: (0, ""), 3: (0, "")}
        for s in range(3):
            if not modes[s]:
                continue
            sha = data[i:i + rawsz].hex()
            i += rawsz
            rec[s + 1] = (modes[s], sha)
        out[path] = rec
    return out


def write_index(repo: Repository, idx: Index) -> None:
    buf = bytearray()
    # The index is upgraded to v3 only when an entry carries extended flags
    # (intent-to-add / skip-worktree); otherwise it stays byte-identical v2.
    version = 3 if any(e.extended_flags for e in idx.entries) else 2
    buf += b"DIRC" + struct.pack(">II", version, len(idx.entries))
    idx.entries.sort(key=lambda e: (e.path, e.stage))
    for e in idx.entries:
        start = len(buf)
        path_bytes = e.path.encode("utf-8")
        # Preserve stage (0x3000) and assume-valid (0x8000); CE_EXTENDED (0x4000)
        # is derived solely from extended_flags so a cleared flag drops the word.
        flags = (e.flags & 0xB000) | min(len(path_bytes), 0x0FFF)
        if e.extended_flags:
            flags |= 0x4000  # CE_EXTENDED
        buf += struct.pack(
            f">10I{repo.hash_len}sH",
            e.ctime_s & 0xFFFFFFFF, e.ctime_n & 0xFFFFFFFF,
            e.mtime_s & 0xFFFFFFFF, e.mtime_n & 0xFFFFFFFF,
            e.dev & 0xFFFFFFFF, e.ino & 0xFFFFFFFF,
            e.mode, e.uid & 0xFFFFFFFF, e.gid & 0xFFFFFFFF,
            e.size & 0xFFFFFFFF,
            bytes.fromhex(e.sha), flags,
        )
        if e.extended_flags:
            buf += struct.pack(">H", e.extended_flags & 0xFFFF)
        buf += path_bytes + b"\0"
        while (len(buf) - start) % 8 != 0:
            buf += b"\0"
    # REUC (resolve-undo) extension, when present. C git records it sorted by
    # path; our dict is built from the (sorted) index so iterate sorted to be
    # byte-stable.
    if idx.resolve_undo:
        body = bytearray()
        for path in sorted(idx.resolve_undo):
            rec = idx.resolve_undo[path]
            body += path.encode("utf-8") + b"\0"
            for s in (1, 2, 3):
                body += b"%o\0" % (rec.get(s, (0, ""))[0])
            for s in (1, 2, 3):
                mode, sha = rec.get(s, (0, ""))
                if not mode:
                    continue
                body += bytes.fromhex(sha)
        buf += b"REUC" + struct.pack(">I", len(body)) + bytes(body)
    buf += repo.hash_bytes(buf)
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
