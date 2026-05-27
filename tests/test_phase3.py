"""Phase 3 tests: apply, format-patch, am roundtrip, clean, describe, blame,
for-each-ref, shortlog, archive, bundle, show-ref, mktree, check-ignore."""
from __future__ import annotations

import os
import shutil
import sys
import tarfile
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

    tmp = Path(tempfile.mkdtemp(prefix="pygit-p3-"))
    try:
        os.chdir(tmp)
        run("init", str(tmp))
        run("config", "user.name", "t")
        run("config", "user.email", "t@e.com")

        (tmp / "a.txt").write_text("one\ntwo\nthree\n")
        run("add", "a.txt"); run("commit", "-m", "c1")

        (tmp / "a.txt").write_text("one\nTWO\nthree\nfour\n")
        run("add", "a.txt"); run("commit", "-m", "c2")

        # format-patch -1
        out_dir = tmp / "patches"
        rc = run("format-patch", "-o", str(out_dir), "-1")
        check(rc == 0, "format-patch -1")
        patches = sorted(out_dir.glob("*.patch"))
        check(len(patches) == 1, "format-patch wrote a patch")

        # apply same patch on a fresh repo and check content matches
        tmp2 = Path(tempfile.mkdtemp(prefix="pygit-p3b-"))
        try:
            os.chdir(tmp2)
            run("init", str(tmp2))
            run("config", "user.name", "t"); run("config", "user.email", "t@e.com")
            (tmp2 / "a.txt").write_text("one\ntwo\nthree\n")
            run("add", "a.txt"); run("commit", "-m", "c1")
            rc = run("apply", str(patches[0]))
            check(rc == 0, "apply on fresh repo")
            check((tmp2 / "a.txt").read_text() == "one\nTWO\nthree\nfour\n", "apply produces expected file")

            # am: should commit with original message
            (tmp2 / "a.txt").write_text("one\ntwo\nthree\n")
            run("add", "a.txt")
            rc = run("am", str(patches[0]))
            check(rc == 0, "am applies and commits")
        finally:
            os.chdir(tmp)
            shutil.rmtree(tmp2, ignore_errors=True)

        # clean
        (tmp / "junk.txt").write_text("junk")
        rc = run("clean", "-f")
        check(rc == 0, "clean -f runs")
        check(not (tmp / "junk.txt").exists(), "clean removed untracked")

        # describe
        run("tag", "v1")
        rc = run("describe")
        check(rc == 0, "describe on tagged HEAD")

        # blame
        rc = run("blame", "a.txt")
        check(rc == 0, "blame runs")

        # for-each-ref
        rc = run("for-each-ref")
        check(rc == 0, "for-each-ref")

        # shortlog
        rc = run("shortlog", "-s", "-n")
        check(rc == 0, "shortlog -s -n")

        # archive
        rc = run("archive", "--format", "tar", "-o", str(tmp / "out.tar"), "HEAD")
        check(rc == 0, "archive --format tar")
        with tarfile.open(tmp / "out.tar") as tf:
            names = tf.getnames()
        check("a.txt" in names, "archive contains a.txt")

        # bundle
        rc = run("bundle", "create", str(tmp / "out.bundle"), "main")
        check(rc == 0, "bundle create")
        rc = run("bundle", "verify", str(tmp / "out.bundle"))
        check(rc == 0, "bundle verify")

        # show-ref
        rc = run("show-ref")
        check(rc == 0, "show-ref")

        # check-ignore
        (tmp / ".gitignore").write_text("*.log\n")
        rc = run("check-ignore", "foo.log")
        check(rc == 0, "check-ignore matches *.log")
        rc = run("check-ignore", "foo.py")
        check(rc == 1, "check-ignore non-match returns 1")

        # mktree roundtrip
        # use cat-file to get current tree, feed to mktree, compare
        head_tree = None
        from pythongit import refs as r, objects as o
        from pythongit.repo import Repository
        rp = Repository.discover(tmp)
        head = r.read_ref(rp, "refs/heads/main")
        if head:
            head_tree = o.parse_commit(o.read_object(rp, head)[1]).tree
        if head_tree:
            ls_lines = []
            for e in o.parse_tree(o.read_object(rp, head_tree)[1]):
                obj_t = "tree" if e.is_dir() else "blob"
                mode = e.mode if len(e.mode) == 6 else e.mode.zfill(6)
                ls_lines.append(f"{mode} {obj_t} {e.sha}\t{e.name}")
            import io
            old_stdin = sys.stdin
            sys.stdin = io.StringIO("\n".join(ls_lines))
            try:
                rc = run("mktree")
                check(rc == 0, "mktree runs")
            finally:
                sys.stdin = old_stdin

    finally:
        os.chdir(ROOT)
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'ALL PASSED' if failed == 0 else f'{failed} FAILED'}")
    return failed


if __name__ == "__main__":
    raise SystemExit(main())
