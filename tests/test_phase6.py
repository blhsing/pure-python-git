"""Phase 6 tests."""
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
    def __init__(self, text: str):
        self._text = text
    def read(self) -> str:
        return self._text


def main() -> int:
    failed = 0

    def check(cond, label):
        nonlocal failed
        print(("PASS" if cond else "FAIL") + " " + label)
        if not cond:
            failed += 1

    tmp = Path(tempfile.mkdtemp(prefix="pygit-p6-"))
    try:
        os.chdir(tmp)
        run("init", str(tmp))
        run("config", "user.name", "t"); run("config", "user.email", "t@e.com")
        (tmp / "a.txt").write_text("a\n"); run("add", "a.txt"); run("commit", "-m", "c1")
        (tmp / "b.txt").write_text("b\n"); run("add", "b.txt"); run("commit", "-m", "c2")
        run("tag", "v1")

        # pack-refs
        rc = run("pack-refs", "--all")
        check(rc == 0, "pack-refs --all")
        check((tmp / ".git" / "packed-refs").exists(), "packed-refs written")

        # merge-file
        (tmp / "base").write_text("a\nb\nc\n")
        (tmp / "ours").write_text("a\nB\nc\n")
        (tmp / "theirs").write_text("a\nb\nC\n")
        rc = run("merge-file", "-p", str(tmp / "ours"), str(tmp / "base"), str(tmp / "theirs"))
        check(rc == 0, "merge-file clean three-way")

        # fast-export
        old_stdout = sys.stdout
        out_buf = io.StringIO()
        sys.stdout = out_buf
        try:
            rc = run("fast-export", "HEAD")
        finally:
            sys.stdout = old_stdout
        check(rc == 0 and "commit refs/heads/main" in out_buf.getvalue(), "fast-export produced stream")

        # fast-import roundtrip into fresh repo
        tmp2 = Path(tempfile.mkdtemp(prefix="pygit-p6b-"))
        try:
            os.chdir(tmp2)
            run("init", str(tmp2))
            old_stdin = sys.stdin
            sys.stdin = FakeStdin(out_buf.getvalue())
            try:
                rc = run("fast-import")
                check(rc == 0, "fast-import succeeds")
            finally:
                sys.stdin = old_stdin
            rc = run("log", "--oneline")
            check(rc == 0, "fast-imported log readable")
        finally:
            os.chdir(tmp)
            shutil.rmtree(tmp2, ignore_errors=True)

        # interpret-trailers
        sys.stdin = FakeStdin("subject\n\nbody text\n")
        old_stdout = sys.stdout
        out_buf = io.StringIO()
        sys.stdout = out_buf
        try:
            rc = run("interpret-trailers", "--trailer", "Signed-off-by: t <t@e.com>")
        finally:
            sys.stdout = old_stdout
            sys.stdin = sys.__stdin__
        check(rc == 0 and "Signed-off-by:" in out_buf.getvalue(), "interpret-trailers appended")

        # verify-commit
        rc = run("verify-commit", "HEAD")
        check(rc == 0, "verify-commit")

        # commit-graph
        rc = run("commit-graph", "write")
        check(rc == 0, "commit-graph write")
        rc = run("commit-graph", "verify")
        check(rc == 0, "commit-graph verify")

        # rerere
        rc = run("rerere", "status")
        check(rc == 0, "rerere status")

        # column
        sys.stdin = FakeStdin("alpha\nbeta\ngamma\ndelta\n")
        rc = run("column")
        check(rc == 0, "column")
        sys.stdin = sys.__stdin__

        # sparse-checkout
        rc = run("sparse-checkout", "init")
        check(rc == 0, "sparse-checkout init")
        rc = run("sparse-checkout", "set", "a.txt")
        check(rc == 0, "sparse-checkout set")
        rc = run("sparse-checkout", "list")
        check(rc == 0, "sparse-checkout list")
        rc = run("sparse-checkout", "disable")
        check(rc == 0, "sparse-checkout disable")

        # submodule status (empty)
        rc = run("submodule", "status")
        check(rc == 0, "submodule status (empty)")

    finally:
        os.chdir(ROOT)
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'ALL PASSED' if failed == 0 else f'{failed} FAILED'}")
    return failed


if __name__ == "__main__":
    raise SystemExit(main())
