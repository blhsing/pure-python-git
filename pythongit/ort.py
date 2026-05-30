"""Adapter for Git's ort merge engine.

The pure Python merge code remains available as a fallback, but when a real
``git`` binary is present this module asks ``git merge-tree --write-tree`` to
run the same in-core ort engine used by C Git and imports its result tree and
conflicted index stages.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import objects as objs
from . import workdir
from .index import Index, IndexEntry
from .repo import Repository


@dataclass(frozen=True)
class OrtResult:
    tree: str
    conflicts: list[str]
    conflict_index: Optional[Index]


def _is_real_git(path: str) -> bool:
    try:
        proc = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return False
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0 and out.startswith("git version ") and "pygit" not in out


def _real_git_binary() -> Optional[str]:
    env_git = os.environ.get("PYGIT_REAL_GIT")
    if env_git and _is_real_git(env_git):
        return env_git
    git = shutil.which("git")
    if git and _is_real_git(git):
        return git
    for cand in (
        "/usr/bin/git",
        "/usr/local/bin/git",
        "/opt/homebrew/bin/git",
        "/Library/Developer/CommandLineTools/usr/bin/git",
        r"C:\Program Files\Git\bin\git.exe",
        r"C:\Program Files\Git\cmd\git.exe",
    ):
        if Path(cand).exists() and _is_real_git(cand):
            return cand
    return None


def _result_index(repo: Repository, tree: str, stages: list[tuple[str, int, int, str]]) -> Index:
    conflicted = {path for path, _stage, _mode, _sha in stages}
    idx = Index()
    for path, mode, sha in workdir.iter_tree_files(repo, tree):
        if path in conflicted:
            continue
        idx.entries.append(IndexEntry(mode=int(mode, 8), sha=sha, path=path))
    for path, stage, mode, sha in stages:
        e = IndexEntry(mode=mode, sha=sha, path=path)
        e.stage = stage
        idx.entries.append(e)
    return idx


def _loose_object_path(repo: Repository, sha: str) -> Path:
    return repo.gitdir / "objects" / sha[:2] / sha[2:]


def _make_result_objects_writable(repo: Repository, tree: str, stages: list[tuple[str, int, int, str]]) -> None:
    if os.name != "nt":
        return
    seen: set[str] = set()
    stack = [tree, *(sha for _path, _stage, _mode, sha in stages)]
    while stack:
        sha = stack.pop()
        if sha in seen:
            continue
        seen.add(sha)
        path = _loose_object_path(repo, sha)
        if path.exists():
            try:
                path.chmod(path.stat().st_mode | stat.S_IWRITE)
            except OSError:
                pass
        try:
            obj_type, data = objs.read_object(repo, sha)
        except KeyError:
            continue
        if obj_type == "tree":
            for entry in objs.parse_tree(data, repo.hash_len):
                stack.append(entry.sha)


def _parse_merge_tree_output(repo: Repository, raw: bytes) -> OrtResult:
    parts = raw.split(b"\0")
    if parts and parts[-1] == b"":
        parts.pop()
    if not parts:
        raise ValueError("git merge-tree produced no tree")
    tree = parts[0].decode("ascii")
    stages: list[tuple[str, int, int, str]] = []
    for rec in parts[1:]:
        if not rec:
            continue
        meta, sep, path_b = rec.partition(b"\t")
        if not sep:
            continue
        mode_s, sha, stage_s = meta.decode("ascii").split()
        path = path_b.decode("utf-8", errors="replace")
        stages.append((path, int(stage_s), int(mode_s, 8), sha))
    conflicts = sorted({path for path, _stage, _mode, _sha in stages})
    _make_result_objects_writable(repo, tree, stages)
    conflict_index = _result_index(repo, tree, stages) if stages else None
    return OrtResult(tree, conflicts, conflict_index)


def merge_tree(
    repo: Repository,
    merge_base: str,
    ours: str,
    theirs: str,
) -> Optional[OrtResult]:
    """Run C Git's ort merge for three tree-ish arguments.

    Returns ``None`` when no usable C Git backend is available, allowing callers
    to fall back to the pure-Python merge engine.
    """
    if os.environ.get("PYGIT_MERGE_BACKEND", "").lower() == "pure":
        return None
    git = _real_git_binary()
    if not git:
        return None
    env = os.environ.copy()
    env["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        proc = subprocess.run(
            [
                git,
                "-C",
                str(repo.path),
                "merge-tree",
                "--write-tree",
                "--no-messages",
                "-z",
                "--merge-base",
                merge_base,
                ours,
                theirs,
            ],
            capture_output=True,
            env=env,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode not in (0, 1) or not proc.stdout:
        return None
    try:
        return _parse_merge_tree_output(repo, proc.stdout)
    except (ValueError, KeyError, IndexError):
        return None
