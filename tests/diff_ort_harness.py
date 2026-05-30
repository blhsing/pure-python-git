"""Differential harness: compare pythongit.mergeort against real
`git merge-tree --write-tree -z --merge-base` over random three-way tree
merges. Compares result tree oid, conflicted index stages, and conflicted
blob bytes. Run with a real git on PATH."""
from __future__ import annotations

import os
import random
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pythongit.repo import Repository
from pythongit import mergeort


def run(cwd, *args, **kw):
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, **kw)


def git_commit_tree(cwd, files: dict, parent_env=0):
    """Create a commit whose tree contains `files` (path->bytes), return sha.
    Builds the index directly via update-index --cacheinfo (no worktree, so
    file/dir path collisions across commits can't leak)."""
    run(cwd, "read-tree", "--empty")
    args = ["update-index", "--add"]
    for path, content in files.items():
        sha = run(cwd, "hash-object", "-w", "--stdin",
                  input=content).stdout.decode().strip()
        args += ["--cacheinfo", f"100644,{sha},{path}"]
    if files:
        run(cwd, *args)
    tree = run(cwd, "write-tree").stdout.decode().strip()
    c = run(cwd, "commit-tree", tree, input=b"x")
    return c.stdout.decode().strip(), tree


def make_text(rng, lines, tnl=True):
    if not lines:
        return b""
    s = "\n".join(lines)
    if tnl:
        s += "\n"
    return s.encode()


def rand_content(rng):
    n = rng.randint(0, 10)
    lines = [rng.choice("abcdefghij") for _ in range(n)]
    return make_text(rng, lines, rng.random() < 0.85)


def mutate_files(rng, base_files, *, allow_rename=True):
    files = dict(base_files)
    nops = rng.randint(0, 4)
    for _ in range(nops):
        kind = rng.randint(0, 4)
        if kind == 0 and files:  # modify
            p = rng.choice(list(files))
            files[p] = rand_content(rng)
        elif kind == 1:  # add
            name = rng.choice("pqrstuvw") + str(rng.randint(0, 3))
            if rng.random() < 0.3:
                name = "d" + str(rng.randint(0, 2)) + "/" + name
            files[name] = rand_content(rng)
        elif kind == 2 and files:  # delete
            p = rng.choice(list(files))
            del files[p]
        elif kind == 3 and files and allow_rename:  # rename (keep content)
            p = rng.choice(list(files))
            newp = "r" + str(rng.randint(0, 5)) + "_" + p.replace("/", "_")
            files[newp] = files.pop(p)
        elif kind == 4 and files:  # rename + modify slightly
            p = rng.choice(list(files))
            data = files[p]
            lines = data.decode().split("\n")
            if lines:
                idx = rng.randrange(len(lines))
                lines[idx] = rng.choice("XYZW")
            newp = "m" + str(rng.randint(0, 5)) + "_" + p.replace("/", "_")
            files[newp] = "\n".join(lines).encode()
            del files[p]
    return files


def valid_tree(files: dict) -> bool:
    """True if no path is an ancestor directory of another (a tree can't hold
    both a file 'a/z' and a directory 'a/z/...')."""
    keys = set(files)
    for p in keys:
        parts = p.split("/")
        for i in range(1, len(parts)):
            if "/".join(parts[:i]) in keys:
                return False
    return True


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


def py_merge(repo, base, s1, s2):
    opt = mergeort.Opt(repo, base, s1, s2)
    base_t = commit_tree(repo, base)
    s1_t = commit_tree(repo, s1)
    s2_t = commit_tree(repo, s2)
    tree, clean = mergeort.merge_incore_nonrecursive(opt, base_t, s1_t, s2_t)
    stages = mergeort.conflicted_stages(opt)
    return tree, stages


def commit_tree(repo, sha):
    from pythongit import objects as objs
    t, data = objs.read_object(repo, sha)
    return objs.parse_commit(data).tree


def rename_dir(rng, files, src_dir, dst_dir):
    out = {}
    for p, c in files.items():
        if p == src_dir or p.startswith(src_dir + "/"):
            out[dst_dir + p[len(src_dir):]] = c
        else:
            out[p] = c
    return out


def main():
    seed = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 300
    mode = sys.argv[3] if len(sys.argv) > 3 else "mixed"
    rng = random.Random(seed)
    fails = 0
    tree_fail = 0
    stage_fail = 0
    for it in range(n):
        with tempfile.TemporaryDirectory() as d:
            run(d, "init", "-q")
            run(d, "config", "user.email", "a@b.c")
            run(d, "config", "user.name", "t")
            if mode in ("dirrename", "deepdir"):
                # base files concentrated in directories (nested for deepdir)
                base_files = {}
                if mode == "deepdir":
                    dirs = ["a/b", "a/c", "a/b/d", "e"]
                else:
                    dirs = ["d1", "d2"]
                for dd in dirs:
                    for _ in range(rng.randint(0, 3)):
                        base_files[f"{dd}/{rng.choice('mnop')}"] = rand_content(rng)
                if not base_files:
                    base_files["a/b/x"] = rand_content(rng)
                if rng.random() < 0.5:
                    base_files["top"] = rand_content(rng)
                base, _bt = git_commit_tree(d, base_files)
                # side1: rename a directory subtree (and maybe small edits)
                if mode == "deepdir":
                    src, dst = rng.choice([("a", "x"), ("a/b", "a/z"),
                                           ("a", "p/q"), ("a/b", "moved"),
                                           ("a/b/d", "a/dd")])
                else:
                    src, dst = "d1", rng.choice(["nd1", "moved", "d3/inner"])
                s1_files = rename_dir(rng, base_files, src, dst)
                s1_files = mutate_files(rng, s1_files, allow_rename=False)
                # side2: add files into the old directory tree + edits
                s2_files = dict(base_files)
                add_dirs = ([src, src + "/sub", "a/b", "a"] if mode == "deepdir"
                            else ["d1"])
                for _ in range(rng.randint(1, 4)):
                    ad = rng.choice(add_dirs)
                    s2_files[f"{ad}/{rng.choice('xyzw')}"] = rand_content(rng)
                s2_files = mutate_files(rng, s2_files, allow_rename=False)
                if rng.random() < 0.4:
                    s1_files, s2_files = s2_files, s1_files
            else:
                # base files
                base_files = {}
                for _ in range(rng.randint(1, 6)):
                    name = rng.choice("abcdefg")
                    if rng.random() < 0.25:
                        name = "sub/" + name
                    base_files[name] = rand_content(rng)
                base, _bt = git_commit_tree(d, base_files)
                s1_files = mutate_files(rng, base_files)
                s2_files = mutate_files(rng, base_files)
            if not (valid_tree(base_files) and valid_tree(s1_files)
                    and valid_tree(s2_files)):
                continue
            s1, _ = git_commit_tree(d, s1_files)
            s2, _ = git_commit_tree(d, s2_files)

            g = run(d, "merge-tree", "--write-tree", "--no-messages", "-z",
                    "--merge-base", base, s1, s2)
            if g.returncode not in (0, 1):
                continue
            g_tree, g_stages = parse_merge_tree(g.stdout)

            repo = Repository(Path(d))
            try:
                p_tree, p_stages = py_merge(repo, base, s1, s2)
            except Exception as e:
                fails += 1
                if fails <= 6:
                    print(f"--- EXCEPTION it={it}: {type(e).__name__}: {e}")
                    import traceback
                    traceback.print_exc()
                    print("base=", base_files)
                    print("s1=", s1_files)
                    print("s2=", s2_files)
                continue

            ok = True
            if g_tree != p_tree:
                ok = False
                tree_fail += 1
            if g_stages != p_stages:
                ok = False
                stage_fail += 1
            if not ok:
                fails += 1
                if fails <= 8:
                    print(f"--- MISMATCH it={it} seed={seed}")
                    print("base=", base_files)
                    print("s1  =", s1_files)
                    print("s2  =", s2_files)
                    print("git tree   =", g_tree, "stages=", g_stages)
                    print("py  tree   =", p_tree, "stages=", p_stages)
    print(f"done: {n} cases, {fails} mismatches "
          f"(tree={tree_fail}, stage={stage_fail}) seed {seed}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
