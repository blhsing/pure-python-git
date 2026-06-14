from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PYGIT = [sys.executable, "-m", "pythongit"]


def pygit_cmd() -> list[str]:
    return list(PYGIT)


def run_cmd(
    cmd: list[str],
    cwd: Path,
    *,
    env: dict[str, str] | None = None,
    stdin: str | None = None,
    timeout: int = 10,
) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    merged_env["PYTHONPATH"] = str(ROOT) + os.pathsep + merged_env.get("PYTHONPATH", "")
    if env:
        merged_env.update(env)
    return subprocess.run(
        cmd,
        cwd=cwd,
        env=merged_env,
        input=stdin,
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def init_with_pygit(path: Path) -> None:
    path.mkdir()
    result = run_cmd([*pygit_cmd(), "init", "-b", "main", "."], path)
    assert result.returncode == 0, result.stderr


def init_repo_pair(tmp_path: Path) -> tuple[Path, Path]:
    oracle_repo = tmp_path / "oracle"
    actual_repo = tmp_path / "actual"
    init_with_pygit(oracle_repo)
    init_with_pygit(actual_repo)
    return oracle_repo, actual_repo


def configure_pygit_identity(path: Path) -> None:
    assert run_cmd([*pygit_cmd(), "config", "user.name", "Parity"], path).returncode == 0
    assert run_cmd([*pygit_cmd(), "config", "user.email", "parity@example.com"], path).returncode == 0


def assert_same_result(actual: subprocess.CompletedProcess[str], oracle: subprocess.CompletedProcess[str]) -> None:
    assert actual.returncode == oracle.returncode
    assert actual.stdout == oracle.stdout
    assert actual.stderr == oracle.stderr
