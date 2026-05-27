"""Phase 2 tests: merge, rebase, cherry-pick, revert, reflog, stash, merge-base."""
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


def main() -> int:
    failed = 0

    def check(cond, label):
        nonlocal failed
        print(("PASS" if cond else "FAIL") + " " + label)
        if not cond:
            failed += 1

    tmp = Path(tempfile.mkdtemp(prefix="pygit-p2-"))
    try:
        os.chdir(tmp)
        run("init", str(tmp))
        run("config", "user.name", "t")
        run("config", "user.email", "t@e.com")

        # initial commit on main
        (tmp / "a.txt").write_text("one\ntwo\nthree\n")
        run("add", "a.txt")
        run("commit", "-m", "C1")

        # branch + commit
        run("branch", "feat")
        run("checkout", "feat")
        (tmp / "a.txt").write_text("one\ntwo\nthree\nfour\n")
        run("add", "a.txt")
        run("commit", "-m", "C2 on feat")

        # back to main, divergent commit
        run("checkout", "main")
        (tmp / "b.txt").write_text("b\n")
        run("add", "b.txt")
        run("commit", "-m", "C3 on main")

        # merge-base
        rc = run("merge-base", "main", "feat")
        check(rc == 0, "merge-base finds common ancestor")

        # merge feat into main (non-ff)
        rc = run("merge", "feat")
        check(rc == 0, "merge feat into main succeeds (3-way clean)")
        check((tmp / "a.txt").read_text().endswith("four\n"), "merge produces feat's a.txt")
        check((tmp / "b.txt").exists(), "merge preserves main's b.txt")

        # reflog
        rc = run("reflog")
        check(rc == 0, "reflog runs")

        # cherry-pick: reset main to C3, then cherry-pick C2
        rc = run("log", "--oneline", "-n", "1")
        # walk back: HEAD is the merge commit; use HEAD^1 isn't supported in our parser.
        # Instead use rev-parse main and step via commit-tree -- just test cherry-pick from feat tip onto a new branch.
        run("branch", "cp")
        run("checkout", "cp")
        # reset cp to C3
        c3 = subprocess.run(["git", "rev-parse", "HEAD^1"], cwd=tmp, capture_output=True, text=True).stdout.strip() if shutil.which("git") else None
        if c3:
            run("reset", "--hard", c3)
            feat_tip = subprocess.run(["git", "rev-parse", "feat"], cwd=tmp, capture_output=True, text=True).stdout.strip()
            rc = run("cherry-pick", feat_tip)
            check(rc == 0, "cherry-pick clean")

        # revert
        rc = run("log", "--oneline", "-n", "1")
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp, capture_output=True, text=True).stdout.strip() if shutil.which("git") else None
        if head:
            rc = run("revert", head)
            check(rc == 0, "revert HEAD succeeds")

        # stash
        (tmp / "a.txt").write_text("dirty\n")
        rc = run("stash", "push", "-m", "WIP test")
        check(rc == 0, "stash push")
        check((tmp / "a.txt").read_text() != "dirty\n", "worktree reset after stash")
        rc = run("stash", "list")
        check(rc == 0, "stash list")
        rc = run("stash", "pop")
        check(rc == 0, "stash pop")
        check((tmp / "a.txt").read_text() == "dirty\n", "stash pop restores worktree")

        # rebase: build fresh chain
        os.chdir(ROOT)
        shutil.rmtree(tmp)
        tmp.mkdir()
        os.chdir(tmp)
        run("init", str(tmp))
        run("config", "user.name", "t"); run("config", "user.email", "t@e.com")
        (tmp / "f.txt").write_text("A\n"); run("add", "f.txt"); run("commit", "-m", "A")
        run("branch", "topic")
        (tmp / "f.txt").write_text("A\nB-main\n"); run("add", "f.txt"); run("commit", "-m", "B main")
        run("checkout", "topic")
        (tmp / "g.txt").write_text("g\n"); run("add", "g.txt"); run("commit", "-m", "B topic")
        rc = run("rebase", "main")
        check(rc == 0, "rebase clean")
        check((tmp / "f.txt").read_text() == "A\nB-main\n", "rebase carries main's f.txt")
        check((tmp / "g.txt").exists(), "rebase replays topic's commit")

    finally:
        os.chdir(ROOT)
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'ALL PASSED' if failed == 0 else f'{failed} FAILED'}")
    return failed


if __name__ == "__main__":
    raise SystemExit(main())
