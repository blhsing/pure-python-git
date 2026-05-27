"""Phase 7 tests."""
from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pythongit import cli  # noqa: E402


def run(*args: str) -> int:
    return cli.main(list(args))


class FakeStdin:
    def __init__(self, text: str, binary: bytes | None = None):
        self._text = text
        self.buffer = io.BytesIO(binary if binary is not None else text.encode("utf-8"))
    def read(self) -> str:
        return self._text


def main() -> int:
    failed = 0

    def check(cond, label):
        nonlocal failed
        print(("PASS" if cond else "FAIL") + " " + label)
        if not cond:
            failed += 1

    tmp = Path(tempfile.mkdtemp(prefix="pygit-p7-"))
    try:
        os.chdir(tmp)
        run("init", str(tmp))
        run("config", "user.name", "t"); run("config", "user.email", "t@e.com")
        (tmp / "a.txt").write_text("a\n"); run("add", "a.txt"); run("commit", "-m", "c1")
        (tmp / "a.txt").write_text("A\n"); (tmp / "b.txt").write_text("b\n")
        run("add", "."); run("commit", "-m", "c2")

        # diff-tree
        rc = run("diff-tree", "-r", "HEAD")
        check(rc == 0, "diff-tree -r HEAD")

        rc = run("diff-tree", "--name-status", "HEAD")
        check(rc == 0, "diff-tree --name-status")

        # diff-files: modify worktree without staging
        (tmp / "a.txt").write_text("Aaa\n")
        rc = run("diff-files", "--name-only")
        check(rc == 0, "diff-files --name-only")

        # diff-index against HEAD (staged unchanged after revert)
        run("checkout", "a.txt")  # restore
        rc = run("diff-index", "--cached", "HEAD")
        check(rc == 0, "diff-index --cached HEAD")

        # check-attr
        (tmp / ".gitattributes").write_text("*.txt text\n*.bin -text\n")
        rc = run("check-attr", "text", "a.txt")
        check(rc == 0, "check-attr text a.txt")
        rc = run("check-attr", "-a", "a.txt")
        check(rc == 0, "check-attr -a")

        # check-ref-format
        rc = run("check-ref-format", "refs/heads/main")
        check(rc == 0, "check-ref-format good")
        rc = run("check-ref-format", "refs/heads/bad..name")
        check(rc != 0, "check-ref-format rejects '..'")
        rc = run("check-ref-format", "--branch", "feat")
        check(rc == 0, "check-ref-format --branch")

        # check-mailmap
        (tmp / ".mailmap").write_text("Real Name <real@e.com> <old@e.com>\n")
        rc = run("check-mailmap", "Old <old@e.com>")
        check(rc == 0, "check-mailmap")

        # show-index — pack first, then read its idx
        run("pack-objects", "pack", "--all")
        idx_files = list((tmp / ".git" / "objects" / "pack").glob("pack-*.idx"))
        rc = run("show-index", str(idx_files[0]))
        check(rc == 0, "show-index (file)")

        # unpack-file
        from pythongit import objects as o
        from pythongit.repo import Repository
        rp = Repository.discover(tmp)
        blob_sha = o.write_object(rp, "blob", b"hello\n")
        rc = run("unpack-file", blob_sha)
        check(rc == 0, "unpack-file")

        # merge-index (stub)
        rc = run("merge-index", "true")
        check(rc == 0, "merge-index (no-op)")

        # hook — list (no hooks installed) and run absent
        rc = run("hook", "list")
        check(rc == 0, "hook list")
        rc = run("hook", "run", "pre-commit")
        check(rc == 0, "hook run absent -> 0")

        # credential fill
        old_stdin = sys.stdin
        sys.stdin = FakeStdin("protocol=https\nhost=example.com\n")
        try:
            rc = run("credential", "fill")
            check(rc == 0, "credential fill")
        finally:
            sys.stdin = old_stdin

        # get-tar-commit-id: feed something without comment=
        sys.stdin = FakeStdin("", binary=b"hello\n")
        try:
            rc = run("get-tar-commit-id")
            check(rc == 1, "get-tar-commit-id returns 1 when missing")
        finally:
            sys.stdin = old_stdin

    finally:
        os.chdir(ROOT)
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'ALL PASSED' if failed == 0 else f'{failed} FAILED'}")
    return failed


if __name__ == "__main__":
    raise SystemExit(main())
