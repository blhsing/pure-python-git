"""Shared pytest fixtures."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


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
