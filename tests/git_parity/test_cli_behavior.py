from __future__ import annotations

from pathlib import Path

from tests.git_parity.support import (
    assert_same_result,
    configure_pygit_identity,
    init_repo_pair,
    init_with_pygit,
    pygit_cmd,
    run_cmd,
)


def test_status_short_branch_unborn_matches_c_git_254(tmp_path: Path, git_254_oracle: str):
    repo = tmp_path / "repo"
    init_with_pygit(repo)

    oracle = run_cmd([git_254_oracle, "status", "--short", "--branch"], repo)
    actual = run_cmd([*pygit_cmd(), "status", "--short", "--branch"], repo)

    assert_same_result(actual, oracle)


def test_remote_verbose_matches_c_git_254(tmp_path: Path, git_254_oracle: str):
    repo = tmp_path / "repo"
    init_with_pygit(repo)
    assert run_cmd([*pygit_cmd(), "remote", "add", "origin", "https://example.com/repo.git"], repo).returncode == 0

    oracle = run_cmd([git_254_oracle, "remote", "-v"], repo)
    actual = run_cmd([*pygit_cmd(), "remote", "-v"], repo)

    assert_same_result(actual, oracle)


def test_log_oneline_decorate_matches_c_git_254(tmp_path: Path, git_254_oracle: str):
    repo = tmp_path / "repo"
    init_with_pygit(repo)
    configure_pygit_identity(repo)
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    assert run_cmd([*pygit_cmd(), "add", "a.txt"], repo).returncode == 0
    commit = run_cmd([*pygit_cmd(), "commit", "-m", "one"], repo)
    assert commit.returncode == 0, commit.stderr

    oracle = run_cmd([git_254_oracle, "log", "--oneline", "--decorate", "-1"], repo)
    actual = run_cmd([*pygit_cmd(), "log", "--oneline", "--decorate", "-1"], repo)

    assert_same_result(actual, oracle)


def test_global_options_before_command_match_c_git_254(tmp_path: Path, git_254_oracle: str):
    repo = tmp_path / "repo"
    init_with_pygit(repo)

    oracle = run_cmd(
        [git_254_oracle, "-C", str(repo), "--no-pager", "-c", "core.quotePath=false", "status", "--short", "--branch"],
        tmp_path,
    )
    actual = run_cmd(
        [*pygit_cmd(), "-C", str(repo), "--no-pager", "-c", "core.quotePath=false", "status", "--short", "--branch"],
        tmp_path,
    )

    assert_same_result(actual, oracle)


def test_config_replace_all_matches_c_git_254(tmp_path: Path, git_254_oracle: str):
    oracle_repo, actual_repo = init_repo_pair(tmp_path)

    oracle = run_cmd([git_254_oracle, "config", "--replace-all", "user.name", "Alice"], oracle_repo)
    actual = run_cmd([*pygit_cmd(), "config", "--replace-all", "user.name", "Alice"], actual_repo)
    assert_same_result(actual, oracle)

    oracle_read = run_cmd([git_254_oracle, "config", "user.name"], oracle_repo)
    actual_read = run_cmd([*pygit_cmd(), "config", "user.name"], actual_repo)
    assert_same_result(actual_read, oracle_read)


def test_stage_alias_matches_c_git_254(tmp_path: Path, git_254_oracle: str):
    oracle_repo, actual_repo = init_repo_pair(tmp_path)
    (oracle_repo / "a.txt").write_text("one\n", encoding="utf-8")
    (actual_repo / "a.txt").write_text("one\n", encoding="utf-8")

    oracle = run_cmd([git_254_oracle, "stage", "a.txt"], oracle_repo)
    actual = run_cmd([*pygit_cmd(), "stage", "a.txt"], actual_repo)
    assert_same_result(actual, oracle)

    oracle_status = run_cmd([git_254_oracle, "status", "--short"], oracle_repo)
    actual_status = run_cmd([*pygit_cmd(), "status", "--short"], actual_repo)
    assert_same_result(actual_status, oracle_status)


def test_remote_show_without_args_matches_c_git_254(tmp_path: Path, git_254_oracle: str):
    oracle_repo, actual_repo = init_repo_pair(tmp_path)
    for repo, cmd in ((oracle_repo, [git_254_oracle]), (actual_repo, pygit_cmd())):
        added = run_cmd([*cmd, "remote", "add", "origin", "https://example.com/repo.git"], repo)
        assert added.returncode == 0, added.stderr

    oracle = run_cmd([git_254_oracle, "remote", "show"], oracle_repo)
    actual = run_cmd([*pygit_cmd(), "remote", "show"], actual_repo)
    assert_same_result(actual, oracle)
