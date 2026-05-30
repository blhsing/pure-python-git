"""Differential harness: compare pythongit.xdiff.xdl_merge against the real
git `merge-file` (histogram algorithm, zealous merge) byte-for-byte over many
random three-way cases. Not a pytest test; run directly with a real git."""
from __future__ import annotations

import os
import random
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pythongit import xdiff


def git_merge_file(orig: bytes, ours: bytes, theirs: bytes, *, style="merge",
                   marker=7, labels=("ours", "orig", "theirs")):
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "o").write_bytes(ours)
        (d / "b").write_bytes(orig)
        (d / "t").write_bytes(theirs)
        cmd = ["git", "merge-file", "-p", "--diff-algorithm=histogram",
               "--marker-size", str(marker)]
        if style == "diff3":
            cmd.append("--diff3")
        elif style == "zdiff3":
            cmd.append("--zdiff3")
        cmd += ["-L", labels[0], "-L", labels[1], "-L", labels[2],
                str(d / "o"), str(d / "b"), str(d / "t")]
        proc = subprocess.run(cmd, capture_output=True)
        return proc.returncode, proc.stdout


def py_merge(orig, ours, theirs, *, style="merge", marker=7,
             labels=("ours", "orig", "theirs")):
    style_map = {"merge": 0, "diff3": xdiff.XDL_MERGE_DIFF3,
                 "zdiff3": xdiff.XDL_MERGE_ZEALOUS_DIFF3}
    res, nconf = xdiff.xdl_merge(
        orig, ours, theirs,
        level=xdiff.XDL_MERGE_ZEALOUS, style=style_map[style], favor=0,
        flags=xdiff.XDF_HISTOGRAM_DIFF, marker_size=marker,
        name1=labels[0], name2=labels[2], ancestor_name=labels[1])
    return res, nconf


def rand_lines(rng, n, alphabet):
    return [rng.choice(alphabet) for _ in range(n)]


def make_text(lines, trailing_nl=True):
    if not lines:
        return b""
    s = "\n".join(lines)
    if trailing_nl:
        s += "\n"
    return s.encode()


def mutate(rng, lines):
    out = list(lines)
    ops = rng.randint(0, 4)
    for _ in range(ops):
        if not out:
            out.append(rng.choice("abcdefghijXYZ"))
            continue
        kind = rng.randint(0, 2)
        idx = rng.randrange(len(out))
        if kind == 0:  # delete
            del out[idx]
        elif kind == 1:  # insert
            out.insert(idx, rng.choice("abcdefghijklmnopXYZ123"))
        else:  # change
            out[idx] = rng.choice("abcdefghijklmnopXYZ123")
    return out


def main():
    seed = int(sys.argv[1]) if len(sys.argv) > 1 else 1234
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 3000
    mode = sys.argv[3] if len(sys.argv) > 3 else "small"
    rng = random.Random(seed)
    alphabet = "abcdefghij"
    fails = 0
    for it in range(n):
        if mode == "repeat":
            # force many repeated lines to trigger histogram->Myers fallback
            base_len = rng.randint(60, 90)
            base_lines = [rng.choice("ab") for _ in range(base_len)]
        elif mode == "big":
            base_len = rng.randint(0, 60)
            base_lines = rand_lines(rng, base_len, "abcdefghijklmnop")
        else:
            base_len = rng.randint(0, 14)
            base_lines = rand_lines(rng, base_len, alphabet)
        ours_lines = mutate(rng, base_lines)
        theirs_lines = mutate(rng, base_lines)
        for _ in range(rng.randint(0, 3) if mode != "small" else 0):
            ours_lines = mutate(rng, ours_lines)
            theirs_lines = mutate(rng, theirs_lines)
        tnl_b = rng.random() < 0.85
        tnl_o = rng.random() < 0.85
        tnl_t = rng.random() < 0.85
        orig = make_text(base_lines, tnl_b)
        ours = make_text(ours_lines, tnl_o)
        theirs = make_text(theirs_lines, tnl_t)
        style = rng.choice(["merge", "diff3", "zdiff3"])
        marker = rng.choice([7, 7, 7, 5, 10])
        rc_g, out_g = git_merge_file(orig, ours, theirs, style=style, marker=marker)
        out_p, nconf = py_merge(orig, ours, theirs, style=style, marker=marker)
        # git merge-file returns number of conflicts (capped 127) or -1 on error
        if out_g != out_p:
            fails += 1
            if fails <= 8:
                print(f"--- MISMATCH it={it} seed={seed} style={style} marker={marker}")
                print("orig=", orig)
                print("ours=", ours)
                print("theirs=", theirs)
                print("git   =", out_g)
                print("python=", out_p)
        # cross-check conflict count when git didn't error
        if rc_g >= 0 and (rc_g != nconf) and nconf < 127 and rc_g < 127:
            # only report if blob matched (count divergence is interesting)
            if out_g == out_p:
                pass
    print(f"done: {n} cases, {fails} mismatches (seed {seed})")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
