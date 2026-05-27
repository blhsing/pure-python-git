"""End-to-end smoke tests for pythongit.

Validates:
  * init -> add -> commit -> log -> diff cycle
  * on-disk format is readable by the real `git` binary
  * cat-file, ls-tree, rev-parse round-trip
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pythongit import cli  # noqa: E402


def run(*args: str) -> int:
    return cli.main(list(args))


def git_available() -> bool:
    return shutil.which("git") is not None


def main() -> int:
    failed = 0

    def check(cond: bool, label: str) -> None:
        nonlocal failed
        print(("PASS" if cond else "FAIL") + " " + label)
        if not cond:
            failed += 1

    tmp = Path(tempfile.mkdtemp(prefix="pygit-test-"))
    try:
        os.chdir(tmp)

        # init
        assert run("init", str(tmp)) == 0
        check((tmp / ".git" / "HEAD").exists(), "init: .git/HEAD created")
        check((tmp / ".git" / "objects").is_dir(), "init: objects dir")

        # hash-object + cat-file roundtrip
        (tmp / "hello.txt").write_text("hello world\n")
        run("config", "user.name", "test")
        run("config", "user.email", "t@example.com")
        rc = run("add", "hello.txt")
        check(rc == 0, "add: returns 0")
        rc = run("commit", "-m", "initial")
        check(rc == 0, "commit: returns 0")

        # log
        rc = run("log", "--oneline")
        check(rc == 0, "log: returns 0")

        # cat-file -p on HEAD
        rc = run("cat-file", "-p", "HEAD")
        check(rc == 0, "cat-file -p HEAD")

        # write-tree round-trip via real git, if available
        if git_available():
            r = subprocess.run(["git", "log", "--oneline"], cwd=tmp, capture_output=True, text=True)
            check(r.returncode == 0 and r.stdout.strip() != "", f"git log reads our commit: {r.stdout.strip()!r}")

            r = subprocess.run(["git", "fsck", "--no-dangling"], cwd=tmp, capture_output=True, text=True)
            check(r.returncode == 0, f"git fsck passes (stderr={r.stderr.strip()!r})")

            r = subprocess.run(["git", "cat-file", "-p", "HEAD^{tree}"], cwd=tmp, capture_output=True, text=True)
            check(r.returncode == 0 and "hello.txt" in r.stdout, "git can read tree")

        # second commit + diff
        (tmp / "hello.txt").write_text("hello world\nsecond line\n")
        rc = run("diff")
        check(rc == 0, "diff: returns 0")
        run("add", "hello.txt")
        run("commit", "-m", "second")

        # branch + checkout
        run("branch", "feature")
        run("checkout", "feature")
        (tmp / "f.txt").write_text("feature\n")
        run("add", "f.txt")
        run("commit", "-m", "feature commit")

        if git_available():
            r = subprocess.run(["git", "log", "--all", "--oneline"], cwd=tmp, capture_output=True, text=True)
            lines = [l for l in r.stdout.splitlines() if l.strip()]
            check(len(lines) == 3, f"git sees 3 commits across branches (got {len(lines)})")

        # ls-files
        run("checkout", "main")
        rc = run("ls-files")
        check(rc == 0, "ls-files")

        # tag
        run("tag", "v1")
        rc = run("rev-parse", "v1")
        check(rc == 0, "tag + rev-parse")

        # reset --hard back one commit
        rc = run("log", "--oneline")
        rc = run("reset", "--hard", "HEAD")
        check(rc == 0, "reset --hard HEAD no-op")

    finally:
        os.chdir(ROOT)
        try:
            shutil.rmtree(tmp)
        except Exception:
            pass

    print(f"\n{'ALL PASSED' if failed == 0 else f'{failed} FAILED'}")
    return failed


if __name__ == "__main__":
    raise SystemExit(main())
