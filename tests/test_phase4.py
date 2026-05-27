"""Phase 4 tests: pack-objects/unpack-objects/repack/prune/verify-pack/
count-objects/index-pack/mailsplit/mailinfo/notes/bisect/worktree."""
from __future__ import annotations

import io
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

    tmp = Path(tempfile.mkdtemp(prefix="pygit-p4-"))
    try:
        os.chdir(tmp)
        run("init", str(tmp))
        run("config", "user.name", "t"); run("config", "user.email", "t@e.com")
        (tmp / "a.txt").write_text("one\n"); run("add", "a.txt"); run("commit", "-m", "c1")
        (tmp / "a.txt").write_text("one\ntwo\n"); run("add", "a.txt"); run("commit", "-m", "c2")
        (tmp / "a.txt").write_text("one\ntwo\nthree\n"); run("add", "a.txt"); run("commit", "-m", "c3")

        # count-objects
        rc = run("count-objects", "-v")
        check(rc == 0, "count-objects -v")

        # pack-objects --all
        rc = run("pack-objects", "pack", "--all")
        check(rc == 0, "pack-objects --all")
        pack_dir = tmp / ".git" / "objects" / "pack"
        packs = list(pack_dir.glob("pack-*.pack"))
        idxs = list(pack_dir.glob("pack-*.idx"))
        check(len(packs) == 1 and len(idxs) == 1, "pack + idx written")

        # verify-pack
        rc = run("verify-pack", str(packs[0]))
        check(rc == 0, "verify-pack")

        # real git can read our pack
        if shutil.which("git"):
            r = subprocess.run(["git", "verify-pack", "-v", str(packs[0])], cwd=tmp,
                               capture_output=True, text=True)
            check(r.returncode == 0, f"real git verify-pack passes (stderr={r.stderr.strip()[:120]})")

        # index-pack: drop the idx, regenerate
        idxs[0].unlink()
        rc = run("index-pack", str(packs[0]))
        check(rc == 0, "index-pack regenerates idx")
        check(packs[0].with_suffix(".idx").exists(), "idx file re-created")

        # repack -ad
        rc = run("repack", "-a", "-d")
        check(rc == 0, "repack -ad")

        # prune (nothing to prune since just repacked, but command should run)
        rc = run("prune", "-n")
        check(rc == 0, "prune -n")

        # objects still accessible after repack
        rc = run("log", "--oneline")
        check(rc == 0, "log works after repack")

        # unpack-objects (use our pack)
        pack_file = list(pack_dir.glob("pack-*.pack"))[0]
        tmp2 = Path(tempfile.mkdtemp(prefix="pygit-p4b-"))
        try:
            os.chdir(tmp2)
            run("init", str(tmp2))
            old_stdin = sys.stdin
            sys.stdin = io.BytesIO(pack_file.read_bytes()).buffer if hasattr(io.BytesIO(b""), "buffer") else sys.stdin
            try:
                # write a wrapper so cli.cmd_unpack_objects can read from stdin
                import io as _io
                class FakeStdin:
                    buffer = _io.BytesIO(pack_file.read_bytes())
                sys.stdin = FakeStdin()
                rc = run("unpack-objects")
                check(rc == 0, "unpack-objects")
            finally:
                sys.stdin = old_stdin
        finally:
            os.chdir(tmp)
            shutil.rmtree(tmp2, ignore_errors=True)

        # notes
        rc = run("notes", "add", "-m", "hello", "HEAD")
        check(rc == 0, "notes add")
        rc = run("notes", "list")
        check(rc == 0, "notes list")

        # bisect
        rc = run("bisect", "start", "HEAD")
        check(rc == 0, "bisect start (only bad)")
        rc = run("bisect", "reset")
        check(rc == 0, "bisect reset")

        # worktree
        wt = tmp / "wt-feature"
        rc = run("worktree", "add", str(wt), "HEAD")
        check(rc == 0, "worktree add")
        check((wt / "a.txt").exists(), "worktree materialized files")
        rc = run("worktree", "list")
        check(rc == 0, "worktree list")
        rc = run("worktree", "remove", str(wt))
        check(rc == 0, "worktree remove")
        check(not wt.exists(), "worktree dir removed")

        # mailsplit + mailinfo round-trip with format-patch output
        run("format-patch", "-o", str(tmp / "patches"), "-1")
        patches = list((tmp / "patches").glob("*.patch"))
        if patches:
            rc = run("mailsplit", "-o", str(tmp / "split"), str(patches[0]))
            check(rc == 0, "mailsplit")
            class FakeStdin2:
                buffer = None
                def read(self): return patches[0].read_text(encoding="utf-8")
            old_stdin = sys.stdin
            sys.stdin = FakeStdin2()
            try:
                rc = run("mailinfo", str(tmp / "msg.txt"), str(tmp / "patch.txt"))
                check(rc == 0, "mailinfo")
            finally:
                sys.stdin = old_stdin

    finally:
        os.chdir(ROOT)
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'ALL PASSED' if failed == 0 else f'{failed} FAILED'}")
    return failed


if __name__ == "__main__":
    raise SystemExit(main())
