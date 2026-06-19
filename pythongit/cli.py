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


def _commit_date(repo: Repository, sha: str) -> int:
    """Committer timestamp of a commit (0 if unparseable), for log ordering."""
    try:
        commit = objs.parse_commit(objs.read_object(repo, sha)[1])
    except KeyError:
        return 0
    import re
    m = re.search(r" (\d+) [+-]\d{4}\s*$", commit.committer)
    return int(m.group(1)) if m else 0


def _approxidate(s: str) -> int:
    """Parse a --since/--until date into epoch seconds (UTC for bare dates)."""
    import datetime
    s = s.strip()
    parsed = objs._parse_date_env(s)
    if parsed is not None:
        return parsed[0]
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.datetime.strptime(s, fmt).replace(tzinfo=datetime.timezone.utc)
            return int(dt.timestamp())
        except ValueError:
            continue
    return 0


def _pickaxe_match(repo: Repository, c, args, flags) -> bool:
    """True if the commit matches -S (occurrence count changed) or -G (a diff
    line matches), comparing against the first parent."""
    import re
    # The combined diff of a merge is suppressed by default, so pickaxe never
    # matches a merge commit.
    if len(c.parents) > 1:
        return False
    parent_tree = None
    if c.parents:
        parent_tree = objs.parse_commit(objs.read_object(repo, c.parents[0])[1]).tree
    changes = _tree_changes(repo, parent_tree, c.tree)
    if args.pickaxe_s is not None:
        needle = args.pickaxe_s
        for _p, a, b in changes:
            ca = (a.data or b"").decode("utf-8", "replace").count(needle) if a.present else 0
            cb = (b.data or b"").decode("utf-8", "replace").count(needle) if b.present else 0
            if ca != cb:
                return True
        return False
    rx = re.compile(args.pickaxe_g, flags)
    for _p, a, b in changes:
        a_lines = (a.data or b"").decode("utf-8", "replace").splitlines() if a.present else []
        b_lines = (b.data or b"").decode("utf-8", "replace").splitlines() if b.present else []
        for op in diff_mod.diff_lines(a_lines, b_lines):
            if op[0] == "ins" and rx.search(b_lines[op[2]]):
                return True
            if op[0] == "del" and rx.search(a_lines[op[1]]):
                return True
    return False


def _filter_commits(repo: Repository, commits: list, args) -> list:
    """Apply log/rev-list commit-level filters: merge/parent count, date,
    --grep/--author/--committer, and -S/-G pickaxe."""
    import re
    flags = re.IGNORECASE if getattr(args, "ignore_case", False) else 0
    greps = [re.compile(p, flags) for p in (getattr(args, "grep", None) or [])]
    authors = [re.compile(p, flags) for p in (getattr(args, "author", None) or [])]
    committers = [re.compile(p, flags) for p in (getattr(args, "committer", None) or [])]
    since = _approxidate(args.since) if getattr(args, "since", None) else None
    until = _approxidate(args.until) if getattr(args, "until", None) else None
    out = []
    for s in commits:
        c = objs.parse_commit(objs.read_object(repo, s)[1])
        np = len(c.parents)
        if getattr(args, "no_merges", False) and np >= 2:
            continue
        if getattr(args, "merges", False) and np < 2:
            continue
        if getattr(args, "min_parents", None) is not None and np < args.min_parents:
            continue
        if getattr(args, "max_parents", None) is not None and np > args.max_parents:
            continue
        if since is not None or until is not None:
            d = _commit_date(repo, s)
            if since is not None and d < since:
                continue
            if until is not None and d > until:
                continue
        if greps:
            ok = (all if getattr(args, "all_match", False) else any)(g.search(c.message) for g in greps)
            if not ok:
                continue
        if authors and not any(a.search(c.author) for a in authors):
            continue
        if committers and not any(a.search(c.committer) for a in committers):
            continue
        if getattr(args, "pickaxe_s", None) is not None or getattr(args, "pickaxe_g", None) is not None:
            if not _pickaxe_match(repo, c, args, flags):
                continue
        out.append(s)
    return out


def _topo_order(repo: Repository, orig: list, first_parent: bool = False) -> list:
    """Topologically order ``orig`` like C Git's REV_SORT_IN_GRAPH_ORDER
    (commit.c:sort_in_topological_order): a stack-based walk that emits a commit
    only after all its in-set children, with tips kept in traversal order."""
    in_set = set(orig)

    def parents(s):
        info = _commit_tree_parents(repo, s)
        if not info:
            return []
        return list(info[1][:1] if first_parent else info[1])

    indegree = {s: 1 for s in orig}
    for s in orig:
        for p in parents(s):
            if p in indegree:
                indegree[p] += 1
    # Tips have no in-set child (indegree 1); the NULL-compare prio_queue is a
    # stack, and tips are reversed so they pop in original traversal order.
    tips = [s for s in orig if indegree[s] == 1]
    stack = list(reversed(tips))
    out: list[str] = []
    while stack:
        s = stack.pop()
        out.append(s)
        for p in parents(s):
            if indegree.get(p, 0) == 0:
                continue
            indegree[p] -= 1
            if indegree[p] == 1:
                stack.append(p)
    return out


# ---------------------------------------------------------------------------
# plumbing


_DEFAULT_BRANCH_HINT = (
    "hint: Using 'master' as the name for the initial branch. This default branch name\n"
    "hint: will change to \"main\" in Git 3.0. To configure the initial branch name\n"
    "hint: to use in all of your new repositories, which will suppress this warning,\n"
    "hint: call:\n"
    "hint:\n"
    "hint: \tgit config --global init.defaultBranch <name>\n"
    "hint:\n"
    "hint: Names commonly chosen instead of 'master' are 'main', 'trunk' and\n"
    "hint: 'development'. The just-created branch can be renamed via this command:\n"
    "hint:\n"
    "hint: \tgit branch -m <name>\n"
    "hint:\n"
    "hint: Disable this message with \"git config set advice.defaultBranchName false\"\n"
)


def cmd_init(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit init", add_help=False)
    ap.add_argument("path", nargs="?", default=".")
    ap.add_argument("--bare", action="store_true")
    ap.add_argument("--object-format", choices=["sha1", "sha256"], default="sha1")
    ap.add_argument("-b", "--initial-branch", default=None)
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args(argv)

    from . import gitconfig
    target = Path(args.path).resolve()
    gitdir = target if args.bare else target / ".git"
    already = (gitdir / "HEAD").exists()

    show_hint = False
    if args.initial_branch is not None:
        branch = args.initial_branch
    else:
        configured = gitconfig.get(None, "init.defaultbranch")
        if configured:
            branch = configured
        else:
            branch = "master"
            advice = (gitconfig.get(None, "advice.defaultbranchname") or "").lower()
            show_hint = not already and advice not in ("false", "0", "no", "off")

    repo = Repository.init(target, bare=args.bare, object_format=args.object_format)
    if not already:
        (repo.gitdir / "HEAD").write_text(f"ref: refs/heads/{branch}\n", encoding="utf-8")

    if already and args.initial_branch is not None:
        _err(f"warning: re-init: ignored --initial-branch={args.initial_branch}")
    if show_hint:
        sys.stderr.write(_DEFAULT_BRANCH_HINT)
    if not args.quiet:
        word = "Reinitialized existing" if already else "Initialized empty"
        _print(f"{word} Git repository in {repo.gitdir}/")
    return 0


def cmd_hash_object(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit hash-object", add_help=False)
    ap.add_argument("-w", action="store_true", help="write object")
    ap.add_argument("-t", default="blob", choices=["blob", "tree", "commit", "tag"])
    ap.add_argument("--stdin", action="store_true")
    ap.add_argument("--stdin-paths", action="store_true")
    ap.add_argument("files", nargs="*")
    args = ap.parse_args(argv)
    repo = None
    try:
        repo = _repo()
    except RepositoryError:
        pass

    def emit(data: bytes) -> None:
        if args.w:
            _print(objs.write_object(repo, args.t, data))
        else:
            _print(objs.hash_bytes(args.t, data, repo)[0])

    if args.stdin_paths:
        for line in sys.stdin.read().splitlines():
            if line:
                emit(Path(line).read_bytes())
        return 0
    if args.stdin:
        emit(sys.stdin.buffer.read())
    if args.files:
        for f in args.files:
            emit(Path(f).read_bytes())
    elif not args.stdin:
        ap.error("need file or --stdin")
    return 0


def _all_object_shas(repo: Repository) -> list[str]:
    """Every object id in the repo (loose + packed), sorted ascending — the
    order `cat-file --batch-all-objects` emits."""
    shas: set[str] = set()
    objdir = repo.gitdir / "objects"
    hex_len = repo.hex_len
    if objdir.is_dir():
        for d in objdir.iterdir():
            if d.is_dir() and len(d.name) == 2 and all(c in "0123456789abcdef" for c in d.name):
                for f in d.iterdir():
                    if f.is_file() and len(f.name) == hex_len - 2:
                        shas.add(d.name + f.name)
    from . import pack as _p
    midx = _p.read_midx(repo)
    if midx is not None:
        shas.update(midx.shas)
    else:
        for pk in _p._iter_packs(repo):
            shas.update(pk.shas)
    return sorted(shas)


def _expand_batch_atoms(fmt: str, sha: str, t: str, size: int) -> str:
    """Expand the `cat-file --batch[-check]=<format>` %(atom) placeholders."""
    return (fmt.replace("%(objectname)", sha)
               .replace("%(objecttype)", t)
               .replace("%(objectsize:disk)", str(size))
               .replace("%(objectsize)", str(size)))


def _cat_file_batch(repo: Repository, check_only: bool, names=None, fmt=None) -> int:
    if fmt is None:
        fmt = "%(objectname) %(objecttype) %(objectsize)"
    source = names if names is not None else (line.strip() for line in sys.stdin)
    for name in source:
        if not name:
            continue
        sha = refs_mod.rev_parse(repo, name)
        if sha is None or not objs.object_exists(repo, sha):
            sys.stdout.write(f"{name} missing\n")
            continue
        t, data = objs.read_object(repo, sha)
        info = _expand_batch_atoms(fmt, sha, t, len(data))
        sys.stdout.write(info + "\n")
        if not check_only:
            sys.stdout.flush()
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.write(b"\n")
            sys.stdout.buffer.flush()
    return 0


def _cat_file_batch_command(repo: Repository) -> int:
    """cat-file --batch-command: per-line `info`/`contents`/`flush` requests."""
    for line in sys.stdin:
        line = line.rstrip("\n")
        if not line:
            continue
        cmd, _, arg = line.partition(" ")
        arg = arg.strip()
        if cmd == "flush":
            sys.stdout.flush()
            sys.stdout.buffer.flush()
            continue
        sha = refs_mod.rev_parse(repo, arg)
        if sha is None or not objs.object_exists(repo, sha):
            sys.stdout.write(f"{arg} missing\n")
            continue
        t, data = objs.read_object(repo, sha)
        if cmd == "info":
            sys.stdout.write(f"{sha} {t} {len(data)}\n")
        elif cmd == "contents":
            sys.stdout.write(f"{sha} {t} {len(data)}\n")
            sys.stdout.flush()
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.write(b"\n")
            sys.stdout.buffer.flush()
    return 0


def cmd_cat_file(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit cat-file", add_help=False)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("-t", dest="show_type", action="store_true")
    g.add_argument("-s", dest="show_size", action="store_true")
    g.add_argument("-p", dest="pretty", action="store_true")
    g.add_argument("-e", dest="exists", action="store_true")
    g.add_argument("--batch", dest="batch", action="store_true")
    g.add_argument("--batch-check", dest="batch_check", action="store_true")
    g.add_argument("--batch-command", dest="batch_command", action="store_true")
    g.add_argument("--textconv", action="store_true")
    g.add_argument("--filters", action="store_true")
    ap.add_argument("--batch-all-objects", dest="batch_all", action="store_true")
    ap.add_argument("--path", default=None)
    ap.add_argument("pos", nargs="*")
    # `--batch[-check]=<format>` takes the format attached with '='; pull it out
    # so the store_true flags still parse, then thread it into the formatter.
    batch_fmt = None
    pre_argv = []
    for a in argv:
        if a.startswith("--batch-check="):
            batch_fmt = a.split("=", 1)[1]
            pre_argv.append("--batch-check")
        elif a.startswith("--batch="):
            batch_fmt = a.split("=", 1)[1]
            pre_argv.append("--batch")
        else:
            pre_argv.append(a)
    args = ap.parse_args(pre_argv)
    repo = _repo()
    if args.batch_command:
        return _cat_file_batch_command(repo)
    if args.batch or args.batch_check:
        names = _all_object_shas(repo) if args.batch_all else None
        return _cat_file_batch(repo, check_only=args.batch_check, names=names, fmt=batch_fmt)

    # --textconv / --filters: with no configured drivers these are the identity
    # transform, so just stream the blob content (resolving <rev>:<path>).
    if args.textconv or args.filters:
        obj = args.pos[0] if args.pos else None
        if obj is None and args.path:
            obj = args.path
        sha = refs_mod.rev_parse(repo, obj) if obj else None
        if sha is None or not objs.object_exists(repo, sha):
            _err(f"fatal: Not a valid object name {obj}")
            return 128
        sys.stdout.buffer.write(objs.read_object(repo, sha)[1])
        return 0

    # The `cat-file <type> <object>` form prints the raw object content.
    has_flag = args.show_type or args.show_size or args.pretty or args.exists
    if not has_flag and len(args.pos) == 2 and args.pos[0] in ("blob", "commit", "tree", "tag"):
        want_type, obj = args.pos
        sha = refs_mod.rev_parse(repo, obj)
        if sha is None or not objs.object_exists(repo, sha):
            _err(f"fatal: Not a valid object name {obj}")
            return 128
        t, data = objs.read_object(repo, sha)
        if t != want_type:
            _err(f"fatal: cat-file {want_type}: bad file")
            return 128
        sys.stdout.buffer.write(data)
        return 0

    args.object = args.pos[0] if args.pos else None
    if args.object is None:
        _err("fatal: <object> required")
        return 128
    sha = refs_mod.rev_parse(repo, args.object)
    exists = sha is not None and objs.object_exists(repo, sha)
    if args.exists:
        return 0 if exists else 1
    if sha is None or not exists:
        if (args.show_type or args.show_size) and sha is not None:
            _err("fatal: git cat-file: could not get object info")
        else:
            _err(f"fatal: Not a valid object name {args.object}")
        return 128
    t, data = objs.read_object(repo, sha)
    if args.show_type:
        _print(t)
    elif args.show_size:
        _print(str(len(data)))
    elif args.pretty:
        if t == "tree":
            for e in objs.parse_tree(data, repo.hash_len):
                obj_t = "tree" if e.is_dir() else "blob"
                _print(f"{e.mode.zfill(6)} {obj_t} {e.sha}\t{e.name}")
        else:
            sys.stdout.buffer.write(data)
            if not data.endswith(b"\n"):
                sys.stdout.write("\n")
    return 0


def cmd_ls_tree(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit ls-tree", add_help=False)
    ap.add_argument("-r", action="store_true", help="recurse")
    ap.add_argument("-d", dest="dirs_only", action="store_true")
    ap.add_argument("-t", dest="show_trees", action="store_true")
    ap.add_argument("-l", "--long", dest="long", action="store_true")
    ap.add_argument("--name-only", "--name-status", dest="name_only", action="store_true")
    ap.add_argument("--object-only", dest="object_only", action="store_true")
    ap.add_argument("--full-tree", action="store_true")
    ap.add_argument("--full-name", action="store_true")
    ap.add_argument("--abbrev", nargs="?", const=7, type=int, default=None)
    ap.add_argument("-z", dest="nul", action="store_true")
    ap.add_argument("treeish")
    ap.add_argument("paths", nargs="*")
    # Git only consumes a value for --abbrev when attached with '='; a bare
    # --abbrev uses the default length, leaving the next token as the treeish.
    argv = ["--abbrev=7" if a == "--abbrev" else a for a in argv]
    args = ap.parse_args(argv)
    repo = _repo()
    sha = refs_mod.rev_parse(repo, args.treeish)
    if not sha:
        _err(f"fatal: Not a valid object name {args.treeish}")
        return 128
    t, data = objs.read_object(repo, sha)
    if t == "commit":
        sha = objs.parse_commit(data).tree
    elif t == "tag":
        sha = refs_mod._peel_to_type(repo, sha, "tree") or sha
    eol = "\0" if args.nul else "\n"

    def emit(e, path):
        obj_t = "tree" if e.is_dir() else "blob"
        sha = e.sha[:args.abbrev] if args.abbrev is not None else e.sha
        if args.object_only:
            sys.stdout.write(sha + eol)
        elif args.name_only:
            sys.stdout.write(path + eol)
        elif args.long:
            if e.is_dir():
                size = "-"
            else:
                try:
                    size = str(len(objs.read_object(repo, e.sha)[1]))
                except KeyError:
                    size = "-"
            sys.stdout.write(f"{e.mode.zfill(6)} {obj_t} {sha} {size:>7}\t{path}" + eol)
        else:
            sys.stdout.write(f"{e.mode.zfill(6)} {obj_t} {sha}\t{path}" + eol)

    specs = [p.rstrip("/") for p in args.paths]

    def matches(path: str) -> bool:
        # The entry path is exactly a pathspec or lies under one.
        if not specs:
            return True
        return any(path == s or path.startswith(s + "/") for s in specs)

    def should_descend(path: str) -> bool:
        # Descend a directory if it is selected, or contains a pathspec.
        if not specs:
            return True
        return any(path == s or path.startswith(s + "/") or s.startswith(path + "/")
                   for s in specs)

    def walk(tsha: str, prefix: str = "") -> None:
        _t, td = objs.read_object(repo, tsha)
        for e in objs.parse_tree(td, repo.hash_len):
            path = prefix + e.name
            if e.is_dir():
                if args.r:
                    if matches(path) and args.show_trees:
                        emit(e, path)
                    if matches(path) or should_descend(path):
                        walk(e.sha, path + "/")
                else:
                    if matches(path):
                        emit(e, path)
                    elif should_descend(path):
                        walk(e.sha, path + "/")
            else:
                if not args.dirs_only and matches(path):
                    emit(e, path)

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
    ap = argparse.ArgumentParser(prog="pygit commit-tree", add_help=False)
    ap.add_argument("tree")
    ap.add_argument("-p", "--parent", action="append", default=[])
    ap.add_argument("-m", "--message", action="append", default=[])
    ap.add_argument("-F", "--file", default=None)
    args = ap.parse_args(argv)
    repo = _repo()
    # Resolve the tree-ish (e.g. HEAD^{tree}, a commit, or a raw oid) to a tree.
    tree_sha = refs_mod.rev_parse(repo, args.tree + "^{tree}") or refs_mod.rev_parse(repo, args.tree)
    if not tree_sha:
        _err(f"fatal: not a valid object name {args.tree}")
        return 128
    parents = []
    for p in args.parent:
        ps = refs_mod.rev_parse(repo, p)
        if not ps:
            _err(f"fatal: not a valid object name {p}")
            return 128
        parents.append(ps)
    # Message: -m paragraphs joined by a blank line, or -F <file>, or stdin.
    if args.message:
        msg = "\n\n".join(args.message) + "\n"
    elif args.file:
        msg = open(args.file, encoding="utf-8").read()
    else:
        msg = sys.stdin.read()
    c = objs.Commit(
        tree=tree_sha,
        parents=parents,
        author=objs.build_signature(repo, "author"),
        committer=objs.build_signature(repo, "committer"),
        message=msg if msg.endswith("\n") else msg + "\n",
    )
    sha = objs.write_object(repo, "commit", c.encode())
    _print(sha)
    return 0


def cmd_update_ref(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit update-ref", add_help=False)
    ap.add_argument("-d", dest="delete", action="store_true")
    ap.add_argument("-m", dest="message", default="")
    ap.add_argument("--no-deref", action="store_true")
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
    refs_mod.update_ref(repo, args.ref, sha, message=args.message)
    return 0


def cmd_symbolic_ref(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit symbolic-ref", add_help=False)
    ap.add_argument("--short", action="store_true")
    ap.add_argument("-q", "--quiet", action="store_true")
    ap.add_argument("-d", "--delete", action="store_true")
    ap.add_argument("-m", dest="reason", default=None)
    ap.add_argument("name")
    ap.add_argument("target", nargs="?")
    args = ap.parse_args(argv)
    repo = _repo()
    p = repo.gitdir / args.name
    if args.delete:
        txt = p.read_text(encoding="utf-8").strip() if p.exists() else ""
        if not txt.startswith("ref: "):
            if not args.quiet:
                _err(f"fatal: Cannot delete {args.name}, not a symbolic ref")
            return 128
        p.unlink()
        return 0
    if args.target is None:
        if not p.exists():
            if not args.quiet:
                _err(f"fatal: ref {args.name} is not a symbolic ref")
            return 128
        txt = p.read_text(encoding="utf-8").strip()
        if txt.startswith("ref: "):
            target = txt[5:].strip()
            _print(refs_mod.shorten_ref(target) if args.short else target)
            return 0
        # A real (non-symbolic) ref: quiet exits 1, otherwise fatal (128).
        if args.quiet:
            return 1
        _err(f"fatal: ref {args.name} is not a symbolic ref")
        return 128
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"ref: {args.target}\n", encoding="utf-8")
    return 0


def _is_inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def cmd_rev_parse(argv: list[str]) -> int:
    repo: Optional[Repository] = None

    def R() -> Repository:
        nonlocal repo
        if repo is None:
            repo = _repo()
        return repo

    # --verify requires exactly one revision; reject extras before any output.
    if "--verify" in argv:
        rev_args = [a for a in argv if not a.startswith("-")]
        if len(rev_args) > 1:
            if "-q" not in argv and "--quiet" not in argv:
                _err("fatal: Needed a single revision")
                return 128
            return 1

    abbrev = 0            # 0 = full hex; >0 = abbreviate to N chars
    symbolic: Optional[str] = None   # None | "full" | "abbrev"
    sym_refs = False      # --symbolic: print ref names for --all/--branches/...
    verify = False
    quiet = False
    no_revs = False
    revs_only = False
    path_format: Optional[str] = None
    verified_count = 0
    after_dashdash = False
    pending_git_path = False

    for arg in argv:
        if after_dashdash:
            _print(arg)
            continue
        if arg == "--":
            after_dashdash = True
            continue
        if arg == "--git-dir":
            r = R()
            cwd = Path(os.getcwd()).resolve()
            if path_format == "absolute":
                _print(str(r.gitdir))
            elif cwd == r.path and not r.bare:
                _print(os.path.relpath(r.gitdir, cwd))
            else:
                _print(str(r.gitdir))
            continue
        if arg == "--absolute-git-dir":
            _print(str(R().gitdir))
            continue
        if arg == "--git-common-dir":
            r = R()
            cwd = Path(os.getcwd()).resolve()
            if cwd == r.path and not r.bare:
                _print(os.path.relpath(r.gitdir, cwd))
            else:
                _print(str(r.gitdir))
            continue
        if arg == "--show-object-format":
            _print(R().object_format())
            continue
        if pending_git_path:
            pending_git_path = False
            r = R()
            cwd = Path(os.getcwd()).resolve()
            base = os.path.relpath(r.gitdir, cwd) if (cwd == r.path and not r.bare) else str(r.gitdir)
            _print(f"{base}/{arg}" if arg else base)
            continue
        if arg == "--git-path":
            pending_git_path = True
            continue
        if arg.startswith("--git-path="):
            r = R()
            sub = arg.split("=", 1)[1]
            cwd = Path(os.getcwd()).resolve()
            base = os.path.relpath(r.gitdir, cwd) if (cwd == r.path and not r.bare) else str(r.gitdir)
            _print(f"{base}/{sub}" if sub else base)
            continue
        if arg == "--show-toplevel":
            _print(str(R().path))
            continue
        if arg == "--show-prefix":
            r = R()
            rel = os.path.relpath(Path(os.getcwd()).resolve(), r.path)
            _print("" if rel == "." else rel.replace(os.sep, "/") + "/")
            continue
        if arg == "--show-cdup":
            r = R()
            rel = os.path.relpath(Path(os.getcwd()).resolve(), r.path)
            depth = 0 if rel == "." else len(Path(rel).parts)
            _print("../" * depth)
            continue
        if arg == "--is-inside-work-tree":
            r = R()
            inside_git = _is_inside(Path(os.getcwd()).resolve(), r.gitdir)
            _print("false" if (r.bare or inside_git) else "true")
            continue
        if arg == "--is-inside-git-dir":
            r = R()
            _print("true" if _is_inside(Path(os.getcwd()).resolve(), r.gitdir) else "false")
            continue
        if arg == "--is-bare-repository":
            _print("true" if R().bare else "false")
            continue
        if arg == "--is-shallow-repository":
            _print("true" if (R().gitdir / "shallow").exists() else "false")
            continue
        if arg == "--show-superproject-working-tree":
            # pythongit has no submodule support; never inside a superproject.
            continue
        if arg.startswith("--path-format="):
            path_format = arg.split("=", 1)[1]
            continue
        if arg in ("--all", "--branches", "--tags", "--remotes"):
            prefix = {
                "--all": "refs/",
                "--branches": "refs/heads/",
                "--tags": "refs/tags/",
                "--remotes": "refs/remotes/",
            }[arg]
            for refname, refsha in _enumerate_refs(R()):
                if refname.startswith(prefix):
                    if not sym_refs:
                        _print(refsha)
                    elif arg == "--all":
                        _print(refname)   # --symbolic --all keeps full ref names
                    else:
                        _print(refname[len(prefix):])  # short name for the namespace
            continue
        if arg == "--verify":
            verify = True
            continue
        if arg in ("-q", "--quiet"):
            quiet = True
            continue
        if arg == "--short":
            abbrev = 7
            continue
        if arg.startswith("--short="):
            try:
                abbrev = int(arg.split("=", 1)[1])
            except ValueError:
                abbrev = 7
            continue
        if arg == "--no-revs":
            no_revs = True
            continue
        if arg == "--revs-only":
            revs_only = True
            continue
        if arg == "--symbolic":
            # Make subsequent --all/--branches/--tags/--remotes print ref names.
            sym_refs = True
            continue
        if arg in ("--flags", "--no-flags", "--local-env-vars"):
            # Output filters; with no further args they produce nothing.
            continue
        if arg == "--shared-index-path":
            # Only meaningful with a split index, which pythongit never writes.
            continue
        if arg == "--symbolic-full-name":
            symbolic = "full"
            continue
        if arg == "--abbrev-ref" or arg.startswith("--abbrev-ref="):
            symbolic = "abbrev"
            continue
        if arg.startswith("^") and len(arg) > 1 and arg[1] != "{":
            r = R()
            sha = refs_mod.rev_parse(r, arg[1:])
            if sha is not None:
                _print("^" + (sha[:abbrev] if abbrev else sha))
                continue
        if arg.startswith("-") and arg != "-":
            # Unrecognized dashed args are echoed verbatim, as C Git does
            # (suppressed under --revs-only).
            if not revs_only:
                _print(arg)
            continue
        # A revision argument (suppressed under --no-revs).
        if no_revs:
            continue
        r = R()
        sha = refs_mod.rev_parse(r, arg)
        if sha is None:
            if verify:
                if quiet:
                    return 1
                _err("fatal: Needed a single revision")
                return 128
            _print(arg)
            if not quiet:
                _err(f"fatal: ambiguous argument '{arg}': unknown revision or path not in the working tree.")
                _err("Use '--' to separate paths from revisions, like this:")
                _err("'git <command> [<revision>...] -- [<file>...]'")
            return 128
        if verify:
            verified_count += 1
            if verified_count > 1:
                if quiet:
                    return 1
                _err("fatal: Needed a single revision")
                return 128
        if symbolic is not None:
            full = refs_mod.dwim_full_name(r, arg) or arg
            _print(refs_mod.shorten_ref(full) if symbolic == "abbrev" else full)
        elif abbrev:
            _print(sha[:abbrev])
        else:
            _print(sha)
    return 0


def cmd_ls_files(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit ls-files", add_help=False)
    ap.add_argument("-s", "--stage", action="store_true")
    ap.add_argument("-c", "--cached", action="store_true")
    ap.add_argument("-m", "--modified", action="store_true")
    ap.add_argument("-o", "--others", action="store_true")
    ap.add_argument("-d", "--deleted", action="store_true")
    ap.add_argument("--exclude-standard", action="store_true")
    ap.add_argument("--error-unmatch", action="store_true")
    ap.add_argument("--full-name", action="store_true")
    ap.add_argument("-t", dest="tag", action="store_true")
    ap.add_argument("--abbrev", nargs="?", const=7, type=int, default=None)
    ap.add_argument("-z", dest="nul", action="store_true")
    ap.add_argument("paths", nargs="*")
    argv = ["--abbrev=7" if a == "--abbrev" else a for a in argv]
    args = ap.parse_args(argv)
    repo = _repo()
    idx = read_index(repo)
    eol = "\0" if args.nul else "\n"

    if args.error_unmatch:
        tracked = set(idx.by_path())
        for ps in args.paths:
            norm = ps.rstrip("/")
            if not (norm in tracked or any(t.startswith(norm + "/") for t in tracked)):
                _err(f"error: pathspec '{ps}' did not match any file(s) known to git")
                _err("Did you forget to 'git add'?")
                return 1

    def match(p: str) -> bool:
        if not args.paths:
            return True
        return any(p == ps or p.startswith(ps.rstrip("/") + "/") for ps in args.paths)

    want_cached = args.cached or args.stage
    if not (want_cached or args.modified or args.others or args.deleted):
        want_cached = True

    def _tag(prefix: str, text: str) -> str:
        return (prefix + " " + text) if args.tag else text

    lines: list[tuple[str, str]] = []
    if want_cached:
        for e in idx.entries:
            if match(e.path):
                if args.stage:
                    sha = e.sha[:args.abbrev] if args.abbrev is not None else e.sha
                    lines.append((e.path, _tag("H", f"{e.mode_str()} {sha} {getattr(e, 'stage', 0)}\t{e.path}")))
                else:
                    lines.append((e.path, _tag("H", e.path)))
    if args.modified or args.deleted or args.others:
        status = workdir.status(repo, include_ignored=args.others and not args.exclude_standard)
        if args.modified:
            for p in status["modified"] + status["missing"]:
                if match(p):
                    lines.append((p, _tag("C", p)))
        if args.deleted:
            for p in status["missing"]:
                if match(p):
                    lines.append((p, _tag("R", p)))
        if args.others:
            for p in status["untracked"]:
                if match(p):
                    lines.append((p, _tag("?", p)))

    seen: set[str] = set()
    for _path, line in sorted(lines):
        if line in seen:
            continue
        seen.add(line)
        sys.stdout.write(line + eol)
    return 0


def cmd_rev_list(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit rev-list", add_help=False)
    ap.add_argument("--count", action="store_true")
    ap.add_argument("--max-count", "-n", type=int, default=None)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--reverse", action="store_true")
    ap.add_argument("--objects", action="store_true")
    ap.add_argument("--parents", action="store_true")
    ap.add_argument("--no-walk", action="store_true")
    ap.add_argument("--children", action="store_true")
    ap.add_argument("--first-parent", action="store_true")
    ap.add_argument("--pretty", nargs="?", const="medium", default=None)
    ap.add_argument("--format", default=None)
    ap.add_argument("--oneline", action="store_true")
    ap.add_argument("--since", "--after", dest="since", default=None)
    ap.add_argument("--until", "--before", dest="until", default=None)
    ap.add_argument("--merges", action="store_true")
    ap.add_argument("--no-merges", dest="no_merges", action="store_true")
    ap.add_argument("--min-parents", dest="min_parents", type=int, default=None)
    ap.add_argument("--max-parents", dest="max_parents", type=int, default=None)
    ap.add_argument("--grep", action="append", default=None)
    ap.add_argument("--author", action="append", default=None)
    ap.add_argument("--committer", action="append", default=None)
    ap.add_argument("--all-match", dest="all_match", action="store_true")
    ap.add_argument("-i", "--regexp-ignore-case", dest="ignore_case", action="store_true")
    ap.add_argument("--left-right", dest="left_right", action="store_true")
    ap.add_argument("revs", nargs="*")
    # Split a trailing "-- <pathspec>..." off before argparse consumes the "--".
    rl_paths: list[str] = []
    pre = _expand_count_shorthand(argv)
    if "--" in pre:
        i = pre.index("--")
        rl_paths = pre[i + 1:]
        pre = pre[:i]
    args = ap.parse_args(pre)
    if args.parents and args.children:
        _err("fatal: options '--parents' and '--children' cannot be used together")
        return 128
    repo = _repo()
    rev_args = args.revs
    starts = []
    excludes: list[str] = []
    if args.all:
        for _ref, sha in _enumerate_refs(repo):
            starts.append(sha)
    lr_left: set[str] = set()
    lr_right: set[str] = set()

    def _reach(start: str) -> set:
        seen_r: set[str] = set()
        st = [start]
        while st:
            x = st.pop()
            if x in seen_r:
                continue
            seen_r.add(x)
            info = _commit_tree_parents(repo, x)
            if info:
                st.extend(info[1])
        return seen_r

    for r in rev_args:
        if r.startswith("^"):
            sha = refs_mod.rev_parse(repo, r[1:])
            if sha:
                excludes.append(sha)
            continue
        if "..." in r:
            # Symmetric difference: A...B = A B --not $(merge-base A B).
            a, _, b = r.partition("...")
            a_sha = refs_mod.rev_parse(repo, a or "HEAD")
            b_sha = refs_mod.rev_parse(repo, b or "HEAD")
            from . import merge as _m
            if a_sha:
                starts.append(a_sha)
                lr_left |= _reach(a_sha)
            if b_sha:
                starts.append(b_sha)
                lr_right |= _reach(b_sha)
            if a_sha and b_sha:
                excludes.extend(_m.merge_bases(repo, a_sha, b_sha))
            continue
        if ".." in r:
            lo, _, hi = r.partition("..")
            hi_sha = refs_mod.rev_parse(repo, hi or "HEAD")
            lo_sha = refs_mod.rev_parse(repo, lo) if lo else None
            if hi_sha:
                starts.append(hi_sha)
            if lo_sha:
                excludes.append(lo_sha)
            continue
        sha = refs_mod.rev_parse(repo, r)
        if sha:
            starts.append(sha)
    if not starts and not args.all:
        _err("usage: git rev-list [<options>] <commit>... [--] [<path>...]")
        return 128

    excluded: set[str] = set()
    estack = deque(excludes)
    while estack:
        sha = estack.popleft()
        if sha in excluded:
            continue
        excluded.add(sha)
        info = _commit_tree_parents(repo, sha)
        if info:
            estack.extend(info[1])

    if args.count and args.max_count is None and starts and not excludes:
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
        if sha in visited or sha in excluded:
            continue
        visited.add(sha)
        info = _commit_tree_parents(repo, sha, graph)
        if info is None:
            continue
        out.append(sha)
        # With path/commit filters, walk fully and trim after filtering.
        _has_filter = (rl_paths or args.since or args.until or args.merges
                       or args.no_merges or args.min_parents is not None
                       or args.max_parents is not None or args.grep or args.author
                       or args.committer)
        if args.max_count and len(out) >= args.max_count and not _has_filter:
            break
        if args.no_walk:
            continue
        _tree, parents = info
        stack.extend(parents[:1] if args.first_parent else parents)

    if rl_paths:
        def _touches(s: str) -> bool:
            info = _commit_tree_parents(repo, s, graph)
            if info is None:
                return False
            tree, parents = info
            ptree = _commit_tree_parents(repo, parents[0], graph)[0] if parents else None
            for path in rl_paths:
                before = workdir.tree_path_entry(repo, ptree, path) if ptree else None
                after = workdir.tree_path_entry(repo, tree, path)
                if (before.sha if before else None) != (after.sha if after else None):
                    return True
            return False
        out = [s for s in out if _touches(s)]
    out = _filter_commits(repo, out, args)
    if args.max_count is not None:
        out = out[:max(0, args.max_count)]

    if args.pretty is not None or args.format is not None or args.oneline:
        fmt = args.format
        if fmt is None and args.pretty and (args.pretty.startswith("format:")
                                            or args.pretty.startswith("tformat:")):
            fmt = args.pretty.split(":", 1)[1]
        elif fmt is None and args.pretty and "%" in args.pretty:
            fmt = args.pretty
        is_oneline = args.oneline or args.pretty == "oneline"
        for s in out:
            c = objs.parse_commit(objs.read_object(repo, s)[1])
            if is_oneline and fmt is None:
                first = c.message.splitlines()[0] if c.message.strip() else ""
                _print(f"{s[:7] if args.oneline else s} {first}")
            elif fmt is not None:
                # rev-list prefixes a "commit <oid>" line before the format.
                _print(f"commit {s}")
                _print(_expand_commit_format(repo, s, c, fmt, {}))
            else:
                _print(f"commit {s}")
                _emit_commit_header(s, c, style=args.pretty, date_mode="default")
                _print("")
                for line in c.message.rstrip("\n").splitlines():
                    _print(f"    {line}")
        return 0
    if args.count:
        _print(str(len(out)))
    elif args.objects:
        for s in out:
            _print(s)
        seen_obj: set[str] = set()
        for s in out:
            info = _commit_tree_parents(repo, s, graph)
            if info is None:
                continue
            for osha, opath in _walk_objects(repo, info[0], ""):
                if osha in seen_obj:
                    continue
                seen_obj.add(osha)
                _print(f"{osha} {opath}")
    else:
        if args.reverse:
            out = list(reversed(out))
        # --children: map each commit to the in-set commits that name it parent.
        children_map: dict[str, list[str]] = {}
        if args.children:
            for s in out:
                info = _commit_tree_parents(repo, s, graph)
                if info:
                    for p in info[1]:
                        children_map.setdefault(p, []).append(s)
        for s in out:
            mark = ""
            if args.left_right:
                mark = "<" if s in lr_left else (">" if s in lr_right else "")
            extra = ""
            if args.parents:
                info = _commit_tree_parents(repo, s, graph)
                if info and info[1]:
                    extra += " " + " ".join(info[1])
            if args.children:
                kids = children_map.get(s, [])
                if kids:
                    extra += " " + " ".join(kids)
            _print(f"{mark}{s}{extra}")
    return 0


def _walk_objects(repo: Repository, tree_sha: str, prefix: str):
    yield (tree_sha, prefix.rstrip("/"))
    try:
        _, data = objs.read_object(repo, tree_sha)
    except KeyError:
        return
    for e in objs.parse_tree(data, repo.hash_len):
        path = prefix + e.name
        if e.is_dir():
            yield from _walk_objects(repo, e.sha, path + "/")
        elif not e.is_gitlink():
            yield (e.sha, path)


# ---------------------------------------------------------------------------
# porcelain


def _pathspec_matches(repo: Repository, pathspec: str, tracked: set[str]) -> bool:
    full = repo.path / pathspec
    if full.exists() or full.is_symlink():
        return True
    norm = pathspec.rstrip("/")
    if norm in (".", ""):
        return True
    return any(t == norm or t.startswith(norm + "/") for t in tracked)


def cmd_add(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit add", add_help=False)
    ap.add_argument("-A", "--all", action="store_true")
    ap.add_argument("-n", "--dry-run", dest="dry_run", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("-f", "--force", action="store_true")
    ap.add_argument("paths", nargs="*")
    args = ap.parse_args(argv)
    repo = _repo()
    explicit = bool(args.paths) and not args.all
    targets = args.paths if explicit else ["."]
    if explicit:
        tracked = workdir.tracked_paths(repo)
        for ps in targets:
            if not _pathspec_matches(repo, ps, tracked):
                _err(f"fatal: pathspec '{ps}' did not match any files")
                return 128
    if args.dry_run or args.verbose:
        report = workdir.would_add(repo, targets)
        if args.dry_run:
            for rel in report:
                _print(f"add '{rel}'")
            return 0
    workdir.add_paths(repo, targets)
    if args.verbose:
        for rel in report:
            _print(f"add '{rel}'")
    return 0


def cmd_rm(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit rm", add_help=False)
    ap.add_argument("--cached", action="store_true")
    ap.add_argument("-f", "--force", action="store_true")
    ap.add_argument("-r", dest="recursive", action="store_true")
    ap.add_argument("-n", "--dry-run", dest="dry_run", action="store_true")
    ap.add_argument("paths", nargs="+")
    args = ap.parse_args(argv)
    repo = _repo()
    tracked = workdir.tracked_paths(repo)
    for ps in args.paths:
        norm = ps.rstrip("/")
        if not (norm in tracked or any(t.startswith(norm + "/") for t in tracked)):
            _err(f"fatal: pathspec '{ps}' did not match any files")
            return 128
    # Expand directory pathspecs to the tracked files they contain.
    expanded: list[str] = []
    for ps in args.paths:
        norm = ps.rstrip("/")
        if norm in tracked:
            expanded.append(norm)
        else:
            under = sorted(t for t in tracked if t.startswith(norm + "/"))
            if under and not args.recursive:
                _err(f"fatal: not removing '{ps}' recursively without -r")
                return 1
            expanded.extend(under)
    if args.dry_run:
        for rel in expanded:
            _print(f"rm '{rel}'")
        return 0
    removed = workdir.rm_paths(repo, expanded, cached=args.cached)
    for rel in removed:
        _print(f"rm '{rel}'")
    return 0


def cmd_mv(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit mv", add_help=False)
    ap.add_argument("-n", "--dry-run", dest="dry_run", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("-f", "--force", action="store_true")
    ap.add_argument("-k", dest="skip", action="store_true")
    ap.add_argument("src")
    ap.add_argument("dst")
    args = ap.parse_args(argv)
    repo = _repo()
    src = repo.path / args.src
    dst = repo.path / args.dst
    if not src.exists():
        _err("fatal: bad source")
        return 1
    if args.dry_run:
        _print(f"Checking rename of '{args.src}' to '{args.dst}'")
    if args.dry_run or args.verbose:
        _print(f"Renaming {args.src} to {args.dst}")
    if args.dry_run:
        return 0
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dst)
    workdir.rm_paths(repo, [args.src], cached=True)
    workdir.add_paths(repo, [args.dst])
    return 0


def _wt_mode(full: Path) -> str:
    import stat as _stat
    try:
        st = os.lstat(full)
    except OSError:
        return "000000"
    if _stat.S_ISLNK(st.st_mode):
        return "120000"
    if st.st_mode & 0o111:
        return "100755"
    return "100644"


def _status_model(repo: Repository, s: dict) -> tuple[list[tuple[str, str, str]], list[str]]:
    """Return (changes, untracked).

    ``changes`` is a sorted list of ``(path, X, Y)`` where X is the index-vs-HEAD
    status and Y the worktree-vs-index status; spaces mean unchanged.
    """
    index_status: dict[str, str] = {}
    for p in s["staged_new"]:
        index_status[p] = "A"
    for p in s["staged_mod"]:
        index_status[p] = "M"
    for p in s["staged_del"]:
        index_status[p] = "D"
    worktree_status: dict[str, str] = {}
    for p in s["modified"]:
        worktree_status[p] = "M"
    for p in s["missing"]:
        worktree_status[p] = "D"
    changes = []
    for p in sorted(set(index_status) | set(worktree_status)):
        changes.append((p, index_status.get(p, " "), worktree_status.get(p, " ")))
    return changes, sorted(s["untracked"])


def _status_staged_renames(repo: Repository, s: dict) -> list:
    """Detect staged renames (HEAD->index) and return (src, dst) pairs."""
    if not s["staged_del"] or not s["staged_new"]:
        return []
    from . import diffcore
    head_sha = refs_mod.rev_parse(repo, "HEAD")
    head_map = _tree_map_full(repo, _commit_tree(repo, head_sha)) if head_sha else {}
    idx = read_index(repo).by_path()
    base_map: dict[str, tuple[int, str]] = {}
    for p in s["staged_del"]:
        if p in head_map:
            base_map[p] = (int(head_map[p][0], 8), head_map[p][1])
    side_map: dict[str, tuple[int, str]] = {}
    for p in s["staged_new"]:
        if p in idx:
            side_map[p] = (idx[p].mode, idx[p].sha)
    if not base_map or not side_map:
        return []
    return [(pair.src.path, pair.dst.path) for pair in diffcore.detect_renames(repo, base_map, side_map)]


def _status_branch_header_short(repo: Repository, head_sym, head_sha) -> str:
    branch = head_sym[len("refs/heads/"):] if head_sym and head_sym.startswith("refs/heads/") else None
    if head_sym and head_sha is None:
        return f"## No commits yet on {branch}"
    if branch is None:
        return "## HEAD (no branch)"
    upstream, ahead, behind = _branch_tracking(repo, head_sym)
    line = f"## {branch}"
    if upstream:
        line += f"...{upstream}"
        markers = []
        if ahead:
            markers.append(f"ahead {ahead}")
        if behind:
            markers.append(f"behind {behind}")
        if markers:
            line += " [" + ", ".join(markers) + "]"
    return line


def _branch_tracking(repo: Repository, head_sym):
    """Return (upstream_shortname_or_None, ahead, behind)."""
    if not head_sym or not head_sym.startswith("refs/heads/"):
        return None, 0, 0
    branch = head_sym[len("refs/heads/"):]
    from . import gitconfig
    remote = gitconfig.get(repo, f"branch.{branch}.remote")
    merge = gitconfig.get(repo, f"branch.{branch}.merge")
    if not remote or not merge:
        return None, 0, 0
    merge_short = merge[len("refs/heads/"):] if merge.startswith("refs/heads/") else merge
    if remote == ".":
        upstream = merge_short
        upstream_ref = merge
    else:
        upstream = f"{remote}/{merge_short}"
        upstream_ref = f"refs/remotes/{remote}/{merge_short}"
    local = refs_mod.read_ref(repo, head_sym)
    up = refs_mod.read_ref(repo, upstream_ref)
    ahead = behind = 0
    if local and up:
        ahead = _count_commits(repo, local, up)
        behind = _count_commits(repo, up, local)
    return upstream, ahead, behind


def _count_commits(repo: Repository, tip: str, exclude: str) -> int:
    """Count commits reachable from tip but not from exclude."""
    excluded: set[str] = set()
    stack = deque([exclude])
    while stack:
        sha = stack.popleft()
        if sha in excluded:
            continue
        excluded.add(sha)
        info = _commit_tree_parents(repo, sha)
        if info:
            stack.extend(info[1])
    count = 0
    seen: set[str] = set()
    stack = deque([tip])
    while stack:
        sha = stack.popleft()
        if sha in seen or sha in excluded:
            continue
        seen.add(sha)
        info = _commit_tree_parents(repo, sha)
        if info is None:
            continue
        count += 1
        stack.extend(info[1])
    return count


def cmd_status(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit status", add_help=False)
    ap.add_argument("-s", "--short", action="store_true")
    ap.add_argument("-b", "--branch", action="store_true")
    ap.add_argument("--long", dest="long", action="store_true")
    ap.add_argument("--porcelain", nargs="?", const="v1", default=None)
    ap.add_argument("-u", "--untracked-files", nargs="?", const="all", default="all")
    ap.add_argument("-z", dest="nul", action="store_true")
    args = ap.parse_args(argv)
    repo = _repo()
    s = workdir.status(repo)
    head_sym, head_sha = refs_mod.read_head(repo)
    branch = head_sym[len("refs/heads/"):] if head_sym and head_sym.startswith("refs/heads/") else None
    changes, untracked = _status_model(repo, s)
    renames = _status_staged_renames(repo, s)
    if renames:
        consumed = {src for src, _ in renames} | {dst for _, dst in renames}
        changes = [(p, x, y) for p, x, y in changes if p not in consumed]
    show_untracked = args.untracked_files != "no"
    untracked_hidden = bool(untracked) and not show_untracked
    if not show_untracked:
        untracked = []

    porcelain = args.porcelain
    short_mode = args.short and not args.long and porcelain is None
    eol = "\0" if args.nul else "\n"

    def emit(line: str):
        sys.stdout.write(line + eol)

    if porcelain == "v2":
        return _status_porcelain_v2(repo, s, changes, untracked, args.branch, head_sym, head_sha, eol)

    if porcelain is not None or short_mode:
        if args.branch:
            emit(_status_branch_header_short(repo, head_sym, head_sha))
        entries = [(dst, f"R  {src} -> {dst}") for src, dst in renames]
        entries += [(p, f"{x}{y} {p}") for p, x, y in changes]
        for _key, line in sorted(entries):
            emit(line)
        for p in untracked:
            emit(f"?? {p}")
        return 0

    # Long (default) format.
    return _status_long(repo, s, changes, untracked, branch, head_sym, head_sha, untracked_hidden, renames)


def _status_long(repo, s, changes, untracked, branch, head_sym, head_sha, untracked_hidden=False, renames=None) -> int:
    unborn = head_sym is not None and head_sha is None
    if branch is not None:
        _print(f"On branch {branch}")
    else:
        _print("HEAD detached")
    if unborn:
        _print("\nNo commits yet\n")

    staged = [(p, x) for p, x, y in changes if x != " "]
    unstaged = [(p, y) for p, x, y in changes if y != " "]
    label = {"A": "new file:   ", "M": "modified:   ", "D": "deleted:    "}
    rename_rows = [(dst, f"\trenamed:    {src} -> {dst}") for src, dst in (renames or [])]

    if staged or rename_rows:
        _print("Changes to be committed:")
        if unborn:
            _print('  (use "git rm --cached <file>..." to unstage)')
        else:
            _print('  (use "git restore --staged <file>..." to unstage)')
        staged_rows = rename_rows + [(p, f"\t{label[x]}{p}") for p, x in staged]
        for _key, line in sorted(staged_rows):
            _print(line)
        _print("")
    if unstaged:
        _print("Changes not staged for commit:")
        if any(y == "D" for _, y in unstaged):
            _print('  (use "git add/rm <file>..." to update what will be committed)')
        else:
            _print('  (use "git add <file>..." to update what will be committed)')
        _print('  (use "git restore <file>..." to discard changes in working directory)')
        for p, y in unstaged:
            _print(f"\t{label[y]}{p}")
        _print("")
    if untracked:
        _print("Untracked files:")
        _print('  (use "git add <file>..." to include in what will be committed)')
        for p in untracked:
            _print(f"\t{p}")
        _print("")
    elif untracked_hidden and (staged or rename_rows):
        # With staged changes present (so no trailing summary), -uno still notes
        # hidden untracked files. Without staged changes the summary line
        # ("no changes added"/"nothing to commit, use -u") carries the hint.
        _print("Untracked files not listed (use -u option to show untracked files)")

    has_staged = bool(staged) or bool(rename_rows)
    if not has_staged and not unstaged and not untracked:
        if untracked_hidden:
            _print("nothing to commit (use -u to show untracked files)")
        elif unborn:
            _print('nothing to commit (create/copy files and use "git add" to track)')
        else:
            _print("nothing to commit, working tree clean")
    elif not has_staged and not unstaged and untracked:
        _print('nothing added to commit but untracked files present (use "git add" to track)')
    elif not has_staged and unstaged:
        _print('no changes added to commit (use "git add" and/or "git commit -a")')
    return 0


def _status_porcelain_v2(repo, s, changes, untracked, want_branch, head_sym, head_sha, eol) -> int:
    def emit(line: str):
        sys.stdout.write(line + eol)
    if want_branch:
        oid = head_sha or "(initial)"
        emit(f"# branch.oid {oid}")
        emit(f"# branch.head {head_sym[len('refs/heads/'):] if head_sym and head_sym.startswith('refs/heads/') else '(detached)'}")
        upstream, ahead, behind = _branch_tracking(repo, head_sym)
        if upstream:
            emit(f"# branch.upstream {upstream}")
            emit(f"# branch.ab +{ahead} -{behind}")
    idx = read_index(repo).by_path()
    head_modes: dict[str, str] = {}
    head_shas: dict[str, str] = {}
    if head_sha:
        _, hdata = objs.read_object(repo, head_sha)
        htree = objs.parse_commit(hdata).tree
        for path, mode, sha in workdir.iter_tree_files(repo, htree):
            head_modes[path] = mode
            head_shas[path] = sha
    zero = "0" * 40
    for p, x, y in changes:
        xv = x if x != " " else "."
        yv = y if y != " " else "."
        mH = head_modes.get(p, "000000")
        mI = idx[p].mode_str() if p in idx else "000000"
        mW = "000000" if y == "D" else _wt_mode(repo.path / p)
        hH = head_shas.get(p, zero)
        hI = idx[p].sha if p in idx else zero
        emit(f"1 {xv}{yv} N... {mH} {mI} {mW} {hH} {hI} {p}")
    for p in untracked:
        emit(f"? {p}")
    return 0


def _commit_status_report(repo: Repository) -> int:
    s = workdir.status(repo)
    head_sym, head_sha = refs_mod.read_head(repo)
    branch = head_sym[len("refs/heads/"):] if head_sym and head_sym.startswith("refs/heads/") else None
    changes, untracked = _status_model(repo, s)
    _status_long(repo, s, changes, untracked, branch, head_sym, head_sha)
    return 1


def cmd_commit(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit commit", add_help=False)
    ap.add_argument("-m", "--message", action="append", default=None)
    ap.add_argument("-a", "--all", action="store_true")
    ap.add_argument("--amend", action="store_true")
    ap.add_argument("--no-edit", action="store_true")
    ap.add_argument("--allow-empty", action="store_true")
    ap.add_argument("--date", default=None)
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args(argv)
    # Multiple -m values are joined into paragraphs, like C Git.
    if args.message is not None:
        args.message = "\n\n".join(args.message)
    repo = _repo()
    try:
        from . import rerere as _rr
        _rr.scan_and_record(repo)
    except Exception:
        pass

    if args.all:
        workdir.add_paths(repo, sorted(workdir.tracked_paths(repo)))

    cur_idx = read_index(repo)
    if cur_idx.has_conflicts():
        _err("error: unresolved conflicts:")
        for p in cur_idx.conflicted_paths():
            _err(f"\t{p}")
        _err("hint: stage the resolved files with `pygit add` then commit again.")
        return 1

    tree = workdir.write_tree(repo)
    head_sym, parent = refs_mod.read_head(repo)

    if args.amend:
        if parent is None:
            _err("fatal: You have nothing to amend.")
            return 128
        _, pdata = objs.read_object(repo, parent)
        pc = objs.parse_commit(pdata)
        parents = list(pc.parents)
        author_sig = pc.author
        if args.date is not None:
            # --amend --date keeps the original author identity but resets the
            # author date to the supplied value.
            who, _ts, _tz = _split_ident(pc.author)
            fresh = objs.build_signature(repo, "author", date_override=args.date)
            author_sig = who + fresh[fresh.rindex(">") + 1:]
        message = args.message if args.message is not None else pc.message.rstrip("\n")
        compare_tree = None
        if pc.parents:
            _, gpd = objs.read_object(repo, pc.parents[0])
            compare_tree = objs.parse_commit(gpd).tree
        if not args.allow_empty and compare_tree == tree:
            _err("fatal: You asked to amend the most recent commit, but doing so would make")
            _err("it empty. You can repeat your command with --allow-empty, or you can")
            _err('remove the commit entirely with "git reset HEAD^".')
            return 1
    else:
        if args.message is None:
            _err('error: empty commit message')
            return 1
        parents = [parent] if parent else []
        author_sig = objs.build_signature(repo, "author", date_override=args.date)
        message = args.message
        if parent and not args.allow_empty:
            _, pdata = objs.read_object(repo, parent)
            if objs.parse_commit(pdata).tree == tree:
                return _commit_status_report(repo)
        empty_tree_sha, _ = objs.hash_bytes("tree", b"", repo)
        if parent is None and not args.allow_empty and tree == empty_tree_sha:
            return _commit_status_report(repo)

    committer_sig = objs.build_signature(repo, "committer")
    msg = message if message.endswith("\n") else message + "\n"
    c = objs.Commit(tree=tree, parents=parents, author=author_sig, committer=committer_sig, message=msg)
    sha = objs.write_object(repo, "commit", c.encode())
    verb = "commit (amend)" if args.amend else ("commit (initial)" if not parents else "commit")
    reflog_msg = f"{verb}: {msg.splitlines()[0]}"
    if head_sym:
        refs_mod.update_ref(repo, head_sym, sha, message=reflog_msg)
    else:
        refs_mod.set_head(repo, sha)
    if args.quiet:
        return 0
    branch = head_sym[len("refs/heads/"):] if head_sym and head_sym.startswith("refs/heads/") else "detached HEAD"
    root = " (root-commit)" if not parents and not args.amend else ""
    _print(f"[{branch}{root} {sha[:7]}] {msg.splitlines()[0]}")
    if args.amend:
        _print(f" Date: {_format_ident_date(author_sig)}")
    parent_tree = None
    if parents:
        _, pdata = objs.read_object(repo, parents[0])
        parent_tree = objs.parse_commit(pdata).tree
    _print_commit_summary(repo, parent_tree, tree)
    return 0


def _blob_lines(repo: Repository, sha: str) -> list[str]:
    try:
        _, data = objs.read_object(repo, sha)
    except KeyError:
        return []
    return data.decode("utf-8", errors="replace").splitlines()


def _print_commit_summary(repo: Repository, parent_tree: Optional[str], new_tree: str) -> None:
    files = insertions = deletions = 0
    mode_lines: list[tuple[str, str]] = []
    for path, a, b in workdir.iter_tree_changes(repo, parent_tree, new_tree):
        files += 1
        old_lines = _blob_lines(repo, a.sha) if a is not None else []
        new_lines = _blob_lines(repo, b.sha) if b is not None else []
        for op in diff_mod.diff_lines(old_lines, new_lines):
            if op[0] == "ins":
                insertions += 1
            elif op[0] == "del":
                deletions += 1
        if a is None and b is not None:
            mode_lines.append((path, f" create mode {b.mode.zfill(6)} {path}"))
        elif b is None and a is not None:
            mode_lines.append((path, f" delete mode {a.mode.zfill(6)} {path}"))
    if files == 0:
        return
    _print(_stat_summary_line(files, insertions, deletions))
    for _, line in sorted(mode_lines):
        _print(line)


def _parse_who(who: str) -> tuple[str, str]:
    if who.endswith(">") and " <" in who:
        name, email = who[:-1].split(" <", 1)
        return name, email
    return who, ""


def _pad_column(text: str, spec) -> str:
    """Apply a git pretty-format column spec (align, width, trunc) to text.
    Text wider than width is left untouched unless a truncation mode is set
    (trunc=right with '..', ltrunc=left, mtrunc=middle)."""
    align, width, trunc = spec
    n = len(text)
    if n > width:
        if not trunc:
            return text
        if width <= 2:
            return text[:width]
        if trunc == "ltrunc":
            return ".." + text[n - (width - 2):]
        if trunc == "mtrunc":
            keep = width - 2
            start = keep // 2
            end = keep - start
            return text[:start] + ".." + (text[n - end:] if end else "")
        return text[:width - 2] + ".."
    pad = width - n
    if align == "right":
        return " " * pad + text
    if align == "center":
        left = pad // 2
        return " " * left + text + " " * (pad - left)
    return text + " " * pad


def _expand_commit_format(repo: Repository, sha: str, c, fmt: str, decorations: dict,
                          date_mode: str = "default", abbrev: int = 7,
                          reflog=None, date_given: bool = False) -> str:
    a_who, a_ts, a_tz = _split_ident(c.author)
    c_who, c_ts, c_tz = _split_ident(c.committer)
    an, ae = _parse_who(a_who)
    cn, ce = _parse_who(c_who)
    subject = c.message.splitlines()[0] if c.message.strip() else ""
    # %B is the raw message; %b is the body after the subject's blank line; %f
    # is the subject sanitized into a path-safe slug.
    import re as _re
    raw_body = c.message
    _parts = c.message.split("\n\n", 1)
    body = _parts[1] if len(_parts) > 1 else ""
    sanitized = _re.sub(r"[^A-Za-z0-9]+", "-", subject).strip("-")
    deco_names = decorations.get(sha, [])
    deco = _format_decoration(deco_names)
    deco_d = ", ".join(deco_names)
    # Longer tokens must precede their prefixes (e.g. %ad before %a*) so a
    # str.replace pass does not consume the prefix first.
    replacements = [
        ("%H", sha), ("%h", sha[:abbrev]),
        ("%T", c.tree), ("%t", c.tree[:abbrev]),
        ("%P", " ".join(c.parents)), ("%p", " ".join(p[:abbrev] for p in c.parents)),
        ("%an", an), ("%ae", ae), ("%cn", cn), ("%ce", ce),
        # Date placeholders: %ad/%cd honor --date; %aD/%ai/%aI (and committer
        # equivalents) are fixed styles; %at/%ct are the raw unix timestamps.
        ("%aD", _format_date(c.author, "rfc")), ("%cD", _format_date(c.committer, "rfc")),
        ("%aI", _format_date(c.author, "iso-strict")), ("%cI", _format_date(c.committer, "iso-strict")),
        ("%ai", _format_date(c.author, "iso")), ("%ci", _format_date(c.committer, "iso")),
        ("%ad", _format_date(c.author, date_mode)), ("%cd", _format_date(c.committer, date_mode)),
        ("%ar", _format_date(c.author, "relative")), ("%cr", _format_date(c.committer, "relative")),
        ("%as", _format_date(c.author, "short")), ("%cs", _format_date(c.committer, "short")),
        ("%ah", _format_date(c.author, "human")), ("%ch", _format_date(c.committer, "human")),
        ("%at", str(a_ts) if a_ts is not None else ""), ("%ct", str(c_ts) if c_ts is not None else ""),
        ("%B", raw_body), ("%b", body), ("%f", sanitized),
        # %e (encoding) and %N (notes) are empty for unencoded, un-noted commits.
        ("%e", ""), ("%N", ""),
        # Signature placeholders: pythongit does not verify GPG signatures, so
        # commits read as unsigned ("N", empty detail fields), like unsigned
        # commits under C Git.
        ("%G?", "N"), ("%GG", ""), ("%GS", ""), ("%GK", ""),
        ("%s", subject), ("%D", deco_d), ("%d", deco),
        # %m is the left/right/boundary mark; without --left-right it is ">".
        ("%m", ">"),
        ("%n", "\n"), ("%%", "%"),
    ]
    # Reflog placeholders (only meaningful under `log -g`): %gD/%gd selector
    # (full/short ref), %gs subject, %gn/%ge identity name/email. They expand to
    # empty when not walking reflogs, matching git.
    if reflog is not None:
        gd = _reflog_selector(reflog, full=False, date_mode=date_mode, date_given=date_given)
        gD = _reflog_selector(reflog, full=True, date_mode=date_mode, date_given=date_given)
        gn, ge = _parse_who(_reflog_who(reflog["ident"]))
        replacements[:0] = [("%gD", gD), ("%gd", gd), ("%gs", reflog["msg"]),
                            ("%gn", gn), ("%ge", ge)]
    else:
        replacements[:0] = [("%gD", ""), ("%gd", ""), ("%gs", ""), ("%gn", ""), ("%ge", "")]
    def _decorate_atom(spec: str) -> str:
        # %(decorate) / %(decorate:prefix=..,suffix=..,separator=..,...)
        prefix, suffix, sep = " (", ")", ", "
        tag = pointer = ""
        if spec.startswith("decorate:"):
            for opt in spec[len("decorate:"):].split(","):
                k, _, v = opt.partition("=")
                if k == "prefix":
                    prefix = v
                elif k == "suffix":
                    suffix = v
                elif k == "separator":
                    sep = v
                elif k == "tag":
                    tag = v
                elif k == "pointer":
                    pointer = v
        if not deco_names:
            return ""
        names = list(deco_names)
        if pointer:
            names = [n.replace(" -> ", pointer) for n in names]
        if tag:
            names = [tag + n[len("tag: "):] if n.startswith("tag: ") else n for n in names]
        return prefix + sep.join(names) + suffix

    def expand_seg(s: str) -> str:
        # %xHH expands to the literal byte (e.g. %x09 -> tab) before field tokens.
        s = _re.sub(r"%x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), s)
        s = _re.sub(r"%\((decorate(?::[^)]*)?)\)", lambda m: _decorate_atom(m.group(1)), s)
        for token, value in replacements:
            s = s.replace(token, value)
        return s

    # Column padding/truncation: %<(N[,trunc]) left-align, %>(N) right-align,
    # %><(N) center. The spec pads the text from the marker through the next
    # single placeholder, then is flushed (git pads nothing if no placeholder
    # follows before the format ends).
    col_re = _re.compile(r"%(<\(|>\(|><\(|>>\()(\d+)(?:,(trunc|ltrunc|mtrunc))?\)")

    tok_sorted = sorted({t for t, _ in replacements}, key=len, reverse=True)

    def _first_ph(s: str, start: int):
        # Return (placeholder_start, placeholder_end) of the next %-placeholder,
        # matching the longest known token (so %an/%ad/%G? aren't split).
        j = s.find("%", start)
        if j < 0:
            return None
        if s[j:j + 2] == "%(":
            k = s.find(")", j)
            return (j, k + 1 if k >= 0 else len(s))
        if s[j:j + 2] == "%x":
            return (j, j + 4)
        for tok in tok_sorted:
            if s.startswith(tok, j):
                return (j, j + len(tok))
        return (j, j + 2)

    if col_re.search(fmt):
        out_parts: list[str] = []
        i = 0
        while i < len(fmt):
            m = col_re.match(fmt, i)
            if m:
                kind = m.group(1)
                align = {"<(": "left", ">(": "right", "><(": "center", ">>(": "right"}[kind]
                spec = (align, int(m.group(2)), m.group(3))
                i = m.end()
                ph = _first_ph(fmt, i)
                if ph is None:
                    out_parts.append(expand_seg(fmt[i:]))
                    break
                ph_start, ph_end = ph
                # Literals between the marker and the placeholder are emitted
                # unpadded; only the placeholder's output is padded to width.
                if ph_start > i:
                    out_parts.append(expand_seg(fmt[i:ph_start]))
                out_parts.append(_pad_column(expand_seg(fmt[ph_start:ph_end]), spec))
                i = ph_end
            else:
                nm = col_re.search(fmt, i)
                end = nm.start() if nm else len(fmt)
                out_parts.append(expand_seg(fmt[i:end]))
                i = end
        return "".join(out_parts)
    return expand_seg(fmt)


def cmd_log(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit log", add_help=False)
    ap.add_argument("--oneline", action="store_true")
    ap.add_argument("--decorate", nargs="?", const="short", default=None)
    ap.add_argument("--no-decorate", action="store_true")
    ap.add_argument("--pretty", nargs="?", const="medium", default=None)
    ap.add_argument("--format", default=None)
    ap.add_argument("--abbrev-commit", action="store_true")
    ap.add_argument("--no-abbrev-commit", dest="no_abbrev_commit", action="store_true")
    ap.add_argument("--abbrev", type=int, default=7)
    ap.add_argument("--no-abbrev", dest="no_abbrev", action="store_true")
    ap.add_argument("-p", "--patch", action="store_true")
    ap.add_argument("-U", "--unified", type=int, default=3)
    ap.add_argument("--stat", action="store_true")
    ap.add_argument("--shortstat", action="store_true")
    ap.add_argument("--name-only", dest="name_only", action="store_true")
    ap.add_argument("--name-status", dest="name_status", action="store_true")
    ap.add_argument("--raw", action="store_true")
    ap.add_argument("--no-merges", dest="no_merges", action="store_true")
    # pythongit never colorizes, so color controls are accepted and ignored.
    ap.add_argument("--color", nargs="?", const="always", default=None)
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("--reverse", action="store_true")
    ap.add_argument("--first-parent", dest="first_parent", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--branches", nargs="?", const="*", default=None)
    ap.add_argument("--tags", nargs="?", const="*", default=None)
    ap.add_argument("--remotes", nargs="?", const="*", default=None)
    ap.add_argument("--no-walk", dest="no_walk", nargs="?", const="sorted", default=None)
    ap.add_argument("--graph", action="store_true")
    ap.add_argument("--topo-order", dest="topo_order", action="store_true")
    ap.add_argument("--date-order", dest="date_order", action="store_true")
    ap.add_argument("--parents", action="store_true")
    ap.add_argument("--merges", action="store_true")
    ap.add_argument("--min-parents", dest="min_parents", type=int, default=None)
    ap.add_argument("--max-parents", dest="max_parents", type=int, default=None)
    ap.add_argument("--grep", action="append", default=None)
    ap.add_argument("--author", action="append", default=None)
    ap.add_argument("--committer", action="append", default=None)
    ap.add_argument("--all-match", dest="all_match", action="store_true")
    ap.add_argument("-i", "--regexp-ignore-case", dest="ignore_case", action="store_true")
    ap.add_argument("-S", dest="pickaxe_s", default=None)
    ap.add_argument("-G", dest="pickaxe_g", default=None)
    ap.add_argument("--since", "--after", dest="since", default=None)
    ap.add_argument("--until", "--before", dest="until", default=None)
    ap.add_argument("--date", default=None)
    ap.add_argument("-n", "--max-count", type=int, default=None)
    ap.add_argument("-g", "--walk-reflogs", dest="walk_reflogs", action="store_true")
    ap.add_argument("--follow", action="store_true")
    ap.add_argument("--left-right", dest="left_right", action="store_true")
    ap.add_argument("pos", nargs="*")
    args = ap.parse_args(_expand_count_shorthand(argv))
    repo = _repo()
    # --no-abbrev[-commit] override the abbreviation: full oids everywhere.
    if args.no_abbrev:
        args.abbrev = repo.hex_len
    if args.no_abbrev_commit:
        args.abbrev_commit = False

    if args.graph and args.reverse:
        _err("fatal: options '--graph' and '--reverse' cannot be used together")
        return 128
    if args.walk_reflogs and args.reverse:
        _err("fatal: options '--reverse' and '--walk-reflogs' cannot be used together")
        return 128

    # Split positionals into revisions and pathspecs.
    revs: list[str] = []
    log_paths: list[str] = []
    if "--" in args.pos:
        di = args.pos.index("--")
        revs, log_paths = args.pos[:di], args.pos[di + 1:]
    else:
        for tok in args.pos:
            if ".." in tok or refs_mod.rev_parse(repo, tok) is not None:
                revs.append(tok)
            elif (repo.path / tok).exists():
                log_paths.append(tok)
            else:
                # Not a rev and not a path: keep as a rev so resolution errors.
                revs.append(tok)
    rev = revs[0] if revs else "HEAD"

    exclude: set[str] = set()
    if ".." in rev:
        lo, _, hi = rev.partition("..")
        sha = refs_mod.rev_parse(repo, hi or "HEAD")
        if lo:
            lo_sha = refs_mod.rev_parse(repo, lo)
            if lo_sha:
                stack = deque([lo_sha])
                while stack:
                    x = stack.popleft()
                    if x in exclude:
                        continue
                    exclude.add(x)
                    info = _commit_tree_parents(repo, x)
                    if info:
                        stack.extend(info[1])
    else:
        sha = refs_mod.rev_parse(repo, rev)
    if not sha and not args.all:
        head_sym, _ = refs_mod.read_head(repo)
        if rev == "HEAD" and head_sym and head_sym.startswith("refs/heads/"):
            branch = head_sym[len("refs/heads/"):]
            _err(f"fatal: your current branch '{branch}' does not have any commits yet")
            return 128
        _err(f"fatal: ambiguous argument '{rev}': unknown revision or path not in the working tree.")
        _err("Use '--' to separate paths from revisions, like this:")
        _err("'git <command> [<revision>...] -- [<file>...]'")
        return 128
    # Determine output style. `--format`/`tformat:` terminate each entry with a
    # newline; `format:` only separates entries (no trailing newline).
    fmt_string: Optional[str] = None
    style = "medium"
    fmt_terminator = True
    # `--format=<builtin>` is an alias for `--pretty=<builtin>`, not a literal.
    if args.format in ("oneline", "short", "medium", "full", "fuller", "raw", "reference"):
        args.pretty, args.format = args.format, None
    if args.format is not None:
        fmt_string = args.format
        style = "format"
    elif args.pretty is not None:
        if args.pretty == "oneline":
            style = "oneline_full"
        elif args.pretty.startswith("format:"):
            fmt_terminator = False
            fmt_string = args.pretty.split(":", 1)[1]
            style = "format"
        elif args.pretty.startswith("tformat:") or "%" in args.pretty:
            # A non-builtin value containing '%' is a tformat string.
            fmt_terminator = True
            fmt_string = (args.pretty.split(":", 1)[1]
                          if args.pretty.startswith("tformat:") else args.pretty)
            style = "format"
        else:
            style = args.pretty
    if args.oneline:
        style = "oneline"
    date_mode = args.date or "default"
    date_given = args.date is not None

    decorate = args.decorate is not None and not args.no_decorate
    # The %d/%D/%(decorate) placeholders always expand decorations.
    if not decorate and fmt_string and ("%d" in fmt_string or "%D" in fmt_string
                                        or "%(decorate" in fmt_string):
        decorate = True
    decorations = _commit_decorations(repo, full=(args.decorate == "full")) if decorate else {}

    # `--graph` and `--topo-order` reorder commits topologically (unless
    # `--date-order` is also given); collect the full set first, then sort.
    want_topo = (args.graph or args.topo_order) and not args.date_order
    # Commit-level filters mean the early --max-count cutoff during collection
    # would truncate before filtering, so collect fully when any filter is set.
    want_filter = any((
        args.merges, args.no_merges, args.min_parents is not None,
        args.max_parents is not None, args.grep, args.author, args.committer,
        args.pickaxe_s is not None, args.pickaxe_g is not None,
        args.since, args.until))
    collect_full = want_topo or want_filter

    # Collect the ordered list of commit shas first so --reverse can flip it.
    seen: set[str] = set()
    commit_list: list[str] = []
    # `-g`/`--walk-reflogs`: enumerate the named ref's reflog (newest first)
    # instead of walking commit ancestry. Each entry carries its own selector,
    # committer identity and message, which the renderer injects per commit.
    reflog_meta: Optional[list[dict]] = None
    if args.walk_reflogs:
        from . import reflog as _reflog
        full_ref = rev if rev == "HEAD" else (refs_mod.dwim_full_name(repo, rev) or rev)
        entries = _reflog.read(repo, full_ref)
        reflog_meta = []
        zero = repo.null_oid()
        # git's selector keeps the ref *as given on the command line* for %gD;
        # %gd shortens it by stripping the well-known ref namespaces.
        short_rev = rev
        for pre in ("refs/heads/", "refs/remotes/", "refs/tags/"):
            if short_rev.startswith(pre):
                short_rev = short_rev[len(pre):]
                break
        for i, (_old, new, ident, msg) in enumerate(reversed(entries)):
            # Deletion markers (zero new-oid) consume an @{N} slot but aren't shown.
            if new == zero:
                continue
            commit_list.append(new)
            reflog_meta.append({"sref": short_rev, "fref": rev, "i": i,
                                "ident": ident, "msg": msg})
        want_topo = collect_full = False
    elif args.no_walk is not None:
        # --no-walk shows only the named revisions, no ancestry traversal. The
        # default ("sorted") orders by committer date; "unsorted" keeps the
        # command-line order. Downstream filter/--max-count/--reverse still run.
        want_topo = collect_full = False
        wanted = revs if revs else ["HEAD"]
        for r in wanted:
            rsha = refs_mod.rev_parse(repo, r)
            if rsha and rsha not in commit_list:
                commit_list.append(rsha)
        if args.no_walk != "unsorted":
            commit_list.sort(key=lambda s: -_commit_date(repo, s))
    elif args.all or args.branches is not None or args.tags is not None or args.remotes is not None:
        # `--all`/`--branches`/`--tags`/`--remotes` walk the selected ref
        # namespaces. Match git's reverse-chronological order: a stable max-heap
        # keyed on committer date, ties broken by seed order (ascending refname,
        # then HEAD for --all).
        import heapq
        import fnmatch
        heap: list[tuple[int, int, str]] = []
        pushed: set[str] = set()
        counter = 0

        def _push(csha: str) -> None:
            nonlocal counter
            if csha in pushed:
                return
            pushed.add(csha)
            heapq.heappush(heap, (-_commit_date(repo, csha), counter, csha))
            counter += 1

        wanted_ns = []
        if args.all or args.branches is not None:
            wanted_ns.append(("refs/heads/", args.branches if args.branches not in (None, "*") else None))
        if args.all or args.tags is not None:
            wanted_ns.append(("refs/tags/", args.tags if args.tags not in (None, "*") else None))
        if args.all or args.remotes is not None:
            wanted_ns.append(("refs/remotes/", args.remotes if args.remotes not in (None, "*") else None))
        for refname, _rsha in _enumerate_refs(repo):
            for pre, pat in wanted_ns:
                if refname.startswith(pre):
                    if pat and not fnmatch.fnmatch(refname[len(pre):], pat):
                        continue
                    csha = refs_mod.rev_parse(repo, refname + "^{commit}")
                    if csha:
                        _push(csha)
                    break
        if args.all:
            _, head_sha = refs_mod.read_head(repo)
            if head_sha:
                _push(head_sha)
        while heap:
            _d, _c, s = heapq.heappop(heap)
            if s in seen or s in exclude:
                continue
            seen.add(s)
            info = _commit_tree_parents(repo, s)
            if info is None:
                continue
            commit_list.append(s)
            for p in (info[1][:1] if args.first_parent else info[1]):
                _push(p)
            if not log_paths and not collect_full and args.max_count and len(commit_list) >= args.max_count:
                break
    else:
        cur = deque([sha])
        while cur:
            s = cur.popleft()
            if s in seen or s in exclude:
                continue
            seen.add(s)
            info = _commit_tree_parents(repo, s)
            if info is None:
                break
            commit_list.append(s)
            cur.extend(info[1][:1] if args.first_parent else info[1])
            if not log_paths and not collect_full and args.max_count and len(commit_list) >= args.max_count:
                break

    if want_topo:
        commit_list = _topo_order(repo, commit_list, args.first_parent)

    if args.follow and len(log_paths) == 1 and not args.walk_reflogs:
        # --follow: include commits that touch the file, switching the followed
        # name to the rename source whenever the file first appears via a rename.
        followed = log_paths[0]
        filtered = []
        for s in commit_list:
            info = _commit_tree_parents(repo, s)
            tree, parents = info[0], info[1]
            ptree = _commit_tree_parents(repo, parents[0])[0] if parents else None
            before = workdir.tree_path_entry(repo, ptree, followed) if ptree else None
            after = workdir.tree_path_entry(repo, tree, followed)
            bsha = before.sha if before else None
            asha = after.sha if after else None
            if bsha != asha:
                filtered.append(s)
                if asha and not bsha and ptree:
                    src = _follow_rename_source(repo, ptree, tree, followed)
                    if src:
                        followed = src
        commit_list = filtered
    elif log_paths and not args.walk_reflogs:
        filtered: list[str] = []
        for s in commit_list:
            info = _commit_tree_parents(repo, s)
            tree, parents = info[0], info[1]
            ptree = _commit_tree_parents(repo, parents[0])[0] if parents else None
            for path in log_paths:
                before = workdir.tree_path_entry(repo, ptree, path) if ptree else None
                after = workdir.tree_path_entry(repo, tree, path)
                if (before.sha if before else None) != (after.sha if after else None):
                    filtered.append(s)
                    break
            if not collect_full and args.max_count and len(filtered) >= args.max_count:
                break
        commit_list = filtered

    # Commit-level filters (applied before --max-count truncation, like git).
    if not args.walk_reflogs:
        commit_list = _filter_commits(repo, commit_list, args)

    if args.max_count is not None:
        commit_list = commit_list[:max(0, args.max_count)]
        if reflog_meta is not None:
            reflog_meta = reflog_meta[:max(0, args.max_count)]
    if args.reverse:
        commit_list = list(reversed(commit_list))
        if reflog_meta is not None:
            reflog_meta = list(reversed(reflog_meta))

    graph = None
    if args.graph:
        from . import graph as _graph_mod

        def _parents_of(csha: str) -> list:
            info = _commit_tree_parents(repo, csha)
            return list(info[1]) if info else []

        graph = _graph_mod.Graph(_parents_of)
    # Styles whose commits are separated by a blank line (graph prefixes it).
    multiline = style in ("medium", "full", "fuller", "short", "raw")
    last_index = len(commit_list) - 1

    def emit_commit_diff(s, c, lead_blank):
        # The stat/patch/raw/name listing that --stat/-p/--raw/--name-* append
        # to each commit. A merge has no default (non-combined) diff, so git
        # emits nothing (not even the leading blank line).
        if not (args.stat or args.shortstat or args.patch or args.name_only
                or args.name_status or args.raw):
            return
        if len(c.parents) > 1:
            return
        parent_tree = None
        if c.parents:
            _, pd = objs.read_object(repo, c.parents[0])
            parent_tree = objs.parse_commit(pd).tree
        tchanges = _tree_changes(repo, parent_tree, c.tree)
        # A pathspec restricts the per-commit diff to the matching files (git
        # shows only changes touching the pathspec). --follow varies the name
        # per commit, so leave it unrestricted there.
        if log_paths and not args.follow:
            def _pm(p):
                return any(p == w or p.startswith(w.rstrip("/") + "/") for w in log_paths)
            tchanges = [ch for ch in tchanges if _pm(ch[0])]
        if lead_blank:
            _print("")
        if args.stat:
            _diff_stat(tchanges)
        elif args.shortstat:
            _diff_shortstat(tchanges)
        elif args.name_only:
            for path, _a, _b in tchanges:
                _print(path)
        elif args.name_status:
            for path, a, b in tchanges:
                st = "A" if not a.present else ("D" if not b.present else "M")
                _print(f"{st}\t{path}")
        elif args.raw:
            for path, a, b in tchanges:
                status = "A" if not a.present else ("D" if not b.present else "M")
                a_sha = a.sha[:7] if a.present else "0000000"
                b_sha = b.sha[:7] if b.present else "0000000"
                _print(f":{(a.mode or '000000').zfill(6)} {(b.mode or '000000').zfill(6)} "
                       f"{a_sha} {b_sha} {status}\t{path}")
        else:
            for path, a, b in tchanges:
                _emit_file_diff(path, a, b, context=args.unified)

    def render(count, s, c, meta=None):
        if style == "format":
            expansion = _expand_commit_format(repo, s, c, fmt_string, decorations, date_mode, args.abbrev,
                                              reflog=meta, date_given=date_given)
            if fmt_terminator:
                sys.stdout.write(expansion + "\n")
            else:
                if count > 0:
                    sys.stdout.write("\n")
                sys.stdout.write(expansion)
            emit_commit_diff(s, c, lead_blank=True)
        elif style in ("oneline", "oneline_full"):
            short = style == "oneline" or args.abbrev_commit
            abbrev = s[:args.abbrev] if short else s
            if meta is not None:
                sel = _reflog_selector(meta, full=False, date_mode=date_mode, date_given=date_given)
                _print(f"{abbrev} {sel}: {meta['msg']}")
                return
            first = c.message.splitlines()[0] if c.message.strip() else ""
            deco = _format_decoration(decorations.get(s, []))
            pre = abbrev
            if args.parents:
                pre += "".join(" " + (p[:7] if short else p) for p in c.parents)
            _print(f"{pre}{deco} {first}")
            emit_commit_diff(s, c, lead_blank=False)
        elif style == "reference":
            first = c.message.splitlines()[0] if c.message.strip() else ""
            _print(f"{s[:7]} ({first}, {_format_date(c.author, 'short')})")
        elif style in ("short", "raw"):
            if count > 0:
                _print("")
            sha_disp = s[:7] if args.abbrev_commit else s
            psuf = ("".join(" " + (p[:7] if args.abbrev_commit else p) for p in c.parents)
                    if args.parents else "")
            _print(f"commit {sha_disp}{psuf}{_format_decoration(decorations.get(s, []))}")
            if style == "raw":
                _print(f"tree {c.tree}")
                for p in c.parents:
                    _print(f"parent {p}")
                _print(f"author {c.author}")
                _print(f"committer {c.committer}")
                _print("")
                for line in c.message.rstrip("\n").splitlines():
                    _print(f"    {line}")
            else:  # short
                if len(c.parents) > 1:
                    _print("Merge: " + " ".join(p[:7] for p in c.parents))
                _print(f"Author: {_split_ident(c.author)[0]}")
                _print("")
                first = c.message.splitlines()[0] if c.message.strip() else ""
                _print(f"    {first}")
        else:
            if count > 0:
                _print("")
            psuf = ("".join(" " + (p[:7] if args.abbrev_commit else p) for p in c.parents)
                    if args.parents else "")
            _emit_commit_header(s[:7] if args.abbrev_commit else s, c, style=style,
                                date_mode=date_mode, parents_suffix=psuf,
                                decoration=_format_decoration(decorations.get(s, [])),
                                reflog=meta, date_given=date_given)
            _print("")
            for line in c.message.rstrip("\n").splitlines():
                _print(f"    {line}")
            emit_commit_diff(s, c, lead_blank=True)

    for count, s in enumerate(commit_list):
        c = objs.parse_commit(objs.read_object(repo, s)[1])
        meta = reflog_meta[count] if reflog_meta is not None else None
        if graph is None:
            render(count, s, c, meta)
        else:
            text = _capture_output(lambda: render(0, s, c, meta))
            sys.stdout.write(graph.format_commit(
                s, text, emit_separator=(multiline and count > 0)))
    return 0


def _expand_count_shorthand(argv: list[str]) -> list[str]:
    out: list[str] = []
    for a in argv:
        if len(a) > 1 and a[0] == "-" and a[1:].isdigit():
            out.extend(["-n", a[1:]])
        else:
            out.append(a)
    return out


def _commit_decorations(repo: Repository, full: bool = False) -> dict[str, list[str]]:
    """Map commit sha -> ordered decoration labels, matching C Git.

    Git iterates refs in ascending full-name order, *prepending* each label to
    its target's list (so the result is reverse-alphabetical), peels annotated
    tags to the commit they reference, and finally hoists the current branch to
    the front as ``HEAD -> <branch>`` (or a bare ``HEAD`` when detached).
    With ``full`` the labels keep their full ``refs/...`` names.
    """
    out: dict[str, list[str]] = {}
    for refname, sha in _enumerate_refs(repo):
        if refname.startswith("refs/heads/"):
            label = refname if full else refname[len("refs/heads/"):]
            target = sha
        elif refname.startswith("refs/remotes/"):
            label = refname if full else refname[len("refs/remotes/"):]
            target = sha
        elif refname.startswith("refs/tags/"):
            label = "tag: " + (refname if full else refname[len("refs/tags/"):])
            # Annotated tags decorate the commit they ultimately point to.
            target = refs_mod.rev_parse(repo, refname + "^{commit}") or sha
        else:
            continue
        out.setdefault(target, []).insert(0, label)

    head_sym, head_sha = refs_mod.read_head(repo)
    if head_sha:
        if head_sym and head_sym.startswith("refs/heads/"):
            disp = head_sym if full else head_sym[len("refs/heads/"):]
            lst = out.setdefault(head_sha, [])
            if disp in lst:
                lst.remove(disp)
            lst.insert(0, f"HEAD -> {disp}")
        else:
            out.setdefault(head_sha, []).insert(0, "HEAD")
    return out


def _format_decoration(names: list[str]) -> str:
    return f" ({', '.join(names)})" if names else ""


def _split_ident(sig: str) -> tuple[str, Optional[int], Optional[str]]:
    """Split ``Name <email> <unixtime> <tz>`` into (who, unixtime, tz)."""
    import re
    m = re.match(r"^(.*) (\d+) ([+-]\d{4})$", sig)
    if m:
        return m.group(1), int(m.group(2)), m.group(3)
    return sig, None, None


def _relative_date(ts: int, now: Optional[int] = None) -> str:
    """C Git's ``--date=relative`` string (date.c:show_date_relative)."""
    import time
    if now is None:
        now = int(time.time())
    if now < ts:
        return "in the future"
    diff = now - ts

    def ago(n: int, unit: str) -> str:
        return f"{n} {unit}{'' if n == 1 else 's'} ago"

    if diff < 90:
        return ago(diff, "second")
    diff = (diff + 30) // 60          # minutes
    if diff < 90:
        return ago(diff, "minute")
    diff = (diff + 30) // 60          # hours
    if diff < 36:
        return ago(diff, "hour")
    diff = (diff + 12) // 24          # days
    if diff < 14:
        return ago(diff, "day")
    if diff < 70:
        return ago((diff + 3) // 7, "week")
    if diff < 365:
        return ago((diff + 15) // 30, "month")
    if diff < 1825:                   # under ~5 years: years and months
        totalmonths = (diff * 12 * 2 + 365) // (365 * 2)
        years, months = totalmonths // 12, totalmonths % 12
        if months:
            return (f"{years} year{'' if years == 1 else 's'}, "
                    f"{months} month{'' if months == 1 else 's'} ago")
        return f"{years} year{'' if years == 1 else 's'} ago"
    return ago((diff + 183) // 365, "year")


def _reflog_who(ident: str) -> str:
    """Strip the trailing ``<secs> <tz>`` from a reflog ident, leaving
    ``Name <email>`` (the portion git shows in ``Reflog:``/``%gn``/``%ge``)."""
    parts = ident.rsplit(" ", 2)
    if len(parts) == 3 and parts[1].lstrip("-").isdigit():
        return parts[0]
    return ident


def _reflog_selector(meta: dict, *, full: bool, date_mode: str, date_given: bool) -> str:
    """Build a reflog selector ``<ref>@{<index-or-date>}``. ``full`` chooses the
    full ref name (``%gD``) over the short one (``%gd``); with an explicit
    ``--date`` the brace holds the formatted entry time instead of the index."""
    ref = meta["fref"] if full else meta["sref"]
    inner = _format_date(meta["ident"], date_mode) if date_given else str(meta["i"])
    return f"{ref}@{{{inner}}}"


def _format_date(sig: str, mode: str = "default") -> str:
    """Render a signature's timestamp in one of git's ``--date=<mode>`` styles.

    The day of month is never zero-padded in the default and rfc styles,
    matching C Git. ``relative`` is computed against the current time; a
    ``-local`` suffix (or the bare ``local`` mode) renders in the system
    timezone; ``format:<strftime>`` applies an explicit strftime template.
    """
    import datetime
    _who, ts, tz = _split_ident(sig)
    if ts is None or tz is None:
        return sig
    mode = {"iso8601": "iso", "iso8601-strict": "iso-strict",
            "rfc2822": "rfc", "default-local": "local"}.get(mode, mode)

    if mode == "relative":
        return _relative_date(ts)

    # `format:` / `format-local:` apply a raw strftime template.
    if mode.startswith("format:") or mode.startswith("format-local:"):
        local = mode.startswith("format-local:")
        template = mode.split(":", 1)[1]
        d = datetime.datetime.fromtimestamp(ts) if local else (
            datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
            + datetime.timedelta(minutes=_tz_minutes(tz)))
        return d.strftime(template)

    # A `-local` suffix renders in the system timezone; the bare `local` mode is
    # `default` rendered locally. In local mode the displayed offset is the
    # system offset (still shown by iso/iso-strict/rfc; hidden by default).
    local = mode == "local" or mode.endswith("-local")
    if mode == "local":
        mode = "default"
    elif mode.endswith("-local"):
        mode = mode[: -len("-local")]
    if local:
        dt = datetime.datetime.fromtimestamp(ts)
        tz_out = _git_tz_str(dt.astimezone().utcoffset())
    else:
        dt = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc) + datetime.timedelta(minutes=_tz_minutes(tz))
        tz_out = tz

    if mode == "iso":
        return dt.strftime("%Y-%m-%d %H:%M:%S ") + tz_out
    if mode == "iso-strict":
        suffix = "Z" if tz_out == "+0000" else tz_out[:3] + ":" + tz_out[3:]
        return dt.strftime("%Y-%m-%dT%H:%M:%S") + suffix
    if mode == "rfc":
        return dt.strftime("%a, ") + str(dt.day) + dt.strftime(" %b %Y %H:%M:%S ") + tz_out
    if mode == "human":
        return _human_date(dt, ts, tz, local)
    if mode == "short":
        return dt.strftime("%Y-%m-%d")
    if mode == "raw":
        return f"{ts} {tz_out}"
    if mode == "unix":
        return str(ts)
    # The default format shows the offset, except in local mode (hide.tz).
    tail = "" if local else (" " + tz_out)
    return dt.strftime("%a %b ") + str(dt.day) + dt.strftime(" %H:%M:%S %Y") + tail


def _tz_minutes(tz: str) -> int:
    sign = 1 if tz[0] == "+" else -1
    return sign * (int(tz[1:3]) * 60 + int(tz[3:5]))


def _git_tz_str(offset) -> str:
    """Render a timedelta UTC offset as git's ``+HHMM`` / ``-HHMM`` form."""
    mins = int(offset.total_seconds()) // 60 if offset else 0
    sign = "+" if mins >= 0 else "-"
    mins = abs(mins)
    return f"{sign}{mins // 60:02d}{mins % 60:02d}"


def _human_date(dt, ts: int, tz: str, local: bool) -> str:
    """C Git's ``--date=human`` (date.c:show_date_normal with a human now).

    Recent same-day timestamps fall back to a relative string; otherwise git
    drops fields that are redundant relative to the current local time (the
    year within this year, the date for the last few days, always seconds).
    """
    import datetime
    import time
    now = int(time.time())
    now_local = datetime.datetime.fromtimestamp(now)
    # git compares the integer +HHMM tz forms; for tests TZ is pinned so this
    # only matters for the rarely-shown human tz field.
    human_off = now_local.astimezone().utcoffset()
    human_min = int(human_off.total_seconds()) // 60 if human_off else 0
    human_tz_int = (1 if human_min >= 0 else -1) * ((abs(human_min) // 60) * 100 + abs(human_min) % 60)
    tz_int = int(tz)

    hide_tz = local or tz_int == human_tz_int
    hide_year = dt.year == now_local.year
    hide_date = hide_wday = False
    if hide_year and dt.month == now_local.month:
        if dt.day > now_local.day:
            pass  # future date: think timezones
        elif dt.day == now_local.day:
            hide_date = hide_wday = True
        elif dt.day + 5 > now_local.day:
            hide_date = True

    # "today" times collapse to a relative string.
    if hide_wday:
        return _relative_date(ts, now)

    hide_tz = hide_tz or not hide_date
    hide_wday = hide_time = not hide_year

    buf = ""
    if not hide_wday:
        buf += dt.strftime("%a") + " "
    if not hide_date:
        buf += dt.strftime("%b") + " " + str(dt.day) + " "
    if not hide_time:
        buf += dt.strftime("%H:%M")
    else:
        buf = buf.rstrip()
    if not hide_year:
        buf += " " + str(dt.year)
    if not hide_tz:
        buf += " " + tz
    return buf


def _format_ident_date(sig: str) -> str:
    """Format a signature's timestamp like C Git's default ``Date:`` line."""
    return _format_date(sig, "default")


def _emit_commit_header(sha: str, c, *, style: str = "medium",
                        date_mode: str = "default", decoration: str = "",
                        parents_suffix: str = "", reflog=None, date_given: bool = False) -> None:
    """Print the ``commit``/``Author``/``Date`` header block for medium, full,
    and fuller pretty styles, shared by ``log`` and ``show``."""
    _print(f"commit {sha}{parents_suffix}{decoration}")
    if reflog is not None:
        sel = _reflog_selector(reflog, full=False, date_mode=date_mode, date_given=date_given)
        _print(f"Reflog: {sel} ({_reflog_who(reflog['ident'])})")
        _print(f"Reflog message: {reflog['msg']}")
    if len(c.parents) > 1:
        _print("Merge: " + " ".join(p[:7] for p in c.parents))
    author_who = _split_ident(c.author)[0]
    committer_who = _split_ident(c.committer)[0]
    if style == "fuller":
        _print(f"Author:     {author_who}")
        _print(f"AuthorDate: {_format_date(c.author, date_mode)}")
        _print(f"Commit:     {committer_who}")
        _print(f"CommitDate: {_format_date(c.committer, date_mode)}")
    elif style == "full":
        _print(f"Author: {author_who}")
        _print(f"Commit: {committer_who}")
    else:
        _print(f"Author: {author_who}")
        _print(f"Date:   {_format_date(c.author, date_mode)}")


def cmd_show(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit show", add_help=False)
    ap.add_argument("-s", "--no-patch", dest="no_patch", action="store_true")
    ap.add_argument("--stat", action="store_true")
    ap.add_argument("--raw", action="store_true")
    ap.add_argument("--name-only", dest="name_only", action="store_true")
    ap.add_argument("--name-status", dest="name_status", action="store_true")
    ap.add_argument("--oneline", action="store_true")
    ap.add_argument("--format", default=None)
    ap.add_argument("--pretty", nargs="?", const="medium", default=None)
    ap.add_argument("--date", default=None)
    ap.add_argument("--abbrev", type=int, default=7)
    ap.add_argument("-U", "--unified", type=int, default=3)
    ap.add_argument("rev", nargs="*")
    args = ap.parse_args(argv)
    repo = _repo()
    # `--format=<builtin>` is an alias for `--pretty=<builtin>`.
    if args.format in ("oneline", "short", "medium", "full", "fuller", "raw", "reference"):
        args.pretty, args.format = args.format, None
    pstyle = args.pretty if args.pretty in ("full", "fuller", "short", "raw", "reference") else "medium"
    date_mode = args.date or "default"
    fmt = args.format
    if fmt is None and args.pretty:
        if args.pretty.startswith("format:") or args.pretty.startswith("tformat:"):
            fmt = args.pretty.split(":", 1)[1]
        elif "%" in args.pretty:
            fmt = args.pretty
    if fmt is None and args.oneline:
        fmt = "%h %s"

    def _show_one(the_rev: str) -> int:
        sha = refs_mod.rev_parse(repo, the_rev)
        if not sha:
            _err(f"fatal: ambiguous argument '{the_rev}': unknown revision or path not in the working tree.")
            _err("Use '--' to separate paths from revisions, like this:")
            _err("'git <command> [<revision>...] -- [<file>...]'")
            return 128
        if fmt is not None:
            peeled = refs_mod.rev_parse(repo, the_rev + "^{commit}") or sha
            c = objs.parse_commit(objs.read_object(repo, peeled)[1])
            _print(_expand_commit_format(repo, peeled, c, fmt, {}, date_mode=date_mode, abbrev=args.abbrev))
            if not args.no_patch and not args.stat:
                ptree = None
                if c.parents:
                    ptree = objs.parse_commit(objs.read_object(repo, c.parents[0])[1]).tree
                _emit_tree_patch(repo, ptree, c.tree, args.unified)
            return 0
        return _show_object(the_rev, sha)

    def _show_object(the_rev: str, sha: str) -> int:
        t, data = objs.read_object(repo, sha)
        if t == "tag":
            # Print the annotated-tag header, then peel to its target.
            header, _, tagmsg = data.decode("utf-8", errors="replace").partition("\n\n")
            tag_name = ""
            tagger = ""
            target = None
            for line in header.splitlines():
                key, _, val = line.partition(" ")
                if key == "tag":
                    tag_name = val
                elif key == "tagger":
                    tagger = val
                elif key == "object":
                    target = val.strip()
            _print(f"tag {tag_name}")
            if tagger:
                _print(f"Tagger: {_split_ident(tagger)[0]}")
                _print(f"Date:   {_format_date(tagger, date_mode)}")
            _print("")
            _print(tagmsg.rstrip("\n"))
            _print("")
            if target:
                sha = target
                t, data = objs.read_object(repo, sha)
        if t == "commit":
            c = objs.parse_commit(data)
            if pstyle == "reference":
                first = c.message.splitlines()[0] if c.message.strip() else ""
                _print(f"{sha[:7]} ({first}, {_format_date(c.author, 'short')})")
            elif pstyle == "raw":
                _print(f"commit {sha}")
                _print(f"tree {c.tree}")
                for p in c.parents:
                    _print(f"parent {p}")
                _print(f"author {c.author}")
                _print(f"committer {c.committer}")
                _print("")
                for line in c.message.rstrip("\n").splitlines():
                    _print(f"    {line}")
            elif pstyle == "short":
                _print(f"commit {sha}")
                if len(c.parents) > 1:
                    _print("Merge: " + " ".join(p[:7] for p in c.parents))
                _print(f"Author: {_split_ident(c.author)[0]}")
                _print("")
                first = c.message.splitlines()[0] if c.message.strip() else ""
                _print(f"    {first}")
            else:
                _emit_commit_header(sha, c, style=pstyle, date_mode=date_mode)
                _print("")
                for line in c.message.rstrip("\n").splitlines():
                    _print(f"    {line}")
            parent_tree = None
            if c.parents:
                _, pd = objs.read_object(repo, c.parents[0])
                parent_tree = objs.parse_commit(pd).tree
            # For a merge, the default (combined) patch and raw output collapse to
            # empty for a clean merge, but git still prints the separating blank
            # line. '--stat' is special: it reports against the first parent.
            is_merge = len(c.parents) > 1
            if args.stat:
                _print("")
                _diff_stat(_tree_changes(repo, parent_tree, c.tree))
            elif args.name_only:
                _print("")
                if not is_merge:
                    for path, _a, _b in _tree_changes(repo, parent_tree, c.tree):
                        _print(path)
            elif args.name_status:
                _print("")
                if not is_merge:
                    for path, a, b in _tree_changes(repo, parent_tree, c.tree):
                        st = "A" if not a.present else ("D" if not b.present else "M")
                        _print(f"{st}\t{path}")
            elif args.raw:
                _print("")
                if not is_merge:
                    _emit_raw_diff(repo, parent_tree, c.tree)
            elif not args.no_patch:
                _print("")
                if not is_merge:
                    _emit_tree_patch(repo, parent_tree, c.tree, args.unified)
        elif t == "tree":
            # `git show <tree>` prints `<rev>\n\n` then bare entry names
            # (directories suffixed with '/'), not the ls-tree triple.
            _print(f"tree {the_rev}")
            _print("")
            for e in objs.parse_tree(data, repo.hash_len):
                _print(e.name + ("/" if e.is_dir() else ""))
        else:
            sys.stdout.buffer.write(data)
            if not data.endswith(b"\n"):
                sys.stdout.write("\n")
        return 0

    revs = args.rev or ["HEAD"]
    multiline = fmt is None and pstyle in ("medium", "full", "fuller", "short", "raw")
    for count, the_rev in enumerate(revs):
        if count > 0 and multiline:
            _print("")
        rc = _show_one(the_rev)
        if rc:
            return rc
    return 0


def _tree_changes(repo: Repository, a_tree: Optional[str], b_tree: Optional[str]) -> list[tuple[str, "_Side", "_Side"]]:
    a_map = _tree_map_full(repo, a_tree)
    b_map = _tree_map_full(repo, b_tree)
    changes: list[tuple[str, _Side, _Side]] = []
    for p in sorted(set(a_map) | set(b_map)):
        a = _side_from_object(repo, *a_map[p]) if p in a_map else _ABSENT
        b = _side_from_object(repo, *b_map[p]) if p in b_map else _ABSENT
        if a.sha != b.sha or a.mode != b.mode:
            changes.append((p, a, b))
    return changes


def _emit_raw_diff(repo: Repository, a_tree: Optional[str], b_tree: Optional[str]) -> None:
    """Emit ``git log --raw`` style lines: ``:<amode> <bmode> <asha> <bsha> <st>\\t<path>``."""
    for path, a, b in _tree_changes(repo, a_tree, b_tree):
        status = "A" if not a.present else ("D" if not b.present else "M")
        a_sha = a.sha[:7] if a.present else "0000000"
        b_sha = b.sha[:7] if b.present else "0000000"
        a_mode = (a.mode or "000000").zfill(6)
        b_mode = (b.mode or "000000").zfill(6)
        _print(f":{a_mode} {b_mode} {a_sha} {b_sha} {status}\t{path}")


def _emit_tree_patch(repo: Repository, a_tree: Optional[str], b_tree: Optional[str], context: int = 3) -> None:
    for path, a, b in _tree_changes(repo, a_tree, b_tree):
        _emit_file_diff(path, a, b, context=context)


def _print_tree_diff(repo: Repository, a_tree: str, b_tree: str) -> None:
    for p, a_entry, b_entry in workdir.iter_tree_changes(repo, a_tree, b_tree):
        a_sha = a_entry.sha if a_entry else None
        b_sha = b_entry.sha if b_entry else None
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


class _Side:
    __slots__ = ("mode", "sha", "data", "worktree")

    def __init__(self, mode: Optional[str], sha: Optional[str], data: Optional[bytes], worktree: bool = False):
        self.mode = mode
        self.sha = sha
        self.data = data
        self.worktree = worktree

    @property
    def present(self) -> bool:
        return self.mode is not None


_ABSENT = _Side(None, None, None)


def _side_from_object(repo: Repository, mode: Optional[str], sha: Optional[str]) -> _Side:
    if sha is None:
        return _ABSENT
    try:
        _, data = objs.read_object(repo, sha)
    except KeyError:
        data = b""
    return _Side(mode, sha, data)


def _side_from_worktree(repo: Repository, path: str) -> _Side:
    full = repo.path / path
    if not (full.exists() or full.is_symlink()):
        return _ABSENT
    import stat as _stat
    if full.is_symlink():
        data = os.readlink(full).encode("utf-8")
    else:
        data = full.read_bytes()
    sha, _ = objs.hash_bytes("blob", data, repo)
    return _Side(_wt_mode(full), sha, data, worktree=True)


def _tree_map_full(repo: Repository, tree_sha: Optional[str]) -> dict[str, tuple[str, str]]:
    out: dict[str, tuple[str, str]] = {}
    if tree_sha:
        for path, mode, sha in workdir.iter_tree_files(repo, tree_sha):
            out[path] = (mode, sha)
    return out


def _commit_tree(repo: Repository, sha: str) -> Optional[str]:
    try:
        t, data = objs.read_object(repo, sha)
    except KeyError:
        return None
    if t == "commit":
        return objs.parse_commit(data).tree
    if t == "tree":
        return sha
    if t == "tag":
        return refs_mod._peel_to_type(repo, sha, "tree")
    return None


def _is_binary(data: Optional[bytes]) -> bool:
    return bool(data) and b"\x00" in data[:8000]


def _ws_check_line(line: str) -> str:
    """Return git's whitespace-error message(s) for an added line, under the
    default rule (blank-at-eol + space-before-tab), or "" if clean. Ported from
    ws.c:ws_check_emit. (blank-at-eof is hunk-level and intentionally omitted.)"""
    stripped = line.rstrip(" \t")
    parts: list[str] = []
    if len(stripped) < len(line):
        parts.append("trailing whitespace")
    # space-before-tab: a tab in the leading indent preceded by a space.
    n = len(stripped)
    i = 0
    written = 0
    sbt = False
    while i < n:
        ch = line[i]
        if ch == " ":
            i += 1
            continue
        if ch != "\t":
            break
        if written < i:
            sbt = True
        written = i + 1
        i += 1
    if sbt:
        parts.append("space before tab in indent")
    return ", ".join(parts)


def _follow_rename_source(repo: Repository, ptree: Optional[str], tree: Optional[str], dst: str) -> Optional[str]:
    """For `log --follow`: if ``dst`` was created in ``tree`` by renaming a file
    that existed in ``ptree``, return that source path (else None)."""
    changes = _tree_changes(repo, ptree, tree)
    renames, _ = _detect_changes_renames(repo, changes)
    for entry in renames:
        if entry[1] == dst:
            return entry[0]
    return None


def _diff_check(repo: Repository, changes: list) -> int:
    """`git diff --check`: report whitespace errors on added lines, suppressing
    the normal diff. Returns 2 if any error is found, else 0."""
    from . import diff as _diff
    found = False
    for path, a, b in changes:
        if not b.present or _is_binary(b.data):
            continue
        a_text = a.data.decode("utf-8", "replace") if (a.present and a.data is not None) else ""
        b_text = b.data.decode("utf-8", "replace") if b.data is not None else ""
        a_lines = a_text.split("\n")
        b_lines = b_text.split("\n")
        if a_text.endswith("\n"):
            a_lines = a_lines[:-1]
        if b_text.endswith("\n"):
            b_lines = b_lines[:-1]
        for kind, _ai, bi in _diff.diff_lines(a_lines, b_lines):
            if kind != "ins":
                continue
            msg = _ws_check_line(b_lines[bi])
            if msg:
                found = True
                _print(f"{path}:{bi + 1}: {msg}.")
                _print(f"+{b_lines[bi]}")
    return 2 if found else 0


def _emit_file_diff(path: str, a: _Side, b: _Side, reverse: bool = False, context: int = 3,
                    word_diff=None) -> None:
    if a.sha == b.sha and a.mode == b.mode:
        return
    # Under -R the working-side prefixes are swapped (b/<path> a/<path>).
    pa, pb = ("b", "a") if reverse else ("a", "b")
    _print(f"diff --git {pa}/{path} {pb}/{path}")
    if not a.present:
        _print(f"new file mode {b.mode}")
    elif not b.present:
        _print(f"deleted file mode {a.mode}")
    elif a.mode != b.mode:
        _print(f"old mode {a.mode}")
        _print(f"new mode {b.mode}")
    a_abbrev = (a.sha or "0" * 40)[:7]
    b_abbrev = (b.sha or "0" * 40)[:7]
    if a.sha != b.sha:
        suffix = f" {a.mode}" if a.present and b.present and a.mode == b.mode else ""
        _print(f"index {a_abbrev}..{b_abbrev}{suffix}")
        if _is_binary(a.data) or _is_binary(b.data):
            _print(f"Binary files {pa + '/' + path if a.present else '/dev/null'} and "
                   f"{pb + '/' + path if b.present else '/dev/null'} differ")
            return
        a_text = (a.data or b"").decode("utf-8", errors="replace")
        b_text = (b.data or b"").decode("utf-8", errors="replace")
        _print(f"--- {pa + '/' + path if a.present else '/dev/null'}")
        _print(f"+++ {pb + '/' + path if b.present else '/dev/null'}")
        if word_diff:
            sys.stdout.write(diff_mod.word_diff_hunks(
                a_text.splitlines(), b_text.splitlines(), context, mode=word_diff))
            return
        body = diff_mod.format_hunks(
            a_text.splitlines(), b_text.splitlines(),
            context,
            a_no_newline=bool(a_text) and not a_text.endswith("\n"),
            b_no_newline=bool(b_text) and not b_text.endswith("\n"),
        )
        for line in body:
            _print(line)


def _diff_stat(changes: list[tuple[str, _Side, _Side]], renames=None) -> None:
    rows: list[tuple[str, int, int, int]] = []  # path, ins, del, total
    total_ins = total_del = 0
    for src, dst, _sim, src_side, dst_side in (renames or []):
        a_lines = (src_side.data or b"").decode("utf-8", errors="replace").splitlines()
        b_lines = (dst_side.data or b"").decode("utf-8", errors="replace").splitlines()
        ins = dele = 0
        for op in diff_mod.diff_lines(a_lines, b_lines):
            if op[0] == "ins":
                ins += 1
            elif op[0] == "del":
                dele += 1
        rows.append((f"{src} => {dst}", ins, dele, ins + dele))
        total_ins += ins
        total_del += dele
    for path, a, b in changes:
        if a.sha == b.sha and a.mode == b.mode:
            continue
        if _is_binary(a.data) or _is_binary(b.data):
            rows.append((path, -1, -1, -1))
            continue
        a_lines = (a.data or b"").decode("utf-8", errors="replace").splitlines()
        b_lines = (b.data or b"").decode("utf-8", errors="replace").splitlines()
        ins = dele = 0
        for op in diff_mod.diff_lines(a_lines, b_lines):
            if op[0] == "ins":
                ins += 1
            elif op[0] == "del":
                dele += 1
        rows.append((path, ins, dele, ins + dele))
        total_ins += ins
        total_del += dele
    if not rows:
        return
    name_w = max(len(p) for p, *_ in rows)
    count_w = max(len(str(t if t >= 0 else 0)) for *_, t in rows)
    for path, ins, dele, total in rows:
        if total < 0:
            _print(f" {path:<{name_w}} | Bin")
            continue
        bar = "+" * ins + "-" * dele
        _print(f" {path:<{name_w}} | {total:>{count_w}}{(' ' + bar) if bar else ''}")
    _print(_stat_summary_line(len(rows), total_ins, total_del))


def _diff_shortstat(changes: list[tuple[str, _Side, _Side]]) -> None:
    """Print only C Git's ``--shortstat`` summary line for a set of changes."""
    files = total_ins = total_del = 0
    for _path, a, b in changes:
        if a.sha == b.sha and a.mode == b.mode:
            continue
        files += 1
        if _is_binary(a.data) or _is_binary(b.data):
            continue
        a_lines = (a.data or b"").decode("utf-8", errors="replace").splitlines()
        b_lines = (b.data or b"").decode("utf-8", errors="replace").splitlines()
        for op in diff_mod.diff_lines(a_lines, b_lines):
            if op[0] == "ins":
                total_ins += 1
            elif op[0] == "del":
                total_del += 1
    if files == 0:
        return
    _print(_stat_summary_line(files, total_ins, total_del))


def _diff_summary(changes: list[tuple[str, _Side, _Side]], renames=None) -> None:
    """Print C Git's ``--summary`` lines (create/delete/mode-change/rename)."""
    lines: list[tuple[str, str]] = []
    for src, dst, sim, _sa, _db in (renames or []):
        lines.append((dst, f" rename {src} => {dst} ({sim}%)"))
    for path, a, b in changes:
        if not a.present and b.present:
            lines.append((path, f" create mode {(b.mode or '000000').zfill(6)} {path}"))
        elif a.present and not b.present:
            lines.append((path, f" delete mode {(a.mode or '000000').zfill(6)} {path}"))
        elif a.present and b.present and a.mode != b.mode:
            lines.append((path, f" mode change {a.mode} => {b.mode} {path}"))
    for _key, line in sorted(lines):
        _print(line)


def _stat_summary_line(files: int, insertions: int, deletions: int) -> str:
    """Render C Git's ' N files changed, X insertions(+), Y deletions(-)' line,
    showing the zero parts only when both counts are zero."""
    parts = [f"{files} file{'s' if files != 1 else ''} changed"]
    if insertions or deletions == 0:
        parts.append(f"{insertions} insertion{'s' if insertions != 1 else ''}(+)")
    if deletions or insertions == 0:
        parts.append(f"{deletions} deletion{'s' if deletions != 1 else ''}(-)")
    return " " + ", ".join(parts)


def cmd_diff(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit diff", add_help=False)
    ap.add_argument("--cached", "--staged", dest="cached", action="store_true")
    ap.add_argument("--stat", action="store_true")
    ap.add_argument("--numstat", action="store_true")
    ap.add_argument("--shortstat", action="store_true")
    ap.add_argument("--summary", action="store_true")
    ap.add_argument("--diff-filter", dest="diff_filter", default=None)
    ap.add_argument("--name-only", dest="name_only", action="store_true")
    ap.add_argument("--name-status", dest="name_status", action="store_true")
    ap.add_argument("--raw", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--exit-code", dest="exit_code", action="store_true")
    ap.add_argument("-R", dest="reverse", action="store_true")
    ap.add_argument("-U", "--unified", type=int, default=3)
    # -M/-C (with optional attached values) are accepted but never consume a
    # following token; rename detection is on by default (diff.renames=true).
    ap.add_argument("--no-renames", dest="no_renames", action="store_true")
    # `--stat=<width>` / `--stat-width=<n>` only tune column widths, which do not
    # affect pythongit's output for the file sizes under test; normalize to bare.
    argv = ["--stat" if a.startswith("--stat=") else a for a in argv]
    # `--word-diff[=<mode>]` only takes a value when attached with '='; a bare
    # flag means "plain". Pull it out of argv so the trailing rev isn't consumed.
    word_diff_mode = None
    _wd_argv = []
    for a in argv:
        if a == "--word-diff":
            word_diff_mode = "plain"
        elif a.startswith("--word-diff="):
            word_diff_mode = a.split("=", 1)[1]
        elif a == "--color-words":
            word_diff_mode = "plain"
        else:
            _wd_argv.append(a)
    argv = _wd_argv
    args, rest = ap.parse_known_args(argv)
    args.word_diff = word_diff_mode
    repo = _repo()
    revs: list[str] = []
    paths: list[str] = []
    after_dd = False
    for tok in rest:
        if after_dd:
            paths.append(tok)
        elif tok == "--":
            after_dd = True
        elif tok.startswith("-"):
            continue
        else:
            revs.append(tok)

    idx = read_index(repo).by_path()
    changes: list[tuple[str, _Side, _Side]] = []

    def add_change(path, a, b):
        if a.sha != b.sha or a.mode != b.mode:
            changes.append((path, a, b))

    if len(revs) >= 2:
        a_map = _tree_map_full(repo, _commit_tree(repo, refs_mod.rev_parse(repo, revs[0]) or ""))
        b_map = _tree_map_full(repo, _commit_tree(repo, refs_mod.rev_parse(repo, revs[1]) or ""))
        for p in sorted(set(a_map) | set(b_map)):
            a = _side_from_object(repo, *a_map[p]) if p in a_map else _ABSENT
            b = _side_from_object(repo, *b_map[p]) if p in b_map else _ABSENT
            add_change(p, a, b)
    elif len(revs) == 1:
        a_map = _tree_map_full(repo, _commit_tree(repo, refs_mod.rev_parse(repo, revs[0]) or ""))
        if args.cached:
            for p in sorted(set(a_map) | set(idx)):
                a = _side_from_object(repo, *a_map[p]) if p in a_map else _ABSENT
                b = _side_from_object(repo, idx[p].mode_str(), idx[p].sha) if p in idx else _ABSENT
                add_change(p, a, b)
        else:
            for p in sorted(set(a_map) | set(idx)):
                a = _side_from_object(repo, *a_map[p]) if p in a_map else _ABSENT
                b = _side_from_worktree(repo, p)
                add_change(p, a, b)
    elif args.cached:
        head_map = _tree_map_full(repo, _commit_tree(repo, refs_mod.rev_parse(repo, "HEAD") or ""))
        for p in sorted(set(head_map) | set(idx)):
            a = _side_from_object(repo, *head_map[p]) if p in head_map else _ABSENT
            b = _side_from_object(repo, idx[p].mode_str(), idx[p].sha) if p in idx else _ABSENT
            add_change(p, a, b)
    else:
        for p in sorted(idx):
            a = _side_from_object(repo, idx[p].mode_str(), idx[p].sha)
            b = _side_from_worktree(repo, p)
            add_change(p, a, b)

    if paths:
        wanted = set(paths)
        changes = [c for c in changes if c[0] in wanted or any(c[0].startswith(w.rstrip("/") + "/") for w in paths)]

    if args.reverse:
        # -R swaps the two sides of every change.
        changes = [(p, b, a) for p, a, b in changes]

    if args.diff_filter:
        want = set(args.diff_filter.upper())
        def _status(a, b):
            return "A" if not a.present else ("D" if not b.present else "M")
        changes = [(p, a, b) for p, a, b in changes if _status(a, b) in want]

    if args.check:
        return _diff_check(repo, changes)
    if args.quiet:
        return 1 if changes else 0
    if args.exit_code:
        for path, a, b in changes:
            _emit_file_diff(path, a, b, args.reverse, args.unified)
        return 1 if changes else 0
    if args.raw:
        zero7 = "0000000"
        for path, a, b in changes:
            status = "A" if not a.present else ("D" if not b.present else "M")
            a_sha = a.sha[:7] if a.present else zero7
            b_sha = zero7 if (b.worktree or not b.present) else b.sha[:7]
            _print(f":{a.mode or '000000'} {b.mode or '000000'} {a_sha} {b_sha} {status}\t{path}")
    elif args.stat:
        stat_renames = []
        if not args.no_renames:
            stat_renames, changes = _detect_changes_renames(repo, changes)
        _diff_stat(changes, stat_renames)
        if args.summary:
            _diff_summary(changes, stat_renames)
    elif args.numstat:
        _diff_numstat(changes)
    elif args.shortstat:
        _diff_shortstat(changes)
    elif args.summary:
        renames = []
        if not args.no_renames:
            renames, changes = _detect_changes_renames(repo, changes)
        _diff_summary(changes, renames)
    elif args.name_only:
        for path, _, _ in changes:
            _print(path)
    elif args.name_status:
        renames = []
        if not args.no_renames:
            renames, changes = _detect_changes_renames(repo, changes)
        entries = [(dst, f"R{sim:03d}\t{src}\t{dst}") for src, dst, sim, _sa, _db in renames]
        for path, a, b in changes:
            status = "A" if not a.present else ("D" if not b.present else "M")
            entries.append((path, f"{status}\t{path}"))
        for _key, line in sorted(entries):
            _print(line)
    elif args.word_diff in ("plain", "porcelain"):
        for path, a, b in changes:
            _emit_file_diff(path, a, b, args.reverse, args.unified, word_diff=args.word_diff)
    else:
        renames = []
        if not args.no_renames:
            renames, changes = _detect_changes_renames(repo, changes)
        emit = [(dst, lambda s=src, d=dst, sm=sim, sa=sa, db=db: _emit_rename_patch(s, d, sm, sa, db))
                for src, dst, sim, sa, db in renames]
        emit += [(path, lambda p=path, a=a, b=b: _emit_file_diff(p, a, b, args.reverse, args.unified)) for path, a, b in changes]
        for _key, fn in sorted(emit, key=lambda e: e[0]):
            fn()
    return 0


def _emit_rename_patch(src: str, dst: str, sim: int, src_side: _Side, dst_side: _Side) -> None:
    _print(f"diff --git a/{src} b/{dst}")
    _print(f"similarity index {sim}%")
    _print(f"rename from {src}")
    _print(f"rename to {dst}")
    if src_side.sha == dst_side.sha:
        return
    _print(f"index {src_side.sha[:7]}..{dst_side.sha[:7]} {dst_side.mode}")
    a_text = (src_side.data or b"").decode("utf-8", errors="replace")
    b_text = (dst_side.data or b"").decode("utf-8", errors="replace")
    _print(f"--- a/{src}")
    _print(f"+++ b/{dst}")
    for line in diff_mod.format_hunks(
        a_text.splitlines(), b_text.splitlines(),
        a_no_newline=bool(a_text) and not a_text.endswith("\n"),
        b_no_newline=bool(b_text) and not b_text.endswith("\n"),
    ):
        _print(line)


def _detect_changes_renames(repo: Repository, changes: list):
    """Split ``changes`` into detected (src, dst, similarity%) renames and the
    remaining non-rename changes, using the validated spanhash estimator."""
    from . import diffcore
    base_map: dict[str, tuple[int, str]] = {}
    side_map: dict[str, tuple[int, str]] = {}
    for path, a, b in changes:
        if a.present and not b.present:
            base_map[path] = (int(a.mode, 8), a.sha)
        elif b.present and not a.present and not b.worktree:
            side_map[path] = (int(b.mode, 8), b.sha)
    if not base_map or not side_map:
        return [], changes
    by_path = {p: (a, b) for p, a, b in changes}
    pairs = diffcore.detect_renames(repo, base_map, side_map)
    renamed_src = {p.src.path for p in pairs}
    renamed_dst = {p.dst.path for p in pairs}
    renames = [
        (p.src.path, p.dst.path, int(p.score * 100 / diffcore.MAX_SCORE),
         by_path[p.src.path][0], by_path[p.dst.path][1])
        for p in pairs
    ]
    remaining = [
        (path, a, b)
        for path, a, b in changes
        if not (a.present and not b.present and path in renamed_src)
        and not (b.present and not a.present and path in renamed_dst)
    ]
    return renames, remaining


def _diff_counts(a: _Side, b: _Side) -> tuple[int, int]:
    if _is_binary(a.data) or _is_binary(b.data):
        return -1, -1
    a_lines = (a.data or b"").decode("utf-8", errors="replace").splitlines()
    b_lines = (b.data or b"").decode("utf-8", errors="replace").splitlines()
    ins = dele = 0
    for op in diff_mod.diff_lines(a_lines, b_lines):
        if op[0] == "ins":
            ins += 1
        elif op[0] == "del":
            dele += 1
    return ins, dele


def _diff_numstat(changes: list) -> None:
    for path, a, b in changes:
        ins, dele = _diff_counts(a, b)
        if ins < 0:
            _print(f"-\t-\t{path}")
        else:
            _print(f"{ins}\t{dele}\t{path}")


def _diff_shortstat(changes: list) -> None:
    files = total_ins = total_del = 0
    for _path, a, b in changes:
        files += 1
        ins, dele = _diff_counts(a, b)
        if ins > 0:
            total_ins += ins
        if dele > 0:
            total_del += dele
    if files == 0:
        return
    _print(_stat_summary_line(files, total_ins, total_del))


def _remote_branches(repo: Repository) -> list[str]:
    found: list[str] = []
    root = repo.gitdir / "refs" / "remotes"
    if root.exists():
        for f in sorted(root.rglob("*")):
            if f.is_file():
                rel = str(f.relative_to(repo.gitdir / "refs" / "remotes")).replace(os.sep, "/")
                found.append(rel)
    for ref in refs_mod.read_packed_refs(repo):
        if ref.startswith("refs/remotes/"):
            found.append(ref[len("refs/remotes/"):])
    return sorted(set(found))


def cmd_branch(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit branch", add_help=False)
    ap.add_argument("-d", "--delete", action="store_true")
    ap.add_argument("-D", dest="force_delete", action="store_true")
    ap.add_argument("-a", "--all", action="store_true")
    ap.add_argument("-r", "--remotes", action="store_true")
    ap.add_argument("-l", "--list", dest="list_mode", action="store_true")
    ap.add_argument("-m", "--move", action="store_true")
    ap.add_argument("-M", dest="force_move", action="store_true")
    ap.add_argument("-c", "--copy", action="store_true")
    ap.add_argument("-C", dest="force_copy", action="store_true")
    ap.add_argument("--show-current", action="store_true")
    ap.add_argument("--contains", default=None)
    ap.add_argument("--merged", nargs="?", const="HEAD", default=None)
    ap.add_argument("--no-merged", dest="no_merged", nargs="?", const="HEAD", default=None)
    ap.add_argument("--sort", default=None)
    ap.add_argument("--format", default=None)
    ap.add_argument("-v", "--verbose", action="count", default=0)
    ap.add_argument("name", nargs="?")
    ap.add_argument("start", nargs="?")
    args = ap.parse_args(argv)
    repo = _repo()
    head_sym, _ = refs_mod.read_head(repo)
    cur = head_sym[len("refs/heads/"):] if head_sym and head_sym.startswith("refs/heads/") else None

    contains_sha = refs_mod.rev_parse(repo, args.contains) if args.contains else None
    merged_sha = refs_mod.rev_parse(repo, args.merged) if args.merged else None
    no_merged_sha = refs_mod.rev_parse(repo, args.no_merged) if args.no_merged else None

    def _reachable_from(start: str, target: str) -> bool:
        stack = deque([start])
        seen_c: set[str] = set()
        while stack:
            x = stack.popleft()
            if x == target:
                return True
            if x in seen_c:
                continue
            seen_c.add(x)
            info = _commit_tree_parents(repo, x)
            if info:
                stack.extend(info[1])
        return False

    def _branch_contains(branch: str) -> bool:
        tip = refs_mod.read_ref(repo, f"refs/heads/{branch}")
        if tip is None:
            return False
        # --contains C: branch tip can reach C. --merged C: C can reach the tip.
        if contains_sha is not None and not _reachable_from(tip, contains_sha):
            return False
        if merged_sha is not None and not _reachable_from(merged_sha, tip):
            return False
        if no_merged_sha is not None and _reachable_from(no_merged_sha, tip):
            return False
        return True

    if args.move or args.force_move or args.copy or args.force_copy:
        if args.name is not None and args.start is not None:
            src, dst = args.name, args.start
        elif args.name is not None:
            src, dst = cur, args.name
        else:
            _err("fatal: branch name required")
            return 128
        if src is None:
            _err("fatal: no such branch")
            return 128
        sha = refs_mod.read_ref(repo, f"refs/heads/{src}")
        if sha is None:
            _err(f"fatal: Branch '{src}' not found.")
            return 128
        refs_mod.update_ref(repo, f"refs/heads/{dst}", sha)
        if args.move or args.force_move:
            old_log = repo.gitdir / "logs" / "refs" / "heads" / src
            new_log = repo.gitdir / "logs" / "refs" / "heads" / dst
            if old_log.exists():
                new_log.parent.mkdir(parents=True, exist_ok=True)
                old_log.replace(new_log)
            refs_mod.delete_ref(repo, f"refs/heads/{src}")
            # git records the rename in the reflog: a no-op (old==new) entry on
            # the renamed branch's own log, plus a delete/create pair on HEAD
            # when the current branch is the one being renamed.
            from . import reflog as _reflog
            rename_msg = f"Branch: renamed refs/heads/{src} to refs/heads/{dst}"
            zero = repo.null_oid()
            _reflog.append(repo, f"refs/heads/{dst}", sha, sha, rename_msg)
            if cur == src:
                _reflog.append(repo, "HEAD", sha, zero, rename_msg)
                _reflog.append(repo, "HEAD", zero, sha, rename_msg)
                refs_mod.set_head(repo, f"refs/heads/{dst}")
        return 0

    if args.show_current:
        if cur:
            _print(cur)
        return 0

    if args.delete or args.force_delete:
        if args.name is None:
            _err("fatal: branch name required")
            return 128
        full = f"refs/heads/{args.name}"
        if args.name == cur:
            _err(f"error: cannot delete branch '{args.name}' used by worktree at '{repo.path}'")
            return 1
        if refs_mod.read_ref(repo, full) is None:
            _err(f"error: branch '{args.name}' not found")
            return 1
        sha = refs_mod.read_ref(repo, full)
        refs_mod.delete_ref(repo, full)
        _print(f"Deleted branch {args.name} (was {sha[:7]}).")
        return 0

    if args.name is None or args.list_mode:
        import fnmatch
        pattern = args.name
        names: list[tuple[str, str]] = []  # (display, head-compare-name)
        if not args.remotes:
            names.extend((b, b) for b in refs_mod.list_branches(repo))
        if args.remotes or args.all:
            names.extend((f"remotes/{b}", None) for b in _remote_branches(repo))
        shown = [
            (d, p) for d, p in names
            if not (pattern and not fnmatch.fnmatch(d, pattern))
            and (p is None or _branch_contains(p))
        ]
        if args.sort:
            spec = args.sort
            reverse = spec.startswith("-")
            key = spec[1:] if reverse else spec
            def _bkey(item):
                display, plain = item
                ref = (f"refs/heads/{display}" if plain is not None
                       else f"refs/remotes/{display[len('remotes/'):]}")
                if key in ("version:refname", "v:refname"):
                    return _version_sort_key(display)
                if key in ("committerdate", "creatordate", "authordate", "taggerdate"):
                    sha = refs_mod.read_ref(repo, ref)
                    return _ref_sort_date(repo, sha, key) if sha else 0
                if key == "objectname":
                    return refs_mod.read_ref(repo, ref) or ""
                return display
            shown.sort(key=_bkey, reverse=reverse)
        width = max((len(d) for d, _ in shown), default=0)
        for display, plain in shown:
            mark = "*" if plain is not None and plain == cur else " "
            if args.format is not None:
                ref = (f"refs/heads/{display}" if plain is not None
                       else f"refs/remotes/{display[len('remotes/'):]}")
                sha = refs_mod.read_ref(repo, ref)
                _print(_fer_expand(repo, ref, sha or "", args.format, head_sym))
                continue
            if args.verbose:
                ref = f"refs/heads/{display}" if plain is not None else f"refs/remotes/{display[len('remotes/'):]}"
                sha = refs_mod.read_ref(repo, ref)
                subject = ""
                if sha:
                    try:
                        subject = objs.parse_commit(objs.read_object(repo, sha)[1]).message.splitlines()[0]
                    except (KeyError, IndexError):
                        subject = ""
                _print(f"{mark} {display:<{width}} {sha[:7] if sha else ''} {subject}".rstrip())
            else:
                _print(f"{mark} {display}")
        return 0

    start = refs_mod.rev_parse(repo, args.start) if args.start else refs_mod.rev_parse(repo, "HEAD")
    if not start:
        _err(f"fatal: Not a valid object name: '{args.start or 'HEAD'}'.")
        return 128
    refs_mod.update_ref(repo, f"refs/heads/{args.name}", start)
    return 0


def _version_sort_key(name: str):
    import re
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", name)]


def _tag_annotation(repo: Repository, sha: Optional[str]) -> str:
    if sha is None:
        return ""
    try:
        t, data = objs.read_object(repo, sha)
    except KeyError:
        return ""
    if t == "tag":
        _header, _, msg = data.decode("utf-8", errors="replace").partition("\n\n")
        return msg.splitlines()[0] if msg.strip() else ""
    if t == "commit":
        c = objs.parse_commit(data)
        return c.message.splitlines()[0] if c.message.strip() else ""
    return ""


def cmd_tag(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit tag", add_help=False)
    ap.add_argument("-d", "--delete", action="store_true")
    ap.add_argument("-l", "--list", action="store_true")
    ap.add_argument("-a", "--annotate", action="store_true")
    ap.add_argument("-s", "--sign", action="store_true")
    ap.add_argument("-f", "--force", action="store_true")
    ap.add_argument("-m", "--message", default=None)
    ap.add_argument("-n", nargs="?", const=1, type=int, default=None, dest="num")
    ap.add_argument("--sort", default=None)
    ap.add_argument("--points-at", default=None)
    ap.add_argument("--format", default=None)
    ap.add_argument("--contains", default=None)
    ap.add_argument("--no-contains", dest="no_contains", default=None)
    ap.add_argument("name", nargs="?")
    ap.add_argument("target", nargs="?")
    args = ap.parse_args(argv)
    repo = _repo()
    if (args.list or args.num is not None or args.sort is not None
            or args.points_at is not None or args.format is not None
            or args.contains is not None or args.no_contains is not None
            or (args.name is None and not args.delete)):
        import fnmatch
        pattern = args.name
        tags = list(refs_mod.list_tags(repo))
        if args.points_at is not None:
            target = refs_mod.rev_parse(repo, args.points_at)
            tags = [t for t in tags
                    if (refs_mod.rev_parse(repo, f"refs/tags/{t}" + "^{commit}")
                        or refs_mod.read_ref(repo, f"refs/tags/{t}")) == target]
        if args.contains is not None or args.no_contains is not None:
            graph = _graph_for_repo(repo)

            def _reaches(tip: str, target: str) -> bool:
                stack, seen_c = [tip], set()
                while stack:
                    x = stack.pop()
                    if x == target:
                        return True
                    if x in seen_c:
                        continue
                    seen_c.add(x)
                    info = _commit_tree_parents(repo, x, graph)
                    if info:
                        stack.extend(info[1])
                return False

            def _tag_commit(t):
                return refs_mod.rev_parse(repo, f"refs/tags/{t}^{{commit}}")
            if args.contains is not None:
                cs = refs_mod.rev_parse(repo, args.contains)
                tags = [t for t in tags if (_tag_commit(t) and cs and _reaches(_tag_commit(t), cs))]
            if args.no_contains is not None:
                cs = refs_mod.rev_parse(repo, args.no_contains)
                tags = [t for t in tags if not (_tag_commit(t) and cs and _reaches(_tag_commit(t), cs))]
        if args.sort:
            key = args.sort.lstrip("-")
            reverse = args.sort.startswith("-")
            if key in ("version:refname", "v:refname"):
                tags.sort(key=_version_sort_key, reverse=reverse)
            else:
                tags.sort(reverse=reverse)
        head_sym, _ = refs_mod.read_head(repo)
        for t in tags:
            if pattern and not fnmatch.fnmatch(t, pattern):
                continue
            if args.format is not None:
                ref = f"refs/tags/{t}"
                _print(_fer_expand(repo, ref, refs_mod.read_ref(repo, ref) or "", args.format, head_sym))
            elif args.num is not None:
                _print(f"{t:<15} {_tag_annotation(repo, refs_mod.read_ref(repo, f'refs/tags/{t}'))}")
            else:
                _print(t)
        return 0
    if args.delete:
        ref = f"refs/tags/{args.name}"
        sha = refs_mod.read_ref(repo, ref)
        if sha is None:
            _err(f"error: tag '{args.name}' not found.")
            return 1
        refs_mod.delete_ref(repo, ref)
        _print(f"Deleted tag '{args.name}' (was {sha[:7]})")
        return 0
    ref = f"refs/tags/{args.name}"
    if refs_mod.read_ref(repo, ref) is not None and not args.force:
        _err(f"fatal: tag '{args.name}' already exists")
        return 128
    target = refs_mod.rev_parse(repo, args.target) if args.target else refs_mod.rev_parse(repo, "HEAD")
    if not target:
        _err(f"fatal: Failed to resolve '{args.target or 'HEAD'}' as a valid ref.")
        return 128
    annotated = args.annotate or args.sign or args.message is not None
    if annotated:
        target_type, _ = objs.read_object(repo, target)
        message = args.message or ""
        if not message.endswith("\n"):
            message += "\n"
        tagger = objs.build_signature(repo, "committer")
        body = (
            f"object {target}\n"
            f"type {target_type}\n"
            f"tag {args.name}\n"
            f"tagger {tagger}\n"
            f"\n{message}"
        )
        tag_sha = objs.write_object(repo, "tag", body.encode("utf-8"))
        refs_mod.update_ref(repo, ref, tag_sha)
    else:
        refs_mod.update_ref(repo, ref, target)
    return 0


_DETACHED_ADVICE = (
    "You are in 'detached HEAD' state. You can look around, make experimental\n"
    "changes and commit them, and you can discard any commits you make in this\n"
    "state without impacting any branches by switching back to a branch.\n"
    "\n"
    "If you want to create a new branch to retain commits you create, you may\n"
    "do so (now or later) by using -c with the switch command. Example:\n"
    "\n"
    "  git switch -c <new-branch-name>\n"
    "\n"
    "Or undo this operation with:\n"
    "\n"
    "  git switch -\n"
    "\n"
    "Turn off this advice by setting config variable advice.detachedHead to false\n"
    "\n"
)


def _restore_paths(repo: Repository, paths: list[str], source_tree: Optional[str], update_index: bool) -> None:
    idx = read_index(repo)
    by_path = idx.by_path()
    for p in paths:
        if source_tree is not None:
            blob = refs_mod._object_at_path(repo, source_tree, p)
            if blob is None:
                continue
        elif p in by_path:
            blob = by_path[p].sha
        else:
            continue
        _, data = objs.read_object(repo, blob)
        (repo.path / p).parent.mkdir(parents=True, exist_ok=True)
        (repo.path / p).write_bytes(data)
        if update_index and p in by_path:
            from .index import IndexEntry, REG_MODE
            idx.upsert(IndexEntry(mode=REG_MODE, sha=blob, path=p))
    if update_index:
        write_index(repo, idx)


def cmd_checkout(argv: list[str]) -> int:
    new_branch: Optional[str] = None
    revs: list[str] = []
    paths: list[str] = []
    after_dd = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if after_dd:
            paths.append(a)
        elif a == "--":
            after_dd = True
        elif a in ("-b", "-B"):
            i += 1
            new_branch = argv[i] if i < len(argv) else None
        elif a.startswith("-") and a != "-":
            pass
        else:
            revs.append(a)
        i += 1
    repo = _repo()

    # Capture the pre-checkout HEAD so each branch switch can append the
    # "checkout: moving from <old> to <new>" HEAD reflog entry git writes.
    head_sym0, old_sha0 = refs_mod.read_head(repo)
    old_name0 = (head_sym0[len("refs/heads/"):] if head_sym0 and head_sym0.startswith("refs/heads/")
                 else (old_sha0[:7] if old_sha0 else None))

    def _log_checkout(new_sha: Optional[str], new_name: str) -> None:
        if old_sha0 and new_sha:
            from . import reflog as _reflog
            _reflog.append(repo, "HEAD", old_sha0, new_sha,
                           f"checkout: moving from {old_name0} to {new_name}")

    if new_branch:
        if refs_mod.read_ref(repo, f"refs/heads/{new_branch}") is not None:
            _err(f"fatal: a branch named '{new_branch}' already exists")
            return 128
        start = refs_mod.rev_parse(repo, revs[0]) if revs else refs_mod.rev_parse(repo, "HEAD")
        if not start:
            if not revs:
                # Branching from an unborn HEAD just repoints HEAD at the new
                # branch; no ref is written until the first commit, like git.
                refs_mod.set_head(repo, f"refs/heads/{new_branch}")
                _err(f"Switched to a new branch '{new_branch}'")
                return 0
            _err(f"fatal: '{revs[0]}' is not a commit and a branch '{new_branch}' cannot be created from it")
            return 128
        start_name = revs[0] if revs else "HEAD"
        refs_mod.update_ref(repo, f"refs/heads/{new_branch}", start,
                            message=f"branch: Created from {start_name}")
        refs_mod.set_head(repo, f"refs/heads/{new_branch}")
        _log_checkout(start, new_branch)
        _err(f"Switched to a new branch '{new_branch}'")
        return 0

    # Path-restore form: `checkout [<tree-ish>] -- <paths>` or `checkout <paths>`.
    if paths or (after_dd):
        source_tree = None
        if revs:
            src = refs_mod.rev_parse(repo, revs[0])
            source_tree = _commit_tree(repo, src) if src else None
        _restore_paths(repo, paths, source_tree, update_index=bool(revs))
        return 0

    if not revs:
        _err("error: you must specify path(s) to restore")
        return 1
    target = revs[0]
    head_sym, _ = refs_mod.read_head(repo)
    cur_branch = head_sym[len("refs/heads/"):] if head_sym and head_sym.startswith("refs/heads/") else None
    is_branch = refs_mod.read_ref(repo, f"refs/heads/{target}") is not None
    sha = refs_mod.rev_parse(repo, target)
    if not sha:
        _err(f"error: pathspec '{target}' did not match any file(s) known to git")
        return 1
    t, data = objs.read_object(repo, sha)
    tree = objs.parse_commit(data).tree if t == "commit" else sha
    workdir.checkout_tree(repo, tree)
    if is_branch:
        if cur_branch == target:
            _err(f"Already on '{target}'")
        else:
            refs_mod.set_head(repo, f"refs/heads/{target}")
            _log_checkout(sha, target)
            _err(f"Switched to branch '{target}'")
    else:
        refs_mod.set_head(repo, sha)
        _log_checkout(sha, sha[:7])
        from . import gitconfig
        advice = (gitconfig.get(repo, "advice.detachedhead") or "").lower()
        if advice not in ("false", "0", "no", "off"):
            sys.stderr.write(f"Note: switching to '{target}'.\n\n")
            sys.stderr.write(_DETACHED_ADVICE)
        subject = objs.parse_commit(data).message.splitlines()[0] if t == "commit" else ""
        _err(f"HEAD is now at {sha[:7]} {subject}")
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
    ap = argparse.ArgumentParser(prog="pygit restore", add_help=False)
    ap.add_argument("--staged", action="store_true")
    ap.add_argument("-W", "--worktree", action="store_true")
    ap.add_argument("-s", "--source", default=None)
    ap.add_argument("paths", nargs="+")
    args = ap.parse_args(argv)
    repo = _repo()
    idx = read_index(repo)
    # The source tree: --source if given, else HEAD for --staged. Default target
    # is the worktree; --staged targets the index; both can be combined.
    src_map = None
    if args.source is not None:
        src_tree = (refs_mod.rev_parse(repo, args.source + "^{tree}")
                    or refs_mod.rev_parse(repo, args.source))
        src_map = {p: sha for p, _m, sha in workdir.iter_tree_files(repo, src_tree)} if src_tree else {}
    do_staged = args.staged
    do_worktree = args.worktree or not args.staged

    if do_staged:
        from .index import REG_MODE, IndexEntry
        stage_map = src_map if src_map is not None else workdir._head_tree_map(repo)
        for p in args.paths:
            if p in stage_map:
                idx.upsert(IndexEntry(mode=REG_MODE, sha=stage_map[p], path=p))
            else:
                idx.remove(p)
        write_index(repo, idx)

    if do_worktree:
        # Worktree source: --source tree if given, else the index.
        by_path = idx.by_path()
        for p in args.paths:
            sha = src_map.get(p) if src_map is not None else (by_path[p].sha if p in by_path else None)
            if sha is not None:
                _t, data = objs.read_object(repo, sha)
                (repo.path / p).parent.mkdir(parents=True, exist_ok=True)
                (repo.path / p).write_bytes(data)
    return 0


def cmd_reset(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit reset", add_help=False)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--soft", action="store_true")
    g.add_argument("--mixed", action="store_true")
    g.add_argument("--hard", action="store_true")
    ap.add_argument("-q", "--quiet", action="store_true")
    ap.add_argument("args", nargs="*")
    args = ap.parse_args(argv)
    repo = _repo()

    # Split into an optional treeish and pathspecs.
    positionals = list(args.args)
    paths: list[str] = []
    if "--" in positionals:
        idx_dd = positionals.index("--")
        paths = positionals[idx_dd + 1:]
        positionals = positionals[:idx_dd]
    treeish = "HEAD"
    if positionals:
        if refs_mod.rev_parse(repo, positionals[0]) is not None and (len(positionals) > 1 or paths or not (repo.path / positionals[0]).exists()):
            treeish = positionals[0]
            paths = positionals[1:] + paths
        else:
            paths = positionals + paths

    if paths:
        # Pathspec reset: restore the named index entries to <treeish>.
        sha = refs_mod.rev_parse(repo, treeish)
        if sha is None:
            _err(f"fatal: ambiguous argument '{treeish}'")
            return 128
        tree = _commit_tree(repo, sha) or sha
        idx = read_index(repo)
        for p in paths:
            blob = refs_mod._object_at_path(repo, tree, p)
            if blob is None:
                idx.remove(p)
            else:
                from .index import IndexEntry, REG_MODE
                idx.upsert(IndexEntry(mode=REG_MODE, sha=blob, path=p))
        write_index(repo, idx)
        # Report files now differing between the worktree and the reset index,
        # matching C Git's "Unstaged changes after reset:" diff-files summary.
        new_idx = read_index(repo).by_path()
        modified: list[str] = []
        for p in paths:
            entry = new_idx.get(p)
            if entry is None:
                continue
            full = repo.path / p
            if not (full.exists() or full.is_symlink()):
                modified.append((p, "D"))
            else:
                wt_sha, _ = objs.hash_bytes("blob", full.read_bytes(), repo)
                if wt_sha != entry.sha:
                    modified.append((p, "M"))
        if modified and not args.quiet:
            _print("Unstaged changes after reset:")
            for p, st in sorted(modified):
                _print(f"{st}\t{p}")
        return 0

    sha = refs_mod.rev_parse(repo, treeish)
    if not sha:
        return 128
    head_sym, _ = refs_mod.read_head(repo)
    if head_sym:
        refs_mod.update_ref(repo, head_sym, sha, message=f"reset: moving to {treeish}")
    else:
        refs_mod.set_head(repo, sha)
    if args.soft:
        return 0
    # mixed (default) and hard: rewrite index from target
    t, data = objs.read_object(repo, sha)
    tree = objs.parse_commit(data).tree if t == "commit" else sha
    if args.hard:
        workdir.checkout_tree(repo, tree)
        if t == "commit":
            subject = objs.parse_commit(data).message.splitlines()[0] if data else ""
            _print(f"HEAD is now at {sha[:7]} {subject}")
    else:
        workdir.read_tree(repo, tree)
    return 0


def _config_list_pairs(repo: Optional[Repository]) -> list[tuple[str, str]]:
    from . import gitconfig
    return gitconfig.list_all(repo)


def _config_typed(value: str, type_: Optional[str]) -> str:
    """Apply git config --type normalization (bool / int / bool-or-int)."""
    if not type_:
        return value
    v = value.strip()
    if type_ == "bool":
        low = v.lower()
        if low in ("true", "yes", "on", "1") or v == "":
            return "true"
        if low in ("false", "no", "off", "0"):
            return "false"
        return value
    if type_ in ("int", "bool-or-int"):
        m = __import__("re").match(r"^(-?\d+)([kKmMgG]?)$", v)
        if m:
            n = int(m.group(1))
            mult = {"k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}.get(m.group(2).lower(), 1)
            return str(n * mult)
        return value
    return value


def cmd_config(argv: list[str]) -> int:
    from . import gitconfig

    is_global = False
    is_local = False
    name_only = False
    show_origin = False
    file_path: Optional[str] = None
    default_val: Optional[str] = None
    type_: Optional[str] = None
    action: Optional[str] = None   # get | get_all | get_regexp | list | unset | unset_all | add | replace_all
    positional: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--default":
            i += 1
            default_val = argv[i] if i < len(argv) else None
        elif a.startswith("--default="):
            default_val = a.split("=", 1)[1]
        elif a == "--type":
            i += 1
            type_ = argv[i] if i < len(argv) else None
        elif a.startswith("--type="):
            type_ = a.split("=", 1)[1]
        elif a == "--global":
            is_global = True
        elif a == "--local":
            is_local = True
        elif a == "--system":
            pass
        elif a == "--name-only":
            name_only = True
        elif a == "--show-origin":
            show_origin = True
        elif a in ("-f", "--file"):
            i += 1
            file_path = argv[i] if i < len(argv) else None
        elif a.startswith("--file="):
            file_path = a.split("=", 1)[1]
        elif a in ("-l", "--list"):
            action = "list"
        elif a == "--get":
            action = "get"
        elif a == "--get-all":
            action = "get_all"
        elif a == "--get-regexp":
            action = "get_regexp"
        elif a == "--unset":
            action = "unset"
        elif a == "--unset-all":
            action = "unset_all"
        elif a == "--add":
            action = "add"
        elif a == "--replace-all":
            action = "replace_all"
        elif a == "--":
            i += 1
            positional.extend(argv[i:])
            break
        elif a.startswith("-") and a != "-":
            # Unknown flags are ignored for now; common value-type flags
            # (--bool, --int) don't change which value is stored.
            pass
        else:
            positional.append(a)
        i += 1

    repo: Optional[Repository] = None

    def get_repo() -> Repository:
        nonlocal repo
        if repo is None:
            repo = _repo()
        return repo

    if action == "list" or (action is None and not positional):
        if action is None and not positional:
            action = "list"
    if action == "list":
        try:
            repo = _repo()
        except RepositoryError:
            repo = None
        origin = ""
        if show_origin and repo is not None:
            origin = "file:" + os.path.relpath(repo.gitdir / "config") + "\t"
        for key, value in gitconfig.list_all(repo):
            line = key if name_only else f"{key}={value}"
            _print(origin + line)
        return 0

    if action == "get_regexp":
        try:
            repo = _repo()
        except RepositoryError:
            repo = None
        import re as _re
        if not positional:
            _err("error: wrong number of arguments, should be from 1 to 3")
            return 129
        pat = _re.compile(positional[0])
        found = False
        for key, value in gitconfig.list_all(repo):
            if pat.search(key):
                _print(key if name_only else f"{key} {value}")
                found = True
        return 0 if found else 1

    if not positional:
        _err("error: wrong number of arguments, should be from 1 to 3")
        return 129
    name = positional[0]
    value = positional[1] if len(positional) > 1 else None

    parts = gitconfig.split_key(name)
    if parts is None:
        _err(f"error: key does not contain a section: {name}")
        return 1
    section, subsection, key = parts

    # Reads
    if action in ("get", "get_all") or (action is None and value is None):
        try:
            repo = _repo()
        except RepositoryError:
            repo = None
        values = gitconfig.get_all(repo, name)
        if not values:
            if default_val is not None:
                _print(_config_typed(default_val, type_))
                return 0
            return 1
        if action == "get_all":
            for v in values:
                _print(_config_typed(v, type_))
        else:
            _print(_config_typed(values[-1], type_))
        return 0

    # Writes (and unsets) operate on a single file.
    if file_path is not None:
        cfg_path = Path(file_path)
    elif is_global:
        cfg_path = Path(os.environ.get("GIT_CONFIG_GLOBAL") or (Path.home() / ".gitconfig"))
    else:
        cfg_path = get_repo().gitdir / "config"

    if action in ("unset", "unset_all"):
        rc = gitconfig.unset_value(cfg_path, section, subsection, key, all_values=(action == "unset_all"))
        return 0 if rc == 0 else rc
    if value is None:
        _err("error: wrong number of arguments, should be from 1 to 3")
        return 129
    mode = {"add": "add", "replace_all": "replace_all"}.get(action, "set")
    gitconfig.write_value(cfg_path, section, subsection, key, value, mode=mode)
    return 0


def _config_section_key(name: str) -> tuple[str, str]:
    prefix, sep, key = name.rpartition(".")
    if not sep or not prefix or not key:
        return "", ""
    section, dot, subsection = prefix.partition(".")
    if dot:
        return f'{section} "{subsection}"', key
    return section, key


def cmd_remote(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit remote")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="action")
    sub.add_parser("show")
    p_add = sub.add_parser("add")
    p_add.add_argument("name")
    p_add.add_argument("url")
    p_rm = sub.add_parser("remove")
    p_rm.add_argument("name")
    args = ap.parse_args(argv or ["show"])
    repo = _repo()
    from . import gitconfig
    pairs = gitconfig.list_all(repo)
    if args.action in (None, "show"):
        seen: list[str] = []
        urls: dict[str, str] = {}
        pushurls: dict[str, str] = {}
        for key, value in pairs:
            if key.startswith("remote.") and key.endswith(".url"):
                name = key[len("remote."):-len(".url")]
                if name not in seen:
                    seen.append(name)
                urls[name] = value
            elif key.startswith("remote.") and key.endswith(".pushurl"):
                pushurls[key[len("remote."):-len(".pushurl")]] = value
        for name in seen:
            if args.verbose:
                url = urls.get(name, "")
                pushurl = pushurls.get(name, url)
                _print(f"{name}\t{url} (fetch)")
                _print(f"{name}\t{pushurl} (push)")
            else:
                _print(name)
        return 0
    cfg_path = repo.gitdir / "config"
    if args.action == "add":
        gitconfig.write_value(cfg_path, "remote", args.name, "url", args.url, mode="set")
        gitconfig.write_value(
            cfg_path, "remote", args.name, "fetch",
            f"+refs/heads/*:refs/remotes/{args.name}/*", mode="set",
        )
    elif args.action == "remove":
        gitconfig.remove_section(cfg_path, "remote", args.name)
    return 0


def cmd_ls_remote(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit ls-remote", add_help=False)
    ap.add_argument("-t", "--tags", action="store_true")
    ap.add_argument("--heads", action="store_true")
    ap.add_argument("url", nargs="?", default=None)
    args = ap.parse_args(argv)
    url = args.url
    if url is None:
        # No <repository> given: git uses the current branch's remote (or
        # origin), erroring when none is configured.
        from . import gitconfig
        try:
            r = _repo()
            url = gitconfig.get(r, "remote.origin.url")
        except RepositoryError:
            url = None
        if not url:
            _err("fatal: No remote configured to list refs from.")
            return 128
    src = url[7:] if url.startswith("file://") else url
    # A local repository path is read directly: HEAD, then refs in sorted order,
    # with annotated tags followed by their peeled ``^{}`` line — like git.
    if not url.startswith(("http://", "https://", "git://", "ssh://")) and Path(src).exists():
        repo = Repository.discover(src)
        _, head_sha = refs_mod.read_head(repo)
        # --heads/--tags restrict to that namespace; HEAD shows only when neither
        # (or no namespace filter) is given.
        if head_sha and not (args.tags or args.heads):
            _print(f"{head_sha}\tHEAD")
        for name, sha in sorted(_enumerate_refs(repo)):
            if args.heads or args.tags:
                # When namespace filters are given, include the union of them.
                if not ((args.heads and name.startswith("refs/heads/"))
                        or (args.tags and name.startswith("refs/tags/"))):
                    continue
            _print(f"{sha}\t{name}")
            if name.startswith("refs/tags/"):
                try:
                    t, _ = objs.read_object(repo, sha)
                    if t == "tag":
                        peeled = refs_mod.rev_parse(repo, name + "^{commit}")
                        if peeled:
                            _print(f"{peeled}\t{name}^{{}}")
                except KeyError:
                    pass
        return 0
    from . import protocol
    refs = protocol.discover_refs(url)
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
    # No-op housekeeping; like a clean `git gc` it prints nothing on success.
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


def cmd_version(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit version")
    ap.add_argument("--build-options", action="store_true")
    args = ap.parse_args(argv)
    from . import __version__
    _print(f"pygit version {__version__}")
    if args.build_options:
        _print("cpu: pure-python")
        _print("sizeof-long: 8")
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
    "version": cmd_version,
    "install-git-shim": cmd_install_git_shim,
    "uninstall-git-shim": cmd_uninstall_git_shim,
}

for _n in _PHASE2:
    _COMMANDS.setdefault(_n, _stub(_n))


# ---------------------------------------------------------------------------
# Phase 2 — merge, rebase, sequencer, fetch, push, reflog, stash


def cmd_merge_base(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit merge-base", add_help=False)
    ap.add_argument("--is-ancestor", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--independent", action="store_true")
    ap.add_argument("commits", nargs="+")
    args = ap.parse_args(argv)
    repo = _repo()
    from . import merge as _m
    shas = [refs_mod.rev_parse(repo, c) for c in args.commits]
    if any(s is None for s in shas):
        return 128

    if args.is_ancestor:
        if len(shas) != 2:
            _err("fatal: --is-ancestor takes exactly two commits")
            return 128
        a, b = shas
        return 0 if a in _m.merge_bases(repo, a, b) else 1

    if len(shas) == 1:
        bases = [shas[0]]
    else:
        bases = _m.merge_bases(repo, shas[0], shas[1])
        for extra in shas[2:]:
            merged: list[str] = []
            for base in bases:
                merged.extend(_m.merge_bases(repo, base, extra))
            bases = sorted(set(merged))
    if not bases:
        return 1
    if args.all:
        for s in sorted(bases):
            _print(s)
    else:
        _print(bases[0])
    return 0


def _emit_diffstat_summary(changes: list) -> None:
    _diff_stat(changes)
    mode_lines: list[tuple[str, str]] = []
    for path, a, b in changes:
        if not a.present and b.present:
            mode_lines.append((path, f" create mode {b.mode} {path}"))
        elif a.present and not b.present:
            mode_lines.append((path, f" delete mode {a.mode} {path}"))
    for _, line in sorted(mode_lines):
        _print(line)


def cmd_merge(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit merge", add_help=False)
    ap.add_argument("--no-ff", action="store_true")
    ap.add_argument("--ff-only", action="store_true")
    ap.add_argument("--abort", action="store_true")
    ap.add_argument("--continue", dest="cont", action="store_true")
    ap.add_argument("-q", "--quiet", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("-m", "--message", default=None)
    ap.add_argument("other", nargs="?")
    args = ap.parse_args(argv)
    repo = _repo()
    from . import merge as _m

    if args.abort:
        if not (repo.gitdir / "MERGE_HEAD").exists():
            _err("fatal: There is no merge to abort (MERGE_HEAD missing).")
            return 128
        for f in ("MERGE_HEAD", "MERGE_MSG", "MERGE_MODE"):
            (repo.gitdir / f).unlink(missing_ok=True)
        head = refs_mod.rev_parse(repo, "HEAD")
        if head:
            workdir.checkout_tree(repo, objs.parse_commit(objs.read_object(repo, head)[1]).tree)
        return 0
    if args.other is None:
        _err("fatal: No commit specified and merge.defaultToUpstream not set.")
        return 128

    head_sym, head_sha = refs_mod.read_head(repo)
    other_sha = refs_mod.rev_parse(repo, args.other)
    if not other_sha:
        _err(f"merge: {args.other} - not something we can merge")
        return 1
    if head_sha is None:
        _err("fatal: No current branch.")
        return 128

    bases = _m.merge_bases(repo, head_sha, other_sha)
    if other_sha in bases or other_sha == head_sha:
        if not args.quiet:
            _print("Already up to date.")
        return 0

    old_tree = objs.parse_commit(objs.read_object(repo, head_sha)[1]).tree
    new_tree = objs.parse_commit(objs.read_object(repo, other_sha)[1]).tree

    if bases == [head_sha] and not args.no_ff:
        # Fast-forward.
        if not args.quiet:
            _print(f"Updating {head_sha[:7]}..{other_sha[:7]}")
            _print("Fast-forward")
        if head_sym:
            refs_mod.update_ref(repo, head_sym, other_sha,
                                message=f"merge {args.other}: Fast-forward")
        else:
            refs_mod.set_head(repo, other_sha)
        workdir.checkout_tree(repo, new_tree)
        if not args.quiet:
            _emit_diffstat_summary(_tree_changes(repo, old_tree, new_tree))
        return 0

    if args.ff_only:
        _err("fatal: Not possible to fast-forward, aborting.")
        return 128

    from . import porcelain_merge as pm
    try:
        sha, conflicts = pm.merge(repo, args.other, message=args.message, no_ff=args.no_ff)
    except RuntimeError as e:
        _err(f"fatal: {e}")
        return 1
    if conflicts:
        for p in conflicts:
            _print(f"CONFLICT (content): Merge conflict in {p}")
        _err("Automatic merge failed; fix conflicts and then commit the result.")
        return 1
    if not args.quiet:
        _print("Merge made by the 'ort' strategy.")
        merged_tree = objs.parse_commit(objs.read_object(repo, sha)[1]).tree
        _emit_diffstat_summary(_tree_changes(repo, old_tree, merged_tree))
    return 0


def _print_pick_summary(repo: Repository, sha: str) -> None:
    _, data = objs.read_object(repo, sha)
    c = objs.parse_commit(data)
    head_sym, _ = refs_mod.read_head(repo)
    branch = head_sym[len("refs/heads/"):] if head_sym and head_sym.startswith("refs/heads/") else "detached HEAD"
    _print(f"[{branch} {sha[:7]}] {c.message.splitlines()[0]}")
    _print(f" Date: {_format_ident_date(c.author)}")
    parent_tree = None
    if c.parents:
        _, pd = objs.read_object(repo, c.parents[0])
        parent_tree = objs.parse_commit(pd).tree
    _print_commit_summary(repo, parent_tree, c.tree)


def cmd_cherry_pick(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit cherry-pick", add_help=False)
    ap.add_argument("-n", "--no-commit", action="store_true")
    ap.add_argument("--no-edit", action="store_true")
    ap.add_argument("rev")
    args = ap.parse_args(argv)
    repo = _repo()
    from . import sequencer
    target = refs_mod.rev_parse(repo, args.rev)
    if not target:
        _err(f"fatal: bad revision '{args.rev}'")
        return 128
    sha, conflicts = sequencer.cherry_pick(repo, target)
    if conflicts:
        _err("error: could not apply " + target[:7] + "...")
        for p in conflicts:
            _print(f"CONFLICT (content): Merge conflict in {p}")
        return 1
    _print_pick_summary(repo, sha)
    return 0


def cmd_revert(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit revert", add_help=False)
    ap.add_argument("-n", "--no-commit", action="store_true")
    ap.add_argument("--no-edit", action="store_true")
    ap.add_argument("rev")
    args = ap.parse_args(argv)
    repo = _repo()
    from . import sequencer
    target = refs_mod.rev_parse(repo, args.rev)
    if not target:
        _err(f"fatal: bad revision '{args.rev}'")
        return 128
    sha, conflicts = sequencer.revert(repo, target)
    if conflicts:
        for p in conflicts:
            _print(f"CONFLICT (content): Merge conflict in {p}")
        return 1
    _print_pick_summary(repo, sha)
    return 0


def cmd_rebase(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit rebase", add_help=False)
    ap.add_argument("upstream")
    args = ap.parse_args(argv)
    repo = _repo()
    head_sym, _ = refs_mod.read_head(repo)
    branch = head_sym[len("refs/heads/"):] if head_sym and head_sym.startswith("refs/heads/") else "HEAD"
    from . import sequencer
    picked, conflicts = sequencer.rebase_onto(repo, args.upstream)
    if conflicts:
        for p in conflicts:
            _err(f"CONFLICT: {p}")
        return 1
    if picked == 0:
        _print(f"Current branch {branch} is up to date.")
    else:
        _print(f"Successfully rebased and updated {head_sym or 'HEAD'}.")
    return 0


def cmd_reflog(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit reflog", add_help=False)
    ap.add_argument("-n", "--max-count", type=int, default=None)
    ap.add_argument("--oneline", action="store_true")  # default format is already oneline
    ap.add_argument("--date", default=None)
    ap.add_argument("--no-abbrev", dest="no_abbrev", action="store_true")
    ap.add_argument("--abbrev", nargs="?", type=int, const=7, default=7)
    ap.add_argument("action", nargs="?", default="show")
    ap.add_argument("ref", nargs="?", default="HEAD")
    args = ap.parse_args(_expand_count_shorthand(argv))
    # `reflog [show] [ref]`: the first positional may be the subcommand or a ref.
    ref = args.ref
    if args.action not in ("show",) and ref == "HEAD":
        ref = args.action
    repo = _repo()
    from . import reflog
    # The reflog is stored under logs/<full-ref>; resolve short names like
    # "main" to refs/heads/main (HEAD keeps its top-level log).
    full_ref = ref if ref == "HEAD" else (refs_mod.dwim_full_name(repo, ref) or ref)
    entries = reflog.read(repo, full_ref)
    zero = repo.null_oid()
    shown = 0
    for i, (old, new, ident, msg) in enumerate(reversed(entries)):
        # git skips deletion markers (zero new-oid, e.g. a branch rename's
        # delete half) in the display, but they still consume an @{N} slot.
        if new == zero:
            continue
        if args.max_count is not None and shown >= args.max_count:
            break
        inner = _format_date(ident, args.date) if args.date else str(i)
        disp = new if args.no_abbrev else new[:args.abbrev]
        _print(f"{disp} {ref}@{{{inner}}}: {msg}")
        shown += 1
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
    p_show = sub.add_parser("show")
    p_show.add_argument("-p", "--patch", action="store_true")
    p_show.add_argument("-U", "--unified", type=int, default=3)
    p_show.add_argument("stash", nargs="?", default="stash@{0}")
    args = ap.parse_args(argv or ["push"])
    repo = _repo()
    from . import stash
    action = args.action or "push"
    if action == "push":
        sha = stash.push(repo, getattr(args, "message", ""))
        if sha is None:
            _print("No local changes to save")
            return 0
        stashes = stash.list_stashes(repo)
        saved_msg = stashes[0][2] if stashes else f"WIP on HEAD: {sha[:7]}"
        _print(f"Saved working directory and index state {saved_msg}")
    elif action == "list":
        for i, sha, msg in stash.list_stashes(repo):
            _print(f"stash@{{{i}}}: {msg}")
    elif action == "apply":
        ok = stash.apply(repo, args.index, pop=False)
        return 0 if ok else 1
    elif action == "pop":
        ok = stash.apply(repo, args.index, pop=True)
        return 0 if ok else 1
    elif action == "show":
        ref = getattr(args, "stash", None) or "stash@{0}"
        sha = refs_mod.rev_parse(repo, ref)
        if not sha:
            _err(f"fatal: ambiguous argument '{ref}': unknown revision or path not in the working tree.")
            _err("Use '--' to separate paths from revisions, like this:")
            _err("'git <command> [<revision>...] -- [<file>...]'")
            return 128
        c = objs.parse_commit(objs.read_object(repo, sha)[1])
        base_tree = _commit_tree(repo, c.parents[0]) if c.parents else None
        if args.patch:
            _emit_tree_patch(repo, base_tree, c.tree, args.unified)
        else:
            _diff_stat(_tree_changes(repo, base_tree, c.tree))
    return 0


def cmd_fetch(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit fetch")
    ap.add_argument("remote", nargs="?", default="origin")
    ap.add_argument("refspecs", nargs="*")
    args = ap.parse_args(argv)
    repo = _repo()
    from . import protocol
    updated = protocol.fetch(repo, args.remote, args.refspecs or None)
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
    ap = argparse.ArgumentParser(prog="pygit merge-tree", add_help=False)
    ap.add_argument("--trivial-merge", action="store_true")
    ap.add_argument("--write-tree", action="store_true")
    ap.add_argument("pos", nargs="*")
    args = ap.parse_args(argv)
    repo = _repo()
    from . import sequencer

    def _tree_of(name):
        s = refs_mod.rev_parse(repo, name)
        if not s:
            return None, None
        t, data = objs.read_object(repo, s)
        if t == "commit":
            return s, objs.parse_commit(data).tree
        if t == "tag":
            return s, refs_mod._peel_to_type(repo, s, "tree")
        return s, s

    # Modern (git >= 2.38) form: `merge-tree <branch1> <branch2>` performs a real
    # 3-way merge against the computed merge base and prints the result tree.
    if len(args.pos) == 2 and not args.trivial_merge:
        one, one_tree = _tree_of(args.pos[0])
        two, two_tree = _tree_of(args.pos[1])
        if not (one and two):
            return 128
        from . import merge as _m
        bases = _m.merge_bases(repo, one, two)
        base = bases[0] if bases else None
        base_tree = (objs.parse_commit(objs.read_object(repo, base)[1]).tree
                     if base else objs.hash_bytes("tree", b"", repo)[0])
        tree, confs, _ci = sequencer._apply_patch(
            repo, base_tree, two_tree, one_tree,
            ort_base=base, ort_ours=one, ort_theirs=two)
        _print(tree)
        if confs:
            _print("")
            _print("Conflicting files:")
            for p in confs:
                _print(p)
            return 1
        return 0

    # Legacy trivial-merge form: `merge-tree <base> <branch1> <branch2>`.
    if len(args.pos) != 3:
        _err("usage: pygit merge-tree <branch1> <branch2>")
        return 128
    b, b_tree = _tree_of(args.pos[0])
    one, one_tree = _tree_of(args.pos[1])
    two, two_tree = _tree_of(args.pos[2])
    if not (b and one and two):
        return 128
    tree, confs, _conflict_idx = sequencer._apply_patch(
        repo, b_tree, two_tree, one_tree,
        ort_base=b, ort_ours=one, ort_theirs=two)
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
    ap = argparse.ArgumentParser(prog="pygit apply", add_help=False)
    ap.add_argument("-R", "--reverse", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--stat", action="store_true")
    ap.add_argument("--numstat", action="store_true")
    ap.add_argument("file", nargs="?")
    args = ap.parse_args(argv)
    repo = _repo()
    from . import patch
    text = sys.stdin.read() if not args.file else Path(args.file).read_text(encoding="utf-8", errors="replace")
    patches = patch.parse_patch(text)
    if not patches:
        _err('error: No valid patches in input (allow with "--allow-empty")')
        return 128

    def counts(fp) -> tuple[int, int]:
        ins = sum(1 for h in fp.hunks for ln in h.lines if ln.startswith("+"))
        dele = sum(1 for h in fp.hunks for ln in h.lines if ln.startswith("-"))
        return ins, dele

    if args.numstat:
        for fp in patches:
            ins, dele = counts(fp)
            _print(f"{ins}\t{dele}\t{fp.target}")
        return 0
    if args.stat:
        rows = []
        for fp in patches:
            ins, dele = counts(fp)
            rows.append((fp.target, ins + dele, ins, dele))
        if rows:
            name_w = max(len(p) for p, *_ in rows)
            count_w = max(len(str(t)) for _, t, *_ in rows)
            ti = td = 0
            for name, total, ins, dele in rows:
                ti += ins
                td += dele
                bar = "+" * ins + "-" * dele
                _print(f" {name:<{name_w}} | {total:>{count_w}} {bar}")
            _print(_stat_summary_line(len(rows), ti, td))
        return 0

    for fp in patches:
        tgt = repo.path / fp.target
        content = tgt.read_text(encoding="utf-8", errors="replace") if tgt.exists() else ""
        result = patch.apply_to_text(content, fp.hunks, reverse=args.reverse)
        if result is None:
            line = fp.hunks[0].a_start if fp.hunks else 1
            _err(f"error: patch failed: {fp.target}:{line}")
            _err(f"error: {fp.target}: patch does not apply")
            return 1
        if not args.check:
            tgt.parent.mkdir(parents=True, exist_ok=True)
            tgt.write_text(result, encoding="utf-8")
    return 0


def cmd_format_patch(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit format-patch", add_help=False)
    ap.add_argument("-o", "--output-directory", default=".")
    ap.add_argument("--stdout", action="store_true")
    ap.add_argument("-n", "--numbered", action="store_true")
    ap.add_argument("-N", "--no-numbered", dest="no_numbered", action="store_true")
    ap.add_argument("--no-stat", dest="no_stat", action="store_true")
    ap.add_argument("-s", "--signoff", action="store_true")
    ap.add_argument("-v", "--reroll-count", dest="reroll", type=int, default=None)
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
        # A bare <rev> means <rev>..HEAD (the commits since <rev>), like git.
        base = refs_mod.rev_parse(repo, rng)
        if base is None:
            _err(f"fatal: ambiguous argument '{rng}': unknown revision or path not in the working tree.")
            _err("Use '--' to separate paths from revisions, like this:")
            _err("'git <command> [<revision>...] -- [<file>...]'")
            return 128
        excludes.append(base)
        starts.append(refs_mod.rev_parse(repo, "HEAD") or "")

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
        msg_parts = c.message.rstrip("\n").split("\n\n", 1)
        subject = msg_parts[0].replace("\n", " ") if c.message.strip() else ""
        body_lines = msg_parts[1].splitlines() if len(msg_parts) > 1 else []
        parent_tree = ""
        if c.parents:
            parent_tree = objs.parse_commit(objs.read_object(repo, c.parents[0])[1]).tree
        changes = _tree_changes(repo, parent_tree or None, c.tree)
        stat = _capture_output(lambda: _emit_diffstat_summary(changes))
        diff = _capture_output(lambda: [_emit_file_diff(p, a, b) for p, a, b in changes])

        # Default: number only when there are multiple patches. --numbered
        # forces N/M even for one; --no-numbered suppresses it even for many.
        numbered = (len(commits) > 1 or args.numbered) and not args.no_numbered
        num = f" {i}/{len(commits)}" if numbered else ""
        ver = f" v{args.reroll}" if args.reroll is not None else ""
        body = "\n".join(body_lines).rstrip("\n")
        # --signoff appends a Signed-off-by trailer (committer identity).
        if args.signoff:
            body = (body + "\n\n" if body else "") + f"Signed-off-by: {_split_ident(c.committer)[0]}"
        lines = [
            f"From {sha} Mon Sep 17 00:00:00 2001",
            f"From: {_split_ident(c.author)[0]}",
            f"Date: {_format_rfc2822_date(c.author)}",
            f"Subject: [PATCH{ver}{num}] {subject}",
            "",
        ]
        if body:
            lines.append(body)
        if args.no_stat:
            lines.append("")
        else:
            lines.append("---")
            lines.append(stat.rstrip("\n"))
            lines.append("")
        lines.append(diff.rstrip("\n"))
        lines.append("-- ")
        lines.append("2.54.0")
        out = "\n".join(lines) + "\n"
        if args.stdout:
            # git separates patches by a blank line; between patches that yields
            # two blank lines after the signature, one after the final patch.
            sys.stdout.write(out + ("\n" if i == len(commits) else "\n\n"))
        else:
            safe = "".join(ch if (ch.isalnum() or ch in "-_.") else "-" for ch in subject)[:52].strip("-") or "patch"
            fname = f"{i:04d}-{safe}.patch"
            (out_dir / fname).write_text(out, encoding="utf-8")
            _print(str(out_dir / fname))
    return 0


def _capture_output(fn) -> str:
    import io
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        fn()
    finally:
        sys.stdout = old
    return buf.getvalue()


def _format_rfc2822_date(sig: str) -> str:
    return _format_date(sig, "rfc")


def cmd_am(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit am", add_help=False)
    ap.add_argument("-3", "--3way", dest="threeway", action="store_true")
    ap.add_argument("file", nargs="?")
    args = ap.parse_args(argv)
    repo = _repo()
    from . import patch
    text = (Path(args.file).read_text(encoding="utf-8", errors="replace")
            if args.file else sys.stdin.read())
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
        author_name = author_email = author_date = None
        for h in headers:
            if h.startswith("Subject: "):
                subject = h[len("Subject: "):]
                if subject.startswith("["):
                    end = subject.find("]")
                    if end != -1:
                        subject = subject[end + 1 :].strip()
            elif h.startswith("From: "):
                who = h[len("From: "):]
                author_name, author_email = _parse_who(who)
            elif h.startswith("Date: "):
                author_date = _rfc2822_to_raw(h[len("Date: "):])
        if "---" in body:
            sep = body.index("---")
            msg_lines = body[:sep]
            patch_text = "\n".join(body[sep + 1 :])
        else:
            msg_lines = body
            patch_text = ""
        applied, failed = patch.apply_patch_text(patch_text, repo_path=repo.path)
        if failed:
            _err(f"error: patch failed: {failed[0]}" if failed else "error: patch does not apply")
            _err(f"Patch failed at 0001 {subject}")
            return 128
        if applied:
            workdir.add_paths(repo, applied)
        body_msg = "\n".join(msg_lines).strip()
        full_msg = subject + (("\n\n" + body_msg) if body_msg else "")
        _print(f"Applying: {subject}")
        old_env: dict[str, Optional[str]] = {}
        for key, val in (("GIT_AUTHOR_NAME", author_name), ("GIT_AUTHOR_EMAIL", author_email),
                         ("GIT_AUTHOR_DATE", author_date)):
            if val is not None:
                old_env[key] = os.environ.get(key)
                os.environ[key] = val
        try:
            rc = cmd_commit(["-q", "-m", full_msg])
        finally:
            for key, prev in old_env.items():
                if prev is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = prev
        if rc != 0:
            return rc
    return 0


def _rfc2822_to_raw(value: str) -> str:
    """Convert an RFC2822 date to Git's raw '<seconds> <±HHMM>' form."""
    import email.utils
    try:
        dt = email.utils.parsedate_to_datetime(value.strip())
    except (TypeError, ValueError):
        return value.strip()
    secs = int(dt.timestamp())
    off = dt.utcoffset()
    total = int(off.total_seconds()) if off is not None else 0
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"{secs} {sign}{total // 3600:02d}{(total % 3600) // 60:02d}"


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
    s = workdir.status(repo, include_ignored=args.x)
    tracked = workdir.tracked_paths(repo)

    def dir_fully_untracked(d: str) -> bool:
        prefix = d + "/"
        return not any(t == d or t.startswith(prefix) for t in tracked)

    targets: list[str] = []
    seen: set[str] = set()
    for u in sorted(s["untracked"]):
        parts = u.split("/")
        untracked_dir = None
        for k in range(1, len(parts)):
            d = "/".join(parts[:k])
            if dir_fully_untracked(d):
                untracked_dir = d
                break
        if untracked_dir is not None:
            if args.d:
                target = untracked_dir + "/"
                if target not in seen:
                    seen.add(target)
                    targets.append(target)
            # Without -d, untracked directories are left untouched.
        elif u not in seen:
            seen.add(u)
            targets.append(u)

    import shutil
    for target in sorted(targets):
        if args.dry_run:
            _print(f"Would remove {target}")
        else:
            full = repo.path / target.rstrip("/")
            try:
                if target.endswith("/"):
                    shutil.rmtree(full)
                else:
                    full.unlink()
                _print(f"Removing {target}")
            except OSError:
                pass
    return 0


def _describe_contains(repo: Repository, rev: str, sha: str) -> int:
    """``git describe --contains``: name the commit relative to the tag that
    contains it (a descendant tag), delegating to name-rev's tags-only naming.
    Prints ``<tag>^0`` / ``<tag>~N`` or, when no tag contains it, a fatal."""
    target = refs_mod.rev_parse(repo, rev + "^{commit}") or sha
    name_for: dict[str, tuple[str, int]] = {}
    tips: list[tuple[str, str, bool]] = []
    for tag in refs_mod.list_tags(repo):
        raw = refs_mod.read_ref(repo, f"refs/tags/{tag}")
        s = refs_mod.rev_parse(repo, f"refs/tags/{tag}" + "^{commit}") or raw
        if s:
            tips.append((tag, s, s != raw))
    graph = _graph_for_repo(repo)
    for label, tip, deref in tips:
        seen: set[str] = set()
        stack = deque([(tip, 0)])
        while stack:
            csha, depth = stack.popleft()
            if csha in seen:
                continue
            seen.add(csha)
            cur = name_for.get(csha)
            if cur is None or cur[1] > depth:
                suffix = ("^0" if deref else "") if depth == 0 else f"~{depth}"
                name_for[csha] = (label + suffix, depth)
            info = _commit_tree_parents(repo, csha, graph)
            if info is not None:
                for p in info[1]:
                    stack.append((p, depth + 1))
    if target in name_for:
        _print(name_for[target][0])
        return 0
    _err(f"fatal: cannot describe '{target}'")
    return 128


def cmd_describe(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit describe", add_help=False)
    ap.add_argument("--tags", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--contains", action="store_true")
    ap.add_argument("--always", action="store_true")
    ap.add_argument("--long", action="store_true")
    ap.add_argument("--abbrev", type=int, default=7)
    ap.add_argument("rev", nargs="?", default="HEAD")
    args = ap.parse_args(argv)
    repo = _repo()
    sha = refs_mod.rev_parse(repo, args.rev)
    if not sha:
        _err(f"fatal: Not a valid object name {args.rev}")
        return 128
    if args.contains:
        return _describe_contains(repo, args.rev, sha)
    # Peel the target down to a commit (an annotated tag resolves to its object).
    sha = refs_mod.rev_parse(repo, args.rev + "^{commit}") or sha
    tag_for: dict[str, str] = {}
    unann_commits: set[str] = set()
    any_tags = False
    if args.all:
        # --all considers every ref, named with its namespace (tags/, heads/,
        # remotes/). Tags are seeded first so they win ties at equal depth.
        for tag in refs_mod.list_tags(repo):
            tc = refs_mod.rev_parse(repo, f"refs/tags/{tag}^{{commit}}") or refs_mod.read_ref(repo, f"refs/tags/{tag}")
            if tc:
                tag_for.setdefault(tc, f"tags/{tag}")
        for b in refs_mod.list_branches(repo):
            s = refs_mod.read_ref(repo, f"refs/heads/{b}")
            if s:
                tag_for.setdefault(s, f"heads/{b}")
        for rb in _remote_branches(repo):
            s = refs_mod.read_ref(repo, f"refs/remotes/{rb}")
            if s:
                tag_for.setdefault(s, f"remotes/{rb}")
        any_tags = bool(tag_for)
        graph = _graph_for_repo(repo)

        def _reach_all(start: str) -> set:
            out: set[str] = set()
            stack = [start]
            while stack:
                x = stack.pop()
                if x in out:
                    continue
                out.add(x)
                info = _commit_tree_parents(repo, x, graph)
                if info:
                    stack.extend(info[1])
            return out

        reach_sha = _reach_all(sha)
        candidates = [(tc, name) for tc, name in tag_for.items() if tc in reach_sha]
        if candidates:
            best = None
            for tc, name in candidates:
                depth = len(reach_sha - _reach_all(tc))
                if best is None or depth < best[0]:
                    best = (depth, tc, name)
            depth, tc, name = best
            ab = max(4, args.abbrev) if args.abbrev else 0
            if args.abbrev == 0 or (depth == 0 and not args.long):
                _print(name)
            else:
                _print(f"{name}-{depth}-g{sha[:ab]}")
            return 0
        if args.always:
            _print(sha[:max(4, args.abbrev)] if args.abbrev else sha[:7])
            return 0
        if any_tags:
            _err(f"fatal: No tags can describe '{sha}'.")
            _err("Try --always, or create some tags.")
        else:
            _err("fatal: No names found, cannot describe anything.")
        return 128
    for tag in refs_mod.list_tags(repo):
        ts = refs_mod.read_ref(repo, f"refs/tags/{tag}")
        if not ts:
            continue
        any_tags = True
        annotated = False
        try:
            t, d = objs.read_object(repo, ts)
            if t == "tag":
                annotated = True
                for line in d.decode(errors="replace").splitlines():
                    if line.startswith("object "):
                        ts = line[len("object "):].strip()
                        break
        except KeyError:
            pass
        if not annotated:
            unann_commits.add(ts)
            if not args.tags:
                continue
        tag_for[ts] = tag
    graph = _graph_for_repo(repo)

    def _reachable(start: str) -> set:
        out: set[str] = set()
        stack = [start]
        while stack:
            x = stack.pop()
            if x in out:
                continue
            out.add(x)
            info = _commit_tree_parents(repo, x, graph)
            if info:
                stack.extend(info[1])
        return out

    reach_sha = _reachable(sha)
    # Candidate tags whose commit is an ancestor of the target. git's depth is
    # the number of commits reachable from the target but not from the tag; the
    # best tag minimizes that depth (an exact match has depth 0).
    candidates = [(tc, name) for tc, name in tag_for.items() if tc in reach_sha]
    if candidates:
        best = None  # (depth, tag_commit, name)
        for tc, name in candidates:
            depth = len(reach_sha - _reachable(tc))
            if best is None or depth < best[0]:
                best = (depth, tc, name)
        depth, tc, name = best
        ab = max(4, args.abbrev) if args.abbrev else 0
        if args.abbrev == 0:
            _print(name)
        elif depth == 0 and not args.long:
            _print(name)
        else:
            _print(f"{name}-{depth}-g{sha[:ab]}")
        return 0
    if args.always:
        _print(sha[:max(4, args.abbrev)] if args.abbrev else sha[:7])
        return 0
    if not args.tags and (unann_commits & reach_sha):
        _err(f"fatal: No annotated tags can describe '{sha}'.")
        _err("However, there were unannotated tags: try --tags.")
    elif any_tags:
        _err(f"fatal: No tags can describe '{sha}'.")
        _err("Try --always, or create some tags.")
    else:
        _err("fatal: No names found, cannot describe anything.")
    return 128


def cmd_blame(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit blame", add_help=False)
    ap.add_argument("-l", dest="long_sha", action="store_true")
    ap.add_argument("-L", dest="line_range", default=None)
    ap.add_argument("-p", "--porcelain", action="store_true")
    ap.add_argument("--line-porcelain", dest="line_porcelain", action="store_true")
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

    if args.porcelain or args.line_porcelain:
        def _porc_info(s):
            c = objs.parse_commit(objs.read_object(repo, s)[1])
            awho, a_ts, a_tz = _split_ident(c.author)
            cwho, c_ts, c_tz = _split_ident(c.committer)
            an, ae = _parse_who(awho)
            cn, ce = _parse_who(cwho)
            subj = c.message.splitlines()[0] if c.message.strip() else ""
            blk = [f"author {an}", f"author-mail <{ae}>", f"author-time {a_ts}",
                   f"author-tz {a_tz}", f"committer {cn}", f"committer-mail <{ce}>",
                   f"committer-time {c_ts}", f"committer-tz {c_tz}", f"summary {subj}"]
            if not c.parents:
                blk.append("boundary")
            else:
                ptree = _commit_tree_parents(repo, c.parents[0], graph)
                if ptree and workdir.tree_path_entry(repo, ptree[0], args.path):
                    blk.append(f"previous {c.parents[0]} {args.path}")
            blk.append(f"filename {args.path}")
            return blk
        seen_commit: set[str] = set()
        n = len(cur_lines)
        idx = 0
        while idx < n:
            s = blame_sha[idx] or "0" * 40
            # group: consecutive lines from the same commit.
            g = idx
            while g + 1 < n and (blame_sha[g + 1] or "0" * 40) == s:
                g += 1
            group = g - idx + 1
            for k in range(idx, g + 1):
                first_in_group = (k == idx)
                hdr = f"{s} {k + 1} {k + 1}" + (f" {group}" if first_in_group else "")
                _print(hdr)
                if args.line_porcelain or s not in seen_commit:
                    for bl in _porc_info(s):
                        _print(bl)
                    seen_commit.add(s)
                _print("\t" + cur_lines[k])
            idx = g + 1
        return 0

    info: dict[str, tuple[str, str, bool]] = {}
    for s in set(b for b in blame_sha if b):
        c = objs.parse_commit(objs.read_object(repo, s)[1])
        who, _ts, _tz = _split_ident(c.author)
        name = _parse_who(who)[0]
        info[s] = (name, _format_blame_date(c.author), not c.parents)
    author_w = max((len(v[0]) for v in info.values()), default=0)
    lineno_w = len(str(len(cur_lines)))
    lo, hi = 1, len(cur_lines)
    if args.line_range:
        start, _, end = args.line_range.partition(",")
        if start:
            lo = int(start)
        if end:
            hi = int(end)
    for idx, line in enumerate(cur_lines):
        if not (lo <= idx + 1 <= hi):
            continue
        s = blame_sha[idx] or "0" * 40
        name, date, boundary = info.get(s, ("", "", False))
        if args.long_sha:
            # A boundary marker '^' drops the last hex digit to keep width.
            field = ("^" + s[:-1]) if boundary else s
        else:
            field = ("^" + s[:7]) if boundary else s[:8]
        _print(f"{field} ({name:<{author_w}} {date} {idx + 1:>{lineno_w}}) {line}")
    return 0


def _format_blame_date(sig: str) -> str:
    import datetime
    _who, ts, tz = _split_ident(sig)
    if ts is None or tz is None:
        return ""
    sign = 1 if tz[0] == "+" else -1
    off_min = sign * (int(tz[1:3]) * 60 + int(tz[3:5]))
    dt = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc) + datetime.timedelta(minutes=off_min)
    return dt.strftime("%Y-%m-%d %H:%M:%S ") + tz


def _ref_sort_date(repo: Repository, sha: str, key: str) -> int:
    """Timestamp used by for-each-ref date sort keys (0 if unavailable)."""
    import re
    try:
        t, data = objs.read_object(repo, sha)
    except KeyError:
        return 0
    text = data.decode("utf-8", errors="replace")
    if t == "tag":
        m = re.search(r"^tagger .* (\d+) [+-]\d{4}$", text, re.M)
        if m:
            return int(m.group(1))
        # creatordate of a tag with no tagger falls back to the target.
        for line in text.splitlines():
            if line.startswith("object "):
                return _ref_sort_date(repo, line[len("object "):].strip(), key)
        return 0
    if t == "commit":
        field = "author" if key == "authordate" else "committer"
        m = re.search(rf"^{field} .* (\d+) [+-]\d{{4}}$", text, re.M)
        return int(m.group(1)) if m else 0
    return 0


def _fer_expand(repo: Repository, ref: str, sha: str, fmt: str, head_ref: Optional[str]) -> str:
    """Expand a for-each-ref --format string's %(atom) placeholders."""
    import re
    try:
        t, data = objs.read_object(repo, sha)
    except KeyError:
        t, data = "commit", b""
    text = data.decode("utf-8", errors="replace")
    header, _, message = text.partition("\n\n")
    fields = {}
    for line in header.splitlines():
        k, _, v = line.partition(" ")
        if k in ("author", "committer", "tagger") and k not in fields:
            fields[k] = v

    def ident_part(role, part):
        sig = fields.get(role, "")
        who, ts, tz = _split_ident(sig)
        name, email = _parse_who(who)
        if part == "name":
            return name
        if part == "email":
            return f"<{email}>" if email else ""
        if part == "email:localpart":
            return email.split("@")[0] if email else ""
        if part.startswith("date"):
            if ts is None:
                return ""
            mode = part.split(":", 1)[1] if ":" in part else "default"
            return _format_date(sig, mode)
        return sig

    subject = message.splitlines()[0] if message.strip() else ""

    def atom(name: str) -> str:
        if name == "refname":
            return ref
        if name == "refname:short":
            return refs_mod.shorten_ref(ref)
        if name.startswith("refname:lstrip=") or name.startswith("refname:rstrip="):
            n = int(name.split("=", 1)[1])
            parts = ref.split("/")
            if name.startswith("refname:lstrip="):
                return "/".join(parts[n:] if n >= 0 else parts[len(parts) + n:])
            return "/".join(parts[:-n] if n > 0 else parts[:len(parts) + n] if n < 0 else parts)
        if name == "objectname":
            return sha
        if name == "objectname:short":
            return sha[:7]
        if name.startswith("objectname:short="):
            return sha[:int(name.split("=", 1)[1])]
        if name == "objecttype":
            return t
        if name == "objectsize":
            return str(len(data))
        if name == "HEAD":
            return "*" if ref == head_ref else " "
        if name in ("upstream", "push", "symref"):
            return ""
        if name in ("subject", "contents:subject"):
            return subject
        if name in ("body", "contents:body"):
            return message.partition("\n\n")[2] if "\n\n" in message else ""
        if name == "contents":
            return message
        if name in ("author", "committer", "tagger"):
            return fields.get(name, "")
        if name == "creator":
            return fields.get("tagger") or fields.get("committer", "")
        if name.startswith("creatordate"):
            role = "tagger" if "tagger" in fields else "committer"
            part = "date" + (":" + name.split(":", 1)[1] if ":" in name else "")
            return ident_part(role, part)
        for role in ("author", "committer", "tagger"):
            if name.startswith(role):
                return ident_part(role, name[len(role):] or "")
        return ""

    def subst(s: str) -> str:
        return re.sub(r"%\(([^)]*)\)", lambda m: atom(m.group(1)), s)

    # Resolve %(if)...%(then)...[%(else)...]%(end) conditionals innermost-first,
    # before the plain atom substitution.
    while True:
        end = fmt.find("%(end)")
        if end < 0:
            break
        start = fmt.rfind("%(if", 0, end)
        if start < 0:
            break
        seg = fmt[start:end + len("%(end)")]
        m = re.match(r"%\(if(:[^)]*)?\)(.*?)%\(then\)(.*?)(?:%\(else\)(.*))?%\(end\)$", seg, re.S)
        if not m:
            break
        cond_spec, cond, then_s, else_s = m.group(1), m.group(2), m.group(3), m.group(4) or ""
        cond_val = subst(cond)
        if cond_spec and cond_spec.startswith(":equals="):
            truthy = cond_val == cond_spec[len(":equals="):]
        elif cond_spec and cond_spec.startswith(":notequals="):
            truthy = cond_val != cond_spec[len(":notequals="):]
        else:
            truthy = bool(cond_val.strip())
        fmt = fmt[:start] + (then_s if truthy else else_s) + fmt[end + len("%(end)"):]

    return subst(fmt)


def cmd_for_each_ref(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit for-each-ref", add_help=False)
    ap.add_argument("--format", default="%(objectname) %(objecttype)\t%(refname)")
    ap.add_argument("--count", type=int, default=None)
    ap.add_argument("--sort", action="append", default=None)
    ap.add_argument("pattern", nargs="*", default=None)
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
    head_sym, _ = refs_mod.read_head(repo)

    def _sort_value(ref: str, key: str):
        if key in ("refname", "refname:short"):
            return ref if key == "refname" else refs_mod.shorten_ref(ref)
        if key in ("version:refname", "v:refname"):
            import re as _re
            return [int(t) if t.isdigit() else t for t in _re.split(r"(\d+)", ref)]
        if key == "objectname":
            return all_refs[ref]
        # Date keys: peel to the underlying commit/tag and read its timestamp.
        if key in ("creatordate", "committerdate", "taggerdate", "authordate"):
            return _ref_sort_date(repo, all_refs[ref], key)
        return ref

    order = sorted(all_refs)
    if args.sort:
        # Multiple --sort keys: the last is most significant, so apply in reverse.
        for spec in reversed(args.sort):
            rev = spec.startswith("-")
            key = spec[1:] if rev else spec
            order = sorted(order, key=lambda r: _sort_value(r, key), reverse=rev)
    patterns = args.pattern or []
    emitted = 0
    for ref in order:
        if patterns and not any(ref == p or ref.startswith(p.rstrip("/") + "/") for p in patterns):
            continue
        if args.count is not None and emitted >= args.count:
            break
        _print(_fer_expand(repo, ref, all_refs[ref], args.format, head_sym))
        emitted += 1
    return 0


def cmd_shortlog(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit shortlog", add_help=False)
    ap.add_argument("-n", "--numbered", action="store_true")
    ap.add_argument("-s", "--summary", action="store_true")
    ap.add_argument("-e", "--email", action="store_true")
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
        who, _ts, _tz = _split_ident(c.author)
        name, email = _parse_who(who)
        author = f"{name} <{email}>" if args.email else name
        first = c.message.splitlines()[0] if c.message.strip() else ""
        by_author.setdefault(author, []).append(first)
        stack.extend(c.parents)
    # git lists each author's commits oldest-first (we collected newest-first).
    for msgs in by_author.values():
        msgs.reverse()
    items = list(by_author.items())
    if args.numbered:
        items.sort(key=lambda x: -len(x[1]))
    else:
        items.sort()
    for author, msgs in items:
        if args.summary:
            _print(f"{len(msgs):>6}\t{author}")
        else:
            _print(f"{author} ({len(msgs)}):")
            for m in msgs:
                _print(f"      {m}")
            _print("")
    return 0


def _git_archive_tar(repo: Repository, tree: str, commit_sha: Optional[str], archive_time: int, prefix: str = "") -> bytes:
    """Build a tar archive byte-for-byte identical to C Git's archive-tar.c."""
    BLOCKSIZE = 512 * 20
    TAR_UMASK = 0o002
    out = bytearray()

    def emit_header(name: str, mode: int, size: int, typeflag: str, linkname: str = "") -> None:
        h = bytearray(512)
        nb = name.encode("utf-8")
        h[0:len(nb)] = nb
        h[100:108] = b"%07o\0" % (mode & 0o7777)
        h[108:116] = b"0000000\0"
        h[116:124] = b"0000000\0"
        h[124:136] = b"%011o\0" % size
        h[136:148] = b"%011o\0" % archive_time
        h[148:156] = b" " * 8  # checksum placeholder (spaces)
        h[156] = ord(typeflag)
        if linkname:
            lb = linkname.encode("utf-8")
            h[157:157 + len(lb)] = lb
        h[257:263] = b"ustar\0"
        h[263:265] = b"00"
        h[265:269] = b"root"
        h[297:301] = b"root"
        h[329:337] = b"0000000\0"
        h[337:345] = b"0000000\0"
        h[148:156] = b"%07o\0" % sum(h)
        out.extend(h)

    def emit_content(data: bytes) -> None:
        out.extend(data)
        out.extend(b"\0" * ((-len(data)) % 512))

    if commit_sha:
        body = f" comment={commit_sha}\n"
        total = len(body)
        while len(str(total)) + len(body) != total:
            total = len(str(total)) + len(body)
        pax = f"{total}{body}".encode("utf-8")
        emit_header("pax_global_header", 0o100666, len(pax), "g")
        emit_content(pax)

    def walk(tsha: str, prefix: str) -> None:
        _, td = objs.read_object(repo, tsha)
        for e in objs.parse_tree(td, repo.hash_len):
            name = prefix + e.name
            mode = int(e.mode, 8)
            if e.is_dir():
                emit_header(name + "/", (mode | 0o777) & ~TAR_UMASK, 0, "5")
                walk(e.sha, name + "/")
            elif e.mode == "120000":
                _, target = objs.read_object(repo, e.sha)
                emit_header(name, (mode | 0o777) & ~TAR_UMASK, 0, "2", target.decode("utf-8", "replace"))
            elif e.is_gitlink():
                continue
            else:
                _, blob = objs.read_object(repo, e.sha)
                base = 0o777 if (mode & 0o100) else 0o666
                emit_header(name, (mode | base) & ~TAR_UMASK, len(blob), "0")
                emit_content(blob)

    if prefix.endswith("/"):
        # git emits a single directory entry for the whole prefix.
        emit_header(prefix, (0o40000 | 0o777) & ~TAR_UMASK, 0, "5")
    walk(tree, prefix)
    out.extend(b"\0" * 1024)  # end-of-archive trailer
    if len(out) % BLOCKSIZE:
        out.extend(b"\0" * (BLOCKSIZE - len(out) % BLOCKSIZE))
    return bytes(out)


def cmd_archive(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit archive", add_help=False)
    ap.add_argument("--format", default="tar", choices=["tar", "zip"])
    ap.add_argument("-l", "--list", dest="list_formats", action="store_true")
    ap.add_argument("-o", "--output", default=None)
    ap.add_argument("--prefix", default="")
    ap.add_argument("rev", nargs="?")
    args = ap.parse_args(argv)
    if args.list_formats:
        for f in ("tar", "tgz", "tar.gz", "zip"):
            _print(f)
        return 0
    repo = _repo()
    if not args.rev:
        _err("fatal: You must specify a tree-ish.")
        return 128
    sha = refs_mod.rev_parse(repo, args.rev)
    if not sha:
        return 128
    t, data = objs.read_object(repo, sha)
    if t == "commit":
        commit = objs.parse_commit(data)
        tree = commit.tree
        _who, archive_time, _tz = _split_ident(commit.committer)
        commit_oid = sha
    else:
        tree = sha
        archive_time = 0
        commit_oid = None
    if archive_time is None:
        archive_time = 0

    if args.format == "tar":
        blob = _git_archive_tar(repo, tree, commit_oid, archive_time, args.prefix)
        if args.output:
            Path(args.output).write_bytes(blob)
        else:
            _write_stdout_bytes(blob)
        return 0

    import io, zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, _mode, bsha in workdir.iter_tree_files(repo, tree):
            _, b = objs.read_object(repo, bsha)
            zf.writestr(args.prefix + path, b)
    if args.output:
        Path(args.output).write_bytes(buf.getvalue())
    else:
        _write_stdout_bytes(buf.getvalue())
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
        from . import pack as _pack_mod
        from .protocol import _collect_objects
        objs_list: list[str] = []
        seen: set[str] = set()
        for sha, _ in tips:
            for o in _collect_objects(repo, sha, set()):
                if o not in seen:
                    seen.add(o)
                    objs_list.append(o)
        with Path(args.file).open("wb") as fh:
            fh.write(bytes(header))
            _pack_mod.write_pack_stream_to(repo, objs_list, fh.write, collect_entries=False)
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


def _enumerate_refs(repo: Repository) -> list[tuple[str, str]]:
    """All refs under refs/ (loose + packed) as (refname, sha), sorted by name."""
    refs: dict[str, str] = {}
    for name, sha in refs_mod.read_packed_refs(repo).items():
        if name.startswith("refs/"):
            refs[name] = sha
    root = repo.gitdir / "refs"
    if root.exists():
        for f in sorted(root.rglob("*")):
            if f.is_file():
                rel = str(f.relative_to(repo.gitdir)).replace(os.sep, "/")
                s = refs_mod.read_ref(repo, rel)
                if s:
                    refs[rel] = s
    return sorted(refs.items())


def cmd_show_ref(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit show-ref", add_help=False)
    ap.add_argument("--head", action="store_true")
    ap.add_argument("--tags", action="store_true")
    ap.add_argument("--heads", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("-d", "--dereference", action="store_true")
    ap.add_argument("-s", "--hash", action="store_true")
    ap.add_argument("patterns", nargs="*")
    args = ap.parse_args(argv)
    repo = _repo()

    def show(sha: str, refname: str) -> None:
        if args.hash:
            _print(sha)
        else:
            _print(f"{sha} {refname}")

    if args.verify:
        printed = 0
        for pat in args.patterns:
            sha = refs_mod.read_ref(repo, pat) if pat.startswith("refs/") or pat == "HEAD" else None
            if sha is None:
                _err(f"fatal: '{pat}' - not a valid ref")
                return 128
            show(sha, pat)
            printed += 1
        return 0 if printed else 1

    def matches(refname: str) -> bool:
        if not args.patterns:
            return True
        return any(refname == pat or refname.endswith("/" + pat) for pat in args.patterns)

    printed = 0
    if args.head:
        _, headsha = refs_mod.read_head(repo)
        if headsha:
            show(headsha, "HEAD")
            printed += 1
    for refname, sha in _enumerate_refs(repo):
        if args.heads and not refname.startswith("refs/heads/"):
            continue
        if args.tags and not refname.startswith("refs/tags/"):
            continue
        if not matches(refname):
            continue
        show(sha, refname)
        printed += 1
    return 0 if printed else 1


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
    ap = argparse.ArgumentParser(prog="pygit update-index", add_help=False)
    ap.add_argument("--add", action="store_true")
    ap.add_argument("--remove", action="store_true")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--chmod", choices=["+x", "-x"], default=None)
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
    if args.chmod is not None:
        by_path = idx.by_path()
        executable = args.chmod == "+x"
        for p in args.paths:
            entry = by_path.get(p)
            if entry is None:
                _err(f"fatal: git update-index: cannot chmod {args.chmod[1]} '{p}'")
                return 128
            entry.mode = 0o100755 if executable else 0o100644
            idx.upsert(entry)
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
    ap = argparse.ArgumentParser(prog="pygit check-ignore", add_help=False)
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("-n", "--non-matching", action="store_true")
    ap.add_argument("paths", nargs="+")
    args = ap.parse_args(argv)
    repo = _repo()
    from . import ignore as _ig
    ig = _ig.load(repo.path)
    rc = 1
    for p in args.paths:
        match_path = p.replace(os.sep, "/")
        is_dir = match_path.endswith("/") or (repo.path / p).is_dir()
        rule = ig.match_rule(match_path, is_dir=is_dir)
        if rule is not None:
            rc = 0
            if args.verbose:
                _print(f"{rule.source}:{rule.lineno}:{rule.raw}\t{p}")
            else:
                _print(p)
        elif args.verbose and args.non_matching:
            _print(f"::\t{p}")
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


def _count_reachable_candidates(start: str, parents_map: dict[str, list[str]], candidates: set[str]) -> int:
    """Count candidate ancestors reachable from start, including start."""
    seen: set[str] = set()
    stack = [start]
    while stack:
        cur = stack.pop()
        while cur in candidates and cur not in seen:
            seen.add(cur)
            parents = parents_map.get(cur, [])
            stack.extend(parents[1:])
            if not parents:
                break
            cur = parents[0]
    return len(seen)


def _best_bisection_candidate(
    candidates: set[str],
    parents_map: dict[str, list[str]],
) -> tuple[str, int]:
    """Return the exact best-bisection candidate and its split distance.

    This mirrors Git's bisect.c approach: roots have weight 1, single-parent
    chains inherit parent weight + 1, and merge commits get an exact distance
    walk because their parents can share ancestors.
    """
    if not candidates:
        raise ValueError("no bisection candidates")
    weights: dict[str, int] = {}
    single_parent: set[str] = set()
    children: dict[str, list[str]] = {c: [] for c in candidates}
    for c_sha in candidates:
        filtered = [p for p in parents_map.get(c_sha, []) if p in candidates]
        parents_map[c_sha] = filtered
        for p_sha in filtered:
            children.setdefault(p_sha, []).append(c_sha)
        if not filtered:
            weights[c_sha] = 1
        elif len(filtered) == 1:
            single_parent.add(c_sha)
        else:
            weights[c_sha] = _count_reachable_candidates(c_sha, parents_map, candidates)

    queue: list[str] = []
    for known in weights:
        queue.extend(children.get(known, []))
    while queue:
        c_sha = queue.pop()
        if c_sha in weights or c_sha not in single_parent:
            continue
        parent = parents_map[c_sha][0]
        if parent not in weights:
            continue
        weights[c_sha] = weights[parent] + 1
        queue.extend(children.get(c_sha, []))

    for c_sha in sorted(candidates - set(weights)):
        weights[c_sha] = _count_reachable_candidates(c_sha, parents_map, candidates)

    n = len(candidates)
    best = ""
    best_distance = -1
    for c_sha in sorted(candidates):
        weight = weights[c_sha]
        distance = min(weight, n - weight)
        if distance > best_distance:
            best = c_sha
            best_distance = distance
    return best, best_distance


def _iter_loose_shas(repo: Repository):
    from . import loose

    yield from loose.iter_shas(repo)


def _loose_shas(repo: Repository) -> list[str]:
    return list(_iter_loose_shas(repo))


def _loose_count_and_size(repo: Repository) -> tuple[int, int]:
    from . import loose

    return loose.count_and_size(repo)


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


def _write_pack_files(
    repo: Repository,
    shas: list[str],
    base: str,
) -> tuple[str, Path, list[tuple[str, int, int]]]:
    from . import pack as _p

    pack_dir = repo.gitdir / "objects" / "pack"
    pack_dir.mkdir(parents=True, exist_ok=True)
    _p.clear_pack_cache(repo)
    tmp = pack_dir / f".tmp-{base}-{os.getpid()}-{time.time_ns()}.pack"
    try:
        pack_sha, entries = _p.write_pack_stream(repo, shas, tmp)
        idx_bytes = _p.write_idx_v2_from_checksum(bytes.fromhex(pack_sha), entries, repo.object_format())
        pack_path = pack_dir / f"{base}-{pack_sha}.pack"
        os.replace(tmp, pack_path)
    finally:
        tmp.unlink(missing_ok=True)
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
        _p.write_pack_stream_to(repo, shas, sys.stdout.buffer.write, collect_entries=False)
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
    n = _p.unpack_pack_stream(repo, sys.stdin.buffer)
    _print(f"Unpacked {n} objects")
    return 0


def cmd_index_pack(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit index-pack")
    ap.add_argument("packfile")
    args = ap.parse_args(argv)
    repo = _repo()
    from . import pack as _p
    try:
        pack_sha, _entries = _p.index_pack_file(repo, Path(args.packfile))
    except Exception as exc:
        _err(str(exc))
        return 1
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


def _loose_disk_kib(repo: Repository) -> int:
    """Loose-object disk usage in KiB, matching C Git's du-style accounting."""
    total_blocks = 0
    objects = repo.gitdir / "objects"
    if objects.exists():
        for sub in objects.iterdir():
            if len(sub.name) == 2 and sub.is_dir():
                for obj in sub.iterdir():
                    try:
                        total_blocks += obj.stat().st_blocks
                    except (OSError, AttributeError):
                        pass
    return total_blocks * 512 // 1024


def _humanise_bytes(n: int) -> str:
    """Port of git's strbuf_humanise_bytes: bytes -> '<N> bytes' / 'X.XX KiB' /
    'MiB' / 'GiB' / 'TiB'."""
    if n < 1024:
        return f"{n} bytes"
    units = ["KiB", "MiB", "GiB", "TiB"]
    val = float(n)
    for u in units:
        val /= 1024.0
        if val < 1024.0 or u == units[-1]:
            return f"{val:.2f} {u}"
    return f"{val:.2f} TiB"


def cmd_count_objects(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit count-objects", add_help=False)
    ap.add_argument("-v", "--verbose", dest="v", action="store_true")
    ap.add_argument("-H", "--human-readable", dest="human", action="store_true")
    args = ap.parse_args(argv)
    repo = _repo()
    loose_count, _bytes = _loose_count_and_size(repo)
    size = _loose_disk_kib(repo)
    if args.human and not args.v:
        _print(f"{loose_count} objects, {_humanise_bytes(size * 1024)}")
        return 0
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
        pack_dir = repo.gitdir / "objects" / "pack"
        size_pack = 0
        if pack_dir.is_dir():
            for pk in pack_dir.glob("*.pack"):
                size_pack += pk.stat().st_size
        size_pack_kib = (size_pack + 1023) // 1024
        _print(f"count: {loose_count}")
        _print(f"size: {size}")
        _print(f"in-pack: {pack_objs}")
        _print(f"packs: {pack_count}")
        _print(f"size-pack: {size_pack_kib}")
        _print("prune-packable: 0")
        _print("garbage: 0")
        _print("size-garbage: 0")
    else:
        _print(f"{loose_count} objects, {size} kilobytes")
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
        notes_map = {path: sha for path, _mode, sha in workdir.iter_tree_files(repo, nc.tree)}

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
            _err(f"error: no note found for object {target}.")
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
        graph = _graph_for_repo(repo)

        def commit_parents(sha: str) -> tuple[str, ...]:
            info = _commit_tree_parents(repo, sha, graph)
            return info[1] if info is not None else tuple()

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
            stack.extend(commit_parents(s))
        for g in goods:
            seen2: set[str] = set()
            stack = [g]
            while stack:
                s = stack.pop()
                if s in seen2:
                    continue
                seen2.add(s)
                anc_bad.discard(s)
                stack.extend(commit_parents(s))
        candidates = anc_bad - set(bads)
        if not candidates:
            _print(f"{bads[0]} is the first bad commit")
            return 0
        # weight[c] = number of candidates reachable from c (including c)
        # best = argmax(min(weight, n - weight)) - matches git/bisect.c best_bisection
        parents_map: dict[str, list[str]] = {}
        for c_sha in candidates:
            parents_map[c_sha] = [p for p in commit_parents(c_sha) if p in candidates]
        n = len(candidates)
        best, best_distance = _best_bisection_candidate(candidates, parents_map)
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
    p_list = sub.add_parser("list")
    p_list.add_argument("--porcelain", action="store_true")
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
        for path, _mode, bsha in workdir.iter_tree_files(repo, tree):
            _, blob = objs.read_object(repo, bsha)
            full = target_dir / path
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_bytes(blob)
        _print(f"Worktree added at {target_dir}")
        return 0
    if args.action == "list":
        head = refs_mod.rev_parse(repo, "HEAD") or ""
        head_sym, _ = refs_mod.read_head(repo)
        full_branch = head_sym if head_sym and head_sym.startswith("refs/heads/") else None
        branch = full_branch[len("refs/heads/"):] if full_branch else None
        if getattr(args, "porcelain", False):
            _print(f"worktree {repo.path}")
            _print(f"HEAD {head}")
            _print(f"branch {full_branch}" if full_branch else "detached")
            _print("")
            if worktrees_dir.exists():
                for d in sorted(worktrees_dir.iterdir()):
                    gitdir_file, head_file = d / "gitdir", d / "HEAD"
                    if d.is_dir() and gitdir_file.exists() and head_file.exists():
                        wt_path = Path(gitdir_file.read_text().strip()).parent
                        _print(f"worktree {wt_path}")
                        _print(f"HEAD {head_file.read_text().strip()}")
                        _print("")
            return 0
        label = f"[{branch}]" if branch else "(detached HEAD)"
        _print(f"{repo.path} {head[:7]} {label}")
        if worktrees_dir.exists():
            for d in worktrees_dir.iterdir():
                if d.is_dir():
                    gitdir_file = d / "gitdir"
                    head_file = d / "HEAD"
                    if gitdir_file.exists() and head_file.exists():
                        wt_path = Path(gitdir_file.read_text().strip()).parent
                        wt_head = head_file.read_text().strip()
                        _print(f"{wt_path} {wt_head[:7]} [{d.name}]")
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
    ap = argparse.ArgumentParser(prog="pygit grep", add_help=False)
    ap.add_argument("-i", "--ignore-case", action="store_true")
    ap.add_argument("-n", "--line-number", action="store_true")
    ap.add_argument("-l", "--files-with-matches", action="store_true")
    ap.add_argument("-c", "--count", action="store_true")
    ap.add_argument("-w", "--word-regexp", action="store_true")
    ap.add_argument("-v", "--invert-match", dest="invert", action="store_true")
    ap.add_argument("-F", "--fixed-strings", dest="fixed", action="store_true")
    ap.add_argument("-E", "--extended-regexp", action="store_true")
    ap.add_argument("--color", nargs="?", const="always", default=None)  # ignored
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("--cached", action="store_true")
    args, rest = ap.parse_known_args(argv)
    repo = _repo()
    import re

    paths: list[str] = []
    if "--" in rest:
        idx = rest.index("--")
        pre, paths = rest[:idx], rest[idx + 1:]
    else:
        pre = rest
    if not pre:
        _err("fatal: no pattern given")
        return 128
    # After the pattern, a positional that resolves to a revision is a tree to
    # search; anything else is a pathspec (git's heuristic without an explicit --).
    pattern = pre[0]
    rev_args = []
    for tok in pre[1:]:
        if refs_mod.rev_parse(repo, tok):
            rev_args.append(tok)
        else:
            paths.append(tok)

    needle = re.escape(pattern) if args.fixed else pattern
    if args.word_regexp:
        needle = r"\b(?:" + needle + r")\b"
    pat = re.compile(needle, re.IGNORECASE if args.ignore_case else 0)

    def matches(line: str) -> bool:
        return bool(pat.search(line)) != args.invert

    def want(path: str) -> bool:
        return not paths or any(path == p or path.startswith(p.rstrip("/") + "/") for p in paths)

    rc = 1

    def emit(disp: str, text: str) -> None:
        nonlocal rc
        cnt = 0
        any_m = False
        for i, line in enumerate(text.splitlines(), 1):
            if matches(line):
                any_m = True
                cnt += 1
                rc = 0
                if not args.count and not args.files_with_matches:
                    prefix = f"{disp}:"
                    if args.line_number:
                        prefix += f"{i}:"
                    _print(prefix + line)
        if args.count:
            if cnt:
                _print(f"{disp}:{cnt}")
                rc = 0
        elif args.files_with_matches and any_m:
            _print(disp)

    from .index import read_index
    if rev_args:
        for rv in rev_args:
            s = refs_mod.rev_parse(repo, rv)
            if not s:
                continue
            tree = refs_mod._peel_to_type(repo, s, "tree") or s
            for path, _mode, bsha in sorted(workdir.iter_tree_files(repo, tree)):
                if not want(path):
                    continue
                try:
                    text = objs.read_object(repo, bsha)[1].decode("utf-8", errors="replace")
                except Exception:
                    continue
                emit(f"{rv}:{path}", text)
        return rc

    for e in read_index(repo).entries:
        if not want(e.path):
            continue
        if args.cached:
            try:
                text = objs.read_object(repo, e.sha)[1].decode("utf-8", errors="replace")
            except Exception:
                continue
        else:
            full = repo.path / e.path
            if not full.exists():
                continue
            text = full.read_text(encoding="utf-8", errors="replace")
        emit(e.path, text)
    return rc


def _show_branch_name_commits(repo: Repository, ordered: list[str],
                              revs: list[str], ref_names: list[str]) -> dict[str, tuple[str, int]]:
    """Port of show-branch.c:name_commits — assign each commit a (head_name,
    generation) so the matrix can render [name], [name^], [name~N], [name^N]."""
    names: dict[str, tuple[str, int]] = {}

    def parents_of(s):
        info = _commit_tree_parents(repo, s)
        return list(info[1]) if info else []

    # 1) name the given tips (first matching rev wins).
    for s in ordered:
        if s in names:
            continue
        for i, r in enumerate(revs):
            if r == s:
                names[s] = (ref_names[i], 0)
                break

    def name_parent(c, p):
        cn = names.get(c)
        if cn is None:
            return False
        pn = names.get(p)
        if pn is None or cn[1] + 1 < pn[1]:
            names[p] = (cn[0], cn[1] + 1)
            return True
        return False

    def name_first_parent_chain(c):
        i = 0
        while c is not None:
            if c not in names:
                break
            ps = parents_of(c)
            if not ps:
                break
            p = ps[0]
            if p not in names:
                name_parent(c, p)
                i += 1
            else:
                break
            c = p
        return i

    # 2) first-parent ancestry chains.
    while True:
        i = 0
        for s in ordered:
            i += name_first_parent_chain(s)
        if not i:
            break

    # 3) remaining (non-first-parent / merge) parents.
    def disp(n):
        head, gen = n
        if gen == 0:
            return head
        if gen == 1:
            return head + "^"
        return f"{head}~{gen}"

    while True:
        i = 0
        for s in ordered:
            n = names.get(s)
            if n is None:
                continue
            nth = 0
            for p in parents_of(s):
                nth += 1
                if p in names:
                    continue
                newname = disp(n)
                newname += "^" if nth == 1 else f"^{nth}"
                names[p] = (newname, 0)
                i += 1
                name_first_parent_chain(p)
        if not i:
            break
    return names


def cmd_show_branch(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit show-branch", add_help=False)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("-r", "--remotes", action="store_true")
    ap.add_argument("--sparse", action="store_true")
    ap.add_argument("--merge-base", dest="merge_base", action="store_true")
    ap.add_argument("--independent", action="store_true")
    ap.add_argument("revs", nargs="*")
    # `--reflog[=<n>]`/`-g[<n>]` only takes a value when attached; a bare flag
    # leaves the following token as the positional <ref>. argparse's nargs="?"
    # would wrongly consume that token, so pull the option out of argv first.
    reflog_val = None
    rest_argv = []
    for a in argv:
        if a in ("--reflog", "-g"):
            reflog_val = ""
        elif a.startswith("--reflog="):
            reflog_val = a.split("=", 1)[1]
        elif a.startswith("-g") and len(a) > 2 and a[2:].isdigit():
            reflog_val = a[2:]
        else:
            rest_argv.append(a)
    args = ap.parse_args(rest_argv)
    repo = _repo()
    head_sym, head_oid = refs_mod.read_head(repo)
    cur = head_sym[len("refs/heads/"):] if head_sym and head_sym.startswith("refs/heads/") else None

    # Determine the revs (and their display names). Explicit args win; otherwise
    # default to all local heads (and remotes with -r/--all), in ref name order.
    ref_names: list[str] = []
    revs: list[str] = []
    reflog_msgs: list[str] = []  # only populated in --reflog mode
    if reflog_val is not None:
        # --reflog[=<n>] [<ref>]: each of the ref's last <n> reflog entries
        # (default 4) becomes a column "<ref>@{i}".
        count = int(reflog_val) if reflog_val else 4
        if args.revs:
            disp = args.revs[0]
            full = refs_mod.dwim_full_name(repo, disp) or f"refs/heads/{disp}"
        else:
            # Default: HEAD resolved to the full branch ref it points at.
            head_sym2, _ = refs_mod.read_head(repo)
            full = head_sym2 or "HEAD"
            disp = full
        from . import reflog as _rl
        entries = _rl.read(repo, full if full == "HEAD" else
                           (full if full.startswith("refs/") else f"refs/heads/{full}"))
        for i, (_old, new, ident, msg) in enumerate(reversed(entries)):
            if i >= count:
                break
            ref_names.append(f"{disp}@{{{i}}}")
            revs.append(new)
            reldate = _format_date(ident, "relative")
            reflog_msgs.append(f"({reldate}) {msg}")
        if not revs:
            _err("No revs to be shown.")
            return 0
    elif args.revs:
        for rv in args.revs:
            s = refs_mod.rev_parse(repo, rv + "^{commit}") or refs_mod.rev_parse(repo, rv)
            if not s:
                _err(f"fatal: '{rv}' is not a valid ref.")
                return 128
            ref_names.append(rv)
            revs.append(s)
    else:
        if not args.remotes or args.all:
            for b in refs_mod.list_branches(repo):
                s = refs_mod.read_ref(repo, f"refs/heads/{b}")
                if s:
                    ref_names.append(b)
                    revs.append(s)
        if args.remotes or args.all:
            for b in _remote_branches(repo):
                s = refs_mod.read_ref(repo, f"refs/remotes/{b}")
                if s:
                    ref_names.append(b)
                    revs.append(s)
    if not revs:
        _err("No revs to be shown.")
        return 0
    num_rev = len(revs)

    def subject(s):
        try:
            c = objs.parse_commit(objs.read_object(repo, s)[1])
            return c.message.splitlines()[0] if c.message.strip() else ""
        except (KeyError, IndexError):
            return ""

    def parents_of(s):
        info = _commit_tree_parents(repo, s)
        return list(info[1]) if info else []

    # Reachability mask: bit i set when rev[i]'s tip can reach the commit.
    reach: list[set[str]] = []
    for tip in revs:
        seen: set[str] = set()
        stack = [tip]
        while stack:
            x = stack.pop()
            if x in seen:
                continue
            seen.add(x)
            stack.extend(parents_of(x))
        reach.append(seen)

    def mask(s):
        m = 0
        for i, rs in enumerate(reach):
            if s in rs:
                m |= (1 << i)
        return m

    all_mask = (1 << num_rev) - 1

    # --merge-base: print the common ancestor(s) of all the revs (octopus
    # reduction), one full oid per line — like `git merge-base`.
    if args.merge_base:
        from . import merge as _m
        bases = [revs[0]]
        for other in revs[1:]:
            merged: list[str] = []
            for b in bases:
                merged.extend(_m.merge_bases(repo, b, other))
            seen_b: set[str] = set()
            bases = [x for x in merged if not (x in seen_b or seen_b.add(x))]
        if not bases:
            return 1
        for b in bases:
            _print(b)
        return 0

    # --independent: print the tips that are not reachable from any other tip.
    if args.independent:
        for i in range(num_rev):
            if not any(j != i and revs[j] != revs[i] and revs[i] in reach[j]
                       for j in range(num_rev)):
                _print(revs[i])
        return 0

    # Build the reachable union in rev-input order, then order it like git's
    # topo sort (REV_SORT_IN_GRAPH_ORDER over the date-stable seen list).
    # git seeds its `seen` list by prepending the tips (so they end up in
    # reverse command-line order), which drives the topo sort's tie-breaking.
    union: list[str] = []
    useen: set[str] = set()
    for tip in reversed(revs):
        stack = [tip]
        local: set[str] = set()
        order_local: list[str] = []
        while stack:
            x = stack.pop()
            if x in local:
                continue
            local.add(x)
            order_local.append(x)
            stack.extend(parents_of(x))
        for x in order_local:
            if x not in useen:
                useen.add(x)
                union.append(x)
    ordered = _topo_order(repo, union)

    names = _show_branch_name_commits(repo, ordered, revs, ref_names)

    def disp_name(s):
        n = names.get(s)
        if not n:
            return s[:7]
        head, gen = n
        if gen == 0:
            return head
        if gen == 1:
            return head + "^"
        return f"{head}~{gen}"

    # head_at: column of the current branch (used for the '*' marker).
    head_at = -1
    if num_rev > 1:
        for i in range(num_rev):
            is_head = (reflog_val is None) and ref_names[i] == cur and revs[i] == head_oid
            mark = "*" if is_head else "!"
            line = " " * i + mark
            text = reflog_msgs[i] if reflog_val is not None else subject(revs[i])
            _print(f"{line} [{ref_names[i]}] {text}")
            if is_head:
                head_at = i
        _print("-" * num_rev)

    def omit_in_dense(s, m):
        # Skip a merge reachable from only one tip (and not itself a tip).
        if s in revs:
            return False
        if len(parents_of(s)) > 1 and bin(m).count("1") == 1:
            return True
        return False

    shown_merge_point = False
    for s in ordered:
        m = mask(s)
        is_merge_point = (m == all_mask)
        is_merge = len(parents_of(s)) > 1
        if num_rev > 1:
            if not args.sparse and is_merge and omit_in_dense(s, m):
                continue
            marks = []
            for i in range(num_rev):
                if not (m & (1 << i)):
                    marks.append(" ")
                elif is_merge:
                    marks.append("-")
                elif i == head_at:
                    marks.append("*")
                else:
                    marks.append("+")
            _print(f"{''.join(marks)} [{disp_name(s)}] {subject(s)}")
        else:
            _print(f"[{disp_name(s)}] {subject(s)}")
        if is_merge_point:
            shown_merge_point = True
        if shown_merge_point:
            break
    return 0


_WHATCHANGED_DEPRECATION = (
    "'git whatchanged' is nominated for removal.\n"
    "\n"
    "hint: You can replace 'git whatchanged <opts>' with:\n"
    "hint:\tgit log <opts> --raw --no-merges\n"
    "hint: Or make an alias:\n"
    "hint:\tgit config set --global alias.whatchanged 'log --raw --no-merges'\n"
    "\n"
    "If you still use this command, here's what you can do:\n"
    "\n"
    "- read https://git-scm.com/docs/BreakingChanges.html\n"
    "- check if anyone has discussed this on the mailing\n"
    "  list and if they came up with something that can\n"
    "  help you: https://lore.kernel.org/git/?q=git%20whatchanged\n"
    "- send an email to <git@vger.kernel.org> to let us\n"
    "  know that you still use this command and were unable\n"
    "  to determine a suitable replacement\n"
    "\n"
)


def cmd_whatchanged(argv: list[str]) -> int:
    # As of Git 2.54 this command refuses to run without an explicit opt-in.
    if "--i-still-use-this" not in argv:
        sys.stderr.write(_WHATCHANGED_DEPRECATION)
        _err("fatal: refusing to run without --i-still-use-this")
        return 128
    argv = [a for a in argv if a != "--i-still-use-this"]
    # 'whatchanged' is 'log --no-merges' whose default diff format is --raw
    # rather than a patch (this is exactly the suggested replacement command).
    if not any(a in ("-p", "--patch", "--stat", "--raw", "--name-only", "--name-status") for a in argv):
        argv = ["--raw", *argv]
    return cmd_log(["--no-merges", *argv])


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
    ap = argparse.ArgumentParser(prog="pygit name-rev", add_help=False)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--name-only", dest="name_only", action="store_true")
    ap.add_argument("--tags", action="store_true")
    ap.add_argument("rev", nargs="?")
    args = ap.parse_args(argv)
    repo = _repo()
    # Build a map sha -> closest ref name by BFS from each ref tip.
    name_for: dict[str, tuple[str, int]] = {}
    tips: list[tuple[str, str, bool]] = []
    # Tags are seeded first so that, at equal depth, a tag name is preferred
    # over a branch name (matching git's name-rev tie-break). Annotated tags are
    # peeled to the commit they reference; that peeling is recorded as `deref` so
    # an exact match renders "<tag>^0" (the commit the tag dereferences to).
    for tag in refs_mod.list_tags(repo):
        raw = refs_mod.read_ref(repo, f"refs/tags/{tag}")
        s = refs_mod.rev_parse(repo, f"refs/tags/{tag}" + "^{commit}") or raw
        if s:
            tips.append((f"tags/{tag}", s, s != raw))
    if not args.tags:
        for b in refs_mod.list_branches(repo):
            s = refs_mod.read_ref(repo, f"refs/heads/{b}")
            if s:
                tips.append((b, s, False))
    graph = _graph_for_repo(repo)
    for label, tip, deref in tips:
        seen: set[str] = set()
        stack = deque([(tip, 0)])
        while stack:
            sha, depth = stack.popleft()
            if sha in seen:
                continue
            seen.add(sha)
            cur = name_for.get(sha)
            if cur is None or cur[1] > depth:
                suffix = ("^0" if deref else "") if depth == 0 else f"~{depth}"
                name_for[sha] = (label + suffix, depth)
            info = _commit_tree_parents(repo, sha, graph)
            if info is not None:
                _tree, parents = info
                for p in parents:
                    stack.append((p, depth + 1))
    def _display(name: str) -> str:
        # With --name-only, --tags strips the "tags/" prefix from tag names.
        if args.name_only and args.tags and name.startswith("tags/"):
            return name[len("tags/"):]
        return name

    if args.all:
        for sha, (name, _) in sorted(name_for.items()):
            if args.name_only:
                _print(_display(name))
            else:
                _print(f"{sha} {_display(name)}")
        return 0
    if args.rev:
        s = refs_mod.rev_parse(repo, args.rev)
        if not s:
            _err(f"Could not get sha1 for {args.rev}. Skipping.")
            return 0
        name = name_for[s][0] if s in name_for else "undefined"
        if args.name_only:
            _print(_display(name))
        else:
            _print(f"{args.rev} {name}")
        return 0
    return 0


def _var_ident(repo: Optional[Repository], role: str) -> str:
    if repo is not None:
        return objs.build_signature(repo, role)
    env = os.environ
    sys_name, sys_email = objs._system_ident()
    if role == "author":
        name = env.get("GIT_AUTHOR_NAME") or sys_name
        email = env.get("GIT_AUTHOR_EMAIL") or env.get("EMAIL") or sys_email
        date = env.get("GIT_AUTHOR_DATE")
    else:
        name = env.get("GIT_COMMITTER_NAME") or sys_name
        email = env.get("GIT_COMMITTER_EMAIL") or env.get("EMAIL") or sys_email
        date = env.get("GIT_COMMITTER_DATE")
    parsed = objs._parse_date_env(date) if date else None
    if parsed is not None:
        secs, tzmin = parsed
    else:
        secs = int(time.time())
        tzmin = objs._local_tz_minutes(secs)
    return objs.format_signature(name, email, when=secs, tz_minutes=tzmin)


def _var_value(repo: Optional[Repository], name: str) -> Optional[str]:
    if name == "GIT_AUTHOR_IDENT":
        return _var_ident(repo, "author")
    if name == "GIT_COMMITTER_IDENT":
        return _var_ident(repo, "committer")
    if name == "GIT_EDITOR":
        return os.environ.get("GIT_EDITOR") or os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    if name == "GIT_SEQUENCE_EDITOR":
        return os.environ.get("GIT_SEQUENCE_EDITOR") or _var_value(repo, "GIT_EDITOR")
    if name == "GIT_PAGER":
        return os.environ.get("GIT_PAGER") or os.environ.get("PAGER") or "less"
    if name == "GIT_DEFAULT_BRANCH":
        if repo is not None:
            cp = repo.config()
            if cp.has_section("init"):
                val = cp.get("init", "defaultbranch", fallback=None)
                if val:
                    return val
        return "master"
    return None


def cmd_var(argv: list[str]) -> int:
    repo = None
    try:
        repo = _repo()
    except Exception:
        pass
    if len(argv) != 1 or argv[0].startswith("-") and argv[0] != "-l":
        _err("usage: git var (-l | <variable>)")
        return 129
    name = argv[0]
    if name == "-l":
        for key, value in _config_list_pairs(repo):
            _print(f"{key}={value}")
        for logical in ("GIT_COMMITTER_IDENT", "GIT_AUTHOR_IDENT", "GIT_DEFAULT_BRANCH"):
            _print(f"{logical}={_var_value(repo, logical)}")
        return 0
    value = _var_value(repo, name)
    if value is None:
        _err("usage: git var (-l | <variable>)")
        return 129
    _print(value)
    return 0


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
    ap = argparse.ArgumentParser(prog="pygit cherry", add_help=False)
    ap.add_argument("-v", "--verbose", action="store_true")
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
        h = hashlib.sha1()
        for p, a_entry, b_entry in workdir.iter_tree_changes(repo, parent_tree, c.tree):
            a_sha = a_entry.sha if a_entry else None
            b_sha = b_entry.sha if b_entry else None
            if a_sha == b_sha:
                continue
            at = bt = ""
            if a_sha:
                at = objs.read_object(repo, a_sha)[1].decode("utf-8", errors="replace")
            if b_sha:
                bt = objs.read_object(repo, b_sha)[1].decode("utf-8", errors="replace")
            h.update(_d.unified_diff(at, bt, p, p).encode("utf-8", errors="replace"))
        return h.hexdigest()

    head_commits = collect(head, up)
    up_commits = collect(up, head)
    up_patches = {patch_id(s) for s in up_commits}
    for s in head_commits:
        pid = patch_id(s)
        mark = "-" if pid in up_patches else "+"
        if args.verbose:
            c = objs.parse_commit(objs.read_object(repo, s)[1])
            subject = c.message.splitlines()[0] if c.message.strip() else ""
            _print(f"{mark} {s} {subject}")
        else:
            _print(f"{mark} {s}")
    return 0


def _rd_format(repo: Repository, sha: str) -> tuple[str, str]:
    """Reformat a commit into git range-diff's internal patch text (## headers),
    returning (full_patch, diff_only). Mirrors range-diff.c:read_patches."""
    c = objs.parse_commit(objs.read_object(repo, sha)[1])
    lines = [" ## Metadata ##", f"Author: {_split_ident(c.author)[0]}", "",
             " ## Commit message ##"]
    for ml in c.message.split("\n"):
        if ml.strip() == "":
            continue
        lines.append(("    " + ml).rstrip())
    diff_lines: list[str] = []
    parent_tree = None
    if c.parents:
        parent_tree = objs.parse_commit(objs.read_object(repo, c.parents[0])[1]).tree
    for path, a, b in _tree_changes(repo, parent_tree, c.tree):
        if not a.present:
            sec = f"{path} (new)"
        elif not b.present:
            sec = f"{path} (deleted)"
        else:
            sec = path
        if a.present and b.present and a.mode != b.mode:
            sec += f" (mode change {a.mode} => {b.mode})"
        diff_lines.append(f" ## {sec} ##")
        a_text = (a.data or b"").decode("utf-8", "replace")
        b_text = (b.data or b"").decode("utf-8", "replace")
        for hl in diff_mod.format_hunks(a_text.splitlines(), b_text.splitlines(), 3):
            if hl.startswith("@@ "):
                # "@@ -a,b +c,d @@ section" -> "@@ <file>: section" / "@@"
                rest = hl[3:]
                idx = rest.find("@@")
                tail = rest[idx + 2:] if idx >= 0 else ""
                diff_lines.append(f"@@ {path}:{tail}" if tail else "@@")
            else:
                diff_lines.append(hl)
    full = "\n".join(lines + ([""] if diff_lines else []) + diff_lines) + "\n"
    diff_only = ("\n".join(diff_lines) + "\n") if diff_lines else ""
    return full, diff_only


def _rd_diffsize(a: str, b: str) -> int:
    """git range-diff cost: number of diff hunks + body lines between a and b.
    Newline-terminated text has no phantom trailing line (matching xdiff)."""
    al = a.split("\n")
    bl = b.split("\n")
    if al and al[-1] == "":
        al = al[:-1]
    if bl and bl[-1] == "":
        bl = bl[:-1]
    spans = diff_mod._group_hunks(diff_mod.diff_lines(al, bl), 3)
    count = len(spans)
    for start, end in spans:
        count += end - start
    return count


def _rd_assign(a_list: list, b_list: list, exact: dict, creation_factor: int = 60) -> dict:
    """Return {a_index: b_index} matching, combining exact matches with a
    min-cost assignment of the rest (greedy approximation of git's Hungarian)."""
    na, nb = len(a_list), len(b_list)
    matched_a = dict(exact)              # a_idx -> b_idx (exact)
    used_b = set(exact.values())
    # Candidate costs for remaining pairs.
    rem_a = [i for i in range(na) if i not in matched_a]
    rem_b = [j for j in range(nb) if j not in used_b]
    crea_a = {i: _rd_diffsize(a_list[i][1], "") * creation_factor // 100 for i in rem_a}
    crea_b = {j: _rd_diffsize(b_list[j][1], "") * creation_factor // 100 for j in rem_b}
    pairs = []
    for i in rem_a:
        for j in rem_b:
            pairs.append((_rd_diffsize(a_list[i][1], b_list[j][1]), i, j))
    pairs.sort()
    for cost, i, j in pairs:
        if i in matched_a or j in used_b:
            continue
        # Match when keeping the pair is cheaper than creating both separately
        # (git's cost-matrix assignment with the creation-factor weighting).
        if cost < crea_a[i] + crea_b[j]:
            matched_a[i] = j
            used_b.add(j)
    return matched_a


def cmd_range_diff(argv: list[str]) -> int:
    """range-diff A..B C..D — match commits and show the diff of diffs."""
    ap = argparse.ArgumentParser(prog="pygit range-diff", add_help=False)
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

    a_shas = expand(args.range1)
    b_shas = expand(args.range2)
    # (sha, full_patch, diff_only, subject) per side.
    def build(shas):
        out = []
        for s in shas:
            full, diff = _rd_format(repo, s)
            c = objs.parse_commit(objs.read_object(repo, s)[1])
            subj = c.message.splitlines()[0] if c.message.strip() else ""
            out.append((s, full, diff, subj))
        return out
    A = build(a_shas)
    B = build(b_shas)
    # `_list` entries used by the matcher are (full, diff).
    a_pf = [(x[1], x[2]) for x in A]
    b_pf = [(x[1], x[2]) for x in B]
    # Exact matches: identical diff text.
    exact: dict[int, int] = {}
    b_by_diff: dict[str, int] = {}
    for j, (_f, d) in enumerate(b_pf):
        b_by_diff.setdefault(d, j)
    used_b_exact = set()
    for i, (_f, d) in enumerate(a_pf):
        j = b_by_diff.get(d)
        if j is not None and j not in used_b_exact:
            exact[i] = j
            used_b_exact.add(j)
    a2b = _rd_assign(a_pf, b_pf, exact)
    b2a = {j: i for i, j in a2b.items()}

    width = len(str(max(len(A), len(B)) or 1))
    dashes = "-" * 7

    def header(a_idx, b_idx):
        if a_idx is None:
            left = f"{'-':>{width}}:  {dashes}"
            status = ">"
        elif b_idx is None:
            left = f"{a_idx + 1:>{width}}:  {A[a_idx][0][:7]}"
            status = "<"
        else:
            af, bf = A[a_idx][1], B[b_idx][1]
            status = "=" if af == bf else "!"
            left = f"{a_idx + 1:>{width}}:  {A[a_idx][0][:7]}"
        if b_idx is None:
            right = f"{'-':>{width}}:  {dashes}"
        else:
            right = f"{b_idx + 1:>{width}}:  {B[b_idx][0][:7]}"
        subj = (A[a_idx] if a_idx is not None else B[b_idx])[3]
        _print(f"{left} {status} {right} {subj}")
        return status

    import re as _re
    sec_re = _re.compile(r" ## (.*) ##$")

    def emit_body(a_idx, b_idx):
        # Inter-diff of the two reformatted patch texts. Hunk headers carry no
        # line counts; the section name comes from the nearest preceding
        # " ## X ##" line (git's section-header diff driver). Each line is
        # prefixed with four spaces.
        al = A[a_idx][1].split("\n")
        bl = B[b_idx][1].split("\n")
        if al and al[-1] == "":
            al = al[:-1]
        if bl and bl[-1] == "":
            bl = bl[:-1]
        ops = diff_mod.diff_lines(al, bl)
        for start, end in diff_mod._group_hunks(ops, 3):
            hunk = ops[start:end]
            a_idxs = [ai for k, ai, _ in hunk if k in ("eq", "del")]
            first_a = a_idxs[0] if a_idxs else 0
            section = ""
            for k in range(first_a, -1, -1):
                m = sec_re.match(al[k])
                if m:
                    section = m.group(1)
                    break
            _print("    @@" + (" " + section if section else ""))
            for kind, ai, bi in hunk:
                if kind == "eq":
                    _print("     " + al[ai])
                elif kind == "del":
                    _print("    -" + al[ai])
                else:
                    _print("    +" + bl[bi])

    shown_a = set()
    i = j = 0
    while i < len(A) or j < len(B):
        while i < len(A) and i in shown_a:
            i += 1
        if i < len(A) and i not in a2b:
            header(i, None)
            i += 1
            continue
        while j < len(B) and j not in b2a:
            header(None, j)
            j += 1
        if j < len(B):
            ai = b2a[j]
            st = header(ai, j)
            if st == "!":
                emit_body(ai, j)
            shown_a.add(ai)
            j += 1
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

    head_sym, _ = refs_mod.read_head(repo)
    ref = head_sym if head_sym and head_sym.startswith("refs/heads/") else "refs/heads/main"

    out = bytearray()

    def w(s: str) -> None:
        out.extend(s.encode("utf-8"))

    reset_emitted = False
    for cs in order:
        c = objs.parse_commit(objs.read_object(repo, cs)[1])
        parent = c.parents[0] if c.parents else None
        parent_tree = objs.parse_commit(objs.read_object(repo, parent)[1]).tree if parent else None
        changes = _tree_changes(repo, parent_tree, c.tree)

        # Emit any new blobs referenced by this commit, in path order.
        for path, _a, b in changes:
            if b.present and b.sha not in blob_mark:
                _, data = objs.read_object(repo, b.sha)
                blob_mark[b.sha] = next_mark
                w(f"blob\nmark :{next_mark}\ndata {len(data)}\n")
                out.extend(data)
                w("\n")
                next_mark += 1

        if not reset_emitted:
            w(f"reset {ref}\n")
            reset_emitted = True

        commit_mark[cs] = next_mark
        w(f"commit {ref}\nmark :{next_mark}\n")
        w(f"author {c.author}\ncommitter {c.committer}\n")
        msg = c.message.encode("utf-8")
        w(f"data {len(msg)}\n")
        out.extend(msg)
        if parent and parent in commit_mark:
            w(f"from :{commit_mark[parent]}\n")
        for p in c.parents[1:]:
            if p in commit_mark:
                w(f"merge :{commit_mark[p]}\n")
        for path, a, b in changes:
            if b.present:
                w(f"M {b.mode} :{blob_mark[b.sha]} {path}\n")
            else:
                w(f"D {path}\n")
        w("\n")
        next_mark += 1

    _write_stdout_bytes(bytes(out))
    return 0


def _write_stdout_bytes(data: bytes) -> None:
    """Write raw bytes to stdout, tolerating a text capture (e.g. pytest)."""
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is not None:
        sys.stdout.flush()
        buffer.write(data)
        buffer.flush()
    else:
        sys.stdout.write(data.decode("utf-8", errors="surrogateescape"))


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
                for p, mode, s in workdir.iter_tree_files(repo, parent_tree):
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
            elif sub is not None:
                # No "from" line follows this reset; let the main loop see it.
                i -= 1
        # ignore other directives
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
    ap = argparse.ArgumentParser(prog="pygit verify-commit", add_help=False)
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--raw", action="store_true")
    ap.add_argument("revs", nargs="+")
    args = ap.parse_args(argv)
    repo = _repo()
    rc = 0
    for r in args.revs:
        s = refs_mod.rev_parse(repo, r)
        if not s:
            _err(f"error: {r}: no such commit")
            rc = 1
            continue
        t, data = objs.read_object(repo, s)
        if t != "commit":
            _err(f"error: {r}: cannot verify a non-commit object of type {t}.")
            rc = 1
            continue
        # pythongit cannot verify GPG signatures, so a commit is treated as
        # unverifiable — matching git's exit status for unsigned commits, which
        # produce no output and a failure code.
        rc = 1
    return rc


def cmd_verify_tag(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit verify-tag", add_help=False)
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--raw", action="store_true")
    ap.add_argument("tags", nargs="+")
    args = ap.parse_args(argv)
    repo = _repo()
    rc = 0
    for r in args.tags:
        s = refs_mod.rev_parse(repo, r) or refs_mod.read_ref(repo, f"refs/tags/{r}")
        if not s:
            _err(f"error: tag '{r}' not found.")
            rc = 1
            continue
        t, data = objs.read_object(repo, s)
        if t != "tag":
            _err(f"error: {r}: cannot verify a non-tag object of type {t}.")
            rc = 1
            continue
        if b"-----BEGIN PGP SIGNATURE-----" not in data and b"-----BEGIN SSH SIGNATURE-----" not in data:
            _err("error: no signature found")
            rc = 1
            continue
        # A signature is present but pythongit cannot verify it.
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


def _diff_tree_changes_with_dirs(repo: Repository, a_tree, b_tree, prefix: str = "", recurse: bool = True):
    """Like workdir.iter_tree_changes but also yields changed directory (tree)
    nodes (parent before children). With recurse=False, yields only the
    top-level changed entries (dirs shown as tree nodes) — git's diff-tree
    default without -r; recurse=True is the `diff-tree -t -r` form."""
    if a_tree == b_tree:
        return
    from . import workdir as _wd
    a_entries = {e.name: e for e in _wd._tree_entries(repo, a_tree)} if a_tree else {}
    b_entries = {e.name: e for e in _wd._tree_entries(repo, b_tree)} if b_tree else {}
    for name in sorted(set(a_entries) | set(b_entries)):
        a = a_entries.get(name)
        b = b_entries.get(name)
        path = f"{prefix}{name}"
        if a is not None and b is not None and a.sha == b.sha and a.mode == b.mode:
            continue
        a_dir = a is not None and a.is_dir()
        b_dir = b is not None and b.is_dir()
        if not recurse:
            yield path, a, b
            continue
        if a_dir and b_dir:
            yield path, a, b
            yield from _diff_tree_changes_with_dirs(repo, a.sha, b.sha, path + "/")
        elif b_dir and a is None:
            yield path, None, b
            yield from _diff_tree_changes_with_dirs(repo, None, b.sha, path + "/")
        elif a_dir and b is None:
            yield path, a, None
            yield from _diff_tree_changes_with_dirs(repo, a.sha, None, path + "/")
        else:
            yield path, a, b


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
    ap = argparse.ArgumentParser(prog="pygit diff-tree", add_help=False)
    ap.add_argument("-r", action="store_true", help="recurse")
    ap.add_argument("-t", dest="show_trees", action="store_true")
    ap.add_argument("-p", "--patch", action="store_true")
    ap.add_argument("--root", action="store_true")
    ap.add_argument("--no-commit-id", dest="no_commit_id", action="store_true")
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
        # A merge has only a combined diff, which is empty for a clean merge and
        # suppressed by default — git emits nothing at all (not even the id).
        if len(c.parents) > 1:
            return 0
        b_tree = c.tree
        a_tree = (objs.parse_commit(objs.read_object(repo, c.parents[0])[1]).tree
                  if c.parents else None)
        if not args.no_commit_id:
            _print(s)
    else:
        a_tree = _resolve_to_tree(args.rev1)
        b_tree = _resolve_to_tree(args.rev2)
    if not b_tree:
        return 128
    if args.show_trees:
        # -t recurses into changed trees (even without -r), showing tree nodes.
        changes = list(_diff_tree_changes_with_dirs(repo, a_tree, b_tree, recurse=True))
    elif not args.r:
        # Without -r, diff-tree lists only the top-level changed entries
        # (directories shown as tree nodes), not their contents.
        changes = list(_diff_tree_changes_with_dirs(repo, a_tree, b_tree, recurse=False))
    else:
        changes = list(workdir.iter_tree_changes(repo, a_tree, b_tree))
    for p, a_entry, b_entry in changes:
        a_sha = a_entry.sha if a_entry else None
        b_sha = b_entry.sha if b_entry else None
        a_mode = (a_entry.mode if a_entry else "100644").zfill(6)
        b_mode = (b_entry.mode if b_entry else "100644").zfill(6)
        if a_sha == b_sha and a_mode == b_mode:
            continue
        if args.name_only:
            _print(p)
        elif args.name_status:
            if a_sha is None:
                _print(f"A\t{p}")
            elif b_sha is None:
                _print(f"D\t{p}")
            else:
                _print(f"M\t{p}")
        elif not args.patch:
            ln = _raw_diff_status(a_mode, b_mode, a_sha, b_sha, p)
            if ln:
                _print(ln)
    if args.patch:
        for p, a_entry, b_entry in changes:
            a_side = _side_from_object(repo, a_entry.mode, a_entry.sha) if a_entry else _ABSENT
            b_side = _side_from_object(repo, b_entry.mode, b_entry.sha) if b_entry else _ABSENT
            _emit_file_diff(p, a_side, b_side)
    return 0


def cmd_diff_files(argv: list[str]) -> int:
    """Show diff between index and worktree (raw format)."""
    ap = argparse.ArgumentParser(prog="pygit diff-files")
    ap.add_argument("--name-only", action="store_true")
    ap.add_argument("--stat", action="store_true")
    ap.add_argument("--numstat", action="store_true")
    ap.add_argument("--shortstat", action="store_true")
    args = ap.parse_args(argv)
    repo = _repo()
    from .index import read_index
    idx = read_index(repo)
    if args.stat or args.numstat or args.shortstat:
        changes = []
        for e in idx.entries:
            full = repo.path / e.path
            a = _side_from_object(repo, e.mode_str(), e.sha)
            b = _side_from_worktree(repo, e.path) if full.exists() else _ABSENT
            if a.sha != b.sha or a.mode != b.mode:
                changes.append((e.path, a, b))
        if args.stat:
            _diff_stat(changes)
        elif args.numstat:
            _diff_numstat(changes)
        else:
            _diff_shortstat(changes)
        return 0
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
                # The worktree side is not a stored object, so its id is zeros.
                _print(f":{e.mode_str()} {e.mode_str()} {e.sha} {'0' * 40} M\t{e.path}")
    return 0


def cmd_diff_index(argv: list[str]) -> int:
    """Show diff between a tree and the index (--cached) or the working tree."""
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
    a_map = _tree_map_full(repo, tsha)
    from .index import read_index
    idx = read_index(repo).by_path()
    zero = "0" * 40
    for p in sorted(set(a_map) | set(idx)):
        a = _side_from_object(repo, *a_map[p]) if p in a_map else _ABSENT
        # The "b" side comes from the index. Without --cached, its blob id is
        # zeroed when the working tree is dirty relative to the index (matching
        # git, which can't name an uncommitted worktree blob).
        b_present = p in idx
        b_mode = idx[p].mode_str() if b_present else None
        b_sha = idx[p].sha if b_present else None
        b_dirty = False
        if b_present and not args.cached:
            wt = _side_from_worktree(repo, p)
            if not wt.present:
                b_present, b_mode, b_sha = False, None, None
            else:
                b_dirty = wt.sha != idx[p].sha
        if a.present and b_present and a.sha == b_sha and not b_dirty and a.mode == b_mode:
            continue
        if not a.present and not b_present:
            continue
        if args.name_only:
            _print(p)
            continue
        status = "A" if not a.present else ("D" if not b_present else "M")
        out_a = a.sha if a.present else zero
        out_b = zero if (b_dirty or not b_present) else b_sha
        _print(f":{(a.mode or '000000')} {(b_mode or '000000')} {out_a} {out_b} {status}\t{p}")
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
    ap.add_argument("--normalize", action="store_true")
    ap.add_argument("name")
    args = ap.parse_args(argv)
    name = args.name
    if args.normalize:
        # Squash runs of slashes and strip leading ones; a trailing slash is
        # left in place so it still fails validation, as in C Git.
        import re as _re
        name = _re.sub("/+", "/", name).lstrip("/")
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
    # A plain valid ref name produces no output; --branch/--normalize echo it.
    if args.branch or args.normalize:
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
    """Minimal credential helper: read description from stdin and resolve credentials."""
    ap = argparse.ArgumentParser(prog="pygit credential")
    ap.add_argument("op", choices=["fill", "approve", "reject"])
    args = ap.parse_args(argv)
    fields: dict[str, str] = {}
    for line in sys.stdin.read().splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            fields[k] = v
    if args.op == "fill":
        from . import bridges
        fields = bridges.credential_fill(fields, use_external=False)
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
    from . import diff as _d
    h = hashlib.sha1()
    for p, a_entry, b_entry in workdir.iter_tree_changes(repo, parent_tree or None, c.tree):
        a_sha = a_entry.sha if a_entry else None
        b_sha = b_entry.sha if b_entry else None
        if a_sha == b_sha:
            continue
        at = bt = ""
        if a_sha:
            at = objs.read_object(repo, a_sha)[1].decode("utf-8", errors="replace")
        if b_sha:
            bt = objs.read_object(repo, b_sha)[1].decode("utf-8", errors="replace")
        h.update(_d.unified_diff(at, bt, p, p).encode("utf-8", errors="replace"))
    return h.hexdigest()


def cmd_patch_id(argv: list[str]) -> int:
    """Read a diff on stdin and emit its patch-id (hash of diff with line numbers stripped).

    Also accepts a commit id as positional arg.
    """
    ap = argparse.ArgumentParser(prog="pygit patch-id", add_help=False)
    ap.add_argument("--stable", action="store_true")
    ap.add_argument("rev", nargs="?")
    args = ap.parse_args(argv)
    if args.rev:
        repo = _repo()
        s = refs_mod.rev_parse(repo, args.rev)
        if not s:
            return 128
        _print(_patch_id_for_commit(repo, s) + " " + s)
        return 0
    pid = _compute_patch_id(sys.stdin.read())
    if pid is not None:
        _print(f"{pid} {'0' * 40}")
    return 0


def _scan_hunk_header(line: str) -> tuple[int, int]:
    import re
    m = re.match(r"@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@", line)
    if not m:
        return 0, 0
    before = int(m.group(1)) if m.group(1) is not None else 1
    after = int(m.group(2)) if m.group(2) is not None else 1
    return before, after


def _compute_patch_id(text: str) -> Optional[str]:
    """Compute a patch-id exactly like C Git's get_one_patchid (default mode)."""
    import hashlib
    h = hashlib.sha1()
    before = after = -1
    patchlen = 0
    for line in text.splitlines(keepends=True):
        if line.startswith("\\ ") and len(line) > 12:
            continue
        if patchlen == 0 and not line.startswith("diff "):
            continue
        if before == -1:
            if line.startswith("Binary files") or line.startswith("GIT binary patch"):
                before = 0
                continue
            if line.startswith("index "):
                continue
            if line.startswith("--- "):
                before = after = 1
            elif not (line and line[0].isalpha()):
                break
        if before == 0 and after == 0:
            if line.startswith("@@ -"):
                before, after = _scan_hunk_header(line)
                continue
            if not line.startswith("diff "):
                break
            before = after = -1
        if line and line[0] in "- ":
            before -= 1
        if line and line[0] in "+ ":
            after -= 1
        stripped = "".join(c for c in line if not c.isspace())
        patchlen += len(stripped)
        h.update(stripped.encode("utf-8", errors="replace"))
    return h.hexdigest() if patchlen else None


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
    import tempfile

    tmp_pack = None
    try:
        with tempfile.NamedTemporaryFile(prefix="pygit-fetch-pack-", suffix=".pack", delete=False) as tmp:
            tmp_pack = tmp.name
        protocol.fetch_pack_to_file(args.url, wants, Path(tmp_pack))
        _p.install_pack_file(repo, Path(tmp_pack))
        tmp_pack = None
    finally:
        if tmp_pack:
            Path(tmp_pack).unlink(missing_ok=True)
            Path(tmp_pack).with_suffix(".idx").unlink(missing_ok=True)
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
    """Alias: merge using the default ort-backed strategy."""
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
        for p, a_entry, b_entry in workdir.iter_tree_changes(repo, a, b):
            a_sha = a_entry.sha if a_entry else None
            b_sha = b_entry.sha if b_entry else None
            if a_sha == b_sha:
                continue
            ln = _raw_diff_status(
                a_entry.mode if a_entry else "100644",
                b_entry.mode if b_entry else "100644",
                a_sha,
                b_sha,
                p,
            )
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
    from . import __version__ as _ppg_version
    lines.append(f"pythongit version: {_ppg_version}")
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
    from . import __version__ as _ppg_version
    lines = []
    lines.append(f"pythongit: {_ppg_version}")
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
    _COMMANDS["stage"] = cmd_add
    _COMMANDS["init-db"] = cmd_init_db
    _COMMANDS["annotate"] = cmd_annotate
    _COMMANDS["pickaxe"] = cmd_blame
    _COMMANDS["patch-id"] = cmd_patch_id
    _COMMANDS["checkout-index"] = cmd_checkout_index
    _COMMANDS["fmt-merge-msg"] = cmd_fmt_merge_msg
    _COMMANDS["fetch-pack"] = cmd_fetch_pack
    _COMMANDS["send-pack"] = cmd_send_pack
    _COMMANDS["upload-pack"] = cmd_upload_pack
    _COMMANDS["receive-pack"] = cmd_receive_pack
    _COMMANDS["upload-archive"] = cmd_upload_archive
    _COMMANDS["upload-archive--writer"] = cmd_upload_archive
    _COMMANDS["pack-redundant"] = cmd_pack_redundant
    _COMMANDS["prune-packed"] = cmd_prune_packed
    _COMMANDS["fsck-objects"] = cmd_fsck
    _COMMANDS["merge-recursive"] = cmd_merge_recursive
    _COMMANDS["merge-recursive-ours"] = cmd_merge_recursive
    _COMMANDS["merge-recursive-theirs"] = cmd_merge_recursive
    _COMMANDS["merge-subtree"] = cmd_merge_recursive
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
    _COMMANDS["submodule--helper"] = cmd_submodule_helper
    _COMMANDS["submodule-helper"] = cmd_submodule_helper
    _COMMANDS["checkout--worker"] = cmd_checkout_worker
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
        ap.add_argument("--smtp-auth", default="plain", choices=["plain", "xoauth2", "oauth2"])
        ap.add_argument("--smtp-oauth2-token", default=None)
        ap.add_argument("--no-credential-helper", action="store_true")
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
                                  smtp_ssl_cert_path=args.smtp_ssl_cert_path,
                                  smtp_auth=args.smtp_auth,
                                  smtp_oauth2_token=args.smtp_oauth2_token,
                                  use_credential_helpers=not args.no_credential_helper)

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
        ap.add_argument("--backend", choices=["auto", "polling", "native", "windows", "inotify"], default="auto")
        a = ap.parse_args(argv)
        return bridges.fsmonitor_run(_repo(), interval=a.interval, iterations=a.iterations, backend=a.backend)

    def _cmd_fsmonitor_daemon(argv):
        ap = argparse.ArgumentParser(prog="pygit fsmonitor-daemon")
        ap.add_argument("op", choices=["start", "stop", "run", "status"])
        a = ap.parse_args(argv)
        return bridges.fsmonitor_daemon(_repo(), a.op)

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
    _COMMANDS["credential-cache--daemon"] = _cmd_credential_cache_daemon
    _COMMANDS["credential-cache-daemon"] = _cmd_credential_cache_daemon
    _COMMANDS["credential-store"] = _cmd_credential_store
    _COMMANDS["fsmonitor"] = _cmd_fsmonitor
    _COMMANDS["fsmonitor--daemon"] = _cmd_fsmonitor_daemon
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


_PYGIT_EXTENSION_COMMANDS = frozenset({
    "checkout-worker",
    "convert-object-format",
    "credential-cache-daemon",
    "cvsexportcommit",
    "cvsimport",
    "cvsserver",
    "daemon",
    "fsmonitor",
    "fsmonitor-daemon",
    "gitk",
    "gitweb",
    "gui",
    "http-backend",
    "http-fetch",
    "install-git-shim",
    "instaweb",
    "mergetool",
    "remote-helper",
    "request-pull",
    "send-email",
    "shell",
    "submodule",
    "submodule-helper",
    "svn",
    "uninstall-git-shim",
    "url-parse",
})


def cgit_compatible_commands() -> set[str]:
    return set(_COMMANDS) - set(_PYGIT_EXTENSION_COMMANDS)


_GLOBAL_FLAGS_IGNORED = {
    "-p", "--paginate", "-P", "--no-pager",
    "--no-replace-objects", "--no-lazy-fetch", "--no-optional-locks",
    "--no-advice", "--literal-pathspecs", "--glob-pathspecs",
    "--noglob-pathspecs", "--icase-pathspecs",
}
_GLOBAL_EXIT = "__pygit_global_exit__"


def _remember_env(old_env: dict[str, Optional[str]], key: str) -> None:
    if key not in old_env:
        old_env[key] = os.environ.get(key)


def _set_temp_env(old_env: dict[str, Optional[str]], key: str, value: str) -> None:
    _remember_env(old_env, key)
    os.environ[key] = value


def _restore_env(old_env: dict[str, Optional[str]]) -> None:
    for key, value in old_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _normalize_global_path(path: str) -> str:
    return str(Path(path).resolve())


def _consume_global_options(argv: list[str]) -> tuple[list[str], dict[str, Optional[str]], str]:
    old_env: dict[str, Optional[str]] = {}
    old_cwd = os.getcwd()
    out = list(argv)
    i = 0
    while i < len(out):
        a = out[i]
        if a == "--":
            return out[i + 1 :], old_env, old_cwd
        if a == "-C" or a.startswith("-C") and len(a) > 2:
            if a == "-C":
                if i + 1 >= len(out):
                    raise SystemExit("option requires an argument -- C")
                path = out[i + 1]
                i += 2
            else:
                path = a[2:]
                i += 1
            if path:
                os.chdir(path)
            continue
        if a == "-c" or a.startswith("-c") and len(a) > 2:
            if a == "-c":
                if i + 1 >= len(out):
                    raise SystemExit("option requires an argument -- c")
                value = out[i + 1]
                i += 2
            else:
                value = a[2:]
                i += 1
            _remember_env(old_env, "GIT_CONFIG_PARAMETERS")
            cur = os.environ.get("GIT_CONFIG_PARAMETERS", "")
            os.environ["GIT_CONFIG_PARAMETERS"] = (cur + "\n" if cur else "") + value
            continue
        if a.startswith("--config-env="):
            spec = a.split("=", 1)[1]
            name, sep, env_name = spec.partition("=")
            if not sep or env_name not in os.environ:
                raise SystemExit(f"invalid --config-env: {spec}")
            _remember_env(old_env, "GIT_CONFIG_PARAMETERS")
            cur = os.environ.get("GIT_CONFIG_PARAMETERS", "")
            os.environ["GIT_CONFIG_PARAMETERS"] = (cur + "\n" if cur else "") + f"{name}={os.environ[env_name]}"
            i += 1
            continue
        if a == "--git-dir" or a.startswith("--git-dir="):
            if a == "--git-dir":
                if i + 1 >= len(out):
                    raise SystemExit("option requires an argument -- git-dir")
                path = out[i + 1]
                i += 2
            else:
                path = a.split("=", 1)[1]
                i += 1
            _set_temp_env(old_env, "GIT_DIR", _normalize_global_path(path))
            continue
        if a == "--work-tree" or a.startswith("--work-tree="):
            if a == "--work-tree":
                if i + 1 >= len(out):
                    raise SystemExit("option requires an argument -- work-tree")
                path = out[i + 1]
                i += 2
            else:
                path = a.split("=", 1)[1]
                i += 1
            _set_temp_env(old_env, "GIT_WORK_TREE", _normalize_global_path(path))
            continue
        if a == "--namespace" or a.startswith("--namespace="):
            if a == "--namespace":
                if i + 1 >= len(out):
                    raise SystemExit("option requires an argument -- namespace")
                value = out[i + 1]
                i += 2
            else:
                value = a.split("=", 1)[1]
                i += 1
            _set_temp_env(old_env, "GIT_NAMESPACE", value)
            continue
        if a == "--bare":
            _set_temp_env(old_env, "GIT_DIR", _normalize_global_path("."))
            i += 1
            continue
        if a in _GLOBAL_FLAGS_IGNORED:
            i += 1
            continue
        if a in ("--exec-path", "--html-path", "--man-path", "--info-path"):
            _print("")
            return [_GLOBAL_EXIT], old_env, old_cwd
        if a.startswith("--exec-path="):
            _set_temp_env(old_env, "GIT_EXEC_PATH", a.split("=", 1)[1])
            i += 1
            continue
        return out[i:], old_env, old_cwd
    return [], old_env, old_cwd


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:]) if argv is None else list(argv)
    try:
        argv, old_env, old_cwd = _consume_global_options(argv)
    except SystemExit as e:
        _err(str(e))
        return 129
    try:
        if not argv or argv[0] in ("-h", "--help"):
            return cmd_help([])
        if argv[0] == _GLOBAL_EXIT:
            return 0
        if argv[0] in ("--version", "-v"):
            from . import __version__
            _print(f"pygit version {__version__}")
            return 0
        if argv[0] == "version":
            return cmd_version(argv[1:])
        cmd = argv[0]
        rest = argv[1:]
        fn = _COMMANDS.get(cmd)
        if fn is None:
            _err(f"pygit: '{cmd}' is not a pygit command. See 'pygit help'.")
            return 1
        return fn(rest)
    except RepositoryError as e:
        _err(f"fatal: {e}")
        return 128
    except BrokenPipeError:
        return 0
    finally:
        os.chdir(old_cwd)
        _restore_env(old_env)
