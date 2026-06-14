from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
PYGIT = [sys.executable, "-m", "pythongit"]


def _run(cmd: list[str], cwd: Path, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    merged_env["PYTHONPATH"] = str(ROOT) + os.pathsep + merged_env.get("PYTHONPATH", "")
    if env:
        merged_env.update(env)
    return subprocess.run(cmd, cwd=cwd, env=merged_env, text=True, capture_output=True, timeout=10)


@pytest.fixture
def run_cmd():
    return _run


@pytest.fixture
def pygit_cmd() -> list[str]:
    return list(PYGIT)


@pytest.fixture
def oracle_git() -> str:
    git = os.environ.get("PYGIT_PARITY_GIT")
    if not git:
        pytest.skip("set PYGIT_PARITY_GIT to a C Git 2.54.0 binary to run parity tests")
    result = subprocess.run([git, "--version"], text=True, capture_output=True, timeout=10)
    version = (result.stdout + result.stderr).strip()
    if result.returncode != 0 or not version.startswith("git version 2.54.0"):
        pytest.skip(f"PYGIT_PARITY_GIT must be git version 2.54.0, got {version!r}")
    return git


def _init_with_pygit(run_cmd, pygit_cmd: list[str], path: Path) -> None:
    path.mkdir()
    result = run_cmd([*pygit_cmd, "init", "-b", "main", "."], path)
    assert result.returncode == 0, result.stderr


def _configure_identity(run_cmd, pygit_cmd: list[str], path: Path) -> None:
    assert run_cmd([*pygit_cmd, "config", "user.name", "Parity"], path).returncode == 0
    assert run_cmd([*pygit_cmd, "config", "user.email", "parity@example.com"], path).returncode == 0


def test_status_short_branch_unborn_matches_c_git_254(tmp_path: Path, oracle_git: str, pygit_cmd, run_cmd):
    repo = tmp_path / "repo"
    _init_with_pygit(run_cmd, pygit_cmd, repo)

    oracle = run_cmd([oracle_git, "status", "--short", "--branch"], repo)
    actual = run_cmd([*pygit_cmd, "status", "--short", "--branch"], repo)

    assert actual.returncode == oracle.returncode
    assert actual.stdout == oracle.stdout


def test_remote_verbose_matches_c_git_254(tmp_path: Path, oracle_git: str, pygit_cmd, run_cmd):
    repo = tmp_path / "repo"
    _init_with_pygit(run_cmd, pygit_cmd, repo)
    assert run_cmd([*pygit_cmd, "remote", "add", "origin", "https://example.com/repo.git"], repo).returncode == 0

    oracle = run_cmd([oracle_git, "remote", "-v"], repo)
    actual = run_cmd([*pygit_cmd, "remote", "-v"], repo)

    assert actual.returncode == oracle.returncode
    assert actual.stdout == oracle.stdout


def test_log_oneline_decorate_matches_c_git_254(tmp_path: Path, oracle_git: str, pygit_cmd, run_cmd):
    repo = tmp_path / "repo"
    _init_with_pygit(run_cmd, pygit_cmd, repo)
    _configure_identity(run_cmd, pygit_cmd, repo)
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    assert run_cmd([*pygit_cmd, "add", "a.txt"], repo).returncode == 0
    commit = run_cmd([*pygit_cmd, "commit", "-m", "one"], repo)
    assert commit.returncode == 0, commit.stderr

    oracle = run_cmd([oracle_git, "log", "--oneline", "--decorate", "-1"], repo)
    actual = run_cmd([*pygit_cmd, "log", "--oneline", "--decorate", "-1"], repo)

    assert actual.returncode == oracle.returncode
    assert actual.stdout == oracle.stdout


def test_global_options_before_command_match_c_git_254(tmp_path: Path, oracle_git: str, pygit_cmd, run_cmd):
    repo = tmp_path / "repo"
    _init_with_pygit(run_cmd, pygit_cmd, repo)

    oracle = run_cmd(
        [oracle_git, "-C", str(repo), "--no-pager", "-c", "core.quotePath=false", "status", "--short", "--branch"],
        tmp_path,
    )
    actual = run_cmd(
        [*pygit_cmd, "-C", str(repo), "--no-pager", "-c", "core.quotePath=false", "status", "--short", "--branch"],
        tmp_path,
    )

    assert actual.returncode == oracle.returncode
    assert actual.stdout == oracle.stdout
