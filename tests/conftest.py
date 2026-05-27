"""Shared pytest fixtures."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _is_real_git(path: str) -> bool:
    """A real git binary identifies itself as 'git version X.Y.Z'. Pythongit's
    `git` shim prints 'pygit version ...' for --version. Use this to
    distinguish them when pythongit is installed into the same venv."""
    try:
        r = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if r.returncode != 0:
        return False
    out = (r.stdout or "") + (r.stderr or "")
    # Real git prints 'git version 2.x.y'. Reject pythongit's 'pygit version ...'.
    return out.startswith("git version ") and "pygit" not in out


def real_git() -> str | None:
    """Return path of a real git binary, or None if not available.

    Walks PATH and tries every `git` it finds (in order), returning the first
    that identifies as real git. Falls back to known absolute paths.
    """
    seen: set[str] = set()
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d:
            continue
        for name in ("git", "git.exe"):
            cand = os.path.join(d, name)
            if cand in seen:
                continue
            seen.add(cand)
            if os.path.isfile(cand) and _is_real_git(cand):
                return cand
    # Fallbacks
    for cand in (
        "/usr/bin/git", "/usr/local/bin/git",
        "/opt/homebrew/bin/git", "/Library/Developer/CommandLineTools/usr/bin/git",
        r"C:\Program Files\Git\bin\git.exe",
        r"C:\Program Files\Git\cmd\git.exe",
    ):
        if cand not in seen and os.path.isfile(cand) and _is_real_git(cand):
            return cand
    return None


@pytest.fixture
def tmprepo(tmp_path: Path):
    """Initialize a fresh repository and chdir into it. Yields (Repository, Path)."""
    from pythongit.repo import Repository
    cwd = os.getcwd()
    os.chdir(tmp_path)
    repo = Repository.init(tmp_path)
    cfg = repo.gitdir / "config"
    cfg.write_text(cfg.read_text(encoding="utf-8") +
                   "[user]\n\tname = test\n\temail = test@example.com\n",
                   encoding="utf-8")
    try:
        yield repo, tmp_path
    finally:
        os.chdir(cwd)


def commit_one(repo, path: str, content: str, message: str) -> str:
    """Helper: write content, add, commit; return commit sha."""
    from pythongit import cli, refs
    (repo.path / path).write_text(content)
    cli.main(["add", path])
    cli.main(["commit", "-m", message])
    return refs.rev_parse(repo, "HEAD")
