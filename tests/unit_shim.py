"""Unit tests for the opt-in `git` shim installer."""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from pythongit import cli


def cli_run(*args):
    return cli.main(list(args))


def _name(stem: str) -> str:
    return f"{stem}.exe" if os.name == "nt" else stem


def test_install_creates_git_alongside_pygit(tmp_path):
    pygit_launcher = tmp_path / _name("pygit")
    pygit_launcher.write_bytes(b"#!/usr/bin/env python\n# pygit launcher\n")
    if os.name != "nt":
        pygit_launcher.chmod(pygit_launcher.stat().st_mode | 0o111)
    rc = cli_run("install-git-shim", "--dir", str(tmp_path))
    assert rc == 0
    target = tmp_path / _name("git")
    assert target.exists()
    if os.name != "nt":
        assert target.stat().st_mode & stat.S_IXUSR
    assert target.read_bytes() == pygit_launcher.read_bytes()


def test_install_refuses_overwrite_without_force(tmp_path, capsys):
    (tmp_path / _name("pygit")).write_bytes(b"# pygit\n")
    (tmp_path / _name("git")).write_bytes(b"# existing\n")
    rc = cli_run("install-git-shim", "--dir", str(tmp_path))
    assert rc == 1
    err = capsys.readouterr().err
    assert "refusing to overwrite" in err
    # Force should win.
    assert cli_run("install-git-shim", "--dir", str(tmp_path), "--force") == 0
    assert (tmp_path / _name("git")).read_bytes() == b"# pygit\n"


def test_install_fails_when_pygit_launcher_missing(tmp_path, capsys):
    rc = cli_run("install-git-shim", "--dir", str(tmp_path))
    assert rc == 1
    assert "pygit launcher not found" in capsys.readouterr().err


def test_uninstall_removes_shim_then_no_op(tmp_path):
    pygit = tmp_path / _name("pygit")
    pygit.write_bytes(b"# pygit\n")
    cli_run("install-git-shim", "--dir", str(tmp_path))
    assert (tmp_path / _name("git")).exists()
    rc = cli_run("uninstall-git-shim", "--dir", str(tmp_path))
    assert rc == 0
    assert not (tmp_path / _name("git")).exists()
    # second call: no-op, still 0
    rc = cli_run("uninstall-git-shim", "--dir", str(tmp_path))
    assert rc == 0


def test_uninstall_refuses_to_remove_unrelated_git(tmp_path, capsys):
    (tmp_path / _name("pygit")).write_bytes(b"# pygit launcher\n")
    (tmp_path / _name("git")).write_bytes(b"# different content entirely\n" * 5)
    rc = cli_run("uninstall-git-shim", "--dir", str(tmp_path))
    assert rc == 1
    assert "does not look like a pythongit shim" in capsys.readouterr().err
    assert (tmp_path / _name("git")).exists()


def test_install_warns_about_path_shadowing(tmp_path, capsys, monkeypatch):
    (tmp_path / _name("pygit")).write_bytes(b"# pygit\n")
    # Pretend a real git is first on PATH and lives elsewhere.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    real_git = elsewhere / _name("git")
    real_git.write_bytes(b"# pretend real git\n")
    # shutil.which() on Unix only finds executable files.
    if os.name != "nt":
        real_git.chmod(real_git.stat().st_mode | 0o111)
    monkeypatch.setenv("PATH", str(elsewhere) + os.pathsep + str(tmp_path))
    rc = cli_run("install-git-shim", "--dir", str(tmp_path))
    assert rc == 0
    err = capsys.readouterr().err
    assert "already first on PATH" in err
