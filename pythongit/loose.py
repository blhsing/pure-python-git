"""Loose-object enumeration cache.

Git stores loose objects as files under ``objects/xx/yyyy...``. Looking up one
known object is cheap, but commands such as ``count-objects`` and abbreviated
OID resolution need to enumerate many loose objects. This module keeps a small
Git-ignored cache under ``objects/info`` and validates it using the fanout
directory mtimes/sizes, so cold commands avoid repeating full directory walks
when the loose-object set has not changed.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Optional

from .repo import Repository


@dataclass(frozen=True)
class LooseEntry:
    sha: str
    size: int


_CACHE_NAME = "pygit-loose-cache-v1"
_MEM_CACHE: dict[Path, tuple[tuple[tuple[str, int, int], ...], list[LooseEntry]]] = {}


def _cache_path(repo: Repository) -> Path:
    return repo.gitdir / "objects" / "info" / _CACHE_NAME


def _dir_signature(repo: Repository) -> tuple[tuple[str, int, int], ...]:
    obj_root = repo.gitdir / "objects"
    rows: list[tuple[str, int, int]] = []
    if not obj_root.is_dir():
        return tuple()
    try:
        st = obj_root.stat()
        rows.append((".", st.st_mtime_ns, st.st_size))
    except OSError:
        return tuple()
    for i in range(256):
        name = f"{i:02x}"
        d = obj_root / name
        try:
            st = d.stat()
        except FileNotFoundError:
            continue
        except OSError:
            continue
        if d.is_dir():
            rows.append((name, st.st_mtime_ns, st.st_size))
    return tuple(rows)


def _scan_entries(repo: Repository) -> list[LooseEntry]:
    obj_root = repo.gitdir / "objects"
    suffix_len = repo.hex_len - 2
    entries: list[LooseEntry] = []
    if not obj_root.is_dir():
        return entries
    for i in range(256):
        dirname = f"{i:02x}"
        d = obj_root / dirname
        if not d.is_dir():
            continue
        try:
            with os.scandir(d) as it:
                for item in it:
                    if not item.is_file():
                        continue
                    name = item.name
                    if len(name) != suffix_len or any(c not in "0123456789abcdef" for c in name.lower()):
                        continue
                    try:
                        size = item.stat().st_size
                    except OSError:
                        size = 0
                    entries.append(LooseEntry(dirname + name.lower(), size))
        except OSError:
            continue
    entries.sort(key=lambda e: e.sha)
    return entries


def _read_disk_cache(repo: Repository, sig: tuple[tuple[str, int, int], ...]) -> Optional[list[LooseEntry]]:
    path = _cache_path(repo)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if payload.get("version") != 1 or payload.get("object_format") != repo.object_format():
        return None
    if payload.get("signature") != [list(row) for row in sig]:
        return None
    entries_raw = payload.get("entries")
    if not isinstance(entries_raw, list):
        return None
    entries: list[LooseEntry] = []
    for row in entries_raw:
        if (
            isinstance(row, list)
            and len(row) == 2
            and isinstance(row[0], str)
            and isinstance(row[1], int)
            and len(row[0]) == repo.hex_len
        ):
            entries.append(LooseEntry(row[0], row[1]))
    return entries


def _write_disk_cache(
    repo: Repository,
    sig: tuple[tuple[str, int, int], ...],
    entries: list[LooseEntry],
) -> None:
    path = _cache_path(repo)
    payload = {
        "version": 1,
        "object_format": repo.object_format(),
        "signature": [list(row) for row in sig],
        "entries": [[entry.sha, entry.size] for entry in entries],
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def entries(repo: Repository) -> list[LooseEntry]:
    sig = _dir_signature(repo)
    cached = _MEM_CACHE.get(repo.gitdir)
    if cached and cached[0] == sig:
        return cached[1]
    disk_entries = _read_disk_cache(repo, sig)
    if disk_entries is not None:
        _MEM_CACHE[repo.gitdir] = (sig, disk_entries)
        return disk_entries
    before = sig
    scanned = _scan_entries(repo)
    after = _dir_signature(repo)
    if after == before:
        _write_disk_cache(repo, after, scanned)
        sig = after
    _MEM_CACHE[repo.gitdir] = (sig, scanned)
    return scanned


def iter_shas(repo: Repository):
    for entry in entries(repo):
        yield entry.sha


def count_and_size(repo: Repository) -> tuple[int, int]:
    cached = entries(repo)
    return len(cached), sum(entry.size for entry in cached)


def resolve_short(repo: Repository, prefix: str) -> Optional[str]:
    """Resolve a loose abbreviated OID.

    Returns ``""`` when no loose object matches, ``None`` when the prefix is
    ambiguous, and the full OID when exactly one loose object matches.
    """
    matches: list[str] = []
    for entry in entries(repo):
        if entry.sha.startswith(prefix):
            matches.append(entry.sha)
            if len(matches) > 1:
                return None
    return matches[0] if matches else ""


def clear_cache(repo: Repository) -> None:
    _MEM_CACHE.pop(repo.gitdir, None)
