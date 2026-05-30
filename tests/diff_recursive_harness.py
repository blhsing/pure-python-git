"""Differential harness for the recursive (virtual merge base) ort path:
build criss-cross histories (2+ merge bases) and compare
pythongit.ort.merge_commits against `git merge-tree --write-tree <A> <B>`
(no --merge-base) byte-for-byte (result tree + conflicted stages)."""
from __future__ import annotations

import os
import random
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pythongit.repo import Repository
from pythongit import ort


def g(d, *args, **kw):
    return subprocess.run(["git", "-C", str(d), *args], capture_output=True, **kw)


def blob(d, content: bytes) -> str:
    return g(d, "hash-object", "-w", "--stdin", input=content).stdout.decode().strip()


def mktree(d, entries: dict) -> str:
    """entries: path -> (mode, sha). Build a (possibly nested) tree."""
    # group by top-level dir
    spec_lines = []
    subdirs: dict[str, dict] = {}
    direct: dict[str, tuple] = {}
    for path, val in entries.items():
        if "/" in path:
            top, rest = path.split("/", 1)
            subdirs.setdefault(top, {})[rest] = val
        else:
            direct[path] = val
    for name, (mode, sha) in direct.items():
        spec_lines.append(f"{mode} blob {sha}\t{name}")
    for name, sub in subdirs.items():
        sub_sha = mktree(d, sub)
        spec_lines.append(f"040000 tree {sub_sha}\t{name}")
    spec = "\n".join(spec_lines) + "\n"
    return g(d, "mktree", input=spec.encode()).stdout.decode().strip()


def commit(d, tree: str, parents: list) -> str:
    args = ["commit-tree", tree]
    for p in parents:
        args += ["-p", p]
    return g(d, *args, input=b"m").stdout.decode().strip()


def rand_content(rng):
    n = rng.randint(0, 8)
    return ("\n".join(rng.choice("abcdefgh") for _ in range(n)) +
            ("\n" if rng.random() < 0.85 else "")).encode()


def file_set(rng, base, n_changes):
    files = dict(base)
    for _ in range(n_changes):
        k = rng.randint(0, 3)
        if k == 0:
            name = rng.choice("pqrs")
            files[name] = (rng.choice(["100644", "100755"]), None, rand_content(rng))
        elif k == 1 and files:
            name = rng.choice(list(files))
            files[name] = (files[name][0], None, rand_content(rng))
        elif k == 2 and files:
            name = rng.choice(list(files))
            del files[name]
        else:
            name = rng.choice("abcd")
            files.setdefault(name, ("100644", None, rand_content(rng)))
    return files


def materialize(d, files):
    """files: name -> (mode, _, content). Returns tree, writing blobs."""
    entries = {}
    for name, (mode, _sha, content) in files.items():
        entries[name] = (mode, blob(d, content))
    return mktree(d, entries) if entries else g(d, "hash-object", "-t", "tree", "-w", "--stdin", input=b"").stdout.decode().strip()


def parse_merge_tree(out: bytes):
    parts = out.split(b"\0")
    if parts and parts[-1] == b"":
        parts.pop()
    tree = parts[0].decode()
    stages = []
    for rec in parts[1:]:
        if not rec:
            continue
        meta, _, path = rec.partition(b"\t")
        mode_s, oid, stage_s = meta.decode().split()
        stages.append((path.decode(), int(stage_s), int(mode_s, 8), oid))
    return tree, sorted(stages, key=lambda t: (t[0], t[1]))


def main():
    seed = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 100
    rng = random.Random(seed)
    fails = 0
    for it in range(n):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            g(d, "init", "-q")
            g(d, "config", "user.email", "a@b.c")
            g(d, "config", "user.name", "t")
            base = {n: ("100644", None, rand_content(rng)) for n in
                    rng.sample(["a", "b", "c", "d"], rng.randint(1, 3))}
            ot = materialize(d, base)
            o = commit(d, ot, [])
            # two divergent parents off o
            pa_files = file_set(rng, base, rng.randint(1, 3))
            pb_files = file_set(rng, base, rng.randint(1, 3))
            ca = commit(d, materialize(d, pa_files), [o])
            cb = commit(d, materialize(d, pb_files), [o])
            # two criss-cross merges, each with parents ca, cb (order varies)
            m1_files = file_set(rng, pa_files, rng.randint(0, 2))
            m2_files = file_set(rng, pb_files, rng.randint(0, 2))
            if rng.random() < 0.5:
                mc1 = commit(d, materialize(d, m1_files), [ca, cb])
                mc2 = commit(d, materialize(d, m2_files), [cb, ca])
            else:
                mc1 = commit(d, materialize(d, m1_files), [ca, cb])
                mc2 = commit(d, materialize(d, m2_files), [ca, cb])
            # optionally add a tip commit on top of each merge
            if rng.random() < 0.5:
                t1 = file_set(rng, m1_files, rng.randint(0, 2))
                mc1 = commit(d, materialize(d, t1), [mc1])
            if rng.random() < 0.5:
                t2 = file_set(rng, m2_files, rng.randint(0, 2))
                mc2 = commit(d, materialize(d, t2), [mc2])

            gres = g(d, "merge-tree", "--write-tree", "--no-messages", "-z", mc1, mc2)
            if gres.returncode not in (0, 1):
                continue
            g_tree, g_stages = parse_merge_tree(gres.stdout)

            repo = Repository(d)
            try:
                res = ort.merge_commits(repo, mc1, mc2)
            except Exception as e:
                fails += 1
                if fails <= 5:
                    import traceback
                    print(f"--- EXCEPTION it={it}: {type(e).__name__}: {e}")
                    traceback.print_exc()
                continue
            p_stages = sorted(
                [(e.path, e.stage, e.mode, e.sha)
                 for e in (res.conflict_index.entries if res.conflict_index else [])
                 if e.stage],
                key=lambda t: (t[0], t[1]))
            if res.tree != g_tree or p_stages != g_stages:
                fails += 1
                if fails <= 8:
                    nbases = len(g(d, "merge-base", "--all", mc1, mc2).stdout.decode().split())
                    print(f"--- MISMATCH it={it} (#bases={nbases})")
                    print("git tree", g_tree, "stages", g_stages)
                    print("py  tree", res.tree, "stages", p_stages)
                    print("mc1", mc1, "mc2", mc2)
    print(f"done: {n} cases, {fails} mismatches (seed {seed})")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
