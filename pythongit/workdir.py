"""Working directory operations: add, rm, status, write-tree, read-tree, checkout."""
from __future__ import annotations

import os
import stat as st_mod
from collections import OrderedDict
from pathlib import Path
from typing import Iterable, Iterator, Optional

from . import ignore as ignore_mod
from . import objects as objs
from . import refs as refs_mod
from .index import (
    EXE_MODE, REG_MODE, SYM_MODE,
    Index, IndexEntry, read_index, stat_to_entry, write_index,
)
from .repo import Repository


# ---------------------------------------------------------------------------
# helpers


def _rel(repo: Repository, path: Path) -> str:
    return str(path.resolve().relative_to(repo.path)).replace(os.sep, "/")


def _norm(p: str) -> str:
    return p.replace(os.sep, "/")


def _ignored(rel: str) -> bool:
    parts = rel.split("/")
    return ".git" in parts


def _is_dir_no_follow(path: Path) -> bool:
    try:
        return st_mod.S_ISDIR(path.lstat().st_mode)
    except FileNotFoundError:
        return False


def iter_worktree(repo: Repository) -> list[str]:
    out: list[str] = []
    base = repo.path
    for root, dirs, files in os.walk(base):
        rel_root = _norm(os.path.relpath(root, base))
        if rel_root == ".":
            rel_root = ""
        dirs[:] = [d for d in dirs if d != ".git"]
        for f in files:
            rel = f if not rel_root else f"{rel_root}/{f}"
            if not _ignored(rel):
                out.append(rel)
    return sorted(out)


def _mode_for(path: Path) -> int:
    try:
        st = path.lstat()
    except FileNotFoundError:
        return REG_MODE
    if st_mod.S_ISLNK(st.st_mode):
        return SYM_MODE
    if st.st_mode & 0o111:
        return EXE_MODE
    return REG_MODE


def _blob_data(path: Path) -> bytes:
    st = path.lstat()
    if st_mod.S_ISLNK(st.st_mode):
        return os.readlink(path).encode("utf-8")
    return path.read_bytes()


_TREE_ENTRY_CACHE_MAX = 8192
_TREE_ENTRY_CACHE: OrderedDict[tuple[Path, int, str], tuple[objs.TreeEntry, ...]] = OrderedDict()


def _tree_entries(repo: Repository, tree_sha: str) -> tuple[objs.TreeEntry, ...]:
    key = (repo.gitdir, repo.hash_len, tree_sha)
    cached = _TREE_ENTRY_CACHE.get(key)
    if cached is not None:
        _TREE_ENTRY_CACHE.move_to_end(key)
        return cached
    obj_type, data = objs.read_object(repo, tree_sha)
    if obj_type != "tree":
        entries: tuple[objs.TreeEntry, ...] = tuple()
    else:
        entries = tuple(objs.parse_tree(data, repo.hash_len))
    _TREE_ENTRY_CACHE[key] = entries
    if len(_TREE_ENTRY_CACHE) > _TREE_ENTRY_CACHE_MAX:
        _TREE_ENTRY_CACHE.popitem(last=False)
    return entries


def tree_path_entry(repo: Repository, tree_sha: str, path: str) -> Optional[objs.TreeEntry]:
    """Return the tree entry at ``path`` without flattening the whole tree."""
    parts = [p for p in path.replace("\\", "/").strip("/").split("/") if p and p != "."]
    if not parts:
        return None
    cur_tree = tree_sha
    for idx, part in enumerate(parts):
        found = None
        for entry in _tree_entries(repo, cur_tree):
            if entry.name == part:
                found = entry
                break
        if found is None:
            return None
        if idx == len(parts) - 1:
            return found
        if not found.is_dir():
            return None
        cur_tree = found.sha
    return None


# ---------------------------------------------------------------------------
# add / rm


def add_paths(repo: Repository, paths: Iterable[str]) -> None:
    idx = read_index(repo)
    tracked = set(idx.by_path())
    ignores = ignore_mod.load(repo.path)
    to_add: list[str] = []
    for p in paths:
        ap = (repo.path / p).resolve()
        if ap.is_dir():
            for root, dirs, files in os.walk(ap):
                dirs[:] = [d for d in dirs if d != ".git"]
                for f in files:
                    rel = _norm(os.path.relpath(os.path.join(root, f), repo.path))
                    if not _ignored(rel) and (rel in tracked or not ignores.is_ignored(rel)):
                        to_add.append(rel)
        else:
            rel = _norm(os.path.relpath(ap, repo.path))
            if rel in tracked or not ignores.is_ignored(rel, is_dir=_is_dir_no_follow(ap)):
                to_add.append(rel)
    for rel in sorted(set(to_add)):
        full = repo.path / rel
        if not full.exists() and not full.is_symlink():
            idx.remove(rel)
            continue
        data = _blob_data(full)
        sha = objs.write_object(repo, "blob", data)
        st = full.lstat()
        entry = stat_to_entry(rel, st, sha, _mode_for(full))
        # clear conflict stages (1/2/3) on add — resolution
        idx.remove(rel, stage=1)
        idx.remove(rel, stage=2)
        idx.remove(rel, stage=3)
        idx.upsert(entry)
    write_index(repo, idx)


def rm_paths(repo: Repository, paths: Iterable[str], *, cached: bool = False) -> None:
    idx = read_index(repo)
    for p in paths:
        rel = _norm(p)
        idx.remove(rel)
        if not cached:
            f = repo.path / rel
            if f.exists() or f.is_symlink():
                f.unlink()
    write_index(repo, idx)


# ---------------------------------------------------------------------------
# status / diff target lists


def status(repo: Repository, *, include_ignored: bool = False) -> dict[str, list[str]]:
    """Return groups: staged_new, staged_mod, staged_del, mod, untracked."""
    idx = read_index(repo)
    by_path = idx.by_path()
    ignores = None if include_ignored else ignore_mod.load(repo.path)

    head_tree = _head_tree_map(repo)

    staged_new, staged_mod, staged_del = [], [], []
    for path, entry in by_path.items():
        if path not in head_tree:
            staged_new.append(path)
        elif head_tree[path] != entry.sha:
            staged_mod.append(path)
    for path in head_tree:
        if path not in by_path:
            staged_del.append(path)

    modified, untracked = [], []
    seen = set(by_path)
    for rel in iter_worktree(repo):
        full = repo.path / rel
        if rel in by_path:
            data = _blob_data(full)
            sha, _ = objs.hash_bytes("blob", data, repo)
            if sha != by_path[rel].sha:
                modified.append(rel)
        elif ignores is not None and ignores.is_ignored(rel, is_dir=_is_dir_no_follow(full)):
            pass
        else:
            untracked.append(rel)
        seen.discard(rel)
    missing = sorted(seen)

    return {
        "staged_new": sorted(staged_new),
        "staged_mod": sorted(staged_mod),
        "staged_del": sorted(staged_del),
        "modified": sorted(modified),
        "missing": missing,
        "untracked": sorted(untracked),
    }


def _head_tree_map(repo: Repository) -> dict[str, str]:
    _, head_sha = refs_mod.read_head(repo)
    if not head_sha:
        return {}
    try:
        t, data = objs.read_object(repo, head_sha)
    except KeyError:
        return {}
    if t != "commit":
        return {}
    commit = objs.parse_commit(data)
    return flatten_tree(repo, commit.tree)


def flatten_tree(repo: Repository, tree_sha: str, prefix: str = "") -> dict[str, str]:
    out: dict[str, str] = {}
    stack = [(prefix, tree_sha)]
    while stack:
        cur_prefix, cur_tree = stack.pop()
        for e in _tree_entries(repo, cur_tree):
            path = f"{cur_prefix}{e.name}"
            if e.is_dir():
                stack.append((path + "/", e.sha))
            elif e.is_gitlink():
                # gitlinks don't have blob contents in this repo; skip
                continue
            else:
                out[path] = e.sha
    return out


def iter_tree_files(repo: Repository, tree_sha: str, prefix: str = "") -> Iterator[tuple[str, str, str]]:
    """Yield ``(path, mode, sha)`` for blobs in a tree without materializing it."""
    stack = [(prefix, tree_sha)]
    while stack:
        cur_prefix, cur_tree = stack.pop()
        dirs: list[tuple[str, str]] = []
        for e in _tree_entries(repo, cur_tree):
            path = f"{cur_prefix}{e.name}"
            if e.is_dir():
                dirs.append((path + "/", e.sha))
            elif e.is_gitlink():
                continue
            else:
                yield path, e.mode, e.sha
        for item in reversed(dirs):
            stack.append(item)


def _iter_subtree_changes(
    repo: Repository,
    tree_sha: str,
    prefix: str,
    *,
    old: bool,
) -> Iterator[tuple[str, Optional[objs.TreeEntry], Optional[objs.TreeEntry]]]:
    for path, mode, sha in iter_tree_files(repo, tree_sha, prefix):
        entry = objs.TreeEntry(mode, path.rsplit("/", 1)[-1], sha)
        if old:
            yield path, entry, None
        else:
            yield path, None, entry


def iter_tree_changes(
    repo: Repository,
    a_tree: Optional[str],
    b_tree: Optional[str],
    prefix: str = "",
) -> Iterator[tuple[str, Optional[objs.TreeEntry], Optional[objs.TreeEntry]]]:
    """Yield changed blob paths between two trees, skipping identical subtrees."""
    if a_tree == b_tree:
        return
    a_entries = {e.name: e for e in _tree_entries(repo, a_tree)} if a_tree else {}
    b_entries = {e.name: e for e in _tree_entries(repo, b_tree)} if b_tree else {}
    for name in sorted(set(a_entries) | set(b_entries)):
        a = a_entries.get(name)
        b = b_entries.get(name)
        path = f"{prefix}{name}"
        if a is not None and b is not None and a.sha == b.sha and a.mode == b.mode:
            continue
        if a is not None and a.is_gitlink():
            a = None
        if b is not None and b.is_gitlink():
            b = None
        if a is None and b is None:
            continue
        if a is not None and b is not None and a.is_dir() and b.is_dir():
            yield from iter_tree_changes(repo, a.sha, b.sha, path + "/")
        elif a is not None and a.is_dir():
            yield from _iter_subtree_changes(repo, a.sha, path + "/", old=True)
            if b is not None:
                yield path, None, b
        elif b is not None and b.is_dir():
            if a is not None:
                yield path, a, None
            yield from _iter_subtree_changes(repo, b.sha, path + "/", old=False)
        else:
            yield path, a, b


def flatten_gitlinks(repo: Repository, tree_sha: str, prefix: str = "") -> dict[str, str]:
    """Like flatten_tree but only returns gitlink entries (path -> commit sha)."""
    out: dict[str, str] = {}
    stack = [(prefix, tree_sha)]
    while stack:
        cur_prefix, cur_tree = stack.pop()
        for e in _tree_entries(repo, cur_tree):
            path = f"{cur_prefix}{e.name}"
            if e.is_dir():
                stack.append((path + "/", e.sha))
            elif e.is_gitlink():
                out[path] = e.sha
    return out


# ---------------------------------------------------------------------------
# tree <-> index


def write_tree(repo: Repository) -> str:
    """Build trees from the current index, returning the root tree sha.

    Refuses to run while conflict stages are present in the index — fail
    early instead of building a tree from a half-resolved state.
    """
    idx = read_index(repo)
    if idx.has_conflicts():
        raise RuntimeError(
            "cannot write-tree: index has unresolved conflicts at "
            + ", ".join(idx.conflicted_paths())
        )
    # only stage-0 entries
    root: dict = {}
    for e in idx.entries:
        if e.stage != 0:
            continue
        parts = e.path.split("/")
        cur = root
        for part in parts[:-1]:
            cur = cur.setdefault(part, {})
            if not isinstance(cur, dict):
                raise ValueError(f"path conflict at {part}")
        cur[parts[-1]] = e

    def emit(node: dict) -> str:
        entries: list[objs.TreeEntry] = []
        for name, val in node.items():
            if isinstance(val, dict):
                sub_sha = emit(val)
                entries.append(objs.TreeEntry("40000", name, sub_sha))
            else:
                entries.append(objs.TreeEntry(val.mode_str().lstrip("0") or "0", name, val.sha))
        data = objs.encode_tree(entries)
        return objs.write_object(repo, "tree", data)

    return emit(root)


def read_tree(repo: Repository, tree_sha: str) -> None:
    """Replace the index with the contents of the given tree (no worktree update)."""
    idx = Index()
    for path, mode, sha in iter_tree_files(repo, tree_sha):
        idx.entries.append(IndexEntry(mode=int(mode, 8), sha=sha, path=path))
    write_index(repo, idx)


def checkout_tree(repo: Repository, tree_sha: str) -> None:
    """Materialize the tree to the worktree and rewrite the index."""
    target_paths = {path for path, _mode, _sha in iter_tree_files(repo, tree_sha)}
    cur_idx = read_index(repo).by_path()

    # remove files present in current index but not in target
    for path in cur_idx:
        if path not in target_paths:
            f = repo.path / path
            if f.exists() or f.is_symlink():
                try:
                    f.unlink()
                except OSError:
                    pass

    new_idx = Index()
    for path, mode, sha in iter_tree_files(repo, tree_sha):
        t, data = objs.read_object(repo, sha)
        if t != "blob":
            continue
        full = repo.path / path
        full.parent.mkdir(parents=True, exist_ok=True)
        if mode == "120000":
            if full.exists() or full.is_symlink():
                full.unlink()
            try:
                os.symlink(data.decode("utf-8"), full)
            except (AttributeError, NotImplementedError, OSError):
                full.write_bytes(data)
        else:
            if full.exists() and full.is_symlink():
                full.unlink()
            full.write_bytes(data)
            if int(mode, 8) & 0o111:
                try:
                    full.chmod(full.stat().st_mode | 0o111)
                except OSError:
                    pass
        st = full.lstat()
        new_idx.entries.append(stat_to_entry(path, st, sha, int(mode, 8)))
    write_index(repo, new_idx)
