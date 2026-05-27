"""Phase 5 tests: grep / show-branch / whatchanged / mktag / name-rev /
var / stripspace / update-server-info / replace / cherry / range-diff.

(pull requires network; we test fetch+merge composition separately.)
"""
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

    tmp = Path(tempfile.mkdtemp(prefix="pygit-p5-"))
    try:
        os.chdir(tmp)
        run("init", str(tmp))
        run("config", "user.name", "t"); run("config", "user.email", "t@e.com")

        (tmp / "a.py").write_text("def hello():\n    print('hi')\n")
        run("add", "a.py"); run("commit", "-m", "c1")
        (tmp / "b.py").write_text("def bye():\n    print('bye')\n")
        run("add", "b.py"); run("commit", "-m", "c2")

        # grep
        rc = run("grep", "hello")
        check(rc == 0, "grep finds 'hello'")
        rc = run("grep", "nonexistent_xyz")
        check(rc == 1, "grep returns 1 on no match")
        rc = run("grep", "-l", "print")
        check(rc == 0, "grep -l")
        rc = run("grep", "-n", "print")
        check(rc == 0, "grep -n")
        rc = run("grep", "--cached", "hello")
        check(rc == 0, "grep --cached")

        # show-branch
        run("branch", "feat")
        rc = run("show-branch")
        check(rc == 0, "show-branch")

        # whatchanged
        rc = run("whatchanged")
        check(rc == 0, "whatchanged")

        # name-rev
        run("tag", "v1")
        rc = run("name-rev", "HEAD")
        check(rc == 0, "name-rev HEAD")

        # mktag
        from pythongit import refs as r, objects as o
        from pythongit.repo import Repository
        rp = Repository.discover(tmp)
        head = r.read_ref(rp, "refs/heads/main")
        tag_text = f"object {head}\ntype commit\ntag mytag\ntagger t <t@e.com> 0 +0000\n\nmessage\n"
        old_stdin = sys.stdin
        sys.stdin = FakeStdin(tag_text)
        try:
            rc = run("mktag")
            check(rc == 0, "mktag")
        finally:
            sys.stdin = old_stdin

        # var
        rc = run("var", "GIT_AUTHOR_IDENT")
        check(rc == 0, "var GIT_AUTHOR_IDENT")
        rc = run("var", "GIT_EDITOR")
        check(rc == 0, "var GIT_EDITOR")

        # stripspace
        sys.stdin = FakeStdin("  hello  \n\n\nworld\n\n# comment\n\n")
        try:
            rc = run("stripspace")
            check(rc == 0, "stripspace")
        finally:
            sys.stdin = old_stdin

        # update-server-info
        rc = run("update-server-info")
        check(rc == 0, "update-server-info")
        check((tmp / ".git" / "info" / "refs").exists(), "info/refs written")

        # replace
        head_sha = r.read_ref(rp, "refs/heads/main")
        # Create another commit to replace with
        (tmp / "c.py").write_text("# x\n"); run("add", "c.py"); run("commit", "-m", "c3")
        other = r.read_ref(rp, "refs/heads/main")
        rc = run("replace", head_sha, other)
        check(rc == 0, "replace add")
        rc = run("replace", "--list")
        check(rc == 0, "replace --list")
        rc = run("replace", "-d", head_sha)
        check(rc == 0, "replace --delete")

        # cherry: build a divergent branch and check
        run("branch", "topic")
        run("checkout", "topic")
        (tmp / "topicfile").write_text("topic\n"); run("add", "topicfile"); run("commit", "-m", "topic1")
        run("checkout", "main")
        rc = run("cherry", "main", "topic")
        check(rc == 0, "cherry runs")

        # range-diff
        rc = run("range-diff", "main..topic", "main..topic")
        check(rc == 0, "range-diff self-compare")

    finally:
        os.chdir(ROOT)
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'ALL PASSED' if failed == 0 else f'{failed} FAILED'}")
    return failed


if __name__ == "__main__":
    raise SystemExit(main())
