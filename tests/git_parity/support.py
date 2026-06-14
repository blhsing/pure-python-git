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


# A hermetic, deterministic environment so that commit object ids (and thus all
# downstream output) are byte-identical between the oracle and pythongit.
DETERMINISTIC_ENV = {
    "GIT_AUTHOR_NAME": "Parity",
    "GIT_AUTHOR_EMAIL": "parity@example.com",
    "GIT_COMMITTER_NAME": "Parity",
    "GIT_COMMITTER_EMAIL": "parity@example.com",
    "GIT_AUTHOR_DATE": "1700000000 +0000",
    "GIT_COMMITTER_DATE": "1700000000 +0000",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "LC_ALL": "C",
    "LANG": "C",
    "TZ": "UTC",
}


def _norm(text: str, oracle_repo: Path, pygit_repo: Path) -> str:
    return text.replace(str(pygit_repo), "<REPO>").replace(str(oracle_repo), "<REPO>")


def assert_command_parity(oracle: str, tmp_path: Path, setup, probe, *, stdin: str | None = None) -> None:
    """Build identical repos with the oracle and pythongit, then assert that
    ``probe`` yields identical return code, stdout, and stderr on both.

    ``setup`` is a sequence of steps run on each repo. A step is either a
    command argv (``["add", "-A"]``) or a tuple: ``("write", path, text)`` to
    create/overwrite a file or ``("rm", path)`` to delete one. Absolute repo
    paths are normalized away before comparison.
    """
    results: dict[str, subprocess.CompletedProcess[str]] = {}
    repos: dict[str, Path] = {}
    for tool, base in (("oracle", [oracle]), ("pygit", pygit_cmd())):
        repo = tmp_path / tool / "repo"
        repo.mkdir(parents=True)
        repos[tool] = repo
        init = run_cmd([*base, "init", "-b", "main", "."], repo, env=DETERMINISTIC_ENV)
        assert init.returncode == 0, init.stderr
        for step in setup:
            if isinstance(step, tuple) and step and step[0] == "write":
                (repo / step[1]).parent.mkdir(parents=True, exist_ok=True)
                (repo / step[1]).write_text(step[2], encoding="utf-8")
            elif isinstance(step, tuple) and step and step[0] == "rm":
                (repo / step[1]).unlink()
            else:
                run_cmd([*base, *step], repo, env=DETERMINISTIC_ENV)
        results[tool] = run_cmd([*base, *probe], repo, env=DETERMINISTIC_ENV, stdin=stdin)
    oc, py = results["oracle"], results["pygit"]
    o_repo, p_repo = repos["oracle"], repos["pygit"]
    assert py.returncode == oc.returncode, (
        f"rc {py.returncode} != {oc.returncode}\npygit stderr: {py.stderr}\noracle stderr: {oc.stderr}"
    )
    assert _norm(py.stdout, o_repo, p_repo) == _norm(oc.stdout, o_repo, p_repo)
    assert _norm(py.stderr, o_repo, p_repo) == _norm(oc.stderr, o_repo, p_repo)
