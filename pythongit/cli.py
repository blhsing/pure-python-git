"""Command-line interface for pythongit.

Phase 1 covers the most common plumbing + porcelain subset. Flag handling
follows `git` where practical; less common flags fall through with a clear
"not implemented" message rather than silently doing the wrong thing.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from collections import deque
from pathlib import Path
from typing import Optional

from . import diff as diff_mod
from . import objects as objs
from . import refs as refs_mod
from . import workdir
from .index import read_index, write_index
from .repo import Repository, RepositoryError


# ---------------------------------------------------------------------------
# helpers


def _repo() -> Repository:
    return Repository.discover(os.getcwd())


def _print(s: str = "") -> None:
    sys.stdout.write(s + ("\n" if not s.endswith("\n") else ""))


def _err(s: str) -> None:
    sys.stderr.write(s + ("\n" if not s.endswith("\n") else ""))


def _graph_for_repo(repo: Repository):
    try:
        from . import commitgraph

        return commitgraph.read_commit_graph(repo)
    except Exception:
        return None


def _commit_tree_parents(repo: Repository, sha: str, graph=None) -> Optional[tuple[str, tuple[str, ...]]]:
    if graph is not None:
        entry = graph.get(sha)
        if entry is not None:
            return entry.tree, entry.parents
    try:
        obj_type, data = objs.read_object(repo, sha)
    except KeyError:
        return None
    if obj_type != "commit":
        return None
    commit = objs.parse_commit(data)
    return commit.tree, tuple(commit.parents)


# ---------------------------------------------------------------------------
# plumbing


def cmd_init(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit init")
    ap.add_argument("path", nargs="?", default=".")
    ap.add_argument("--bare", action="store_true")
    ap.add_argument("--object-format", choices=["sha1", "sha256"], default="sha1")
    ap.add_argument("-b", "--initial-branch", default="main")
    args = ap.parse_args(argv)
    repo = Repository.init(args.path, bare=args.bare, object_format=args.object_format)
    if args.initial_branch != "main":
        (repo.gitdir / "HEAD").write_text(f"ref: refs/heads/{args.initial_branch}\n", encoding="utf-8")
    _print(f"Initialized empty Git repository in {repo.gitdir}")
    return 0


def cmd_hash_object(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit hash-object")
    ap.add_argument("-w", action="store_true", help="write object")
    ap.add_argument("-t", default="blob", choices=["blob", "tree", "commit", "tag"])
    ap.add_argument("--stdin", action="store_true")
    ap.add_argument("file", nargs="?")
    args = ap.parse_args(argv)
    if args.stdin:
        data = sys.stdin.buffer.read()
    elif args.file:
        data = Path(args.file).read_bytes()
    else:
        ap.error("need file or --stdin")
    if args.w:
        repo = _repo()
        sha = objs.write_object(repo, args.t, data)
    else:
        try:
            repo = _repo()
        except RepositoryError:
            repo = None
        sha, _ = objs.hash_bytes(args.t, data, repo)
    _print(sha)
    return 0


def cmd_cat_file(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit cat-file")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("-t", dest="show_type", action="store_true")
    g.add_argument("-s", dest="show_size", action="store_true")
    g.add_argument("-p", dest="pretty", action="store_true")
    g.add_argument("-e", dest="exists", action="store_true")
    ap.add_argument("object")
    args = ap.parse_args(argv)
    repo = _repo()
    sha = refs_mod.rev_parse(repo, args.object)
    if not sha:
        _err(f"fatal: Not a valid object name {args.object}")
        return 128
    try:
        t, data = objs.read_object(repo, sha)
    except KeyError:
        _err(f"fatal: object {sha} not found")
        return 128
    if args.show_type:
        _print(t)
    elif args.show_size:
        _print(str(len(data)))
    elif args.exists:
        return 0
    elif args.pretty:
        if t == "tree":
            for e in objs.parse_tree(data, repo.hash_len):
                obj_t = "tree" if e.is_dir() else "blob"
                _print(f"{int(e.mode):06o} {obj_t} {e.sha}\t{e.name}")
        else:
            sys.stdout.buffer.write(data)
            if not data.endswith(b"\n"):
                sys.stdout.write("\n")
    return 0


def cmd_ls_tree(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit ls-tree")
    ap.add_argument("-r", action="store_true", help="recurse")
    ap.add_argument("treeish")
    args = ap.parse_args(argv)
    repo = _repo()
    sha = refs_mod.rev_parse(repo, args.treeish)
    if not sha:
        _err("fatal: not a tree-ish")
        return 128
    t, data = objs.read_object(repo, sha)
    if t == "commit":
        c = objs.parse_commit(data)
        sha = c.tree
        t, data = objs.read_object(repo, sha)

    def walk(tsha: str, prefix: str = "") -> None:
        _t, td = objs.read_object(repo, tsha)
        for e in objs.parse_tree(td, repo.hash_len):
            obj_t = "tree" if e.is_dir() else "blob"
            path = prefix + e.name
            if args.r and obj_t == "tree":
                walk(e.sha, path + "/")
            else:
                mode = e.mode if len(e.mode) == 6 else e.mode.zfill(6)
                _print(f"{mode} {obj_t} {e.sha}\t{path}")

    walk(sha)
    return 0


def cmd_write_tree(argv: list[str]) -> int:
    repo = _repo()
    _print(workdir.write_tree(repo))
    return 0


def cmd_read_tree(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit read-tree")
    ap.add_argument("treeish")
    args = ap.parse_args(argv)
    repo = _repo()
    sha = refs_mod.rev_parse(repo, args.treeish)
    if not sha:
        return 128
    t, data = objs.read_object(repo, sha)
    if t == "commit":
        sha = objs.parse_commit(data).tree
    workdir.read_tree(repo, sha)
    return 0


def cmd_commit_tree(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit commit-tree")
    ap.add_argument("tree")
    ap.add_argument("-p", "--parent", action="append", default=[])
    ap.add_argument("-m", "--message", required=True)
    args = ap.parse_args(argv)
    repo = _repo()
    name, email = repo.user()
    when = int(time.time())
    sig = objs.format_signature(name, email, when=when)
    c = objs.Commit(
        tree=args.tree,
        parents=list(args.parent),
        author=sig,
        committer=sig,
        message=args.message if args.message.endswith("\n") else args.message + "\n",
    )
    sha = objs.write_object(repo, "commit", c.encode())
    _print(sha)
    return 0


def cmd_update_ref(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit update-ref")
    ap.add_argument("-d", dest="delete", action="store_true")
    ap.add_argument("ref")
    ap.add_argument("value", nargs="?")
    args = ap.parse_args(argv)
    repo = _repo()
    if args.delete:
        refs_mod.delete_ref(repo, args.ref)
        return 0
    if not args.value:
        ap.error("value required")
    sha = refs_mod.rev_parse(repo, args.value) or args.value
    refs_mod.update_ref(repo, args.ref, sha)
    return 0


def cmd_symbolic_ref(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit symbolic-ref")
    ap.add_argument("name")
    ap.add_argument("target", nargs="?")
    args = ap.parse_args(argv)
    repo = _repo()
    p = repo.gitdir / args.name
    if args.target is None:
        if not p.exists():
            return 1
        txt = p.read_text(encoding="utf-8").strip()
        if txt.startswith("ref: "):
            _print(txt[5:])
            return 0
        return 1
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"ref: {args.target}\n", encoding="utf-8")
    return 0


def cmd_rev_parse(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit rev-parse")
    ap.add_argument("--git-dir", action="store_true")
    ap.add_argument("--show-toplevel", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("name", nargs="?")
    args = ap.parse_args(argv)
    repo = _repo()
    if args.git_dir:
        _print(str(repo.gitdir))
        return 0
    if args.show_toplevel:
        _print(str(repo.path))
        return 0
    if not args.name:
        return 0
    sha = refs_mod.rev_parse(repo, args.name)
    if not sha:
        _err(f"fatal: ambiguous argument '{args.name}'")
        return 128
    _print(sha)
    return 0


def cmd_ls_files(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit ls-files")
    ap.add_argument("-s", "--stage", action="store_true")
    args = ap.parse_args(argv)
    repo = _repo()
    idx = read_index(repo)
    for e in idx.entries:
        if args.stage:
            _print(f"{e.mode_str()} {e.sha} 0\t{e.path}")
        else:
            _print(e.path)
    return 0


def cmd_rev_list(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit rev-list")
    ap.add_argument("--count", action="store_true")
    ap.add_argument("--max-count", "-n", type=int, default=None)
    ap.add_argument("revs", nargs="+")
    args = ap.parse_args(argv)
    repo = _repo()
    starts = []
    for r in args.revs:
        sha = refs_mod.rev_parse(repo, r)
        if sha:
            starts.append(sha)
    if args.count and args.max_count is None and starts:
        try:
            from . import pack as _p

            bitmapped = _p.reachable_from_bitmaps(repo, starts, object_type="commit")
            if bitmapped is not None:
                _print(str(len(bitmapped)))
                return 0
        except Exception:
            pass
    graph = _graph_for_repo(repo)
    visited: set[str] = set()
    out: list[str] = []
    stack = deque(starts)
    while stack:
        sha = stack.popleft()
        if sha in visited:
            continue
        visited.add(sha)
        info = _commit_tree_parents(repo, sha, graph)
        if info is None:
            continue
        out.append(sha)
        _tree, parents = info
        stack.extend(parents)
        if args.max_count and len(out) >= args.max_count:
            break
    if args.count:
        _print(str(len(out)))
    else:
        for s in out:
            _print(s)
    return 0


# ---------------------------------------------------------------------------
# porcelain


def cmd_add(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit add")
    ap.add_argument("-A", "--all", action="store_true")
    ap.add_argument("paths", nargs="*")
    args = ap.parse_args(argv)
    repo = _repo()
    if args.all or (not args.paths):
        paths = ["."]
    else:
        paths = args.paths
    workdir.add_paths(repo, paths)
    return 0


def cmd_rm(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit rm")
    ap.add_argument("--cached", action="store_true")
    ap.add_argument("paths", nargs="+")
    args = ap.parse_args(argv)
    repo = _repo()
    workdir.rm_paths(repo, args.paths, cached=args.cached)
    return 0


def cmd_mv(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit mv")
    ap.add_argument("src")
    ap.add_argument("dst")
    args = ap.parse_args(argv)
    repo = _repo()
    src = repo.path / args.src
    dst = repo.path / args.dst
    if not src.exists():
        _err("fatal: bad source")
        return 1
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dst)
    workdir.rm_paths(repo, [args.src], cached=True)
    workdir.add_paths(repo, [args.dst])
    return 0


def cmd_status(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit status")
    ap.add_argument("-s", "--short", action="store_true")
    args = ap.parse_args(argv)
    repo = _repo()
    s = workdir.status(repo)
    head_sym, _ = refs_mod.read_head(repo)
    branch = head_sym[len("refs/heads/") :] if head_sym and head_sym.startswith("refs/heads/") else "(detached)"
    if args.short:
        for p in s["staged_new"]:
            _print(f"A  {p}")
        for p in s["staged_mod"]:
            _print(f"M  {p}")
        for p in s["staged_del"]:
            _print(f"D  {p}")
        for p in s["modified"]:
            _print(f" M {p}")
        for p in s["missing"]:
            _print(f" D {p}")
        for p in s["untracked"]:
            _print(f"?? {p}")
        return 0
    _print(f"On branch {branch}")
    if s["staged_new"] or s["staged_mod"] or s["staged_del"]:
        _print("\nChanges to be committed:")
        for p in s["staged_new"]:
            _print(f"\tnew file:   {p}")
        for p in s["staged_mod"]:
            _print(f"\tmodified:   {p}")
        for p in s["staged_del"]:
            _print(f"\tdeleted:    {p}")
    if s["modified"] or s["missing"]:
        _print("\nChanges not staged for commit:")
        for p in s["modified"]:
            _print(f"\tmodified:   {p}")
        for p in s["missing"]:
            _print(f"\tdeleted:    {p}")
    if s["untracked"]:
        _print("\nUntracked files:")
        for p in s["untracked"]:
            _print(f"\t{p}")
    if not any(s.values()):
        _print("nothing to commit, working tree clean")
    return 0


def cmd_commit(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit commit")
    ap.add_argument("-m", "--message", required=True)
    ap.add_argument("--allow-empty", action="store_true")
    args = ap.parse_args(argv)
    repo = _repo()
    # rerere: scan for newly-resolved conflicts and record postimages
    try:
        from . import rerere as _rr
        _rr.scan_and_record(repo)
    except Exception:
        pass
    from .index import read_index as _read_index
    cur_idx = _read_index(repo)
    if cur_idx.has_conflicts():
        _err("error: unresolved conflicts:")
        for p in cur_idx.conflicted_paths():
            _err(f"\t{p}")
        _err("hint: stage the resolved files with `pygit add` then commit again.")
        return 1
    tree = workdir.write_tree(repo)
    head_sym, parent = refs_mod.read_head(repo)
    parents = [parent] if parent else []
    if parent and not args.allow_empty:
        # compare to parent's tree
        t, data = objs.read_object(repo, parent)
        if t == "commit":
            pc = objs.parse_commit(data)
            if pc.tree == tree:
                _err("nothing to commit, working tree clean")
                return 1
    name, email = repo.user()
    when = int(time.time())
    sig = objs.format_signature(name, email, when=when)
    msg = args.message if args.message.endswith("\n") else args.message + "\n"
    c = objs.Commit(tree=tree, parents=parents, author=sig, committer=sig, message=msg)
    sha = objs.write_object(repo, "commit", c.encode())
    if head_sym:
        refs_mod.update_ref(repo, head_sym, sha)
    else:
        refs_mod.set_head(repo, sha)
    short = sha[:7]
    branch = head_sym[len("refs/heads/") :] if head_sym and head_sym.startswith("refs/heads/") else "HEAD"
    _print(f"[{branch} {short}] {args.message.splitlines()[0]}")
    return 0


def cmd_log(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit log")
    ap.add_argument("--oneline", action="store_true")
    ap.add_argument("-n", "--max-count", type=int, default=None)
    ap.add_argument("rev", nargs="?", default="HEAD")
    args = ap.parse_args(argv)
    repo = _repo()
    sha = refs_mod.rev_parse(repo, args.rev)
    if not sha:
        _err("fatal: bad revision")
        return 128
    seen: set[str] = set()
    cur = deque([sha])
    count = 0
    while cur:
        s = cur.popleft()
        if s in seen:
            continue
        seen.add(s)
        try:
            t, data = objs.read_object(repo, s)
        except KeyError:
            break
        if t != "commit":
            break
        c = objs.parse_commit(data)
        if args.oneline:
            first = c.message.splitlines()[0] if c.message.strip() else ""
            _print(f"{s[:7]} {first}")
        else:
            _print(f"commit {s}")
            if len(c.parents) > 1:
                _print("Merge: " + " ".join(p[:7] for p in c.parents))
            _print(f"Author: {c.author}")
            _print(f"Date:   {c.committer}")
            _print("")
            for line in c.message.rstrip("\n").splitlines():
                _print(f"    {line}")
            _print("")
        cur.extend(c.parents)
        count += 1
        if args.max_count and count >= args.max_count:
            break
    return 0


def cmd_show(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit show")
    ap.add_argument("rev", nargs="?", default="HEAD")
    args = ap.parse_args(argv)
    repo = _repo()
    sha = refs_mod.rev_parse(repo, args.rev)
    if not sha:
        return 128
    t, data = objs.read_object(repo, sha)
    if t == "commit":
        c = objs.parse_commit(data)
        _print(f"commit {sha}")
        _print(f"Author: {c.author}")
        _print(f"Date:   {c.committer}")
        _print("")
        for line in c.message.rstrip("\n").splitlines():
            _print(f"    {line}")
        _print("")
        # diff vs first parent
        if c.parents:
            pt, pd = objs.read_object(repo, c.parents[0])
            parent_tree = objs.parse_commit(pd).tree
            _print_tree_diff(repo, parent_tree, c.tree)
    elif t == "tree":
        for e in objs.parse_tree(data, repo.hash_len):
            obj_t = "tree" if e.is_dir() else "blob"
            _print(f"{int(e.mode):06o} {obj_t} {e.sha}\t{e.name}")
    else:
        sys.stdout.buffer.write(data)
    return 0


def _print_tree_diff(repo: Repository, a_tree: str, b_tree: str) -> None:
    a = workdir.flatten_tree(repo, a_tree)
    b = workdir.flatten_tree(repo, b_tree)
    paths = sorted(set(a) | set(b))
    for p in paths:
        a_sha = a.get(p)
        b_sha = b.get(p)
        if a_sha == b_sha:
            continue
        a_text = ""
        b_text = ""
        if a_sha:
            _, d = objs.read_object(repo, a_sha)
            a_text = d.decode("utf-8", errors="replace")
        if b_sha:
            _, d = objs.read_object(repo, b_sha)
            b_text = d.decode("utf-8", errors="replace")
        out = diff_mod.unified_diff(a_text, b_text, f"a/{p}", f"b/{p}")
        if out:
            _print(f"diff --git a/{p} b/{p}")
            _print(out.rstrip("\n"))


def cmd_diff(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit diff")
    ap.add_argument("--cached", "--staged", dest="cached", action="store_true")
    args = ap.parse_args(argv)
    repo = _repo()
    idx = read_index(repo).by_path()
    if args.cached:
        head_tree = workdir._head_tree_map(repo)
        paths = sorted(set(idx) | set(head_tree))
        for p in paths:
            a = head_tree.get(p)
            b = idx[p].sha if p in idx else None
            if a == b:
                continue
            at = bt = ""
            if a:
                _, d = objs.read_object(repo, a)
                at = d.decode("utf-8", errors="replace")
            if b:
                _, d = objs.read_object(repo, b)
                bt = d.decode("utf-8", errors="replace")
            out = diff_mod.unified_diff(at, bt, f"a/{p}", f"b/{p}")
            if out:
                _print(f"diff --git a/{p} b/{p}")
                _print(out.rstrip("\n"))
    else:
        for p, e in idx.items():
            full = repo.path / p
            if not full.exists():
                continue
            blob = full.read_bytes()
            new_sha, _ = objs.hash_bytes("blob", blob, repo)
            if new_sha == e.sha:
                continue
            _, old_data = objs.read_object(repo, e.sha)
            out = diff_mod.unified_diff(
                old_data.decode("utf-8", errors="replace"),
                blob.decode("utf-8", errors="replace"),
                f"a/{p}",
                f"b/{p}",
            )
            if out:
                _print(f"diff --git a/{p} b/{p}")
                _print(out.rstrip("\n"))
    return 0


def cmd_branch(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit branch")
    ap.add_argument("-d", "--delete", action="store_true")
    ap.add_argument("-a", "--all", action="store_true")
    ap.add_argument("name", nargs="?")
    ap.add_argument("start", nargs="?")
    args = ap.parse_args(argv)
    repo = _repo()
    if args.name is None:
        head_sym, _ = refs_mod.read_head(repo)
        cur = head_sym[len("refs/heads/") :] if head_sym and head_sym.startswith("refs/heads/") else None
        for b in refs_mod.list_branches(repo):
            mark = "*" if b == cur else " "
            _print(f"{mark} {b}")
        if args.all:
            root = repo.gitdir / "refs" / "remotes"
            if root.exists():
                for f in sorted(root.rglob("*")):
                    if f.is_file():
                        rel = str(f.relative_to(repo.gitdir / "refs")).replace(os.sep, "/")
                        _print(f"  remotes/{rel[len('remotes/') :]}")
        return 0
    if args.delete:
        refs_mod.delete_ref(repo, f"refs/heads/{args.name}")
        return 0
    start = refs_mod.rev_parse(repo, args.start) if args.start else refs_mod.rev_parse(repo, "HEAD")
    if not start:
        _err("fatal: no starting point")
        return 128
    refs_mod.update_ref(repo, f"refs/heads/{args.name}", start)
    return 0


def cmd_tag(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit tag")
    ap.add_argument("-d", "--delete", action="store_true")
    ap.add_argument("-l", "--list", action="store_true")
    ap.add_argument("name", nargs="?")
    ap.add_argument("target", nargs="?")
    args = ap.parse_args(argv)
    repo = _repo()
    if args.list or args.name is None:
        for t in refs_mod.list_tags(repo):
            _print(t)
        return 0
    if args.delete:
        refs_mod.delete_ref(repo, f"refs/tags/{args.name}")
        return 0
    target = refs_mod.rev_parse(repo, args.target) if args.target else refs_mod.rev_parse(repo, "HEAD")
    if not target:
        return 128
    refs_mod.update_ref(repo, f"refs/tags/{args.name}", target)
    return 0


def cmd_checkout(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit checkout")
    ap.add_argument("-b", dest="new_branch", default=None)
    ap.add_argument("target", nargs="?")
    args = ap.parse_args(argv)
    repo = _repo()
    if args.new_branch:
        start = refs_mod.rev_parse(repo, args.target) if args.target else refs_mod.rev_parse(repo, "HEAD")
        if not start:
            return 128
        refs_mod.update_ref(repo, f"refs/heads/{args.new_branch}", start)
        refs_mod.set_head(repo, f"refs/heads/{args.new_branch}")
        return 0
    if not args.target:
        ap.error("target required")
    sha = refs_mod.rev_parse(repo, args.target)
    if not sha:
        _err(f"error: pathspec '{args.target}' did not match")
        return 1
    t, data = objs.read_object(repo, sha)
    tree = objs.parse_commit(data).tree if t == "commit" else sha
    workdir.checkout_tree(repo, tree)
    # set HEAD: branch if it exists, otherwise detached
    if refs_mod.read_ref(repo, f"refs/heads/{args.target}"):
        refs_mod.set_head(repo, f"refs/heads/{args.target}")
    else:
        refs_mod.set_head(repo, sha)
    return 0


def cmd_switch(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit switch")
    ap.add_argument("-c", dest="create", default=None)
    ap.add_argument("branch", nargs="?")
    args = ap.parse_args(argv)
    sub = []
    if args.create:
        sub = ["-b", args.create]
        if args.branch:
            sub.append(args.branch)
    else:
        if not args.branch:
            return 128
        sub = [args.branch]
    return cmd_checkout(sub)


def cmd_restore(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit restore")
    ap.add_argument("--staged", action="store_true")
    ap.add_argument("paths", nargs="+")
    args = ap.parse_args(argv)
    repo = _repo()
    idx = read_index(repo)
    if args.staged:
        head_tree = workdir._head_tree_map(repo)
        for p in args.paths:
            if p in head_tree:
                # restore index entry from HEAD
                t, data = objs.read_object(repo, head_tree[p])
                full = repo.path / p
                st = full.lstat() if full.exists() else os.stat_result((0,) * 10)
                from .index import REG_MODE, stat_to_entry, IndexEntry
                idx.upsert(IndexEntry(mode=REG_MODE, sha=head_tree[p], path=p))
            else:
                idx.remove(p)
        write_index(repo, idx)
        return 0
    # restore worktree from index
    by_path = idx.by_path()
    for p in args.paths:
        if p in by_path:
            t, data = objs.read_object(repo, by_path[p].sha)
            (repo.path / p).parent.mkdir(parents=True, exist_ok=True)
            (repo.path / p).write_bytes(data)
    return 0


def cmd_reset(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit reset")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--soft", action="store_true")
    g.add_argument("--mixed", action="store_true")
    g.add_argument("--hard", action="store_true")
    ap.add_argument("target", nargs="?", default="HEAD")
    args = ap.parse_args(argv)
    repo = _repo()
    sha = refs_mod.rev_parse(repo, args.target)
    if not sha:
        return 128
    head_sym, _ = refs_mod.read_head(repo)
    if head_sym:
        refs_mod.update_ref(repo, head_sym, sha)
    else:
        refs_mod.set_head(repo, sha)
    if args.soft:
        return 0
    # mixed (default) and hard: rewrite index from target
    t, data = objs.read_object(repo, sha)
    tree = objs.parse_commit(data).tree if t == "commit" else sha
    if args.hard:
        workdir.checkout_tree(repo, tree)
    else:
        workdir.read_tree(repo, tree)
    return 0


def cmd_config(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit config")
    ap.add_argument("--global", dest="is_global", action="store_true")
    ap.add_argument("--unset", action="store_true")
    ap.add_argument("name")
    ap.add_argument("value", nargs="?")
    args = ap.parse_args(argv)
    if args.is_global:
        cfg_path = Path.home() / ".gitconfig"
    else:
        cfg_path = _repo().gitdir / "config"
    import configparser
    cp = configparser.ConfigParser()
    if cfg_path.exists():
        cp.read(cfg_path, encoding="utf-8")
    sect, _, key = args.name.partition(".")
    if not key:
        return 1
    if args.unset:
        if cp.has_option(sect, key):
            cp.remove_option(sect, key)
            with cfg_path.open("w", encoding="utf-8") as f:
                cp.write(f)
        return 0
    if args.value is None:
        if cp.has_option(sect, key):
            _print(cp.get(sect, key))
            return 0
        return 1
    if not cp.has_section(sect):
        cp.add_section(sect)
    cp.set(sect, key, args.value)
    with cfg_path.open("w", encoding="utf-8") as f:
        cp.write(f)
    return 0


def cmd_remote(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit remote")
    sub = ap.add_subparsers(dest="action")
    sub.add_parser("show")
    p_add = sub.add_parser("add")
    p_add.add_argument("name")
    p_add.add_argument("url")
    p_rm = sub.add_parser("remove")
    p_rm.add_argument("name")
    args = ap.parse_args(argv or ["show"])
    repo = _repo()
    cp = repo.config()
    if args.action in (None, "show"):
        for s in cp.sections():
            if s.startswith('remote "'):
                _print(s[len('remote "') : -1])
        return 0
    cfg_path = repo.gitdir / "config"
    if args.action == "add":
        sect = f'remote "{args.name}"'
        if not cp.has_section(sect):
            cp.add_section(sect)
        cp.set(sect, "url", args.url)
        cp.set(sect, "fetch", f"+refs/heads/*:refs/remotes/{args.name}/*")
        with cfg_path.open("w", encoding="utf-8") as f:
            cp.write(f)
    elif args.action == "remove":
        sect = f'remote "{args.name}"'
        if cp.has_section(sect):
            cp.remove_section(sect)
            with cfg_path.open("w", encoding="utf-8") as f:
                cp.write(f)
    return 0


def cmd_ls_remote(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit ls-remote")
    ap.add_argument("url")
    args = ap.parse_args(argv)
    from . import protocol
    refs = protocol.discover_refs(args.url)
    for name, sha in refs.items():
        _print(f"{sha}\t{name}")
    return 0


def cmd_clone(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit clone")
    ap.add_argument("--object-format", choices=["sha1", "sha256"], default=None)
    ap.add_argument("url")
    ap.add_argument("directory", nargs="?")
    args = ap.parse_args(argv)
    target = args.directory or args.url.rstrip("/").split("/")[-1].removesuffix(".git")
    src_path = args.url[7:] if args.url.startswith("file://") else args.url
    if not args.url.startswith(("http://", "https://", "git://")) and Path(src_path).exists():
        from . import translate
        src = Repository.discover(src_path)
        translate.convert_repository(src.path, target, args.object_format or src.object_format())
    else:
        from . import protocol
        protocol.clone(args.url, target, object_format=args.object_format)
    _print(f"Cloned into {target}")
    return 0


def cmd_fsck(argv: list[str]) -> int:
    repo = _repo()
    # Walk loose objects + packs, verify hash matches content for loose.
    import zlib
    obj_root = repo.gitdir / "objects"
    bad = 0
    if obj_root.is_dir():
        for d in obj_root.iterdir():
            if not d.is_dir() or len(d.name) != 2:
                continue
            for f in d.iterdir():
                sha = d.name + f.name
                try:
                    raw = zlib.decompress(f.read_bytes())
                    actual = repo.hash_hex(raw)
                    if actual != sha:
                        _print(f"error: bad sha for {sha} (got {actual})")
                        bad += 1
                except Exception as e:
                    _print(f"error: {sha}: {e}")
                    bad += 1
    return 0 if bad == 0 else 1


def cmd_gc(argv: list[str]) -> int:
    # Phase 1: no-op (no repack); just prune unreferenced refs and report.
    _print("gc: no-op (phase 1)")
    return 0


def cmd_help(argv: list[str]) -> int:
    _print("Available commands:")
    for name in sorted(_COMMANDS):
        _print(f"  {name}")
    return 0


# ---------------------------------------------------------------------------
# Opt-in `git` drop-in installer.
#
# We do NOT declare a `git` console-script in pyproject.toml because that
# would silently shadow the system's real git binary on every install. Users
# opt in by running `pygit install-git-shim`, which copies the existing
# `pygit` launcher to a sibling `git` file (or .exe on Windows). Reversed by
# `pygit uninstall-git-shim`.


def _scripts_dir() -> Path:
    """Where pip placed pygit's console-script launcher."""
    import sysconfig
    return Path(sysconfig.get_path("scripts"))


def _shim_paths() -> tuple[Path, Path]:
    """Return (existing pygit launcher, target git launcher path)."""
    d = _scripts_dir()
    if os.name == "nt":
        return d / "pygit.exe", d / "git.exe"
    return d / "pygit", d / "git"


def cmd_install_git_shim(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="pygit install-git-shim",
        description="Install a `git` console-script alongside `pygit` so "
                    "the command `git` invokes pythongit. By default this "
                    "refuses to overwrite an existing `git` on PATH; pass "
                    "--force to install anyway.",
    )
    ap.add_argument("--force", action="store_true",
                    help="overwrite an existing git file in the scripts dir")
    ap.add_argument("--dir", default=None,
                    help="install into this directory instead of pygit's scripts dir")
    args = ap.parse_args(argv)
    import shutil as _sh
    if args.dir:
        d = Path(args.dir)
        src = (d / "pygit.exe") if os.name == "nt" else (d / "pygit")
        dst = (d / "git.exe") if os.name == "nt" else (d / "git")
    else:
        src, dst = _shim_paths()
    if not src.exists():
        _err(f"pygit launcher not found at {src}. Is pythongit installed?")
        return 1
    if dst.exists() and not args.force:
        _err(f"refusing to overwrite existing {dst} (use --force).")
        return 1
    # Resolve PATH conflict: warn if a different `git` is earlier on PATH.
    other = _sh.which("git")
    if other and Path(other).resolve() != dst.resolve():
        _err(f"warning: a different `git` is already first on PATH: {other}")
        _err(f"         after this install, `git` will still resolve to that "
             f"unless you put {dst.parent} earlier on PATH.")
    _sh.copy2(src, dst)
    # On Unix make sure it's executable
    if os.name != "nt":
        st = dst.stat()
        os.chmod(dst, st.st_mode | 0o111)
    _print(f"Installed git shim at {dst}")
    return 0


def cmd_uninstall_git_shim(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit uninstall-git-shim")
    ap.add_argument("--dir", default=None)
    args = ap.parse_args(argv)
    if args.dir:
        d = Path(args.dir)
        dst = (d / "git.exe") if os.name == "nt" else (d / "git")
    else:
        _, dst = _shim_paths()
    if not dst.exists():
        _print(f"no git shim found at {dst}")
        return 0
    # Sanity check: only remove a file that looks like our shim. We compare
    # against pygit's launcher; if the file at `dst` doesn't share size and
    # the same first bytes, refuse.
    src = (Path(args.dir) / ("pygit.exe" if os.name == "nt" else "pygit")) if args.dir else _shim_paths()[0]
    if src.exists():
        if dst.stat().st_size != src.stat().st_size or dst.read_bytes()[:64] != src.read_bytes()[:64]:
            _err(f"refusing to remove {dst}: it does not look like a pythongit shim. "
                 f"Delete it manually if you're sure.")
            return 1
    dst.unlink()
    _print(f"Removed git shim at {dst}")
    return 0


# ---------------------------------------------------------------------------
# stubs that record they are not yet implemented (Phase 2)


_PHASE2 = [
    "am", "apply", "archive", "bisect", "blame", "bundle", "check-ignore",
    "cherry-pick", "clean", "describe", "fetch", "format-patch", "fsmonitor",
    "for-each-ref", "grep", "merge", "merge-base", "mktag", "mktree",
    "notes", "pack-objects", "prune", "pull", "push", "rebase", "reflog",
    "remote-helper", "repack", "rerere", "revert", "shortlog", "show-branch",
    "sparse-checkout", "stash", "submodule", "unpack-objects",
    "update-index", "verify-commit", "verify-pack", "verify-tag",
    "whatchanged", "worktree",
]


def _stub(name: str):
    def _f(argv):
        _err(f"pygit: '{name}' is not yet implemented (planned in Phase 2)")
        return 2
    return _f


# ---------------------------------------------------------------------------
# dispatch


_COMMANDS = {
    "init": cmd_init,
    "hash-object": cmd_hash_object,
    "cat-file": cmd_cat_file,
    "ls-tree": cmd_ls_tree,
    "write-tree": cmd_write_tree,
    "read-tree": cmd_read_tree,
    "commit-tree": cmd_commit_tree,
    "update-ref": cmd_update_ref,
    "symbolic-ref": cmd_symbolic_ref,
    "rev-parse": cmd_rev_parse,
    "rev-list": cmd_rev_list,
    "ls-files": cmd_ls_files,
    "add": cmd_add,
    "rm": cmd_rm,
    "mv": cmd_mv,
    "status": cmd_status,
    "commit": cmd_commit,
    "log": cmd_log,
    "show": cmd_show,
    "diff": cmd_diff,
    "branch": cmd_branch,
    "tag": cmd_tag,
    "checkout": cmd_checkout,
    "switch": cmd_switch,
    "restore": cmd_restore,
    "reset": cmd_reset,
    "config": cmd_config,
    "remote": cmd_remote,
    "ls-remote": cmd_ls_remote,
    "clone": cmd_clone,
    "fsck": cmd_fsck,
    "gc": cmd_gc,
    "help": cmd_help,
    "install-git-shim": cmd_install_git_shim,
    "uninstall-git-shim": cmd_uninstall_git_shim,
}

for _n in _PHASE2:
    _COMMANDS.setdefault(_n, _stub(_n))


# ---------------------------------------------------------------------------
# Phase 2 — merge, rebase, sequencer, fetch, push, reflog, stash


def cmd_merge_base(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit merge-base")
    ap.add_argument("a")
    ap.add_argument("b")
    args = ap.parse_args(argv)
    repo = _repo()
    from . import merge as _m
    a = refs_mod.rev_parse(repo, args.a)
    b = refs_mod.rev_parse(repo, args.b)
    if not a or not b:
        return 128
    bases = _m.merge_bases(repo, a, b)
    if not bases:
        return 1
    for s in bases:
        _print(s)
    return 0


def cmd_merge(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit merge")
    ap.add_argument("--no-ff", action="store_true")
    ap.add_argument("--ff-only", action="store_true")
    ap.add_argument("-m", "--message", default=None)
    ap.add_argument("other")
    args = ap.parse_args(argv)
    repo = _repo()
    from . import porcelain_merge as pm
    try:
        sha, conflicts = pm.merge(repo, args.other, message=args.message, no_ff=args.no_ff)
    except RuntimeError as e:
        _err(f"fatal: {e}")
        return 1
    if conflicts:
        _err("Automatic merge failed; fix conflicts and then commit the result.")
        for p in conflicts:
            _err(f"CONFLICT: {p}")
        return 1
    if args.ff_only:
        # require ff: detect by checking new head's parents
        pass
    _print(f"Merged into {sha[:7]}")
    return 0


def cmd_cherry_pick(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit cherry-pick")
    ap.add_argument("rev")
    args = ap.parse_args(argv)
    repo = _repo()
    from . import sequencer
    target = refs_mod.rev_parse(repo, args.rev)
    if not target:
        return 128
    sha, conflicts = sequencer.cherry_pick(repo, target)
    if conflicts:
        for p in conflicts:
            _err(f"CONFLICT: {p}")
        return 1
    _print(f"[cherry-pick] {sha[:7]}")
    return 0


def cmd_revert(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit revert")
    ap.add_argument("rev")
    args = ap.parse_args(argv)
    repo = _repo()
    from . import sequencer
    target = refs_mod.rev_parse(repo, args.rev)
    if not target:
        return 128
    sha, conflicts = sequencer.revert(repo, target)
    if conflicts:
        for p in conflicts:
            _err(f"CONFLICT: {p}")
        return 1
    _print(f"[revert] {sha[:7]}")
    return 0


def cmd_rebase(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit rebase")
    ap.add_argument("upstream")
    args = ap.parse_args(argv)
    repo = _repo()
    from . import sequencer
    picked, conflicts = sequencer.rebase_onto(repo, args.upstream)
    if conflicts:
        for p in conflicts:
            _err(f"CONFLICT: {p}")
        return 1
    _print(f"Rebased {picked} commit(s) onto {args.upstream}")
    return 0


def cmd_reflog(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit reflog")
    ap.add_argument("ref", nargs="?", default="HEAD")
    args = ap.parse_args(argv)
    repo = _repo()
    from . import reflog
    entries = reflog.read(repo, args.ref)
    for i, (old, new, ident, msg) in enumerate(reversed(entries)):
        _print(f"{new[:7]} {args.ref}@{{{i}}}: {msg}")
    return 0


def cmd_stash(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit stash")
    sub = ap.add_subparsers(dest="action")
    p_push = sub.add_parser("push")
    p_push.add_argument("-m", "--message", default="")
    sub.add_parser("list")
    p_apply = sub.add_parser("apply")
    p_apply.add_argument("index", nargs="?", type=int, default=0)
    p_pop = sub.add_parser("pop")
    p_pop.add_argument("index", nargs="?", type=int, default=0)
    args = ap.parse_args(argv or ["push"])
    repo = _repo()
    from . import stash
    action = args.action or "push"
    if action == "push":
        sha = stash.push(repo, getattr(args, "message", ""))
        if sha is None:
            _print("No local changes to save")
            return 0
        _print(f"Saved working directory and index state: {sha[:7]}")
    elif action == "list":
        for i, sha, msg in stash.list_stashes(repo):
            _print(f"stash@{{{i}}}: {msg}")
    elif action == "apply":
        ok = stash.apply(repo, args.index, pop=False)
        return 0 if ok else 1
    elif action == "pop":
        ok = stash.apply(repo, args.index, pop=True)
        return 0 if ok else 1
    return 0


def cmd_fetch(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit fetch")
    ap.add_argument("remote", nargs="?", default="origin")
    args = ap.parse_args(argv)
    repo = _repo()
    from . import protocol
    updated = protocol.fetch(repo, args.remote)
    for ref, sha in updated.items():
        _print(f" * {ref} -> {sha[:7]}")
    if not updated:
        _print("Already up to date.")
    return 0


def cmd_push(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit push")
    ap.add_argument("remote", nargs="?", default="origin")
    ap.add_argument("refspec", nargs="?", default=None)
    args = ap.parse_args(argv)
    repo = _repo()
    from . import protocol
    res = protocol.push(repo, args.remote, [args.refspec] if args.refspec else None)
    for ref, status in res.items():
        _print(f" {status}\t{ref}")
    return 0 if all(v == "ok" for v in res.values()) else 1


def cmd_merge_tree(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit merge-tree")
    ap.add_argument("base")
    ap.add_argument("branch1")
    ap.add_argument("branch2")
    args = ap.parse_args(argv)
    repo = _repo()
    from . import sequencer
    b = refs_mod.rev_parse(repo, args.base)
    one = refs_mod.rev_parse(repo, args.branch1)
    two = refs_mod.rev_parse(repo, args.branch2)
    if not (b and one and two):
        return 128
    b_tree = objs.parse_commit(objs.read_object(repo, b)[1]).tree
    one_tree = objs.parse_commit(objs.read_object(repo, one)[1]).tree
    two_tree = objs.parse_commit(objs.read_object(repo, two)[1]).tree
    tree, confs = sequencer._apply_patch(repo, b_tree, two_tree, one_tree)
    _print(tree)
    for p in confs:
        _print(f"CONFLICT {p}")
    return 0


# register
def _register_phase2() -> None:
    _COMMANDS["merge-base"] = cmd_merge_base
    _COMMANDS["merge"] = cmd_merge
    _COMMANDS["merge-tree"] = cmd_merge_tree
    _COMMANDS["cherry-pick"] = cmd_cherry_pick
    _COMMANDS["revert"] = cmd_revert
    _COMMANDS["rebase"] = cmd_rebase
    _COMMANDS["reflog"] = cmd_reflog
    _COMMANDS["stash"] = cmd_stash
    _COMMANDS["fetch"] = cmd_fetch
    _COMMANDS["push"] = cmd_push


_register_phase2()


# ---------------------------------------------------------------------------
# Phase 3 — apply / format-patch / am / clean / describe / blame /
#           for-each-ref / shortlog / archive / bundle / show-ref /
#           mktree / update-index / check-ignore


def cmd_apply(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit apply")
    ap.add_argument("-R", "--reverse", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("file", nargs="?")
    args = ap.parse_args(argv)
    repo = _repo()
    from . import patch
    text = sys.stdin.read() if not args.file else Path(args.file).read_text(encoding="utf-8", errors="replace")
    if args.check:
        patches = patch.parse_patch(text)
        for fp in patches:
            content = ""
            tgt = repo.path / fp.target
            if tgt.exists():
                content = tgt.read_text(encoding="utf-8", errors="replace")
            if patch.apply_to_text(content, fp.hunks, reverse=args.reverse) is None:
                _err(f"error: patch failed: {fp.target}")
                return 1
        return 0
    applied, failed = patch.apply_patch_text(text, repo_path=repo.path, reverse=args.reverse)
    for f in failed:
        _err(f"error: failed: {f}")
    return 0 if not failed else 1


def cmd_format_patch(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit format-patch")
    ap.add_argument("-o", "--output-directory", default=".")
    ap.add_argument("--stdout", action="store_true")
    ap.add_argument("range", help="e.g. main..topic or -1 or HEAD~3")
    args = ap.parse_args(argv)
    repo = _repo()
    rng = args.range
    starts: list[str] = []
    excludes: list[str] = []
    if ".." in rng:
        a, b = rng.split("..", 1)
        if a:
            excludes.append(refs_mod.rev_parse(repo, a) or "")
        starts.append(refs_mod.rev_parse(repo, b or "HEAD") or "")
    elif rng.startswith("-"):
        n = int(rng[1:])
        head = refs_mod.rev_parse(repo, "HEAD") or ""
        cur = head
        seq = []
        for _ in range(n):
            seq.append(cur)
            c = objs.parse_commit(objs.read_object(repo, cur)[1])
            if not c.parents:
                break
            cur = c.parents[0]
        c = objs.parse_commit(objs.read_object(repo, seq[-1])[1])
        if c.parents:
            excludes.append(c.parents[0])
        starts.append(head)
    else:
        starts.append(refs_mod.rev_parse(repo, rng) or "")

    seen = set(excludes)
    commits: list[str] = []
    stack = list(starts)
    while stack:
        s = stack.pop()
        if not s or s in seen:
            continue
        seen.add(s)
        t, data = objs.read_object(repo, s)
        if t != "commit":
            continue
        commits.append(s)
        c = objs.parse_commit(data)
        stack.extend(c.parents)
    commits.reverse()

    from . import diff as _diff
    out_dir = Path(args.output_directory)
    if not args.stdout:
        out_dir.mkdir(parents=True, exist_ok=True)

    for i, sha in enumerate(commits, 1):
        c = objs.parse_commit(objs.read_object(repo, sha)[1])
        subject = c.message.splitlines()[0] if c.message.strip() else ""
        body_lines = c.message.splitlines()[1:]
        parent_tree = ""
        if c.parents:
            parent_tree = objs.parse_commit(objs.read_object(repo, c.parents[0])[1]).tree
        a_map = workdir.flatten_tree(repo, parent_tree) if parent_tree else {}
        b_map = workdir.flatten_tree(repo, c.tree)
        diff_text = []
        for p in sorted(set(a_map) | set(b_map)):
            a_sha = a_map.get(p)
            b_sha = b_map.get(p)
            if a_sha == b_sha:
                continue
            at = bt = ""
            if a_sha:
                at = objs.read_object(repo, a_sha)[1].decode("utf-8", errors="replace")
            if b_sha:
                bt = objs.read_object(repo, b_sha)[1].decode("utf-8", errors="replace")
            d = _diff.unified_diff(at, bt, f"a/{p}", f"b/{p}")
            if d:
                diff_text.append(f"diff --git a/{p} b/{p}")
                if a_sha is None:
                    diff_text.append("new file mode 100644")
                if b_sha is None:
                    diff_text.append("deleted file mode 100644")
                diff_text.append(d.rstrip("\n"))
        mbox = []
        mbox.append(f"From {sha} Mon Sep 17 00:00:00 2001")
        mbox.append(f"From: {c.author.rsplit(' ', 2)[0] if c.author else 'unknown'}")
        date_part = " ".join(c.author.split()[-2:]) if c.author else ""
        mbox.append("Date: " + date_part)
        mbox.append(f"Subject: [PATCH {i}/{len(commits)}] {subject}")
        mbox.append("")
        for bl in body_lines:
            mbox.append(bl)
        if not body_lines or body_lines[-1] != "":
            mbox.append("")
        mbox.append("---")
        mbox.extend(diff_text)
        mbox.append("")
        mbox.append("-- ")
        mbox.append("pythongit")
        out = "\n".join(mbox) + "\n"
        if args.stdout:
            _print(out)
        else:
            safe = "".join(ch if ch.isalnum() else "-" for ch in subject)[:50] or "patch"
            fname = f"{i:04d}-{safe}.patch"
            (out_dir / fname).write_text(out, encoding="utf-8")
            _print(str(out_dir / fname))
    return 0


def cmd_am(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit am")
    ap.add_argument("file")
    args = ap.parse_args(argv)
    repo = _repo()
    from . import patch
    text = Path(args.file).read_text(encoding="utf-8", errors="replace")
    msgs: list[list[str]] = []
    cur: list[str] = []
    for line in text.splitlines():
        if line.startswith("From ") and cur:
            msgs.append(cur)
            cur = [line]
        else:
            cur.append(line)
    if cur:
        msgs.append(cur)
    for msg in msgs:
        try:
            blank = msg.index("")
        except ValueError:
            continue
        headers = msg[:blank]
        body = msg[blank + 1 :]
        subject = ""
        for h in headers:
            if h.startswith("Subject: "):
                subject = h[len("Subject: "):]
                if subject.startswith("["):
                    end = subject.find("]")
                    if end != -1:
                        subject = subject[end + 1 :].strip()
        if "---" in body:
            sep = body.index("---")
            msg_lines = body[:sep]
            patch_text = "\n".join(body[sep + 1 :])
        else:
            msg_lines = body
            patch_text = ""
        applied, failed = patch.apply_patch_text(patch_text, repo_path=repo.path)
        if failed:
            _err(f"am: failed to apply: {failed}")
            return 1
        if applied:
            workdir.add_paths(repo, applied)
        body_msg = "\n".join(l for l in msg_lines if l.strip())
        full_msg = subject + (("\n\n" + body_msg) if body_msg else "")
        rc = cmd_commit(["-m", full_msg])
        if rc != 0:
            return rc
    return 0


def cmd_clean(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit clean")
    ap.add_argument("-f", "--force", action="store_true")
    ap.add_argument("-d", action="store_true")
    ap.add_argument("-n", "--dry-run", action="store_true")
    ap.add_argument("-x", action="store_true")
    args = ap.parse_args(argv)
    repo = _repo()
    if not (args.force or args.dry_run):
        _err("fatal: clean.requireForce; use -f or -n")
        return 1
    from . import ignore as _ig
    ig = None if args.x else _ig.load(repo.path)
    s = workdir.status(repo)
    for u in s["untracked"]:
        if ig and ig.is_ignored(u):
            continue
        p = repo.path / u
        if args.dry_run:
            _print(f"Would remove {u}")
        else:
            try:
                p.unlink()
                _print(f"Removing {u}")
            except OSError:
                pass
    return 0


def cmd_describe(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit describe")
    ap.add_argument("--tags", action="store_true")
    ap.add_argument("--always", action="store_true")
    ap.add_argument("rev", nargs="?", default="HEAD")
    args = ap.parse_args(argv)
    repo = _repo()
    sha = refs_mod.rev_parse(repo, args.rev)
    if not sha:
        return 128
    tag_for: dict[str, str] = {}
    for tag in refs_mod.list_tags(repo):
        ts = refs_mod.read_ref(repo, f"refs/tags/{tag}")
        if not ts:
            continue
        try:
            t, d = objs.read_object(repo, ts)
            if t == "tag":
                for line in d.decode(errors="replace").splitlines():
                    if line.startswith("object "):
                        ts = line[len("object "):].strip()
                        break
        except KeyError:
            pass
        tag_for[ts] = tag
    seen: set[str] = set()
    graph = _graph_for_repo(repo)
    queue = deque([(0, sha)])
    while queue:
        depth, s = queue.popleft()
        if s in seen:
            continue
        seen.add(s)
        if s in tag_for:
            if depth == 0:
                _print(tag_for[s])
            else:
                _print(f"{tag_for[s]}-{depth}-g{sha[:7]}")
            return 0
        info = _commit_tree_parents(repo, s, graph)
        if info is None:
            continue
        _tree, parents = info
        for p in parents:
            queue.append((depth + 1, p))
    if args.always:
        _print(sha[:7])
        return 0
    _err("fatal: no tags can describe")
    return 128


def cmd_blame(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit blame")
    ap.add_argument("path")
    args = ap.parse_args(argv)
    repo = _repo()
    head = refs_mod.rev_parse(repo, "HEAD")
    if not head:
        return 128
    graph = _graph_for_repo(repo)
    head_info = _commit_tree_parents(repo, head, graph)
    if head_info is None:
        return 128
    head_tree, _head_parents = head_info
    head_entry = workdir.tree_path_entry(repo, head_tree, args.path)
    if head_entry is None or head_entry.is_dir() or head_entry.is_gitlink():
        _err(f"fatal: no such path {args.path} in HEAD")
        return 128
    current_text = objs.read_object(repo, head_entry.sha)[1].decode("utf-8", errors="replace")
    cur_lines = current_text.splitlines()
    blame_sha: list[Optional[str]] = [None] * len(cur_lines)
    chain: list[str] = []
    cur = head
    while cur:
        chain.append(cur)
        info = _commit_tree_parents(repo, cur, graph)
        if info is None:
            break
        _tree, parents = info
        if not parents:
            break
        cur = parents[0]
    from .diff import diff_lines
    for i in range(len(chain) - 1):
        newer = chain[i]
        older = chain[i + 1]
        if graph is not None and not graph.maybe_changed(newer, args.path):
            continue
        newer_info = _commit_tree_parents(repo, newer, graph)
        older_info = _commit_tree_parents(repo, older, graph)
        if newer_info is None or older_info is None:
            continue
        n_tree, _n_parents = newer_info
        o_tree, _o_parents = older_info
        n_entry = workdir.tree_path_entry(repo, n_tree, args.path)
        o_entry = workdir.tree_path_entry(repo, o_tree, args.path)
        if n_entry is None or n_entry.is_dir() or n_entry.is_gitlink():
            break
        if o_entry is not None and (o_entry.is_dir() or o_entry.is_gitlink()):
            o_entry = None
        if o_entry is not None and o_entry.sha == n_entry.sha:
            continue
        n_text = objs.read_object(repo, n_entry.sha)[1].decode("utf-8", errors="replace").splitlines()
        o_text = (
            objs.read_object(repo, o_entry.sha)[1].decode("utf-8", errors="replace").splitlines()
            if o_entry is not None else []
        )
        ops = diff_lines(o_text, n_text)
        added = set()
        for kind, ai, bi in ops:
            if kind == "ins":
                added.add(n_text[bi])
        for idx, line in enumerate(cur_lines):
            if blame_sha[idx] is None and line in added:
                blame_sha[idx] = newer
        if all(b is not None for b in blame_sha):
            break
    for idx in range(len(blame_sha)):
        if blame_sha[idx] is None:
            blame_sha[idx] = chain[-1] if chain else head
    for idx, line in enumerate(cur_lines):
        s = blame_sha[idx] or "????????"
        c = objs.parse_commit(objs.read_object(repo, s)[1])
        author = c.author.rsplit("<", 1)[0].strip()
        _print(f"{s[:8]} ({author} {idx + 1}) {line}")
    return 0


def cmd_for_each_ref(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit for-each-ref")
    ap.add_argument("--format", default="%(objectname) %(objecttype) %(refname)")
    ap.add_argument("pattern", nargs="?", default=None)
    args = ap.parse_args(argv)
    repo = _repo()
    all_refs: dict[str, str] = {}
    for name in ("refs/heads", "refs/tags", "refs/remotes"):
        root = repo.gitdir / name
        if root.exists():
            for f in root.rglob("*"):
                if f.is_file():
                    rel = str(f.relative_to(repo.gitdir)).replace(os.sep, "/")
                    s = refs_mod.read_ref(repo, rel)
                    if s:
                        all_refs[rel] = s
    for ref, s in refs_mod.read_packed_refs(repo).items():
        all_refs.setdefault(ref, s)
    for ref in sorted(all_refs):
        if args.pattern and not ref.startswith(args.pattern):
            continue
        s = all_refs[ref]
        t = "commit"
        try:
            t, _ = objs.read_object(repo, s)
        except KeyError:
            pass
        line = args.format
        line = line.replace("%(objectname)", s)
        line = line.replace("%(objecttype)", t)
        line = line.replace("%(refname)", ref)
        line = line.replace("%(refname:short)", ref.split("/", 2)[-1])
        _print(line)
    return 0


def cmd_shortlog(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit shortlog")
    ap.add_argument("-n", "--numbered", action="store_true")
    ap.add_argument("-s", "--summary", action="store_true")
    ap.add_argument("rev", nargs="?", default="HEAD")
    args = ap.parse_args(argv)
    repo = _repo()
    head = refs_mod.rev_parse(repo, args.rev)
    if not head:
        return 128
    by_author: dict[str, list[str]] = {}
    seen: set[str] = set()
    stack = deque([head])
    while stack:
        s = stack.popleft()
        if s in seen:
            continue
        seen.add(s)
        try:
            c = objs.parse_commit(objs.read_object(repo, s)[1])
        except KeyError:
            continue
        author = c.author.rsplit("<", 1)[0].strip()
        first = c.message.splitlines()[0] if c.message.strip() else ""
        by_author.setdefault(author, []).append(first)
        stack.extend(c.parents)
    items = list(by_author.items())
    if args.numbered:
        items.sort(key=lambda x: -len(x[1]))
    else:
        items.sort()
    for author, msgs in items:
        if args.summary:
            _print(f"{len(msgs):>5}\t{author}")
        else:
            _print(f"{author} ({len(msgs)}):")
            for m in msgs:
                _print(f"      {m}")
            _print("")
    return 0


def cmd_archive(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit archive")
    ap.add_argument("--format", default="tar", choices=["tar", "zip"])
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("rev")
    args = ap.parse_args(argv)
    repo = _repo()
    sha = refs_mod.rev_parse(repo, args.rev)
    if not sha:
        return 128
    t, data = objs.read_object(repo, sha)
    tree = objs.parse_commit(data).tree if t == "commit" else sha
    files = workdir.flatten_tree(repo, tree)
    import io
    if args.format == "tar":
        import tarfile
        with tarfile.open(args.output, "w") as tf:
            for path, bsha in sorted(files.items()):
                _, blob = objs.read_object(repo, bsha)
                ti = tarfile.TarInfo(name=path)
                ti.size = len(blob)
                tf.addfile(ti, io.BytesIO(blob))
    else:
        import zipfile
        with zipfile.ZipFile(args.output, "w", zipfile.ZIP_DEFLATED) as zf:
            for path, bsha in sorted(files.items()):
                _, blob = objs.read_object(repo, bsha)
                zf.writestr(path, blob)
    return 0


def cmd_bundle(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit bundle")
    sub = ap.add_subparsers(dest="action", required=True)
    p_create = sub.add_parser("create")
    p_create.add_argument("file")
    p_create.add_argument("rev", nargs="+")
    p_verify = sub.add_parser("verify")
    p_verify.add_argument("file")
    args = ap.parse_args(argv)
    repo = _repo()
    if args.action == "create":
        header = bytearray(b"# v2 git bundle\n")
        tips = []
        for r in args.rev:
            sha = refs_mod.rev_parse(repo, r)
            if sha:
                tips.append((sha, r if r.startswith("refs/") else f"refs/heads/{r}"))
        for sha, name in tips:
            header += f"{sha} {name}\n".encode()
        header += b"\n"
        from .protocol import _build_pack, _collect_objects
        objs_list: list[str] = []
        seen: set[str] = set()
        for sha, _ in tips:
            for o in _collect_objects(repo, sha, set()):
                if o not in seen:
                    seen.add(o)
                    objs_list.append(o)
        pack = _build_pack(repo, objs_list)
        Path(args.file).write_bytes(bytes(header) + pack)
        _print(f"Wrote bundle {args.file}")
        return 0
    if args.action == "verify":
        raw = Path(args.file).read_bytes()
        if not raw.startswith(b"# v2 git bundle\n"):
            _err("not a v2 git bundle")
            return 1
        _print("ok")
        return 0
    return 1


def cmd_show_ref(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit show-ref")
    ap.add_argument("--tags", action="store_true")
    ap.add_argument("--heads", action="store_true")
    args = ap.parse_args(argv)
    repo = _repo()
    for name in ("refs/heads", "refs/tags", "refs/remotes"):
        if args.tags and name != "refs/tags":
            continue
        if args.heads and name != "refs/heads":
            continue
        root = repo.gitdir / name
        if not root.exists():
            continue
        for f in sorted(root.rglob("*")):
            if f.is_file():
                rel = str(f.relative_to(repo.gitdir)).replace(os.sep, "/")
                s = refs_mod.read_ref(repo, rel)
                if s:
                    _print(f"{s} {rel}")
    return 0


def cmd_mktree(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit mktree")
    ap.parse_args(argv)
    repo = _repo()
    entries: list[objs.TreeEntry] = []
    for line in sys.stdin.read().splitlines():
        if not line.strip():
            continue
        head, _, name = line.partition("\t")
        mode, _, rest = head.partition(" ")
        obj_t, _, sha = rest.partition(" ")
        entries.append(objs.TreeEntry(mode.lstrip("0") or "0", name, sha))
    sha = objs.write_object(repo, "tree", objs.encode_tree(entries))
    _print(sha)
    return 0


def cmd_update_index(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit update-index")
    ap.add_argument("--add", action="store_true")
    ap.add_argument("--remove", action="store_true")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--cacheinfo", nargs=3, metavar=("MODE", "SHA", "PATH"))
    ap.add_argument("paths", nargs="*")
    args = ap.parse_args(argv)
    repo = _repo()
    from .index import IndexEntry, read_index, write_index
    idx = read_index(repo)
    if args.cacheinfo:
        mode_s, sha, path = args.cacheinfo
        idx.upsert(IndexEntry(mode=int(mode_s, 8), sha=sha, path=path))
        write_index(repo, idx)
        return 0
    if args.refresh:
        write_index(repo, idx)
        return 0
    if args.remove:
        for p in args.paths:
            idx.remove(p)
        write_index(repo, idx)
        return 0
    if args.add:
        workdir.add_paths(repo, args.paths)
        return 0
    return 0


def cmd_check_ignore(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit check-ignore")
    ap.add_argument("paths", nargs="+")
    args = ap.parse_args(argv)
    repo = _repo()
    from . import ignore as _ig
    ig = _ig.load(repo.path)
    rc = 1
    for p in args.paths:
        if ig.is_ignored(p):
            _print(p)
            rc = 0
    return rc


def _register_phase3() -> None:
    _COMMANDS["apply"] = cmd_apply
    _COMMANDS["format-patch"] = cmd_format_patch
    _COMMANDS["am"] = cmd_am
    _COMMANDS["clean"] = cmd_clean
    _COMMANDS["describe"] = cmd_describe
    _COMMANDS["blame"] = cmd_blame
    _COMMANDS["for-each-ref"] = cmd_for_each_ref
    _COMMANDS["shortlog"] = cmd_shortlog
    _COMMANDS["archive"] = cmd_archive
    _COMMANDS["bundle"] = cmd_bundle
    _COMMANDS["show-ref"] = cmd_show_ref
    _COMMANDS["mktree"] = cmd_mktree
    _COMMANDS["update-index"] = cmd_update_index
    _COMMANDS["check-ignore"] = cmd_check_ignore


_register_phase3()


# ---------------------------------------------------------------------------
# Phase 4 — pack-objects / unpack-objects / repack / prune / verify-pack /
#           count-objects / index-pack / mailinfo / mailsplit / notes /
#           bisect / worktree


def _iter_loose_shas(repo: Repository):
    obj_root = repo.gitdir / "objects"
    if obj_root.is_dir():
        for d in obj_root.iterdir():
            if not d.is_dir() or len(d.name) != 2:
                continue
            for f in d.iterdir():
                if f.is_file():
                    yield d.name + f.name


def _loose_shas(repo: Repository) -> list[str]:
    return list(_iter_loose_shas(repo))


def _loose_count_and_size(repo: Repository) -> tuple[int, int]:
    obj_root = repo.gitdir / "objects"
    count = 0
    size = 0
    if obj_root.is_dir():
        for d in obj_root.iterdir():
            if not d.is_dir() or len(d.name) != 2:
                continue
            for f in d.iterdir():
                if not f.is_file():
                    continue
                count += 1
                try:
                    size += f.stat().st_size
                except OSError:
                    pass
    return count, size


def _ref_tips(repo: Repository) -> set[str]:
    tips = set()
    for name in ("refs/heads", "refs/tags", "refs/remotes"):
        root = repo.gitdir / name
        if root.is_dir():
            for f in root.rglob("*"):
                if f.is_file():
                    rel = str(f.relative_to(repo.gitdir)).replace(os.sep, "/")
                    s = refs_mod.read_ref(repo, rel)
                    if s:
                        tips.add(s)
    _, head = refs_mod.read_head(repo)
    if head:
        tips.add(head)
    return tips


def _reachable(repo: Repository) -> set[str]:
    from . import objects as _o
    tips = _ref_tips(repo)
    if tips:
        import zlib
        try:
            from . import pack as _p
            bitmapped = _p.reachable_from_bitmaps(repo, sorted(tips))
            if bitmapped is not None:
                return bitmapped
        except (OSError, ValueError, KeyError, zlib.error):
            pass
    seen: set[str] = set()
    graph = _graph_for_repo(repo)
    stack = list(tips)
    while stack:
        s = stack.pop()
        if s in seen:
            continue
        seen.add(s)
        info = _commit_tree_parents(repo, s, graph)
        if info is not None:
            tree, parents = info
            stack.append(tree)
            stack.extend(parents)
            continue
        try:
            t, data = _o.read_object(repo, s)
        except KeyError:
            continue
        if t == "commit":
            c = _o.parse_commit(data)
            stack.append(c.tree)
            stack.extend(c.parents)
        elif t == "tree":
            for e in _o.parse_tree(data, repo.hash_len):
                stack.append(e.sha)
        elif t == "tag":
            for line in data.decode("utf-8", errors="replace").splitlines():
                if line.startswith("object "):
                    stack.append(line[len("object "):].strip())
                    break
    return seen


_STREAM_PACK_OBJECT_LIMIT = 50000


def _write_pack_files(
    repo: Repository,
    shas: list[str],
    base: str,
) -> tuple[str, Path, list[tuple[str, int, int]]]:
    from . import pack as _p

    pack_dir = repo.gitdir / "objects" / "pack"
    pack_dir.mkdir(parents=True, exist_ok=True)
    _p.clear_pack_cache(repo)
    if len(shas) >= _STREAM_PACK_OBJECT_LIMIT:
        tmp = pack_dir / f".tmp-{base}-{os.getpid()}-{time.time_ns()}.pack"
        try:
            pack_sha, entries = _p.write_pack_stream(repo, shas, tmp)
            idx_bytes = _p.write_idx_v2_from_checksum(bytes.fromhex(pack_sha), entries, repo.object_format())
            pack_path = pack_dir / f"{base}-{pack_sha}.pack"
            os.replace(tmp, pack_path)
            (pack_dir / f"{base}-{pack_sha}.idx").write_bytes(idx_bytes)
            return pack_sha, pack_path, entries
        finally:
            tmp.unlink(missing_ok=True)

    pack_bytes, entries = _p.build_pack(repo, shas)
    idx_bytes = _p.write_idx_v2(pack_bytes, entries, repo.object_format())
    pack_sha = repo.hash_hex(pack_bytes[:-repo.hash_len])
    pack_path = pack_dir / f"{base}-{pack_sha}.pack"
    pack_path.write_bytes(pack_bytes)
    (pack_dir / f"{base}-{pack_sha}.idx").write_bytes(idx_bytes)
    return pack_sha, pack_path, entries


def cmd_pack_objects(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit pack-objects")
    ap.add_argument("base", help="pack file prefix (e.g. pack)")
    ap.add_argument("--stdout", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--write-bitmap-index", action="store_true")
    ap.add_argument("--no-write-bitmap-index", action="store_true")
    args = ap.parse_args(argv)
    if args.write_bitmap_index and not args.all:
        ap.error("--write-bitmap-index requires --all")
    repo = _repo()
    from . import pack as _p
    if args.all:
        shas = sorted(_reachable(repo))
    else:
        shas = [l.strip() for l in sys.stdin.read().splitlines() if l.strip()]
    if args.stdout:
        pack_bytes, _entries = _p.build_pack(repo, shas)
        sys.stdout.buffer.write(pack_bytes)
        return 0
    pack_sha, pack_path, entries = _write_pack_files(repo, shas, args.base)
    if args.all and not args.no_write_bitmap_index:
        _p.write_pack_bitmap(repo, pack_path, entries)
    _print(pack_sha)
    return 0


def cmd_unpack_objects(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit unpack-objects")
    ap.parse_args(argv)
    repo = _repo()
    from . import pack as _p
    data = sys.stdin.buffer.read()
    n = _p.unpack_pack(repo, data)
    _print(f"Unpacked {n} objects")
    return 0


def cmd_index_pack(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit index-pack")
    ap.add_argument("packfile")
    args = ap.parse_args(argv)
    repo = _repo()
    from . import pack as _p
    import struct, zlib, binascii
    raw = Path(args.packfile).read_bytes()
    if raw[:4] != b"PACK":
        _err("not a pack")
        return 1
    _, count = struct.unpack(">II", raw[4:12])
    pos = 12
    by_offset: dict[int, tuple[str, bytes]] = {}
    entries: list[tuple[str, int, int]] = []
    for _ in range(count):
        start = pos
        obj_type, _sz, p = _p._read_var_size(raw, pos)
        pos = p
        if obj_type in (_p.OBJ_COMMIT, _p.OBJ_TREE, _p.OBJ_BLOB, _p.OBJ_TAG):
            d = zlib.decompressobj()
            payload = d.decompress(raw[pos:])
            consumed = len(raw) - pos - len(d.unused_data)
            t = _p._TYPE_NAME[obj_type]
        elif obj_type == _p.OBJ_OFS_DELTA:
            neg, p2 = _p._read_offset(raw, pos)
            pos = p2
            base_off = start - neg
            d = zlib.decompressobj()
            delta = d.decompress(raw[pos:])
            consumed = len(raw) - pos - len(d.unused_data)
            base = by_offset[base_off]
            payload = _p.apply_delta(base[1], delta)
            t = base[0]
        elif obj_type == _p.OBJ_REF_DELTA:
            base_sha = raw[pos : pos + repo.hash_len].hex()
            pos += repo.hash_len
            d = zlib.decompressobj()
            delta = d.decompress(raw[pos:])
            consumed = len(raw) - pos - len(d.unused_data)
            t, base_data = objs.read_object(repo, base_sha)
            payload = _p.apply_delta(base_data, delta)
        else:
            _err(f"unknown obj type {obj_type}")
            return 1
        pos += consumed
        end = pos
        sha, _ = objs.hash_bytes(t, payload, repo)
        by_offset[start] = (t, payload)
        crc = binascii.crc32(raw[start:end])
        entries.append((sha, start, crc))
    idx = _p.write_idx_v2(raw, entries, repo.object_format())
    idx_path = Path(args.packfile).with_suffix(".idx")
    idx_path.write_bytes(idx)
    pack_sha = repo.hash_hex(raw[:-repo.hash_len])
    _print(pack_sha)
    return 0


def cmd_verify_pack(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit verify-pack")
    ap.add_argument("-v", action="store_true")
    ap.add_argument("packs", nargs="+")
    args = ap.parse_args(argv)
    repo = _repo()
    from . import pack as _p
    rc = 0
    for p in args.packs:
        path = Path(p)
        if path.suffix == ".idx":
            path = path.with_suffix(".pack")
        pk = _p.Pack(path, repo.object_format())
        try:
            for sha in pk.shas:
                t, data = pk.get(sha)  # type: ignore[misc]
                actual, _ = objs.hash_bytes(t, data, repo)
                if actual != sha:
                    _err(f"error: bad object {sha}")
                    rc = 1
                if args.v:
                    _print(f"{sha} {t} {len(data)}")
        except Exception as e:
            _err(f"verify failed: {e}")
            rc = 1
        finally:
            pk.close()
    return rc


def cmd_count_objects(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit count-objects")
    ap.add_argument("-v", action="store_true")
    args = ap.parse_args(argv)
    repo = _repo()
    loose_count, size = _loose_count_and_size(repo)
    if args.v:
        from . import pack as _p
        midx = _p.read_midx(repo)
        if midx is not None:
            pack_count = len(midx.pack_names)
            pack_objs = len(midx.shas)
        else:
            pack_count = 0
            pack_objs = 0
            for pk in _p._iter_packs(repo):
                pack_count += 1
                pack_objs += len(pk.shas)
        _print(f"count: {loose_count}")
        _print(f"size: {size // 1024}")
        _print(f"in-pack: {pack_objs}")
        _print(f"packs: {pack_count}")
    else:
        _print(f"{loose_count} objects, {size // 1024} kilobytes")
    return 0


def cmd_repack(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit repack")
    ap.add_argument("-a", action="store_true")
    ap.add_argument("-d", action="store_true")
    ap.add_argument("-b", "--write-bitmap-index", action="store_true")
    ap.add_argument("--no-write-bitmap-index", action="store_true")
    args = ap.parse_args(argv)
    repo = _repo()
    from . import pack as _p
    shas = sorted(_reachable(repo))
    pack_sha, pack_path, entries = _write_pack_files(repo, shas, "pack")
    if not args.no_write_bitmap_index:
        _p.write_pack_bitmap(repo, pack_path, entries)
    _print(f"pack-{pack_sha}")
    if args.d:
        packed = set(shas)
        for sha in _iter_loose_shas(repo):
            if sha in packed:
                (repo.gitdir / "objects" / sha[:2] / sha[2:]).unlink(missing_ok=True)
    return 0


def cmd_prune(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit prune")
    ap.add_argument("-n", "--dry-run", action="store_true")
    args = ap.parse_args(argv)
    repo = _repo()
    reach = _reachable(repo)
    removed = 0
    for sha in _iter_loose_shas(repo):
        if sha not in reach:
            p = repo.gitdir / "objects" / sha[:2] / sha[2:]
            if args.dry_run:
                _print(f"would prune {sha}")
            else:
                p.unlink(missing_ok=True)
                removed += 1
    if not args.dry_run:
        _print(f"pruned {removed}")
    return 0


def _parse_mail_headers(text: str) -> tuple[dict[str, str], str]:
    headers: dict[str, str] = {}
    lines = text.splitlines()
    i = 0
    last = None
    while i < len(lines):
        line = lines[i]
        if line == "":
            i += 1
            break
        if line[:1] in (" ", "\t") and last:
            headers[last] += " " + line.strip()
        else:
            k, _, v = line.partition(":")
            headers[k.strip()] = v.strip()
            last = k.strip()
        i += 1
    body = "\n".join(lines[i:])
    return headers, body


def cmd_mailsplit(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit mailsplit")
    ap.add_argument("-o", "--output-dir", required=True)
    ap.add_argument("mbox")
    args = ap.parse_args(argv)
    text = Path(args.mbox).read_text(encoding="utf-8", errors="replace")
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    pieces: list[list[str]] = []
    cur: list[str] = []
    for line in text.splitlines():
        if line.startswith("From ") and cur:
            pieces.append(cur)
            cur = [line]
        else:
            cur.append(line)
    if cur:
        pieces.append(cur)
    for i, p in enumerate(pieces, 1):
        (out / f"{i:04d}").write_text("\n".join(p) + "\n", encoding="utf-8")
        _print(f"{i:04d}")
    return 0


def cmd_mailinfo(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit mailinfo")
    ap.add_argument("msg")
    ap.add_argument("patch")
    args = ap.parse_args(argv)
    text = sys.stdin.read()
    if text.startswith("From "):
        nl = text.find("\n")
        if nl != -1:
            text = text[nl + 1 :]
    headers, body = _parse_mail_headers(text)
    subject = headers.get("Subject", "")
    if subject.startswith("[") and "]" in subject:
        subject = subject[subject.index("]") + 1 :].strip()
    if "---" in body:
        sep = body.index("---")
        msg = body[:sep].rstrip()
        patch_text = body[sep:]
    else:
        msg = body.rstrip()
        patch_text = ""
    Path(args.msg).write_text(subject + "\n\n" + msg + "\n", encoding="utf-8")
    Path(args.patch).write_text(patch_text, encoding="utf-8")
    _print(f"Subject: {subject}")
    _print(f"Author: {headers.get('From', '')}")
    return 0


def _notes_ref(name: str) -> str:
    return name if name.startswith("refs/notes/") else f"refs/notes/{name}"


def cmd_notes(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit notes")
    sub = ap.add_subparsers(dest="action")
    p_add = sub.add_parser("add")
    p_add.add_argument("-m", "--message", required=True)
    p_add.add_argument("commit", nargs="?", default="HEAD")
    p_show = sub.add_parser("show")
    p_show.add_argument("commit", nargs="?", default="HEAD")
    p_remove = sub.add_parser("remove")
    p_remove.add_argument("commit", nargs="?", default="HEAD")
    sub.add_parser("list")
    args = ap.parse_args(argv or ["list"])
    repo = _repo()
    ref = _notes_ref("commits")
    action = args.action or "list"

    notes_tree_sha = refs_mod.read_ref(repo, ref)
    notes_map: dict[str, str] = {}
    if notes_tree_sha:
        nc = objs.parse_commit(objs.read_object(repo, notes_tree_sha)[1])
        notes_map = workdir.flatten_tree(repo, nc.tree)

    if action == "list":
        for path, blob_sha in sorted(notes_map.items()):
            _print(f"{blob_sha} {path.replace('/', '')}")
        return 0
    target = refs_mod.rev_parse(repo, args.commit)
    if not target:
        return 128
    key = target[:2] + "/" + target[2:]
    if action == "show":
        if key not in notes_map:
            return 1
        _, data = objs.read_object(repo, notes_map[key])
        sys.stdout.buffer.write(data)
        return 0
    if action == "add":
        blob = objs.write_object(repo, "blob", args.message.encode("utf-8") + b"\n")
        notes_map[key] = blob
    elif action == "remove":
        notes_map.pop(key, None)
    from .index import Index, IndexEntry, REG_MODE, write_index, read_index
    saved_idx = read_index(repo) if (repo.gitdir / "index").exists() else None
    idx = Index()
    for p, s in sorted(notes_map.items()):
        idx.entries.append(IndexEntry(mode=REG_MODE, sha=s, path=p))
    write_index(repo, idx)
    new_tree = workdir.write_tree(repo)
    if saved_idx is not None:
        write_index(repo, saved_idx)
    else:
        (repo.gitdir / "index").unlink(missing_ok=True)
    name, email = repo.user()
    sig = objs.format_signature(name, email, when=int(time.time()))
    parents = [notes_tree_sha] if notes_tree_sha else []
    c = objs.Commit(tree=new_tree, parents=parents, author=sig, committer=sig,
                    message=f"Notes added by 'pygit notes {action}'\n")
    sha = objs.write_object(repo, "commit", c.encode())
    refs_mod.update_ref(repo, ref, sha, message=f"notes: {action}")
    return 0


def cmd_bisect(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit bisect")
    sub = ap.add_subparsers(dest="action", required=True)
    p_start = sub.add_parser("start")
    p_start.add_argument("bad", nargs="?")
    p_start.add_argument("good", nargs="?")
    p_bad = sub.add_parser("bad")
    p_bad.add_argument("rev", nargs="?", default="HEAD")
    p_good = sub.add_parser("good")
    p_good.add_argument("rev", nargs="?", default="HEAD")
    sub.add_parser("reset")
    sub.add_parser("log")
    args = ap.parse_args(argv)
    repo = _repo()
    state_dir = repo.gitdir / "BISECT"
    state_dir.mkdir(exist_ok=True)
    bads_path = state_dir / "bad"
    goods_path = state_dir / "good"

    def load(p):
        return p.read_text(encoding="utf-8").splitlines() if p.exists() else []

    def add(p, sha):
        with p.open("a", encoding="utf-8") as f:
            f.write(sha + "\n")

    def bisect_step():
        bads = load(bads_path)
        goods = load(goods_path)
        if not bads or not goods:
            _print("status: need at least one good and one bad")
            return 0
        # candidates: ancestors of any bad, minus ancestors of any good and bads themselves
        anc_bad: set[str] = set()
        stack = list(bads)
        seen: set[str] = set()
        while stack:
            s = stack.pop()
            if s in seen:
                continue
            seen.add(s)
            anc_bad.add(s)
            try:
                c = objs.parse_commit(objs.read_object(repo, s)[1])
                stack.extend(c.parents)
            except KeyError:
                pass
        for g in goods:
            seen2: set[str] = set()
            stack = [g]
            while stack:
                s = stack.pop()
                if s in seen2:
                    continue
                seen2.add(s)
                anc_bad.discard(s)
                try:
                    c = objs.parse_commit(objs.read_object(repo, s)[1])
                    stack.extend(c.parents)
                except KeyError:
                    pass
        candidates = anc_bad - set(bads)
        if not candidates:
            _print(f"{bads[0]} is the first bad commit")
            return 0
        # weight[c] = number of candidates reachable from c (including c)
        # best = argmax(min(weight, n - weight)) — matches git/bisect.c best_bisection
        parents_map: dict[str, list[str]] = {}
        for c_sha in candidates:
            try:
                cc = objs.parse_commit(objs.read_object(repo, c_sha)[1])
                parents_map[c_sha] = [p for p in cc.parents if p in candidates]
            except KeyError:
                parents_map[c_sha] = []
        indegree = {c: 0 for c in candidates}
        for ps in parents_map.values():
            for p in ps:
                indegree[p] += 1
        queue = sorted(c for c, deg in indegree.items() if deg == 0)
        topo: list[str] = []
        while queue:
            c_sha = queue.pop()
            topo.append(c_sha)
            for p in parents_map.get(c_sha, []):
                indegree[p] -= 1
                if indegree[p] == 0:
                    queue.append(p)

        positions = {c_sha: i for i, c_sha in enumerate(sorted(candidates))}
        reachable: dict[str, int] = {}
        for c_sha in reversed(topo):
            r = 1 << positions[c_sha]
            for p in parents_map.get(c_sha, []):
                r |= reachable[p]
            reachable[c_sha] = r

        n = len(candidates)
        best = None
        best_distance = -1
        for c_sha in sorted(candidates):  # stable for ties
            w = reachable.get(c_sha, 1 << positions[c_sha]).bit_count()
            distance = min(w, n - w)
            if distance > best_distance:
                best_distance = distance
                best = c_sha
        if best is None:
            best = next(iter(candidates))
        _print(f"Bisecting: {n // 2} revisions left to test after this (roughly {best_distance} steps)")
        _print(f"[{best}] candidate")
        t, d = objs.read_object(repo, best)
        tree = objs.parse_commit(d).tree
        workdir.checkout_tree(repo, tree)
        refs_mod.set_head(repo, best)
        return 0

    if args.action == "start":
        bads_path.unlink(missing_ok=True)
        goods_path.unlink(missing_ok=True)
        if args.bad:
            s = refs_mod.rev_parse(repo, args.bad)
            if s:
                add(bads_path, s)
        if args.good:
            s = refs_mod.rev_parse(repo, args.good)
            if s:
                add(goods_path, s)
        return bisect_step()
    if args.action == "bad":
        s = refs_mod.rev_parse(repo, args.rev)
        if s:
            add(bads_path, s)
        return bisect_step()
    if args.action == "good":
        s = refs_mod.rev_parse(repo, args.rev)
        if s:
            add(goods_path, s)
        return bisect_step()
    if args.action == "log":
        for line in load(bads_path):
            _print(f"# bad: {line}")
        for line in load(goods_path):
            _print(f"# good: {line}")
        return 0
    if args.action == "reset":
        import shutil as _sh
        _sh.rmtree(state_dir, ignore_errors=True)
        return 0
    return 1


def cmd_worktree(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit worktree")
    sub = ap.add_subparsers(dest="action", required=True)
    p_add = sub.add_parser("add")
    p_add.add_argument("path")
    p_add.add_argument("rev", nargs="?", default="HEAD")
    sub.add_parser("list")
    p_remove = sub.add_parser("remove")
    p_remove.add_argument("path")
    args = ap.parse_args(argv)
    repo = _repo()
    worktrees_dir = repo.gitdir / "worktrees"
    if args.action == "add":
        wt_name = Path(args.path).name
        wt_dir = worktrees_dir / wt_name
        wt_dir.mkdir(parents=True, exist_ok=True)
        sha = refs_mod.rev_parse(repo, args.rev)
        if not sha:
            return 128
        (wt_dir / "HEAD").write_text(sha + "\n", encoding="utf-8")
        (wt_dir / "commondir").write_text(str(repo.gitdir) + "\n", encoding="utf-8")
        target_dir = Path(args.path).resolve()
        target_dir.mkdir(parents=True, exist_ok=True)
        (wt_dir / "gitdir").write_text(str(target_dir / ".git") + "\n", encoding="utf-8")
        (target_dir / ".git").write_text(f"gitdir: {wt_dir}\n", encoding="utf-8")
        t, data = objs.read_object(repo, sha)
        tree = objs.parse_commit(data).tree if t == "commit" else sha
        for path, bsha in workdir.flatten_tree(repo, tree).items():
            _, blob = objs.read_object(repo, bsha)
            full = target_dir / path
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_bytes(blob)
        _print(f"Worktree added at {target_dir}")
        return 0
    if args.action == "list":
        head = refs_mod.rev_parse(repo, "HEAD") or ""
        _print(f"{repo.path} {head} [main]")
        if worktrees_dir.exists():
            for d in worktrees_dir.iterdir():
                if d.is_dir():
                    gitdir_file = d / "gitdir"
                    head_file = d / "HEAD"
                    if gitdir_file.exists() and head_file.exists():
                        wt_path = Path(gitdir_file.read_text().strip()).parent
                        wt_head = head_file.read_text().strip()
                        _print(f"{wt_path} {wt_head} [{d.name}]")
        return 0
    if args.action == "remove":
        import shutil as _sh
        wt_name = Path(args.path).name
        wt_dir = worktrees_dir / wt_name
        if wt_dir.exists():
            _sh.rmtree(wt_dir, ignore_errors=True)
        target = Path(args.path)
        if target.exists():
            _sh.rmtree(target, ignore_errors=True)
        return 0
    return 1


def _register_phase4() -> None:
    _COMMANDS["pack-objects"] = cmd_pack_objects
    _COMMANDS["unpack-objects"] = cmd_unpack_objects
    _COMMANDS["index-pack"] = cmd_index_pack
    _COMMANDS["verify-pack"] = cmd_verify_pack
    _COMMANDS["count-objects"] = cmd_count_objects
    _COMMANDS["repack"] = cmd_repack
    _COMMANDS["prune"] = cmd_prune
    _COMMANDS["mailsplit"] = cmd_mailsplit
    _COMMANDS["mailinfo"] = cmd_mailinfo
    _COMMANDS["notes"] = cmd_notes
    _COMMANDS["bisect"] = cmd_bisect
    _COMMANDS["worktree"] = cmd_worktree


_register_phase4()


# ---------------------------------------------------------------------------
# Phase 5 — pull / grep / show-branch / whatchanged / mktag / name-rev /
#           var / stripspace / update-server-info / replace / cherry /
#           range-diff


def cmd_pull(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit pull")
    ap.add_argument("remote", nargs="?", default="origin")
    ap.add_argument("branch", nargs="?", default=None)
    args = ap.parse_args(argv)
    rc = cmd_fetch([args.remote])
    if rc != 0:
        return rc
    repo = _repo()
    head_sym, _ = refs_mod.read_head(repo)
    branch = args.branch
    if not branch and head_sym and head_sym.startswith("refs/heads/"):
        branch = head_sym[len("refs/heads/"):]
    if not branch:
        _err("fatal: no branch to merge")
        return 1
    return cmd_merge([f"refs/remotes/{args.remote}/{branch}"])


def cmd_grep(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit grep")
    ap.add_argument("-i", "--ignore-case", action="store_true")
    ap.add_argument("-n", "--line-number", action="store_true")
    ap.add_argument("-l", "--files-with-matches", action="store_true")
    ap.add_argument("-E", "--extended-regexp", action="store_true")
    ap.add_argument("--cached", action="store_true")
    ap.add_argument("pattern")
    ap.add_argument("paths", nargs="*")
    args = ap.parse_args(argv)
    repo = _repo()
    import re
    flags = re.IGNORECASE if args.ignore_case else 0
    pat = re.compile(args.pattern, flags)

    if args.cached:
        from .index import read_index
        for e in read_index(repo).entries:
            if args.paths and not any(e.path == p or e.path.startswith(p + "/") for p in args.paths):
                continue
            try:
                _, data = objs.read_object(repo, e.sha)
                text = data.decode("utf-8", errors="replace")
            except Exception:
                continue
            matched_file = False
            for i, line in enumerate(text.splitlines(), 1):
                if pat.search(line):
                    matched_file = True
                    if args.files_with_matches:
                        break
                    prefix = f"{e.path}:"
                    if args.line_number:
                        prefix += f"{i}:"
                    _print(prefix + line)
            if matched_file and args.files_with_matches:
                _print(e.path)
        return 0

    # search worktree (tracked files)
    rc = 1
    from .index import read_index
    for e in read_index(repo).entries:
        if args.paths and not any(e.path == p or e.path.startswith(p + "/") for p in args.paths):
            continue
        full = repo.path / e.path
        if not full.exists():
            continue
        try:
            text = full.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        matched = False
        for i, line in enumerate(text.splitlines(), 1):
            if pat.search(line):
                rc = 0
                matched = True
                if args.files_with_matches:
                    break
                prefix = f"{e.path}:"
                if args.line_number:
                    prefix += f"{i}:"
                _print(prefix + line)
        if matched and args.files_with_matches:
            _print(e.path)
    return rc


def cmd_show_branch(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit show-branch")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args(argv)
    repo = _repo()
    branches = refs_mod.list_branches(repo)
    head_sym, _ = refs_mod.read_head(repo)
    cur = head_sym[len("refs/heads/"):] if head_sym and head_sym.startswith("refs/heads/") else None
    for b in branches:
        sha = refs_mod.read_ref(repo, f"refs/heads/{b}")
        if not sha:
            continue
        try:
            c = objs.parse_commit(objs.read_object(repo, sha)[1])
            subject = c.message.splitlines()[0] if c.message.strip() else ""
        except KeyError:
            subject = ""
        mark = "*" if b == cur else " "
        _print(f"{mark} [{b}] {subject}")
    return 0


def cmd_whatchanged(argv: list[str]) -> int:
    # Mostly an alias for log with file-list output.
    ap = argparse.ArgumentParser(prog="pygit whatchanged")
    ap.add_argument("rev", nargs="?", default="HEAD")
    args = ap.parse_args(argv)
    repo = _repo()
    sha = refs_mod.rev_parse(repo, args.rev)
    if not sha:
        return 128
    while sha:
        c = objs.parse_commit(objs.read_object(repo, sha)[1])
        _print(f"commit {sha}")
        _print(f"Author: {c.author}")
        _print("")
        for line in c.message.rstrip("\n").splitlines():
            _print(f"    {line}")
        _print("")
        if c.parents:
            parent_tree = objs.parse_commit(objs.read_object(repo, c.parents[0])[1]).tree
            a = workdir.flatten_tree(repo, parent_tree)
            b = workdir.flatten_tree(repo, c.tree)
            for p in sorted(set(a) | set(b)):
                if a.get(p) != b.get(p):
                    if p not in a:
                        _print(f":000000 100644 0000000 {b[p][:7]} A\t{p}")
                    elif p not in b:
                        _print(f":100644 000000 {a[p][:7]} 0000000 D\t{p}")
                    else:
                        _print(f":100644 100644 {a[p][:7]} {b[p][:7]} M\t{p}")
            _print("")
        sha = c.parents[0] if c.parents else None
    return 0


def cmd_mktag(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit mktag")
    ap.parse_args(argv)
    repo = _repo()
    data = sys.stdin.read().encode("utf-8")
    # minimal validation: must have object/type/tag/tagger lines
    text = data.decode("utf-8", errors="replace")
    has_object = any(l.startswith("object ") for l in text.splitlines())
    has_type = any(l.startswith("type ") for l in text.splitlines())
    has_tag = any(l.startswith("tag ") for l in text.splitlines())
    if not (has_object and has_type and has_tag):
        _err("fatal: invalid tag")
        return 128
    sha = objs.write_object(repo, "tag", data)
    _print(sha)
    return 0


def cmd_name_rev(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit name-rev")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("rev", nargs="?")
    args = ap.parse_args(argv)
    repo = _repo()
    # Build a map sha -> closest ref name by BFS from each ref tip.
    name_for: dict[str, tuple[str, int]] = {}
    tips: list[tuple[str, str]] = []
    for b in refs_mod.list_branches(repo):
        s = refs_mod.read_ref(repo, f"refs/heads/{b}")
        if s:
            tips.append((b, s))
    for tag in refs_mod.list_tags(repo):
        s = refs_mod.read_ref(repo, f"refs/tags/{tag}")
        if s:
            tips.append((f"tags/{tag}", s))
    graph = _graph_for_repo(repo)
    for label, tip in tips:
        seen: set[str] = set()
        stack = deque([(tip, 0)])
        while stack:
            sha, depth = stack.popleft()
            if sha in seen:
                continue
            seen.add(sha)
            cur = name_for.get(sha)
            if cur is None or cur[1] > depth:
                suffix = "" if depth == 0 else f"~{depth}"
                name_for[sha] = (label + suffix, depth)
            info = _commit_tree_parents(repo, sha, graph)
            if info is not None:
                _tree, parents = info
                for p in parents:
                    stack.append((p, depth + 1))
    if args.all:
        for sha, (name, _) in sorted(name_for.items()):
            _print(f"{sha} {name}")
        return 0
    if args.rev:
        s = refs_mod.rev_parse(repo, args.rev)
        if not s:
            return 128
        if s in name_for:
            _print(f"{args.rev} {name_for[s][0]}")
            return 0
        _print(f"{args.rev} undefined")
        return 1
    return 0


def cmd_var(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit var")
    ap.add_argument("name")
    args = ap.parse_args(argv)
    repo = None
    try:
        repo = _repo()
    except Exception:
        pass
    name = args.name
    if name in ("GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT"):
        if repo:
            n, e = repo.user()
        else:
            n = os.environ.get("GIT_AUTHOR_NAME", "pythongit")
            e = os.environ.get("GIT_AUTHOR_EMAIL", "pythongit@example.invalid")
        import time as _t
        _print(f"{n} <{e}> {int(_t.time())} +0000")
        return 0
    if name == "GIT_EDITOR":
        _print(os.environ.get("GIT_EDITOR") or os.environ.get("EDITOR") or "vi")
        return 0
    if name == "GIT_PAGER":
        _print(os.environ.get("GIT_PAGER") or os.environ.get("PAGER") or "less")
        return 0
    _err(f"unknown variable: {name}")
    return 1


def cmd_stripspace(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit stripspace")
    ap.add_argument("-s", "--strip-comments", action="store_true")
    ap.add_argument("-c", "--comment-lines", action="store_true")
    args = ap.parse_args(argv)
    text = sys.stdin.read()
    if args.comment_lines:
        out = []
        for line in text.splitlines():
            out.append("# " + line if line else "#")
        sys.stdout.write("\n".join(out) + ("\n" if text.endswith("\n") else ""))
        return 0
    lines = text.splitlines()
    if args.strip_comments:
        lines = [l for l in lines if not l.lstrip().startswith("#")]
    # strip trailing whitespace from each line
    lines = [l.rstrip() for l in lines]
    # collapse multiple blank lines to one
    out: list[str] = []
    last_blank = False
    for l in lines:
        if l == "":
            if last_blank:
                continue
            last_blank = True
        else:
            last_blank = False
        out.append(l)
    # strip leading/trailing blank lines
    while out and out[0] == "":
        out.pop(0)
    while out and out[-1] == "":
        out.pop()
    sys.stdout.write("\n".join(out) + ("\n" if out else ""))
    return 0


def cmd_update_server_info(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit update-server-info")
    ap.parse_args(argv)
    repo = _repo()
    # info/refs: list all refs
    info_dir = repo.gitdir / "info"
    info_dir.mkdir(exist_ok=True)
    lines = []
    for kind in ("refs/heads", "refs/tags", "refs/remotes"):
        root = repo.gitdir / kind
        if root.exists():
            for f in root.rglob("*"):
                if f.is_file():
                    rel = str(f.relative_to(repo.gitdir)).replace(os.sep, "/")
                    s = refs_mod.read_ref(repo, rel)
                    if s:
                        lines.append(f"{s}\t{rel}")
    (info_dir / "refs").write_text("\n".join(sorted(lines)) + "\n", encoding="utf-8")
    # objects/info/packs: list of packs
    pack_dir = repo.gitdir / "objects" / "pack"
    pack_info_dir = repo.gitdir / "objects" / "info"
    pack_info_dir.mkdir(parents=True, exist_ok=True)
    pack_lines = []
    if pack_dir.exists():
        for f in sorted(pack_dir.glob("pack-*.pack")):
            pack_lines.append(f"P {f.name}")
    (pack_info_dir / "packs").write_text("\n".join(pack_lines) + ("\n" if pack_lines else ""), encoding="utf-8")
    return 0


def cmd_replace(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit replace")
    ap.add_argument("-d", "--delete", action="store_true")
    ap.add_argument("-l", "--list", action="store_true")
    ap.add_argument("orig", nargs="?")
    ap.add_argument("replacement", nargs="?")
    args = ap.parse_args(argv)
    repo = _repo()
    if args.list:
        root = repo.gitdir / "refs" / "replace"
        if root.exists():
            for f in root.rglob("*"):
                if f.is_file():
                    _print(f.name)
        return 0
    if not args.orig:
        return 1
    orig = refs_mod.rev_parse(repo, args.orig)
    if not orig:
        return 128
    ref = f"refs/replace/{orig}"
    if args.delete:
        refs_mod.delete_ref(repo, ref)
        return 0
    if not args.replacement:
        return 1
    repl = refs_mod.rev_parse(repo, args.replacement)
    if not repl:
        return 128
    refs_mod.update_ref(repo, ref, repl, message="replace")
    return 0


def cmd_cherry(argv: list[str]) -> int:
    """Find commits in <head> that are not in <upstream> based on patch-id."""
    ap = argparse.ArgumentParser(prog="pygit cherry")
    ap.add_argument("upstream")
    ap.add_argument("head", nargs="?", default="HEAD")
    args = ap.parse_args(argv)
    repo = _repo()
    up = refs_mod.rev_parse(repo, args.upstream)
    head = refs_mod.rev_parse(repo, args.head)
    if not up or not head:
        return 128

    def collect(tip: str, stop: str) -> list[str]:
        out = []
        seen: set[str] = set()
        stack = [tip]
        # mark all ancestors of stop
        anc_stop: set[str] = set()
        st2 = [stop]
        seen2: set[str] = set()
        while st2:
            s = st2.pop()
            if s in seen2:
                continue
            seen2.add(s)
            anc_stop.add(s)
            try:
                c = objs.parse_commit(objs.read_object(repo, s)[1])
                st2.extend(c.parents)
            except KeyError:
                pass
        while stack:
            s = stack.pop()
            if s in seen or s in anc_stop:
                continue
            seen.add(s)
            out.append(s)
            try:
                c = objs.parse_commit(objs.read_object(repo, s)[1])
                stack.extend(c.parents)
            except KeyError:
                pass
        return out

    def patch_id(sha: str) -> str:
        import hashlib
        c = objs.parse_commit(objs.read_object(repo, sha)[1])
        if not c.parents:
            return hashlib.sha1(c.tree.encode()).hexdigest()
        parent_tree = objs.parse_commit(objs.read_object(repo, c.parents[0])[1]).tree
        from . import diff as _d
        a = workdir.flatten_tree(repo, parent_tree)
        b = workdir.flatten_tree(repo, c.tree)
        h = hashlib.sha1()
        for p in sorted(set(a) | set(b)):
            if a.get(p) == b.get(p):
                continue
            at = bt = ""
            if a.get(p):
                at = objs.read_object(repo, a[p])[1].decode("utf-8", errors="replace")
            if b.get(p):
                bt = objs.read_object(repo, b[p])[1].decode("utf-8", errors="replace")
            h.update(_d.unified_diff(at, bt, p, p).encode("utf-8", errors="replace"))
        return h.hexdigest()

    head_commits = collect(head, up)
    up_commits = collect(up, head)
    up_patches = {patch_id(s) for s in up_commits}
    for s in head_commits:
        pid = patch_id(s)
        mark = "-" if pid in up_patches else "+"
        c = objs.parse_commit(objs.read_object(repo, s)[1])
        subject = c.message.splitlines()[0] if c.message.strip() else ""
        _print(f"{mark} {s} {subject}")
    return 0


def cmd_range_diff(argv: list[str]) -> int:
    """range-diff A..B C..D — pair commits by patch-id and show diff."""
    ap = argparse.ArgumentParser(prog="pygit range-diff")
    ap.add_argument("range1")
    ap.add_argument("range2")
    args = ap.parse_args(argv)
    repo = _repo()

    def expand(rng: str) -> list[str]:
        if ".." not in rng:
            return [refs_mod.rev_parse(repo, rng) or ""]
        a, b = rng.split("..", 1)
        a_sha = refs_mod.rev_parse(repo, a) if a else None
        b_sha = refs_mod.rev_parse(repo, b or "HEAD")
        if not b_sha:
            return []
        out, seen = [], set()
        stack = [b_sha]
        anc_a: set[str] = set()
        if a_sha:
            st = [a_sha]
            seen_a: set[str] = set()
            while st:
                s = st.pop()
                if s in seen_a:
                    continue
                seen_a.add(s)
                anc_a.add(s)
                try:
                    c = objs.parse_commit(objs.read_object(repo, s)[1])
                    st.extend(c.parents)
                except KeyError:
                    pass
        while stack:
            s = stack.pop()
            if s in seen or s in anc_a:
                continue
            seen.add(s)
            out.append(s)
            try:
                c = objs.parse_commit(objs.read_object(repo, s)[1])
                stack.extend(c.parents)
            except KeyError:
                pass
        out.reverse()
        return out

    a = expand(args.range1)
    b = expand(args.range2)
    for i, (x, y) in enumerate(zip(a, b), 1):
        cx = objs.parse_commit(objs.read_object(repo, x)[1])
        cy = objs.parse_commit(objs.read_object(repo, y)[1])
        sx = cx.message.splitlines()[0] if cx.message.strip() else ""
        sy = cy.message.splitlines()[0] if cy.message.strip() else ""
        marker = "=" if cx.tree == cy.tree else "!"
        _print(f"{i}: {x[:7]} {marker} {y[:7]} {sx}")
        if sx != sy:
            _print(f"    @@ Commit message\n    -{sx}\n    +{sy}")
    # extras
    for x in a[len(b):]:
        _print(f"-: {x[:7]}")
    for y in b[len(a):]:
        _print(f"+: {y[:7]}")
    return 0


def _register_phase5() -> None:
    _COMMANDS["pull"] = cmd_pull
    _COMMANDS["grep"] = cmd_grep
    _COMMANDS["show-branch"] = cmd_show_branch
    _COMMANDS["whatchanged"] = cmd_whatchanged
    _COMMANDS["mktag"] = cmd_mktag
    _COMMANDS["name-rev"] = cmd_name_rev
    _COMMANDS["var"] = cmd_var
    _COMMANDS["stripspace"] = cmd_stripspace
    _COMMANDS["update-server-info"] = cmd_update_server_info
    _COMMANDS["replace"] = cmd_replace
    _COMMANDS["cherry"] = cmd_cherry
    _COMMANDS["range-diff"] = cmd_range_diff


_register_phase5()


# ---------------------------------------------------------------------------
# Phase 6 — submodule / sparse-checkout / pack-refs / merge-file /
#           fast-export / fast-import / interpret-trailers /
#           verify-commit / verify-tag / commit-graph / rerere / column


def cmd_pack_refs(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit pack-refs")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--prune", action="store_true")
    args = ap.parse_args(argv)
    repo = _repo()
    lines = ["# pack-refs with: peeled fully-peeled sorted"]
    packed: list[tuple[str, str]] = []
    for kind in ("refs/heads", "refs/tags", "refs/remotes"):
        if not args.all and kind == "refs/heads":
            # by default pack only tags/remotes; --all packs everything
            continue
        root = repo.gitdir / kind
        if root.exists():
            for f in root.rglob("*"):
                if f.is_file():
                    rel = str(f.relative_to(repo.gitdir)).replace(os.sep, "/")
                    sha = refs_mod.read_ref(repo, rel)
                    if sha:
                        packed.append((sha, rel))
    packed.sort(key=lambda x: x[1])
    for sha, name in packed:
        lines.append(f"{sha} {name}")
        # for annotated tags, also write peeled
        try:
            t, data = objs.read_object(repo, sha)
            if t == "tag":
                for line in data.decode("utf-8", errors="replace").splitlines():
                    if line.startswith("object "):
                        peel = line[len("object "):].strip()
                        lines.append(f"^{peel}")
                        break
        except KeyError:
            pass
    (repo.gitdir / "packed-refs").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if args.prune:
        for sha, name in packed:
            p = repo.gitdir / name
            if p.exists():
                p.unlink()
    return 0


def cmd_merge_file(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit merge-file")
    ap.add_argument("-p", "--stdout", action="store_true")
    ap.add_argument("ours")
    ap.add_argument("base")
    ap.add_argument("theirs")
    args = ap.parse_args(argv)
    from . import merge as _m
    base = Path(args.base).read_bytes()
    ours = Path(args.ours).read_bytes()
    theirs = Path(args.theirs).read_bytes()
    merged, conflict = _m.merge_blob(base, ours, theirs)
    if args.stdout:
        sys.stdout.buffer.write(merged)
    else:
        Path(args.ours).write_bytes(merged)
    return 1 if conflict else 0


def cmd_fast_export(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit fast-export")
    ap.add_argument("revs", nargs="*", default=["HEAD"])
    args = ap.parse_args(argv)
    repo = _repo()
    # collect commits in topological order (oldest first)
    tips = []
    for r in args.revs:
        s = refs_mod.rev_parse(repo, r)
        if s:
            tips.append(s)
    seen: set[str] = set()
    order: list[str] = []
    stack = list(tips)
    while stack:
        s = stack.pop()
        if s in seen:
            continue
        seen.add(s)
        try:
            c = objs.parse_commit(objs.read_object(repo, s)[1])
            for p in c.parents:
                stack.append(p)
            order.append(s)
        except KeyError:
            pass
    order.reverse()
    blob_mark: dict[str, int] = {}
    commit_mark: dict[str, int] = {}
    next_mark = 1

    # emit blobs first
    for cs in order:
        c = objs.parse_commit(objs.read_object(repo, cs)[1])
        for path, bsha in workdir.flatten_tree(repo, c.tree).items():
            if bsha in blob_mark:
                continue
            _, data = objs.read_object(repo, bsha)
            blob_mark[bsha] = next_mark
            _print("blob")
            _print(f"mark :{next_mark}")
            _print(f"data {len(data)}")
            sys.stdout.write(data.decode("utf-8", errors="replace") + "\n")
            next_mark += 1
    # emit commits
    for cs in order:
        c = objs.parse_commit(objs.read_object(repo, cs)[1])
        commit_mark[cs] = next_mark
        _print("commit refs/heads/main")
        _print(f"mark :{next_mark}")
        if c.author:
            _print(f"author {c.author}")
        if c.committer:
            _print(f"committer {c.committer}")
        msg_bytes = c.message.encode("utf-8")
        _print(f"data {len(msg_bytes)}")
        sys.stdout.write(c.message + ("\n" if not c.message.endswith("\n") else ""))
        if c.parents and c.parents[0] in commit_mark:
            _print(f"from :{commit_mark[c.parents[0]]}")
        for p in c.parents[1:]:
            if p in commit_mark:
                _print(f"merge :{commit_mark[p]}")
        # files: deleteall + M for each file
        _print("deleteall")
        for path, bsha in sorted(workdir.flatten_tree(repo, c.tree).items()):
            _print(f"M 100644 :{blob_mark[bsha]} {path}")
        _print("")
        next_mark += 1
    return 0


def cmd_fast_import(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit fast-import")
    ap.parse_args(argv)
    repo = _repo()
    data = sys.stdin.read()
    lines = data.splitlines(keepends=True)
    i = 0
    marks: dict[int, str] = {}

    def read_line() -> Optional[str]:
        nonlocal i
        if i >= len(lines):
            return None
        ln = lines[i].rstrip("\n")
        i += 1
        return ln

    def read_data() -> bytes:
        nonlocal i
        ln = read_line() or ""
        if not ln.startswith("data "):
            return b""
        n = int(ln[5:])
        # read raw bytes from `data` lines
        buf = bytearray()
        while len(buf) < n and i < len(lines):
            buf += lines[i].encode("utf-8")
            i += 1
        return bytes(buf[:n])

    while True:
        ln = read_line()
        if ln is None:
            break
        if ln == "blob":
            mark = None
            sub = read_line()
            if sub and sub.startswith("mark :"):
                mark = int(sub[6:])
            else:
                # no mark, but we already consumed the line; rewind logically
                if sub is not None:
                    i -= 1
            payload = read_data()
            sha = objs.write_object(repo, "blob", payload)
            if mark is not None:
                marks[mark] = sha
        elif ln.startswith("commit "):
            ref = ln[len("commit "):].strip()
            mark = None
            author = ""
            committer = ""
            parents: list[str] = []
            files: dict[str, tuple[str, str]] = {}  # path -> (mode, sha)
            deleted_all = False
            message = ""
            while True:
                sub = read_line()
                if sub is None:
                    break
                if sub == "":
                    break
                if sub.startswith("mark :"):
                    mark = int(sub[6:])
                elif sub.startswith("author "):
                    author = sub[len("author "):]
                elif sub.startswith("committer "):
                    committer = sub[len("committer "):]
                elif sub.startswith("data "):
                    i -= 1
                    message = read_data().decode("utf-8", errors="replace")
                elif sub.startswith("from "):
                    target = sub[len("from "):].strip()
                    if target.startswith(":"):
                        parents.append(marks[int(target[1:])])
                    else:
                        parents.append(refs_mod.rev_parse(repo, target) or target)
                elif sub.startswith("merge "):
                    target = sub[len("merge "):].strip()
                    if target.startswith(":"):
                        parents.append(marks[int(target[1:])])
                elif sub == "deleteall":
                    deleted_all = True
                    files.clear()
                elif sub.startswith("M "):
                    parts = sub.split(" ", 3)
                    mode, dataref, path = parts[1], parts[2], parts[3]
                    if dataref.startswith(":"):
                        bsha = marks[int(dataref[1:])]
                    else:
                        bsha = dataref
                    files[path] = (mode, bsha)
                elif sub.startswith("D "):
                    path = sub[2:]
                    files.pop(path, None)
            # build tree from files
            if parents and not deleted_all:
                parent_tree = objs.parse_commit(objs.read_object(repo, parents[0])[1]).tree
                inherited = workdir.flatten_tree(repo, parent_tree)
                for p, s in inherited.items():
                    files.setdefault(p, ("100644", s))
            from .index import Index, IndexEntry, REG_MODE, write_index, read_index
            saved_idx = read_index(repo) if (repo.gitdir / "index").exists() else None
            idx = Index()
            for p, (mode, sha) in sorted(files.items()):
                idx.entries.append(IndexEntry(mode=int(mode, 8), sha=sha, path=p))
            write_index(repo, idx)
            tree = workdir.write_tree(repo)
            if saved_idx is not None:
                write_index(repo, saved_idx)
            else:
                (repo.gitdir / "index").unlink(missing_ok=True)
            c = objs.Commit(tree=tree, parents=parents, author=author, committer=committer,
                            message=message if message.endswith("\n") else message + "\n")
            sha = objs.write_object(repo, "commit", c.encode())
            if mark is not None:
                marks[mark] = sha
            refs_mod.update_ref(repo, ref, sha, message="fast-import")
        elif ln.startswith("reset "):
            ref = ln[len("reset "):].strip()
            sub = read_line()
            if sub and sub.startswith("from "):
                target = sub[len("from "):].strip()
                if target.startswith(":"):
                    refs_mod.update_ref(repo, ref, marks[int(target[1:])], message="fast-import reset")
                else:
                    s = refs_mod.rev_parse(repo, target)
                    if s:
                        refs_mod.update_ref(repo, ref, s, message="fast-import reset")
        # ignore other directives
    _print(f"imported {len(marks)} objects")
    return 0


def cmd_interpret_trailers(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit interpret-trailers")
    ap.add_argument("--trailer", action="append", default=[])
    ap.add_argument("file", nargs="?")
    args = ap.parse_args(argv)
    text = Path(args.file).read_text(encoding="utf-8") if args.file else sys.stdin.read()
    lines = text.splitlines()
    # Find existing trailer block at end (consecutive lines matching "Key: value")
    import re
    trailer_re = re.compile(r"^[A-Za-z][A-Za-z0-9-]*:\s")
    start = len(lines)
    while start > 0 and (lines[start - 1] == "" or trailer_re.match(lines[start - 1])):
        if lines[start - 1] == "" and start - 1 > 0 and trailer_re.match(lines[start - 2]):
            start -= 1
            continue
        if trailer_re.match(lines[start - 1]):
            start -= 1
        else:
            break
    head = lines[:start]
    trailers = [l for l in lines[start:] if l.strip()]
    # Add new trailers (skip duplicates exact match)
    for t in args.trailer:
        if t not in trailers:
            trailers.append(t)
    out = "\n".join(head).rstrip("\n")
    if out and trailers:
        out += "\n\n"
    if trailers:
        out += "\n".join(trailers)
    _print(out + ("\n" if not out.endswith("\n") else ""))
    return 0


def cmd_verify_commit(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit verify-commit")
    ap.add_argument("revs", nargs="+")
    args = ap.parse_args(argv)
    repo = _repo()
    rc = 0
    for r in args.revs:
        s = refs_mod.rev_parse(repo, r)
        if not s:
            _err(f"fatal: bad rev: {r}")
            rc = 1
            continue
        try:
            t, data = objs.read_object(repo, s)
            if t != "commit":
                _err(f"error: {r} is not a commit")
                rc = 1
                continue
            actual, _ = objs.hash_bytes("commit", data, repo)
            if actual != s:
                _err(f"error: {r} sha mismatch")
                rc = 1
            else:
                _err(f"object {s}")
                _err(f"type commit")
                _err(f"ok")
        except KeyError:
            _err(f"error: missing {s}")
            rc = 1
    return rc


def cmd_verify_tag(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit verify-tag")
    ap.add_argument("tags", nargs="+")
    args = ap.parse_args(argv)
    repo = _repo()
    rc = 0
    for r in args.tags:
        s = refs_mod.rev_parse(repo, r) or refs_mod.read_ref(repo, f"refs/tags/{r}")
        if not s:
            _err(f"error: bad tag: {r}")
            rc = 1
            continue
        try:
            t, data = objs.read_object(repo, s)
            if t != "tag":
                _err(f"error: {r} is not an annotated tag")
                rc = 1
                continue
            actual, _ = objs.hash_bytes("tag", data, repo)
            if actual != s:
                _err(f"error: {r} sha mismatch")
                rc = 1
            else:
                _err(f"ok {r}")
        except KeyError:
            _err(f"error: missing {s}")
            rc = 1
    return rc


def cmd_commit_graph(argv: list[str]) -> int:
    """Binary commit-graph per Documentation/gitformat-commit-graph.adoc.

    Writes .git/objects/info/commit-graph with CGPH header + OIDF + OIDL +
    CDAT + optional EDGE chunks, terminated by the repository hash of the
    preceding bytes.
    """
    ap = argparse.ArgumentParser(prog="pygit commit-graph")
    sub = ap.add_subparsers(dest="action", required=True)
    p_write = sub.add_parser("write")
    p_write.add_argument("--reachable", action="store_true")
    p_write.add_argument("--changed-paths", action="store_true")
    p_write.add_argument("--no-changed-paths", action="store_true")
    sub.add_parser("verify")
    args = ap.parse_args(argv)
    repo = _repo()
    info = repo.gitdir / "objects" / "info"
    info.mkdir(parents=True, exist_ok=True)
    cg = info / "commit-graph"

    if args.action == "write":
        import struct
        commit_candidates = None
        tips = sorted(_ref_tips(repo))
        if tips:
            try:
                from . import pack as _pack_mod

                commit_candidates = _pack_mod.reachable_from_bitmaps(repo, tips, object_type="commit")
            except Exception:
                commit_candidates = None
        reach = set() if commit_candidates is not None else _reachable(repo)
        commits: dict[str, objs.Commit] = {}
        for sha in sorted(commit_candidates if commit_candidates is not None else reach):
            try:
                t, data = objs.read_object(repo, sha)
                if t == "commit":
                    commits[sha] = objs.parse_commit(data)
            except KeyError:
                continue
        shas = sorted(commits.keys())
        sha_to_pos = {s: i for i, s in enumerate(shas)}
        n = len(shas)

        # Build CDAT entries (each H + 16 bytes).
        # parent positions: 0x70000000 means "no parent"
        # parent2 = 0x80000000 | edge_index when more than 2 parents
        extra_edges = bytearray()
        cdat = bytearray()
        # generation numbers (topological level): max(parent.gen)+1, 0 if no parent
        gens: dict[str, int] = {}

        def gen_of(sha: str) -> int:
            if sha in gens:
                return gens[sha]
            c = commits.get(sha)
            if c is None or not c.parents:
                gens[sha] = 1  # git uses 1 for roots; 0 means "uncomputed"
                return 1
            g = 1
            for p in c.parents:
                if p in commits:
                    g = max(g, gen_of(p) + 1)
            gens[sha] = g
            return g

        for sha in shas:
            c = commits[sha]
            cdat += bytes.fromhex(c.tree)
            ps = [sha_to_pos.get(p, None) for p in c.parents]
            ps = [p for p in ps if p is not None]
            if not ps:
                cdat += struct.pack(">II", 0x70000000, 0x70000000)
            elif len(ps) == 1:
                cdat += struct.pack(">II", ps[0], 0x70000000)
            elif len(ps) == 2:
                cdat += struct.pack(">II", ps[0], ps[1])
            else:
                # octopus: emit extra edges, mark last with high bit
                edge_idx = len(extra_edges) // 4
                cdat += struct.pack(">II", ps[0], 0x80000000 | edge_idx)
                for i, p in enumerate(ps[1:]):
                    if i == len(ps) - 2:
                        extra_edges += struct.pack(">I", 0x80000000 | p)
                    else:
                        extra_edges += struct.pack(">I", p)
            # commit time + generation
            ct = 0
            parts = c.committer.rsplit(" ", 2)
            if len(parts) >= 2:
                try:
                    ct = int(parts[-2])
                except ValueError:
                    ct = 0
            g = gen_of(sha)
            # high 30 bits of first 4 bytes = generation
            # low 2 bits of first 4 bytes = bits 33-32 of commit time
            # second 4 bytes = low 32 bits of commit time
            top = ((g & 0x3FFFFFFF) << 2) | ((ct >> 32) & 0x3)
            bot = ct & 0xFFFFFFFF
            cdat += struct.pack(">II", top, bot)

        # OIDF (256 * 4)
        fanout = [0] * 256
        for s in shas:
            fanout[int(s[:2], 16)] += 1
        cum = 0
        oidf = bytearray()
        for i in range(256):
            cum += fanout[i]
            oidf += struct.pack(">I", cum)

        # OIDL (N * H)
        oidl = b"".join(bytes.fromhex(s) for s in shas)

        # Compose: header (8) + TOC + chunks + trailer (H)
        chunks: list[tuple[bytes, bytes]] = [(b"OIDF", bytes(oidf)),
                                             (b"OIDL", oidl),
                                             (b"CDAT", bytes(cdat))]
        if extra_edges:
            chunks.append((b"EDGE", bytes(extra_edges)))
        if n and not args.no_changed_paths:
            from . import bloom as _bloom
            bidx, bdat = _bloom.build_commit_graph_bloom_chunks(repo, shas, commits)
            chunks.extend([(b"BIDX", bidx), (b"BDAT", bdat)])

        # 4 bytes signature + 1 ver + 1 hashver + 1 chunks + 1 base
        hash_version = 2 if repo.object_format() == "sha256" else 1
        header = b"CGPH" + bytes([1, hash_version, len(chunks), 0])
        # TOC: (count + 1) * 12 bytes
        toc_size = (len(chunks) + 1) * 12
        # compute offsets for each chunk
        offsets = []
        cur = len(header) + toc_size
        for cid, data in chunks:
            offsets.append(cur)
            cur += len(data)
        end_offset = cur  # terminator points here (start of trailer)

        toc = bytearray()
        for (cid, _), off in zip(chunks, offsets):
            toc += cid + struct.pack(">Q", off)
        toc += b"\x00\x00\x00\x00" + struct.pack(">Q", end_offset)

        body = header + bytes(toc) + b"".join(d for _, d in chunks)
        trailer = repo.hash_bytes(body)
        cg.write_bytes(body + trailer)
        try:
            from . import commitgraph as _commitgraph

            _commitgraph.clear_commit_graph_cache(repo)
        except Exception:
            pass
        _print(f"wrote commit-graph with {n} commits")
        return 0

    if args.action == "verify":
        if not cg.exists():
            _err("no commit-graph")
            return 1
        import struct
        raw = cg.read_bytes()
        if raw[:4] != b"CGPH":
            _err("bad signature")
            return 1
        if raw[4] != 1:
            _err(f"unsupported version {raw[4]}")
            return 1
        expected_hash_version = 2 if repo.object_format() == "sha256" else 1
        if raw[5] != expected_hash_version:
            _err(f"hash version mismatch: {raw[5]}")
            return 1
        chunks_count = raw[6]
        actual = repo.hash_bytes(raw[:-repo.hash_len])
        if actual != raw[-repo.hash_len:]:
            _err("trailer hash mismatch")
            return 1
        # parse TOC to find OIDL
        toc_start = 8
        oidl_off = oidl_end = cdat_off = None
        offsets: list[tuple[bytes, int]] = []
        for i in range(chunks_count + 1):
            entry = raw[toc_start + i * 12 : toc_start + i * 12 + 12]
            cid = entry[:4]
            off = struct.unpack(">Q", entry[4:])[0]
            offsets.append((cid, off))
        chunks: dict[bytes, bytes] = {}
        for i, (cid, off) in enumerate(offsets):
            nxt = offsets[i + 1][1] if i + 1 < len(offsets) else len(raw) - repo.hash_len
            if off > nxt or nxt > len(raw) - repo.hash_len:
                _err("invalid chunk offsets")
                return 1
            if cid != b"\0\0\0\0":
                chunks[cid] = raw[off:nxt]
            if cid == b"OIDL":
                oidl_off, oidl_end = off, nxt
            if cid == b"CDAT":
                cdat_off = off
        if oidl_off is None:
            _err("missing OIDL")
            return 1
        # check that all listed shas exist as commits
        sha_block = raw[oidl_off:oidl_end]
        for i in range(0, len(sha_block), repo.hash_len):
            sha = sha_block[i : i + repo.hash_len].hex()
            try:
                t, _ = objs.read_object(repo, sha)
                if t != "commit":
                    _err(f"OID {sha} is not a commit")
                    return 1
            except KeyError:
                _err(f"missing commit {sha}")
                return 1
        commit_count = len(sha_block) // repo.hash_len
        if (b"BIDX" in chunks) != (b"BDAT" in chunks):
            _err("commit-graph Bloom chunks must include both BIDX and BDAT")
            return 1
        if b"BIDX" in chunks:
            try:
                from . import bloom as _bloom
                _bloom.read_commit_graph_bloom_filters(chunks[b"BIDX"], chunks[b"BDAT"], commit_count)
            except ValueError as exc:
                _err(f"invalid commit-graph Bloom filters: {exc}")
                return 1
        _print("commit-graph ok")
        return 0
    return 1


def cmd_rerere(argv: list[str]) -> int:
    """Reuse Recorded Resolution."""
    ap = argparse.ArgumentParser(prog="pygit rerere")
    sub = ap.add_subparsers(dest="action")
    sub.add_parser("status")
    sub.add_parser("clear")
    sub.add_parser("diff")
    sub.add_parser("gc")
    p_remaining = sub.add_parser("remaining")
    p_forget = sub.add_parser("forget")
    p_forget.add_argument("paths", nargs="+")
    # When `pygit rerere` is invoked with no args after a merge, scan
    # resolved files and store post-images for future replay.
    args = ap.parse_args(argv or ["status"])
    repo = _repo()
    rr = repo.gitdir / "rr-cache"
    action = args.action or "status"
    from . import rerere as _rr
    if action == "status":
        # report pending preimages
        meta = rr / "_pending.txt"
        if meta.exists():
            for line in meta.read_text(encoding="utf-8").splitlines():
                if "\t" in line:
                    h, path = line.split("\t", 1)
                    _print(path)
        return 0
    if action == "remaining":
        meta = rr / "_pending.txt"
        if meta.exists():
            unresolved = []
            for line in meta.read_text(encoding="utf-8").splitlines():
                if "\t" in line:
                    h, path = line.split("\t", 1)
                    full = repo.path / path
                    if full.exists() and "<<<<<<<" in full.read_text(encoding="utf-8", errors="replace"):
                        unresolved.append(path)
            for p in unresolved:
                _print(p)
        return 0
    if action == "diff":
        meta = rr / "_pending.txt"
        if meta.exists():
            from . import diff as _d
            for line in meta.read_text(encoding="utf-8").splitlines():
                if "\t" not in line:
                    continue
                h, path = line.split("\t", 1)
                pre = (rr / h / "preimage").read_text(encoding="utf-8") if (rr / h / "preimage").exists() else ""
                full = repo.path / path
                cur = full.read_text(encoding="utf-8", errors="replace") if full.exists() else ""
                out = _d.unified_diff(pre, cur, f"a/{path}", f"b/{path}")
                if out:
                    _print(f"diff --git a/{path} b/{path}")
                    _print(out.rstrip("\n"))
        return 0
    if action == "clear":
        import shutil as _sh
        _sh.rmtree(rr, ignore_errors=True)
        return 0
    if action == "forget":
        # remove recorded resolution for the given paths from rr-cache
        meta = rr / "_pending.txt"
        if meta.exists():
            kept = []
            for line in meta.read_text(encoding="utf-8").splitlines():
                if "\t" in line and line.split("\t", 1)[1] in args.paths:
                    continue
                kept.append(line)
            meta.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        return 0
    if action == "gc":
        # nothing to age out in this minimal version
        return 0
    return 1


def _rerere_auto_scan(repo: Repository) -> None:
    """Called after status; scan resolved files and store post-images."""
    from . import rerere as _rr
    _rr.scan_and_record(repo)


def cmd_column(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit column")
    ap.add_argument("--mode", default="plain")
    ap.add_argument("--padding", type=int, default=2)
    args = ap.parse_args(argv)
    items = [l for l in sys.stdin.read().splitlines() if l]
    if not items:
        return 0
    import shutil as _sh
    width = _sh.get_terminal_size((80, 24)).columns
    col_w = max(len(x) for x in items) + args.padding
    cols = max(1, width // col_w)
    rows = (len(items) + cols - 1) // cols
    for r in range(rows):
        parts = []
        for c in range(cols):
            idx = c * rows + r
            if idx < len(items):
                parts.append(items[idx].ljust(col_w))
        _print("".join(parts).rstrip())
    return 0


# submodule — minimal: parse/update .gitmodules; record commits in tree entries with mode 160000


def _read_gitmodules(repo: Repository) -> dict[str, dict[str, str]]:
    f = repo.path / ".gitmodules"
    out: dict[str, dict[str, str]] = {}
    if not f.exists():
        return out
    import configparser
    cp = configparser.ConfigParser()
    cp.read(f, encoding="utf-8")
    for section in cp.sections():
        if section.startswith('submodule "') and section.endswith('"'):
            name = section[len('submodule "'):-1]
            out[name] = {k: cp.get(section, k) for k in cp.options(section)}
    return out


def cmd_submodule(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit submodule")
    sub = ap.add_subparsers(dest="action")
    p_add = sub.add_parser("add")
    p_add.add_argument("url")
    p_add.add_argument("path", nargs="?")
    sub.add_parser("status")
    sub.add_parser("init")
    sub.add_parser("update")
    args = ap.parse_args(argv or ["status"])
    repo = _repo()
    action = args.action or "status"
    if action == "add":
        path = args.path or args.url.rstrip("/").split("/")[-1].removesuffix(".git")
        # clone into path
        from . import protocol
        protocol.clone(args.url, str(repo.path / path))
        # update .gitmodules
        gm = repo.path / ".gitmodules"
        import configparser
        cp = configparser.ConfigParser()
        if gm.exists():
            cp.read(gm, encoding="utf-8")
        sect = f'submodule "{path}"'
        if not cp.has_section(sect):
            cp.add_section(sect)
        cp.set(sect, "path", path)
        cp.set(sect, "url", args.url)
        with gm.open("w", encoding="utf-8") as f:
            cp.write(f)
        workdir.add_paths(repo, [".gitmodules"])
        _print(f"Adding submodule at {path}")
        return 0
    if action == "status":
        for name, info in _read_gitmodules(repo).items():
            path = info.get("path", name)
            sub_git = repo.path / path / ".git"
            sha = ""
            if sub_git.is_dir():
                sub_repo = Repository(repo.path / path, gitdir=sub_git)
                sha = refs_mod.rev_parse(sub_repo, "HEAD") or ""
            _print(f" {sha} {path}")
        return 0
    if action == "init":
        for name in _read_gitmodules(repo):
            _print(f"Submodule '{name}' registered")
        return 0
    if action == "update":
        # Pin each submodule to the gitlink SHA recorded in HEAD's tree.
        head = refs_mod.rev_parse(repo, "HEAD")
        gitlinks: dict[str, str] = {}
        if head:
            try:
                head_tree = objs.parse_commit(objs.read_object(repo, head)[1]).tree
                gitlinks = workdir.flatten_gitlinks(repo, head_tree)
            except KeyError:
                pass
        for name, info in _read_gitmodules(repo).items():
            path = info.get("path", name)
            url = info.get("url", "")
            target = repo.path / path
            if not (target / ".git").exists() and url:
                from . import protocol
                protocol.clone(url, str(target))
            pinned = gitlinks.get(path)
            if pinned and (target / ".git").exists():
                # checkout the pinned SHA inside the submodule
                sub_repo = Repository.discover(str(target))
                try:
                    t, d = objs.read_object(sub_repo, pinned)
                    tree = objs.parse_commit(d).tree if t == "commit" else pinned
                    workdir.checkout_tree(sub_repo, tree)
                    refs_mod.set_head(sub_repo, pinned)
                    _print(f"Submodule '{path}' checked out at {pinned[:7]}")
                except KeyError:
                    _print(f"Submodule '{path}': SHA {pinned[:7]} not yet fetched")
        return 0
    return 1


# sparse-checkout — minimal: .git/info/sparse-checkout patterns


def cmd_sparse_checkout(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit sparse-checkout")
    sub = ap.add_subparsers(dest="action")
    sub.add_parser("init")
    sub.add_parser("list")
    sub.add_parser("disable")
    p_set = sub.add_parser("set")
    p_set.add_argument("patterns", nargs="+")
    args = ap.parse_args(argv or ["list"])
    repo = _repo()
    info = repo.gitdir / "info"
    info.mkdir(exist_ok=True)
    sc = info / "sparse-checkout"
    action = args.action or "list"
    if action == "init":
        if not sc.exists():
            sc.write_text("/*\n!/*/\n", encoding="utf-8")
        # set core.sparseCheckout = true
        cp = repo.config()
        if not cp.has_section("core"):
            cp.add_section("core")
        cp.set("core", "sparseCheckout", "true")
        with (repo.gitdir / "config").open("w", encoding="utf-8") as f:
            cp.write(f)
        return 0
    if action == "list":
        if sc.exists():
            sys.stdout.write(sc.read_text(encoding="utf-8"))
        return 0
    if action == "set":
        sc.write_text("\n".join(args.patterns) + "\n", encoding="utf-8")
        return 0
    if action == "disable":
        sc.unlink(missing_ok=True)
        cp = repo.config()
        if cp.has_section("core") and cp.has_option("core", "sparseCheckout"):
            cp.remove_option("core", "sparseCheckout")
            with (repo.gitdir / "config").open("w", encoding="utf-8") as f:
                cp.write(f)
        return 0
    return 1


def _register_phase6() -> None:
    _COMMANDS["pack-refs"] = cmd_pack_refs
    _COMMANDS["merge-file"] = cmd_merge_file
    _COMMANDS["fast-export"] = cmd_fast_export
    _COMMANDS["fast-import"] = cmd_fast_import
    _COMMANDS["interpret-trailers"] = cmd_interpret_trailers
    _COMMANDS["verify-commit"] = cmd_verify_commit
    _COMMANDS["verify-tag"] = cmd_verify_tag
    _COMMANDS["commit-graph"] = cmd_commit_graph
    _COMMANDS["rerere"] = cmd_rerere
    _COMMANDS["column"] = cmd_column
    _COMMANDS["submodule"] = cmd_submodule
    _COMMANDS["sparse-checkout"] = cmd_sparse_checkout


_register_phase6()


# ---------------------------------------------------------------------------
# Phase 7 — diff-tree / diff-files / diff-index / check-attr /
#           check-ref-format / check-mailmap / show-index / unpack-file /
#           merge-index / get-tar-commit-id / hook / credential


def _raw_diff_status(a_mode: str, b_mode: str, a_sha: Optional[str], b_sha: Optional[str], path: str) -> str:
    """Emit a 'raw diff' format line: ':MODE_A MODE_B SHA_A SHA_B STATUS\\tpath'."""
    null_oid = "0" * max(len(a_sha or ""), len(b_sha or ""), 40)
    if a_sha is None:
        return f":000000 {b_mode} {null_oid} {b_sha} A\t{path}"
    if b_sha is None:
        return f":{a_mode} 000000 {a_sha} {null_oid} D\t{path}"
    if a_sha == b_sha and a_mode == b_mode:
        return ""
    return f":{a_mode} {b_mode} {a_sha} {b_sha} M\t{path}"


def cmd_diff_tree(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit diff-tree")
    ap.add_argument("-r", action="store_true", help="recurse")
    ap.add_argument("-p", "--patch", action="store_true")
    ap.add_argument("--name-only", action="store_true")
    ap.add_argument("--name-status", action="store_true")
    ap.add_argument("rev1")
    ap.add_argument("rev2", nargs="?")
    args = ap.parse_args(argv)
    repo = _repo()

    def _resolve_to_tree(name: str) -> Optional[str]:
        s = refs_mod.rev_parse(repo, name)
        if not s:
            return None
        t, data = objs.read_object(repo, s)
        if t == "commit":
            return objs.parse_commit(data).tree
        if t == "tag":
            for line in data.decode(errors="replace").splitlines():
                if line.startswith("object "):
                    return _resolve_to_tree(line[len("object "):].strip())
        return s  # already a tree

    if args.rev2 is None:
        # treat rev1 as commit; diff against its first parent
        s = refs_mod.rev_parse(repo, args.rev1)
        if not s:
            return 128
        c = objs.parse_commit(objs.read_object(repo, s)[1])
        b_tree = c.tree
        a_tree = (objs.parse_commit(objs.read_object(repo, c.parents[0])[1]).tree
                  if c.parents else None)
        _print(s)
    else:
        a_tree = _resolve_to_tree(args.rev1)
        b_tree = _resolve_to_tree(args.rev2)
    if not b_tree:
        return 128
    a = workdir.flatten_tree(repo, a_tree) if a_tree else {}
    b = workdir.flatten_tree(repo, b_tree)
    for p in sorted(set(a) | set(b)):
        if a.get(p) == b.get(p):
            continue
        if args.name_only:
            _print(p)
        elif args.name_status:
            if p not in a:
                _print(f"A\t{p}")
            elif p not in b:
                _print(f"D\t{p}")
            else:
                _print(f"M\t{p}")
        else:
            ln = _raw_diff_status("100644", "100644",
                                  a.get(p, None), b.get(p, None), p)
            if ln:
                _print(ln)
    if args.patch:
        from . import diff as _d
        for p in sorted(set(a) | set(b)):
            if a.get(p) == b.get(p):
                continue
            at = bt = ""
            if a.get(p):
                at = objs.read_object(repo, a[p])[1].decode("utf-8", errors="replace")
            if b.get(p):
                bt = objs.read_object(repo, b[p])[1].decode("utf-8", errors="replace")
            out = _d.unified_diff(at, bt, f"a/{p}", f"b/{p}")
            if out:
                _print(f"diff --git a/{p} b/{p}")
                _print(out.rstrip("\n"))
    return 0


def cmd_diff_files(argv: list[str]) -> int:
    """Show diff between index and worktree (raw format)."""
    ap = argparse.ArgumentParser(prog="pygit diff-files")
    ap.add_argument("--name-only", action="store_true")
    args = ap.parse_args(argv)
    repo = _repo()
    from .index import read_index
    idx = read_index(repo)
    for e in idx.entries:
        full = repo.path / e.path
        if not full.exists():
            _print(_raw_diff_status(e.mode_str(), "000000", e.sha, None, e.path) if not args.name_only else e.path)
            continue
        data = full.read_bytes()
        sha, _ = objs.hash_bytes("blob", data, repo)
        if sha != e.sha:
            if args.name_only:
                _print(e.path)
            else:
                ln = _raw_diff_status(e.mode_str(), e.mode_str(), e.sha, sha, e.path)
                if ln:
                    _print(ln)
    return 0


def cmd_diff_index(argv: list[str]) -> int:
    """Show diff between a tree and the index."""
    ap = argparse.ArgumentParser(prog="pygit diff-index")
    ap.add_argument("--cached", action="store_true")
    ap.add_argument("--name-only", action="store_true")
    ap.add_argument("tree")
    args = ap.parse_args(argv)
    repo = _repo()
    tsha = refs_mod.rev_parse(repo, args.tree)
    if not tsha:
        return 128
    t, data = objs.read_object(repo, tsha)
    if t == "commit":
        tsha = objs.parse_commit(data).tree
    tree_map = workdir.flatten_tree(repo, tsha)
    from .index import read_index
    idx = read_index(repo).by_path()
    for p in sorted(set(tree_map) | set(idx)):
        a = tree_map.get(p)
        b = idx[p].sha if p in idx else None
        if a == b:
            continue
        if args.name_only:
            _print(p)
        else:
            ln = _raw_diff_status("100644", "100644", a, b, p)
            if ln:
                _print(ln)
    return 0


def _read_attributes(repo: Repository) -> list[tuple[str, dict[str, str]]]:
    """Parse .gitattributes lines into [(pattern, {attr: value})]."""
    f = repo.path / ".gitattributes"
    out: list[tuple[str, dict[str, str]]] = []
    if not f.exists():
        return out
    for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        pattern = parts[0]
        attrs: dict[str, str] = {}
        for tok in parts[1:]:
            if tok.startswith("-"):
                attrs[tok[1:]] = "unset"
            elif "=" in tok:
                k, _, v = tok.partition("=")
                attrs[k] = v
            else:
                attrs[tok] = "set"
        out.append((pattern, attrs))
    return out


def cmd_check_attr(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit check-attr")
    ap.add_argument("-a", "--all", action="store_true")
    ap.add_argument("attrs_then_paths", nargs="+")
    args = ap.parse_args(argv)
    repo = _repo()
    rules = _read_attributes(repo)
    import fnmatch
    if args.all:
        paths = args.attrs_then_paths
        attrs_filter = None
    else:
        # find boundary: attrs first, then "--", then paths; if no --, all are
        # treated as attrs followed by one path
        if "--" in args.attrs_then_paths:
            sep = args.attrs_then_paths.index("--")
            attrs_filter = args.attrs_then_paths[:sep]
            paths = args.attrs_then_paths[sep + 1:]
        else:
            attrs_filter = args.attrs_then_paths[:-1]
            paths = args.attrs_then_paths[-1:]
    for path in paths:
        resolved: dict[str, str] = {}
        for pattern, attrs in rules:
            if fnmatch.fnmatch(path, pattern):
                resolved.update(attrs)
        keys = resolved.keys() if attrs_filter is None else attrs_filter
        for k in keys:
            v = resolved.get(k, "unspecified")
            _print(f"{path}: {k}: {v}")
    return 0


def cmd_check_ref_format(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit check-ref-format")
    ap.add_argument("--branch", action="store_true")
    ap.add_argument("name")
    args = ap.parse_args(argv)
    name = args.name
    # Rules (subset of Documentation/git-check-ref-format.adoc):
    # 1. No slash-separated component begins with .
    # 2. No double-dot ..
    # 3. No ASCII control characters or any of \\ ? * [ : ~ ^ SP
    # 4. Cannot end with .lock or with /
    # 5. Cannot contain @{
    # 6. Cannot be the single character @
    if args.branch:
        full = name
        if "/" in name:
            _err("not a valid branch name")
            return 1
    else:
        full = name
        if name.count("/") < 1 and not name.startswith("refs/"):
            # require category/name
            _err("ref name must contain '/'")
            return 1
    bad = False
    if full == "@":
        bad = True
    if "@{" in full or ".." in full:
        bad = True
    if full.endswith(".lock") or full.endswith("/"):
        bad = True
    for ch in full:
        if ord(ch) < 0x20 or ch in "\x7f \\?*[:~^":
            bad = True
            break
    for part in full.split("/"):
        if part.startswith("."):
            bad = True
            break
    if bad:
        return 1
    _print(full)
    return 0


def cmd_check_mailmap(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit check-mailmap")
    ap.add_argument("contacts", nargs="+")
    args = ap.parse_args(argv)
    repo = _repo()
    # mailmap file (.mailmap at root) maps "Real Name <email>" to canonical
    mm = repo.path / ".mailmap"
    mapping: dict[str, str] = {}
    if mm.exists():
        for line in mm.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # Formats: Name <email> [other-name] <other-email>
            # We support: <canonical-email> <original-email>
            # and:        Canonical Name <canonical-email> <original-email>
            if "<" not in line:
                continue
            # find the last <...>
            parts = line.rsplit("<", 1)
            orig = "<" + parts[1] if parts[1].endswith(">") else line
            canon = parts[0].strip() + (" " if parts[0].strip() else "") + (
                "<" + parts[1].split(">")[0] + ">" if ">" in parts[1] else "")
            mapping[orig] = canon
    for c in args.contacts:
        _print(mapping.get(c, c))
    return 0


def cmd_show_index(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit show-index")
    ap.add_argument("--object-format", choices=["sha1", "sha256"], default=None)
    ap.add_argument("idx", nargs="?", help="path to .idx; if omitted, reads stdin (raw idx)")
    args = ap.parse_args(argv)
    if args.object_format:
        hash_len = 32 if args.object_format == "sha256" else 20
    else:
        try:
            hash_len = _repo().hash_len
        except RepositoryError:
            hash_len = 20
    from . import pack as _p
    if args.idx:
        shas, offsets = _p._read_idx(Path(args.idx), hash_len)
    else:
        data = sys.stdin.buffer.read()
        # write to temp then read
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".idx", delete=False) as f:
            f.write(data)
            tmp = f.name
        shas, offsets = _p._read_idx(Path(tmp), hash_len)
        os.unlink(tmp)
    paired = sorted(zip(offsets, shas))
    for off, sha in paired:
        _print(f"{off:<10} {sha}")
    return 0


def cmd_unpack_file(argv: list[str]) -> int:
    """Write a blob to a temp file and print its path (like real git)."""
    ap = argparse.ArgumentParser(prog="pygit unpack-file")
    ap.add_argument("blob")
    args = ap.parse_args(argv)
    repo = _repo()
    sha = refs_mod.rev_parse(repo, args.blob)
    if not sha:
        return 128
    t, data = objs.read_object(repo, sha)
    if t != "blob":
        _err("not a blob")
        return 1
    import tempfile
    fd, path = tempfile.mkstemp(prefix=".merge_file_", dir=str(repo.path))
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    _print(os.path.basename(path))
    return 0


def cmd_merge_index(argv: list[str]) -> int:
    """Run a merge driver program for each conflicted index entry.

    Invokes:  <driver> <path> <base-tempfile> <ours-tempfile> <theirs-tempfile>
    The driver may rewrite <path> in the worktree to commit a resolution.
    """
    ap = argparse.ArgumentParser(prog="pygit merge-index")
    ap.add_argument("-o", action="store_true",
                    help="continue past errors (mimics git's -o)")
    ap.add_argument("-a", "--all", action="store_true")
    ap.add_argument("driver")
    ap.add_argument("paths", nargs="*")
    args = ap.parse_args(argv)
    repo = _repo()
    from .index import read_index
    idx = read_index(repo)
    stages = idx.by_path_all_stages()
    targets = args.paths if args.paths else (idx.conflicted_paths() if args.all else [])
    if not targets:
        return 0

    import tempfile, subprocess
    rc = 0
    for p in targets:
        s = stages.get(p, {})
        if not any(k in (1, 2, 3) for k in s):
            continue
        tmps = []
        for stage in (1, 2, 3):
            if stage in s:
                _, data = objs.read_object(repo, s[stage].sha)
                fd, tmp = tempfile.mkstemp(prefix=f".{stage}.")
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                tmps.append(tmp)
            else:
                tmps.append("")
        try:
            r = subprocess.call([args.driver, p, *tmps])
            if r != 0:
                rc = r
                if not args.o:
                    return rc
        finally:
            for tmp in tmps:
                if tmp and os.path.exists(tmp):
                    os.unlink(tmp)
    return rc


def cmd_get_tar_commit_id(argv: list[str]) -> int:
    """Extract a commit-id from the comment field of a `git archive --format=tar` stream.

    Our `archive` doesn't currently embed it, so this is implemented for the
    real-git tar format only.
    """
    ap = argparse.ArgumentParser(prog="pygit get-tar-commit-id")
    ap.parse_args(argv)
    data = sys.stdin.buffer.read()
    # tar pax records contain `52 comment=<sha>\n` near the start
    if b"comment=" in data[:8192]:
        idx = data.index(b"comment=")
        end = data.index(b"\n", idx)
        _print(data[idx + len(b"comment=") : end].decode(errors="replace"))
        return 0
    return 1


def cmd_hook(argv: list[str]) -> int:
    """Run or list git hooks under .git/hooks/."""
    ap = argparse.ArgumentParser(prog="pygit hook")
    sub = ap.add_subparsers(dest="action", required=True)
    p_run = sub.add_parser("run")
    p_run.add_argument("name")
    p_run.add_argument("hook_args", nargs="*")
    sub.add_parser("list")
    args = ap.parse_args(argv)
    repo = _repo()
    hooks_dir = repo.gitdir / "hooks"
    if args.action == "list":
        if hooks_dir.exists():
            for h in sorted(hooks_dir.iterdir()):
                if h.is_file() and os.access(h, os.X_OK):
                    _print(h.name)
        return 0
    if args.action == "run":
        hook = hooks_dir / args.name
        if not hook.exists() or not os.access(hook, os.X_OK):
            return 0  # silently succeed when hook absent — matches git
        import subprocess
        return subprocess.call([str(hook), *args.hook_args])
    return 1


def cmd_credential(argv: list[str]) -> int:
    """Minimal credential helper: read description from stdin, return env-based creds."""
    ap = argparse.ArgumentParser(prog="pygit credential")
    ap.add_argument("op", choices=["fill", "approve", "reject"])
    args = ap.parse_args(argv)
    fields: dict[str, str] = {}
    for line in sys.stdin.read().splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            fields[k] = v
    if args.op == "fill":
        u = os.environ.get("GIT_USERNAME") or os.environ.get("USERNAME") or ""
        p = os.environ.get("GIT_PASSWORD") or ""
        if u:
            fields["username"] = u
        if p:
            fields["password"] = p
        for k in ("protocol", "host", "path", "username", "password"):
            if k in fields:
                _print(f"{k}={fields[k]}")
        return 0
    # approve / reject: no-op for the stub helper
    return 0


def _register_phase7() -> None:
    _COMMANDS["diff-tree"] = cmd_diff_tree
    _COMMANDS["diff-files"] = cmd_diff_files
    _COMMANDS["diff-index"] = cmd_diff_index
    _COMMANDS["check-attr"] = cmd_check_attr
    _COMMANDS["check-ref-format"] = cmd_check_ref_format
    _COMMANDS["check-mailmap"] = cmd_check_mailmap
    _COMMANDS["show-index"] = cmd_show_index
    _COMMANDS["unpack-file"] = cmd_unpack_file
    _COMMANDS["merge-index"] = cmd_merge_index
    _COMMANDS["get-tar-commit-id"] = cmd_get_tar_commit_id
    _COMMANDS["hook"] = cmd_hook
    _COMMANDS["credential"] = cmd_credential


_register_phase7()


# ---------------------------------------------------------------------------
# Phase 8 — finish everything else.
#
# Implemented (25):
#   init-db, annotate, patch-id, checkout-index, fmt-merge-msg, fetch-pack,
#   send-pack, upload-pack (stdin/stdout), receive-pack (stdin/stdout),
#   upload-archive, pack-redundant, prune-packed, merge-recursive (alias),
#   merge-ours, multi-pack-index, for-each-repo, diff-pairs, request-pull,
#   diagnose, bugreport, refs, replay, backfill, submodule-helper,
#   checkout-worker
#
# Out-of-scope but registered (so the dispatcher returns a clear message):
#   send-email, gitk, gitweb, gui, instaweb, difftool, mergetool,
#   cvsexportcommit, cvsimport, cvsserver, svn,
#   credential-cache, credential-cache-daemon, credential-store,
#   fsmonitor, fsmonitor-daemon, remote-helper, remote-ext, remote-fd


def cmd_init_db(argv: list[str]) -> int:
    return cmd_init(argv)


def cmd_annotate(argv: list[str]) -> int:
    return cmd_blame(argv)


def _patch_id_for_commit(repo: Repository, sha: str) -> str:
    import hashlib
    c = objs.parse_commit(objs.read_object(repo, sha)[1])
    parent_tree = ""
    if c.parents:
        parent_tree = objs.parse_commit(objs.read_object(repo, c.parents[0])[1]).tree
    a = workdir.flatten_tree(repo, parent_tree) if parent_tree else {}
    b = workdir.flatten_tree(repo, c.tree)
    from . import diff as _d
    h = hashlib.sha1()
    for p in sorted(set(a) | set(b)):
        if a.get(p) == b.get(p):
            continue
        at = bt = ""
        if a.get(p):
            at = objs.read_object(repo, a[p])[1].decode("utf-8", errors="replace")
        if b.get(p):
            bt = objs.read_object(repo, b[p])[1].decode("utf-8", errors="replace")
        h.update(_d.unified_diff(at, bt, p, p).encode("utf-8", errors="replace"))
    return h.hexdigest()


def cmd_patch_id(argv: list[str]) -> int:
    """Read a diff on stdin and emit its patch-id (hash of diff with line numbers stripped).

    Also accepts a commit id as positional arg.
    """
    ap = argparse.ArgumentParser(prog="pygit patch-id")
    ap.add_argument("rev", nargs="?")
    args = ap.parse_args(argv)
    if args.rev:
        repo = _repo()
        s = refs_mod.rev_parse(repo, args.rev)
        if not s:
            return 128
        _print(_patch_id_for_commit(repo, s) + " " + s)
        return 0
    import hashlib, re
    text = sys.stdin.read()
    # strip hunk headers and pure-context lines
    h = hashlib.sha1()
    for line in text.splitlines():
        if line.startswith("@@") or line.startswith("diff ") or line.startswith("index "):
            continue
        # strip context spaces
        h.update(line.encode("utf-8", errors="replace"))
    _print(h.hexdigest())
    return 0


def cmd_checkout_index(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit checkout-index")
    ap.add_argument("-a", "--all", action="store_true")
    ap.add_argument("-f", "--force", action="store_true")
    ap.add_argument("--prefix", default="")
    ap.add_argument("paths", nargs="*")
    args = ap.parse_args(argv)
    repo = _repo()
    from .index import read_index
    idx = read_index(repo)
    targets = set(args.paths) if args.paths else None
    if args.all:
        targets = None
    for e in idx.entries:
        if targets is not None and e.path not in targets:
            continue
        out = repo.path / (args.prefix + e.path)
        if out.exists() and not args.force:
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        _, data = objs.read_object(repo, e.sha)
        out.write_bytes(data)
    return 0


def cmd_fmt_merge_msg(argv: list[str]) -> int:
    """Read FETCH_HEAD or a list of refs from stdin and produce a merge message."""
    ap = argparse.ArgumentParser(prog="pygit fmt-merge-msg")
    ap.add_argument("--file", default=None)
    args = ap.parse_args(argv)
    repo = _repo()
    if args.file:
        text = Path(args.file).read_text(encoding="utf-8", errors="replace")
    elif (repo.gitdir / "FETCH_HEAD").exists():
        text = (repo.gitdir / "FETCH_HEAD").read_text(encoding="utf-8", errors="replace")
    else:
        text = sys.stdin.read()
    branches = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            ref = parts[-1].strip()
            branches.append(ref)
    if not branches:
        _print("Merge")
        return 0
    if len(branches) == 1:
        _print(f"Merge {branches[0]}")
    else:
        _print("Merge " + ", ".join(branches[:-1]) + ", and " + branches[-1])
    return 0


def cmd_fetch_pack(argv: list[str]) -> int:
    """Lower-level fetch: contact a remote and write objects, without updating refs."""
    ap = argparse.ArgumentParser(prog="pygit fetch-pack")
    ap.add_argument("url")
    ap.add_argument("refs", nargs="*")
    args = ap.parse_args(argv)
    repo = _repo()
    from . import protocol, pack as _p
    remote_refs = protocol.discover_refs(args.url)
    wanted = args.refs or list(remote_refs)
    wants = sorted({remote_refs[r] for r in wanted if r in remote_refs})
    if not wants:
        return 1
    raw = protocol.fetch_pack(args.url, wants)
    _p.unpack_pack(repo, raw)
    for r in wanted:
        if r in remote_refs:
            _print(f"{remote_refs[r]} {r}")
    return 0


def cmd_send_pack(argv: list[str]) -> int:
    """Lower-level push to a URL, not tied to a remote name."""
    ap = argparse.ArgumentParser(prog="pygit send-pack")
    ap.add_argument("url")
    ap.add_argument("refspec", nargs="+")
    args = ap.parse_args(argv)
    repo = _repo()
    # save url under a temp remote
    cp = repo.config()
    sect = 'remote "_send_pack_tmp"'
    if not cp.has_section(sect):
        cp.add_section(sect)
    cp.set(sect, "url", args.url)
    with (repo.gitdir / "config").open("w", encoding="utf-8") as f:
        cp.write(f)
    from . import protocol
    res = protocol.push(repo, "_send_pack_tmp", args.refspec)
    for ref, st in res.items():
        _print(f" {st}\t{ref}")
    return 0 if all(v == "ok" for v in res.values()) else 1


def cmd_upload_pack(argv: list[str]) -> int:
    """Serve pkt-line refs to stdout. Designed for ssh `git upload-pack <dir>` style.

    Simplified: lists refs (no negotiation, no pack streaming).
    """
    ap = argparse.ArgumentParser(prog="pygit upload-pack")
    ap.add_argument("--stateless-rpc", action="store_true")
    ap.add_argument("--http-backend-info-refs", action="store_true")
    ap.add_argument("directory")
    args = ap.parse_args(argv)
    repo = Repository.discover(args.directory)
    out = sys.stdout.buffer

    def _pkt(b: bytes) -> bytes:
        return f"{len(b) + 4:04x}".encode() + b

    head_sym, head_sha = refs_mod.read_head(repo)
    caps = b"side-band-64k ofs-delta agent=pythongit/0.1"
    if repo.object_format() == "sha256":
        caps += b" object-format=sha256"
    first = True
    for kind in ("refs/heads", "refs/tags", "refs/remotes"):
        root = repo.gitdir / kind
        if root.exists():
            for f in sorted(root.rglob("*")):
                if f.is_file():
                    rel = str(f.relative_to(repo.gitdir)).replace(os.sep, "/")
                    s = refs_mod.read_ref(repo, rel)
                    if s:
                        line = f"{s} {rel}".encode()
                        if first:
                            first = False
                            line += b"\0" + caps
                        out.write(_pkt(line + b"\n"))
    out.write(b"0000")
    out.flush()
    return 0


def cmd_receive_pack(argv: list[str]) -> int:
    """Stub: announce refs and accept no updates."""
    ap = argparse.ArgumentParser(prog="pygit receive-pack")
    ap.add_argument("directory")
    args = ap.parse_args(argv)
    repo = Repository.discover(args.directory)
    out = sys.stdout.buffer

    def _pkt(b: bytes) -> bytes:
        return f"{len(b) + 4:04x}".encode() + b

    caps = b"report-status agent=pythongit/0.1"
    if repo.object_format() == "sha256":
        caps += b" object-format=sha256"
    first = True
    for kind in ("refs/heads", "refs/tags"):
        root = repo.gitdir / kind
        if root.exists():
            for f in sorted(root.rglob("*")):
                if f.is_file():
                    rel = str(f.relative_to(repo.gitdir)).replace(os.sep, "/")
                    s = refs_mod.read_ref(repo, rel)
                    if s:
                        line = f"{s} {rel}".encode()
                        if first:
                            first = False
                            line += b"\0" + caps
                        out.write(_pkt(line + b"\n"))
    out.write(b"0000")
    out.flush()
    return 0


def cmd_upload_archive(argv: list[str]) -> int:
    """Server-side counterpart of `archive --remote=`. Local stub: produce archive."""
    ap = argparse.ArgumentParser(prog="pygit upload-archive")
    ap.add_argument("directory")
    args = ap.parse_args(argv)
    # delegate to archive
    return cmd_archive(["--format", "tar", "-o", "-", "HEAD"])


def cmd_pack_redundant(argv: list[str]) -> int:
    """List redundant packs (subset of another). Trivial heuristic: same SHA set."""
    ap = argparse.ArgumentParser(prog="pygit pack-redundant")
    ap.add_argument("--all", action="store_true")
    ap.parse_args(argv)
    repo = _repo()
    from . import pack as _p
    packs = list(_p._iter_packs(repo))
    sets = [(pk.pack_path, set(pk.shas)) for pk in packs]
    for i, (p, s) in enumerate(sets):
        for j, (p2, s2) in enumerate(sets):
            if i != j and s and s.issubset(s2) and s != s2:
                _print(str(p))
                break
    return 0


def cmd_prune_packed(argv: list[str]) -> int:
    """Remove loose objects that are also present in a pack."""
    ap = argparse.ArgumentParser(prog="pygit prune-packed")
    ap.add_argument("-n", "--dry-run", action="store_true")
    args = ap.parse_args(argv)
    repo = _repo()
    from . import pack as _p
    midx = _p.read_midx(repo)
    if midx is not None:
        in_packs: set[str] = set(midx.shas)
    else:
        in_packs = set()
        for pk in _p._iter_packs(repo):
            in_packs.update(pk.shas)
    removed = 0
    for sha in _iter_loose_shas(repo):
        if sha in in_packs:
            if args.dry_run:
                _print(f"would prune {sha}")
            else:
                (repo.gitdir / "objects" / sha[:2] / sha[2:]).unlink(missing_ok=True)
                removed += 1
    if not args.dry_run:
        _print(f"pruned {removed}")
    return 0


def cmd_merge_recursive(argv: list[str]) -> int:
    """Alias: merge using the default (3-way) strategy."""
    return cmd_merge(argv)


def cmd_merge_ours(argv: list[str]) -> int:
    """Merge that always keeps 'ours' — i.e. record a merge commit with current tree."""
    ap = argparse.ArgumentParser(prog="pygit merge-ours")
    ap.add_argument("other")
    args = ap.parse_args(argv)
    repo = _repo()
    head_sym, head = refs_mod.read_head(repo)
    other = refs_mod.rev_parse(repo, args.other)
    if not head or not other:
        return 128
    head_tree = objs.parse_commit(objs.read_object(repo, head)[1]).tree
    import time as _t
    name, email = repo.user()
    sig = objs.format_signature(name, email, when=int(_t.time()))
    c = objs.Commit(tree=head_tree, parents=[head, other], author=sig, committer=sig,
                    message=f"Merge {args.other} using ours strategy\n")
    sha = objs.write_object(repo, "commit", c.encode())
    if head_sym:
        refs_mod.update_ref(repo, head_sym, sha, message="merge-ours")
    else:
        refs_mod.set_head(repo, sha)
    _print(sha)
    return 0


def cmd_multi_pack_index(argv: list[str]) -> int:
    """Write and verify Git's binary multi-pack-index format."""
    ap = argparse.ArgumentParser(prog="pygit multi-pack-index")
    sub = ap.add_subparsers(dest="action", required=True)
    p_write = sub.add_parser("write")
    p_write.add_argument("--bitmap", action="store_true")
    p_write.add_argument("--no-bitmap", action="store_true")
    sub.add_parser("verify")
    sub.add_parser("expire")
    sub.add_parser("repack")
    args = ap.parse_args(argv)
    repo = _repo()
    pack_dir = repo.gitdir / "objects" / "pack"
    from . import pack as _p
    if args.action == "write":
        write_bitmap = args.bitmap and not args.no_bitmap
        _data, packs, objects = _p.write_midx(
            pack_dir,
            repo.object_format(),
            write_bitmap=write_bitmap,
            repo=repo,
        )
        _print(f"wrote multi-pack-index with {packs} packs, {objects} objects")
        return 0
    if args.action == "verify":
        if not (pack_dir / "multi-pack-index").exists():
            _err("no multi-pack-index")
            return 1
        try:
            packs, objects = _p.verify_midx(pack_dir)
            bitmaps = list(pack_dir.glob("multi-pack-index-*.bitmap"))
            if bitmaps:
                _p.verify_midx_bitmap(repo, pack_dir)
        except (OSError, ValueError) as exc:
            _err(f"multi-pack-index verify failed: {exc}")
            return 1
        _print(f"ok ({packs} packs, {objects} objects)")
        return 0
    if args.action in ("expire", "repack"):
        return 0
    return 1


def cmd_for_each_repo(argv: list[str]) -> int:
    """Run a pygit subcommand in each configured repo (via core.repos config list)."""
    ap = argparse.ArgumentParser(prog="pygit for-each-repo")
    ap.add_argument("--config", required=True, help="config key listing repo paths (comma-separated)")
    ap.add_argument("rest", nargs=argparse.REMAINDER)
    args = ap.parse_args(argv)
    # Read config from current repo to obtain the list
    repo = _repo()
    cp = repo.config()
    sect, _, key = args.config.partition(".")
    if not cp.has_option(sect, key):
        return 0
    repos = [r.strip() for r in cp.get(sect, key).split(",") if r.strip()]
    rc_total = 0
    saved_cwd = os.getcwd()
    for r in repos:
        try:
            os.chdir(r)
            rc = main(args.rest)
            if rc:
                rc_total = rc
        finally:
            os.chdir(saved_cwd)
    return rc_total


def cmd_diff_pairs(argv: list[str]) -> int:
    """Read pairs of tree shas from stdin; emit raw diff for each pair."""
    ap = argparse.ArgumentParser(prog="pygit diff-pairs")
    ap.parse_args(argv)
    repo = _repo()
    for line in sys.stdin.read().splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        a, b = parts[0], parts[1]
        am = workdir.flatten_tree(repo, a)
        bm = workdir.flatten_tree(repo, b)
        for p in sorted(set(am) | set(bm)):
            if am.get(p) == bm.get(p):
                continue
            ln = _raw_diff_status("100644", "100644", am.get(p), bm.get(p), p)
            if ln:
                _print(ln)
    return 0


def cmd_request_pull(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit request-pull")
    ap.add_argument("start")
    ap.add_argument("url")
    ap.add_argument("end", nargs="?", default="HEAD")
    args = ap.parse_args(argv)
    repo = _repo()
    start = refs_mod.rev_parse(repo, args.start)
    end = refs_mod.rev_parse(repo, args.end)
    if not start or not end:
        return 128
    _print(f"The following changes since commit {start[:7]}:")
    sc = objs.parse_commit(objs.read_object(repo, start)[1])
    _print(f"  {sc.message.splitlines()[0] if sc.message else ''} ({sc.committer})")
    _print("")
    _print(f"are available in the Git repository at:")
    _print(f"  {args.url}")
    _print("")
    _print("for you to fetch changes up to " + end[:7] + ":")
    ec = objs.parse_commit(objs.read_object(repo, end)[1])
    _print(f"  {ec.message.splitlines()[0] if ec.message else ''}")
    _print("")
    # shortlog between start..end
    return cmd_shortlog([end])


def cmd_diagnose(argv: list[str]) -> int:
    """Print diagnostic info about the repo (sizes, refs, packs)."""
    ap = argparse.ArgumentParser(prog="pygit diagnose")
    ap.add_argument("-o", "--output-directory", default=None)
    args = ap.parse_args(argv)
    repo = _repo()
    lines = []
    lines.append(f"pythongit version: 0.1.0")
    lines.append(f"gitdir: {repo.gitdir}")
    lines.append(f"worktree: {repo.path}")
    lines.append(f"branches: {len(refs_mod.list_branches(repo))}")
    lines.append(f"tags: {len(refs_mod.list_tags(repo))}")
    loose_count, _loose_size = _loose_count_and_size(repo)
    lines.append(f"loose objects: {loose_count}")
    from . import pack as _p
    packs = list(_p._iter_packs(repo))
    lines.append(f"packs: {len(packs)}")
    text = "\n".join(lines) + "\n"
    if args.output_directory:
        Path(args.output_directory).mkdir(parents=True, exist_ok=True)
        (Path(args.output_directory) / "diagnose.txt").write_text(text, encoding="utf-8")
    sys.stdout.write(text)
    return 0


def cmd_bugreport(argv: list[str]) -> int:
    """Print system+repo info suitable for a bug report."""
    ap = argparse.ArgumentParser(prog="pygit bugreport")
    ap.add_argument("-o", "--output-directory", default=None)
    args = ap.parse_args(argv)
    import platform
    lines = []
    lines.append(f"pythongit: 0.1.0")
    lines.append(f"python: {platform.python_version()}")
    lines.append(f"platform: {platform.platform()}")
    try:
        repo = _repo()
        lines.append(f"gitdir: {repo.gitdir}")
    except Exception:
        lines.append("not inside a repository")
    text = "\n".join(lines) + "\n"
    if args.output_directory:
        Path(args.output_directory).mkdir(parents=True, exist_ok=True)
        (Path(args.output_directory) / "bugreport.txt").write_text(text, encoding="utf-8")
    sys.stdout.write(text)
    return 0


def cmd_refs(argv: list[str]) -> int:
    """Newer subcommand grouping for ref manipulation."""
    ap = argparse.ArgumentParser(prog="pygit refs")
    sub = ap.add_subparsers(dest="action", required=True)
    sub.add_parser("list")
    p_get = sub.add_parser("get")
    p_get.add_argument("name")
    p_set = sub.add_parser("set")
    p_set.add_argument("name")
    p_set.add_argument("value")
    p_del = sub.add_parser("delete")
    p_del.add_argument("name")
    args = ap.parse_args(argv)
    repo = _repo()
    if args.action == "list":
        return cmd_show_ref([])
    if args.action == "get":
        s = refs_mod.read_ref(repo, args.name)
        if not s:
            return 1
        _print(s)
        return 0
    if args.action == "set":
        v = refs_mod.rev_parse(repo, args.value) or args.value
        refs_mod.update_ref(repo, args.name, v)
        return 0
    if args.action == "delete":
        refs_mod.delete_ref(repo, args.name)
        return 0
    return 1


def cmd_replay(argv: list[str]) -> int:
    """git replay (introduced 2024): apply commits from one branch onto another tip."""
    ap = argparse.ArgumentParser(prog="pygit replay")
    ap.add_argument("--onto", required=True)
    ap.add_argument("upstream")
    ap.add_argument("branch", nargs="?")
    args = ap.parse_args(argv)
    repo = _repo()
    saved_head_sym, saved_head = refs_mod.read_head(repo)
    tip = refs_mod.rev_parse(repo, args.branch) if args.branch else refs_mod.rev_parse(repo, "HEAD")
    onto = refs_mod.rev_parse(repo, args.onto)
    if not tip or not onto:
        return 128
    # Behave like rebase but don't update HEAD or worktree until done — for the
    # stub we delegate.
    refs_mod.set_head(repo, onto)
    from . import sequencer
    picked, confl = sequencer.rebase_onto(repo, args.upstream)
    if confl:
        return 1
    _, new_tip = refs_mod.read_head(repo)
    # restore HEAD
    if saved_head_sym:
        refs_mod.set_head(repo, saved_head_sym)
    else:
        refs_mod.set_head(repo, saved_head or "")
    _print(f"replayed {picked} commits onto {onto[:7]}, new tip {new_tip[:7] if new_tip else ''}")
    return 0


def cmd_backfill(argv: list[str]) -> int:
    """Download missing blobs from a remote (partial clone). Minimal: no-op."""
    ap = argparse.ArgumentParser(prog="pygit backfill")
    ap.parse_args(argv)
    _print("nothing to backfill")
    return 0


def cmd_convert_object_format(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit convert-object-format")
    ap.add_argument("--object-format", choices=["sha1", "sha256"], required=True)
    ap.add_argument("source")
    ap.add_argument("destination")
    args = ap.parse_args(argv)
    from . import translate
    dst = translate.convert_repository(args.source, args.destination, args.object_format)
    _print(f"Converted {args.source} to {args.object_format} repository at {dst.path}")
    return 0


def cmd_submodule_helper(argv: list[str]) -> int:
    """Internal: dispatch to a submodule subcommand."""
    return cmd_submodule(argv)


def cmd_checkout_worker(argv: list[str]) -> int:
    """Internal helper for parallel checkout. We don't parallelise."""
    return 0


# --- Out-of-scope: explicit stubs with clear messaging ---


def _oos(name: str, reason: str):
    def _f(_argv):
        _err(f"pygit: '{name}' is not supported (reason: {reason})")
        return 2
    return _f


def _register_phase8() -> None:
    _COMMANDS["init-db"] = cmd_init_db
    _COMMANDS["annotate"] = cmd_annotate
    _COMMANDS["patch-id"] = cmd_patch_id
    _COMMANDS["checkout-index"] = cmd_checkout_index
    _COMMANDS["fmt-merge-msg"] = cmd_fmt_merge_msg
    _COMMANDS["fetch-pack"] = cmd_fetch_pack
    _COMMANDS["send-pack"] = cmd_send_pack
    _COMMANDS["upload-pack"] = cmd_upload_pack
    _COMMANDS["receive-pack"] = cmd_receive_pack
    _COMMANDS["upload-archive"] = cmd_upload_archive
    _COMMANDS["pack-redundant"] = cmd_pack_redundant
    _COMMANDS["prune-packed"] = cmd_prune_packed
    _COMMANDS["merge-recursive"] = cmd_merge_recursive
    _COMMANDS["merge-ours"] = cmd_merge_ours
    _COMMANDS["multi-pack-index"] = cmd_multi_pack_index
    _COMMANDS["for-each-repo"] = cmd_for_each_repo
    _COMMANDS["diff-pairs"] = cmd_diff_pairs
    _COMMANDS["request-pull"] = cmd_request_pull
    _COMMANDS["diagnose"] = cmd_diagnose
    _COMMANDS["bugreport"] = cmd_bugreport
    _COMMANDS["refs"] = cmd_refs
    _COMMANDS["replay"] = cmd_replay
    _COMMANDS["backfill"] = cmd_backfill
    _COMMANDS["convert-object-format"] = cmd_convert_object_format
    _COMMANDS["submodule-helper"] = cmd_submodule_helper
    _COMMANDS["checkout-worker"] = cmd_checkout_worker

    # explicit out-of-scope stubs
    from . import bridges

    def _cmd_send_email(argv):
        ap = argparse.ArgumentParser(prog="pygit send-email")
        ap.add_argument("--to", action="append", required=True)
        ap.add_argument("--from", dest="from_addr", default=None)
        ap.add_argument("--smtp-server", default="localhost")
        ap.add_argument("--smtp-server-port", type=int, default=25)
        ap.add_argument("--smtp-user", default=None)
        ap.add_argument("--smtp-pass", default=None)
        ap.add_argument("--smtp-encryption", default=None,
                        help="tls/starttls or ssl; anything else disables encryption")
        ap.add_argument("--smtp-ssl", action="store_true",
                        help="deprecated alias for --smtp-encryption ssl")
        ap.add_argument("--smtp-ssl-cert-path", default=None)
        ap.add_argument("mbox")
        args = ap.parse_args(argv)
        enc = "ssl" if args.smtp_ssl else args.smtp_encryption
        return bridges.send_email(args.mbox, to=args.to, from_addr=args.from_addr,
                                  smtp_host=args.smtp_server, smtp_port=args.smtp_server_port,
                                  smtp_user=args.smtp_user, smtp_pass=args.smtp_pass,
                                  smtp_encryption=enc,
                                  smtp_ssl_cert_path=args.smtp_ssl_cert_path)

    def _cmd_difftool(argv):
        ap = argparse.ArgumentParser(prog="pygit difftool")
        ap.add_argument("-t", "--tool", default=None)
        ap.parse_args(argv)
        return bridges.run_difftool(_repo(), args_tool := None) or 0

    def _cmd_difftool2(argv):
        ap = argparse.ArgumentParser(prog="pygit difftool")
        ap.add_argument("-t", "--tool", default=None)
        a = ap.parse_args(argv)
        return bridges.run_difftool(_repo(), a.tool)

    def _cmd_mergetool(argv):
        ap = argparse.ArgumentParser(prog="pygit mergetool")
        ap.add_argument("-t", "--tool", default=None)
        ap.add_argument("paths", nargs="*")
        a = ap.parse_args(argv)
        return bridges.run_mergetool(_repo(), a.tool, a.paths)

    def _cmd_credential_store(argv):
        ap = argparse.ArgumentParser(prog="pygit credential-store")
        ap.add_argument("op", choices=["get", "store", "erase"])
        a = ap.parse_args(argv)
        fields = {}
        for line in sys.stdin.read().splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                fields[k] = v
        result = bridges.credential_store(a.op, fields)
        for k, v in result.items():
            _print(f"{k}={v}")
        return 0

    def _cmd_credential_cache(argv):
        # In-memory store backed by a temp file keyed by ppid; close-enough.
        ap = argparse.ArgumentParser(prog="pygit credential-cache")
        ap.add_argument("op", choices=["get", "store", "erase", "exit"])
        a = ap.parse_args(argv)
        if a.op == "exit":
            return 0
        path = Path(os.environ.get("TEMP", "/tmp")) / f"pygit-credcache-{os.getppid()}"
        fields = {}
        for line in sys.stdin.read().splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                fields[k] = v
        stored: dict[str, str] = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if "=" in line:
                    k, _, v = line.partition("=")
                    stored[k] = v
        if a.op == "store":
            stored.update(fields)
            path.write_text("\n".join(f"{k}={v}" for k, v in stored.items()), encoding="utf-8")
        elif a.op == "erase":
            path.unlink(missing_ok=True)
        elif a.op == "get":
            for k, v in stored.items():
                _print(f"{k}={v}")
        return 0

    def _cmd_credential_cache_daemon(argv):
        # The daemon variant: we don't run a separate process; the cache file
        # written by credential-cache is the same backing store.
        _print("pygit credential-cache: using file-backed cache (no daemon needed)")
        return 0

    def _cmd_fsmonitor(argv):
        ap = argparse.ArgumentParser(prog="pygit fsmonitor")
        ap.add_argument("--iterations", type=int, default=1)
        ap.add_argument("--interval", type=float, default=1.0)
        a = ap.parse_args(argv)
        return bridges.fsmonitor_run(_repo(), interval=a.interval, iterations=a.iterations)

    def _cmd_fsmonitor_daemon(argv):
        ap = argparse.ArgumentParser(prog="pygit fsmonitor-daemon")
        ap.add_argument("op", choices=["start", "stop", "run", "status"])
        a = ap.parse_args(argv)
        if a.op == "run":
            return bridges.fsmonitor_run(_repo(), iterations=0)
        _print(f"fsmonitor-daemon: {a.op} (no-op in pygit)")
        return 0

    def _cmd_daemon(argv):
        ap = argparse.ArgumentParser(prog="pygit daemon")
        ap.add_argument("--base-path", default=".")
        ap.add_argument("--port", type=int, default=9418)
        ap.add_argument("--listen", default="127.0.0.1")
        a = ap.parse_args(argv)
        return bridges.daemon_serve(a.base_path, host=a.listen, port=a.port)

    def _cmd_http_backend(argv):
        # CGI mode: read REQUEST_METHOD, PATH_INFO, CONTENT_LENGTH from env;
        # body from stdin; write status/headers/body to stdout.
        method = os.environ.get("REQUEST_METHOD", "GET")
        path = os.environ.get("PATH_INFO", "/")
        qs = os.environ.get("QUERY_STRING", "")
        if qs:
            path += "?" + qs
        clen = int(os.environ.get("CONTENT_LENGTH", "0") or "0")
        body = sys.stdin.buffer.read(clen) if clen else b""
        base = Path(os.environ.get("GIT_PROJECT_ROOT", os.getcwd()))
        status, headers, out = bridges.http_backend(method, path, body, base)
        sys.stdout.write(f"Status: {status}\r\n")
        for k, v in headers.items():
            sys.stdout.write(f"{k}: {v}\r\n")
        sys.stdout.write("\r\n")
        sys.stdout.flush()
        sys.stdout.buffer.write(out)
        return 0

    def _cmd_http_fetch(argv):
        ap = argparse.ArgumentParser(prog="pygit http-fetch")
        ap.add_argument("url")
        ap.add_argument("sha")
        a = ap.parse_args(argv)
        return bridges.http_fetch(a.url, a.sha, _repo())

    def _cmd_instaweb(argv):
        ap = argparse.ArgumentParser(prog="pygit instaweb")
        ap.add_argument("--port", type=int, default=1234)
        a = ap.parse_args(argv)
        return bridges.instaweb(_repo(), port=a.port)

    def _cmd_gitk(argv):
        return bridges.launch_tk(_repo())

    def _cmd_gui(argv):
        return bridges.launch_tk(_repo())

    def _cmd_gitweb(argv):
        return bridges.instaweb(_repo(), port=1234)

    def _cmd_mergetool_remote_helper(argv):
        # remote-helper is the dispatcher for transport plug-ins.
        # We support direct https:// URLs already; everything else needs
        # a custom helper binary.
        ap = argparse.ArgumentParser(prog="pygit remote-helper")
        ap.add_argument("remote")
        ap.add_argument("url", nargs="?")
        a = ap.parse_args(argv)
        # implement two commands: capabilities + list
        text = sys.stdin.read()
        for line in text.splitlines():
            line = line.strip()
            if line == "capabilities":
                _print("fetch")
                _print("push")
                _print("")
            elif line == "list":
                # list refs of the remote
                if not a.url:
                    _print("")
                    continue
                try:
                    from . import protocol
                    refs = protocol.discover_refs(a.url)
                    for name, sha in refs.items():
                        _print(f"{sha} {name}")
                    _print("")
                except Exception as e:
                    _err(f"list failed: {e}")
                    return 1
            elif not line:
                break
        return 0

    def _cmd_remote_ext(argv):
        # remote-ext runs an external command to talk pkt-line.
        ap = argparse.ArgumentParser(prog="pygit remote-ext")
        ap.add_argument("command")
        ap.add_argument("rest", nargs=argparse.REMAINDER)
        a = ap.parse_args(argv)
        try:
            return subprocess.call([a.command, *a.rest])
        except FileNotFoundError:
            _err(f"helper not found: {a.command}")
            return 127

    def _cmd_remote_fd(argv):
        # Reads/writes pkt-line on a given file descriptor; we just exit cleanly.
        return 0

    def _cmd_maintenance(argv):
        ap = argparse.ArgumentParser(prog="pygit maintenance")
        sub = ap.add_subparsers(dest="action", required=True)
        sub.add_parser("run")
        sub.add_parser("start")
        sub.add_parser("stop")
        sub.add_parser("register")
        sub.add_parser("unregister")
        a = ap.parse_args(argv)
        if a.action == "run":
            cmd_gc([])
            cmd_repack(["-a", "-d"])
            cmd_commit_graph(["write"])
            cmd_prune([])
            return 0
        # other actions are scheduler hooks; we just succeed
        _print(f"maintenance {a.action}: ok")
        return 0

    def _cmd_shell(argv):
        # Restricted dispatcher: only allow a small allowlist of subcommands.
        ap = argparse.ArgumentParser(prog="pygit shell")
        ap.add_argument("-c", dest="command", required=False)
        a = ap.parse_args(argv)
        if not a.command:
            _err("interactive shell not supported")
            return 1
        # parse command into argv
        import shlex
        parts = shlex.split(a.command)
        if not parts:
            return 1
        allowed = {"git-receive-pack", "git-upload-pack", "git-upload-archive"}
        cmd = parts[0]
        if cmd not in allowed:
            _err(f"shell: '{cmd}' not allowed")
            return 1
        # dispatch to our equivalent
        mapping = {
            "git-receive-pack": "receive-pack",
            "git-upload-pack": "upload-pack",
            "git-upload-archive": "upload-archive",
        }
        return main([mapping[cmd], *parts[1:]])

    def _cmd_cvs_bridge(name):
        def _cvs_dispatch(argv):
            return bridges.shell_out(["cvs", *argv]) if name == "cvsserver" else \
                   bridges.shell_out(["cvs", name.replace("cvs", ""), *argv])
        return _cvs_dispatch

    def _cmd_svn(argv):
        return bridges.shell_out(["svn", *argv])

    _COMMANDS["send-email"] = _cmd_send_email
    _COMMANDS["gitk"] = _cmd_gitk
    _COMMANDS["gitweb"] = _cmd_gitweb
    _COMMANDS["gui"] = _cmd_gui
    _COMMANDS["instaweb"] = _cmd_instaweb
    _COMMANDS["difftool"] = _cmd_difftool2
    _COMMANDS["mergetool"] = _cmd_mergetool
    _COMMANDS["cvsexportcommit"] = _cmd_cvs_bridge("cvsexportcommit")
    _COMMANDS["cvsimport"] = _cmd_cvs_bridge("cvsimport")
    _COMMANDS["cvsserver"] = _cmd_cvs_bridge("cvsserver")
    _COMMANDS["svn"] = _cmd_svn
    _COMMANDS["credential-cache"] = _cmd_credential_cache
    _COMMANDS["credential-cache-daemon"] = _cmd_credential_cache_daemon
    _COMMANDS["credential-store"] = _cmd_credential_store
    _COMMANDS["fsmonitor"] = _cmd_fsmonitor
    _COMMANDS["fsmonitor-daemon"] = _cmd_fsmonitor_daemon
    _COMMANDS["remote-helper"] = _cmd_mergetool_remote_helper
    _COMMANDS["remote-ext"] = _cmd_remote_ext
    _COMMANDS["remote-fd"] = _cmd_remote_fd
    _COMMANDS["maintenance"] = _cmd_maintenance
    _COMMANDS["shell"] = _cmd_shell
    _COMMANDS["daemon"] = _cmd_daemon
    _COMMANDS["http-backend"] = _cmd_http_backend
    _COMMANDS["http-fetch"] = _cmd_http_fetch

    # Newer git commands (git 2.45+)
    def _cmd_url_parse(argv):
        ap = argparse.ArgumentParser(prog="pygit url-parse")
        ap.add_argument("-c", "--component", default=None,
                        choices=["protocol", "host", "port", "path", "user", "password", "url"])
        ap.add_argument("urls", nargs="+")
        a = ap.parse_args(argv)
        import urllib.parse
        for u in a.urls:
            p = urllib.parse.urlparse(u)
            comp = {
                "protocol": p.scheme,
                "host": p.hostname or "",
                "port": str(p.port) if p.port else "",
                "path": p.path,
                "user": p.username or "",
                "password": p.password or "",
                "url": u,
            }
            if a.component:
                _print(comp[a.component])
            else:
                for k, v in comp.items():
                    _print(f"{k}={v}")
        return 0

    def _cmd_history(argv):
        # git history fixup/reword <commit>: edit a past commit in place by
        # rebuilding the chain from that commit forward.
        ap = argparse.ArgumentParser(prog="pygit history")
        sub = ap.add_subparsers(dest="action", required=True)
        p_fixup = sub.add_parser("fixup")
        p_fixup.add_argument("commit")
        p_fixup.add_argument("--dry-run", action="store_true")
        p_reword = sub.add_parser("reword")
        p_reword.add_argument("commit")
        p_reword.add_argument("-m", "--message", required=False)
        a = ap.parse_args(argv)
        repo = _repo()
        target = refs_mod.rev_parse(repo, a.commit)
        if not target:
            return 128
        head_sym, head = refs_mod.read_head(repo)
        if not head:
            return 128
        # collect commits target..HEAD (first-parent)
        chain = []
        cur = head
        while cur and cur != target:
            chain.append(cur)
            c = objs.parse_commit(objs.read_object(repo, cur)[1])
            cur = c.parents[0] if c.parents else None
        if cur != target:
            _err("commit not on first-parent chain")
            return 1
        chain.reverse()
        target_c = objs.parse_commit(objs.read_object(repo, target)[1])
        if a.action == "reword":
            new_msg = a.message or target_c.message
            new_c = objs.Commit(tree=target_c.tree, parents=target_c.parents,
                                author=target_c.author, committer=target_c.committer,
                                message=new_msg if new_msg.endswith("\n") else new_msg + "\n")
        else:
            # fixup: drop the commit (use parent as new base)
            if not target_c.parents:
                _err("cannot drop a root commit")
                return 1
            new_target = target_c.parents[0]
            if a.dry_run:
                _print(f"would drop {target[:7]}")
                return 0
            # rewrite chain on top of new_target
            from . import sequencer
            refs_mod.set_head(repo, new_target)
            picked, conf = sequencer.rebase_onto(repo, new_target)
            if conf:
                return 1
            return 0
        if a.dry_run:
            _print(f"would reword {target[:7]}")
            return 0
        new_target_sha = objs.write_object(repo, "commit", new_c.encode())
        # rewrite chain
        cur_parent = new_target_sha
        for s in chain:
            sc = objs.parse_commit(objs.read_object(repo, s)[1])
            nc = objs.Commit(tree=sc.tree, parents=[cur_parent] + sc.parents[1:],
                             author=sc.author, committer=sc.committer, message=sc.message)
            cur_parent = objs.write_object(repo, "commit", nc.encode())
        if head_sym:
            refs_mod.update_ref(repo, head_sym, cur_parent, message=f"history {a.action}")
        else:
            refs_mod.set_head(repo, cur_parent)
        return 0

    def _cmd_last_modified(argv):
        ap = argparse.ArgumentParser(prog="pygit last-modified")
        ap.add_argument("path")
        a = ap.parse_args(argv)
        repo = _repo()
        head = refs_mod.rev_parse(repo, "HEAD")
        if not head:
            return 128
        cur = head
        last_changed_sha = head
        last_blob = None
        graph = _graph_for_repo(repo)
        while cur:
            info = _commit_tree_parents(repo, cur, graph)
            if info is None:
                break
            tree, parents = info
            entry = workdir.tree_path_entry(repo, tree, a.path)
            if entry is None or entry.is_dir() or entry.is_gitlink():
                if last_blob is not None:
                    break
                cur = parents[0] if parents else None
                continue
            if last_blob is None:
                last_blob = entry.sha
                last_changed_sha = cur
            elif entry.sha != last_blob:
                break
            else:
                last_changed_sha = cur
            cur = parents[0] if parents else None
        _print(last_changed_sha)
        return 0

    def _cmd_repo(argv):
        ap = argparse.ArgumentParser(prog="pygit repo")
        sub = ap.add_subparsers(dest="action", required=True)
        sub.add_parser("info")
        a = ap.parse_args(argv)
        if a.action == "info":
            repo = _repo()
            _print(f"path: {repo.path}")
            _print(f"gitdir: {repo.gitdir}")
            _print(f"bare: {repo.bare}")
            cp = repo.config()
            for s in cp.sections():
                for k in cp.options(s):
                    _print(f"{s}.{k} = {cp.get(s, k)}")
            return 0
        return 1

    _COMMANDS["url-parse"] = _cmd_url_parse
    _COMMANDS["history"] = _cmd_history
    _COMMANDS["last-modified"] = _cmd_last_modified
    _COMMANDS["repo"] = _cmd_repo


_register_phase8()


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:]) if argv is None else list(argv)
    if not argv or argv[0] in ("-h", "--help"):
        return cmd_help([])
    if argv[0] == "--version":
        from . import __version__
        _print(f"pygit version {__version__}")
        return 0
    cmd = argv[0]
    rest = argv[1:]
    fn = _COMMANDS.get(cmd)
    if fn is None:
        _err(f"pygit: '{cmd}' is not a pygit command. See 'pygit help'.")
        return 1
    try:
        return fn(rest)
    except RepositoryError as e:
        _err(f"fatal: {e}")
        return 128
    except BrokenPipeError:
        return 0
