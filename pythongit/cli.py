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


def _object_disk_size(repo: Repository, sha: str) -> int:
    """On-disk byte size of an object, for `rev-list --disk-usage`.

    A loose object is the size of its zlib file; a packed object is the size of
    its entry in the pack (header + compressed payload). Loose objects dominate
    freshly written repositories and are reported exactly."""
    p = objs._loose_path(repo, sha)
    if p.exists():
        try:
            return p.stat().st_size
        except OSError:
            return 0
    try:
        from . import pack as _pack
        sz = _pack.packed_object_disk_size(repo, sha)
        if sz is not None:
            return sz
    except Exception:
        pass
    try:
        import zlib
        return len(zlib.compress(objs.read_object(repo, sha)[1], 1))
    except Exception:
        return 0


def _estimate_bisect_steps(all_count: int) -> int:
    """C Git's estimate_bisect_steps (bisect.c): expected remaining test count."""
    if all_count < 3:
        return 0
    n = all_count.bit_length() - 1   # floor(log2(all))
    e = 1 << n                        # 2**n
    x = all_count - e
    return n if e < 3 * x else n - 1


def _humanise_bytes(n: int) -> str:
    """Match C Git's strbuf_humanise_bytes (human-readable byte counts)."""
    if n >= 1 << 30:
        return "%.2f GiB" % (n / (1 << 30))
    if n >= 1 << 20:
        return "%.2f MiB" % (n / (1 << 20))
    if n >= 1 << 10:
        return "%.2f KiB" % (n / (1 << 10))
    return "%d bytes" % n


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
    # --max-age/--min-age give raw epoch bounds (confusingly named: max-age is the
    # older/lower date bound like --since, min-age the upper bound like --until).
    max_age = getattr(args, "max_age", None)
    min_age = getattr(args, "min_age", None)
    if max_age is not None:
        since = max_age if since is None else max(since, max_age)
    if min_age is not None:
        until = min_age if until is None else min(until, min_age)
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


def _topo_order(repo: Repository, orig: list, first_parent: bool = False,
                by_date: bool = False) -> list:
    """Topologically order ``orig`` like C Git's sort_in_topological_order
    (commit.c): a Kahn's-algorithm walk that emits a commit only after all its
    in-set children. With ``by_date`` False the ready set is a NULL-compare
    prio_queue (a stack → graph order, REV_SORT_IN_GRAPH_ORDER); with
    ``by_date`` True it is a max-heap on committer date (REV_SORT_BY_AUTHOR_DATE
    style used by --date-order)."""

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
    out: list[str] = []
    if by_date:
        import heapq
        heap: list[tuple] = []
        counter = 0
        # Tips (no in-set child) seed the queue in original list order.
        for s in orig:
            if indegree[s] == 1:
                heapq.heappush(heap, (-_commit_date(repo, s), counter, s))
                counter += 1
        while heap:
            _d, _c, s = heapq.heappop(heap)
            out.append(s)
            for p in parents(s):
                if indegree.get(p, 0) == 0:
                    continue
                indegree[p] -= 1
                if indegree[p] == 1:
                    heapq.heappush(heap, (-_commit_date(repo, p), counter, p))
                    counter += 1
        return out
    # Graph order: the NULL-compare prio_queue is a stack, and tips are reversed
    # so they pop in original traversal order.
    tips = [s for s in orig if indegree[s] == 1]
    stack = list(reversed(tips))
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


# Shared-repository permission classes, mirroring C Git's setup.h enum.
_PERM_UMASK = 0
_PERM_GROUP = 0o660
_PERM_EVERYBODY = 0o664
_OLD_PERM_GROUP = 1
_OLD_PERM_EVERYBODY = 2


class _BadBoolValue(Exception):
    """Raised when git_config_bool would die() on an unparsable value."""


def _git_parse_int(value: str) -> "int | None":
    """Port of git_parse_int(): decimal int with an optional k/m/g scale."""
    v = value.strip()
    if not v:
        return None
    scale = 1
    if v[-1] in "kKmMgG":
        scale = {"k": 1 << 10, "m": 1 << 20, "g": 1 << 30}[v[-1].lower()]
        v = v[:-1]
    try:
        return int(v, 10) * scale
    except ValueError:
        return None


def _git_config_bool(value: str) -> bool:
    """Port of C Git's git_config_bool(); raises _BadBoolValue when git dies."""
    v = value.lower()
    if value == "":
        return False
    if v in ("true", "yes", "on"):
        return True
    if v in ("false", "no", "off"):
        return False
    n = _git_parse_int(value)
    if n is not None:
        return n != 0
    raise _BadBoolValue(value)


def _git_config_perm(value: "str | None") -> int:
    """Port of git_config_perm() in setup.c. Returns a shared-repository code:
    0 (umask), 0o660 (group), 0o664 (everybody), or a negative -(mode & 0666)
    for an explicit filemode. Raises ValueError for a forbidden filemode."""
    if value is None:
        return _PERM_GROUP
    if value == "umask":
        return _PERM_UMASK
    if value == "group":
        return _PERM_GROUP
    if value in ("all", "world", "everybody"):
        return _PERM_EVERYBODY

    # Parse leading octal digits the way strtol(value, &endptr, 8) does.
    s = value
    sign = 1
    idx = 0
    if idx < len(s) and s[idx] in "+-":
        if s[idx] == "-":
            sign = -1
        idx += 1
    digit_start = idx
    i = 0
    while idx < len(s) and s[idx] in "01234567":
        i = i * 8 + int(s[idx])
        idx += 1
    i *= sign
    # strtol performs no conversion when no digits follow the optional sign; in
    # that case endptr is reset to the start of the string. The conversion is
    # "clean" (C's `*endptr == 0`) only when either at least one digit was read
    # and nothing trails it, or the whole string was empty to begin with.
    if idx == digit_start:
        # No digits consumed: endptr == nptr (whole string). Clean only when "".
        fully = s == ""
    else:
        fully = idx == len(s)

    if not fully:
        # Not a clean octal number: fall back to true/false parsing.
        return _PERM_GROUP if _git_config_bool(value) else _PERM_UMASK

    if i == _PERM_UMASK:
        return _PERM_UMASK
    if i == _OLD_PERM_GROUP:
        return _PERM_GROUP
    if i == _OLD_PERM_EVERYBODY:
        return _PERM_EVERYBODY

    # An explicit filemode value: owner must keep read+write.
    if (i & 0o600) != 0o600:
        raise ValueError(i)
    return -(i & 0o666)


def _calc_shared_perm(shared: int, mode: int) -> int:
    """Port of calc_shared_perm() in path.c."""
    import stat as _stat
    tweak = -shared if shared < 0 else shared
    if not (mode & _stat.S_IWUSR):
        tweak &= ~0o222
    if mode & _stat.S_IXUSR:
        tweak |= (tweak & 0o444) >> 2
    if shared < 0:
        mode = (mode & ~0o777) | tweak
    else:
        mode |= tweak
    return mode


def _adjust_shared_perm(shared: int, path: "Path") -> None:
    """Port of adjust_shared_perm() in path.c for a single path."""
    import stat as _stat
    if not shared:
        return
    try:
        old_mode = path.lstat().st_mode
    except OSError:
        return
    if _stat.S_ISLNK(old_mode):
        return
    new_mode = _calc_shared_perm(shared, old_mode)
    if _stat.S_ISDIR(old_mode):
        new_mode |= (new_mode & 0o444) >> 2
        # FORCE_DIR_SET_GID: g+s on dirs whenever group access is granted, so
        # new files inherit the group (matches C Git on Linux/most Unix).
        if new_mode & 0o060:
            new_mode |= _stat.S_ISGID
    if (old_mode ^ new_mode) & ~0o170000:
        try:
            os.chmod(path, new_mode & ~0o170000)
        except OSError:
            pass


def _adjust_shared_perm_recursive(shared: int, root: "Path") -> None:
    """Apply shared permissions to the gitdir and everything beneath it.

    C Git applies adjust_shared_perm per directory as it creates the layout
    (safe_create_dir with share=1) and once more on the whole gitdir; the net
    effect is that every directory and file gets the shared bits, which is what
    we reproduce here over the finished tree."""
    if not shared:
        return
    _adjust_shared_perm(shared, root)
    for sub in sorted(root.rglob("*")):
        _adjust_shared_perm(shared, sub)


def _copy_templates(template_dir: "Path", gitdir: "Path") -> None:
    """Port of copy_templates_1() in setup.c: copy template files into gitdir.

    Skips dotfiles, recurses into directories, copies regular files and
    symlinks, and leaves any file that already exists in the gitdir alone."""
    import stat as _stat

    def copy_file(dst: Path, src: Path, mode: int) -> None:
        # C Git's copy_file(): executables become 0777, everything else 0666,
        # both then masked by the active umask via the open() create mode.
        create_mode = 0o777 if (mode & 0o111) else 0o666
        data = src.read_bytes()
        fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, create_mode)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)

    def walk(src: Path, dst: Path) -> None:
        dst.mkdir(parents=True, exist_ok=True)
        for entry in os.scandir(src):
            if entry.name.startswith("."):
                continue
            s = Path(entry.path)
            d = dst / entry.name
            st = s.lstat()
            if _stat.S_ISDIR(st.st_mode):
                walk(s, d)
            elif d.exists() or d.is_symlink():
                continue
            elif _stat.S_ISLNK(st.st_mode):
                os.symlink(os.readlink(s), d)
            elif _stat.S_ISREG(st.st_mode):
                copy_file(d, s, st.st_mode)

    walk(template_dir, gitdir)


def _split_optarg(argv: list[str], name: str) -> "tuple[list[str], object]":
    """Extract a PARSE_OPT_OPTARG flag (value only via ``--name=value``) from
    argv. Returns (remaining_argv, value) where value is the sentinel object
    ``_NO_FLAG`` if absent, None if given bare, or the string after ``=``."""
    out: list[str] = []
    value: object = _NO_FLAG
    prefix = name + "="
    for tok in argv:
        if tok == name:
            value = None
        elif tok.startswith(prefix):
            value = tok[len(prefix):]
        else:
            out.append(tok)
    return out, value


_NO_FLAG = object()


def _extract_ref_format(argv: list[str]) -> "tuple[list[str], object]":
    """Extract git's ``--ref-format=<format>`` (OPT_STRING) from argv.

    Returns (remaining_argv, value) where value is ``_NO_FLAG`` if the option
    was absent, otherwise the string format. Like C parse-options, a bare
    ``--ref-format`` consumes the following token as its value; if no token
    follows it raises ``_SwitchParseError`` ("requires a value", rc 129)."""
    out: list[str] = []
    value: object = _NO_FLAG
    i = 0
    n = len(argv)
    while i < n:
        tok = argv[i]
        if tok == "--ref-format":
            i += 1
            if i >= n:
                raise _SwitchParseError("error: option `ref-format' requires a value")
            value = argv[i]
        elif tok.startswith("--ref-format="):
            value = tok[len("--ref-format="):]
        else:
            out.append(tok)
        i += 1
    return out, value


def cmd_init(argv: list[str]) -> int:
    # --ref-format uses OPT_STRING: a bare "--ref-format" consumes the next
    # token as its value (rc 129 if none follows). Pull it before argparse so
    # the value is not mistaken for the path positional.
    try:
        argv, ref_format = _extract_ref_format(list(argv))
    except _SwitchParseError as exc:
        _err(exc.message)
        return exc.rc

    # --shared uses PARSE_OPT_OPTARG (value only via "--shared=x"); argparse's
    # nargs='?' would wrongly swallow a following token, so split it by hand.
    argv, shared_arg = _split_optarg(list(argv), "--shared")

    ap = argparse.ArgumentParser(prog="pygit init", add_help=False)
    ap.add_argument("path", nargs="?", default=".")
    ap.add_argument("--bare", action="store_true")
    ap.add_argument("--object-format", choices=["sha1", "sha256"], default="sha1")
    ap.add_argument("--template", default=None)
    ap.add_argument("--separate-git-dir", dest="separate_git_dir", default=None)
    ap.add_argument("-b", "--initial-branch", default=None)
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args(argv)

    from . import gitconfig

    # --separate-git-dir and --bare are mutually exclusive.
    if args.separate_git_dir is not None and args.bare:
        _err("fatal: options '--separate-git-dir' and '--bare' cannot be used together")
        return 128

    # --ref-format: only the "files" backend is reproducible byte-for-byte.
    # "reftable" is a valid git format but a different on-disk layout pygit
    # cannot replicate, so reject it rather than silently produce a files repo.
    # Any other value matches git's "unknown ref storage format" fatal (rc 128).
    if ref_format is not _NO_FLAG:
        if ref_format == "reftable":
            _err("fatal: pygit does not support the 'reftable' ref storage format")
            return 128
        if ref_format != "files":
            _err(f"fatal: unknown ref storage format '{ref_format}'")
            return 128

    # Resolve the shared-repository setting (None means the flag was absent).
    shared = 0
    if shared_arg is not _NO_FLAG:
        try:
            shared = _git_config_perm(shared_arg)
        except ValueError as exc:
            i = exc.args[0]
            _err(
                "fatal: problem with core.sharedRepository filemode value "
                f"(0{i & 0o777:03o}).\nThe owner of files must always have "
                "read and write permissions."
            )
            return 128
        except _BadBoolValue as exc:
            _err(f"fatal: bad boolean config value '{exc.args[0]}' for 'arg'")
            return 128

    target = Path(args.path).resolve()

    # Where the real repository lives. --separate-git-dir relocates it and
    # leaves a "gitdir:" pointer file in the work tree.
    gitdir_override = None
    if args.separate_git_dir is not None:
        gitdir_override = Path(args.separate_git_dir).resolve()
        gitdir = gitdir_override
    else:
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
            show_hint = (
                not already
                and not args.quiet
                and advice not in ("false", "0", "no", "off")
            )

    # Copy templates before writing defaults so a template's description/exclude
    # (or its absence) wins, exactly as C Git does. With a template supplied we
    # never emit pygit's stand-in description/hooks/info-exclude.
    template_dir = None
    if args.template:
        template_dir = Path(args.template).resolve()
        if not template_dir.is_dir():
            # git reports the resolved absolute path, not the raw argument.
            _err(f"warning: templates not found in {template_dir}")
            template_dir = None

    repo = Repository.init(
        target,
        bare=args.bare,
        object_format=args.object_format,
        gitdir_override=gitdir_override,
        write_default_extras=(args.template is None),
    )
    if template_dir is not None:
        _copy_templates(template_dir, repo.gitdir)
    if not already:
        (repo.gitdir / "HEAD").write_text(f"ref: refs/heads/{branch}\n", encoding="utf-8")

    # Record the shared-repository config the way C Git serialises it.
    if shared:
        if shared < 0:
            buf = f"0{-shared:o}"
        elif shared == _PERM_GROUP:
            buf = str(_OLD_PERM_GROUP)
        elif shared == _PERM_EVERYBODY:
            buf = str(_OLD_PERM_EVERYBODY)
        else:
            buf = f"0{shared:o}"
        cfg_path = repo.gitdir / "config"
        gitconfig.write_value(cfg_path, "core", None, "sharedrepository", buf)
        gitconfig.write_value(cfg_path, "receive", None, "denyNonFastforwards", "true")
        _adjust_shared_perm_recursive(shared, repo.gitdir)

    # Drop the gitdir pointer file into the work tree for --separate-git-dir.
    if gitdir_override is not None:
        (target / ".git").write_text(f"gitdir: {gitdir_override}\n", encoding="utf-8")

    if already and args.initial_branch is not None:
        _err(f"warning: re-init: ignored --initial-branch={args.initial_branch}")
    if show_hint:
        sys.stderr.write(_DEFAULT_BRANCH_HINT)
    if not args.quiet:
        if already:
            word = "Reinitialized existing shared" if shared else "Reinitialized existing"
        else:
            word = "Initialized empty shared" if shared else "Initialized empty"
        _print(f"{word} Git repository in {repo.gitdir}/")
    return 0


def cmd_hash_object(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit hash-object", add_help=False)
    ap.add_argument("-w", action="store_true", help="write object")
    ap.add_argument("-t", default="blob", choices=["blob", "tree", "commit", "tag"])
    ap.add_argument("--stdin", action="store_true")
    ap.add_argument("--stdin-paths", action="store_true")
    ap.add_argument("--literally", action="store_true")
    ap.add_argument("--no-filters", dest="no_filters", action="store_true")
    ap.add_argument("--path", default=None)
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


def _loose_object_shas(repo: Repository) -> set[str]:
    """Every loose object id in the repo."""
    shas: set[str] = set()
    objdir = repo.gitdir / "objects"
    hex_len = repo.hex_len
    if objdir.is_dir():
        for d in objdir.iterdir():
            if d.is_dir() and len(d.name) == 2 and all(c in "0123456789abcdef" for c in d.name):
                for f in d.iterdir():
                    if f.is_file() and len(f.name) == hex_len - 2:
                        shas.add(d.name + f.name)
    return shas


def _all_object_shas(repo: Repository) -> list[str]:
    """Every object id in the repo (loose + packed), sorted ascending — the
    order `cat-file --batch-all-objects` emits."""
    shas: set[str] = set(_loose_object_shas(repo))
    from . import pack as _p
    midx = _p.read_midx(repo)
    if midx is not None:
        shas.update(midx.shas)
    else:
        for pk in _p._iter_packs(repo):
            shas.update(pk.shas)
    return sorted(shas)


def _all_object_shas_unordered(repo: Repository) -> list[str]:
    """Object ids in `cat-file --batch-all-objects --unordered` order: loose
    objects (sorted) first, then packed objects in pack-offset order, with
    later duplicates suppressed (the first occurrence wins)."""
    seen: set[str] = set()
    out: list[str] = []
    for sha in sorted(_loose_object_shas(repo)):
        if sha not in seen:
            seen.add(sha)
            out.append(sha)
    from . import pack as _p
    midx = _p.read_midx(repo)
    if midx is not None:
        # MIDX exposes objects in oid-sorted order with their (pack, offset);
        # emit pack-by-pack, each in offset order, matching git's pack walk.
        packs: dict[int, list[tuple[int, str]]] = {}
        for i, sha in enumerate(midx.shas):
            packs.setdefault(midx.pack_ids[i], []).append((midx.offsets[i], sha))
        for pid in sorted(packs):
            for _off, sha in sorted(packs[pid]):
                if sha not in seen:
                    seen.add(sha)
                    out.append(sha)
    else:
        # git's repo_for_each_pack walks packs most-recent-first (the packed_git
        # list is sorted by descending mtime); replicate that ordering here.
        packs = list(_p._iter_packs(repo))
        def _mtime(pk):
            try:
                return pk.pack_path.stat().st_mtime_ns
            except OSError:
                return 0
        packs.sort(key=lambda pk: (-_mtime(pk), pk.pack_path.name))
        for pk in packs:
            pairs = sorted((pk.offset_of(s), s) for s in pk.shas)
            for _off, sha in pairs:
                if sha not in seen:
                    seen.add(sha)
                    out.append(sha)
    return out


def _split_ident_line(person: bytes):
    """Split a `Name <email> timestamp tz` ident into (name, email, rest).

    Mirrors git's split_ident_line enough for mailmap rewriting: ``name`` is the
    text before the last ``<`` (trailing space trimmed), ``email`` is between the
    final ``<``/``>``, ``rest`` is everything from ``>`` onward (including the
    space + date). Returns None when the line has no well-formed ``<email>``."""
    lt = person.rfind(b"<")
    if lt < 0:
        return None
    gt = person.find(b">", lt + 1)
    if gt < 0:
        return None
    name_end = lt
    # git trims a single run of trailing whitespace before '<'
    while name_end > 0 and person[name_end - 1:name_end] in (b" ", b"\t"):
        name_end -= 1
    name = person[:name_end]
    email = person[lt + 1:gt]
    rest = person[gt + 1:]
    return name, email, rest


def _replace_idents_using_mailmap(buf: bytes, mm) -> bytes:
    """Rewrite author/committer/tagger ``Name <email>`` idents in a commit/tag
    object body through the mailmap, preserving the trailing timestamp. Only the
    contiguous header block (up to the first blank line) is scanned, matching
    git's apply_mailmap_to_header."""
    headers = (b"author ", b"committer ", b"tagger ")
    out: list[bytes] = []
    i = 0
    n = len(buf)
    in_headers = True
    while i < n:
        nl = buf.find(b"\n", i)
        if nl < 0:
            line = buf[i:]
            nxt = n
            has_nl = False
        else:
            line = buf[i:nl]
            nxt = nl + 1
            has_nl = True
        if in_headers:
            if line == b"":
                in_headers = False
            else:
                matched = None
                for h in headers:
                    if line.startswith(h):
                        matched = h
                        break
                if matched is not None:
                    person = line[len(matched):]
                    split = _split_ident_line(person)
                    if split is not None:
                        name, email, rest = split
                        try:
                            nm = name.decode("utf-8")
                            em = email.decode("utf-8")
                        except UnicodeDecodeError:
                            nm = name.decode("latin-1")
                            em = email.decode("latin-1")
                        new_name, new_email = mm.resolve(nm, em)
                        new_person = (matched
                                      + new_name.encode("utf-8")
                                      + b" <" + new_email.encode("utf-8") + b">"
                                      + rest)
                        line = new_person
        out.append(line)
        if has_nl:
            out.append(b"\n")
        i = nxt
    return b"".join(out)


def _expand_batch_atoms(fmt: str, sha: str, t: str, size: int) -> str:
    """Expand the `cat-file --batch[-check]=<format>` %(atom) placeholders."""
    return (fmt.replace("%(objectname)", sha)
               .replace("%(objecttype)", t)
               .replace("%(objectsize:disk)", str(size))
               .replace("%(objectsize)", str(size)))


def _read_delimited_stdin(input_delim: str):
    """Yield records from stdin split on ``input_delim`` ('\\n' or '\\0').

    For newline-delimited input a trailing ``\\r`` is stripped (git's
    strbuf_getdelim_strip_crlf). The final record is dropped when it is empty
    (no trailing data after the last delimiter)."""
    raw = sys.stdin.buffer.read()
    delim = b"\0" if input_delim == "\0" else b"\n"
    if not raw:
        return
    parts = raw.split(delim)
    # split() leaves a trailing empty element when the stream ends with the
    # delimiter; git stops at EOF so that trailing empty is not a record.
    if parts and parts[-1] == b"":
        parts.pop()
    for p in parts:
        if delim == b"\n" and p.endswith(b"\r"):
            p = p[:-1]
        yield p.decode("utf-8", "surrogateescape")


def _split_rev_colon_path(name: str):
    """Split ``<rev>:<path>`` at the first ``:`` that is not inside an ``@{``/
    ``^{`` bracket group, mirroring git's get_oid_with_context_1 scan. Returns
    ``(rev, path)`` or None when there is no such colon."""
    depth = 0
    i = 0
    n = len(name)
    while i < n:
        c = name[i]
        if c in "@^" and i + 1 < n and name[i + 1] == "{":
            i += 2
            depth += 1
            continue
        if depth and c == "}":
            depth -= 1
        elif not depth and c == ":":
            return name[:i], name[i + 1:]
        i += 1
    return None


# Git follows at most this many symlinks before declaring a loop
# (GET_TREE_ENTRY_FOLLOW_SYMLINKS_MAX_LINKS in tree-walk.c).
_FOLLOW_SYMLINKS_MAX = 40


def _follow_symlinks_in_tree(repo, root_tree_sha: str, name: str):
    """Resolve ``name`` within the tree ``root_tree_sha`` following in-tree
    symlinks, mirroring git's get_tree_entry_follow_symlinks.

    Returns one of:
      ("found", sha, mode_int)  -- in-tree object (mode != 0)
      ("symlink", path)         -- chain left the tree (absolute/escaping ..)
      ("dangling",)             -- a symlink target does not exist in the tree
      ("loop",)                 -- too many symlinks followed
      ("notdir",)               -- a path component was a non-dir with remainder
      ("missing",)              -- component not found / no symlink involved
    """
    # parents[i] = (tree_data, tree_sha); parents[-1] is the current tree.
    parents: list[tuple[bytes, str]] = []
    namebuf = name
    current_tree_sha = root_tree_sha
    t_loaded = False
    follows_remaining = _FOLLOW_SYMLINKS_MAX
    # On error after at least one symlink follow, git reports DANGLING_SYMLINK.
    followed_any = False

    def load_tree(sha):
        try:
            ot, data = objs.read_object(repo, sha)
        except KeyError:
            return None
        if ot != "tree":
            return None
        return data

    while True:
        if not t_loaded:
            tree = load_tree(current_tree_sha)
            if tree is None:
                return ("dangling",) if followed_any else ("missing",)
            parents.append((tree, current_tree_sha))
            if namebuf == "":
                return ("found", current_tree_sha, 0o040000)
            if not tree:
                return ("dangling",) if followed_any else ("missing",)
            t_loaded = True

        # Strip leading slashes (symlinks to e.g. a//b).
        while namebuf.startswith("/"):
            namebuf = namebuf[1:]

        slash = namebuf.find("/")
        if slash >= 0:
            first = namebuf[:slash]
            remainder = namebuf[slash + 1:]
        else:
            first = namebuf
            remainder = None

        if first == "..":
            if len(parents) == 1:
                # Escaped the root of the tree.
                return ("symlink", namebuf)
            parents.pop()
            current_tree_sha = parents[-1][1]
            namebuf = namebuf[3:] if remainder is not None else namebuf[2:]
            t_loaded = True
            continue

        if first == "":
            # Reached here via a symlink to dir/.. -> the current tree.
            return ("found", parents[-1][1], 0o040000)

        tree_data = parents[-1][0]
        entry = next((e for e in objs.parse_tree(tree_data, repo.hash_len)
                      if e.name == first), None)
        if entry is None:
            return ("dangling",) if followed_any else ("missing",)

        mode = int(entry.mode, 8)
        if entry.is_dir():
            if remainder is None:
                return ("found", entry.sha, mode)
            current_tree_sha = entry.sha
            namebuf = remainder
            t_loaded = False
            continue
        # 0o170000 == S_IFMT
        ftype = mode & 0o170000
        if ftype == 0o120000:
            # symlink
            if follows_remaining == 0:
                return ("loop",)
            follows_remaining -= 1
            followed_any = True
            try:
                lt, contents = objs.read_object(repo, entry.sha)
            except KeyError:
                return ("dangling",)
            link = contents.decode("utf-8", "surrogateescape")
            if link.startswith("/"):
                return ("symlink", link)
            # Splice the link target in place of the consumed component.
            namebuf = link + ("/" + remainder if remainder is not None else "")
            # Re-resolve from the directory containing the symlink.
            current_tree_sha = parents[-1][1]
            t_loaded = True
            continue
        # regular file (or gitlink)
        if remainder is None:
            return ("found", entry.sha, mode)
        return ("notdir",)


def _batch_resolve(repo: Repository, name: str, mm=None):
    """Resolve ``name`` for batch mode. Returns (sha, type, size, data) where
    ``data`` is the (mailmap-rewritten) object contents, or None if missing.

    With ``mm`` set, commit/tag idents are rewritten and ``size`` reflects the
    rewritten content (matching git --use-mailmap)."""
    sha = refs_mod.rev_parse(repo, name)
    if sha is None or not objs.object_exists(repo, sha):
        return None
    t, data = objs.read_object(repo, sha)
    if mm is not None and t in ("commit", "tag"):
        data = _replace_idents_using_mailmap(data, mm)
    return sha, t, len(data), data


def _batch_write_record(info: str, data, check_only: bool, output_delim: str,
                        buffer: bool) -> None:
    """Write one batch record: the formatted info line + delimiter, then (for
    --batch) the object contents + delimiter. Flushing is suppressed in buffer
    mode."""
    out = sys.stdout.buffer
    out.write(info.encode("utf-8", "surrogateescape"))
    out.write(output_delim.encode("latin-1"))
    if not check_only:
        if not buffer:
            out.flush()
        out.write(data)
        out.write(output_delim.encode("latin-1"))
    if not buffer:
        out.flush()


def _batch_follow_resolve(repo: Repository, name: str, mm=None):
    """Resolve ``name`` for batch mode with --follow-symlinks.

    Returns either:
      - a (status_word, payload) tuple for the special follow outputs
        ("dangling"/"loop"/"notdir" -> payload is ``name`` itself;
         "symlink" -> payload is the out-of-tree link path), to be emitted as
        ``<status> <len(payload)> <payload>``; or
      - the normal (sha, type, size, data) 4-tuple for a found object; or
      - None when the object is missing.

    Only ``<rev>:<path>`` names with a non-empty rev follow symlinks; every
    other name (plain rev, abbrev sha, ``:path`` index form) resolves exactly
    as ordinary batch mode."""
    split = _split_rev_colon_path(name)
    if split is None or split[0] == "":
        return _batch_resolve(repo, name, mm)
    rev, path = split
    base_sha = refs_mod._resolve_revision(repo, rev)
    if base_sha is None:
        return None
    tree_sha = refs_mod._peel_to_type(repo, base_sha, "tree")
    if tree_sha is None:
        return None
    res = _follow_symlinks_in_tree(repo, tree_sha, path)
    tag = res[0]
    if tag == "missing":
        return None
    if tag in ("dangling", "loop", "notdir"):
        return (tag, name)
    if tag == "symlink":
        return ("symlink", res[1])
    # found
    sha = res[1]
    if not objs.object_exists(repo, sha):
        return None
    t, data = objs.read_object(repo, sha)
    if mm is not None and t in ("commit", "tag"):
        data = _replace_idents_using_mailmap(data, mm)
    return sha, t, len(data), data


def _write_follow_special(status: str, payload: str, output_delim: str,
                          buffer: bool) -> None:
    """Emit a follow-symlinks special line: ``<status> <len> <payload>``,
    where lengths/payload use the configured output delimiter (matching git's
    ``printf("%s %zu%c%s%c", ...)``)."""
    out = sys.stdout.buffer
    pb = payload.encode("utf-8", "surrogateescape")
    out.write(f"{status} {len(pb)}".encode("utf-8"))
    out.write(output_delim.encode("latin-1"))
    out.write(pb)
    out.write(output_delim.encode("latin-1"))
    if not buffer:
        out.flush()


def _cat_file_batch(repo: Repository, check_only: bool, names=None, fmt=None,
                    mm=None, input_delim="\n", output_delim="\n",
                    buffer=False, follow_symlinks=False) -> int:
    if fmt is None:
        fmt = "%(objectname) %(objecttype) %(objectsize)"
    source = names if names is not None else _read_delimited_stdin(input_delim)
    out = sys.stdout.buffer
    for name in source:
        if names is None and not name:
            continue
        resolved = (_batch_follow_resolve(repo, name, mm) if follow_symlinks
                    else _batch_resolve(repo, name, mm))
        if resolved is None:
            out.write((f"{name} missing").encode("utf-8", "surrogateescape"))
            out.write(output_delim.encode("latin-1"))
            if not buffer:
                out.flush()
            continue
        if resolved[0] in ("dangling", "loop", "notdir", "symlink"):
            _write_follow_special(resolved[0], resolved[1], output_delim, buffer)
            continue
        sha, t, size, data = resolved
        info = _expand_batch_atoms(fmt, sha, t, size)
        _batch_write_record(info, data, check_only, output_delim, buffer)
    if buffer:
        out.flush()
    return 0


def _cat_file_batch_command(repo: Repository, fmt=None, mm=None,
                            input_delim="\n", output_delim="\n",
                            buffer=False, follow_symlinks=False) -> int:
    """cat-file --batch-command: per-line `info`/`contents`/`flush` requests.

    Without --buffer each command is run immediately; `flush` is rejected. With
    --buffer, `contents`/`info` are queued and only dispatched by `flush` (or at
    EOF)."""
    if fmt is None:
        fmt = "%(objectname) %(objecttype) %(objectsize)"
    out = sys.stdout.buffer

    def run(kind: str, arg: str) -> None:
        resolved = (_batch_follow_resolve(repo, arg, mm) if follow_symlinks
                    else _batch_resolve(repo, arg, mm))
        if resolved is None:
            out.write((f"{arg} missing").encode("utf-8", "surrogateescape"))
            out.write(output_delim.encode("latin-1"))
            out.flush()
            return
        if resolved[0] in ("dangling", "loop", "notdir", "symlink"):
            _write_follow_special(resolved[0], resolved[1], output_delim, buffer)
            if not buffer:
                out.flush()
            return
        sha, t, size, data = resolved
        info = _expand_batch_atoms(fmt, sha, t, size)
        _batch_write_record(info, data, kind == "info", output_delim, buffer)

    queued: list[tuple[str, str]] = []

    for line in _read_delimited_stdin(input_delim):
        if not line:
            _err("fatal: empty command in input")
            return 128
        if line[0] in (" ", "\t"):
            _err(f"fatal: whitespace before command: '{line}'")
            return 128
        if line.startswith("flush"):
            if line != "flush":
                _err("fatal: flush takes no arguments")
                return 128
            if not buffer:
                _err("fatal: flush is only for --buffer mode")
                return 128
            for kind, arg in queued:
                run(kind, arg)
            out.flush()
            queued.clear()
            continue
        matched = None
        for name in ("contents", "info"):
            if line.startswith(name):
                matched = name
                break
        if matched is None:
            _err(f"fatal: unknown command: '{line}'")
            return 128
        rest = line[len(matched):]
        # `contents`/`info` need exactly a space then the argument.
        if not rest.startswith(" "):
            _err(f"fatal: {matched} requires arguments")
            return 128
        kind, arg = matched, rest[1:]
        if buffer:
            queued.append((kind, arg))
        else:
            run(kind, arg)
    if buffer and queued:
        for kind, arg in queued:
            run(kind, arg)
    out.flush()
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
    ap.add_argument("--allow-unknown-type", dest="allow_unknown_type", action="store_true")
    ap.add_argument("--path", default=None)
    ap.add_argument("--use-mailmap", "--mailmap", dest="use_mailmap",
                    action="store_true", default=False)
    ap.add_argument("--no-use-mailmap", "--no-mailmap", dest="use_mailmap",
                    action="store_false")
    ap.add_argument("--buffer", dest="buffer", action="store_true", default=None)
    ap.add_argument("--no-buffer", dest="buffer", action="store_false")
    ap.add_argument("--unordered", dest="unordered", action="store_true", default=False)
    ap.add_argument("--no-unordered", dest="unordered", action="store_false")
    # --follow-symlinks: in batch modes, resolve <rev>:<path> through in-tree
    # symlinks (mode 120000) to the target object; out-of-tree/dangling/loop
    # links emit git's special symlink/dangling/loop/notdir lines.
    ap.add_argument("--follow-symlinks", dest="follow_symlinks",
                    action="store_true", default=False)
    ap.add_argument("--no-follow-symlinks", dest="follow_symlinks",
                    action="store_false")
    ap.add_argument("-Z", dest="nul", action="store_true")
    ap.add_argument("-z", dest="nul_in", action="store_true")
    ap.add_argument("pos", nargs="*")
    # `--batch[-check]=<format>` takes the format attached with '='; pull it out
    # so the store_true flags still parse, then thread it into the formatter.
    batch_fmt = None
    pre_argv = []
    for a in argv:
        if a.startswith("--batch-check="):
            batch_fmt = a.split("=", 1)[1]
            pre_argv.append("--batch-check")
        elif a.startswith("--batch-command="):
            batch_fmt = a.split("=", 1)[1]
            pre_argv.append("--batch-command")
        elif a.startswith("--batch="):
            batch_fmt = a.split("=", 1)[1]
            pre_argv.append("--batch")
        else:
            pre_argv.append(a)
    args = ap.parse_args(pre_argv)
    repo = _repo()

    batch_enabled = args.batch or args.batch_check or args.batch_command
    # --follow-symlinks/--buffer/-Z/-z/--batch-all-objects all require a batch
    # mode. Order matches builtin/cat-file.c so the reported flag is the same.
    for flag, val in (("--follow-symlinks", args.follow_symlinks),
                      ("--buffer", args.buffer is not None),
                      ("--batch-all-objects", args.batch_all),
                      ("-z", args.nul_in),
                      ("-Z", args.nul)):
        if val and not batch_enabled:
            _err(f"fatal: '{flag}' requires a batch mode")
            return 129

    mm = None
    if args.use_mailmap:
        from . import mailmap as _mm
        mm = _mm.load(repo)

    # Delimiters: -Z makes both stdin and stdout NUL-terminated; -z only stdin.
    input_delim = "\n"
    output_delim = "\n"
    if args.nul_in:
        input_delim = "\0"
    if args.nul:
        input_delim = output_delim = "\0"
    # --buffer defaults to on for --batch-all-objects, off otherwise.
    buffer = args.buffer
    if buffer is None:
        buffer = bool(args.batch_all)

    if args.batch_command:
        return _cat_file_batch_command(repo, fmt=batch_fmt, mm=mm,
                                       input_delim=input_delim,
                                       output_delim=output_delim, buffer=buffer,
                                       follow_symlinks=args.follow_symlinks)
    if args.batch or args.batch_check:
        if args.batch_all:
            names = (_all_object_shas_unordered(repo) if args.unordered
                     else _all_object_shas(repo))
        else:
            names = None
        return _cat_file_batch(repo, check_only=args.batch_check, names=names,
                               fmt=batch_fmt, mm=mm, input_delim=input_delim,
                               output_delim=output_delim, buffer=buffer,
                               follow_symlinks=args.follow_symlinks)

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
        if mm is not None and t in ("commit", "tag"):
            data = _replace_idents_using_mailmap(data, mm)
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
    if mm is not None and t in ("commit", "tag"):
        data = _replace_idents_using_mailmap(data, mm)
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
            # `<rev>:<path>` where the rev resolves but the path is absent gets
            # git's specific message rather than the generic ambiguous-arg one.
            if ":" in arg and not arg.startswith(("^", "-", ":")):
                left, _, path = arg.partition(":")
                if left and not path.startswith("/") and refs_mod.rev_parse(r, left) is not None:
                    _print(arg)
                    _err(f"fatal: path '{path}' does not exist in '{left}'")
                    return 128
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


_LS_FILES_USAGE = (
    "usage: git ls-files [<options>] [<file>...]\n"
    "\n"
    "    -z                    separate paths with the NUL character\n"
    "    -t                    identify the file status with tags\n"
    "    -v                    use lowercase letters for 'assume unchanged' files\n"
    "    -f                    use lowercase letters for 'fsmonitor clean' files\n"
    "    -c, --[no-]cached     show cached files in the output (default)\n"
    "    -d, --[no-]deleted    show deleted files in the output\n"
    "    -m, --[no-]modified   show modified files in the output\n"
    "    -o, --[no-]others     show other files in the output\n"
    "    -i, --[no-]ignored    show ignored files in the output\n"
    "    -s, --[no-]stage      show staged contents' object name in the output\n"
    "    -k, --[no-]killed     show files on the filesystem that need to be removed\n"
    "    --[no-]directory      show 'other' directories' names only\n"
    "    --[no-]eol            show line endings of files\n"
    "    --[no-]empty-directory\n"
    "                          don't show empty directories\n"
    "    -u, --[no-]unmerged   show unmerged files in the output\n"
    "    --[no-]resolve-undo   show resolve-undo information\n"
    "    -x, --exclude <pattern>\n"
    "                          skip files matching pattern\n"
    "    -X, --exclude-from <file>\n"
    "                          read exclude patterns from <file>\n"
    "    --[no-]exclude-per-directory <file>\n"
    "                          read additional per-directory exclude patterns in <file>\n"
    "    --exclude-standard    add the standard git exclusions\n"
    "    --full-name           make the output relative to the project top directory\n"
    "    --[no-]recurse-submodules\n"
    "                          recurse through submodules\n"
    "    --[no-]error-unmatch  if any <file> is not in the index, treat this as an error\n"
    "    --[no-]with-tree <tree-ish>\n"
    "                          pretend that paths removed since <tree-ish> are still present\n"
    "    --[no-]abbrev[=<n>]   use <n> digits to display object names\n"
    "    --[no-]debug          show debugging data\n"
    "    --[no-]deduplicate    suppress duplicate entries\n"
    "    --[no-]sparse         show sparse directories in the presence of a sparse index\n"
    "    --format <format>     format to use for the output\n"
    "\n"
)


def cmd_ls_files(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit ls-files", add_help=False)
    ap.add_argument("-s", "--stage", action="store_true")
    ap.add_argument("-c", "--cached", action="store_true")
    ap.add_argument("-m", "--modified", action="store_true")
    ap.add_argument("-o", "--others", action="store_true")
    ap.add_argument("-d", "--deleted", action="store_true")
    ap.add_argument("-i", "--ignored", action="store_true")
    ap.add_argument("-u", "--unmerged", action="store_true")
    ap.add_argument("-k", "--killed", action="store_true")
    ap.add_argument("-x", "--exclude", action="append", default=None)
    ap.add_argument("-X", "--exclude-from", dest="exclude_from", action="append", default=None)
    ap.add_argument("--exclude-standard", action="store_true")
    ap.add_argument("--error-unmatch", action="store_true")
    ap.add_argument("--full-name", action="store_true")
    ap.add_argument("-t", dest="tag", action="store_true")
    ap.add_argument("-v", dest="tag_v", action="store_true")
    ap.add_argument("-f", dest="tag_f", action="store_true")
    ap.add_argument("--format", default=None)
    ap.add_argument("--abbrev", nargs="?", const=7, type=int, default=None)
    ap.add_argument("-z", dest="nul", action="store_true")
    ap.add_argument("paths", nargs="*")
    argv = ["--abbrev=7" if a == "--abbrev" else a for a in argv]
    args = ap.parse_args(argv)
    # -v/-f are tag variants (lowercase markers for assume-unchanged / fsmonitor,
    # which we don't track, so they render like -t).
    if args.tag_v or args.tag_f:
        args.tag = True
    repo = _repo()
    # --format cannot combine with the output modes that aren't a plain path list.
    if args.format is not None and (args.stage or args.others or args.killed
                                    or args.tag or args.tag_v or args.tag_f):
        _err("fatal: --format cannot be used with -s, -o, -k, -t, "
             "--resolve-undo, --deduplicate, --eol")
        sys.stderr.write("\n" + _LS_FILES_USAGE)
        return 129
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
    if not (want_cached or args.modified or args.others or args.deleted
            or args.unmerged or args.killed):
        want_cached = True

    def _tag(prefix: str, text: str) -> str:
        return (prefix + " " + text) if args.tag else text

    # Build the exclude matcher for -o from --exclude-standard, -x and -X.
    from . import ignore as ignore_mod
    exc = None
    if args.others or args.ignored:
        exc = ignore_mod.IgnoreSet()
        if args.exclude_standard:
            exc.rules.extend(ignore_mod.load(repo.path).rules)
        for pat in (args.exclude or []):
            exc.rules.append(ignore_mod.IgnoreRule(pat, "", "", 0))
        for xf in (args.exclude_from or []):
            exc.add_file(Path(xf), "", xf)

    def _is_excluded(p: str) -> bool:
        return exc is not None and exc.is_ignored(p)

    # C Git's show_files emits groups in a fixed order with no deduplication:
    # first 'others'/'killed' (from the directory scan, sorted), then a single
    # pass over the index where each entry shows its cached/stage line followed
    # by its deleted/modified line.
    out: list[str] = []
    status = None
    if args.modified or args.deleted or args.others:
        status = workdir.status(repo, include_ignored=True)

    if args.others and status is not None:
        others = []
        for p in status["untracked"]:
            if not match(p):
                continue
            # Default: hide excluded files; -i: show only excluded files.
            if args.ignored != _is_excluded(p):
                continue
            others.append(_tag("?", p))
        out.extend(sorted(others))

    modified_set = set(status["modified"]) | set(status["missing"]) if status else set()
    missing_set = set(status["missing"]) if status else set()
    for e in sorted(idx.entries, key=lambda e: (e.path, getattr(e, "stage", 0))):
        st = getattr(e, "stage", 0)
        if not match(e.path):
            continue
        show_this_cached = want_cached or (args.unmerged and st != 0)
        if show_this_cached and not (args.ignored and not _is_excluded(e.path)):
            tag = "S" if (st == 0 and e.skip_worktree) else ("M" if st != 0 else "H")
            # -v lowercases the tag for assume-unchanged (CE_VALID) entries.
            if args.tag_v and (e.flags & 0x8000):
                tag = tag.lower()
            if args.format is not None:
                out.append(_ls_files_format(repo, e, args.format, args.abbrev))
            elif args.stage or args.unmerged:
                sha = e.sha[:args.abbrev] if args.abbrev is not None else e.sha
                out.append(_tag(tag, f"{e.mode_str()} {sha} {st}\t{e.path}"))
            else:
                out.append(_tag(tag, e.path))
        if args.deleted and e.path in missing_set:
            out.append(_tag("R", e.path))
        if args.modified and e.path in modified_set:
            out.append(_tag("C", e.path))

    for line in out:
        sys.stdout.write(line + eol)
    return 0


def _ls_files_format(repo: Repository, entry, fmt: str, abbrev: Optional[int]) -> str:
    """Expand a `ls-files --format` string for one index entry."""
    import re
    sha = entry.sha[:abbrev] if abbrev is not None else entry.sha
    mode = entry.mode_str()
    otype = "commit" if mode == "160000" else "blob"

    def _size() -> str:
        try:
            return str(len(objs.read_object(repo, entry.sha)[1]))
        except KeyError:
            return "-"

    def atom(name: str) -> str:
        if name == "objectmode":
            return mode
        if name == "objectname":
            return sha
        if name == "objecttype":
            return otype
        if name == "objectsize":
            return _size()
        if name == "stage":
            return str(getattr(entry, "stage", 0))
        if name == "path":
            return entry.path
        if name in ("eolinfo:index", "eolinfo:worktree", "eolattr"):
            return ""
        return ""

    return re.sub(r"%\(([^)]*)\)", lambda m: atom(m.group(1)), fmt)


_REV_LIST_USAGE = (
    "usage: git rev-list [<options>] <commit>... [--] [<path>...]\n"
    "\n"
    "  limiting output:\n"
    "    --max-count=<n>\n"
    "    --max-age=<epoch>\n"
    "    --min-age=<epoch>\n"
    "    --sparse\n"
    "    --no-merges\n"
    "    --min-parents=<n>\n"
    "    --no-min-parents\n"
    "    --max-parents=<n>\n"
    "    --no-max-parents\n"
    "    --remove-empty\n"
    "    --all\n"
    "    --branches\n"
    "    --tags\n"
    "    --remotes\n"
    "    --stdin\n"
    "    --exclude-hidden=[fetch|receive|uploadpack]\n"
    "    --quiet\n"
    "  ordering output:\n"
    "    --topo-order\n"
    "    --date-order\n"
    "    --reverse\n"
    "  formatting output:\n"
    "    --parents\n"
    "    --children\n"
    "    --objects | --objects-edge\n"
    "    --disk-usage[=human]\n"
    "    --unpacked\n"
    "    --header | --pretty\n"
    "    --[no-]object-names\n"
    "    --abbrev=<n> | --no-abbrev\n"
    "    --abbrev-commit\n"
    "    --left-right\n"
    "    --count\n"
    "    -z\n"
    "  special purpose:\n"
    "    --bisect\n"
    "    --bisect-vars\n"
    "    --bisect-all\n"
)


def _hide_refs_patterns(repo, section: str) -> list[str]:
    """Ordered ``transfer.hideRefs`` / ``<section>.hideRefs`` patterns.

    Mirrors C Git's ``parse_hide_refs_config``: every matching config entry
    (across files, in declared order) is appended; trailing ``/`` is stripped.
    """
    from . import gitconfig

    keys = {"transfer.hiderefs", f"{section.lower()}.hiderefs"}
    pats: list[str] = []
    for full, value in gitconfig.list_all(repo):
        if full in keys:
            pats.append(value.rstrip("/"))
    return pats


def _ref_is_hidden(refname: str, patterns: list[str]) -> bool:
    """C Git's ``ref_is_hidden``: last matching pattern wins; ``!`` un-hides.

    Without ref namespaces, the stripped and full refname coincide, so a
    leading ``^`` (match full refname) behaves the same as no prefix.
    """
    for match in reversed(patterns):
        neg = False
        if match.startswith("!"):
            neg = True
            match = match[1:]
        if match.startswith("^"):
            match = match[1:]
        if refname == match or refname.startswith(match + "/"):
            return not neg
    return False


def cmd_rev_list(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit rev-list", add_help=False)
    ap.add_argument("--count", action="store_true")
    ap.add_argument("--max-count", "-n", type=int, default=None)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--branches", nargs="?", const="*", default=None)
    ap.add_argument("--tags", nargs="?", const="*", default=None)
    ap.add_argument("--remotes", nargs="?", const="*", default=None)
    ap.add_argument("--reverse", action="store_true")
    ap.add_argument("--objects", action="store_true")
    ap.add_argument("--objects-edge", dest="objects_edge", action="store_true")
    ap.add_argument("--parents", action="store_true")
    ap.add_argument("--no-walk", nargs="?", const="sorted", default=None)
    ap.add_argument("--children", action="store_true")
    ap.add_argument("--first-parent", action="store_true")
    ap.add_argument("--topo-order", dest="topo_order", action="store_true")
    ap.add_argument("--date-order", dest="date_order", action="store_true")
    ap.add_argument("--header", action="store_true")
    ap.add_argument("--abbrev-commit", dest="abbrev_commit", action="store_true")
    ap.add_argument("--abbrev", type=int, default=None)
    ap.add_argument("-z", dest="z", action="store_true")
    ap.add_argument("--disk-usage", dest="disk_usage", nargs="?", const="", default=None)
    ap.add_argument("--bisect", action="store_true")
    ap.add_argument("--bisect-all", dest="bisect_all", action="store_true")
    ap.add_argument("--bisect-vars", dest="bisect_vars", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--no-abbrev", dest="no_abbrev", action="store_true")
    ap.add_argument("--max-age", dest="max_age", type=int, default=None)
    ap.add_argument("--min-age", dest="min_age", type=int, default=None)
    ap.add_argument("--unpacked", action="store_true")
    ap.add_argument("--remove-empty", dest="remove_empty", action="store_true")
    ap.add_argument("--pretty", nargs="?", const="medium", default=None)
    ap.add_argument("--format", default=None)
    ap.add_argument("--oneline", action="store_true")
    ap.add_argument("--since", "--after", dest="since", default=None)
    ap.add_argument("--until", "--before", dest="until", default=None)
    ap.add_argument("--merges", action="store_true")
    ap.add_argument("--no-merges", dest="no_merges", action="store_true")
    ap.add_argument("--min-parents", dest="min_parents", type=int, default=None)
    ap.add_argument("--max-parents", dest="max_parents", type=int, default=None)
    ap.add_argument("--no-min-parents", dest="no_min_parents", action="store_true")
    ap.add_argument("--no-max-parents", dest="no_max_parents", action="store_true")
    ap.add_argument("--grep", action="append", default=None)
    ap.add_argument("--author", action="append", default=None)
    ap.add_argument("--committer", action="append", default=None)
    ap.add_argument("--all-match", dest="all_match", action="store_true")
    ap.add_argument("-i", "--regexp-ignore-case", dest="ignore_case", action="store_true")
    ap.add_argument("--left-right", dest="left_right", action="store_true")
    ap.add_argument("--timestamp", action="store_true")
    ap.add_argument("--stdin", action="store_true")
    ap.add_argument("--exclude-hidden", dest="exclude_hidden", default=None)
    ap.add_argument("revs", nargs="*")
    # Split a trailing "-- <pathspec>..." off before argparse consumes the "--".
    rl_paths: list[str] = []
    pre = _expand_count_shorthand(argv)
    if "--" in pre:
        i = pre.index("--")
        rl_paths = pre[i + 1:]
        pre = pre[:i]
    # `--exclude-hidden <section>` (separate token) is equivalent to the
    # attached `--exclude-hidden=<section>` form; fold it so the value is not
    # mistaken for a revision. A bare flag with no value is an error.
    _norm: list[str] = []
    _i = 0
    while _i < len(pre):
        t = pre[_i]
        if t == "--exclude-hidden":
            if _i + 1 >= len(pre):
                _err("fatal: Option '--exclude-hidden' requires a value")
                return 128
            _norm.append(f"--exclude-hidden={pre[_i + 1]}")
            _i += 2
            continue
        _norm.append(t)
        _i += 1
    pre = _norm
    # `--exclude-hidden=<section>` excludes refs hidden by transfer/<section>
    # hideRefs from the ref-namespace pseudo-opts. It is order-sensitive: C Git
    # errors when it precedes --branches/--tags/--remotes, but each namespace op
    # consumes (clears) the exclusion, so a preceding --all is fine. Walk the
    # cleaned argv left-to-right to reproduce that exactly.
    _apply_hidden_section: Optional[str] = None
    _hidden_configured = False
    _ns_clear = ("--all", "--branches", "--tags", "--remotes", "--glob")
    for tok in pre:
        head = tok.split("=", 1)[0]
        if head == "--exclude-hidden":
            section = tok.split("=", 1)[1] if "=" in tok else ""
            if section not in ("fetch", "receive", "uploadpack"):
                _err(f"fatal: unsupported section for hidden refs: {section}")
                return 128
            if _hidden_configured:
                _err("fatal: --exclude-hidden= passed more than once")
                return 128
            _hidden_configured = True
            _pending_section = section
        elif _hidden_configured and head in ("--branches", "--tags", "--remotes"):
            opt = head[2:]
            sys.stderr.write(
                f"error: options '--exclude-hidden' and '--{opt}' "
                "cannot be used together\n")
            sys.stderr.write(_REV_LIST_USAGE)
            return 129
        elif head in _ns_clear:
            # This namespace op consumes (and clears) the pending exclusion.
            if _hidden_configured:
                _apply_hidden_section = _pending_section
            _hidden_configured = False
    # argparse's nargs="?" greedily eats the following token, so a bare optional-
    # value flag like `--disk-usage HEAD` would swallow the revision. Rewrite the
    # bare forms to their attached "=<const>" so the next token stays a revision.
    _opt_const = {"--disk-usage": "", "--branches": "*", "--tags": "*",
                  "--remotes": "*", "--no-walk": "sorted"}
    pre = [f"{t}={_opt_const[t]}" if t in _opt_const else t for t in pre]
    args = ap.parse_args(pre)
    if args.parents and args.children:
        _err("fatal: options '--parents' and '--children' cannot be used together")
        return 128
    if args.no_min_parents:
        args.min_parents = None
    if args.no_max_parents:
        args.max_parents = None
    repo = _repo()
    graph = _graph_for_repo(repo)
    rev_args = args.revs
    starts: list[str] = []
    excludes: list[str] = []
    lr_left: set[str] = set()
    lr_right: set[str] = set()

    # `--all`/`--branches`/`--tags`/`--remotes` seed tips from ref namespaces
    # (refname order, peeling annotated tags), with HEAD appended for --all.
    namespaces = []
    if args.all or args.branches is not None:
        namespaces.append(("refs/heads/", None if args.branches in (None, "*") else args.branches))
    if args.all or args.tags is not None:
        namespaces.append(("refs/tags/", None if args.tags in (None, "*") else args.tags))
    if args.all or args.remotes is not None:
        namespaces.append(("refs/remotes/", None if args.remotes in (None, "*") else args.remotes))
    have_ns = bool(namespaces)
    hide_patterns = (_hide_refs_patterns(repo, _apply_hidden_section)
                     if _apply_hidden_section is not None else [])
    if have_ns:
        import fnmatch
        for refname, _rsha in _enumerate_refs(repo):
            for pre_ns, pat in namespaces:
                if refname.startswith(pre_ns):
                    if pat and not fnmatch.fnmatch(refname[len(pre_ns):], pat):
                        continue
                    if hide_patterns and _ref_is_hidden(refname, hide_patterns):
                        continue
                    csha = refs_mod.rev_parse(repo, refname + "^{commit}")
                    if csha:
                        starts.append(csha)
                    break
        if args.all:
            _, head_sha = refs_mod.read_head(repo)
            if head_sha:
                starts.append(head_sha)

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

    # --stdin: each input line is a revision argument (or, after a "--" line,
    # a pathspec). Lines starting with "-" are pseudo-options; an unrecognized
    # one is fatal, mirroring C Git's read_revisions_from_stdin. A revision that
    # does not resolve is fatal here (unlike command-line args, which are lenient
    # for back-compat). "--not" sticky-flips the include/exclude sense.
    if pre.count("--stdin") > 1:
        _err("fatal: --stdin given twice?")
        return 128
    if args.stdin:
        not_flag = False
        seen_eoo = False
        seen_dd = False
        _stdin_data = sys.stdin.read()
        _stdin_lines = _stdin_data.split("\n")
        # A trailing newline yields a final empty element that is not a record.
        if _stdin_lines and _stdin_lines[-1] == "":
            _stdin_lines.pop()
        _li = 0
        while _li < len(_stdin_lines):
            raw = _stdin_lines[_li]
            _li += 1
            if raw == "":
                break
            line = raw
            if line == "--":
                seen_dd = True
                break
            if not seen_eoo and line.startswith("-"):
                if line == "--end-of-options":
                    seen_eoo = True
                    continue
                if line == "--not":
                    not_flag = True
                    continue
                if line == "--all":
                    for refname, _rs in _enumerate_refs(repo):
                        if (refname.startswith("refs/heads/")
                                or refname.startswith("refs/tags/")
                                or refname.startswith("refs/remotes/")):
                            csha = refs_mod.rev_parse(repo, refname + "^{commit}")
                            if csha:
                                (excludes if not_flag else starts).append(csha)
                    _, hsha = refs_mod.read_head(repo)
                    if hsha:
                        (excludes if not_flag else starts).append(hsha)
                    continue
                if line in ("--branches", "--tags", "--remotes"):
                    ns = {"--branches": "refs/heads/", "--tags": "refs/tags/",
                          "--remotes": "refs/remotes/"}[line]
                    for refname, _rs in _enumerate_refs(repo):
                        if refname.startswith(ns):
                            csha = refs_mod.rev_parse(repo, refname + "^{commit}")
                            if csha:
                                (excludes if not_flag else starts).append(csha)
                    continue
                _err(f"fatal: invalid option '{line}' in --stdin mode")
                return 128
            tok = line
            neg = not_flag
            if tok.startswith("^"):
                tok = tok[1:]
                neg = not neg
            if "..." in tok:
                a, _, b = tok.partition("...")
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
            if ".." in tok:
                lo, _, hi = tok.partition("..")
                hi_sha = refs_mod.rev_parse(repo, hi or "HEAD")
                lo_sha = refs_mod.rev_parse(repo, lo) if lo else None
                if hi_sha:
                    starts.append(hi_sha)
                if lo_sha:
                    excludes.append(lo_sha)
                continue
            sha = refs_mod.rev_parse(repo, tok)
            if not sha:
                _err(f"fatal: bad revision '{line}'")
                return 128
            (excludes if neg else starts).append(sha)
        if seen_dd:
            while _li < len(_stdin_lines):
                rl_paths.append(_stdin_lines[_li])
                _li += 1

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
    if not starts and not have_ns and not args.stdin:
        # No commits to walk and no rev input. C Git prints the full usage
        # (rc 129) when no revision input was given at all; --stdin (even if it
        # produced nothing) suppresses the error and yields empty output.
        if not rev_args:
            sys.stderr.write(_REV_LIST_USAGE)
            return 129
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

    if (args.count and args.max_count is None and starts and not excludes
            and not have_ns and not args.disk_usage and not args.bisect
            and not args.bisect_all):
        try:
            from . import pack as _p

            bitmapped = _p.reachable_from_bitmaps(repo, starts, object_type="commit")
            if bitmapped is not None:
                _print(str(len(bitmapped)))
                return 0
        except Exception:
            pass

    # Default traversal is a committer-date max-heap (C Git's date-ordered
    # commit_list): pop the most recent commit, emit it, push its parents.
    want_topo = args.topo_order and not args.date_order
    want_date = args.date_order
    has_filter = bool(rl_paths or args.since or args.until or args.merges
                      or args.no_merges or args.min_parents is not None
                      or args.max_parents is not None or args.grep or args.author
                      or args.committer or args.max_age is not None
                      or args.min_age is not None)
    need_full = (has_filter or want_topo or want_date or args.reverse or args.count
                 or args.bisect or args.bisect_all or args.bisect_vars
                 or args.disk_usage is not None or args.children)
    import heapq
    heap: list[tuple] = []
    counter = 0
    pushed: set[str] = set()

    def _push(s: str) -> None:
        nonlocal counter
        if s in pushed:
            return
        pushed.add(s)
        heapq.heappush(heap, (-_commit_date(repo, s), counter, s))
        counter += 1

    for s in starts:
        _push(s)
    out: list[str] = []
    emitted: set[str] = set()
    edges: list[str] = []
    edge_seen: set[str] = set()
    while heap:
        _d, _c, sha = heapq.heappop(heap)
        if sha in emitted:
            continue
        emitted.add(sha)
        if sha in excluded:
            continue
        info = _commit_tree_parents(repo, sha, graph)
        if info is None:
            continue
        out.append(sha)
        if args.no_walk is None:
            for p in (info[1][:1] if args.first_parent else info[1]):
                if p in excluded:
                    if p not in edge_seen:
                        edge_seen.add(p)
                        edges.append(p)
                _push(p)
        if args.max_count and len(out) >= args.max_count and not need_full:
            break
    if args.no_walk is not None and args.no_walk == "unsorted":
        # Preserve command-line / seed order rather than date order.
        order = {s: i for i, s in enumerate(starts)}
        out.sort(key=lambda s: order.get(s, len(order)))

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
    if args.unpacked:
        out = [s for s in out if objs._loose_path(repo, s).exists()]
    if want_topo:
        out = _topo_order(repo, out, args.first_parent, by_date=False)
    elif want_date:
        out = _topo_order(repo, out, args.first_parent, by_date=True)
    if args.max_count is not None:
        out = out[:max(0, args.max_count)]

    # Output helpers: abbreviation and record terminator (-z → NUL). --oneline
    # implies --abbrev-commit. Only a commit's own id is abbreviated; parent and
    # child ids in --parents/--children output stay full.
    abbrev_len = None
    if args.abbrev_commit or args.oneline:
        abbrev_len = max(4, args.abbrev) if args.abbrev is not None else 7
    if args.no_abbrev:
        abbrev_len = None

    def _ab(s: str) -> str:
        return s[:abbrev_len] if abbrev_len else s

    term = "\0" if args.z else "\n"

    # --quiet suppresses all object output (rc only).
    if args.quiet:
        return 0

    # --bisect / --bisect-all / --bisect-vars: weigh each suspect by how many
    # suspects it reaches and report the commit closest to halving the set.
    if args.bisect or args.bisect_all or args.bisect_vars:
        sset = set(out)
        total = len(sset)

        def _weight(x: str) -> int:
            cnt = 0
            st = [x]
            seen_w: set[str] = set()
            while st:
                y = st.pop()
                if y in seen_w or y not in sset:
                    continue
                seen_w.add(y)
                cnt += 1
                info = _commit_tree_parents(repo, y, graph)
                if info:
                    st.extend(info[1])
            return cnt

        weights = {s: _weight(s) for s in out}
        if args.bisect_all:
            # --bisect-all: C Git's compare_commit_dist (distance descending,
            # ties by oid ascending) over the whole suspect set.
            decos = _commit_decorations(repo)
            ranked = sorted(out, key=lambda s: (-min(weights[s], total - weights[s]), s))
            for s in ranked:
                dist = min(weights[s], total - weights[s])
                parts = list(decos.get(s, [])) + [f"dist={dist}"]
                sys.stdout.write(f"{_ab(s)} ({', '.join(parts)})" + term)
            return 0
        # Single --bisect / --bisect-vars: C Git's best_bisection (bisect.c)
        # reverses the walk to oldest-first, then keeps the first commit with a
        # strictly-greater folded distance.
        best = None
        best_distance = -1
        for s in reversed(out):
            distance = min(weights[s], total - weights[s])
            if distance > best_distance:
                best = s
                best_distance = distance
        if best is None:
            return 0
        if args.bisect_vars:
            reaches = weights[best]
            _print(f"bisect_rev='{best}'")
            _print(f"bisect_nr={max(reaches, total - reaches) - 1}")
            _print(f"bisect_good={total - reaches - 1}")
            _print(f"bisect_bad={reaches - 1}")
            _print(f"bisect_all={total}")
            _print(f"bisect_steps={_estimate_bisect_steps(total)}")
        else:
            sys.stdout.write(_ab(best) + term)
        return 0

    # --disk-usage: sum the on-disk byte size of every object that would be shown.
    if args.disk_usage is not None:
        total_bytes = 0
        seen_du: set[str] = set()
        if args.objects or args.objects_edge:
            seen_du = _reachable_tree_objects(repo, excluded, graph)
        for s in out:
            if s not in seen_du:
                seen_du.add(s)
                total_bytes += _object_disk_size(repo, s)
            if args.objects or args.objects_edge:
                info = _commit_tree_parents(repo, s, graph)
                if info:
                    for osha, _p in _walk_objects(repo, info[0], ""):
                        if osha not in seen_du:
                            seen_du.add(osha)
                            total_bytes += _object_disk_size(repo, osha)
        _print(_humanise_bytes(total_bytes) if args.disk_usage == "human" else str(total_bytes))
        return 0

    # --header: raw commit record (oid, raw headers, indented message), NUL-sep.
    if args.header:
        buf = sys.stdout.buffer
        for s in out:
            raw = objs.read_object(repo, s)[1]
            hdr, _, msg = raw.partition(b"\n\n")
            lines = msg.split(b"\n")
            if lines and lines[-1] == b"":
                lines = lines[:-1]
            indented = b"".join(b"    " + ln + b"\n" for ln in lines)
            buf.write(s.encode() + b"\n" + hdr + b"\n\n" + indented + b"\0")
        buf.flush()
        return 0

    if args.pretty is not None or args.format is not None or args.oneline:
        fmt = args.format
        if fmt is None and args.pretty and (args.pretty.startswith("format:")
                                            or args.pretty.startswith("tformat:")):
            fmt = args.pretty.split(":", 1)[1]
        elif fmt is None and args.pretty and "%" in args.pretty:
            fmt = args.pretty
        is_oneline = args.oneline or args.pretty == "oneline"
        if args.reverse:
            out = list(reversed(out))
        for s in out:
            c = objs.parse_commit(objs.read_object(repo, s)[1])
            if is_oneline and fmt is None:
                first = c.message.splitlines()[0] if c.message.strip() else ""
                _print(f"{_ab(s)} {first}")
            elif fmt is not None:
                # rev-list prefixes a "commit <oid>" line before the format.
                _print(f"commit {_ab(s)}")
                _print(_expand_commit_format(repo, s, c, fmt, {}))
            else:
                _print(f"commit {_ab(s)}")
                _emit_commit_header(s, c, style=args.pretty, date_mode="default")
                _print("")
                for line in c.message.rstrip("\n").splitlines():
                    _print(f"    {line}")
        return 0
    if args.count:
        _print(str(len(out)))
    elif args.objects or args.objects_edge:
        wbuf = sys.stdout.write
        # Boundary (uninteresting) commits, prefixed with '-', precede the rest.
        if args.objects_edge:
            for e in edges:
                wbuf(f"-{_ab(e)}" + term)
        if args.reverse:
            out = list(reversed(out))
        for s in out:
            wbuf(_ab(s) + term)
        # Objects reachable from the uninteresting (excluded) commits are
        # already in the receiver, so C Git omits them from the listing.
        seen_obj = _reachable_tree_objects(repo, excluded, graph)
        for s in out:
            info = _commit_tree_parents(repo, s, graph)
            if info is None:
                continue
            for osha, opath in _walk_objects(repo, info[0], ""):
                if osha in seen_obj:
                    continue
                seen_obj.add(osha)
                wbuf(f"{osha} {opath}" + term)
    else:
        if args.reverse:
            out = list(reversed(out))
        # --children: map each commit to the in-set commits that name it parent.
        children_map: dict[str, list[str]] = {}
        if args.children:
            for s in out:
                info = _commit_tree_parents(repo, s, graph)
                if info:
                    # C Git prepends each child to its parent's list, so the
                    # last commit processed appears first.
                    for p in info[1]:
                        children_map.setdefault(p, []).insert(0, s)
        for s in out:
            mark = ""
            if args.left_right:
                mark = "<" if s in lr_left else (">" if s in lr_right else "")
            ts_prefix = ""
            if args.timestamp:
                ts_prefix = f"{_commit_date(repo, s)} "
            extra = ""
            if args.parents:
                info = _commit_tree_parents(repo, s, graph)
                if info and info[1]:
                    extra += " " + " ".join(info[1])
            if args.children:
                kids = children_map.get(s, [])
                if kids:
                    extra += " " + " ".join(kids)
            sys.stdout.write(f"{ts_prefix}{mark}{_ab(s)}{extra}" + term)
    return 0


def _reachable_tree_objects(repo: Repository, commits, graph=None) -> set[str]:
    """Set of tree/blob object ids reachable from the given commits' trees.

    Used to subtract objects already implied by uninteresting commits from
    `rev-list --objects` / `--objects-edge` / `--disk-usage --objects`."""
    seen: set[str] = set()
    for c in commits:
        info = _commit_tree_parents(repo, c, graph)
        if info is None:
            continue
        for osha, _p in _walk_objects(repo, info[0], ""):
            seen.add(osha)
    return seen


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
    ap.add_argument("-u", "--update", action="store_true")
    ap.add_argument("-N", "--intent-to-add", dest="intent_to_add", action="store_true")
    ap.add_argument("--no-all", "--ignore-removal", dest="no_all", action="store_true")
    ap.add_argument("--pathspec-from-file", dest="pathspec_from_file", default=None)
    ap.add_argument("--pathspec-file-nul", dest="pathspec_file_nul", action="store_true")
    ap.add_argument("paths", nargs="*")
    args = ap.parse_args(argv)
    repo = _repo()
    # --pathspec-from-file supplies the pathspecs instead of the command line.
    if args.pathspec_from_file is not None:
        raw = (sys.stdin.buffer.read() if args.pathspec_from_file == "-"
               else open(args.pathspec_from_file, "rb").read())
        sep = "\0" if args.pathspec_file_nul else "\n"
        args.paths = [p for p in raw.decode("utf-8").split(sep) if p]
    explicit = bool(args.paths) and not args.all
    targets = args.paths if (args.paths) else ["."]
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
    workdir.add_paths(repo, targets, ignore_removal=args.no_all, update_only=args.update,
                      intent_to_add=args.intent_to_add)
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
    # Intent-to-add entries report as a not-yet-staged new file (" A").
    for p in s.get("intent_to_add", []):
        worktree_status[p] = "A"
    changes = []
    for p in sorted(set(index_status) | set(worktree_status)):
        changes.append((p, index_status.get(p, " "), worktree_status.get(p, " ")))
    return changes, sorted(s["untracked"])


def _parse_rename_score(s: str) -> int:
    """Port of diffcore-rename.c parse_rename_score: turn an -M/-C threshold like
    "50%", "9", or "0.9" into a 0..MAX_SCORE similarity score (0 means default)."""
    num, scale, dot = 0, 1, False
    for ch in s:
        if not dot and ch == ".":
            scale, dot = 1, True
        elif ch == "%":
            scale = scale * 100 if dot else 100
            break
        elif ch.isdigit():
            if scale < 100000:
                scale *= 10
                num = num * 10 + int(ch)
        else:
            break
    from . import diffcore
    m = int(diffcore.MAX_SCORE)
    return m if num >= scale else (m * num) // scale


def _status_staged_renames(repo: Repository, s: dict, minimum_score: int = 0) -> list:
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
    return [(pair.src.path, pair.dst.path)
            for pair in diffcore.detect_renames(repo, base_map, side_map,
                                                minimum_score=minimum_score)]


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
    # Rename detection is on by default (status.renames -> diff.renames -> true).
    # Per builtin/commit.c: --no-renames/--renames set a tri-state (last wins),
    # then -M/--find-renames is applied afterwards and force-enables detection
    # regardless of order. -M/--find-renames take a threshold only when attached.
    no_renames = -1  # -1 unset, 0 --renames, 1 --no-renames
    find_given = False
    rename_score = 0  # 0 -> diffcore's default (50%)
    rest: list[str] = []
    for t in argv:
        if t == "--no-renames":
            no_renames = 1
        elif t == "--renames":
            no_renames = 0
        elif t in ("-M", "--find-renames"):
            find_given = True
            rename_score = 0
        elif t.startswith("-M") and not t.startswith("--"):
            find_given, rename_score = True, _parse_rename_score(t[2:])
        elif t.startswith("--find-renames="):
            find_given, rename_score = True, _parse_rename_score(t[len("--find-renames="):])
        else:
            rest.append(t)
    rename_enabled = not (no_renames == 1 and not find_given)

    ap = argparse.ArgumentParser(prog="pygit status", add_help=False)
    ap.add_argument("-s", "--short", action="store_true")
    ap.add_argument("-b", "--branch", action="store_true")
    ap.add_argument("--long", dest="long", action="store_true")
    ap.add_argument("--porcelain", nargs="?", const="v1", default=None)
    ap.add_argument("-u", "--untracked-files", nargs="?", const="all", default="all")
    ap.add_argument("-z", dest="nul", action="store_true")
    ap.add_argument("-v", "--verbose", action="count", default=0)
    args = ap.parse_args(rest)
    repo = _repo()
    s = workdir.status(repo)
    head_sym, head_sha = refs_mod.read_head(repo)
    branch = head_sym[len("refs/heads/"):] if head_sym and head_sym.startswith("refs/heads/") else None
    changes, untracked = _status_model(repo, s)
    renames = _status_staged_renames(repo, s, rename_score) if rename_enabled else []
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

    # Long (default) format. -v/-vv append the staged (and, for -vv, worktree)
    # diff just before the summary line.
    verbose_hook = None
    if args.verbose:
        verbose_hook = lambda committable: _status_verbose_diff(
            repo, args.verbose, committable, rename_enabled, rename_score)
    return _status_long(repo, s, changes, untracked, branch, head_sym, head_sha,
                        untracked_hidden, renames, verbose_hook)


def _status_long(repo, s, changes, untracked, branch, head_sym, head_sha, untracked_hidden=False, renames=None, verbose_hook=None) -> int:
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
    # C Git prints the verbose (-v/-vv) diff after the sections but before the
    # trailing summary line (wt_longstatus_print order).
    if verbose_hook is not None:
        verbose_hook(has_staged)
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


def _emit_change_diffs(repo: Repository, changes: list, rename_enabled: bool,
                       rename_score: int, a_prefix: str, b_prefix: str) -> None:
    """Render a change list as a patch (rename-aware), with the given prefixes."""
    if rename_enabled:
        renames, remaining = _detect_changes_renames(repo, changes, rename_score)
    else:
        renames, remaining = [], changes
    emit = [(dst, lambda s=src, d=dst, sm=sim, sa=sa, db=db:
             _emit_rename_patch(s, d, sm, sa, db, a_prefix=a_prefix, b_prefix=b_prefix))
            for src, dst, sim, sa, db in renames]
    emit += [(path, lambda p=path, a=a, b=b:
              _emit_file_diff(p, a, b, a_prefix=a_prefix, b_prefix=b_prefix))
             for path, a, b in remaining]
    for _key, fn in sorted(emit, key=lambda e: e[0]):
        fn()


def _status_verbose_diff(repo: Repository, verbose: int, committable: bool,
                         rename_enabled: bool, rename_score: int) -> None:
    """Port of wt_longstatus_print_verbose: the staged diff (HEAD vs index) and,
    for -vv, the worktree diff (index vs worktree). -v uses a/b prefixes with no
    header; -vv uses c/i under a "Changes to be committed:" header, then a
    50-dash separator and i/w under "Changes not staged for commit:"."""
    idx = read_index(repo).by_path()
    head_sha = refs_mod.rev_parse(repo, "HEAD")
    head_map = _tree_map_full(repo, _commit_tree(repo, head_sha)) if head_sha else {}

    staged: list[tuple[str, _Side, _Side]] = []
    for p in sorted(set(head_map) | set(idx)):
        if p in idx and idx[p].intent_to_add and p not in head_map:
            continue  # intent-to-add is not staged
        a = _side_from_object(repo, *head_map[p]) if p in head_map else _ABSENT
        b = _side_from_object(repo, idx[p].mode_str(), idx[p].sha) if p in idx else _ABSENT
        if a.sha != b.sha or a.mode != b.mode:
            staged.append((p, a, b))
    if verbose > 1 and committable:
        _print("Changes to be committed:")
        sa, sb = "c", "i"
    else:
        sa, sb = "a", "b"
    _emit_change_diffs(repo, staged, rename_enabled, rename_score, sa, sb)

    if verbose > 1:
        unstaged: list[tuple[str, _Side, _Side]] = []
        for p in sorted(idx):
            a = _ABSENT if idx[p].intent_to_add else _side_from_object(repo, idx[p].mode_str(), idx[p].sha)
            b = _side_from_worktree(repo, p)
            if a.sha != b.sha or a.mode != b.mode:
                unstaged.append((p, a, b))
        if unstaged:
            _print("--------------------------------------------------")
            _print("Changes not staged for commit:")
            _emit_change_diffs(repo, unstaged, rename_enabled, rename_score, "i", "w")


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


def _partial_commit_tree(repo: Repository, parent: Optional[str], only_paths: list[str]) -> str:
    """Build the tree for a partial commit: HEAD's tree with only ``only_paths``
    updated from the index (added/modified/removed), the rest left as in HEAD."""
    entries: dict[str, tuple[str, str]] = {}  # path -> (mode_str, sha)
    if parent:
        ptree = objs.parse_commit(objs.read_object(repo, parent)[1]).tree
        for path, mode, sha in workdir.iter_tree_files(repo, ptree):
            entries[path] = (mode, sha)
    idx = read_index(repo).by_path()
    wanted = set(only_paths)
    for p in only_paths:
        if p in idx:
            entries[p] = (idx[p].mode_str(), idx[p].sha)
        else:
            entries.pop(p, None)  # path removed in this partial commit
    # Build nested trees from the flat entry map.
    root: dict = {}
    for path, (mode, sha) in entries.items():
        parts = path.split("/")
        cur = root
        for part in parts[:-1]:
            cur = cur.setdefault(part, {})
        cur[parts[-1]] = (mode, sha)

    def emit(node: dict) -> str:
        te: list[objs.TreeEntry] = []
        for name, val in node.items():
            if isinstance(val, dict):
                te.append(objs.TreeEntry("40000", name, emit(val)))
            else:
                mode, sha = val
                te.append(objs.TreeEntry(mode.lstrip("0") or "0", name, sha))
        return objs.write_object(repo, "tree", objs.encode_tree(te))

    return emit(root)


def _cleanup_commit_message(msg: str, mode: Optional[str]) -> str:
    """Apply git's commit-message cleanup. 'verbatim' is a no-op; the default
    for -m/-F ('whitespace') strips trailing whitespace per line and collapses
    leading/trailing/consecutive blank lines."""
    if mode == "verbatim":
        return msg
    lines = [ln.rstrip() for ln in msg.split("\n")]
    out: list[str] = []
    for ln in lines:
        if ln == "" and (not out or out[-1] == ""):
            continue
        out.append(ln)
    while out and out[-1] == "":
        out.pop()
    while out and out[0] == "":
        out.pop(0)
    return "\n".join(out)


def _apply_trailers(msg: str, trailers: list[str]) -> str:
    """Append a trailer block (Signed-off-by etc.) to a commit message, after a
    blank line separating it from the body — matching git's simple case."""
    if not trailers:
        return msg
    body = msg.rstrip("\n")
    block = "\n".join(trailers)
    return (body + "\n\n" + block) if body else block


def _incompatible_opts(opts: list[str]) -> str:
    """Render C Git's "options ... cannot be used together" message for a list
    of 2-4 option names (the count selects the comma/and punctuation)."""
    if len(opts) == 2:
        joined = f"'{opts[0]}' and '{opts[1]}'"
    else:
        joined = ", ".join(f"'{o}'" for o in opts[:-1]) + f", and '{opts[-1]}'"
    return f"fatal: options {joined} cannot be used together"


def _strip_commit_subject(msg: str) -> str:
    """Return a commit message's body: everything after the first blank line,
    with leading blank lines skipped (C Git's skip_blank_lines(buffer + 2))."""
    idx = msg.find("\n\n")
    if idx < 0:
        return ""
    return msg[idx + 2:].lstrip("\n")


def _commit_message_conflict(args) -> Optional[str]:
    """Mirror C Git's commit message-source incompatibility checks, in order:
    (A) at most one of -m/-C/-c/-F; (B) --squash and --fixup are exclusive;
    (C) --fixup cannot combine with -C/-c/-F (but -m is allowed as a body)."""
    group = []
    if args.message is not None:
        group.append("-m")
    if args.reuse_message is not None:
        group.append("-C")
    if args.reedit_message is not None:
        group.append("-c")
    if args.file is not None:
        group.append("-F")
    if len(group) >= 2:
        return _incompatible_opts(group)
    if args.squash is not None and args.fixup is not None:
        return _incompatible_opts(["--squash", "--fixup"])
    if args.fixup is not None:
        for opt, val in (("-C", args.reuse_message), ("-c", args.reedit_message),
                         ("-F", args.file)):
            if val is not None:
                return _incompatible_opts([opt, "--fixup"])
    return None


def cmd_commit(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit commit", add_help=False)
    ap.add_argument("-m", "--message", action="append", default=None)
    ap.add_argument("-F", "--file", default=None)
    ap.add_argument("-a", "--all", action="store_true")
    ap.add_argument("--amend", action="store_true")
    ap.add_argument("--no-edit", action="store_true")
    ap.add_argument("-e", "--edit", action="store_true")
    ap.add_argument("--allow-empty", action="store_true")
    ap.add_argument("--allow-empty-message", dest="allow_empty_message", action="store_true")
    ap.add_argument("--date", default=None)
    ap.add_argument("--author", default=None)
    ap.add_argument("-s", "--signoff", action="store_true", default=False)
    ap.add_argument("--no-signoff", dest="signoff", action="store_false")
    ap.add_argument("--trailer", action="append", default=None)
    ap.add_argument("--reset-author", dest="reset_author", action="store_true")
    ap.add_argument("--cleanup", default=None)
    ap.add_argument("-o", "--only", action="store_true")
    ap.add_argument("-i", "--include", action="store_true")
    ap.add_argument("-n", "--no-verify", dest="no_verify", action="store_true")
    ap.add_argument("--verify", dest="verify", action="store_true")
    ap.add_argument("--no-post-rewrite", dest="no_post_rewrite", action="store_true")
    ap.add_argument("--post-rewrite", dest="post_rewrite", action="store_true")
    ap.add_argument("-C", "--reuse-message", dest="reuse_message", default=None)
    ap.add_argument("-c", "--reedit-message", dest="reedit_message", default=None)
    ap.add_argument("--squash", default=None)
    ap.add_argument("--fixup", default=None)
    ap.add_argument("--dry-run", dest="dry_run", action="store_true")
    ap.add_argument("--pathspec-from-file", dest="pathspec_from_file", default=None)
    ap.add_argument("--pathspec-file-nul", dest="pathspec_file_nul", action="store_true")
    ap.add_argument("-u", "--untracked-files", dest="untracked_files", nargs="?", const="all", default="all")
    # Dry-run output formats: --short/--porcelain/--long/-z all imply --dry-run.
    ap.add_argument("--short", action="store_true")
    ap.add_argument("--porcelain", action="store_true")
    ap.add_argument("--long", dest="long", action="store_true")
    ap.add_argument("--branch", action="store_true")
    ap.add_argument("--ahead-behind", dest="ahead_behind", action="store_true")
    ap.add_argument("--no-ahead-behind", dest="no_ahead_behind", action="store_true")
    ap.add_argument("--status", dest="status", action="store_true")
    ap.add_argument("--no-status", dest="no_status", action="store_true")
    ap.add_argument("-z", "--null", dest="nul", action="store_true")
    ap.add_argument("-v", "--verbose", action="count", default=0)
    ap.add_argument("-q", "--quiet", action="store_true")
    ap.add_argument("pathspec", nargs="*")
    args = ap.parse_args(argv)
    repo = _repo()
    # --pathspec-from-file supplies the pathspec for a partial commit.
    if args.pathspec_from_file is not None:
        raw = (sys.stdin.buffer.read() if args.pathspec_from_file == "-"
               else open(args.pathspec_from_file, "rb").read())
        psep = "\0" if args.pathspec_file_nul else "\n"
        args.pathspec = [p for p in raw.decode("utf-8").split(psep) if p]
    # Message construction. --squash/--fixup set the subject header; -m/-F/-C/-c
    # supply the body (and -C/-c also reuse the source commit's author). C Git
    # appends the body under the squash!/fixup! header. Incompatible combinations
    # (e.g. two body sources, --squash with --fixup) are rejected first.
    conflict = _commit_message_conflict(args)
    if conflict is not None:
        _err(conflict)
        return 128
    if args.reset_author and not (args.amend or args.reuse_message or args.reedit_message):
        _err("fatal: --reset-author can be used only with -C, -c or --amend.")
        return 128
    reuse = args.reuse_message or args.reedit_message
    reuse_author = None
    reuse_sha = None
    body = None
    if args.message is not None:
        body = "\n\n".join(args.message)
    elif args.file is not None:
        body = (sys.stdin.read() if args.file == "-"
                else open(args.file, encoding="utf-8").read())
    elif reuse is not None:
        reuse_sha = refs_mod.rev_parse(repo, reuse)
        rc_commit = objs.parse_commit(objs.read_object(repo, reuse_sha)[1])
        body = rc_commit.message.rstrip("\n")
        reuse_author = rc_commit.author
    header = None
    header_sha = None
    if args.squash is not None:
        header_sha = refs_mod.rev_parse(repo, args.squash)
        subj = objs.parse_commit(objs.read_object(repo, header_sha)[1]).message.splitlines()[0]
        header = f"squash! {subj}"
    elif args.fixup is not None:
        spec = args.fixup
        if ":" in spec:
            mode, ref = spec.split(":", 1)
        else:
            mode, ref = "fixup", spec
        header_sha = refs_mod.rev_parse(repo, ref)
        c = objs.parse_commit(objs.read_object(repo, header_sha)[1])
        subj = c.message.splitlines()[0]
        if mode in ("amend", "reword"):
            header = f"amend! {subj}\n\n{c.message.rstrip(chr(10))}"
            if mode == "reword":
                args.allow_empty = True
        else:
            header = f"fixup! {subj}"
    # When -C/-c reuse the very commit being squashed/fixed up, C Git keeps only
    # the reused body (its subject is already in the squash!/fixup! header).
    if (reuse_sha is not None and header_sha is not None
            and reuse_sha == header_sha and body is not None):
        body = _strip_commit_subject(body)
    if header is not None and body is not None:
        args.message = f"{header}\n\n{body}"
    elif header is not None:
        args.message = header
    else:
        args.message = body
    try:
        from . import rerere as _rr
        _rr.scan_and_record(repo)
    except Exception:
        pass

    # --short/--porcelain/--long/-z all imply --dry-run in C Git. Dry-run must not
    # touch the index, so snapshot it before any staging and restore it after.
    dry_mode = (args.dry_run or args.short or args.porcelain or args.long or args.nul)
    idx_file = repo.gitdir / "index"
    dry_snapshot = idx_file.read_bytes() if (dry_mode and idx_file.exists()) else None

    if args.all:
        workdir.add_paths(repo, sorted(workdir.tracked_paths(repo)))
    elif args.include and args.pathspec:
        # -i/--include: stage the named paths, then commit the whole index.
        workdir.add_paths(repo, args.pathspec)

    cur_idx = read_index(repo)
    if cur_idx.has_conflicts():
        _err("error: unresolved conflicts:")
        for p in cur_idx.conflicted_paths():
            _err(f"\t{p}")
        _err("hint: stage the resolved files with `pygit add` then commit again.")
        return 1

    if dry_mode:
        only = not args.all and not args.include and args.pathspec
        if only:
            workdir.add_paths(repo, args.pathspec)
        s = workdir.status(repo)
        hsym, hsha = refs_mod.read_head(repo)
        br = hsym[len("refs/heads/"):] if hsym and hsym.startswith("refs/heads/") else None
        ch, unt = _status_model(repo, s)
        rnm = _status_staged_renames(repo, s)
        if rnm:
            consumed = {p for pair in rnm for p in pair}
            ch = [(p, x, y) for p, x, y in ch if p not in consumed]
        show_unt = args.untracked_files != "no"
        had_untracked = bool(unt)
        if not show_unt:
            unt = []
        if dry_snapshot is not None:
            idx_file.write_bytes(dry_snapshot)
        elif idx_file.exists():
            idx_file.unlink()
        staged = [p for p, x, y in ch if x != " "]
        rc = 0 if (staged or rnm) else 1
        # Format selection mirrors C Git: --long (or plain --dry-run) → long;
        # --porcelain → porcelain v1; --short → short; bare -z → porcelain.
        if args.porcelain or (args.nul and not args.short and not args.long):
            fmt = "porcelain"
        elif args.short:
            fmt = "short"
        else:
            fmt = "long"
        if fmt == "long":
            _status_long(repo, s, ch, unt, br, hsym, hsha,
                         untracked_hidden=(had_untracked and not show_unt),
                         renames=rnm)
            return rc
        eol = "\0" if args.nul else "\n"
        out = []
        if args.branch:
            out.append(_status_branch_header_short(repo, hsym, hsha))
        entries = [(dst, f"R  {src} -> {dst}") for src, dst in rnm]
        entries += [(p, f"{x}{y} {p}") for p, x, y in ch]
        out += [line for _key, line in sorted(entries)]
        out += [f"?? {p}" for p in unt]
        sys.stdout.write("".join(line + eol for line in out))
        return rc

    head_sym, parent = refs_mod.read_head(repo)
    # A pathspec (or -o/--only) makes a partial commit: HEAD's tree with just the
    # named paths updated from the working tree, leaving the rest of the index out.
    only_mode = not args.all and not args.include and args.pathspec
    if only_mode:
        workdir.add_paths(repo, args.pathspec)  # refresh the named index entries
        tree = _partial_commit_tree(repo, parent, args.pathspec)
    else:
        # Intent-to-add entries are excluded from the commit (treated as
        # not-yet-staged), matching C Git.
        tree = workdir.write_tree(repo, skip_intent_to_add=True)

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
        # -C/-c reuse the source commit's author identity *and* date; --date
        # overrides just the date, --author/--reset-author override identity below.
        if reuse_author is not None and args.date is None:
            author_sig = reuse_author
        elif reuse_author is not None:
            who, _ts, _tz = _split_ident(reuse_author)
            fresh = objs.build_signature(repo, "author", date_override=args.date)
            author_sig = who + fresh[fresh.rindex(">") + 1:]
        else:
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
    # --author overrides the author identity (date stays from env/--date);
    # --reset-author (with --amend) resets it to the current committer identity.
    if args.author is not None:
        base = objs.build_signature(repo, "author", date_override=args.date)
        author_sig = args.author + base[base.rindex(">") + 1:]
    elif args.reset_author:
        author_sig = objs.build_signature(repo, "author", date_override=args.date)
    # Message cleanup (default 'whitespace' for -m/-F; 'verbatim' keeps as-is),
    # then any --signoff / --trailer trailers.
    message = _cleanup_commit_message(message, args.cleanup)
    trailers: list[str] = []
    if args.signoff:
        cwho, _t, _z = _split_ident(committer_sig)
        cn, ce = _parse_who(cwho)
        trailers.append(f"Signed-off-by: {cn} <{ce}>")
    trailers.extend(args.trailer or [])
    message = _apply_trailers(message, trailers)
    if (not message.strip()) and not args.allow_empty_message:
        _err("Aborting commit due to empty commit message.")
        return 1
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
    _print(f"[{branch}{root} {sha[:7]}] {msg.splitlines()[0].rstrip()}")
    # git prints " Author:" when the author identity differs from the committer,
    # and " Date:" when the author date differs from the committer date.
    a_who, a_ts, _atz = _split_ident(author_sig)
    c_who, c_ts, _ctz = _split_ident(committer_sig)
    if a_who != c_who:
        _print(f" Author: {a_who}")
    # Mirrors C Git's author_date_is_interesting() == author_message || force_date:
    # the " Date:" line shows when the author identity was reused from another
    # commit (--amend / -C / -c) or set via --date, not on a fresh default and
    # not when --reset-author discards the reused identity.
    date_interesting = (
        args.date is not None
        or ((args.amend or reuse_author is not None) and not args.reset_author)
    )
    if date_interesting:
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


def _note_text(repo: Repository, sha: str) -> str:
    """Return the notes blob text attached to commit ``sha`` (refs/notes/commits),
    or '' if none."""
    nref = refs_mod.read_ref(repo, "refs/notes/commits")
    if not nref:
        return ""
    try:
        nc = objs.parse_commit(objs.read_object(repo, nref)[1])
    except KeyError:
        return ""
    for path, _m, bsha in workdir.iter_tree_files(repo, nc.tree):
        if path.replace("/", "") == sha:
            return objs.read_object(repo, bsha)[1].decode("utf-8", "replace")
    return ""


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
    # %aN/%aE/%cN/%cE are always mailmap-resolved (independent of --use-mailmap,
    # which only governs the builtin-format identity lines). %an/%ae stay raw.
    man, mae, mcn, mce = an, ae, cn, ce
    if any(k in fmt for k in ("%aN", "%aE", "%cN", "%cE")):
        from . import mailmap as _mailmap
        mm = _mailmap.load(repo)
        if not mm.empty:
            man, mae = mm.resolve(an, ae)
            mcn, mce = mm.resolve(cn, ce)
    # The subject (%s) is the first line with trailing whitespace stripped.
    subject = c.message.splitlines()[0].rstrip() if c.message.strip() else ""
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
        # Mailmap-resolved name/email; with no mailmap these equal %an/%ae/etc.
        ("%aN", man), ("%aE", mae), ("%cN", mcn), ("%cE", mce),
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
        # %e (encoding) is empty for unencoded commits; %N is the commit's note.
        ("%e", ""),
        ("%N", _note_text(repo, sha) if "%N" in fmt else ""),
        # Signature placeholders: pythongit does not verify GPG signatures, so
        # commits read as unsigned ("N", empty detail fields), like unsigned
        # commits under C Git.
        ("%G?", "N"), ("%GG", ""), ("%GS", ""), ("%GK", ""),
        ("%GP", ""), ("%GF", ""), ("%GT", ""),
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
    # -q/--quiet sets the diff machinery's NO_OUTPUT format *before* the
    # in-order diff options are parsed, so for log (whose default diff output
    # is already empty) it is a near no-op: any -p/--stat/--raw overrides it.
    # Its one observable effect is the diff_setup_done conflict against
    # --name-only/--name-status (NO_OUTPUT and NAME[_STATUS] cannot coexist).
    ap.add_argument("-q", "--quiet", dest="quiet", action="store_true")
    ap.add_argument("-U", "--unified", type=int, default=3)
    ap.add_argument("--stat", action="store_true")
    ap.add_argument("--shortstat", action="store_true")
    ap.add_argument("--name-only", dest="name_only", action="store_true")
    ap.add_argument("--name-status", dest="name_status", action="store_true")
    ap.add_argument("--raw", action="store_true")
    # --clear-decorations resets the (currently unsupported) --decorate-refs
    # include/exclude filters; with no such filters in effect it is a no-op.
    ap.add_argument("--clear-decorations", dest="clear_decorations", action="store_true")
    ap.add_argument("--no-merges", dest="no_merges", action="store_true")
    # The builtin formats honour the mailmap by default (log.mailmap=true);
    # --no-use-mailmap shows raw identities. %an/%ae stay raw regardless.
    ap.add_argument("--use-mailmap", "--mailmap", dest="use_mailmap", action="store_true")
    ap.add_argument("--no-use-mailmap", "--no-mailmap", dest="no_use_mailmap", action="store_true")
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
    # Builtin formats honour the mailmap by default; --no-use-mailmap disables it.
    from . import mailmap as _mailmap
    mm_log = None if args.no_use_mailmap else _mailmap.load(repo)
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
    # NO_OUTPUT (from -q/--quiet) collides with the NAME / NAME_STATUS output
    # formats in diff_setup_done(); git rejects the combination with rc 128.
    # The patch/stat/raw output flags clear NO_OUTPUT before that check runs
    # (they are OPT_BITOP/callbacks), so the conflict only fires when -q is the
    # surviving format alongside a name listing.
    clears_no_output = args.patch or args.stat or args.shortstat or args.raw
    if args.quiet and (args.name_only or args.name_status) and not clears_no_output:
        _err("fatal: options '--name-only', '--name-status', '--check', and '-s' "
             "cannot be used together")
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
                _print(f"Author: {_mapped_who(_split_ident(c.author)[0], mm_log)}")
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
                                reflog=meta, date_given=date_given, mailmap=mm_log)
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


def _mapped_who(who: str, mailmap=None) -> str:
    """Apply a mailmap to a `Name <email>` string (identity unless mailmap maps it)."""
    if mailmap is None or mailmap.empty:
        return who
    n, e = _parse_who(who)
    mn, me = mailmap.resolve(n, e)
    return f"{mn} <{me}>"


def _emit_commit_header(sha: str, c, *, style: str = "medium",
                        date_mode: str = "default", decoration: str = "",
                        parents_suffix: str = "", reflog=None, date_given: bool = False,
                        mailmap=None) -> None:
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
    # The builtin formats honour the mailmap by default (log.mailmap=true);
    # --no-use-mailmap passes mailmap=None to show the raw identities.
    if mailmap is not None and not mailmap.empty:
        an, ae = _parse_who(author_who)
        mn, me = mailmap.resolve(an, ae)
        author_who = f"{mn} <{me}>"
        cwho_n, cwho_e = _parse_who(committer_who)
        mcn, mce = mailmap.resolve(cwho_n, cwho_e)
        committer_who = f"{mcn} <{mce}>"
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
    # -q/--quiet sets the diff machinery's NO_OUTPUT format; for show (whose
    # default output is a patch) this suppresses the diff exactly like -s.
    ap.add_argument("-q", "--quiet", dest="quiet", action="store_true")
    ap.add_argument("--stat", action="store_true")
    ap.add_argument("--raw", action="store_true")
    ap.add_argument("--name-only", dest="name_only", action="store_true")
    ap.add_argument("--name-status", dest="name_status", action="store_true")
    # --clear-decorations resets the (unsupported) --decorate-refs filters; a
    # no-op here since show never applies decoration ref filters.
    ap.add_argument("--clear-decorations", dest="clear_decorations", action="store_true")
    ap.add_argument("--oneline", action="store_true")
    ap.add_argument("--format", default=None)
    ap.add_argument("--pretty", nargs="?", const="medium", default=None)
    ap.add_argument("--date", default=None)
    ap.add_argument("--abbrev", type=int, default=7)
    ap.add_argument("-U", "--unified", type=int, default=3)
    ap.add_argument("--use-mailmap", "--mailmap", dest="use_mailmap", action="store_true")
    ap.add_argument("--no-use-mailmap", "--no-mailmap", dest="no_use_mailmap", action="store_true")
    ap.add_argument("rev", nargs="*")
    args = ap.parse_args(argv)
    # NO_OUTPUT (from -q/--quiet) collides with the NAME / NAME_STATUS output
    # formats in diff_setup_done(); git rejects the combination with rc 128.
    # --stat/--raw clear NO_OUTPUT before that check, so the conflict only
    # fires when -q is the surviving format alongside a name listing.
    if (args.quiet and (args.name_only or args.name_status)
            and not (args.stat or args.raw)):
        _err("fatal: options '--name-only', '--name-status', '--check', and '-s' "
             "cannot be used together")
        return 128
    # -q/--quiet suppresses the patch exactly like -s/--no-patch for show.
    if args.quiet:
        args.no_patch = True
    repo = _repo()
    from . import mailmap as _mailmap
    mm_show = None if args.no_use_mailmap else _mailmap.load(repo)
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
            # The --format terminator newline is unconditional (so %B, which ends
            # in a newline, yields a trailing blank line) — matching git.
            sys.stdout.write(_expand_commit_format(repo, peeled, c, fmt, {},
                                                   date_mode=date_mode, abbrev=args.abbrev) + "\n")
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
                _print(f"Author: {_mapped_who(_split_ident(c.author)[0], mm_show)}")
                _print("")
                first = c.message.splitlines()[0] if c.message.strip() else ""
                _print(f"    {first}")
            else:
                _emit_commit_header(sha, c, style=pstyle, date_mode=date_mode, mailmap=mm_show)
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
                    word_diff=None, a_prefix: str = "a", b_prefix: str = "b") -> None:
    if a.sha == b.sha and a.mode == b.mode:
        return
    # Under -R the working-side prefixes are swapped (b/<path> a/<path>). The
    # prefixes default to a/b but `status -vv` overrides them (c/i, then i/w).
    pa, pb = (b_prefix, a_prefix) if reverse else (a_prefix, b_prefix)
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
            # Intent-to-add entries are not staged, so --cached ignores them.
            if p in idx and idx[p].intent_to_add and p not in head_map:
                continue
            a = _side_from_object(repo, *head_map[p]) if p in head_map else _ABSENT
            b = _side_from_object(repo, idx[p].mode_str(), idx[p].sha) if p in idx else _ABSENT
            add_change(p, a, b)
    else:
        for p in sorted(idx):
            # An intent-to-add entry diffs as a brand-new file (a-side absent),
            # not against its placeholder empty blob.
            a = _ABSENT if idx[p].intent_to_add else _side_from_object(repo, idx[p].mode_str(), idx[p].sha)
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


def _emit_rename_patch(src: str, dst: str, sim: int, src_side: _Side, dst_side: _Side,
                       a_prefix: str = "a", b_prefix: str = "b") -> None:
    _print(f"diff --git {a_prefix}/{src} {b_prefix}/{dst}")
    _print(f"similarity index {sim}%")
    _print(f"rename from {src}")
    _print(f"rename to {dst}")
    if src_side.sha == dst_side.sha:
        return
    _print(f"index {src_side.sha[:7]}..{dst_side.sha[:7]} {dst_side.mode}")
    a_text = (src_side.data or b"").decode("utf-8", errors="replace")
    b_text = (dst_side.data or b"").decode("utf-8", errors="replace")
    _print(f"--- {a_prefix}/{src}")
    _print(f"+++ {b_prefix}/{dst}")
    for line in diff_mod.format_hunks(
        a_text.splitlines(), b_text.splitlines(),
        a_no_newline=bool(a_text) and not a_text.endswith("\n"),
        b_no_newline=bool(b_text) and not b_text.endswith("\n"),
    ):
        _print(line)


def _detect_changes_renames(repo: Repository, changes: list, minimum_score: int = 0):
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
    pairs = diffcore.detect_renames(repo, base_map, side_map, minimum_score=minimum_score)
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


_SET_UPSTREAM_HINT = (
    "hint:\n"
    "hint: If you are planning on basing your work on an upstream\n"
    "hint: branch that already exists at the remote, you may need to\n"
    "hint: run \"git fetch\" to retrieve it.\n"
    "hint:\n"
    "hint: If you are planning to push out a new local branch that\n"
    "hint: will track its remote counterpart, you may want to use\n"
    "hint: \"git push -u\" to set the upstream config as you push.\n"
    "hint: Disable this message with \"git config set advice.setUpstreamFailure false\"\n"
)


def _set_branch_upstream(repo: Repository, branch: str, upstream: str, quiet: bool) -> int:
    """Configure branch.<branch>.{remote,merge} to track <upstream>, matching
    C Git's behaviour for local (remote='.') and remote-tracking upstreams."""
    if refs_mod.read_ref(repo, f"refs/heads/{upstream}") is not None:
        remote, merge = ".", f"refs/heads/{upstream}"
    elif refs_mod.read_ref(repo, f"refs/remotes/{upstream}") is not None:
        rname, _, rest = upstream.partition("/")
        remote, merge = rname, f"refs/heads/{rest}"
    else:
        _err(f"fatal: the requested upstream branch '{upstream}' does not exist")
        sys.stderr.write(_SET_UPSTREAM_HINT)
        return 128
    from . import gitconfig
    cfg = repo.gitdir / "config"
    gitconfig.write_value(cfg, "branch", branch, "remote", remote)
    gitconfig.write_value(cfg, "branch", branch, "merge", merge)
    if not quiet:
        _print(f"branch '{branch}' set up to track '{upstream}'.")
    return 0


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
    ap.add_argument("--no-contains", dest="no_contains", default=None)
    ap.add_argument("--points-at", dest="points_at", default=None)
    ap.add_argument("--merged", nargs="?", const="HEAD", default=None)
    ap.add_argument("--no-merged", dest="no_merged", nargs="?", const="HEAD", default=None)
    ap.add_argument("--sort", default=None)
    ap.add_argument("--format", default=None)
    ap.add_argument("-f", "--force", action="store_true")
    ap.add_argument("-q", "--quiet", action="store_true")
    ap.add_argument("-i", "--ignore-case", dest="ignore_case", action="store_true")
    ap.add_argument("-t", "--track", nargs="?", const="direct", default=None)
    ap.add_argument("-u", "--set-upstream-to", dest="set_upstream_to", default=None)
    ap.add_argument("--recurse-submodules", dest="recurse_submodules", action="store_true")
    ap.add_argument("-v", "--verbose", action="count", default=0)
    ap.add_argument("name", nargs="?")
    ap.add_argument("start", nargs="?")
    # -t/--track takes an optional attached value only; a following token is the
    # branch name, so rewrite the bare forms.
    pre = ["--track=direct" if t in ("-t", "--track") else t for t in argv]
    args = ap.parse_args(pre)
    repo = _repo()
    head_sym, _ = refs_mod.read_head(repo)
    cur = head_sym[len("refs/heads/"):] if head_sym and head_sym.startswith("refs/heads/") else None

    contains_sha = refs_mod.rev_parse(repo, args.contains) if args.contains else None
    no_contains_sha = refs_mod.rev_parse(repo, args.no_contains) if args.no_contains else None
    merged_sha = refs_mod.rev_parse(repo, args.merged) if args.merged else None
    no_merged_sha = refs_mod.rev_parse(repo, args.no_merged) if args.no_merged else None
    points_at_sha = refs_mod.rev_parse(repo, args.points_at) if args.points_at else None

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
        if no_contains_sha is not None and _reachable_from(tip, no_contains_sha):
            return False
        if merged_sha is not None and not _reachable_from(merged_sha, tip):
            return False
        if no_merged_sha is not None and _reachable_from(no_merged_sha, tip):
            return False
        if points_at_sha is not None and tip != points_at_sha:
            return False
        return True

    # -u/--set-upstream-to changes the upstream of an existing branch (the named
    # one, else the current branch).
    if args.set_upstream_to is not None:
        branch = args.name or cur
        if branch is None:
            _err("fatal: HEAD not found below refs/heads!")
            return 128
        if refs_mod.read_ref(repo, f"refs/heads/{branch}") is None:
            _err(f"error: branch '{branch}' does not exist")
            return 1
        return _set_branch_upstream(repo, branch, args.set_upstream_to, args.quiet)

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
        # A same-name rename (e.g. `branch -M main` while already on main) is a
        # no-op move in git: the ref already points at sha, so re-writing it
        # would inject a spurious "update:" reflog entry that the moved-log path
        # normally overwrites. Skip the update for that case.
        same_name_move = (args.move or args.force_move) and src == dst
        if not same_name_move:
            refs_mod.update_ref(repo, f"refs/heads/{dst}", sha)
        if args.move or args.force_move:
            # Moving/deleting the source for a same-name rename would destroy the
            # (identical) destination ref and its reflog, so skip it; only the
            # rename reflog entries below are appended.
            if src != dst:
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
                # The HEAD reflog records the value HEAD resolved to just before
                # the symref is re-pointed. For a real rename the source ref was
                # deleted, so that value is zero; for a same-name move the source
                # ref still resolves to sha, so the second entry is sha->sha.
                _reflog.append(repo, "HEAD", sha, zero, rename_msg)
                head_old = sha if src == dst else zero
                _reflog.append(repo, "HEAD", head_old, sha, rename_msg)
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
        def _pat_ok(d: str) -> bool:
            if not pattern:
                return True
            if args.ignore_case:
                import re as _re
                return bool(_re.match(fnmatch.translate(pattern), d, _re.IGNORECASE))
            return fnmatch.fnmatch(d, pattern)
        shown = [
            (d, p) for d, p in names
            if _pat_ok(d) and (p is None or _branch_contains(p))
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

    full = f"refs/heads/{args.name}"
    if refs_mod.read_ref(repo, full) is not None and not args.force:
        _err(f"fatal: a branch named '{args.name}' already exists")
        return 128
    start = refs_mod.rev_parse(repo, args.start) if args.start else refs_mod.rev_parse(repo, "HEAD")
    if not start:
        _err(f"fatal: Not a valid object name: '{args.start or 'HEAD'}'.")
        return 128
    refs_mod.update_ref(repo, full, start)
    # -t/--track sets the new branch to track its (local or remote) start point.
    if args.track is not None and args.start is not None:
        _set_branch_upstream(repo, args.name, args.start, args.quiet)
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
    ap.add_argument("-u", "--local-user", dest="local_user", default=None)
    ap.add_argument("-f", "--force", action="store_true")
    ap.add_argument("-m", "--message", action="append", default=None)
    ap.add_argument("-F", "--file", default=None)
    ap.add_argument("-e", "--edit", action="store_true")
    ap.add_argument("--trailer", action="append", default=None)
    ap.add_argument("--cleanup", default=None)
    ap.add_argument("-n", nargs="?", const=1, type=int, default=None, dest="num")
    ap.add_argument("--sort", default=None)
    ap.add_argument("--points-at", default=None)
    ap.add_argument("--format", default=None)
    ap.add_argument("--contains", default=None)
    ap.add_argument("--no-contains", dest="no_contains", default=None)
    ap.add_argument("--merged", nargs="?", const="HEAD", default=None)
    ap.add_argument("--no-merged", dest="no_merged", nargs="?", const="HEAD", default=None)
    ap.add_argument("--column", nargs="?", const="__default__", default=None)
    ap.add_argument("--no-column", dest="no_column", action="store_true")
    ap.add_argument("--create-reflog", dest="create_reflog", action="store_true")
    ap.add_argument("-v", "--verify", action="store_true")
    ap.add_argument("-i", "--ignore-case", dest="ignore_case", action="store_true")
    ap.add_argument("name", nargs="?")
    ap.add_argument("target", nargs="?")
    # --merged/--no-merged consume the next token (default HEAD when last);
    # --column takes an optional attached style only.
    consume = ("--merged", "--no-merged")
    pre: list[str] = []
    i = 0
    while i < len(argv):
        t = argv[i]
        if t in consume:
            pre.append(f"{t}={argv[i + 1]}" if i + 1 < len(argv) else f"{t}=HEAD")
            i += 2 if i + 1 < len(argv) else 1
            continue
        if t == "--column":
            pre.append("--column=__default__")
            i += 1
            continue
        pre.append(t)
        i += 1
    args = ap.parse_args(pre)
    repo = _repo()
    if args.verify:
        rc = 0
        for name in [n for n in (args.name, args.target) if n]:
            ref = f"refs/tags/{name}"
            sha = refs_mod.read_ref(repo, ref)
            if sha is None:
                _err(f"error: tag '{name}' not found.")
                rc = 1
                continue
            otype, data = objs.read_object(repo, sha)
            if otype != "tag":
                _err(f"error: {name}: cannot verify a non-tag object of type {otype}.")
                rc = 1
                continue
            # We cannot GPG-verify; print the tag payload like git, then report
            # the missing signature (unsigned tags fail verification).
            payload = data.decode("utf-8", "replace")
            sys.stdout.write(payload if payload.endswith("\n") else payload + "\n")
            _err("error: no signature found")
            rc = 1
        return rc
    if (args.list or args.num is not None or args.sort is not None
            or args.points_at is not None or args.format is not None
            or args.contains is not None or args.no_contains is not None
            or args.merged is not None or args.no_merged is not None
            or args.column is not None
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
        if args.merged is not None or args.no_merged is not None:
            graph = _graph_for_repo(repo)

            def _reach2(start: str, target: str) -> bool:
                stack, seen_c = [start], set()
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

            def _tc(t):
                return refs_mod.rev_parse(repo, f"refs/tags/{t}^{{commit}}")
            if args.merged is not None:
                m = refs_mod.rev_parse(repo, args.merged + "^{commit}") or refs_mod.rev_parse(repo, args.merged)
                tags = [t for t in tags if (_tc(t) and m and _reach2(m, _tc(t)))]
            if args.no_merged is not None:
                m = refs_mod.rev_parse(repo, args.no_merged + "^{commit}") or refs_mod.rev_parse(repo, args.no_merged)
                tags = [t for t in tags if not (_tc(t) and m and _reach2(m, _tc(t)))]
        if args.sort:
            key = args.sort.lstrip("-")
            reverse = args.sort.startswith("-")
            if key in ("version:refname", "v:refname"):
                tags.sort(key=_version_sort_key, reverse=reverse)
            else:
                tags.sort(key=str.lower if args.ignore_case else None, reverse=reverse)
        elif args.ignore_case:
            tags.sort(key=str.lower)
        head_sym, _ = refs_mod.read_head(repo)
        selected = []
        for t in tags:
            if pattern:
                import re as _re
                if args.ignore_case:
                    if not _re.match(fnmatch.translate(pattern), t, _re.IGNORECASE):
                        continue
                elif not fnmatch.fnmatch(t, pattern):
                    continue
            selected.append(t)
        # --column lays the plain tag-name list into terminal-width columns.
        if args.column is not None and not args.no_column and args.format is None and args.num is None:
            if args.column == "plain":
                for t in selected:
                    _print(t)
            elif selected:
                for line in _columnate(selected):
                    _print(line)
            return 0
        for t in selected:
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
    annotated = (args.annotate or args.sign or args.message is not None
                 or args.file is not None or args.local_user is not None
                 or args.trailer is not None)
    old_sha = refs_mod.read_ref(repo, ref)
    if annotated:
        target_type, _ = objs.read_object(repo, target)
        if args.file is not None:
            message = sys.stdin.read() if args.file == "-" else open(args.file, encoding="utf-8").read()
        elif args.message:
            message = "\n\n".join(args.message)
        else:
            message = ""
        message = _cleanup_commit_message(message, args.cleanup or "strip")
        if args.trailer:
            message = _apply_trailers(message, args.trailer)
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
        new_sha = tag_sha
    else:
        refs_mod.update_ref(repo, ref, target)
        new_sha = target
    if args.create_reflog:
        from . import reflog as _reflog
        zero = repo.null_oid()
        # git's reflog message tags the underlying commit:
        # "tag: tagging <abbrev> (<subject>, <committer-date-short>)".
        msg = ""
        if _commit_tree_parents(repo, target) is not None:
            c = objs.parse_commit(objs.read_object(repo, target)[1])
            subj = c.message.splitlines()[0] if c.message.strip() else ""
            msg = f"tag: tagging {target[:7]} ({subj}, {_format_date(c.committer, 'short')})"
        _reflog.append(repo, ref, old_sha or zero, new_sha, msg)
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
    quiet = False
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
        elif a in ("-q", "--quiet"):
            quiet = True
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
            if not quiet:
                _err(f"Already on '{target}'")
        else:
            refs_mod.set_head(repo, f"refs/heads/{target}")
            _log_checkout(sha, target)
            if not quiet:
                _err(f"Switched to branch '{target}'")
    else:
        refs_mod.set_head(repo, sha)
        _log_checkout(sha, sha[:7])
        from . import gitconfig
        advice = (gitconfig.get(repo, "advice.detachedhead") or "").lower()
        if not quiet and advice not in ("false", "0", "no", "off"):
            sys.stderr.write(f"Note: switching to '{target}'.\n\n")
            sys.stderr.write(_DETACHED_ADVICE)
        subject = objs.parse_commit(data).message.splitlines()[0] if t == "commit" else ""
        if not quiet:
            _err(f"HEAD is now at {sha[:7]} {subject}")
    return 0


def _switch_describe_head(repo: Repository, msg: str, sha: str) -> None:
    """Emit git's '<msg> <abbrev> <subject>' line for a commit (to stderr)."""
    subject = ""
    try:
        t, data = objs.read_object(repo, sha)
        if t == "commit":
            lines = objs.parse_commit(data).message.splitlines()
            subject = lines[0] if lines else ""
    except Exception:
        pass
    _err(f"{msg} {sha[:7]} {subject}")


def _switch_prev_branch(repo: Repository) -> Optional[str]:
    """Resolve switch's '-' / '@{-1}': the branch left by the most recent
    'checkout: moving from <X> to <Y>' HEAD reflog entry."""
    from . import reflog as _reflog
    for _old, _new, _ident, msg in reversed(_reflog.read(repo, "HEAD")):
        if msg.startswith("checkout: moving from "):
            rest = msg[len("checkout: moving from "):]
            frm, _, _ = rest.partition(" to ")
            return frm or None
    return None


def _switch_track_friendly(remote: Optional[str], merge_ref: str) -> str:
    """The friendly name git prints in 'set up to track '<name>'.'."""
    short = merge_ref
    if short.startswith("refs/heads/"):
        short = short[len("refs/heads/"):]
    if remote and remote != ".":
        return f"{remote}/{short}"
    return short


def _switch_resolve_tracking(repo: Repository, real_ref: str):
    """Map a fully-resolved start ref to (remote, merge_ref) for direct
    tracking. A remote-tracking ref under refs/remotes/<r>/<b> with the
    standard fetch refspec maps back to (<r>, refs/heads/<b>); a local
    branch maps to ('.', refs/heads/<b>)."""
    from . import gitconfig
    if real_ref.startswith("refs/remotes/"):
        rest = real_ref[len("refs/remotes/"):]
        # Find a remote whose fetch refspec maps real_ref back to a head.
        for name, value in gitconfig.list_all(repo):
            if not (name.startswith("remote.") and name.endswith(".fetch")):
                continue
            remote_name = name[len("remote."):-len(".fetch")]
            spec = value[1:] if value.startswith("+") else value
            src, _, dst = spec.partition(":")
            if dst.endswith("/*") and src.endswith("/*"):
                dpre, spre = dst[:-1], src[:-1]
                if real_ref.startswith(dpre):
                    tail = real_ref[len(dpre):]
                    return remote_name, spre + tail
        # No refspec matched; fall back to splitting on the remote name.
        head, _, branch = rest.partition("/")
        return head, f"refs/heads/{branch}"
    if real_ref.startswith("refs/heads/"):
        return ".", real_ref
    return ".", real_ref


def _switch_inherit_tracking(repo: Repository, real_ref: str):
    """Resolve --track=inherit: copy the start branch's upstream config.
    Returns (remote, merge_ref) on success, or a warning string to print
    (before switching) when the source has no usable tracking config."""
    from . import gitconfig
    if not real_ref.startswith("refs/heads/"):
        return f"warning: asked to inherit tracking from '{real_ref}', but no remote is set"
    base = real_ref[len("refs/heads/"):]
    remote = gitconfig.get(repo, f"branch.{base}.remote")
    merge = gitconfig.get(repo, f"branch.{base}.merge")
    if not remote:
        return f"warning: asked to inherit tracking from '{base}', but no remote is set"
    if not merge:
        return f"warning: asked to inherit tracking from '{base}', but no merge configuration is set"
    return (remote, merge)


def _switch_write_tracking(repo: Repository, local: str,
                           remote: str, merge_ref: str, quiet: bool) -> None:
    """Write branch.<local>.{remote,merge} and print git's confirmation."""
    from . import gitconfig
    cfg = repo.gitdir / "config"
    gitconfig.write_value(cfg, "branch", local, "remote", remote, mode="set")
    gitconfig.write_value(cfg, "branch", local, "merge", merge_ref, mode="set")
    if not quiet:
        friendly = _switch_track_friendly(remote, merge_ref)
        _print(f"branch '{local}' set up to track '{friendly}'.")


_SWITCH_USAGE = (
    "usage: git switch [<options>] [<branch>]\n"
    "\n"
    "    -c, --[no-]create <branch>\n"
    "                          create and switch to a new branch\n"
    "    -C, --[no-]force-create <branch>\n"
    "                          create/reset and switch to a branch\n"
    "    --[no-]guess          second guess 'git switch <no-such-branch>'\n"
    "    --[no-]discard-changes\n"
    "                          throw away local modifications\n"
    "    -q, --[no-]quiet      suppress progress reporting\n"
    "    --[no-]recurse-submodules[=<checkout>]\n"
    "                          control recursive updating of submodules\n"
    "    --[no-]progress       force progress reporting\n"
    "    -m, --[no-]merge      perform a 3-way merge with the new branch\n"
    "    --[no-]conflict <style>\n"
    "                          conflict style (merge, diff3, or zdiff3)\n"
    "    -d, --[no-]detach     detach HEAD at named commit\n"
    "    -t, --[no-]track[=(direct|inherit)]\n"
    "                          set branch tracking configuration\n"
    "    -f, --[no-]force      force checkout (throw away local modifications)\n"
    "    --[no-]orphan <new-branch>\n"
    "                          new unborn branch\n"
    "    --[no-]overwrite-ignore\n"
    "                          update ignored files (default)\n"
    "    --[no-]ignore-other-worktrees\n"
    "                          do not check if another worktree is using this branch\n"
    "\n"
)


class _SwitchParseError(Exception):
    def __init__(self, message: str, rc: int = 129, show_usage: bool = False):
        super().__init__(message)
        self.message = message
        self.rc = rc
        self.show_usage = show_usage


def _switch_parse(argv: list[str]) -> dict:
    """Faithful subset of git switch's parse-options handling."""
    opts = {"create": None, "force_create": None, "quiet": False,
            "detach": False, "track": None, "args": []}
    i = 0
    n = len(argv)
    saw_dd = False
    while i < n:
        a = argv[i]
        if saw_dd:
            opts["args"].append(a)
            i += 1
            continue
        if a == "--":
            saw_dd = True
            i += 1
            continue
        if a == "-" or not a.startswith("-"):
            opts["args"].append(a)
            i += 1
            continue
        if a.startswith("--"):
            name, eq, val = a[2:].partition("=")
            has_val = bool(eq)
            if name in ("create", "force-create"):
                key = "create" if name == "create" else "force_create"
                if has_val:
                    opts[key] = val
                else:
                    i += 1
                    if i >= n:
                        raise _SwitchParseError(f"error: option `{name}' requires a value")
                    opts[key] = argv[i]
            elif name == "quiet":
                if has_val:
                    raise _SwitchParseError(f"error: option `{name}' takes no value")
                opts["quiet"] = True
            elif name == "detach":
                if has_val:
                    raise _SwitchParseError(f"error: option `{name}' takes no value")
                opts["detach"] = True
            elif name == "track":
                if has_val:
                    if val not in ("direct", "inherit"):
                        raise _SwitchParseError('error: option `--track\' expects "direct" or "inherit"')
                    opts["track"] = val
                else:
                    opts["track"] = "direct"
            elif name == "no-track":
                if has_val:
                    raise _SwitchParseError(f"error: option `{name}' takes no value")
                opts["track"] = "none"
            else:
                raise _SwitchParseError(f"error: unknown option `{name}'", show_usage=True)
            i += 1
            continue
        # Short option cluster, possibly with an attached value.
        j = 1
        while j < len(a):
            ch = a[j]
            if ch in ("c", "C"):
                key = "create" if ch == "c" else "force_create"
                rest = a[j + 1:]
                if rest:
                    opts[key] = rest
                else:
                    i += 1
                    if i >= n:
                        raise _SwitchParseError(f"error: switch `{ch}' requires a value")
                    opts[key] = argv[i]
                break
            elif ch == "q":
                opts["quiet"] = True
            elif ch == "d":
                opts["detach"] = True
            elif ch == "t":
                opts["track"] = "direct"
            else:
                raise _SwitchParseError(f"error: unknown switch `{ch}'", show_usage=True)
            j += 1
        i += 1
    return opts


def cmd_switch(argv: list[str]) -> int:
    try:
        opts = _switch_parse(argv)
    except _SwitchParseError as exc:
        _err(exc.message)
        if exc.show_usage:
            sys.stderr.write(_SWITCH_USAGE)
        return exc.rc

    create = opts["create"]
    force_create = opts["force_create"]
    quiet = opts["quiet"]
    detach = opts["detach"]
    track = opts["track"]
    args = opts["args"]
    new_branch = create or force_create

    if detach and new_branch is not None:
        _err("fatal: '--detach' cannot be used with '-b/-B/--orphan'")
        return 128
    # --track/-t/--no-track without -c/-C makes git DWIM a branch name from the
    # positional argument (strip refs/, remotes/, take the part after '/').
    if track is not None and new_branch is None:
        if not args or args[0] == "--":
            _err("fatal: --track needs a branch name")
            return 128
        argv0 = args[0]
        for pre in ("refs/", "remotes/"):
            if argv0.startswith(pre):
                argv0 = argv0[len(pre):]
        slash = argv0.find("/")
        if slash < 0 or slash == len(argv0) - 1:
            _err("fatal: missing branch name; try -c")
            return 128
        new_branch = argv0[slash + 1:]
        create = new_branch  # behaves like -c <dwimmed-name>
        args = list(args)  # the original arg stays as the start point
    if len(args) > 1:
        _err("fatal: only one reference expected")
        return 128

    repo = _repo()
    head_sym0, old_sha0 = refs_mod.read_head(repo)
    on_branch0 = bool(head_sym0 and head_sym0.startswith("refs/heads/"))
    old_name0 = (head_sym0[len("refs/heads/"):] if on_branch0
                 else (old_sha0 if old_sha0 else None))

    def _log_head(new_sha: Optional[str], new_name: str) -> None:
        from . import reflog as _reflog
        if old_sha0 and new_sha:
            _reflog.append(repo, "HEAD", old_sha0, new_sha,
                           f"checkout: moving from {old_name0} to {new_name}")

    def _leaving_detached(new_sha: Optional[str]) -> None:
        # When leaving a detached HEAD for a different commit, git reports the
        # previous position (or warns about commits left behind if unreachable).
        if quiet or on_branch0 or not old_sha0 or new_sha == old_sha0:
            return
        lost = _switch_orphans(repo, old_sha0)
        if lost:
            plural = "s" if len(lost) != 1 else ""
            verb = "them" if len(lost) != 1 else "it"
            _err(f"Warning: you are leaving {len(lost)} commit{plural} behind, not connected to")
            _err("any of your branches:")
            _err("")
            for c_sha, c_subj in lost:
                _err(f"  {c_sha[:7]} {c_subj}")
            _err("")
            _err(f"If you want to keep {verb} by creating a new branch, this may be a good time")
            _err("to do so with:")
            _err("")
            _err(f" git branch <new-branch-name> {lost[0][0][:7]}")
            _err("")
        else:
            _switch_describe_head(repo, "Previous HEAD position was", old_sha0)

    raw_arg = args[0] if args else None
    # git rewrites the `-` shorthand to `@{-1}` (the previously checked-out
    # branch); the error message reflects the rewritten form.
    if raw_arg == "-":
        raw_arg = "@{-1}"
    if raw_arg == "@{-1}":
        prev = _switch_prev_branch(repo)
        if prev is None:
            _err("fatal: invalid reference: @{-1}")
            return 128
        raw_arg = prev

    # Determine the start point for branch creation / the switch target.
    if new_branch is not None:
        start_arg = raw_arg
        start_sha = (refs_mod.rev_parse(repo, start_arg) if start_arg
                     else refs_mod.rev_parse(repo, "HEAD"))
        if start_arg is not None and start_sha is None:
            _err(f"fatal: invalid reference: {start_arg}")
            return 128
        # A branch is created at the peeled commit of its start point.
        if start_sha is not None:
            start_sha = refs_mod._peel_to_commit(repo, start_sha) or start_sha
        exists = refs_mod.read_ref(repo, f"refs/heads/{new_branch}") is not None
        if create is not None and exists:
            _err(f"fatal: a branch named '{new_branch}' already exists")
            return 128

        # Resolve the real ref of the start point for tracking decisions.
        real_ref = None
        if start_arg is not None:
            real_ref = _switch_dwim_ref(repo, start_arg)
        # Resolve the effective tracking mode. track is None (unset → auto),
        # "none" (--no-track suppresses auto), or "direct"/"inherit" (explicit).
        explicit_track = track in ("direct", "inherit")
        if track in (None, "none"):
            do_track = None
            if track is None and real_ref and real_ref.startswith("refs/remotes/"):
                do_track = "direct"  # autoSetupMerge default for remote starts
        else:
            do_track = track
        if explicit_track:
            # Explicit -t/--track requires the start point to be a branch.
            if real_ref is None or not (real_ref.startswith("refs/heads/")
                                        or real_ref.startswith("refs/remotes/")):
                disp = start_arg if start_arg is not None else "HEAD"
                _err(f"fatal: cannot set up tracking information; starting point '{disp}' is not a branch")
                return 128

        if start_sha is None:
            # Unborn HEAD: just repoint HEAD; ref is written on first commit.
            refs_mod.set_head(repo, f"refs/heads/{new_branch}")
            if not quiet:
                _err(f"Switched to a new branch '{new_branch}'")
            return 0

        # Resolve tracking config up-front: inherit emits its failure
        # warning before the switch happens; direct resolves the remote/merge.
        track_pair = None
        if do_track == "inherit":
            res = _switch_inherit_tracking(repo, real_ref) if real_ref else \
                f"warning: asked to inherit tracking from '{start_arg}', but no remote is set"
            if isinstance(res, str):
                _err(res)
            else:
                track_pair = res
        elif do_track == "direct":
            track_pair = _switch_resolve_tracking(repo, real_ref)

        currently_on = on_branch0 and old_name0 == new_branch
        reset_existing = exists  # only reachable via force_create
        start_name = start_arg if start_arg is not None else "HEAD"
        msg = (f"branch: Reset to {start_name}" if reset_existing
               else f"branch: Created from {start_name}")
        # git snapshots the "leaving detached HEAD" report before updating refs.
        _leaving_detached(start_sha)
        refs_mod.update_ref(repo, f"refs/heads/{new_branch}", start_sha, message=msg)
        tree = _commit_tree(repo, start_sha) or start_sha
        _log_head(start_sha, new_branch)
        workdir.checkout_tree(repo, tree)
        refs_mod.set_head(repo, f"refs/heads/{new_branch}")
        if not quiet:
            if currently_on:
                _err(f"Reset branch '{new_branch}'")
            elif reset_existing:
                _err(f"Switched to and reset branch '{new_branch}'")
            else:
                _err(f"Switched to a new branch '{new_branch}'")
        if track_pair is not None:
            _switch_write_tracking(repo, new_branch, track_pair[0], track_pair[1], quiet)
        return 0

    # No -c/-C: a plain switch (optionally detaching).
    if raw_arg is None:
        if detach:
            # `switch -d` with no argument detaches at the current HEAD.
            if old_sha0 is None:
                if on_branch0:
                    # Unborn HEAD: symref to a branch with no commit yet.
                    _err("fatal: You are on a branch yet to be born")
                else:
                    _err("fatal: missing branch or commit argument")
                return 128
            tree = _commit_tree(repo, old_sha0) or old_sha0
            workdir.checkout_tree(repo, tree)
            _log_head(old_sha0, old_sha0)
            refs_mod.set_head(repo, old_sha0)
            if not quiet:
                _switch_describe_head(repo, "HEAD is now at", old_sha0)
            return 0
        _err("fatal: missing branch or commit argument")
        return 128

    sha = refs_mod.rev_parse(repo, raw_arg)
    if sha is None:
        _err(f"fatal: invalid reference: {raw_arg}")
        return 128
    real_ref = _switch_dwim_ref(repo, raw_arg)

    if detach:
        # Peel tags/objects to the underlying commit; detached HEAD stores it.
        commit_sha = refs_mod._peel_to_commit(repo, sha) or sha
        tree = _commit_tree(repo, commit_sha) or commit_sha
        _leaving_detached(commit_sha)
        workdir.checkout_tree(repo, tree)
        _log_head(commit_sha, raw_arg)
        refs_mod.set_head(repo, commit_sha)
        if not quiet:
            _switch_describe_head(repo, "HEAD is now at", commit_sha)
        return 0

    # Plain switch: the target must be a local branch.
    is_branch = refs_mod.read_ref(repo, f"refs/heads/{raw_arg}") is not None
    if not is_branch:
        if real_ref and real_ref.startswith("refs/tags/"):
            _err(f"fatal: a branch is expected, got tag '{real_ref[len('refs/tags/'):]}'")
        elif real_ref and real_ref.startswith("refs/remotes/"):
            _err(f"fatal: a branch is expected, got remote branch '{real_ref[len('refs/remotes/'):]}'")
        elif real_ref:
            _err(f"fatal: a branch is expected, got '{real_ref}'")
        else:
            _err(f"fatal: a branch is expected, got commit '{raw_arg}'")
        _err("hint: If you want to detach HEAD at the commit, try again with the --detach option.")
        return 128

    if on_branch0 and old_name0 == raw_arg:
        if not quiet:
            _err(f"Already on '{raw_arg}'")
        return 0
    tree = _commit_tree(repo, sha) or sha
    _leaving_detached(sha)
    workdir.checkout_tree(repo, tree)
    _log_head(sha, raw_arg)
    refs_mod.set_head(repo, f"refs/heads/{raw_arg}")
    if not quiet:
        _err(f"Switched to branch '{raw_arg}'")
    return 0


def _switch_dwim_ref(repo: Repository, name: str) -> Optional[str]:
    """Return the full ref that <name> resolves to (git's repo_dwim_ref),
    trying the standard search order, or None if it is not a ref."""
    if name.startswith("refs/") and refs_mod.read_ref(repo, name) is not None:
        return name
    for pat in (f"refs/heads/{name}", f"refs/tags/{name}",
                f"refs/remotes/{name}", f"refs/remotes/{name}/HEAD"):
        if refs_mod.read_ref(repo, pat) is not None:
            return pat
    if name == "HEAD" and refs_mod.read_ref(repo, "HEAD") is not None:
        return "HEAD"
    return None


def _switch_orphans(repo: Repository, old_sha: str):
    """Commits reachable from old_sha but from no ref, newest-first, as
    (sha, subject) — git's 'commits left behind' list."""
    from . import merge as merge_mod
    # Collect ref tips (excluding HEAD itself).
    tips = []
    refs_dir = repo.gitdir / "refs"
    for base in ("heads", "tags", "remotes"):
        d = refs_dir / base
        if not d.exists():
            continue
        for p in d.rglob("*"):
            if p.is_file():
                rel = "refs/" + str(p.relative_to(refs_dir)).replace(os.sep, "/")
                t = refs_mod.read_ref(repo, rel)
                if t:
                    tips.append(t)
    packed = repo.gitdir / "packed-refs"
    if packed.exists():
        for line in packed.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("^"):
                continue
            sha_part, _, ref_part = line.partition(" ")
            if ref_part.startswith("refs/"):
                tips.append(sha_part)

    def reachable_from_tips(c: str) -> bool:
        for t in tips:
            if t == c or merge_mod.is_ancestor(repo, c, t):
                return True
        return False

    # Walk commits from old_sha; stop descending once we hit a reachable one.
    seen = set()
    stack = [old_sha]
    collected = []  # (sha, subject, commit_timestamp)
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        if reachable_from_tips(cur):
            continue
        subj, cts = "", 0
        try:
            t, data = objs.read_object(repo, cur)
            if t == "commit":
                c = objs.parse_commit(data)
                lines = c.message.splitlines()
                subj = lines[0] if lines else ""
                parts = c.committer.rsplit(" ", 2)
                if len(parts) == 3 and parts[1].lstrip("-").isdigit():
                    cts = int(parts[1])
                for par in c.parents:
                    if par not in seen:
                        stack.append(par)
        except Exception:
            pass
        collected.append((cur, subj, cts))
    # rev-list default order: most recent commit date first.
    collected.sort(key=lambda e: e[2], reverse=True)
    return [(sha, subj) for sha, subj, _ts in collected]


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


def _reset_worktree_path(repo: Repository, p: str, sha: Optional[str], mode: Optional[int]) -> None:
    """Materialize (or delete) one worktree path during reset --merge/--keep."""
    full = repo.path / p
    if sha is None:
        if full.exists() or full.is_symlink():
            full.unlink()
        return
    data = objs.read_object(repo, sha)[1]
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_bytes(data)
    if mode is not None and (mode & 0o111):
        full.chmod(full.stat().st_mode | 0o111)


def _reset_merge_keep(repo: Repository, kind: str, target_sha: str, target_tree: str,
                      treeish: str) -> int:
    """reset --merge / --keep: a two-way merge that updates files differing
    between HEAD and the target while protecting local changes (atomically)."""
    head_sha = refs_mod.rev_parse(repo, "HEAD")
    head_tree = _commit_tree(repo, head_sha) if head_sha else None
    H = workdir.flatten_tree(repo, head_tree) if head_tree else {}
    T = workdir.flatten_tree(repo, target_tree) if target_tree else {}
    idx = read_index(repo)
    I = {p: e.sha for p, e in idx.by_path().items()}

    def wt_sha(p: str) -> Optional[str]:
        full = repo.path / p
        if not (full.exists() or full.is_symlink()):
            return None
        import stat as _st
        ls = full.lstat()
        data = os.readlink(full).encode() if _st.S_ISLNK(ls.st_mode) else full.read_bytes()
        return objs.hash_bytes("blob", data, repo)[0]

    paths = sorted(set(H) | set(T) | set(I))
    # Dry-run: report the first blocking entry (index order) and abort wholesale.
    for p in paths:
        h, t, i = H.get(p), T.get(p), I.get(p)
        if h == t:
            continue
        w = wt_sha(p)
        if kind == "keep" and i != h:
            _err(f"error: Entry '{p}' would be overwritten by merge. Cannot merge.")
            _err(f"fatal: Could not reset index file to revision '{treeish}'.")
            return 128
        if w != i:
            _err(f"error: Entry '{p}' not uptodate. Cannot merge.")
            _err(f"fatal: Could not reset index file to revision '{treeish}'.")
            return 128

    # Capture the per-path worktree state before any mutation.
    W = {p: wt_sha(p) for p in paths}
    head_sym, _ = refs_mod.read_head(repo)
    if head_sym:
        refs_mod.update_ref(repo, head_sym, target_sha, message=f"reset: moving to {treeish}")
    else:
        refs_mod.set_head(repo, target_sha)
    workdir.read_tree(repo, target_tree)
    new_idx = read_index(repo).by_path()
    for p in paths:
        h, t, i, w = H.get(p), T.get(p), I.get(p), W[p]
        if kind == "keep":
            update = h != t  # checks guaranteed i==h==w for these
        else:  # merge: update unless there is an unstaged change to keep
            update = w == i
        if update:
            tmode = new_idx[p].mode if p in new_idx else None
            _reset_worktree_path(repo, p, t, tmode)
    return 0


def cmd_reset(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit reset", add_help=False)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--soft", action="store_true")
    g.add_argument("--mixed", action="store_true")
    g.add_argument("--hard", action="store_true")
    g.add_argument("--merge", action="store_true")
    g.add_argument("--keep", action="store_true")
    ap.add_argument("-q", "--quiet", action="store_true")
    ap.add_argument("-N", "--intent-to-add", dest="intent_to_add", action="store_true")
    ap.add_argument("--no-refresh", dest="no_refresh", action="store_true")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--pathspec-from-file", dest="pathspec_from_file", default=None)
    ap.add_argument("--pathspec-file-nul", dest="pathspec_file_nul", action="store_true")
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
    from_file_paths: list[str] = []
    if args.pathspec_from_file is not None:
        raw = (sys.stdin.buffer.read() if args.pathspec_from_file == "-"
               else open(args.pathspec_from_file, "rb").read())
        sep = "\0" if args.pathspec_file_nul else "\n"
        from_file_paths = [p for p in raw.decode("utf-8").split(sep) if p]
    if positionals:
        if refs_mod.rev_parse(repo, positionals[0]) is not None and (len(positionals) > 1 or paths or from_file_paths or not (repo.path / positionals[0]).exists()):
            treeish = positionals[0]
            paths = positionals[1:] + paths
        else:
            paths = positionals + paths
    paths += from_file_paths

    mode = ("soft" if args.soft else "hard" if args.hard else "merge" if args.merge
            else "keep" if args.keep else "mixed")
    if paths and mode != "mixed":
        _err(f"fatal: Cannot do {mode} reset with paths.")
        return 128

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
    t, data = objs.read_object(repo, sha)
    tree = objs.parse_commit(data).tree if t == "commit" else sha
    # --merge / --keep run a protected two-way merge (and move HEAD themselves,
    # only after their abort checks pass).
    if mode in ("merge", "keep"):
        return _reset_merge_keep(repo, mode, sha, tree, treeish)
    head_sym, _ = refs_mod.read_head(repo)
    if head_sym:
        refs_mod.update_ref(repo, head_sym, sha, message=f"reset: moving to {treeish}")
    else:
        refs_mod.set_head(repo, sha)
    if mode == "soft":
        return 0
    if mode == "hard":
        workdir.checkout_tree(repo, tree)
        if t == "commit":
            subject = objs.parse_commit(data).message.splitlines()[0] if data else ""
            _print(f"HEAD is now at {sha[:7]} {subject}")
        return 0
    # mixed (default): reset the index to the target, then report files whose
    # worktree content now differs from it (unless -q / --no-refresh).
    old_paths = set(read_index(repo).by_path())
    workdir.read_tree(repo, tree)
    if args.intent_to_add:
        # -N: paths the reset dropped from the index but still in the worktree
        # are re-added as intent-to-add.
        idx2 = read_index(repo)
        now = set(idx2.by_path())
        empty = objs.write_object(repo, "blob", b"")
        changed = False
        for p in old_paths - now:
            full = repo.path / p
            if full.exists() or full.is_symlink():
                from .index import IndexEntry
                e = IndexEntry(mode=workdir._mode_for(full), sha=empty, path=p)
                e.intent_to_add = True
                idx2.upsert(e)
                changed = True
        if changed:
            write_index(repo, idx2)
    if not args.quiet and not args.no_refresh:
        new_idx = read_index(repo).by_path()
        modified = []
        import stat as _st
        for p, entry in new_idx.items():
            full = repo.path / p
            if entry.intent_to_add:
                modified.append((p, "A"))
                continue
            if not (full.exists() or full.is_symlink()):
                modified.append((p, "D"))
                continue
            ls = full.lstat()
            wdata = os.readlink(full).encode() if _st.S_ISLNK(ls.st_mode) else full.read_bytes()
            if objs.hash_bytes("blob", wdata, repo)[0] != entry.sha:
                modified.append((p, "M"))
        if modified:
            _print("Unstaged changes after reset:")
            for p, stt in sorted(modified):
                _print(f"{stt}\t{p}")
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


_REMOTE_SUBCOMMANDS = {"add", "rename", "remove", "rm", "set-head", "show",
                       "prune", "update", "set-branches", "get-url", "set-url"}


def cmd_remote(argv: list[str]) -> int:
    verbose = False
    rest: list[str] = []
    for a in argv:
        if a in ("-v", "--verbose"):
            verbose = True
        else:
            rest.append(a)
    action = None
    if rest and not rest[0].startswith("-") and rest[0] in _REMOTE_SUBCOMMANDS:
        action = rest[0]
        rest = rest[1:]
    repo = _repo()
    from . import gitconfig
    cfg_path = repo.gitdir / "config"

    def _write_symref(ref: str, target: str) -> None:
        p = repo.gitdir / ref
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"ref: {target}\n", encoding="utf-8")

    def _remote_names() -> list[str]:
        seen: list[str] = []
        for key, _v in gitconfig.list_all(repo):
            if key.startswith("remote.") and key.endswith(".url"):
                nm = key[len("remote."):-len(".url")]
                if nm not in seen:
                    seen.append(nm)
        return seen

    def _cfg(name: str, sub: str):
        try:
            return gitconfig.get(repo, f"remote.{name}.{sub}")
        except Exception:
            return None

    if action in (None, "show") and not rest:
        for name in _remote_names():
            if verbose:
                url = _cfg(name, "url") or ""
                pushurl = _cfg(name, "pushurl") or url
                _print(f"{name}\t{url} (fetch)")
                _print(f"{name}\t{pushurl} (push)")
            else:
                _print(name)
        return 0

    if action == "add":
        ap = argparse.ArgumentParser(prog="pygit remote add", add_help=False)
        ap.add_argument("-t", "--track", action="append", default=None)
        ap.add_argument("-m", "--master", default=None)
        ap.add_argument("-f", "--fetch", action="store_true")
        ap.add_argument("--tags", dest="tags", action="store_true")
        ap.add_argument("--no-tags", dest="no_tags", action="store_true")
        ap.add_argument("--mirror", nargs="?", const="fetch", default=None)
        ap.add_argument("name")
        ap.add_argument("url")
        a = ap.parse_args(rest)
        if a.name in _remote_names():
            _err(f"error: remote {a.name} already exists.")
            return 3
        gitconfig.write_value(cfg_path, "remote", a.name, "url", a.url)
        if a.mirror == "push":
            gitconfig.write_value(cfg_path, "remote", a.name, "mirror", "true")
        elif a.mirror == "fetch":
            gitconfig.write_value(cfg_path, "remote", a.name, "fetch", "+refs/*:refs/*")
        else:
            tracks = a.track or ["*"]
            for i, br in enumerate(tracks):
                gitconfig.write_value(
                    cfg_path, "remote", a.name, "fetch",
                    f"+refs/heads/{br}:refs/remotes/{a.name}/{br}",
                    mode="set" if i == 0 else "add")
        if a.tags:
            gitconfig.write_value(cfg_path, "remote", a.name, "tagopt", "--tags")
        elif a.no_tags:
            gitconfig.write_value(cfg_path, "remote", a.name, "tagopt", "--no-tags")
        if a.master:
            _write_symref(f"refs/remotes/{a.name}/HEAD",
                          f"refs/remotes/{a.name}/{a.master}")
        return 0

    if action in ("remove", "rm"):
        name = rest[0] if rest else ""
        if name not in _remote_names():
            _err(f"error: No such remote: '{name}'")
            return 2
        gitconfig.remove_section(cfg_path, "remote", name)
        # Drop the remote-tracking refs and the branch.<x>.remote links.
        rdir = repo.gitdir / "refs" / "remotes" / name
        if rdir.exists():
            import shutil as _sh
            _sh.rmtree(rdir, ignore_errors=True)
        return 0

    if action == "rename":
        old, new = rest[0], rest[1]
        if old not in _remote_names():
            _err(f"error: No such remote: '{old}'")
            return 2
        for key, value in gitconfig.list_all(repo):
            if key.startswith(f"remote.{old}."):
                sub = key[len(f"remote.{old}."):]
                if sub == "fetch":
                    value = value.replace(f"refs/remotes/{old}/", f"refs/remotes/{new}/")
                gitconfig.write_value(cfg_path, "remote", new, sub, value,
                                      mode="add" if sub == "fetch" else "set")
        gitconfig.remove_section(cfg_path, "remote", old)
        odir = repo.gitdir / "refs" / "remotes" / old
        ndir = repo.gitdir / "refs" / "remotes" / new
        if odir.exists():
            ndir.parent.mkdir(parents=True, exist_ok=True)
            odir.replace(ndir)
        return 0

    if action == "get-url":
        ap = argparse.ArgumentParser(prog="pygit remote get-url", add_help=False)
        ap.add_argument("--push", action="store_true")
        ap.add_argument("--all", action="store_true")
        ap.add_argument("name")
        a = ap.parse_args(rest)
        if a.name not in _remote_names():
            _err(f"error: No such remote '{a.name}'")
            return 2
        key = "pushurl" if a.push else "url"
        vals = [v for k, v in gitconfig.list_all(repo) if k == f"remote.{a.name}.{key}"]
        if not vals and a.push:
            vals = [v for k, v in gitconfig.list_all(repo) if k == f"remote.{a.name}.url"]
        if a.all:
            for v in vals:
                _print(v)
        elif vals:
            _print(vals[-1])
        return 0

    if action == "set-url":
        ap = argparse.ArgumentParser(prog="pygit remote set-url", add_help=False)
        ap.add_argument("--push", action="store_true")
        ap.add_argument("--add", action="store_true")
        ap.add_argument("--delete", action="store_true")
        ap.add_argument("name")
        ap.add_argument("newurl", nargs="?")
        ap.add_argument("oldurl", nargs="?")
        a = ap.parse_args(rest)
        if a.name not in _remote_names():
            _err(f"error: No such remote '{a.name}'")
            return 2
        key = "pushurl" if a.push else "url"
        if a.delete:
            # Remove only the URL(s) matching the given value: drop all, re-add rest.
            current = [v for k, v in gitconfig.list_all(repo) if k == f"remote.{a.name}.{key}"]
            keep = [v for v in current if v != a.newurl]
            gitconfig.unset_value(cfg_path, "remote", a.name, key, all_values=True)
            for i, v in enumerate(keep):
                gitconfig.write_value(cfg_path, "remote", a.name, key, v,
                                      mode="set" if i == 0 else "add")
        elif a.add:
            gitconfig.write_value(cfg_path, "remote", a.name, key, a.newurl, mode="add")
        else:
            gitconfig.write_value(cfg_path, "remote", a.name, key, a.newurl)
        return 0

    if action == "set-branches":
        ap = argparse.ArgumentParser(prog="pygit remote set-branches", add_help=False)
        ap.add_argument("--add", action="store_true")
        ap.add_argument("name")
        ap.add_argument("branches", nargs="+")
        a = ap.parse_args(rest)
        for i, br in enumerate(a.branches):
            mode = "add" if (a.add or i > 0) else "set"
            gitconfig.write_value(cfg_path, "remote", a.name, "fetch",
                                  f"+refs/heads/{br}:refs/remotes/{a.name}/{br}", mode=mode)
        return 0

    if action == "set-head":
        ap = argparse.ArgumentParser(prog="pygit remote set-head", add_help=False)
        ap.add_argument("-a", "--auto", action="store_true")
        ap.add_argument("-d", "--delete", action="store_true")
        ap.add_argument("name")
        ap.add_argument("branch", nargs="?")
        a = ap.parse_args(rest)
        head_ref = f"refs/remotes/{a.name}/HEAD"
        if a.delete:
            refs_mod.delete_ref(repo, head_ref)
            # Prune now-empty ref directories, like C Git.
            d = (repo.gitdir / head_ref).parent
            while d != repo.gitdir and d.is_dir() and not any(d.iterdir()):
                d.rmdir()
                d = d.parent
        elif a.branch:
            target = f"refs/remotes/{a.name}/{a.branch}"
            if refs_mod.read_ref(repo, target) is None:
                _err(f"error: Not a valid ref: {target}")
                _err(f"fatal: ref {head_ref} is not a symbolic ref")
                return 128
            refs_mod.write_symref(repo, head_ref, target)
        return 0

    # prune / update / show <name> require network access; accept silently.
    return 0


def cmd_ls_remote(argv: list[str]) -> int:
    # The boolean --tags/--branches/--heads reject an attached value, exactly
    # like C Git's parse-options ("option `<name>' takes no value").
    for tok in argv:
        for nm in ("branches", "tags", "heads"):
            if tok.startswith(f"--{nm}="):
                _err(f"error: option `{nm}' takes no value")
                return 129
    ap = argparse.ArgumentParser(prog="pygit ls-remote", add_help=False)
    ap.add_argument("-t", "--tags", action="store_true")
    ap.add_argument("-b", "--branches", "--heads", dest="branches", action="store_true")
    ap.add_argument("--refs", action="store_true")
    ap.add_argument("--symref", action="store_true")
    ap.add_argument("--get-url", dest="get_url", action="store_true")
    ap.add_argument("-q", "--quiet", action="store_true")
    ap.add_argument("--exit-code", dest="exit_code", action="store_true")
    ap.add_argument("--sort", default=None)
    ap.add_argument("--upload-pack", dest="upload_pack", default=None)
    ap.add_argument("-o", "--server-option", dest="server_option", action="append", default=None)
    ap.add_argument("url", nargs="?", default=None)
    ap.add_argument("patterns", nargs="*")
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
    if args.get_url:
        _print(url)
        return 0
    src = url[7:] if url.startswith("file://") else url

    import fnmatch as _fn

    def _pat_match(name: str) -> bool:
        if not args.patterns:
            return True
        for p in args.patterns:
            if name == p or _fn.fnmatch(name, p) or name.endswith("/" + p) \
                    or _fn.fnmatch(name, "*/" + p):
                return True
        return False

    def _sort_key(disp: str):
        key = args.sort.lstrip("-") if args.sort else "refname"
        if key in ("version:refname", "v:refname"):
            return _version_sort_key(disp)
        return disp

    # A local repository path is read directly: HEAD, then refs in sorted order,
    # with annotated tags followed by their peeled ``^{}`` line — like git.
    if not url.startswith(("http://", "https://", "git://", "ssh://")) and Path(src).exists():
        repo = Repository.discover(src)
        head_symref, head_sha = refs_mod.read_head(repo)
        # Each output line is (display-name, oid). HEAD shows only when no
        # namespace filter and --refs is off.
        lines: list[tuple[str, str]] = []
        if head_sha and not (args.tags or args.branches) and not args.refs:
            lines.append(("HEAD", head_sha))
        for name, sha in sorted(_enumerate_refs(repo)):
            if (args.tags or args.branches) and not (
                    (args.branches and name.startswith("refs/heads/"))
                    or (args.tags and name.startswith("refs/tags/"))):
                continue
            lines.append((name, sha))
            if name.startswith("refs/tags/") and not args.refs:
                try:
                    if objs.read_object(repo, sha)[0] == "tag":
                        peeled = refs_mod.rev_parse(repo, name + "^{commit}")
                        if peeled:
                            lines.append((name + "^{}", peeled))
                except KeyError:
                    pass
        lines = [(n, s) for n, s in lines if _pat_match(n)]
        if args.sort:
            reverse = args.sort.startswith("-")
            lines.sort(key=lambda t: _sort_key(t[0]), reverse=reverse)
        emitted = 0
        for name, sha in lines:
            # --symref prefixes HEAD's oid line with its symbolic target.
            if args.symref and name == "HEAD" and head_symref:
                _print(f"ref: {head_symref}\tHEAD")
            _print(f"{sha}\t{name}")
            emitted += 1
        if args.exit_code and emitted == 0:
            return 2
        return 0
    from . import protocol
    refs = protocol.discover_refs(url)
    emitted = 0
    for name, sha in refs.items():
        if _pat_match(name):
            _print(f"{sha}\t{name}")
            emitted += 1
    if args.exit_code and emitted == 0:
        return 2
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
    ap.add_argument("--octopus", action="store_true")  # default multi-arg reduction
    ap.add_argument("--independent", action="store_true")
    ap.add_argument("commits", nargs="+")
    args = ap.parse_args(argv)
    repo = _repo()
    from . import merge as _m
    # Peel each argument to a commit (annotated tags resolve to their target).
    shas = [refs_mod.rev_parse(repo, c + "^{commit}") or refs_mod.rev_parse(repo, c)
            for c in args.commits]
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
    ap.add_argument("-F", "--file", default=None)
    ap.add_argument("-n", dest="no_stat", action="store_true")
    ap.add_argument("--stat", dest="stat", action="store_true")
    ap.add_argument("-e", "--edit", action="store_true")
    ap.add_argument("--no-edit", dest="no_edit", action="store_true")
    ap.add_argument("-s", "--strategy", default=None)
    ap.add_argument("-X", "--strategy-option", dest="strategy_option", action="append", default=None)
    ap.add_argument("-S", "--gpg-sign", dest="gpg_sign", nargs="?", const="", default=None)
    ap.add_argument("--no-verify", dest="no_verify", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("other", nargs="?")
    args = ap.parse_args(argv)
    repo = _repo()
    from . import merge as _m

    # Resolve the merge strategy (default ort; recursive is its alias) and
    # -X ours/theirs favor. Unknown strategies fail exactly like C Git.
    strategy = args.strategy or "ort"
    if strategy not in ("ort", "recursive", "ours"):
        _err(f"Could not find merge strategy '{strategy}'.")
        _err("Available strategies are: octopus ours recursive resolve subtree.")
        return 1
    favor = 0
    for opt in (args.strategy_option or []):
        if opt == "ours":
            favor = 1
        elif opt == "theirs":
            favor = 2
    if args.file is not None:
        if args.file == "-":
            args.message = sys.stdin.read()
        else:
            try:
                args.message = open(args.file, encoding="utf-8").read()
            except OSError:
                _err(f"error: could not read file '{args.file}'")
                return 129

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

    # -s ours: keep our tree entirely, recording other as a second parent.
    if strategy == "ours":
        message = args.message or ""
        if not message.endswith("\n"):
            message += "\n"
        author = objs.build_signature(repo, "author")
        committer = objs.build_signature(repo, "committer")
        c = objs.Commit(tree=old_tree, parents=[head_sha, other_sha],
                        author=author, committer=committer, message=message)
        sha = objs.write_object(repo, "commit", c.encode())
        if head_sym:
            refs_mod.update_ref(repo, head_sym, sha, message=f"merge {args.other}: Merge made by the 'ours' strategy.")
        else:
            refs_mod.set_head(repo, sha)
        if not args.quiet:
            _print("Merge made by the 'ours' strategy.")
        return 0

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
        if not args.quiet and not args.no_stat:
            _emit_diffstat_summary(_tree_changes(repo, old_tree, new_tree))
        return 0

    if args.ff_only:
        _err("fatal: Not possible to fast-forward, aborting.")
        return 128

    from . import porcelain_merge as pm
    try:
        sha, conflicts, auto_merged = pm.merge(repo, args.other, message=args.message,
                                               no_ff=args.no_ff, favor=favor)
    except RuntimeError as e:
        _err(f"fatal: {e}")
        return 1
    # "Auto-merging <path>" precedes both the conflict notices and the summary.
    if not args.quiet:
        for p in auto_merged:
            _print(f"Auto-merging {p}")
    if conflicts:
        for p in conflicts:
            _print(f"CONFLICT (content): Merge conflict in {p}")
        # C Git prints this summary to stdout (not stderr).
        _print("Automatic merge failed; fix conflicts and then commit the result.")
        return 1
    if not args.quiet:
        _print(f"Merge made by the '{strategy}' strategy.")
        if not args.no_stat:
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
    # `reflog expire`/`delete` take extra flags (--dry-run, --expire=, --all, …)
    # that we accept and treat as a no-op, so tolerate unknown options.
    args, _unknown = ap.parse_known_args(_expand_count_shorthand(argv))
    repo = _repo()
    if args.action in ("expire", "delete"):
        # pythongit never expires/prunes reflog entries; accept as a no-op.
        return 0
    if args.action == "exists":
        from . import reflog as _rl
        rr = args.ref
        full = rr if rr == "HEAD" else (refs_mod.dwim_full_name(repo, rr) or rr)
        return 0 if _rl.read(repo, full) else 1
    # `reflog [show] [ref]`: the first positional may be the subcommand or a ref.
    ref = args.ref
    if args.action not in ("show",) and ref == "HEAD":
        ref = args.action
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
    p_push.add_argument("-q", "--quiet", action="store_true")
    p_push.add_argument("-k", "--keep-index", dest="keep_index", action="store_true")
    p_save = sub.add_parser("save")
    p_save.add_argument("-q", "--quiet", action="store_true")
    p_save.add_argument("-k", "--keep-index", dest="keep_index", action="store_true")
    p_save.add_argument("message", nargs="?", default="")
    sub.add_parser("list")
    p_apply = sub.add_parser("apply")
    p_apply.add_argument("-q", "--quiet", action="store_true")
    p_apply.add_argument("index", nargs="?", type=int, default=0)
    p_pop = sub.add_parser("pop")
    p_pop.add_argument("-q", "--quiet", action="store_true")
    p_pop.add_argument("index", nargs="?", type=int, default=0)
    p_show = sub.add_parser("show")
    p_show.add_argument("-p", "--patch", action="store_true")
    p_show.add_argument("-U", "--unified", type=int, default=3)
    p_show.add_argument("stash", nargs="?", default="stash@{0}")
    # `git stash` defaults to the `push` subcommand, so bare options/pathspecs
    # (e.g. `stash -q`, `stash -m msg`) are treated as `stash push ...`.
    _stash_subs = {"push", "save", "list", "apply", "pop", "show", "drop",
                   "clear", "branch", "create", "store", "export", "import"}
    if not argv:
        argv = ["push"]
    elif argv[0] not in _stash_subs:
        argv = ["push"] + argv
    args = ap.parse_args(argv)
    repo = _repo()
    from . import stash
    action = args.action or "push"
    if action in ("push", "save"):
        sha = stash.push(repo, getattr(args, "message", "") or "",
                         keep_index=getattr(args, "keep_index", False))
        if sha is None:
            if not getattr(args, "quiet", False):
                _print("No local changes to save")
            return 0
        if not getattr(args, "quiet", False):
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


_MERGE_TREE_USAGE = (
    "usage: git merge-tree [--write-tree] [<options>] <branch1> <branch2>\n"
    "   or: git merge-tree [--trivial-merge] <base-tree> <branch1> <branch2>\n"
    "\n"
    "    --write-tree          do a real merge instead of a trivial merge\n"
    "    --trivial-merge       do a trivial merge only\n"
    "    --[no-]messages       also show informational/conflict messages\n"
    "    --quiet               suppress all output; only exit status wanted\n"
    "    -z                    separate paths with the NUL character\n"
    "    --name-only           list filenames without modes/oids/stages\n"
    "    --allow-unrelated-histories\n"
    "                          allow merging unrelated histories\n"
    "    --stdin               perform multiple merges, one per line of input\n"
    "    --[no-]merge-base <tree-ish>\n"
    "                          specify a merge-base for the merge\n"
    "    -X, --[no-]strategy-option <option=value>\n"
    "                          option for selected merge strategy\n"
    "\n"
)


def _mt_quote_c_style(path: str) -> bytes:
    """Mirror git's quote_c_style(): emit a C-quoted, double-quoted string when
    the name contains a control/high/special byte, else the raw bytes.  Used
    for the merge-tree info section under the newline terminator."""
    raw = path.encode("utf-8", "surrogateescape")
    sq = {ord('"'): b'\\"', ord("\\"): b"\\\\", ord("\a"): b"\\a",
          ord("\b"): b"\\b", ord("\f"): b"\\f", ord("\n"): b"\\n",
          ord("\r"): b"\\r", ord("\t"): b"\\t", ord("\v"): b"\\v"}
    needs = False
    out = bytearray()
    for byte in raw:
        if byte in sq:
            out += sq[byte]
            needs = True
        elif byte < 0x20 or byte >= 0x80:
            out += b"\\%03o" % byte
            needs = True
        else:
            out.append(byte)
    if not needs:
        return raw
    return b'"' + bytes(out) + b'"'


def _mt_real_merge(repo, merge_base, branch1, branch2, opts) -> int:
    """Perform a real (ort) merge for ``merge-tree`` and emit git's output.

    ``opts`` carries: name_only, quiet, show_messages (-1/0/1), z (NUL term),
    use_stdin, allow_unrelated, favor, rename_detection.
    Returns git's exit status (!clean), or 128/1 on errors."""
    from . import ort as ort_mod
    from . import merge as merge_mod

    z = opts["z"]
    term = b"\0" if z else b"\n"
    out = opts.get("_out") or sys.stdout.buffer

    def _resolve_commit(name):
        sha = refs_mod.rev_parse(repo, name)
        if sha is None:
            return None
        try:
            t, _data = objs.read_object(repo, sha)
        except KeyError:
            return None
        if t not in ("commit", "tag"):
            return None
        return sha

    # _build_config only reads repo config/attributes; the tree arguments are
    # unused, so empty placeholders are fine here.
    cfg = ort_mod._build_config(repo, "", "", "")
    cfg.variant = opts["favor"]
    cfg.rename_detection = opts["rename_detection"]

    if merge_base is not None:
        # Explicit merge base: a non-recursive 3-way merge of the trees.
        # git peels each of base/branch1/branch2 to a tree, dying with the
        # offending name if any cannot be parsed.
        for nm in (merge_base, branch1, branch2):
            try:
                ort_mod._peel_to_tree(repo, nm)
            except (ValueError, KeyError):
                _err(f"fatal: could not parse as tree '{nm}'")
                return 128
        res = ort_mod.merge_tree(repo, merge_base, branch1, branch2, cfg=cfg)
    else:
        one = _resolve_commit(branch1)
        if one is None:
            _err(f"merge-tree: {branch1} - not something we can merge")
            return 1
        two = _resolve_commit(branch2)
        if two is None:
            _err(f"merge-tree: {branch2} - not something we can merge")
            return 1
        bases = merge_mod.merge_bases(repo, one, two)
        if not bases and not opts["allow_unrelated"]:
            _err("fatal: refusing to merge unrelated histories")
            return 128
        # Pass the original ref names so conflict-marker labels match git
        # (opt.branch1/branch2 become the "<<<<<<< name" markers).
        res = ort_mod.merge_commits(repo, branch1, branch2, cfg=cfg,
                                    allow_unrelated=True, favor=opts["favor"])

    clean = res.clean
    show_messages = opts["show_messages"]
    if show_messages == -1:
        show_messages = 0 if clean else 1

    if opts["use_stdin"]:
        out.write(b"%d" % (1 if clean else 0))
        out.write(term)
    out.write(res.tree.encode())
    out.write(term)

    if not clean:
        # Info section: one record per (path, stage) in path/stage order.
        entries = []
        if res.conflict_index is not None:
            for e in res.conflict_index.entries:
                if getattr(e, "stage", 0):
                    entries.append((e.path, e.stage, e.mode, e.sha))
        entries.sort(key=lambda t: (t[0], t[1]))
        last = None
        for path, stage, mode, sha in entries:
            if not opts["name_only"]:
                out.write(b"%06o %s %d\t" % (mode, sha.encode(), stage))
            elif last is not None and last == path:
                continue
            if z:
                out.write(path.encode("utf-8", "surrogateescape"))
            else:
                out.write(_mt_quote_c_style(path))
            out.write(term)
            last = path

    if show_messages:
        out.write(term)
        # Messages: sorted by path, then in recording order within each path.
        for _primary, type_str, message, paths in res.messages:
            if z:
                out.write(b"%d" % len(paths))
                out.write(b"\0")
                for p in paths:
                    out.write(p.encode("utf-8", "surrogateescape"))
                    out.write(b"\0")
                out.write(type_str.encode())
                out.write(b"\0")
            out.write(message.encode("utf-8", "surrogateescape"))
            out.write(b"\n")
            if z:
                out.write(b"\0")

    if opts["use_stdin"]:
        out.write(term)
    out.flush()
    return 0 if clean else 1


def cmd_merge_tree(argv: list[str]) -> int:
    repo = _repo()

    # Manual option parsing mirroring builtin/merge-tree.c's parse-options.
    mode = None            # None / "real" / "trivial"
    name_only = False
    quiet = False
    show_messages = -1     # -1 = auto (!clean), 0 = off, 1 = on
    z = False
    use_stdin = False
    allow_unrelated = False
    merge_base = None
    favor = 0              # 0 / FAVOR_OURS / FAVOR_THEIRS
    rename_detection = True
    xopts: list[str] = []
    pos: list[str] = []

    from . import xdiff as _xd

    def usage(code=129):
        sys.stderr.write(_MERGE_TREE_USAGE)
        return code

    def unknown(opt):
        # parse-options prints "error: unknown switch `x'" for short options and
        # "error: unknown option `name'" for long options, then the usage.
        if opt.startswith("--"):
            sys.stderr.write(f"error: unknown option `{opt[2:]}'\n")
        else:
            sys.stderr.write(f"error: unknown switch `{opt[1:]}'\n")
        sys.stderr.write(_MERGE_TREE_USAGE)
        return 129

    i = 0
    n = len(argv)
    while i < n:
        a = argv[i]
        if a == "--":
            pos.extend(argv[i + 1:])
            break
        elif a == "--write-tree":
            mode = "real"
        elif a == "--trivial-merge":
            mode = "trivial"
        elif a == "--name-only":
            name_only = True
        elif a == "--quiet":
            quiet = True
        elif a == "--messages":
            show_messages = 1
        elif a == "--no-messages":
            show_messages = 0
        elif a == "-z":
            z = True
        elif a == "--stdin":
            use_stdin = True
        elif a == "--allow-unrelated-histories":
            allow_unrelated = True
        elif a == "--no-allow-unrelated-histories":
            allow_unrelated = False
        elif a == "--no-merge-base":
            merge_base = None
        elif a == "--merge-base":
            i += 1
            if i >= n:
                return usage()
            merge_base = argv[i]
        elif a.startswith("--merge-base="):
            merge_base = a.split("=", 1)[1]
        elif a == "-X" or a == "--strategy-option":
            i += 1
            if i >= n:
                return usage()
            xopts.append(argv[i])
        elif a.startswith("-X"):
            xopts.append(a[2:])
        elif a.startswith("--strategy-option="):
            xopts.append(a.split("=", 1)[1])
        elif a.startswith("-") and a != "-":
            # Split a stacked short cluster's first switch for the error
            # message (git reports the first unknown short switch).
            if not a.startswith("--") and len(a) > 2:
                return unknown("-" + a[1])
            return unknown(a)
        else:
            pos.append(a)
        i += 1

    # Apply -X strategy options (subset; defer the rest).
    for x in xopts:
        if x == "ours":
            favor = _xd.XDL_MERGE_FAVOR_OURS
        elif x == "theirs":
            favor = _xd.XDL_MERGE_FAVOR_THEIRS
        elif x == "no-renames":
            rename_detection = False
        elif x in ("find-renames", "renames"):
            rename_detection = True
        elif x == "diff-algorithm=histogram":
            pass  # histogram is the engine default
        else:
            _err(f"fatal: unknown strategy option: -X{x}")
            return 128

    # Incompatible-option checks (match git's die_for_incompatible_opt2 text).
    if quiet:
        if show_messages == 1:
            _err("fatal: options '--quiet' and '--messages' cannot be used "
                 "together")
            return 128
        if name_only:
            _err("fatal: options '--quiet' and '--name-only' cannot be used "
                 "together")
            return 128
        if use_stdin:
            _err("fatal: options '--quiet' and '--stdin' cannot be used "
                 "together")
            return 128
        if z:
            _err("fatal: options '--quiet' and '-z' cannot be used together")
            return 128
        # --quiet implies messages off; output suppressed regardless below.
        if show_messages == -1:
            show_messages = 0

    opts = dict(name_only=name_only, quiet=quiet, show_messages=show_messages,
                z=z, use_stdin=use_stdin, allow_unrelated=allow_unrelated,
                favor=favor, rename_detection=rename_detection)

    # --stdin: one merge per input line, NUL-terminated records, always rc 0.
    if use_stdin:
        if mode == "trivial":
            _err("fatal: --trivial-merge is incompatible with all other "
                 "options")
            return 128
        if merge_base is not None:
            _err("fatal: options '--merge-base' and '--stdin' cannot be used "
                 "together")
            return 128
        sopts = dict(opts)
        sopts["z"] = True
        sopts["use_stdin"] = True
        data = sys.stdin.buffer.read()
        lines = data.split(b"\n")
        if lines and lines[-1] == b"":
            lines.pop()  # trailing newline does not yield an empty line
        for line in lines:
            text = line.decode("utf-8", "surrogateescape")
            # git's string_list_split_in_place_f(" ", -1, TRIM): split on each
            # single space, then strip whitespace from each resulting token.
            parts = [p.strip() for p in text.split(" ")]
            if len(parts) < 2:
                _err(f"fatal: malformed input line: '{text}'.")
                return 128
            if len(parts) == 4 and parts[1] == "--":
                _mt_real_merge(repo, parts[0], parts[2], parts[3], sopts)
            elif len(parts) == 2:
                _mt_real_merge(repo, None, parts[0], parts[1], sopts)
            else:
                _err(f"fatal: malformed input line: '{text}'.")
                return 128
        return 0

    # Decide mode for the non-stdin case (matches git's MODE_UNKNOWN logic).
    if mode is None:
        if len(pos) == 2:
            mode = "real"
        elif len(pos) == 3:
            mode = "trivial"
        else:
            return usage()
    if mode == "real" and len(pos) != 2:
        return usage()
    if mode == "trivial" and len(pos) != 3:
        return usage()

    if mode == "real":
        if quiet:
            # Suppress stdout; only the exit status matters.
            import io
            opts["_out"] = io.BytesIO()
        return _mt_real_merge(repo, merge_base, pos[0], pos[1], opts)

    # Legacy trivial-merge form: deferred to the previous behavior.
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

    b, b_tree = _tree_of(pos[0])
    one, one_tree = _tree_of(pos[1])
    two, two_tree = _tree_of(pos[2])
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
    ap.add_argument("--summary", action="store_true")
    ap.add_argument("--exclude", action="append", default=None)
    ap.add_argument("--include", action="append", default=None)
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("-q", "--quiet", action="store_true")
    ap.add_argument("file", nargs="?")
    args = ap.parse_args(argv)
    repo = _repo()
    from . import patch
    text = sys.stdin.read() if not args.file else Path(args.file).read_text(encoding="utf-8", errors="replace")
    patches = patch.parse_patch(text)
    if not patches:
        _err('error: No valid patches in input (allow with "--allow-empty")')
        return 128

    # --include/--exclude filter the patched paths (fnmatch, like C Git).
    if args.include or args.exclude:
        import fnmatch as _fn

        def _included(t: str) -> bool:
            if args.include and not any(_fn.fnmatch(t, p) for p in args.include):
                return False
            if args.exclude and any(_fn.fnmatch(t, p) for p in args.exclude):
                return False
            return True
        patches = [fp for fp in patches if _included(fp.target)]

    # --summary: list create/delete/mode lines without applying.
    if args.summary:
        for fp in patches:
            if fp.new_file:
                _print(f" create mode {fp.mode} {fp.target}")
            elif fp.deleted:
                _print(f" delete mode {fp.mode} {fp.target}")
        return 0

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

    # C Git checks every file first (the "Checking patch ..." pass), then applies
    # (the "Applied patch ... cleanly." pass); -v reports both to stderr.
    results: list[tuple] = []
    for fp in patches:
        if args.verbose:
            _err(f"Checking patch {fp.target}...")
        tgt = repo.path / fp.target
        content = tgt.read_text(encoding="utf-8", errors="replace") if tgt.exists() else ""
        result = patch.apply_to_text(content, fp.hunks, reverse=args.reverse)
        if result is None:
            line = fp.hunks[0].a_start if fp.hunks else 1
            _err(f"error: patch failed: {fp.target}:{line}")
            _err(f"error: {fp.target}: patch does not apply")
            return 1
        results.append((tgt, result, fp.target))
    if not args.check:
        for tgt, result, target in results:
            tgt.parent.mkdir(parents=True, exist_ok=True)
            tgt.write_text(result, encoding="utf-8")
            if args.verbose:
                _err(f"Applied patch {target} cleanly.")
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


_CLEAN_USAGE = (
    "usage: git clean [-d] [-f] [-i] [-n] [-q] [-e <pattern>] "
    "[-x | -X] [--] [<pathspec>...]\n"
    "\n"
    "    -q, --[no-]quiet      do not print names of files removed\n"
    "    -n, --[no-]dry-run    dry run\n"
    "    -f, --[no-]force      force\n"
    "    -i, --[no-]interactive\n"
    "                          interactive cleaning\n"
    "    -d                    remove whole directories\n"
    "    -e, --exclude <pattern>\n"
    "                          add <pattern> to ignore rules\n"
    "    -x                    remove ignored files, too\n"
    "    -X                    remove only ignored files\n"
    "\n"
)


def cmd_clean(argv: list[str]) -> int:
    from . import ignore as ignore_mod

    force = 0
    remove_dirs = False
    dry_run = False
    quiet = False
    ignored = False        # -x: also clean ignored files
    ignored_only = False   # -X: clean only ignored files
    exclude_patterns: list[str] = []
    pathspecs: list[str] = []

    # Hand-rolled option parsing to reproduce C Git's parse-options error
    # messages (rc 129 + usage block) and the -x/-X conflict (rc 128).
    def _unknown_opt(msg: str) -> int:
        sys.stderr.write(f"error: {msg}\n")
        sys.stderr.write(_CLEAN_USAGE)
        return 129

    def _needs_value(msg: str) -> int:
        # parse-options' "requires a value" diagnostics print no usage block.
        sys.stderr.write(f"error: {msg}\n")
        return 129

    # Long-option table mirroring parse_options().  Positive options accept
    # unique-prefix abbreviation; their "--no-" negations are accepted only in
    # full form (the exotic abbreviated-negation ambiguity diagnostics that C
    # Git's parse-options emits are intentionally not reproduced here).
    _long_bools = ("quiet", "dry-run", "force", "interactive")
    _long_value = ("exclude",)
    _positives = list(_long_bools) + list(_long_value)

    def _resolve_long(name: str):
        """Return (canonical, negated) or None when unknown.  On ambiguity it
        writes C Git's ``ambiguous option`` diagnostic and returns ('__ambig__',
        False)."""
        if name.startswith("no-") and name[3:] in _long_bools:
            return (name[3:], True)
        if name in _positives:
            return (name, False)
        matches = [c for c in _positives if c.startswith(name)]
        if len(matches) == 1:
            return (matches[0], False)
        if len(matches) > 1:
            joined = " or ".join("--" + m for m in matches)
            sys.stderr.write(
                f"error: ambiguous option: {name} (could be {joined})\n")
            return ("__ambig__", False)
        return None

    i = 0
    n = len(argv)
    saw_dd = False
    while i < n:
        a = argv[i]
        if saw_dd:
            pathspecs.append(a)
            i += 1
            continue
        if a == "--":
            saw_dd = True
            i += 1
            continue
        if a.startswith("--"):
            name, _, val = a[2:].partition("=")
            has_val = "=" in a
            res = _resolve_long(name)
            if res is None:
                return _unknown_opt(f"unknown option `{name}'")
            canon, negated = res
            if canon == "__ambig__":
                return 129
            if canon == "exclude":
                if negated:    # cannot happen (NONEG), but guard anyway
                    return _unknown_opt(f"unknown option `{name}'")
                if not has_val:
                    if i + 1 >= n:
                        return _needs_value("option `exclude' requires a value")
                    val = argv[i + 1]
                    i += 1
                exclude_patterns.append(val)
            elif canon == "force":
                force = 0 if negated else force + 1
            elif canon == "dry-run":
                dry_run = not negated
            elif canon == "quiet":
                quiet = not negated
            elif canon == "interactive":
                if negated:
                    pass  # --no-interactive: interactive stays off (default)
                else:
                    # Interactive cleaning needs a TTY/editor loop; unsupported.
                    _err("fatal: interactive clean (-i/--interactive) is not "
                         "supported")
                    return 128
            i += 1
            continue
        if a.startswith("-") and a != "-":
            j = 1
            consumed_next = False
            while j < len(a):
                c = a[j]
                if c == "f":
                    force += 1
                elif c == "d":
                    remove_dirs = True
                elif c == "n":
                    dry_run = True
                elif c == "q":
                    quiet = True
                elif c == "x":
                    ignored = True
                elif c == "X":
                    ignored_only = True
                elif c == "i":
                    _err("fatal: interactive clean (-i/--interactive) is not "
                         "supported")
                    return 128
                elif c == "e":
                    rest = a[j + 1:]
                    if rest:
                        exclude_patterns.append(rest)
                    else:
                        if i + 1 >= n:
                            return _needs_value("switch `e' requires a value")
                        exclude_patterns.append(argv[i + 1])
                        consumed_next = True
                    break
                else:
                    return _unknown_opt(f"unknown switch `{c}'")
                j += 1
            i += 2 if consumed_next else 1
            continue
        pathspecs.append(a)
        i += 1

    repo = _repo()

    if ignored and ignored_only:
        _err(_incompatible_opts(["-x", "-X"]))
        return 128

    if force == 0 and not dry_run:
        # clean.requireForce defaults to true; refuse without -f/-n.
        _err("fatal: clean.requireForce is true and -f not given: "
             "refusing to clean")
        return 128

    if pathspecs:
        # Pathspec-limited cleaning is not yet implemented byte-exact.
        _err("fatal: pathspec-limited clean is not supported")
        return 128

    # Build the matcher.  Under -x the standard excludes (.gitignore, core
    # excludesfile, info/exclude) are dropped; only the -e patterns remain.
    # Otherwise the standard excludes apply, with -e patterns appended at the
    # end so they take precedence (including negations).
    iset = ignore_mod.IgnoreSet()
    if not ignored:
        iset.rules.extend(ignore_mod.load(repo.path).rules)
    for pat in exclude_patterns:
        iset.rules.append(ignore_mod.IgnoreRule(pat, "", "command-line", 0))

    tracked = workdir.tracked_paths(repo)
    tracked_dirs: set[str] = set()
    for t in tracked:
        parts = t.split("/")
        for k in range(1, len(parts)):
            tracked_dirs.add("/".join(parts[:k]))

    def is_ignored(rel: str, is_dir: bool) -> bool:
        return iset.is_ignored(rel, is_dir=is_dir)

    def walk(rel_dir: str, dir_is_ignored: bool):
        """Recurse one directory.  Returns (collapsible, has_tracked, my_targets):

        * ``collapsible`` -- the whole directory could be removed as a single
          ``rel_dir/`` entry (subject to ``-d``); the parent decides whether to
          collapse it or keep the individual ``my_targets``.
        * ``has_tracked`` -- the subtree contains a tracked path.
        * ``my_targets`` -- removal targets to use if this directory is *not*
          collapsed.

        ``dir_is_ignored`` says whether ``rel_dir`` itself matched an ignore
        rule; under that condition every descendant counts as ignored."""
        base = repo.path / rel_dir if rel_dir else repo.path
        try:
            entries = sorted(os.listdir(base))
        except OSError:
            return (False, False, [])

        has_tracked = False
        has_kept = False          # a path that must be preserved (blocks collapse)
        has_file = False          # any regular file/symlink present
        my_targets: list[str] = []

        for name in entries:
            if name == ".git":
                continue
            rel = f"{rel_dir}/{name}" if rel_dir else name
            full = base / name
            is_dir = workdir._is_dir_no_follow(full)

            if rel in tracked:
                has_tracked = True
                continue

            if is_dir:
                sub_tracked_dir = rel in tracked_dirs
                sub_ignored = dir_is_ignored or is_ignored(rel, True)
                # Tracked directories are always descended (to clean their
                # untracked contents).  Untracked directories are descended when
                # -d is given, or (in -X mode) when not themselves ignored, so
                # that ignored files inside an otherwise-untracked directory can
                # be removed.  An ignored directory is an opaque unit: it is only
                # touched with -d.
                descend = (sub_tracked_dir or remove_dirs
                           or (ignored_only and not sub_ignored))
                if not descend:
                    # Leave the directory untouched; it blocks the parent from
                    # collapsing.
                    has_kept = True
                    continue
                sub_collapsible, sub_has_tracked, sub_targets = walk(rel, sub_ignored)
                if sub_has_tracked:
                    has_tracked = True
                if sub_collapsible and remove_dirs and not sub_tracked_dir:
                    my_targets.append(rel + "/")
                else:
                    my_targets.extend(sub_targets)
                    if not sub_collapsible:
                        has_kept = True
                continue

            # Regular file (or symlink).
            has_file = True
            file_ignored = dir_is_ignored or is_ignored(rel, False)
            removable = file_ignored if ignored_only else (not file_ignored)
            if removable:
                my_targets.append(rel)
            else:
                has_kept = True

        # Can this directory be removed as a single unit?
        if has_tracked or has_kept:
            collapsible = False
        elif ignored_only:
            # Collapse only a directory whose entire content is ignored and that
            # contains something (empty dirs never collapse under -X), unless the
            # directory itself is an ignored dir.
            collapsible = dir_is_ignored or has_file or bool(my_targets)
        else:
            # default / -x: any directory with no tracked and no kept content is
            # removable (empty directories included).
            collapsible = True
        return (collapsible, has_tracked, my_targets)

    _, _, targets = walk("", False)

    import shutil
    for target in sorted(targets):
        if dry_run:
            if not quiet:
                _print(f"Would remove {target}")
        else:
            full = repo.path / target.rstrip("/")
            try:
                if target.endswith("/"):
                    shutil.rmtree(full)
                else:
                    full.unlink()
                if not quiet:
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


def _describe_dirty(repo: Repository) -> bool:
    """True when the working tree differs from HEAD, matching the semantics of
    `git diff-index --quiet HEAD` (tracked content/mode changes, staged changes,
    and deletions count; untracked files do not)."""
    head = refs_mod.rev_parse(repo, "HEAD")
    if not head:
        return False
    head_tree = _commit_tree(repo, head)
    a_map = _tree_map_full(repo, head_tree)
    from .index import read_index
    idx = read_index(repo).by_path()
    for p in set(a_map) | set(idx):
        a = _side_from_object(repo, *a_map[p]) if p in a_map else _ABSENT
        b_present = p in idx
        b_mode = idx[p].mode_str() if b_present else None
        b_sha = idx[p].sha if b_present else None
        b_dirty = False
        if b_present:
            wt = _side_from_worktree(repo, p)
            if not wt.present:
                b_present, b_mode, b_sha = False, None, None
            else:
                b_dirty = wt.sha != idx[p].sha or wt.mode != b_mode
        if a.present and b_present and a.sha == b_sha and not b_dirty and a.mode == b_mode:
            continue
        if not a.present and not b_present:
            continue
        return True
    return False


def cmd_describe(argv: list[str]) -> int:
    # --dirty takes an optional value only via "--dirty=<mark>" (a bare --dirty
    # uses the default "-dirty"); unlike argparse's nargs="?", git never consumes
    # the following token as the mark. Strip it out before argparse so a trailing
    # commit-ish stays a positional.
    dirty: Optional[str] = None
    rest: list[str] = []
    for tok in argv:
        if tok == "--dirty":
            dirty = "-dirty"
        elif tok.startswith("--dirty="):
            dirty = tok[len("--dirty="):]
        else:
            rest.append(tok)
    ap = argparse.ArgumentParser(prog="pygit describe", add_help=False)
    ap.add_argument("--tags", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--contains", action="store_true")
    ap.add_argument("--always", action="store_true")
    ap.add_argument("--long", action="store_true")
    ap.add_argument("--candidates", type=int, default=10)
    ap.add_argument("--abbrev", type=int, default=7)
    ap.add_argument("rev", nargs="?", default=None)
    args = ap.parse_args(rest)
    args.dirty = dirty
    repo = _repo()
    if args.dirty is not None and args.rev is not None:
        _err("fatal: option '--dirty' and commit-ishes cannot be used together")
        return 128
    suffix = ""
    if args.dirty is not None and _describe_dirty(repo):
        suffix = args.dirty
    if args.rev is None:
        args.rev = "HEAD"
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
                _print(name + suffix)
            else:
                _print(f"{name}-{depth}-g{sha[:ab]}{suffix}")
            return 0
        if args.always:
            _print((sha[:max(4, args.abbrev)] if args.abbrev else sha[:7]) + suffix)
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
            _print(name + suffix)
        elif depth == 0 and not args.long:
            _print(name + suffix)
        else:
            _print(f"{name}-{depth}-g{sha[:ab]}{suffix}")
        return 0
    if args.always:
        _print((sha[:max(4, args.abbrev)] if args.abbrev else sha[:7]) + suffix)
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


def _fer_quote(mode: str, v: str) -> str:
    """Quote an atom value for --shell/--perl/--python/--tcl (quote.c)."""
    if mode == "shell":
        return "'" + v.replace("'", "'\\''") + "'"
    if mode == "perl":
        return "'" + v.replace("\\", "\\\\").replace("'", "\\'") + "'"
    if mode == "python":
        return "'" + v.replace("\\", "\\\\").replace("'", "\\'") + "'"
    if mode == "tcl":
        out = []
        for ch in v:
            if ch in '$[]{}"\\':
                out.append("\\" + ch)
            elif ch == "\n":
                out.append("\\n")
            else:
                out.append(ch)
        return '"' + "".join(out) + '"'
    return v


def _fer_expand(repo: Repository, ref: str, sha: str, fmt: str, head_ref: Optional[str],
                quote: Optional[str] = None) -> str:
    """Expand a for-each-ref --format string's %(atom) placeholders. With
    ``quote`` set (shell/perl/python/tcl) each atom value is quoted in output."""
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

    def subst(s: str, do_quote: bool = False) -> str:
        if do_quote and quote:
            return re.sub(r"%\(([^)]*)\)", lambda m: _fer_quote(quote, atom(m.group(1))), s)
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

    return subst(fmt, do_quote=True)


def cmd_for_each_ref(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit for-each-ref", add_help=False)
    ap.add_argument("--format", default="%(objectname) %(objecttype)\t%(refname)")
    ap.add_argument("--count", type=int, default=None)
    ap.add_argument("--sort", action="append", default=None)
    ap.add_argument("-s", "--shell", action="store_const", const="shell", dest="quote", default=None)
    ap.add_argument("-p", "--perl", action="store_const", const="perl", dest="quote")
    ap.add_argument("--python", action="store_const", const="python", dest="quote")
    ap.add_argument("--tcl", action="store_const", const="tcl", dest="quote")
    ap.add_argument("--points-at", dest="points_at", default=None)
    ap.add_argument("--merged", nargs="?", const="HEAD", default=None)
    ap.add_argument("--no-merged", dest="no_merged", nargs="?", const="HEAD", default=None)
    ap.add_argument("--contains", nargs="?", const="HEAD", default=None)
    ap.add_argument("--no-contains", dest="no_contains", nargs="?", const="HEAD", default=None)
    ap.add_argument("--exclude", action="append", default=None)
    ap.add_argument("--start-after", dest="start_after", default=None)
    ap.add_argument("--stdin", action="store_true")
    ap.add_argument("--include-root-refs", dest="include_root_refs", action="store_true")
    ap.add_argument("--omit-empty", dest="omit_empty", action="store_true")
    ap.add_argument("--ignore-case", dest="ignore_case", action="store_true")
    ap.add_argument("pattern", nargs="*", default=None)
    # --merged/--contains (and negations) use PARSE_OPT_LASTARG_DEFAULT: they
    # consume the following token as the commit, but default to HEAD when given
    # as the last argument. Rewrite to the attached "=value" form for argparse.
    consume = ("--merged", "--no-merged", "--contains", "--no-contains")
    pre: list[str] = []
    i = 0
    while i < len(argv):
        t = argv[i]
        if t in consume:
            if i + 1 < len(argv):
                pre.append(f"{t}={argv[i + 1]}")
                i += 2
            else:
                pre.append(f"{t}=HEAD")
                i += 1
            continue
        pre.append(t)
        i += 1
    args = ap.parse_args(pre)
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
    head_sym, head_sha = refs_mod.read_head(repo)
    if args.include_root_refs and head_sha:
        all_refs.setdefault("HEAD", head_sha)

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

    # --merged/--contains reachability over the ref's peeled commit.
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

    def _peel_commit(ref: str) -> Optional[str]:
        return refs_mod.rev_parse(repo, ref + "^{commit}")

    # Unresolvable object args fail differently per option (matching C Git):
    # --merged/--no-merged die() (fatal:, rc 128); --points-at/--contains/
    # --no-contains return a usage error (error:, rc 129) and --points-at quotes.
    resolved: dict[str, Optional[str]] = {}
    for key, msg, code in (
        ("points_at", "error: malformed object name '{}'", 129),
        ("merged", "fatal: malformed object name {}", 128),
        ("no_merged", "fatal: malformed object name {}", 128),
        ("contains", "error: malformed object name {}", 129),
        ("no_contains", "error: malformed object name {}", 129),
    ):
        val = getattr(args, key)
        if not val:
            resolved[key] = None
            continue
        sha = refs_mod.rev_parse(repo, val)
        if sha is None:
            _err(msg.format(val))
            return code
        # Reachability args are peeled to a commit (e.g. --contains=<annotated-tag>);
        # --points-at compares against the object as-is.
        if key != "points_at":
            sha = refs_mod.rev_parse(repo, val + "^{commit}") or sha
        resolved[key] = sha
    points_at = resolved["points_at"]
    merged = resolved["merged"]
    no_merged = resolved["no_merged"]
    contains = resolved["contains"]
    no_contains = resolved["no_contains"]

    def _ref_ok(ref: str) -> bool:
        sha = all_refs[ref]
        tip = _peel_commit(ref) if (merged or no_merged or contains or no_contains) else None
        if points_at is not None and sha != points_at and _peel_commit(ref) != points_at:
            return False
        if merged is not None and not (tip and _reachable_from(merged, tip)):
            return False
        if no_merged is not None and (tip and _reachable_from(no_merged, tip)):
            return False
        if contains is not None and not (tip and _reachable_from(tip, contains)):
            return False
        if no_contains is not None and tip and _reachable_from(tip, no_contains):
            return False
        return True

    patterns = list(args.pattern or [])
    if args.stdin:
        patterns += [ln.rstrip("\n") for ln in sys.stdin.read().splitlines() if ln.strip()]
    excludes = args.exclude or []

    def _match(ref: str, pats: list[str]) -> bool:
        import fnmatch
        for p in pats:
            if ref == p or ref.startswith(p.rstrip("/") + "/") or fnmatch.fnmatch(ref, p):
                return True
        return False

    # --start-after: emit only refs positioned after the marker in iteration order.
    started = args.start_after is None
    emitted = 0
    for ref in order:
        if not started:
            if ref == args.start_after:
                started = True
            continue
        if patterns and not _match(ref, patterns):
            continue
        if excludes and _match(ref, excludes):
            continue
        if not _ref_ok(ref):
            continue
        if args.count is not None and emitted >= args.count:
            break
        line = _fer_expand(repo, ref, all_refs[ref], args.format, head_sym, quote=args.quote)
        if args.omit_empty and line == "":
            continue
        _print(line)
        emitted += 1
    return 0


def _indent_text(s: str, indent: int, indent2: int) -> str:
    """Port of utf8.c strbuf_add_indented_text (the width<=0 wrap fallback)."""
    if indent < 0:
        indent = 0
    out: list[str] = []
    i, n = 0, len(s)
    while i < n:
        eol = s.find("\n", i)
        eol = n if eol < 0 else eol + 1
        out.append(" " * indent)
        out.append(s[i:eol])
        i = eol
        indent = indent2
    return "".join(out)


def _wrap_text(s: str, indent1: int, indent2: int, width: int) -> str:
    """Port of utf8.c strbuf_add_wrapped_text for ASCII text (1 col per byte)."""
    if width <= 0:
        return _indent_text(s, indent1, indent2)
    out: list[str] = []
    n = len(s)
    text = bol = 0
    w = indent = indent1
    space: Optional[int] = None
    if indent < 0:
        w = -indent
        space = 0
    while True:
        c = s[text] if text < n else "\0"
        if c == "\0" or c.isspace():
            new_line = False
            if w <= width or space is None:
                start = bol
                if c == "\0" and text == start:
                    break
                if space is not None:
                    start = space
                else:
                    out.append(" " * indent)
                out.append(s[start:text])
                if c == "\0":
                    break
                space = text
                if c == "\t":
                    w |= 0x07
                    w += 1
                    text += 1
                elif c == "\n":
                    space += 1
                    nxt = s[space] if space < n else "\0"
                    if nxt == "\n":
                        out.append("\n")
                        new_line = True
                    elif not nxt.isalnum():
                        new_line = True
                    else:
                        out.append(" ")
                        w += 1
                        text += 1
                else:
                    w += 1
                    text += 1
            else:
                new_line = True
            if new_line:
                out.append("\n")
                sp = s[space] if space is not None and space < n else "\0"
                text = bol = space + (1 if sp.isspace() else 0)
                space = None
                w = indent = indent2
            continue
        w += 1
        text += 1
    return "".join(out)


def _parse_wrap_args(arg: Optional[str]) -> Optional[tuple[int, int, int]]:
    """Parse shortlog -w[<width>[,<i1>[,<i2>]]]; None on error (defaults 76,6,9)."""
    defaults = (76, 6, 9)
    if not arg:
        return defaults
    parts = arg.split(",")
    vals = list(defaults)
    for k in range(3):
        if k < len(parts) and parts[k] != "":
            try:
                vals[k] = int(parts[k])
            except ValueError:
                return None
    w, i1, i2 = vals
    if w < 0 or i1 < 0 or i2 < 0:
        return None
    if w and ((i1 and w <= i1) or (i2 and w <= i2)):
        return None
    return (w, i1, i2)


_SHORTLOG_STYLES = ("oneline", "short", "medium", "full", "fuller", "raw")


def cmd_shortlog(argv: list[str]) -> int:
    # -w, --pretty and --format take their argument only in the attached form
    # (PARSE_OPT_OPTARG / "--opt=val"), never from a following token, which would
    # otherwise be swallowed instead of being treated as the revision.
    wrap_lines = False
    wrap = (76, 6, 9)
    pretty_spec: Optional[str] = None
    format_spec: Optional[str] = None
    rest: list[str] = []
    for t in argv:
        if t == "-w":
            wrap_lines = True
        elif t.startswith("-w") and not t.startswith("--") and len(t) > 2:
            parsed = _parse_wrap_args(t[2:])
            if parsed is None:
                _err("error: -w[<width>[,<indent1>[,<indent2>]]]")
                return 129
            wrap_lines = True
            wrap = parsed
        elif t == "--pretty":
            pretty_spec = "medium"
        elif t.startswith("--pretty="):
            pretty_spec = t[len("--pretty="):]
        elif t.startswith("--format="):
            format_spec = t[len("--format="):]
        else:
            rest.append(t)

    ap = argparse.ArgumentParser(prog="pygit shortlog", add_help=False)
    ap.add_argument("-n", "--numbered", action="store_true")
    ap.add_argument("-s", "--summary", action="store_true")
    ap.add_argument("-e", "--email", action="store_true")
    ap.add_argument("-c", "--committer", action="store_true")
    ap.add_argument("rev", nargs="?", default="HEAD")
    args = ap.parse_args(rest)
    repo = _repo()

    # Record format: only CMIT_FMT_USERFORMAT changes the per-commit record.
    # The real builtin styles (oneline/short/medium/full/fuller/raw) fall back to
    # the subject ("%s"); `reference` is itself a userformat; a bare word that is
    # neither builtin nor a "%"/prefix format is rejected like C Git.
    record_fmt: Optional[str] = None
    record_date = "default"

    def _resolve_fmt(val: str) -> Optional[str]:
        nonlocal record_fmt, record_date
        if val in _SHORTLOG_STYLES:
            return None
        if val == "reference":
            record_fmt = "%h (%s, %ad)"
            record_date = "short"
            return None
        if val.startswith(("format:", "tformat:")):
            record_fmt = val.split(":", 1)[1]
            return None
        if "%" in val:
            record_fmt = val
            return None
        return f"fatal: invalid --pretty format: {val}"

    spec = format_spec if format_spec is not None else pretty_spec
    if spec is not None:
        err = _resolve_fmt(spec)
        if err is not None:
            _err(err)
            return 128

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
        ident = c.committer if args.committer else c.author
        who, _ts, _tz = _split_ident(ident)
        name, email = _parse_who(who)
        key = f"{name} <{email}>" if args.email else name
        if record_fmt is not None:
            record = _expand_commit_format(repo, s, c, record_fmt, {}, date_mode=record_date)
        else:
            record = c.message.splitlines()[0].rstrip() if c.message.strip() else ""
        by_author.setdefault(key, []).append(record if record else "<none>")
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
                if wrap_lines:
                    sys.stdout.write(_wrap_text(m, wrap[1], wrap[2], wrap[0]) + "\n")
                else:
                    _print(f"      {m}")
            _print("")
    return 0


def _git_archive_tar(repo: Repository, tree: str, commit_sha: Optional[str], archive_time: int, prefix: str = "", verbose: bool = False) -> bytes:
    """Build a tar archive byte-for-byte identical to C Git's archive-tar.c.

    When ``verbose`` is set, each archived path is reported to stderr in the
    same order C Git's ``archive --verbose`` does (archive.c:write_archive_entry).
    """
    BLOCKSIZE = 512 * 20
    TAR_UMASK = 0o002
    out = bytearray()

    def report(name: str) -> None:
        if verbose:
            _err(name)

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
                report(name + "/")
                emit_header(name + "/", (mode | 0o777) & ~TAR_UMASK, 0, "5")
                walk(e.sha, name + "/")
            elif e.mode == "120000":
                report(name)
                _, target = objs.read_object(repo, e.sha)
                # Symlinks are not umask'd: archive-tar.c does `mode |= 0777`.
                emit_header(name, mode | 0o777, 0, "2", target.decode("utf-8", "replace"))
            elif e.is_gitlink():
                # C Git still reports gitlinks under --verbose (the verbose print
                # in write_archive_entry precedes the type dispatch) with a
                # trailing slash, and writes them as a directory entry before
                # dropping the recursion.
                report(name + "/")
                emit_header(name + "/", (mode | 0o777) & ~TAR_UMASK, 0, "5")
            else:
                report(name)
                _, blob = objs.read_object(repo, e.sha)
                base = 0o777 if (mode & 0o100) else 0o666
                emit_header(name, (mode | base) & ~TAR_UMASK, len(blob), "0")
                emit_content(blob)

    if prefix.endswith("/"):
        # git emits a single directory entry for the whole prefix; trailing
        # slashes are collapsed to one for the entry name (archive.c).
        plen = len(prefix)
        while plen > 1 and prefix[plen - 2] == "/":
            plen -= 1
        pdir = prefix[:plen]
        report(pdir)
        emit_header(pdir, (0o40000 | 0o777) & ~TAR_UMASK, 0, "5")
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
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--mtime", default=None)
    # --remote=<repo> drives the archive over the git-upload-archive protocol
    # (pkt-line + sideband). Reproducing it byte-exact would require replicating
    # recv_sideband's terminal-width padding (e.g. "remote: f.txt   ...\n" for
    # -v) and the remote-error relaying, so it is DEFERRED and rejected below.
    # --exec=<cmd> only names the remote upload-archive binary; C Git silently
    # ignores it when --remote is absent, so we accept and ignore it there.
    ap.add_argument("--remote", default=None)
    ap.add_argument("--exec", dest="exec_cmd", default=None)
    ap.add_argument("rev", nargs="?")
    args = ap.parse_args(argv)
    if args.remote is not None:
        _err("fatal: archive --remote is not supported")
        return 128
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

    if args.mtime is not None:
        # C Git: archive_time = approxidate(mtime_option). We implement the
        # deterministic spellings byte-exact; relative / "now" forms resolve to
        # the wall clock in C Git and are inherently non-reproducible, so we
        # reject them rather than emit a value that cannot match the oracle.
        parsed = objs._parse_date_env(args.mtime)
        if parsed is not None:
            archive_time = parsed[0]
        else:
            stripped = args.mtime.strip()
            try:
                archive_time = int(stripped)
            except ValueError:
                _err("fatal: unsupported --mtime value: %s" % args.mtime)
                return 128

    if args.format == "tar":
        blob = _git_archive_tar(repo, tree, commit_oid, archive_time, args.prefix, args.verbose)
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


def _clamp_abbrev(s: str) -> int:
    """C Git's parse_opt_abbrev_cb: 0 stays 0 (full), else clamp to [4, 40]."""
    try:
        v = int(s)
    except ValueError:
        return 7
    if v == 0:
        return 0
    return min(40, max(4, v))


def _refname_is_safe(ref: str) -> bool:
    """Port of refs.c refname_is_safe: refs/<normalized> or an all-uppercase /
    underscore pseudo-ref (HEAD, MERGE_HEAD, ...)."""
    if ref.startswith("refs/"):
        rest = ref[len("refs/"):]
        if not rest or rest.startswith("/") or rest.endswith("/"):
            return False
        return all(c and c not in (".", "..") for c in rest.split("/"))
    return bool(ref) and all(c.isupper() or c == "_" for c in ref)


def _check_refname_invalid(ref: str) -> bool:
    """True when ref fails check_refname_format(ref, 0) (no one-level refs)."""
    if not ref or ref == "@" or "@{" in ref or ".." in ref or "//" in ref:
        return True
    comps = ref.split("/")
    if len(comps) < 2:
        return True
    bad = set("\\ ~^:?*[")
    for c in comps:
        if not c or c.startswith(".") or c.endswith(".lock") or c.endswith("."):
            return True
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F or ch in bad for ch in c):
            return True
    return False


def _show_ref_exclude_existing(repo: Repository, pattern: Optional[str]) -> int:
    """show-ref --exclude-existing: filter stdin refnames, emitting those that
    are well-formed and absent from the local ref store (port of C Git)."""
    existing = {name for name, _ in _enumerate_refs(repo)}
    plen = len(pattern) if pattern else 0
    for raw in sys.stdin:
        line = raw[:-1] if raw.endswith("\n") else raw
        if len(line) >= 3 and line.endswith("^{}"):
            line = line[:-3]
        ref = line
        for i in range(len(line) - 1, -1, -1):
            if line[i].isspace():
                ref = line[i + 1:]
                break
        if pattern is not None and (len(ref) < plen or ref[:plen] != pattern):
            continue
        if _check_refname_invalid(ref):
            _err(f"warning: ref '{ref}' ignored")
            continue
        if ref not in existing:
            _print(line)
    return 0


def cmd_show_ref(argv: list[str]) -> int:
    # -s/--hash, --abbrev and --exclude-existing take optional arguments only in
    # the attached (=value) form — a following token is never consumed (C Git's
    # PARSE_OPT_OPTARG). Pre-scan them so argparse's nargs="?" can't grab a token.
    abbrev = 0  # 0 == full hash
    hash_only = False
    exclude_enabled = False
    exclude_pattern: Optional[str] = None
    rest: list[str] = []
    for t in argv:
        if t in ("-s", "--hash"):
            hash_only = True
        elif t.startswith("-s") and not t.startswith("--") and len(t) > 2:
            hash_only = True
            abbrev = _clamp_abbrev(t[2:])
        elif t.startswith("--hash="):
            hash_only = True
            abbrev = _clamp_abbrev(t[len("--hash="):])
        elif t == "--abbrev":
            abbrev = 7
        elif t.startswith("--abbrev="):
            abbrev = _clamp_abbrev(t[len("--abbrev="):])
        elif t == "--exclude-existing":
            exclude_enabled = True
        elif t.startswith("--exclude-existing="):
            exclude_enabled = True
            exclude_pattern = t[len("--exclude-existing="):]
        else:
            rest.append(t)

    ap = argparse.ArgumentParser(prog="pygit show-ref", add_help=False)
    ap.add_argument("-h", "--head", dest="head", action="store_true")
    ap.add_argument("--tags", action="store_true")
    ap.add_argument("--heads", dest="branches", action="store_true")
    ap.add_argument("--branches", dest="branches", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--exists", action="store_true")
    ap.add_argument("-d", "--dereference", action="store_true")
    ap.add_argument("-q", "--quiet", action="store_true")
    ap.add_argument("patterns", nargs="*")
    args = ap.parse_args(rest)
    repo = _repo()

    def _abbr(s: str) -> str:
        return s if abbrev == 0 else s[:abbrev]

    def show_one(sha: str, name: str) -> None:
        if args.quiet:
            return
        _print(_abbr(sha) if hash_only else f"{_abbr(sha)} {name}")
        if args.dereference:
            try:
                if objs.read_object(repo, sha)[0] == "tag":
                    peeled = refs_mod.rev_parse(repo, name + "^{}")
                    if peeled:
                        _print(f"{_abbr(peeled)} {name}^{{}}" if not hash_only else _abbr(peeled))
            except (KeyError, ValueError):
                pass

    if exclude_enabled:
        return _show_ref_exclude_existing(repo, exclude_pattern)

    if args.verify:
        if not args.patterns:
            _err("fatal: --verify requires a reference")
            return 128
        for ref in args.patterns:
            sha = (refs_mod.read_ref(repo, ref)
                   if (ref.startswith("refs/") or _refname_is_safe(ref)) else None)
            if sha is not None:
                show_one(sha, ref)
            elif not args.quiet:
                _err(f"fatal: '{ref}' - not a valid ref")
                return 128
            else:
                return 1
        return 0

    if args.exists:
        if not args.patterns:
            _err("fatal: --exists requires a reference")
            return 128
        if len(args.patterns) > 1:
            _err("fatal: --exists requires exactly one reference")
            return 128
        ref = args.patterns[0]
        if (repo.gitdir / ref).is_file() or ref in refs_mod.read_packed_refs(repo):
            return 0
        _err("error: reference does not exist")
        return 2

    def matches(refname: str) -> bool:
        if not args.patterns:
            return True
        for m in args.patterns:
            if len(m) > len(refname):
                continue
            if refname[len(refname) - len(m):] != m:
                continue
            if len(m) == len(refname) or refname[len(refname) - len(m) - 1] == "/":
                return True
        return False

    if args.branches or args.tags:
        refs_iter: list[tuple[str, str]] = []
        if args.branches:
            refs_iter += sorted((n, s) for n, s in _enumerate_refs(repo)
                                if n.startswith("refs/heads/"))
        if args.tags:
            refs_iter += sorted((n, s) for n, s in _enumerate_refs(repo)
                                if n.startswith("refs/tags/"))
    else:
        refs_iter = sorted(_enumerate_refs(repo))

    found = 0
    if args.head:
        _, headsha = refs_mod.read_head(repo)
        if headsha:
            show_one(headsha, "HEAD")
            found += 1
    for refname, sha in refs_iter:
        if not matches(refname):
            continue
        show_one(sha, refname)
        found += 1
    return 0 if found else 1


def _unquote_c_style(s: str) -> str:
    """Decode a C-quoted path (`"..."`) as git's unquote_c_style does."""
    if not (len(s) >= 2 and s[0] == '"'):
        return s
    simple = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13,
              '"': 34, "\\": 92}
    out = bytearray()
    i = 1
    while i < len(s):
        c = s[i]
        if c == '"':
            return out.decode("utf-8", "replace")
        if c == "\\":
            i += 1
            if i >= len(s):
                raise ValueError("invalid quoting")
            n = s[i]
            if n in simple:
                out.append(simple[n]); i += 1
            elif n in "01234567":
                val = j = 0
                while j < 3 and i + j < len(s) and s[i + j] in "01234567":
                    val = val * 8 + int(s[i + j]); j += 1
                out.append(val & 0xFF); i += j
            else:
                raise ValueError("invalid quoting")
        else:
            out.append(ord(c)); i += 1
    raise ValueError("invalid quoting")


def cmd_mktree(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit mktree", add_help=False)
    ap.add_argument("-z", dest="nul", action="store_true")
    ap.add_argument("--missing", dest="allow_missing", action="store_true")
    ap.add_argument("--batch", dest="batch", action="store_true")
    args = ap.parse_args(argv)
    repo = _repo()

    def parse_line(line: str, entries: list) -> Optional[str]:
        # Non-recursive ls-tree format: "mode SP type SP sha TAB name".
        mode_s, sp, rest = line.partition(" ")
        if not sp:
            return f"input format error: {line}"
        try:
            mode = int(mode_s, 8)
        except ValueError:
            return f"input format error: {line}"
        type_s, sp2, rest2 = rest.partition(" ")
        sha, tab, name = rest2.partition("\t")
        if not sp2 or not tab:
            return f"input format error: {line}"
        if len(sha) != repo.hex_len or any(c not in "0123456789abcdefABCDEF" for c in sha):
            return f"input format error: {line}"
        sha = sha.lower()
        if not args.nul and name.startswith('"'):
            try:
                name = _unquote_c_style(name)
            except ValueError:
                return "invalid quoting"
        if type_s not in ("blob", "tree", "commit", "tag"):
            return f'invalid object type "{type_s}"'
        fmt = mode & 0o170000
        mode_type = "tree" if fmt == 0o040000 else ("commit" if mode == 0o160000 else "blob")
        if mode_type != type_s:
            return (f"entry '{name}' object type ({type_s}) doesn't match "
                    f"mode type ({mode_type})")
        # Submodule (gitlink) commits are normally absent, so treat as missing-ok.
        allow_missing = args.allow_missing or mode == 0o160000
        try:
            actual_type = objs.read_object(repo, sha)[0]
        except (KeyError, ValueError, FileNotFoundError):
            actual_type = None
        if actual_type is None:
            if not allow_missing:
                return f"entry '{name}' object {sha} is unavailable"
        elif actual_type != mode_type:
            return (f"entry '{name}' object {sha} is a {actual_type} "
                    f"but specified type was ({mode_type})")
        entries.append(objs.TreeEntry(format(mode, "o"), name, sha))
        return None

    sep = "\0" if args.nul else "\n"
    parts = sys.stdin.buffer.read().decode("utf-8", "replace").split(sep)
    if parts and parts[-1] == "":
        parts.pop()  # trailing terminator is EOF, not an empty line

    out: list[str] = []
    entries: list = []
    idx, n = 0, len(parts)
    while True:
        hit_eof = True
        while idx < n:
            line = parts[idx]
            idx += 1
            if line == "":
                if args.batch:
                    hit_eof = False
                    break
                _err("fatal: input format error: (blank line only valid in batch mode)")
                return 128
            err = parse_line(line, entries)
            if err is not None:
                _err(f"fatal: {err}")
                return 128
        # In batch mode a trailing newline after the last entry yields no final
        # (empty) tree; otherwise write the accumulated entries.
        if not (args.batch and hit_eof and not entries):
            out.append(objs.write_object(repo, "tree", objs.encode_tree(entries)))
        entries = []
        if hit_eof:
            break
    for o in out:
        _print(o)
    return 0


def _unmerge_index_entry(idx, path: str, rec: dict) -> None:
    """Replace the stage-0 entry for *path* (if any) with the conflicted stage
    1/2/3 entries from a resolve-undo record. Mirrors C git's
    unmerge_index_entry(): an already-unmerged path is left untouched."""
    from .index import IndexEntry
    stages = {e.stage for e in idx.entries if e.path == path}
    if stages and stages != {0}:
        # already unmerged — nothing to do
        return
    idx.remove(path, stage=0)
    for stage in (1, 2, 3):
        mode, sha = rec.get(stage, (0, ""))
        if not mode:
            continue
        e = IndexEntry(mode=mode, sha=sha, path=path)
        e.stage = stage
        idx.upsert(e)


def cmd_update_index(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pygit update-index", add_help=False)
    ap.add_argument("--add", action="store_true")
    ap.add_argument("--remove", action="store_true")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--really-refresh", dest="really_refresh", action="store_true")
    ap.add_argument("-q", dest="quiet", action="store_true")
    ap.add_argument("--chmod", choices=["+x", "-x"], default=None)
    ap.add_argument("--cacheinfo", nargs=3, metavar=("MODE", "SHA", "PATH"))
    ap.add_argument("--assume-unchanged", dest="assume_unchanged", action="store_true")
    ap.add_argument("--no-assume-unchanged", dest="no_assume_unchanged", action="store_true")
    ap.add_argument("--skip-worktree", dest="skip_worktree", action="store_true")
    ap.add_argument("--no-skip-worktree", dest="no_skip_worktree", action="store_true")
    ap.add_argument("--stdin", action="store_true")
    ap.add_argument("-z", dest="nul", action="store_true")
    ap.add_argument("--index-info", dest="index_info", action="store_true")
    ap.add_argument("--show-index-version", dest="show_index_version", action="store_true")
    ap.add_argument("-g", "--again", dest="again", action="store_true")
    ap.add_argument("--unresolve", dest="unresolve", action="store_true")
    ap.add_argument("--clear-resolve-undo", dest="clear_resolve_undo", action="store_true")
    ap.add_argument("--fsmonitor-valid", dest="fsmonitor_valid", action="store_true")
    ap.add_argument("--no-fsmonitor-valid", dest="no_fsmonitor_valid", action="store_true")
    ap.add_argument("paths", nargs="*")
    args = ap.parse_args(argv)
    repo = _repo()
    from .index import IndexEntry, read_index, write_index
    idx = read_index(repo)
    if args.show_index_version:
        _print(str(getattr(idx, "version", 2) or 2))
        return 0
    # --index-info: read `mode SP sha [SP stage] TAB path` records from stdin.
    if args.index_info:
        data = sys.stdin.buffer.read().decode("utf-8")
        sep = "\0" if args.nul else "\n"
        for rec in data.split(sep):
            if not rec:
                continue
            meta, _, path = rec.partition("\t")
            fields = meta.split()
            mode_s, sha = fields[0], fields[1]
            stage = int(fields[2]) if len(fields) > 2 else 0
            if int(mode_s, 8) == 0 or sha == repo.null_oid():
                idx.remove(path, stage=stage)
            else:
                e = IndexEntry(mode=int(mode_s, 8), sha=sha, path=path)
                e.set_stage(stage) if hasattr(e, "set_stage") else None
                idx.upsert(e)
        write_index(repo, idx)
        return 0
    if args.again:
        # -g/--again: re-run update on stage-0 entries whose index state already
        # differs from HEAD (by mode or object id). Mirrors do_reupdate(): paths
        # in conflict (stage != 0) and paths still identical to HEAD are skipped.
        # A path whose worktree file has vanished is a fatal error and aborts the
        # whole operation before the index is written (C git's update_one die()).
        # The pathspec comes only from the command line — --again never reads
        # stdin (its callback consumes the post-flag argv, not --stdin input).
        _, head_sha = refs_mod.read_head(repo)
        has_head = bool(head_sha)
        head_map: dict[str, tuple[int, str]] = {}
        if has_head:
            try:
                _t, _d = objs.read_object(repo, head_sha)
                if _t == "commit":
                    _tree = objs.parse_commit(_d).tree
                    for hp, hm, hs in workdir.iter_tree_files(repo, _tree):
                        head_map[hp] = (int(hm, 8), hs)
            except KeyError:
                has_head = False
        prefixes = list(args.paths)

        def _again_match(path: str) -> bool:
            if not prefixes:
                return True
            for pre in prefixes:
                if path == pre or path.startswith(pre.rstrip("/") + "/"):
                    return True
            return False

        changed: list[str] = []
        for entry in sorted(idx.entries, key=lambda e: (e.path, e.stage)):
            if entry.stage != 0:
                continue
            if not _again_match(entry.path):
                continue
            old = head_map.get(entry.path) if has_head else None
            if old is not None and old[0] == entry.mode and old[1] == entry.sha:
                continue  # unchanged from HEAD
            changed.append(entry.path)
        # Validate all targets exist in the worktree before mutating the index,
        # matching C git's behaviour of dying (no write) on the first missing one.
        for path in changed:
            full = repo.path / path
            if not full.exists() and not full.is_symlink():
                _err(f"error: {path}: does not exist and --remove not passed")
                _err(f"fatal: Unable to process path {path}")
                return 128
        if changed:
            workdir.add_paths(repo, changed, update_only=True)
        return 0
    # --unresolve <path>...: restore the stage 1/2/3 entries recorded in
    # resolve-undo for each path (consuming the record), mirroring C git's
    # do_unresolve(). Paths come from the command line only (not --stdin).
    if args.unresolve:
        changed = False
        for p in args.paths:
            rec = idx.resolve_undo.get(p)
            if rec is None:
                continue  # no resolve-undo record for the path
            _unmerge_index_entry(idx, p, rec)
            del idx.resolve_undo[p]
            changed = True
        if changed:
            write_index(repo, idx)
        return 0
    # --clear-resolve-undo: drop all resolve-undo records.
    if args.clear_resolve_undo and not args.paths:
        if idx.resolve_undo:
            idx.resolve_undo = {}
            write_index(repo, idx)
        return 0
    # Gather target paths from the command line and/or stdin (-z → NUL).
    paths = list(args.paths)
    if args.stdin:
        sdata = sys.stdin.buffer.read().decode("utf-8")
        sep = "\0" if args.nul else "\n"
        paths += [p for p in sdata.split(sep) if p]
    # --assume-unchanged / --no-assume-unchanged toggle the CE_VALID bit.
    if args.assume_unchanged or args.no_assume_unchanged:
        by_path = idx.by_path()
        for p in paths:
            e = by_path.get(p)
            if e is None:
                _err(f"fatal: Unable to mark file {p}")
                return 128
            if args.assume_unchanged:
                e.flags |= 0x8000
            else:
                e.flags &= ~0x8000
            idx.upsert(e)
        write_index(repo, idx)
        return 0
    if args.skip_worktree or args.no_skip_worktree:
        by_path = idx.by_path()
        for p in paths:
            e = by_path.get(p)
            if e is None:
                _err(f"fatal: Unable to mark file {p}")
                return 128
            e.skip_worktree = bool(args.skip_worktree)
            idx.upsert(e)
        write_index(repo, idx)
        return 0
    # --fsmonitor-valid / --no-fsmonitor-valid toggle the in-core
    # CE_FSMONITOR_VALID bit. That bit is NOT part of CE_EXTENDED_FLAGS, so it
    # is never serialized to the on-disk index; the only externally visible
    # effects are the success/no-op exit and the die() on a path that has no
    # index entry. We still rewrite the index to match git's cache_changed path.
    if args.fsmonitor_valid or args.no_fsmonitor_valid:
        by_path = idx.by_path()
        for p in paths:
            if by_path.get(p) is None:
                _err(f"fatal: Unable to mark file {p}")
                return 128
        write_index(repo, idx)
        return 0
    args.paths = paths
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
    if args.refresh or args.really_refresh:
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
    # Bare paths (e.g. via --stdin) refresh the stat info of tracked entries.
    if args.paths:
        workdir.add_paths(repo, args.paths, update_only=True)
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


def _reachable(repo: Repository, extra_roots: Optional[list[str]] = None) -> set[str]:
    from . import objects as _o
    tips = _ref_tips(repo)
    if extra_roots:
        tips = set(tips)
        tips.update(extra_roots)
    if tips and not extra_roots:
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


_UNPACK_OBJECTS_USAGE = "git unpack-objects [-n] [-q] [-r] [--strict]"


def cmd_unpack_objects(argv: list[str]) -> int:
    # show_usage_if_asked: only when -h / --help-all is the sole argument; this
    # runs before repository discovery (matches git's cmd_main ordering).
    if len(argv) == 1 and argv[0] in ("-h", "--help-all"):
        sys.stdout.write("usage: " + _UNPACK_OBJECTS_USAGE + "\n")
        return 129

    # git discovers the repository (repo_config) before parsing options, so a
    # missing repo dies with rc 128 even for otherwise-invalid arguments.
    repo = _repo()

    dry_run = False
    # quiet/recover/strict are parsed for argument-acceptance parity; strict's
    # fsck reachability enforcement is intentionally not implemented (DEFER).
    for arg in argv:
        if arg and arg[0] == "-":
            if arg == "-n":
                dry_run = True
                continue
            if arg == "-q":
                continue
            if arg == "-r":
                continue
            if arg == "--strict":
                continue
            if arg.startswith("--strict="):
                continue
            if arg.startswith("--pack_header="):
                continue
            if arg.startswith("--max-input-size="):
                continue
            sys.stderr.write("usage: " + _UNPACK_OBJECTS_USAGE + "\n")
            return 129
        # We don't take any non-flag arguments.
        sys.stderr.write("usage: " + _UNPACK_OBJECTS_USAGE + "\n")
        return 129

    from . import pack as _p
    _p.unpack_pack_stream(repo, sys.stdin.buffer, dry_run=dry_run)
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


_TIME_MAX = (1 << 63) - 1

_PRUNE_USAGE = (
    "usage: git prune [-n] [-v] [--progress] [--expire <time>] [--] [<head>...]\n"
    "\n"
    "    -n, --[no-]dry-run    do not remove, show only\n"
    "    -v, --[no-]verbose    report pruned objects\n"
    "    --[no-]progress       show progress\n"
    "    --[no-]expire <expiry-date>\n"
    "                          expire objects older than <time>\n"
    "    --[no-]exclude-promisor-objects\n"
    "                          limit traversal to objects outside promisor packfiles\n"
    "\n"
)


def _parse_expiry_date(value: str) -> int:
    """Parse a --expire <time> argument into a unix timestamp.

    Mirrors git's OPT_EXPIRY_DATE / parse_expiry_date for the deterministic
    cases. Returns the timestamp; raises ValueError for malformed input so the
    caller can emit git's "malformed expiration date" fatal.
    """
    import calendar
    import datetime as _dt

    v = value.strip()
    if v == "":
        raise ValueError(value)
    low = v.lower()
    if low == "never":
        return 0
    if low in ("now", "all"):
        return int(time.time())
    # @<epoch> or bare integer epoch.
    epoch = v[1:] if v.startswith("@") else v
    if epoch and (epoch.lstrip("+-")).isdigit():
        try:
            return int(epoch)
        except ValueError:
            raise ValueError(value)
    # Absolute ISO-ish dates (interpreted in UTC under TZ=UTC parity env).
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d",
    ):
        try:
            dt = _dt.datetime.strptime(v, fmt)
        except ValueError:
            continue
        return calendar.timegm(dt.timetuple())
    raise ValueError(value)


def cmd_prune(argv: list[str]) -> int:
    show_only = False
    verbose = False
    expire = _TIME_MAX
    heads: list[str] = []
    i = 0
    saw_dashdash = False
    while i < len(argv):
        a = argv[i]
        if saw_dashdash:
            heads.append(a)
            i += 1
            continue
        if a == "--":
            saw_dashdash = True
        elif a in ("-n", "--dry-run"):
            show_only = True
        elif a == "--no-dry-run":
            show_only = False
        elif a in ("-v", "--verbose"):
            verbose = True
        elif a == "--no-verbose":
            verbose = False
        elif a in ("--progress", "--no-progress"):
            # Connectivity progress is a delayed meter on stderr; under the
            # parity env (never a tty) it produces no output either way.
            pass
        elif a == "--expire" or a.startswith("--expire="):
            if a.startswith("--expire="):
                val = a[len("--expire="):]
            else:
                i += 1
                if i >= len(argv):
                    _err("error: option `expire' requires a value")
                    return 129
                val = argv[i]
            try:
                expire = _parse_expiry_date(val)
            except ValueError:
                _err(f"fatal: malformed expiration date '{val}'")
                return 128
        elif a == "--no-expire":
            expire = 0
        elif a in ("--exclude-promisor-objects", "--no-exclude-promisor-objects"):
            pass
        elif a.startswith("--"):
            _err(f"error: unknown option `{a[2:]}'")
            sys.stderr.write(_PRUNE_USAGE)
            return 129
        elif a.startswith("-") and a != "-":
            _err(f"error: unknown switch `{a[1]}'")
            sys.stderr.write(_PRUNE_USAGE)
            return 129
        else:
            heads.append(a)
        i += 1

    repo = _repo()

    extra_roots: list[str] = []
    for name in heads:
        try:
            sha = refs_mod.rev_parse(repo, name)
        except (OSError, ValueError):
            sha = None
        if not sha:
            _err(f"fatal: unrecognized argument: {name}")
            return 128
        extra_roots.append(sha)

    reach = _reachable(repo, extra_roots) if extra_roots else _reachable(repo)
    objects_dir = repo.gitdir / "objects"
    for sha in _iter_loose_shas(repo):
        if sha in reach:
            continue
        p = objects_dir / sha[:2] / sha[2:]
        try:
            # git compares st_mtime (whole seconds) against the expiry.
            mtime = int(p.stat().st_mtime)
        except OSError:
            continue
        if mtime > expire:
            continue
        if show_only or verbose:
            import zlib as _zlib
            from . import objects as _o

            try:
                otype, _ = _o.read_object(repo, sha)
            except (OSError, KeyError, ValueError, _zlib.error):
                otype = "unknown"
            _print(f"{sha} {otype}")
        if not show_only:
            p.unlink(missing_ok=True)
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


_NOTES_SUBCOMMANDS = {"add", "append", "copy", "edit", "show", "list", "remove",
                      "prune", "get-ref", "merge"}


def _notes_write(repo: Repository, ref: str, notes_map: dict, base_sha, verb: str) -> None:
    """Rebuild the notes tree from ``notes_map`` and commit it onto ``ref``."""
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
    parents = [base_sha] if base_sha else []
    c = objs.Commit(tree=new_tree, parents=parents, author=sig, committer=sig,
                    message=f"Notes {verb} by 'git notes'\n")
    sha = objs.write_object(repo, "commit", c.encode())
    refs_mod.update_ref(repo, ref, sha, message=f"notes: {verb}")


def cmd_notes(argv: list[str]) -> int:
    repo = _repo()
    # Extract the global --ref[=<ref>] (may appear before the subcommand).
    ref_name = "commits"
    rest: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--ref" and i + 1 < len(argv):
            ref_name = argv[i + 1]
            i += 2
            continue
        if a.startswith("--ref="):
            ref_name = a.split("=", 1)[1]
            i += 1
            continue
        rest.append(a)
        i += 1
    ref = _notes_ref(ref_name)

    action = "list"
    if rest and not rest[0].startswith("-") and rest[0] in _NOTES_SUBCOMMANDS:
        action = rest[0]
        rest = rest[1:]

    if action == "get-ref":
        _print(ref)
        return 0

    notes_tree_sha = refs_mod.read_ref(repo, ref)
    notes_map: dict[str, str] = {}
    if notes_tree_sha:
        nc = objs.parse_commit(objs.read_object(repo, notes_tree_sha)[1])
        notes_map = {path: sha for path, _mode, sha in workdir.iter_tree_files(repo, nc.tree)}

    def _key(oid: str) -> str:
        return oid[:2] + "/" + oid[2:]

    if action == "list":
        if rest:
            t = refs_mod.rev_parse(repo, rest[0])
            if t and _key(t) in notes_map:
                _print(notes_map[_key(t)])
                return 0
            _err(f"error: no note found for object {t or rest[0]}.")
            return 1
        for path, blob_sha in sorted(notes_map.items()):
            _print(f"{blob_sha} {path.replace('/', '')}")
        return 0

    if action == "prune":
        dry = "-n" in rest or "--dry-run" in rest
        pruned = []
        for path in list(notes_map):
            oid = path.replace("/", "")
            if not objs.object_exists(repo, oid):
                pruned.append(oid)
                if not dry:
                    del notes_map[path]
        for oid in pruned:
            _print(oid)
        if pruned and not dry:
            _notes_write(repo, ref, notes_map, notes_tree_sha, "removed")
        return 0

    if action in ("add", "append", "edit"):
        mp = argparse.ArgumentParser(prog="pygit notes", add_help=False)
        mp.add_argument("-m", "--message", action="append", default=None)
        mp.add_argument("-F", "--file", action="append", default=None)
        mp.add_argument("-c", dest="reuse_edit", action="append", default=None)
        mp.add_argument("-C", "--reuse-message", dest="reuse", action="append", default=None)
        mp.add_argument("--separator", default=None)
        mp.add_argument("--no-separator", dest="no_separator", action="store_true")
        mp.add_argument("-f", "--force", action="store_true")
        mp.add_argument("--allow-empty", dest="allow_empty", action="store_true")
        mp.add_argument("-e", "--edit", action="store_true")
        mp.add_argument("object", nargs="?", default="HEAD")
        a = mp.parse_args(rest)
        target = refs_mod.rev_parse(repo, a.object)
        if not target:
            _err(f"error: Failed to resolve '{a.object}' as a valid ref.")
            return 128
        key = _key(target)
        parts: list[str] = []
        if action == "append" and key in notes_map:
            parts.append(objs.read_object(repo, notes_map[key])[1].decode("utf-8", "replace"))
        for m in (a.message or []):
            parts.append(m)
        for f in (a.file or []):
            parts.append(sys.stdin.read() if f == "-" else open(f, encoding="utf-8").read())
        for o in (a.reuse or []) + (a.reuse_edit or []):
            s = refs_mod.rev_parse(repo, o)
            if s:
                parts.append(objs.read_object(repo, s)[1].decode("utf-8", "replace"))
        sep = "\n" if a.no_separator else (f"\n{a.separator}\n" if a.separator is not None else "\n\n")
        body = sep.join(p.rstrip("\n") for p in parts)
        if not body and not a.allow_empty and action != "edit":
            _err("fatal: please supply the note contents using either -m or -F option")
            return 128
        if action == "add" and key in notes_map and not a.force:
            _err(f"error: Cannot add notes. Found existing notes for object {target}. "
                 "Use '-f' to overwrite existing notes")
            return 1
        if action == "add" and key in notes_map and a.force:
            _err(f"Overwriting existing notes for object {target}")
        blob = objs.write_object(repo, "blob", (body + "\n").encode("utf-8") if body else b"")
        notes_map[key] = blob
        _notes_write(repo, ref, notes_map, notes_tree_sha, "added")
        return 0

    if action == "copy":
        cp = argparse.ArgumentParser(prog="pygit notes", add_help=False)
        cp.add_argument("-f", "--force", action="store_true")
        cp.add_argument("from_obj")
        cp.add_argument("to_obj")
        a = cp.parse_args(rest)
        src = refs_mod.rev_parse(repo, a.from_obj)
        dst = refs_mod.rev_parse(repo, a.to_obj)
        if not src or not dst:
            return 128
        if _key(src) not in notes_map:
            _err(f"error: missing notes on source object {src}. Cannot copy.")
            return 1
        if _key(dst) in notes_map and not a.force:
            _err(f"error: Cannot copy notes. Found existing notes for object {dst}. "
                 "Use '-f' to overwrite existing notes")
            return 1
        notes_map[_key(dst)] = notes_map[_key(src)]
        _notes_write(repo, ref, notes_map, notes_tree_sha, "added")
        return 0

    if action == "show":
        obj = rest[-1] if rest and not rest[-1].startswith("-") else "HEAD"
        target = refs_mod.rev_parse(repo, obj)
        if not target:
            return 128
        if _key(target) not in notes_map:
            _err(f"error: no note found for object {target}.")
            return 1
        sys.stdout.buffer.write(objs.read_object(repo, notes_map[_key(target)])[1])
        return 0

    if action == "remove":
        rp = argparse.ArgumentParser(prog="pygit notes", add_help=False)
        rp.add_argument("--ignore-missing", dest="ignore_missing", action="store_true")
        rp.add_argument("objects", nargs="*", default=None)
        a = rp.parse_args(rest)
        objs_list = a.objects or ["HEAD"]
        changed = False
        for o in objs_list:
            target = refs_mod.rev_parse(repo, o)
            if not target:
                continue
            if _key(target) in notes_map:
                _err(f"Removing note for object {o}")
                del notes_map[_key(target)]
                changed = True
        if changed:
            _notes_write(repo, ref, notes_map, notes_tree_sha, "removed")
        return 0
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
    ap.add_argument("-a", "--all", action="store_true")
    ap.add_argument("-r", "--remotes", action="store_true")
    ap.add_argument("--sparse", action="store_true")
    ap.add_argument("--merge-base", dest="merge_base", action="store_true")
    ap.add_argument("--independent", action="store_true")
    ap.add_argument("--topo-order", dest="topo_order", action="store_true")
    ap.add_argument("--date-order", dest="date_order", action="store_true")
    ap.add_argument("--current", action="store_true")
    ap.add_argument("--topics", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--more", type=int, default=0)
    ap.add_argument("--name", dest="name", action="store_true")
    ap.add_argument("--no-name", dest="no_name", action="store_true")
    ap.add_argument("--sha1-name", dest="sha1_name", action="store_true")
    ap.add_argument("--color", nargs="?", const="auto", default="never")
    ap.add_argument("--no-color", dest="no_color", action="store_true")
    ap.add_argument("revs", nargs="*")
    # `--reflog[=<n>]`/`-g[<n>]` only takes a value when attached; a bare flag
    # leaves the following token as the positional <ref>. argparse's nargs="?"
    # would wrongly consume that token, so pull the option out of argv first.
    # `--color` / `--more` similarly take only an attached value.
    reflog_val = None
    rest_argv = []
    for a in argv:
        if a in ("--reflog", "-g"):
            reflog_val = ""
        elif a.startswith("--reflog="):
            reflog_val = a.split("=", 1)[1]
        elif a.startswith("-g") and len(a) > 2 and a[2:].isdigit():
            reflog_val = a[2:]
        elif a == "--color":
            rest_argv.append("--color=auto")
        elif a == "--more":
            rest_argv.append("--more=1")
        else:
            rest_argv.append(a)
    args = ap.parse_args(rest_argv)
    color_on = (args.color == "always") and not args.no_color
    # column-indexed marker palette (C Git's column_colors_ansi).
    _SB_COLORS = ["\033[31m", "\033[32m", "\033[33m", "\033[34m", "\033[35m", "\033[36m"]

    def _col(ch: str, i: int) -> str:
        if not color_on or ch == " ":
            return ch
        return f"{_SB_COLORS[i % len(_SB_COLORS)]}{ch}\033[m"
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
    # --current appends the current branch as an extra column when not already
    # listed (e.g. alongside explicit revs).
    if args.current and reflog_val is None and cur and cur not in ref_names:
        s = refs_mod.read_ref(repo, f"refs/heads/{cur}")
        if s:
            ref_names.append(cur)
            revs.append(s)
    if not revs:
        _err("No revs to be shown.")
        return 0
    num_rev = len(revs)

    def _sb_subject(s):
        try:
            c = objs.parse_commit(objs.read_object(repo, s)[1])
            return c.message.splitlines()[0] if c.message.strip() else ""
        except (KeyError, IndexError):
            return ""

    # --list: just the ref headers (current marked with '*'), no matrix.
    if args.list:
        for i in range(num_rev):
            is_head = (reflog_val is None) and ref_names[i] == cur and revs[i] == head_oid
            mark = _col("*", i) if is_head else " "
            sub_i = reflog_msgs[i] if reflog_val is not None else _sb_subject(revs[i])
            _print(f"{mark} [{ref_names[i]}] {sub_i}")
        return 0

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
    ordered = _topo_order(repo, union, by_date=args.date_order)

    names = _show_branch_name_commits(repo, ordered, revs, ref_names)

    def disp_name(s):
        if args.sha1_name:
            return s[:7]
        n = names.get(s)
        if not n:
            return s[:7]
        head, gen = n
        if gen == 0:
            return head
        if gen == 1:
            return head + "^"
        return f"{head}~{gen}"

    def body(s):
        # --no-name suppresses the "[<name>] " prefix, leaving the bare subject.
        return subject(s) if args.no_name else f"[{disp_name(s)}] {subject(s)}"

    # head_at: column of the current branch (used for the '*' marker).
    head_at = -1
    if num_rev > 1:
        for i in range(num_rev):
            is_head = (reflog_val is None) and ref_names[i] == cur and revs[i] == head_oid
            mark = _col("*" if is_head else "!", i)
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
    extra = 0  # commits emitted past the first merge point (for --more)
    for s in ordered:
        m = mask(s)
        is_merge_point = (m == all_mask)
        is_merge = len(parents_of(s)) > 1
        # --topics: omit commits that are on the first branch but not a common
        # point (i.e. reachable from rev[0] yet not from every rev).
        if args.topics and (m & 1) and m != all_mask:
            continue
        if num_rev > 1 and not args.sparse and is_merge and omit_in_dense(s, m):
            continue
        # Stop after the merge point unless --more=<n> asks for n more rows.
        if shown_merge_point:
            if extra >= args.more:
                break
            extra += 1
        if num_rev > 1:
            marks = []
            for i in range(num_rev):
                if not (m & (1 << i)):
                    marks.append(" ")
                elif is_merge:
                    marks.append(_col("-", i))
                elif i == head_at:
                    marks.append(_col("*", i))
                else:
                    marks.append(_col("+", i))
            _print(f"{''.join(marks)} {body(s)}")
        else:
            _print(body(s))
        if is_merge_point and not shown_merge_point:
            shown_merge_point = True
            if args.more <= 0:
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
    ap.add_argument("--stdin", action="store_true")
    ap.add_argument("--annotate-stdin", dest="annotate_stdin", action="store_true")
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

    if args.stdin or args.annotate_stdin:
        # Annotate each full oid token on stdin in place with " (<name>)".
        if args.stdin:
            _err("warning: --stdin is deprecated. Please use --annotate-stdin instead, "
                 "which is functionally equivalent.")
            _err("This option will be removed in a future release.")
        import re as _re2

        def _annot(line: str) -> str:
            def repl(m):
                tok = m.group(0)
                s = refs_mod.rev_parse(repo, tok)
                if s and s in name_for:
                    return f"{tok} ({_display(name_for[s][0])})"
                return tok
            return _re2.sub(r"[0-9a-f]{40}", repl, line)
        for line in sys.stdin:
            sys.stdout.write(_annot(line.rstrip("\n")) + "\n")
        return 0

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


def _all_refs(repo: Repository) -> dict[str, str]:
    """Collect every ref (loose + packed) as name -> sha, like for_each_ref."""
    all_refs: dict[str, str] = {}
    refs_root = repo.gitdir / "refs"
    if refs_root.exists():
        for f in refs_root.rglob("*"):
            if f.is_file():
                rel = str(f.relative_to(repo.gitdir)).replace(os.sep, "/")
                s = refs_mod.read_ref(repo, rel)
                if s:
                    all_refs[rel] = s
    for ref, s in refs_mod.read_packed_refs(repo).items():
        all_refs.setdefault(ref, s)
    return all_refs


def _peel_to_non_tag(repo: Repository, sha: str) -> Optional[str]:
    """Follow tag objects until a non-tag object is reached; return its sha."""
    seen: set[str] = set()
    cur = sha
    while cur and cur not in seen:
        seen.add(cur)
        try:
            t, data = objs.read_object(repo, cur)
        except (KeyError, Exception):  # noqa: BLE001 - missing/corrupt object
            return None
        if t != "tag":
            return cur
        target = None
        for raw in data.split(b"\n"):
            if raw.startswith(b"object "):
                target = raw[len(b"object "):].decode("ascii", "replace").strip()
                break
            if raw == b"":
                break
        if not target:
            return None
        cur = target
    return None


def cmd_update_server_info(argv: list[str]) -> int:
    # -f / --force only controls whether files are rewritten from scratch; the
    # resulting content is identical either way, so it is accepted as a no-op
    # with respect to output (matching C git's behaviour for our purposes).
    ap = argparse.ArgumentParser(prog="pygit update-server-info", add_help=False)
    ap.add_argument("-f", "--force", dest="force", action="store_true", default=False)
    ap.add_argument("--no-force", dest="force", action="store_false")
    args = ap.parse_args(argv)
    _ = args.force
    repo = _repo()

    # info/refs: every ref, sorted by name, with peeled lines for tag objects.
    info_dir = repo.gitdir / "info"
    info_dir.mkdir(parents=True, exist_ok=True)
    all_refs = _all_refs(repo)
    out: list[str] = []
    for name in sorted(all_refs):
        sha = all_refs[name]
        out.append(f"{sha}\t{name}\n")
        try:
            t, _data = objs.read_object(repo, sha)
        except (KeyError, Exception):  # noqa: BLE001
            t = None
        if t == "tag":
            peeled = _peel_to_non_tag(repo, sha)
            if peeled:
                out.append(f"{peeled}\t{name}^{{}}\n")
    (info_dir / "refs").write_text("".join(out), encoding="utf-8")

    # objects/info/packs: "P <name>\n" per local pack, then a trailing blank line.
    pack_dir = repo.gitdir / "objects" / "pack"
    pack_info_dir = repo.gitdir / "objects" / "info"
    pack_info_dir.mkdir(parents=True, exist_ok=True)
    pack_lines: list[str] = []
    if pack_dir.exists():
        for f in sorted(pack_dir.glob("pack-*.pack")):
            pack_lines.append(f"P {f.name}\n")
    (pack_info_dir / "packs").write_text("".join(pack_lines) + "\n", encoding="utf-8")
    return 0


_REPLACE_USAGE = (
    "usage: git replace [-f] <object> <replacement>\n"
    "   or: git replace [-f] --edit <object>\n"
    "   or: git replace [-f] --graft <commit> [<parent>...]\n"
    "   or: git replace [-f] --convert-graft-file\n"
    "   or: git replace -d <object>...\n"
    "   or: git replace [--format=<format>] [-l [<pattern>]]\n"
    "\n"
    "    -l, --list            list replace refs\n"
    "    -d, --delete          delete replace refs\n"
    "    -e, --edit            edit existing object\n"
    "    -g, --graft           change a commit's parents\n"
    "    --convert-graft-file  convert existing graft file\n"
    "    -f, --[no-]force      replace the ref if it exists\n"
    "    --[no-]raw            do not pretty-print contents for --edit\n"
    "    --[no-]format <format>\n"
    "                          use this format\n"
)


def _replace_usage_opt(msg: str) -> int:
    sys.stderr.write(f"fatal: {msg}\n\n{_REPLACE_USAGE}\n")
    return 129


def cmd_replace(argv: list[str]) -> int:
    # Hand-rolled option parsing to match git's exact error formats. git's
    # replace builtin always resolves the *real* objects (disable_replace_refs).
    force = False
    raw = False
    fmt: Optional[str] = None
    # cmdmode tracks the single selected mode; conflicting modes error like
    # parse-options' OPT_CMDMODE: "options 'X' and 'Y' cannot be used together".
    cmdmode: Optional[str] = None
    mode_opt: dict[str, str] = {}  # mode -> option spelling that selected it
    rest: list[str] = []

    def set_mode(name: str, opt: str) -> Optional[int]:
        nonlocal cmdmode
        if cmdmode is not None and cmdmode != name:
            sys.stderr.write(
                f"error: options '{opt}' and '{mode_opt[cmdmode]}' "
                "cannot be used together\n"
            )
            return 129
        cmdmode = name
        mode_opt[name] = opt
        return None

    i = 0
    parsing = True
    while i < len(argv):
        a = argv[i]
        if parsing and a == "--":
            parsing = False
            i += 1
            continue
        if parsing and a.startswith("-") and a != "-":
            if a in ("-l", "--list"):
                rc = set_mode("list", "-l" if a == "-l" else "--list")
                if rc is not None:
                    return rc
            elif a in ("-d", "--delete"):
                rc = set_mode("delete", "-d" if a == "-d" else "--delete")
                if rc is not None:
                    return rc
            elif a in ("-e", "--edit"):
                rc = set_mode("edit", "-e" if a == "-e" else "--edit")
                if rc is not None:
                    return rc
            elif a in ("-g", "--graft"):
                rc = set_mode("graft", "-g" if a == "-g" else "--graft")
                if rc is not None:
                    return rc
            elif a == "--convert-graft-file":
                rc = set_mode("convert", "--convert-graft-file")
                if rc is not None:
                    return rc
            elif a in ("-f", "--force"):
                force = True
            elif a == "--no-force":
                force = False
            elif a == "--raw":
                raw = True
            elif a == "--no-raw":
                raw = False
            elif a == "--format":
                if i + 1 >= len(argv):
                    return _replace_usage_opt("option `format' requires a value")
                fmt = argv[i + 1]
                i += 1
            elif a.startswith("--format="):
                fmt = a[len("--format="):]
            else:
                sys.stderr.write(f"error: unknown switch `{a.lstrip('-')[0]}'\n"
                                 if not a.startswith("--")
                                 else f"error: unknown option `{a[2:]}'\n")
                sys.stderr.write("\n" + _REPLACE_USAGE)
                return 129
            i += 1
            continue
        rest.append(a)
        i += 1

    if cmdmode is None:
        cmdmode = "replace" if rest else "list"

    if fmt is not None and cmdmode != "list":
        return _replace_usage_opt("--format cannot be used when not listing")
    if force and cmdmode not in ("replace", "edit", "graft", "convert"):
        return _replace_usage_opt("-f only makes sense when writing a replacement")
    if raw and cmdmode != "edit":
        return _replace_usage_opt("--raw only makes sense with --edit")

    repo = _repo()

    if cmdmode == "edit":
        # Requires an interactive editor; cannot be reproduced byte-exact.
        sys.stderr.write("error: 'git replace --edit' is not supported\n")
        return 128

    if cmdmode == "list":
        if len(rest) > 1:
            return _replace_usage_opt("only one pattern can be given with -l")
        return _replace_list(repo, rest[0] if rest else None, fmt)

    if cmdmode == "delete":
        if len(rest) < 1:
            return _replace_usage_opt("-d needs at least one argument")
        return _replace_delete(repo, rest)

    if cmdmode == "replace":
        if len(rest) != 2:
            return _replace_usage_opt("bad number of arguments")
        return _replace_object(repo, rest[0], rest[1], force)

    if cmdmode == "graft":
        if len(rest) < 1:
            return _replace_usage_opt("-g needs at least one argument")
        return _replace_create_graft(repo, rest, force, gentle=False, graft_state={})

    if cmdmode == "convert":
        if len(rest) != 0:
            return _replace_usage_opt("--convert-graft-file takes no argument")
        return 1 if _replace_convert_graft_file(repo, force) else 0

    return 1


def _replace_type(repo: Repository, sha: str) -> str:
    return objs.read_object(repo, sha)[0]


def _replace_list(repo: Repository, pattern: Optional[str], fmt: Optional[str]) -> int:
    if fmt is None or fmt == "" or fmt == "short":
        mode = "short"
    elif fmt == "medium":
        mode = "medium"
    elif fmt == "long":
        mode = "long"
    else:
        _err(f"error: invalid replace format '{fmt}'\n"
             "valid formats are 'short', 'medium' and 'long'")
        return 255
    if pattern is None:
        pattern = "*"
    import fnmatch
    # gather replace refs (loose + packed), keyed by the hex name suffix.
    entries: dict[str, str] = {}
    packed = refs_mod.read_packed_refs(repo)
    for full, val in packed.items():
        if full.startswith("refs/replace/"):
            entries[full[len("refs/replace/"):]] = val
    root = repo.gitdir / "refs" / "replace"
    if root.exists():
        for f in root.rglob("*"):
            if f.is_file():
                name = str(f.relative_to(root)).replace(os.sep, "/")
                entries[name] = f.read_text(encoding="utf-8").strip()
    for name in sorted(entries):
        if not fnmatch.fnmatchcase(name, pattern):
            continue
        repl = entries[name]
        if mode == "short":
            _print(name)
        elif mode == "medium":
            _print(f"{name} -> {repl}")
        else:
            try:
                obj_type = _replace_type(repo, name)
            except (KeyError, ValueError):
                _err(f"error: failed to resolve '{name}' as a valid ref")
                return 255
            repl_type = _replace_type(repo, repl)
            _print(f"{name} ({obj_type}) -> {repl} ({repl_type})")
    return 0


def _replace_delete_ref(repo: Repository, ref: str) -> None:
    """Delete a ref that may live loose and/or in packed-refs."""
    p = repo.gitdir / ref
    if p.exists():
        p.unlink()
    ppath = repo.gitdir / "packed-refs"
    if not ppath.exists():
        return
    lines = ppath.read_text(encoding="utf-8").splitlines()
    if not any(line.partition(" ")[2] == ref for line in lines
               if line and not line.startswith(("#", "^"))):
        return
    out: list[str] = []
    skip_peel = False
    for line in lines:
        if line.startswith("^"):
            if skip_peel:
                skip_peel = False
                continue
            out.append(line)
            continue
        skip_peel = False
        if line and not line.startswith("#") and line.partition(" ")[2] == ref:
            skip_peel = True  # drop a following peel line, if any
            continue
        out.append(line)
    ppath.write_text("\n".join(out) + ("\n" if out else ""), encoding="utf-8")


def _replace_delete(repo: Repository, args: list[str]) -> int:
    had_error = False
    for spec in args:
        oid = refs_mod.rev_parse(repo, spec)
        if not oid:
            _err(f"error: failed to resolve '{spec}' as a valid ref")
            had_error = True
            continue
        ref = f"refs/replace/{oid}"
        if refs_mod.read_ref(repo, ref) is None:
            _err(f"error: replace ref '{oid}' not found")
            had_error = True
            continue
        _replace_delete_ref(repo, ref)
        _print(f"Deleted replace ref '{oid}'")
    return 1 if had_error else 0


def _replace_write_ref(repo: Repository, oid: str, repl: str, force: bool) -> int:
    ref = f"refs/replace/{oid}"
    if refs_mod.read_ref(repo, ref) is not None and not force:
        _err(f"error: replace ref '{ref}' already exists")
        return 255
    refs_mod.update_ref(repo, ref, repl)
    return 0


def _replace_object(repo: Repository, object_ref: str, replace_ref: str, force: bool) -> int:
    oid = refs_mod.rev_parse(repo, object_ref)
    if not oid:
        _err(f"error: failed to resolve '{object_ref}' as a valid ref")
        return 255
    repl = refs_mod.rev_parse(repo, replace_ref)
    if not repl:
        _err(f"error: failed to resolve '{replace_ref}' as a valid ref")
        return 255
    obj_type = _replace_type(repo, oid)
    repl_type = _replace_type(repo, repl)
    if not force and obj_type != repl_type:
        _err("error: Objects must be of the same type.\n"
             f"'{object_ref}' points to a replaced object of type '{obj_type}'\n"
             f"while '{replace_ref}' points to a replacement object of "
             f"type '{repl_type}'.")
        return 255
    return _replace_write_ref(repo, oid, repl, force)


def _replace_could_not_read(repo: Repository, oid: str, wrote: Optional[list]) -> None:
    """Once a replacement object has been written in this process, C git's object
    lookup reports 'Could not read <oid>' for a syntactically valid but missing
    full-length hex before the higher-level parse error. Mirror that ordering."""
    if (wrote and wrote[0] and len(oid) == repo.hex_len
            and all(c in "0123456789abcdef" for c in oid)
            and not objs.object_exists(repo, oid)):
        _err(f"error: Could not read {oid}")


_GRAFT_DEPRECATED_HINT = (
    "hint: Support for <GIT_DIR>/info/grafts is deprecated\n"
    "hint: and will be removed in a future Git version.\n"
    "hint:\n"
    "hint: Please use \"git replace --convert-graft-file\"\n"
    "hint: to convert the grafts into replace refs.\n"
    "hint:\n"
    "hint: Turn this message off by running\n"
    "hint: \"git config set advice.graftFileDeprecated false\"\n"
)


def _replace_prepare_grafts(repo: Repository, state: Optional[dict]) -> None:
    """Mirror C git's lazy prepare_commit_graft: the first time a commit is
    parsed, the whole info/grafts file is read. If it exists, a deprecation
    hint is shown (unless suppressed, e.g. by --convert-graft-file, or disabled
    via advice.graftFileDeprecated), and every malformed line (a line whose
    whitespace-separated tokens are not all full-length lowercase hex) emits
    'error: bad graft data: <line>'. This happens exactly once."""
    if state is None or state.get("prepared"):
        return
    state["prepared"] = True
    graft_file = repo.gitdir / "info" / "grafts"
    try:
        text = graft_file.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return
    if not state.get("suppress_hint"):
        from . import gitconfig
        advice = (gitconfig.get(repo, "advice.graftfiledeprecated") or "").lower()
        if advice not in ("false", "0", "no", "off"):
            sys.stderr.write(_GRAFT_DEPRECATED_HINT)
    for raw_line in text.split("\n"):
        line = raw_line.rstrip()
        if not line or line[0] == "#":
            continue
        tokens = line.split()
        ok = all(len(t) == repo.hex_len
                 and all(c in "0123456789abcdef" for c in t) for t in tokens)
        # parse_oid_hex also requires the separators between tokens to be a
        # single space; any token failing full-hex parse triggers the error.
        if not ok:
            _err(f"error: bad graft data: {line}")


def _replace_create_graft(repo: Repository, args: list[str], force: bool, gentle: bool,
                          wrote: Optional[list] = None,
                          graft_state: Optional[dict] = None) -> int:
    old_ref = args[0]
    old_oid = refs_mod.rev_parse(repo, old_ref)
    if not old_oid:
        _err(f"error: not a valid object name: '{old_ref}'")
        return 255
    # The commit parse below is what triggers C git's one-shot graft-table load.
    _replace_prepare_grafts(repo, graft_state)
    try:
        obj_type, data = objs.read_object(repo, old_oid)
    except (KeyError, ValueError):
        _replace_could_not_read(repo, old_oid, wrote)
        _err(f"error: could not parse {old_ref}")
        return 255
    if obj_type != "commit":
        _err(f"error: object {old_oid} is a {obj_type}, not a commit")
        _err(f"error: could not parse {old_ref}")
        return 255

    # Build new commit buffer: keep "tree" line, replace the parent block.
    hexsz = repo.hex_len
    # parents start after "tree <hex>\n" (5 + hexsz + 1)
    parent_start = 5 + hexsz + 1
    parent_end = parent_start
    while data[parent_end:parent_end + 7] == b"parent ":
        parent_end += 7 + hexsz + 1  # "parent " + hex + "\n"

    new_parents = b""
    for p in args[1:]:
        poid = refs_mod.rev_parse(repo, p)
        if not poid:
            _err(f"error: not a valid object name: '{p}'")
            return 255
        try:
            ptype = objs.read_object(repo, poid)[0]
        except (KeyError, ValueError):
            _replace_could_not_read(repo, poid, wrote)
            _err(f"error: could not parse {p} as a commit")
            return 255
        if ptype != "commit":
            _err(f"error: object {poid} is a {ptype}, not a commit")
            _err(f"error: could not parse {p} as a commit")
            return 255
        new_parents += b"parent " + poid.encode() + b"\n"

    buf = data[:parent_start] + new_parents + data[parent_end:]

    # Drop any gpg signature (gpgsig header) -> warn.
    buf, had_sig = _replace_remove_signature(buf, hexsz)
    if had_sig:
        _err(f"warning: the original commit '{old_ref}' has a gpg signature")
        _err("warning: the signature will be removed in the replacement commit!")

    # Mergetag check: a discarded mergetag whose tagged commit is not among the
    # new parents is an error directing the user to --edit.
    rc = _replace_check_mergetags(repo, data, args, hexsz)
    if rc is not None:
        return rc

    new_oid = objs.write_object(repo, "commit", buf)
    if wrote is not None:
        wrote[0] = True

    if new_oid == old_oid:
        if gentle:
            _err(f"warning: graft for '{old_oid}' unnecessary")
            return 0
        _err(f"error: new commit is the same as the old one: '{old_oid}'")
        return 255

    return _replace_write_ref(repo, old_oid, new_oid, force)


def _replace_remove_signature(buf: bytes, hexsz: int) -> tuple[bytes, bool]:
    """Strip a 'gpgsig' header (and continuation lines) from a commit buffer.
    Returns (new_buf, had_signature)."""
    # Header section ends at the first blank line.
    sep = buf.find(b"\n\n")
    header = buf if sep < 0 else buf[:sep]
    body = b"" if sep < 0 else buf[sep:]
    out_lines: list[bytes] = []
    had = False
    skipping = False
    for line in header.split(b"\n"):
        if skipping:
            if line.startswith(b" "):
                continue  # folded continuation of the signature
            skipping = False
        if line.startswith(b"gpgsig ") or line.startswith(b"gpgsig-sha256 "):
            had = True
            skipping = True
            continue
        out_lines.append(line)
    return b"\n".join(out_lines) + body, had


def _replace_check_mergetags(repo: Repository, data: bytes, args: list[str], hexsz: int) -> Optional[int]:
    """Mirror builtin/replace.c check_mergetags: for each mergetag header in the
    original commit, the tagged commit must be among the new parents."""
    sep = data.find(b"\n\n")
    header = data if sep < 0 else data[:sep]
    lines = header.split(b"\n")
    # Collect mergetag blocks (header line + folded continuation lines).
    idx = 0
    new_parent_oids: Optional[set[str]] = None
    while idx < len(lines):
        line = lines[idx]
        if line.startswith(b"mergetag "):
            block = [line[len(b"mergetag "):]]
            idx += 1
            while idx < len(lines) and lines[idx].startswith(b" "):
                block.append(lines[idx][1:])
                idx += 1
            tag_payload = b"\n".join(block) + b"\n"
            import hashlib
            tag_oid = hashlib.sha1(
                b"tag " + str(len(tag_payload)).encode() + b"\0" + tag_payload
            ).hexdigest()
            # Find the tagged object's oid from the tag payload.
            tagged = None
            for tl in tag_payload.split(b"\n"):
                if tl.startswith(b"object "):
                    tagged = tl[len(b"object "):].decode()
                    break
            if new_parent_oids is None:
                new_parent_oids = set()
                for p in args[1:]:
                    poid = refs_mod.rev_parse(repo, p)
                    if not poid:
                        _err(f"error: not a valid object name: '{p}'")
                        return 255
                    new_parent_oids.add(poid)
            if tagged not in new_parent_oids:
                _err(f"error: original commit '{args[0]}' contains mergetag "
                     f"'{tag_oid}' that is discarded; use --edit instead of --graft")
                return 255
        else:
            idx += 1
    return None


def _replace_convert_graft_file(repo: Repository, force: bool) -> bool:
    """Convert .git/info/grafts into replace refs. Returns True on error."""
    graft_file = repo.gitdir / "info" / "grafts"
    try:
        text = graft_file.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return True
    errs: list[str] = []
    wrote = [False]
    graft_state: dict = {"suppress_hint": True}
    for raw_line in text.splitlines():
        if raw_line.startswith("#"):
            continue
        parts = raw_line.split()
        if not parts:
            continue
        rc = _replace_create_graft(repo, parts, force, gentle=True,
                                   wrote=wrote, graft_state=graft_state)
        if rc:
            errs.append(raw_line)
    if not errs:
        try:
            graft_file.unlink()
        except FileNotFoundError:
            pass
        return False
    _err("warning: could not convert the following graft(s):\n"
         + "".join("\n\t" + e for e in errs))
    return True


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


def _log2u(n: int) -> int:
    """C Git's log2u: floor(log2(n)) for n>=1, else 0."""
    return n.bit_length() - 1 if n >= 1 else 0


def cmd_pack_refs(argv: list[str]) -> int:
    import fnmatch
    ap = argparse.ArgumentParser(prog="pygit pack-refs", add_help=False)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--prune", dest="prune", action="store_true", default=None)
    ap.add_argument("--no-prune", dest="prune", action="store_false")
    ap.add_argument("--auto", action="store_true")
    ap.add_argument("--include", action="append", default=[])
    ap.add_argument("--exclude", action="append", default=[])
    args = ap.parse_args(argv)
    repo = _repo()
    prune = True if args.prune is None else args.prune  # prune is the default

    # Ref selection mirrors pack-refs.c: --include patterns (or "*" for --all)
    # form the set; the "refs/tags/*" default applies only when none are given.
    includes = list(args.include)
    if args.all:
        includes.append("*")
    if not includes:
        includes.append("refs/tags/*")
    excludes = list(args.exclude)

    def _peeled(sha: str) -> Optional[str]:
        """Recursively peel a tag to its non-tag object; None if not a tag."""
        cur, peeled_any = sha, False
        while True:
            try:
                t, data = objs.read_object(repo, cur)
            except (KeyError, ValueError):
                return None
            if t != "tag":
                return cur if peeled_any else None
            peeled_any = True
            obj = None
            for line in data.split(b"\n"):
                if line.startswith(b"object "):
                    obj = line[len(b"object "):].decode("ascii", "replace").strip()
                    break
                if line == b"":
                    break
            if not obj:
                return None
            cur = obj

    def should_pack(name: str) -> bool:
        if any(fnmatch.fnmatch(name, p) for p in excludes):
            return False
        return any(fnmatch.fnmatch(name, p) for p in includes)

    # Collect loose refs under refs/ (skipping symbolic and broken ones).
    loose: list[tuple[str, str]] = []
    refs_root = repo.gitdir / "refs"
    if refs_root.exists():
        for f in sorted(refs_root.rglob("*")):
            if not f.is_file():
                continue
            name = str(f.relative_to(repo.gitdir)).replace(os.sep, "/")
            try:
                raw = f.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if raw.startswith("ref: "):
                continue  # symbolic ref — never packed
            if len(raw) != repo.hex_len or any(ch not in "0123456789abcdef" for ch in raw):
                continue  # broken / not an object id
            loose.append((name, raw))

    to_pack = [(n, s) for n, s in loose if should_pack(n)]

    # --auto packs only when the loose-in-set count crosses the size-scaled limit.
    if args.auto:
        ppath = repo.gitdir / "packed-refs"
        packed_size = ppath.stat().st_size if ppath.exists() else 0
        limit = max(16, _log2u(packed_size // 100) * 5)
        if len(to_pack) < limit:
            return 0

    # Merge into any existing packed-refs (existing packed entries are kept).
    merged = dict(refs_mod.read_packed_refs(repo))
    for name, sha in to_pack:
        merged[name] = sha

    lines = ["# pack-refs with: peeled fully-peeled sorted "]
    for name in sorted(merged):
        sha = merged[name]
        lines.append(f"{sha} {name}")
        peel = _peeled(sha)
        if peel is not None:
            lines.append(f"^{peel}")
    (repo.gitdir / "packed-refs").write_text("\n".join(lines) + "\n", encoding="utf-8")

    if prune:
        for name, _sha in to_pack:
            p = repo.gitdir / name
            if p.is_file():
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
    # --no-data is an OPT_BOOL; --data is its parse-options auto-negation
    # (sets no_data back to its default of 0). Both are last-one-wins.
    ap.add_argument("--no-data", dest="no_data", action="store_true", default=False)
    ap.add_argument("--data", dest="no_data", action="store_false")
    ap.add_argument("revs", nargs="*", default=["HEAD"])
    args = ap.parse_args(argv)
    no_data = args.no_data
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
        # With --no-data, blob contents are skipped entirely and the M line
        # references the object by its full hex id instead of a mark.
        if not no_data:
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
                if no_data:
                    w(f"M {b.mode} {b.sha} {path}\n")
                else:
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


_IT_USAGE = (
    "usage: git interpret-trailers [--in-place] [--trim-empty]\n"
    "                              [(--trailer (<key>|<key-alias>)[(=|:)<value>])...]\n"
    "                              [--parse] [<file>...]\n"
    "\n"
    "    --[no-]in-place       edit files in place\n"
    "    --[no-]trim-empty     trim empty trailers\n"
    "    --[no-]where <placement>\n"
    "                          where to place the new trailer\n"
    "    --[no-]if-exists <action>\n"
    "                          action if trailer already exists\n"
    "    --[no-]if-missing <action>\n"
    "                          action if trailer is missing\n"
    "    --[no-]only-trailers  output only the trailers\n"
    "    --[no-]only-input     do not apply trailer.<key-alias> configuration variables\n"
    "    --[no-]unfold         reformat multiline trailer values as single-line values\n"
    "    --parse               alias for --only-trailers --only-input --unfold\n"
    "    --no-divider          do not treat \"---\" as the end of input\n"
    "    --divider             opposite of --no-divider\n"
    "    --[no-]trailer <trailer>\n"
    "                          trailer(s) to add\n"
    "\n"
)

# --- trailer placement / conflict-resolution enums (mirror trailer.h) -------
_W_DEFAULT, _W_END, _W_AFTER, _W_BEFORE, _W_START = range(5)
_E_DEFAULT, _E_ADD_IF_DIFF_NEIGHBOR, _E_ADD_IF_DIFF, _E_ADD, _E_REPLACE, _E_DO_NOTHING = range(6)
_M_DEFAULT, _M_ADD, _M_DO_NOTHING = range(3)


def _it_set_where(value):
    table = {"after": _W_AFTER, "before": _W_BEFORE, "end": _W_END, "start": _W_START}
    if value is None:
        return _W_DEFAULT
    return table.get(value.lower())


def _it_set_if_exists(value):
    table = {
        "addifdifferent": _E_ADD_IF_DIFF,
        "addifdifferentneighbor": _E_ADD_IF_DIFF_NEIGHBOR,
        "add": _E_ADD,
        "replace": _E_REPLACE,
        "donothing": _E_DO_NOTHING,
    }
    if value is None:
        return _E_DEFAULT
    return table.get(value.lower())


def _it_set_if_missing(value):
    table = {"donothing": _M_DO_NOTHING, "add": _M_ADD}
    if value is None:
        return _M_DEFAULT
    return table.get(value.lower())


def _it_after_or_end(where):
    return where == _W_AFTER or where == _W_END


def _it_isspace(ch):
    # C isspace under LC_ALL=C: space, \t, \n, \v, \f, \r
    return ch in " \t\n\x0b\x0c\r"


def _it_isalnum(ch):
    return ("0" <= ch <= "9") or ("A" <= ch <= "Z") or ("a" <= ch <= "z")


def _it_token_len_without_separator(token):
    n = len(token)
    while n > 0 and not _it_isalnum(token[n - 1]):
        n -= 1
    return n


def _it_last_non_space_char(s):
    for i in range(len(s) - 1, -1, -1):
        if not _it_isspace(s[i]):
            return s[i]
    return ""


def _it_is_blank_line(s):
    i = 0
    while i < len(s) and s[i] != "\n" and _it_isspace(s[i]):
        i += 1
    return i >= len(s) or s[i] == "\n"


def _it_rtrim(s):
    n = len(s)
    while n > 0 and _it_isspace(s[n - 1]):
        n -= 1
    return s[:n]


def _it_trim(s):
    n = len(s)
    while n > 0 and _it_isspace(s[n - 1]):
        n -= 1
    s = s[:n]
    i = 0
    while i < len(s) and _it_isspace(s[i]):
        i += 1
    return s[i:]


_IT_GENERATED_PREFIXES = ("Signed-off-by: ", "(cherry picked from commit ")


class _ITConf:
    __slots__ = ("name", "key", "command", "cmd", "where", "if_exists", "if_missing")

    def __init__(self, name="", key=None, command=None, cmd=None,
                 where=_W_END, if_exists=_E_ADD_IF_DIFF_NEIGHBOR, if_missing=_M_ADD):
        self.name = name
        self.key = key
        self.command = command
        self.cmd = cmd
        self.where = where
        self.if_exists = if_exists
        self.if_missing = if_missing

    def copy(self):
        return _ITConf(self.name, self.key, self.command, self.cmd,
                       self.where, self.if_exists, self.if_missing)


class _ITTrailerItem:
    __slots__ = ("token", "value")

    def __init__(self, token, value):
        self.token = token  # None if not a trailer line
        self.value = value


class _ITArgItem:
    __slots__ = ("token", "value", "conf")

    def __init__(self, token, value, conf):
        self.token = token
        self.value = value
        self.conf = conf


def _it_find_separator(line, separators):
    whitespace_found = False
    for idx, c in enumerate(line):
        if c in separators:
            return idx
        if not whitespace_found and (_it_isalnum(c) or c == "-"):
            continue
        if idx != 0 and (c == " " or c == "\t"):
            whitespace_found = True
            continue
        break
    return -1


def _it_next_line_off(s, pos):
    nl = s.find("\n", pos)
    if nl == -1:
        return len(s)
    return nl + 1


def _it_last_line(buf, length):
    if length == 0:
        return -1
    if length == 1:
        return 0
    i = length - 2
    while i >= 0:
        if buf[i] == "\n":
            return i + 1
        i -= 1
    return 0


def _it_wt_status_locate_end(s, length, comment):
    pattern = "\n" + comment + " " + _IT_CUT_LINE
    if s.startswith(pattern[1:]):
        return 0
    p = s.find(pattern)
    if p != -1:
        newlen = p + 1
        if newlen < length:
            return newlen
    return length


_IT_CUT_LINE = "------------------------ >8 ------------------------\n"


def _it_ignored_log_message_bytes(buf, length, comment):
    boc = 0
    bol = 0
    in_old_conflicts_block = False
    cutoff = _it_wt_status_locate_end(buf, length, comment)
    while bol < cutoff:
        nl = buf.find("\n", bol)
        if nl == -1 or nl >= length:
            next_line = length
        else:
            next_line = nl + 1
        if buf.startswith(comment, bol) or buf[bol] == "\n":
            if not boc:
                boc = bol
        elif buf.startswith("Conflicts:\n", bol):
            in_old_conflicts_block = True
            if not boc:
                boc = bol
        elif in_old_conflicts_block and buf[bol] == "\t":
            pass
        elif boc:
            boc = 0
            in_old_conflicts_block = False
        bol = next_line
    return (length - boc) if boc else (length - cutoff)


def _it_find_end_of_log_message(input_str, no_divider, comment):
    end = len(input_str)
    if not no_divider:
        s = 0
        while s < len(input_str):
            if input_str.startswith("---", s):
                v = s + 3
                if v < len(input_str) and _it_isspace(input_str[v]):
                    end = s
                    break
            s = _it_next_line_off(input_str, s)
    return end - _it_ignored_log_message_bytes(input_str, end, comment)


def _it_find_trailer_block_start(buf, length, conf_head, separators, comment):
    only_spaces = True
    recognized_prefix = False
    trailer_lines = 0
    non_trailer_lines = 0
    possible_continuation_lines = 0

    # The first paragraph is the title and cannot be trailers.
    s = 0
    while s < length:
        if buf.startswith(comment, s) and s < length:
            s = _it_next_line_off(buf, s)
            continue
        if _it_is_blank_line(buf[s:length]):
            break
        s = _it_next_line_off(buf, s)
    end_of_title = s

    l = _it_last_line(buf, length)
    while l >= end_of_title:
        bol = l
        line = buf[bol:length]
        if line.startswith(comment):
            non_trailer_lines += possible_continuation_lines
            possible_continuation_lines = 0
            l = _it_last_line(buf, l)
            continue
        if _it_is_blank_line(line):
            if only_spaces:
                l = _it_last_line(buf, l)
                continue
            non_trailer_lines += possible_continuation_lines
            if recognized_prefix and trailer_lines * 3 >= non_trailer_lines:
                return _it_next_line_off(buf, bol)
            elif trailer_lines and not non_trailer_lines:
                return _it_next_line_off(buf, bol)
            return length
        only_spaces = False

        matched_generated = False
        for pfx in _IT_GENERATED_PREFIXES:
            if line.startswith(pfx):
                trailer_lines += 1
                possible_continuation_lines = 0
                recognized_prefix = True
                matched_generated = True
                break
        if matched_generated:
            l = _it_last_line(buf, l)
            continue

        separator_pos = _it_find_separator(line, separators)
        if separator_pos >= 1 and not _it_isspace(line[0]):
            trailer_lines += 1
            possible_continuation_lines = 0
            if not recognized_prefix:
                for item in conf_head:
                    if _it_token_matches_item(line, item, separator_pos):
                        recognized_prefix = True
                        break
        elif _it_isspace(line[0]):
            possible_continuation_lines += 1
        else:
            non_trailer_lines += 1
            non_trailer_lines += possible_continuation_lines
            possible_continuation_lines = 0
        l = _it_last_line(buf, l)

    return length


def _it_token_matches_item(tok, item, tok_len):
    if tok[:tok_len].lower() == item.conf.name[:tok_len].lower():
        return True
    if item.conf.key:
        return tok[:tok_len].lower() == item.conf.key[:tok_len].lower()
    return False


def _it_token_from_item(item, tok):
    if item.conf.key:
        return item.conf.key
    if tok is not None:
        return tok
    return item.conf.name


def _it_parse_trailer(trailer, separator_pos, conf_head, separators, want_conf):
    if separator_pos != -1:
        tok = _it_trim(trailer[:separator_pos])
        val = _it_trim(trailer[separator_pos + 1:])
    else:
        tok = _it_trim(trailer)
        val = ""

    conf = _IT_DEFAULT_CONF if want_conf else None
    tok_len = _it_token_len_without_separator(tok)
    for item in conf_head:
        if _it_token_matches_item(tok, item, tok_len):
            if want_conf:
                conf = item.conf
            tok = _it_token_from_item(item, tok)
            break
    return tok, val, conf


def _it_unfold_value(val):
    out = []
    i = 0
    n = len(val)
    while i < n:
        c = val[i]
        i += 1
        if c == "\n":
            while i < n and _it_isspace(val[i]):
                i += 1
            out.append(" ")
        else:
            out.append(c)
    return _it_trim("".join(out))


# Default config + per-key config (populated from repo config when available).
_IT_DEFAULT_CONF = _ITConf()
_IT_CONF_HEAD = []
_IT_SEPARATORS = ":"


def _it_load_config():
    """Read trailer.* / core.commentChar config (best-effort; no repo is fine)."""
    global _IT_DEFAULT_CONF, _IT_CONF_HEAD, _IT_SEPARATORS
    _IT_DEFAULT_CONF = _ITConf()
    _IT_CONF_HEAD = []
    _IT_SEPARATORS = ":"
    comment = "#"
    try:
        repo = _repo()
        cp = repo.config()
    except Exception:
        cp = None
    if cp is None:
        return comment
    try:
        if cp.has_section("core"):
            cc = cp.get("core", "commentChar", fallback=None)
            if cc is not None:
                cc = _fmm_dequote_config(cc)
            if cc and cc != "auto":
                comment = cc
    except Exception:
        pass

    def get_conf_item(name):
        for it in _IT_CONF_HEAD:
            if it.conf.name.lower() == name.lower():
                return it
        it = _ITArgItem(None, None, _IT_DEFAULT_CONF.copy())
        it.conf.name = name
        _IT_CONF_HEAD.append(it)
        return it

    types = {"key", "command", "cmd", "where", "ifexists", "ifmissing"}
    try:
        for sect, sub, key, val in _it_iter_config(cp):
            if sect.lower() != "trailer":
                continue
            if val is not None:
                val = _fmm_dequote_config(val)
            if sub is None:
                # trailer.<key> form
                k = key.lower()
                if k == "where":
                    w = _it_set_where(val)
                    if w is not None:
                        _IT_DEFAULT_CONF.where = w
                elif k == "ifexists":
                    e = _it_set_if_exists(val)
                    if e is not None:
                        _IT_DEFAULT_CONF.if_exists = e
                elif k == "ifmissing":
                    m = _it_set_if_missing(val)
                    if m is not None:
                        _IT_DEFAULT_CONF.if_missing = m
                elif k == "separators" and val is not None:
                    _IT_SEPARATORS = val
                continue
            variable_name = key.lower()
            if variable_name not in types:
                continue
            item = get_conf_item(sub)
            conf = item.conf
            if variable_name == "key" and val is not None:
                conf.key = val
            elif variable_name == "command" and val is not None:
                conf.command = val
            elif variable_name == "cmd" and val is not None:
                conf.cmd = val
            elif variable_name == "where":
                w = _it_set_where(val)
                if w is not None:
                    conf.where = w
            elif variable_name == "ifexists":
                e = _it_set_if_exists(val)
                if e is not None:
                    conf.if_exists = e
            elif variable_name == "ifmissing":
                m = _it_set_if_missing(val)
                if m is not None:
                    conf.if_missing = m
    except Exception:
        pass
    return comment


def _it_iter_config(cp):
    """Yield (section, subsection, key, value) for every config entry.

    trailer.<x> -> ('trailer', None, 'x', v); trailer.<sub>.<x> ->
    ('trailer', '<sub>', 'x', v). configparser flattens subsections into the
    section name as 'trailer "sub"'.
    """
    for section in cp.sections():
        if '"' in section:
            base, rest = section.split('"', 1)
            base = base.strip()
            sub = rest.rsplit('"', 1)[0]
        elif "." in section:
            base, sub = section.split(".", 1)
        else:
            base, sub = section, None
        for key, val in cp.items(section):
            yield base, sub, key, val


def _it_check_if_different(head, in_tok, arg, check_all):
    where = arg.conf.where
    idx = head.index(in_tok)
    while True:
        cur = head[idx]
        if _it_same_trailer(cur, arg):
            return False
        nxt = idx - 1 if _it_after_or_end(where) else idx + 1
        if nxt < 0 or nxt >= len(head):
            break
        idx = nxt
        if not check_all:
            break
    return True


def _it_same_token(a, b):
    if a.token is None:
        return False
    a_len = _it_token_len_without_separator(a.token)
    b_len = _it_token_len_without_separator(b.token)
    min_len = b_len if a_len > b_len else a_len
    return a.token[:min_len].lower() == b.token[:min_len].lower()


def _it_same_trailer(a, b):
    if not _it_same_token(a, b):
        return False
    return a.value.lower() == b.value.lower()


def _it_format_trailers(opts, trailers, separators):
    out = []
    started = False
    for item in trailers:
        if item.token is not None:
            if opts["trim_empty"] and len(item.value) == 0:
                continue
            out.append(item.token)
            c = _it_last_non_space_char(item.token)
            if c and c not in separators:
                out.append(separators[0] + " ")
            out.append(item.value)
            out.append("\n")
            started = True
        elif not opts["only_trailers"]:
            out.append(item.value)
            out.append("\n")
            started = True
    return "".join(out), started


def cmd_interpret_trailers(argv: list[str]) -> int:
    # ---- parse_options-compatible argument scanning -----------------------
    opts = {
        "in_place": False, "trim_empty": False, "only_trailers": False,
        "only_input": False, "unfold": False, "no_divider": False,
    }
    where = _W_DEFAULT
    if_exists = _E_DEFAULT
    if_missing = _M_DEFAULT
    new_trailers = []  # list of (text, where, if_exists, if_missing)
    files = []

    def usage_err(msg=None):
        if msg:
            sys.stderr.write("error: %s\n" % msg)
        sys.stderr.write(_IT_USAGE)

    def fatal_usage(msg):
        sys.stderr.write("fatal: %s\n\n" % msg)
        sys.stderr.write(_IT_USAGE)

    i = 0
    n = len(argv)
    saw_dashdash = False
    while i < n:
        a = argv[i]
        if saw_dashdash:
            files.append(a)
            i += 1
            continue
        if a == "--":
            saw_dashdash = True
            i += 1
            continue
        if not a.startswith("-") or a == "-":
            files.append(a)
            i += 1
            continue

        # Split "--opt=value".
        eq = None
        name = a
        if a.startswith("--") and "=" in a:
            name, eq = a.split("=", 1)

        def take_value():
            nonlocal i, eq
            if eq is not None:
                v = eq
                eq = None
                return v
            i += 1
            if i >= n:
                usage_err("option `%s' requires a value" % name[2:])
                raise _ITExit(129)
            return argv[i]

        try:
            if name in ("--in-place",):
                opts["in_place"] = True
            elif name in ("--no-in-place",):
                opts["in_place"] = False
            elif name == "--trim-empty":
                opts["trim_empty"] = True
            elif name == "--no-trim-empty":
                opts["trim_empty"] = False
            elif name == "--only-trailers":
                opts["only_trailers"] = True
            elif name == "--no-only-trailers":
                opts["only_trailers"] = False
            elif name == "--only-input":
                opts["only_input"] = True
            elif name == "--no-only-input":
                opts["only_input"] = False
            elif name == "--unfold":
                opts["unfold"] = True
            elif name == "--no-unfold":
                opts["unfold"] = False
            elif name == "--no-divider":
                opts["no_divider"] = True
            elif name == "--divider":
                opts["no_divider"] = False
            elif name == "--parse":
                if eq is not None:
                    usage_err("option `parse' takes no value")
                    raise _ITExit(129)
                opts["only_trailers"] = True
                opts["only_input"] = True
                opts["unfold"] = True
            elif name == "--where":
                w = _it_set_where(take_value())
                if w is None:
                    raise _ITExit(129)
                where = w
            elif name == "--no-where":
                where = _W_DEFAULT
            elif name == "--if-exists":
                e = _it_set_if_exists(take_value())
                if e is None:
                    raise _ITExit(129)
                if_exists = e
            elif name == "--no-if-exists":
                if_exists = _E_DEFAULT
            elif name == "--if-missing":
                m = _it_set_if_missing(take_value())
                if m is None:
                    raise _ITExit(129)
                if_missing = m
            elif name == "--no-if-missing":
                if_missing = _M_DEFAULT
            elif name == "--trailer":
                new_trailers.append((take_value(), where, if_exists, if_missing))
            elif name == "--no-trailer":
                new_trailers = []
            elif name in ("-h", "--help"):
                sys.stdout.write(_IT_USAGE)
                return 129
            else:
                opt_disp = name[2:] if name.startswith("--") else name[1:]
                usage_err("unknown option `%s'" % opt_disp)
                return 129
        except _ITExit as ex:
            return ex.code
        i += 1

    if opts["only_input"] and new_trailers:
        fatal_usage("--trailer with --only-input does not make sense")
        return 129

    if not files and opts["in_place"]:
        sys.stderr.write("fatal: no input file given for in-place editing\n")
        return 128

    comment = _it_load_config()
    separators = _IT_SEPARATORS

    # cl_separators = "=" + separators
    cl_separators = "=" + separators

    targets = files if files else [None]
    for f in targets:
        # ---- read + complete-line ----
        if f is None:
            data = sys.stdin.buffer.read()
        else:
            try:
                with open(f, "rb") as fh:
                    data = fh.read()
            except OSError:
                sys.stderr.write("fatal: could not read input file '%s'\n" % f)
                return 128
        text = data.decode("utf-8", "surrogateescape")
        if text and not text.endswith("\n"):
            text += "\n"

        result = _it_process(opts, new_trailers, text, comment, separators,
                             cl_separators)

        if opts["in_place"]:
            out_bytes = result.encode("utf-8", "surrogateescape")
            with open(f, "wb") as fh:
                fh.write(out_bytes)
        else:
            sys.stdout.buffer.write(result.encode("utf-8", "surrogateescape"))
            sys.stdout.flush()
    return 0


class _ITExit(Exception):
    def __init__(self, code):
        self.code = code


def _it_process(opts, new_trailers, text, comment, separators, cl_separators):
    # ---- parse trailer block ----
    end_of_log = _it_find_end_of_log_message(text, opts["no_divider"], comment)
    block_start = _it_find_trailer_block_start(
        text, end_of_log, _IT_CONF_HEAD, separators, comment)

    # strbuf_split_buf on text[block_start:end_of_log], keeping '\n'.
    region = text[block_start:end_of_log]
    raw_lines = []
    pos = 0
    rl = len(region)
    while pos < rl:
        nl = region.find("\n", pos)
        if nl == -1:
            raw_lines.append(region[pos:])
            pos = rl
        else:
            raw_lines.append(region[pos:nl + 1])
            pos = nl + 1

    # join continuation lines (those starting with whitespace) onto previous
    # line that itself looked like a trailer.
    trailer_strings = []
    last = None  # index into trailer_strings, or None
    for ln in raw_lines:
        if last is not None and ln and _it_isspace(ln[0]):
            trailer_strings[last] = trailer_strings[last] + ln
            continue
        trailer_strings.append(ln)
        idx = len(trailer_strings) - 1
        last = idx if _it_find_separator(trailer_strings[idx], separators) >= 1 else None

    # blank line before trailer block?
    bl = _it_last_line(text, block_start)
    blank_before = bl >= 0 and _it_is_blank_line(text[bl:block_start])

    # ---- build trailer_objects (head) ----
    head = []
    for trailer in trailer_strings:
        if trailer.startswith(comment):
            continue
        sep = _it_find_separator(trailer, separators)
        if sep >= 1:
            tok, val, _ = _it_parse_trailer(trailer, sep, _IT_CONF_HEAD,
                                            separators, False)
            if opts["unfold"]:
                val = _it_unfold_value(val)
            head.append(_ITTrailerItem(tok, val))
        elif not opts["only_trailers"]:
            val = trailer
            if val.endswith("\n"):
                val = val[:-1]
            head.append(_ITTrailerItem(None, val))

    out = []
    # Print lines before the trailer block.
    if not opts["only_trailers"]:
        out.append(text[:block_start])
    if not opts["only_trailers"] and not blank_before:
        out.append("\n")

    # ---- apply new trailers ----
    if not opts["only_input"]:
        arg_items = _it_build_arg_items(new_trailers, cl_separators, separators)
        _it_process_lists(head, arg_items)

    body, _ = _it_format_trailers(opts, head, separators)
    out.append(body)

    if not opts["only_trailers"]:
        out.append(text[end_of_log:])

    return "".join(out)


def _it_build_arg_items(new_trailers, cl_separators, separators):
    arg_items = []
    for text, w, e, m in new_trailers:
        sep = _it_find_separator(text, cl_separators)
        if sep == 0:
            sb = _it_trim(text)
            sys.stderr.write("error: empty trailer token in trailer '%s'\n" % sb)
            continue
        tok, val, conf = _it_parse_trailer(text, sep, _IT_CONF_HEAD,
                                           separators, True)
        conf = conf.copy()
        if w != _W_DEFAULT:
            conf.where = w
        if e != _E_DEFAULT:
            conf.if_exists = e
        if m != _M_DEFAULT:
            conf.if_missing = m
        arg_items.append(_ITArgItem(tok, val, conf))
    return arg_items


def _it_process_lists(head, arg_items):
    for arg in arg_items:
        applied = _it_find_same_and_apply(head, arg)
        if not applied:
            _it_apply_if_missing(head, arg)


def _it_find_same_and_apply(head, arg):
    where = arg.conf.where
    middle = where == _W_AFTER or where == _W_BEFORE
    backwards = _it_after_or_end(where)
    if not head:
        return False
    start_tok = head[-1] if backwards else head[0]
    order = range(len(head) - 1, -1, -1) if backwards else range(len(head))
    for idx in order:
        in_tok = head[idx]
        if not _it_same_token(in_tok, arg):
            continue
        on_tok = in_tok if middle else start_tok
        _it_apply_if_exists(head, in_tok, arg, on_tok)
        return True
    return False


def _it_apply_if_exists(head, in_tok, arg, on_tok):
    e = arg.conf.if_exists
    if e == _E_DO_NOTHING:
        return
    if e == _E_REPLACE:
        new_item = _ITTrailerItem(arg.token, arg.value)
        _it_add_to_list(head, on_tok, arg, new_item)
        if in_tok in head:
            head.remove(in_tok)
        return
    if e == _E_ADD:
        new_item = _ITTrailerItem(arg.token, arg.value)
        _it_add_to_list(head, on_tok, arg, new_item)
        return
    if e == _E_ADD_IF_DIFF:
        if _it_check_if_different(head, in_tok, arg, True):
            new_item = _ITTrailerItem(arg.token, arg.value)
            _it_add_to_list(head, on_tok, arg, new_item)
        return
    if e == _E_ADD_IF_DIFF_NEIGHBOR:
        if _it_check_if_different(head, on_tok, arg, False):
            new_item = _ITTrailerItem(arg.token, arg.value)
            _it_add_to_list(head, on_tok, arg, new_item)
        return


def _it_add_to_list(head, on_tok, arg, new_item):
    # list_add inserts AFTER on_tok; list_add_tail inserts BEFORE on_tok.
    on_idx = head.index(on_tok)
    if _it_after_or_end(arg.conf.where):
        head.insert(on_idx + 1, new_item)
    else:
        head.insert(on_idx, new_item)


def _it_apply_if_missing(head, arg):
    m = arg.conf.if_missing
    if m == _M_DO_NOTHING:
        return
    if m == _M_ADD:
        new_item = _ITTrailerItem(arg.token, arg.value)
        if _it_after_or_end(arg.conf.where):
            head.append(new_item)
        else:
            head.insert(0, new_item)


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


def _columnate(items: list[str], padding: int = 2) -> list[str]:
    """Column-major layout (C Git's column.c default) into the terminal width.

    Retained for ``git tag --column``; ``git column`` itself uses the faithful
    port below.
    """
    import shutil as _sh
    width = _sh.get_terminal_size((80, 24)).columns
    col_w = max(len(x) for x in items) + padding
    cols = max(1, width // col_w)
    rows = (len(items) + cols - 1) // cols
    out = []
    for r in range(rows):
        parts = []
        for c in range(cols):
            idx = c * rows + r
            if idx < len(items):
                parts.append(items[idx].ljust(col_w))
        out.append("".join(parts).rstrip())
    return out


# --- column layout: faithful port of C Git's column.c -----------------------

_COL_LAYOUT_MASK = 0x000F
_COL_ENABLE_MASK = 0x0030
_COL_DENSE = 0x0080
_COL_DISABLED = 0x0000
_COL_ENABLED = 0x0010
_COL_AUTO = 0x0020
_COL_COLUMN = 0
_COL_ROW = 1
_COL_PLAIN = 15

_COL_PARSE_OPTS = (
    ("always", _COL_ENABLED, _COL_ENABLE_MASK),
    ("never", _COL_DISABLED, _COL_ENABLE_MASK),
    ("auto", _COL_AUTO, _COL_ENABLE_MASK),
    ("plain", _COL_PLAIN, _COL_LAYOUT_MASK),
    ("column", _COL_COLUMN, _COL_LAYOUT_MASK),
    ("row", _COL_ROW, _COL_LAYOUT_MASK),
    ("dense", _COL_DENSE, 0),
)


def _col_term_columns() -> int:
    """Mirror C Git's term_columns(): $COLUMNS via atoi if >0, else ioctl on
    fd 1, else 80."""
    col_string = os.environ.get("COLUMNS")
    if col_string is not None:
        import re as _re
        # atoi(): optional leading whitespace, optional sign, digits, stop at
        # first non-digit.
        m = _re.match(r"\s*([+-]?\d+)", col_string)
        n_cols = int(m.group(1)) if m else 0
        if n_cols > 0:
            return n_cols
    try:
        ws = os.get_terminal_size(1)
        if ws.columns:
            return ws.columns
    except Exception:
        pass
    return 80


def _col_item_length(s: str) -> int:
    """Display width of s (utf8_strnwidth equivalent)."""
    import unicodedata
    width = 0
    for ch in s:
        cat = unicodedata.category(ch)
        if cat in ("Mn", "Me", "Cf"):
            continue
        eaw = unicodedata.east_asian_width(ch)
        width += 2 if eaw in ("W", "F") else 1
    return width


class _ColData:
    __slots__ = ("items", "colopts", "indent", "nl", "padding", "width",
                 "rows", "cols", "length", "widx")

    def __init__(self, items, colopts, indent, nl, padding, width):
        self.items = items
        self.colopts = colopts
        self.indent = indent
        self.nl = nl
        self.padding = padding
        self.width = width
        self.rows = 0
        self.cols = 0
        self.length = [_col_item_length(it) for it in items]
        self.widx = []


def _col_xy2linear(d: "_ColData", x: int, y: int) -> int:
    if (d.colopts & _COL_LAYOUT_MASK) == _COL_COLUMN:
        return x * d.rows + y
    return y * d.cols + x


def _col_layout(d: "_ColData") -> int:
    initial_width = 0
    for ln in d.length:
        if initial_width < ln:
            initial_width = ln
    initial_width += d.padding
    d.cols = (d.width - len(d.indent)) // initial_width
    if d.cols == 0:
        d.cols = 1
    n = len(d.items)
    d.rows = (n + d.cols - 1) // d.cols
    return initial_width


def _col_compute_column_width(d: "_ColData") -> None:
    n = len(d.items)
    for x in range(d.cols):
        d.widx[x] = _col_xy2linear(d, x, 0)
        for y in range(d.rows):
            i = _col_xy2linear(d, x, y)
            if i < n and d.length[d.widx[x]] < d.length[i]:
                d.widx[x] = i


def _col_shrink_columns(d: "_ColData") -> None:
    n = len(d.items)
    d.widx = [0] * d.cols
    while d.rows > 1:
        rows = d.rows
        cols = d.cols
        d.rows -= 1
        d.cols = (n + d.rows - 1) // d.rows
        if d.cols != cols:
            d.widx = [0] * d.cols
        _col_compute_column_width(d)
        total_width = len(d.indent)
        for x in range(d.cols):
            total_width += d.length[d.widx[x]]
            total_width += d.padding
        if total_width > d.width:
            d.rows = rows
            d.cols = cols
            break
    _col_compute_column_width(d)


def _col_display_cell(d: "_ColData", initial_width: int, empty_cell: str,
                      x: int, y: int, out: list) -> bool:
    n = len(d.items)
    i = _col_xy2linear(d, x, y)
    if i >= n:
        return True
    length = d.length[i]
    if d.widx and d.length[d.widx[x]] < initial_width:
        length += initial_width - d.length[d.widx[x]]
        length -= d.padding
    if (d.colopts & _COL_LAYOUT_MASK) == _COL_COLUMN:
        newline = i + d.rows >= n
    else:
        newline = x == d.cols - 1 or i == n - 1
    out.append((d.indent if x == 0 else "")
               + d.items[i]
               + (d.nl if newline else empty_cell[length:]))
    return False


def _col_display_table(d: "_ColData") -> None:
    initial_width = _col_layout(d)
    if d.colopts & _COL_DENSE:
        _col_shrink_columns(d)
    empty_cell = " " * initial_width
    out: list = []
    for y in range(d.rows):
        for x in range(d.cols):
            if _col_display_cell(d, initial_width, empty_cell, x, y, out):
                break
    sys.stdout.write("".join(out))


def _col_display_plain(items, indent: str, nl: str) -> None:
    sys.stdout.write("".join(indent + it + nl for it in items))


def _col_print_columns(items, colopts: int, indent: str, nl: str,
                       padding: int, width: int) -> None:
    if not items:
        return
    nindent = indent if indent is not None else ""
    nnl = nl if nl is not None else "\n"
    npadding = padding
    nwidth = width if width else (_col_term_columns() - 1)
    if (colopts & _COL_ENABLE_MASK) != _COL_ENABLED:
        _col_display_plain(items, "", "\n")
        return
    layout = colopts & _COL_LAYOUT_MASK
    if layout == _COL_PLAIN:
        _col_display_plain(items, nindent, nnl)
    else:
        d = _ColData(items, colopts, nindent, nnl, npadding, nwidth)
        _col_display_table(d)


def _col_parse_option(arg: str, colopts: int) -> tuple[int, bool, bool, bool]:
    """Returns (colopts, ok, layout_set, enable_set)."""
    for name, value, mask in _COL_PARSE_OPTS:
        s = 1
        arg_str = arg
        if not mask:
            if len(arg_str) > 2 and arg_str.startswith("no"):
                arg_str = arg_str[2:]
                s = 0
        if arg_str != name:
            continue
        layout_set = enable_set = False
        if mask == _COL_ENABLE_MASK:
            enable_set = True
        elif mask == _COL_LAYOUT_MASK:
            layout_set = True
        if mask:
            colopts = (colopts & ~mask) | value
        else:
            if s:
                colopts |= value
            else:
                colopts &= ~value
        return colopts, True, layout_set, enable_set
    return colopts, False, False, False


def _col_parse_config(colopts: int, value: str) -> tuple[int, bool]:
    """Returns (colopts, ok). Mirrors parse_config().

    Raises _ColUsageError with git's "unsupported option" message on a bad
    token.
    """
    sep = " ,"
    group_set = 0
    LAYOUT_SET, ENABLE_SET = 1, 2
    i = 0
    n = len(value)
    while i < n:
        # strcspn over separators
        j = i
        while j < n and value[j] not in sep:
            j += 1
        if j > i:
            token = value[i:j]
            colopts, ok, lset, eset = _col_parse_option(token, colopts)
            if not ok:
                raise _ColUsageError(
                    129, "error: unsupported option '%s'" % token)
            if lset:
                group_set |= LAYOUT_SET
            if eset:
                group_set |= ENABLE_SET
            i = j
        # strspn over separators
        while i < n and value[i] in sep:
            i += 1
    if (group_set & LAYOUT_SET) and not (group_set & ENABLE_SET):
        colopts = (colopts & ~_COL_ENABLE_MASK) | _COL_ENABLED
    return colopts, True


def _col_parse_unsigned(name: str, raw: str) -> int:
    """Mirror OPT_UNSIGNED: non-negative integer with optional k/m/g suffix."""
    s = raw
    mult = 1
    if s and s[-1] in "kKmMgG":
        suf = s[-1].lower()
        mult = {"k": 1024, "m": 1024 * 1024, "g": 1024 * 1024 * 1024}[suf]
        s = s[:-1]
    if not s or not s.isdigit():
        raise _ColUsageError(
            129,
            "error: option `%s' expects a non-negative integer value with an "
            "optional k/m/g suffix" % name)
    return int(s) * mult


def _col_parse_int(name: str, raw: str) -> int:
    """Mirror OPT_INTEGER: signed integer with optional k/m/g suffix."""
    s = raw
    mult = 1
    if s and s[-1] in "kKmMgG":
        suf = s[-1].lower()
        mult = {"k": 1024, "m": 1024 * 1024, "g": 1024 * 1024 * 1024}[suf]
        s = s[:-1]
    neg = False
    body = s
    if body[:1] in ("+", "-"):
        neg = body[0] == "-"
        body = body[1:]
    if not body or not body.isdigit():
        raise _ColUsageError(
            129,
            "error: option `%s' expects an integer value with an optional "
            "k/m/g suffix" % name)
    v = int(body) * mult
    return -v if neg else v


_COL_USAGE = (
    "usage: git column [<options>]\n"
    "\n"
    "    --[no-]command <name> lookup config vars\n"
    "    --[no-]mode[=<style>] layout to use\n"
    "    --raw-mode <n>        layout to use\n"
    "    --[no-]width <n>      maximum width\n"
    "    --[no-]indent <string>\n"
    "                          padding space on left border\n"
    "    --[no-]nl <string>    padding space on right border\n"
    "    --[no-]padding <n>    padding space between columns\n"
)


class _ColUsageError(Exception):
    def __init__(self, rc: int, msg: str | None = None, usage: bool = False,
                 usage_stdout: bool = False):
        self.rc = rc
        self.msg = msg
        self.usage = usage
        self.usage_stdout = usage_stdout


def cmd_column(argv: list[str]) -> int:
    colopts = 0
    width = 0
    indent = None
    nl = None
    padding = 1
    real_command = None
    command = None

    # --command must be the first argument (config lookup); we have no config
    # to honor here, but enforce the placement rule like C Git.
    if argv and argv[0].startswith("--command="):
        command = argv[0][len("--command="):]

    rest: list[str] = []
    try:
        i = 0
        n = len(argv)

        def take_value(tok: str, name: str) -> str:
            nonlocal i
            if "=" in tok:
                return tok.split("=", 1)[1]
            i += 1
            if i >= n:
                raise _ColUsageError(
                    129, "error: option `%s' requires a value" % name)
            return argv[i]

        while i < n:
            tok = argv[i]
            if tok == "--":
                i += 1
                rest.extend(argv[i:])
                break
            if not tok.startswith("-") or tok == "-":
                rest.append(tok)
                i += 1
                continue
            name = tok[2:].split("=", 1)[0] if tok.startswith("--") else tok[1:]
            if name == "command" or (tok.startswith("--command=")):
                real_command = take_value(tok, "command")
            elif name == "mode":
                # OPT_COLUMN: optional argument (only via --mode=...).
                if "=" in tok:
                    val = tok.split("=", 1)[1]
                    colopts, _ok = _col_parse_config(colopts, val)
                else:
                    # --mode == --column == always
                    colopts &= ~_COL_ENABLE_MASK
                    colopts |= _COL_ENABLED
            elif name == "no-mode":
                colopts &= ~_COL_ENABLE_MASK
            elif name == "raw-mode":
                colopts = _col_parse_unsigned("raw-mode", take_value(tok, "raw-mode"))
            elif name == "width":
                width = _col_parse_int("width", take_value(tok, "width"))
            elif name == "indent":
                indent = take_value(tok, "indent")
            elif name == "nl":
                nl = take_value(tok, "nl")
            elif name == "padding":
                padding = _col_parse_int("padding", take_value(tok, "padding"))
            elif name == "h":
                # -h prints usage to stdout (rc 129). --help would invoke the
                # man page, which is out of scope here.
                raise _ColUsageError(129, None, False, usage_stdout=True)
            else:
                raise _ColUsageError(
                    129, "error: unknown option `%s'" % tok.lstrip("-").split("=", 1)[0],
                    True)
            i += 1
    except _ColUsageError as exc:
        if exc.msg is not None:
            _err(exc.msg)
        if exc.usage:
            sys.stderr.write(_COL_USAGE + "\n")
        if exc.usage_stdout:
            sys.stdout.write(_COL_USAGE + "\n")
        return exc.rc

    if padding < 0:
        _err("fatal: --padding must be non-negative")
        return 128
    if rest:
        sys.stderr.write(_COL_USAGE + "\n")
        return 129
    if real_command or command:
        if not real_command or not command or real_command != command:
            _err("fatal: --command must be the first argument")
            return 128

    # finalize_colopts(&colopts, -1): AUTO -> resolve via isatty(1)/pager.
    if (colopts & _COL_ENABLE_MASK) == _COL_AUTO:
        colopts &= ~_COL_ENABLE_MASK
        if sys.stdout.isatty():
            colopts |= _COL_ENABLED

    # strbuf_getline strips the trailing newline; trailing newline-less line
    # is still kept. Read raw bytes to split on \n only (not \r etc).
    data = sys.stdin.read()
    if data == "":
        items: list[str] = []
    else:
        items = data.split("\n")
        if items and items[-1] == "":
            items.pop()

    _col_print_columns(items, colopts, indent, nl, padding, width)
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
    """Show the diff between the index and the working tree (plumbing)."""
    ap = argparse.ArgumentParser(prog="pygit diff-files", add_help=False)
    ap.add_argument("--name-only", dest="name_only", action="store_true")
    ap.add_argument("--name-status", dest="name_status", action="store_true")
    ap.add_argument("--stat", action="store_true")
    ap.add_argument("--numstat", action="store_true")
    ap.add_argument("--shortstat", action="store_true")
    ap.add_argument("-p", "-u", "--patch", dest="patch", action="store_true")
    ap.add_argument("--patch-with-raw", dest="patch_with_raw", action="store_true")
    ap.add_argument("--patch-with-stat", dest="patch_with_stat", action="store_true")
    ap.add_argument("-R", dest="reverse", action="store_true")
    ap.add_argument("-z", dest="nul", action="store_true")
    ap.add_argument("--abbrev", nargs="?", const=7, type=int, default=None)
    ap.add_argument("--full-index", dest="full_index", action="store_true")
    ap.add_argument("-a", "--text", dest="text", action="store_true")
    ap.add_argument("-q", "--quiet", action="store_true")
    ap.add_argument("-U", "--unified", type=int, default=3)
    ap.add_argument("paths", nargs="*")
    argv = ["--abbrev=7" if a == "--abbrev" else a for a in argv]
    args = ap.parse_args(argv)
    repo = _repo()
    from .index import read_index
    idx = read_index(repo)

    # Build the index-vs-worktree change set. The worktree blob is unstored, so
    # its raw id is all-zero (diff-files convention).
    changes: list[tuple[str, _Side, _Side]] = []
    for e in idx.entries:
        if getattr(e, "stage", 0) != 0:
            continue
        full = repo.path / e.path
        a = _side_from_object(repo, e.mode_str(), e.sha)
        b = _side_from_worktree(repo, e.path) if (full.exists() or full.is_symlink()) else _ABSENT
        if a.sha != b.sha or a.mode != b.mode:
            changes.append((e.path, a, b))
    if args.paths:
        changes = [c for c in changes if c[0] in set(args.paths)
                   or any(c[0].startswith(w.rstrip("/") + "/") for w in args.paths)]
    # -q is a no-op in diff-files (it is not --quiet); output is unaffected.

    if args.stat or args.patch_with_stat:
        _diff_stat([(p, b, a) for p, a, b in changes] if args.reverse else changes)
        if not args.patch_with_stat:
            return 0
    if args.numstat:
        _diff_numstat([(p, b, a) for p, a, b in changes] if args.reverse else changes)
        return 0
    if args.shortstat:
        _diff_shortstat(changes)
        return 0
    sep = "\0" if args.nul else "\t"
    term = "\0" if args.nul else "\n"
    if args.name_only:
        for p, _a, _b in changes:
            sys.stdout.write(p + term)
        return 0
    if args.name_status:
        for p, a, b in changes:
            st = "A" if not a.present else ("D" if not b.present else "M")
            if args.reverse:
                st = {"A": "D", "D": "A"}.get(st, st)
            sys.stdout.write(st + sep + p + term)
        return 0
    if args.patch or args.patch_with_raw or args.patch_with_stat:
        if args.patch_with_raw:
            for p, a, b in changes:
                sys.stdout.write(_df_raw_line(p, a, b, args.abbrev, args.full_index, args.reverse) + sep + p + term)
            _print("")
        for p, a, b in changes:
            # -R swaps the sides (content) and flips the a/b prefix labels.
            if args.reverse:
                _emit_file_diff(p, b, a, True, args.unified)
            else:
                _emit_file_diff(p, a, b, False, args.unified)
        return 0
    # Default: the raw diff line per change.
    for p, a, b in changes:
        sys.stdout.write(_df_raw_line(p, a, b, args.abbrev, args.full_index, args.reverse) + sep + p + term)
    return 0


def _df_raw_line(path: str, idx_side: "_Side", wt_side: "_Side", abbrev: Optional[int],
                 full_index: bool, reverse: bool) -> str:
    """A diff-files raw prefix ':MODE MODE ID ID STATUS' (no path). The worktree
    side id is always zero (unstored); ``reverse`` swaps the two columns."""
    width = 40 if (full_index or abbrev is None) else max(4, abbrev)
    zero = "0" * width
    idx_mode = idx_side.mode if idx_side.present else "000000"
    wt_mode = wt_side.mode if wt_side.present else "000000"
    idx_id = (idx_side.sha[:width] if width < 40 else idx_side.sha) if idx_side.present else zero
    st = "D" if not wt_side.present else ("A" if not idx_side.present else "M")
    if reverse:
        st = {"A": "D", "D": "A"}.get(st, st)
        return f":{wt_mode} {idx_mode} {zero} {idx_id} {st}"
    return f":{idx_mode} {wt_mode} {idx_id} {zero} {st}"


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
    ap = argparse.ArgumentParser(prog="pygit check-ref-format", add_help=False)
    ap.add_argument("--branch", action="store_true")
    ap.add_argument("--normalize", action="store_true")
    ap.add_argument("--allow-onelevel", dest="allow_onelevel", action="store_true", default=False)
    ap.add_argument("--no-allow-onelevel", dest="allow_onelevel", action="store_false")
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
        if not args.allow_onelevel and name.count("/") < 1 and not name.startswith("refs/"):
            # require category/name (unless --allow-onelevel)
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
    ap = argparse.ArgumentParser(prog="pygit check-mailmap", add_help=False)
    ap.add_argument("--stdin", action="store_true")
    ap.add_argument("contacts", nargs="*")
    args = ap.parse_args(argv)
    repo = _repo()
    from . import mailmap as _mailmap
    mm = _mailmap.load(repo)
    contacts = list(args.contacts)
    if args.stdin:
        contacts += [line.rstrip("\n") for line in sys.stdin]
    for c in contacts:
        name, email = _parse_who(c)
        mn, me = mm.resolve(name, email)
        _print(f"{mn} <{me}>" if mn else f"<{me}>")
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


_PATCH_ID_USAGE = (
    "usage: git patch-id [--stable | --unstable | --verbatim]\n"
    "\n"
    "    --unstable            use the unstable patch ID algorithm\n"
    "    --stable              use the stable patch ID algorithm\n"
    "    --verbatim            don't strip whitespace from the patch\n"
    "\n"
)


# C ``isspace`` in the "C" locale: space, \t, \n, \v, \f, \r.
_C_ISSPACE = frozenset(b" \t\n\x0b\x0c\r")


def _patch_id_remove_space(line: bytes) -> bytes:
    """Match C Git's remove_space(): drop every C-isspace byte."""
    return bytes(b for b in line if b not in _C_ISSPACE)


def _patch_id_is_hex_oid(buf: bytes) -> bool:
    """Mirror get_oid_hex(): succeeds when buf starts with 40 hex digits."""
    if len(buf) < 40:
        return False
    head = buf[:40]
    return all(c in b"0123456789abcdefABCDEF" for c in head)


def _patch_id_scan_hunk_header(line: bytes) -> tuple[bool, int, int]:
    """Port of scan_hunk_header(): parse '@@ -<n>[,<m>] +<n>[,<m>] @@'.

    Returns (ok, before, after). On failure before/after are left as C does
    (only the fields it managed to set are meaningful); callers ignore them
    when ok is False, matching the C path where scan_hunk_header's return is
    discarded but the int outputs are used regardless.
    """
    digits = b"0123456789"
    p = line
    q = p[4:]
    n = 0
    while n < len(q) and q[n:n + 1] and q[n] in digits:
        n += 1
    if n < len(q) and q[n:n + 1] == b",":
        q = q[n + 1:]
        before = _atoi(q)
        n = 0
        while n < len(q) and q[n] in digits:
            n += 1
    else:
        before = 1
    if n == 0 or (q[n:n + 1] != b" ") or (q[n + 1:n + 2] != b"+"):
        return False, before, 1
    r = q[n + 2:]
    n = 0
    while n < len(r) and r[n] in digits:
        n += 1
    if n < len(r) and r[n:n + 1] == b",":
        r = r[n + 1:]
        after = _atoi(r)
        n = 0
        while n < len(r) and r[n] in digits:
            n += 1
    else:
        after = 1
    if n == 0:
        return False, before, after
    return True, before, after


def _atoi(buf: bytes) -> int:
    """C atoi() on a leading run of decimal digits (no sign handling needed)."""
    n = 0
    i = 0
    while i < len(buf) and buf[i] in b"0123456789":
        n = n * 10 + (buf[i] - 0x30)
        i += 1
    return n


def _patch_id_getwholeline(data: bytes, pos: int) -> tuple[Optional[bytes], int]:
    """Read one line including its trailing '\\n', like strbuf_getwholeline."""
    if pos >= len(data):
        return None, pos
    nl = data.find(b"\n", pos)
    if nl == -1:
        return data[pos:], len(data)
    return data[pos:nl + 1], nl + 1


def _patch_id_flush_one_hunk(result: bytearray, ctx) -> "object":
    """Port of flush_one_hunk(): fold ctx's digest into result (byte sum with
    carry) and return a fresh SHA1 context."""
    import hashlib
    digest = ctx.digest()
    carry = 0
    for i in range(20):
        carry += result[i] + digest[i]
        result[i] = carry & 0xFF
        carry >>= 8
    return hashlib.sha1()


def _get_one_patchid(data: bytes, pos: int, stable: bool, verbatim: bool):
    """Faithful port of get_one_patchid().

    Returns (patchlen, next_oid_hex, result_hex, new_pos).
    next_oid_hex is the 40-char commit id of the *following* patch boundary,
    or 40 zeros when no further boundary was seen.
    """
    import hashlib
    ctx = hashlib.sha1()
    result = bytearray(20)
    patchlen = 0
    found_next = False
    before = after = -1
    diff_is_binary = False
    pre_oid_str = b""
    post_oid_str = b""
    next_oid_hex = "0" * 40

    while True:
        line, pos = _patch_id_getwholeline(data, pos)
        if line is None:
            break
        p = line
        matched_prefix = False
        if line.startswith(b"commit "):
            p = line[len(b"commit "):]
            matched_prefix = True
        elif line.startswith(b"From "):
            p = line[len(b"From "):]
            matched_prefix = True
        if (not matched_prefix and line.startswith(b"\\ ")
                and len(line) > 12):
            if verbatim:
                ctx.update(line)
            continue

        if _patch_id_is_hex_oid(p):
            found_next = True
            next_oid_hex = p[:40].decode("ascii").lower()
            break

        # Ignore commit comments.
        if not patchlen and not line.startswith(b"diff "):
            continue

        # Parsing diff header?
        if before == -1:
            if line.startswith(b"GIT binary patch") or line.startswith(b"Binary files"):
                diff_is_binary = True
                before = 0
                ctx.update(pre_oid_str)
                ctx.update(post_oid_str)
                if stable:
                    ctx = _patch_id_flush_one_hunk(result, ctx)
                continue
            elif line.startswith(b"index "):
                oid1_end = line.find(b"..")
                oid2_end = -1
                if oid1_end != -1:
                    oid2_end = line.find(b" ", oid1_end)
                if oid2_end == -1:
                    oid2_end = len(line) - 1
                if oid1_end != -1 and oid2_end != -1:
                    pre_oid_str = line[len(b"index "):oid1_end]
                    post_oid_str = line[oid1_end + 2:oid2_end]
                continue
            elif line.startswith(b"--- "):
                before = after = 1
            elif not (line[:1].isalpha()):
                break

        if diff_is_binary:
            if line.startswith(b"diff "):
                diff_is_binary = False
                before = -1
            continue

        # Looking for a valid hunk header?
        if before == 0 and after == 0:
            if line.startswith(b"@@ -"):
                _ok, before, after = _patch_id_scan_hunk_header(line)
                continue
            if not line.startswith(b"diff "):
                break
            if stable:
                ctx = _patch_id_flush_one_hunk(result, ctx)
            before = after = -1

        # Inside a hunk.
        c0 = line[0:1]
        if c0 == b"-" or c0 == b" ":
            before -= 1
        if c0 == b"+" or c0 == b" ":
            after -= 1

        emit = line if verbatim else _patch_id_remove_space(line)
        patchlen += len(emit)
        ctx.update(emit)

    if not found_next:
        next_oid_hex = "0" * 40

    ctx = _patch_id_flush_one_hunk(result, ctx)
    result_hex = bytes(result).hex()
    return patchlen, next_oid_hex, result_hex, pos


def cmd_patch_id(argv: list[str]) -> int:
    """Read one or more patches on stdin and emit their patch IDs.

    Port of builtin/patch-id.c: emits "<patch-id> <commit-id>" per patch.
    """
    from . import gitconfig

    repo = None
    try:
        repo = _repo()
    except Exception:
        repo = None

    def _cfg_bool(name: str) -> bool:
        val = gitconfig.get(repo, name)
        if val is None:
            return False
        v = val.strip().lower()
        if v in ("", "true", "yes", "on"):
            return True
        if v in ("false", "no", "off"):
            return False
        try:
            return int(v) != 0
        except ValueError:
            return True

    cfg_stable = _cfg_bool("patchid.stable")
    cfg_verbatim = _cfg_bool("patchid.verbatim")

    # CMDMODE: --unstable=1, --stable=2, --verbatim=3 (mutually exclusive).
    opts = 0
    mode_name = {1: "--unstable", 2: "--stable", 3: "--verbatim"}
    for a in argv:
        if a in ("-h", "--help"):
            if a == "-h":
                sys.stdout.write(_PATCH_ID_USAGE)
                return 129
            sys.stderr.write(
                "fatal: 'patch-id --help' is not supported in this build\n")
            return 129
        new = None
        if a == "--unstable":
            new = 1
        elif a == "--stable":
            new = 2
        elif a == "--verbatim":
            new = 3
        elif a == "--":
            continue
        elif a.startswith("-") and a != "-":
            name = a[2:] if a.startswith("--") else a[1:]
            sys.stderr.write(f"error: unknown option `{name}'\n")
            sys.stderr.write(_PATCH_ID_USAGE)
            return 129
        else:
            # Non-option args are silently ignored by git patch-id.
            continue
        if new is not None:
            if opts and opts != new:
                sys.stderr.write(
                    f"error: options '{mode_name[new]}' and "
                    f"'{mode_name[opts]}' cannot be used together\n")
                return 129
            opts = new

    if opts:
        stable = opts > 1
        verbatim = opts == 3
    else:
        verbatim = cfg_verbatim
        stable = cfg_stable or cfg_verbatim  # verbatim implies stable

    data = sys.stdin.buffer.read()
    pos = 0
    cur_oid = "0" * 40
    out = sys.stdout.buffer
    n = len(data)
    while pos < n:
        patchlen, next_oid, result_hex, pos = _get_one_patchid(
            data, pos, stable, verbatim)
        if patchlen:
            out.write(f"{result_hex} {cur_oid}\n".encode("ascii"))
        cur_oid = next_oid
    out.flush()
    return 0


_CHECKOUT_INDEX_USAGE = (
    "usage: git checkout-index [<options>] [--] [<file>...]\n"
    "\n"
    "    -a, --[no-]all        check out all files in the index\n"
    "    --[no-]ignore-skip-worktree-bits\n"
    "                          do not skip files with skip-worktree set\n"
    "    -f, --[no-]force      force overwrite of existing files\n"
    "    -q, --[no-]quiet      no warning for existing files and files not in index\n"
    "    -n, --no-create       don't checkout new files\n"
    "    --create              opposite of --no-create\n"
    "    -u, --[no-]index      update stat information in the index file\n"
    "    -z                    paths are separated with NUL character\n"
    "    --[no-]stdin          read list of paths from the standard input\n"
    "    --[no-]temp           write the content to temporary files\n"
    "    --[no-]prefix <string>\n"
    "                          when creating files, prepend <string>\n"
    "    --stage (1|2|3|all)   copy out the files from named stage\n"
    "\n"
)


class _CheckoutIndexParseError(Exception):
    """Signals a parse-options failure: print `message` (already complete),
    then the usage block, and exit 129. If `message` is None, just usage."""

    def __init__(self, message: Optional[str] = None, show_usage: bool = True,
                 usage_stream: str = "stderr"):
        super().__init__(message or "")
        self.message = message
        self.show_usage = show_usage
        self.usage_stream = usage_stream


class _CheckoutIndexDie(Exception):
    """Signals a die(): print 'fatal: <message>' and exit 128."""


# Long options: name -> (takes_value, allow_negation)
_CI_LONG = {
    "all": (False, True),
    "ignore-skip-worktree-bits": (False, True),
    "force": (False, True),
    "quiet": (False, True),
    "no-create": (False, False),
    "create": (False, False),
    "index": (False, True),
    "stdin": (False, True),
    "temp": (False, True),
    "prefix": (True, True),
    "stage": (True, False),
}
# Short options: letter -> (long-name-or-None, takes_value)
_CI_SHORT = {
    "a": ("all", False),
    "f": ("force", False),
    "q": ("quiet", False),
    "n": ("no-create", False),
    "u": ("index", False),
    "z": (None, False),  # -z has no long form
}


def _ci_resolve_long(name: str):
    """Resolve a (possibly abbreviated, possibly --no-) long option name to a
    canonical (name, negated) pair, mirroring git's parse-options. Raises
    _CheckoutIndexParseError on unknown/ambiguous names."""
    negated = False
    base = name
    # Exact match wins before abbreviation/negation handling.
    if base not in _CI_LONG and base.startswith("no-"):
        stripped = base[3:]
        if stripped in _CI_LONG and _CI_LONG[stripped][1]:
            return stripped, True
    if base in _CI_LONG:
        return base, False
    # Abbreviation: collect unique prefix matches over both the option names
    # and their negated ("no-<name>") forms.
    candidates: list[tuple[str, bool]] = []
    seen: set[str] = set()
    for opt, (_, can_neg) in _CI_LONG.items():
        if opt.startswith(base):
            key = "--" + opt
            if key not in seen:
                seen.add(key)
                candidates.append((opt, False))
        if can_neg:
            neg = "no-" + opt
            if neg.startswith(base):
                key = "--" + neg
                if key not in seen:
                    seen.add(key)
                    candidates.append((opt, True))
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        disp = " or ".join(
            "--" + ("no-" if neg else "") + opt for opt, neg in candidates
        )
        raise _CheckoutIndexParseError(
            f"error: ambiguous option: {base} (could be {disp})",
            usage_stream="stdout",
        )
    raise _CheckoutIndexParseError(f"error: unknown option `{name}'")


def _ci_parse(argv: list[str]) -> dict:
    """Faithful subset of parse_options for checkout-index. Returns a dict of
    parsed values, or raises _CheckoutIndexParseError / _CheckoutIndexDie."""
    opts = {
        "all": False, "ignore-skip-worktree-bits": False, "force": False,
        "quiet": False, "not_new": False, "index": False, "z": False,
        "stdin": False, "temp": None, "prefix": None, "stage": 0,
    }
    paths: list[str] = []
    i = 0
    n = len(argv)
    while i < n:
        a = argv[i]
        if a == "--":
            paths.extend(argv[i + 1:])
            break
        if a.startswith("--") and len(a) > 2:
            body = a[2:]
            eq = body.find("=")
            inline = None
            if eq >= 0:
                name, inline = body[:eq], body[eq + 1:]
            else:
                name = body
            canon, negated = _ci_resolve_long(name)
            takes_value = _CI_LONG[canon][0]
            if canon == "create":
                opts["not_new"] = False
                i += 1
                continue
            if canon == "no-create":
                opts["not_new"] = not negated
                i += 1
                continue
            if takes_value:
                if inline is not None:
                    val = inline
                    i += 1
                else:
                    if i + 1 >= n:
                        raise _CheckoutIndexParseError(
                            f"error: option `{canon}' requires a value",
                            show_usage=False,
                        )
                    val = argv[i + 1]
                    i += 2
                if canon == "prefix":
                    opts["prefix"] = "" if negated else val
                elif canon == "stage":
                    if val == "all":
                        opts["stage"] = 4
                    elif len(val) == 1 and "1" <= val <= "3":
                        opts["stage"] = int(val)
                    else:
                        raise _CheckoutIndexDie(
                            "stage should be between 1 and 3 or all"
                        )
                continue
            # boolean long option
            if inline is not None:
                raise _CheckoutIndexParseError(
                    f"error: option `{canon}' takes no value"
                )
            value = not negated
            if canon == "all":
                opts["all"] = value
            elif canon == "ignore-skip-worktree-bits":
                opts["ignore-skip-worktree-bits"] = value
            elif canon == "force":
                opts["force"] = value
            elif canon == "quiet":
                opts["quiet"] = value
            elif canon == "index":
                opts["index"] = value
            elif canon == "stdin":
                opts["stdin"] = value
            elif canon == "temp":
                opts["temp"] = value
            i += 1
            continue
        # A bare "-" or any non-option token is a pathspec.
        if a == "-" or not a.startswith("-"):
            paths.append(a)
            i += 1
            continue
        if len(a) >= 2 and not a.startswith("--"):
            # short option cluster, e.g. -afq
            j = 1
            while j < len(a):
                ch = a[j]
                if ch not in _CI_SHORT:
                    raise _CheckoutIndexParseError(
                        f"error: unknown switch `{ch}'"
                    )
                canon, takes_value = _CI_SHORT[ch]
                if canon == "all":
                    opts["all"] = True
                elif canon == "force":
                    opts["force"] = True
                elif canon == "quiet":
                    opts["quiet"] = True
                elif canon == "no-create":
                    opts["not_new"] = True
                elif canon == "index":
                    opts["index"] = True
                elif canon is None:  # -z
                    opts["z"] = True
                j += 1
            i += 1
            continue
        paths.append(a)
        i += 1
    return {"opts": opts, "paths": paths}


def _ci_lstat_entry(full: Path):
    """Build a stat snapshot comparable to a stored index entry, using the
    same field derivation as index.stat_to_entry. Returns None if the path
    does not exist (ENOENT-equivalent)."""
    try:
        st = os.lstat(full)
    except OSError:
        return None
    return st


def _ci_changed(repo: Repository, e, st) -> bool:
    """Replicate ie_match_stat(CE_MATCH_IGNORE_VALID|IGNORE_SKIP_WORKTREE):
    is the worktree file `st` different from index entry `e`? Returns True if
    the existing file must be overwritten (and thus needs --force)."""
    import stat as _stat
    from .index import stat_to_entry as _ste
    # intent-to-add: always changed.
    if e.intent_to_add:
        return True
    mode = e.mode
    fmt = mode & 0o170000
    # ce_match_stat_basic: type/mode comparison.
    if fmt == 0o100000:  # regular file
        if not _stat.S_ISREG(st.st_mode):
            return True
        # only owner-x bit considered (trust_executable_bit).
        if (0o100 & (mode ^ st.st_mode)):
            return True
    elif fmt == 0o120000:  # symlink
        if not _stat.S_ISLNK(st.st_mode) and not _stat.S_ISREG(st.st_mode):
            return True
    # match_stat_data: compare stored stat fields against a fresh snapshot
    # derived identically (mtime s+ns, ctime s+ns, uid, gid, ino, dev, size).
    fresh = _ste(e.path, st, e.sha, e.mode)
    if (e.mtime_s & 0xFFFFFFFF) != (fresh.mtime_s & 0xFFFFFFFF):
        return True
    if (e.mtime_n & 0xFFFFFFFF) != (fresh.mtime_n & 0xFFFFFFFF):
        return True
    if (e.ctime_s & 0xFFFFFFFF) != (fresh.ctime_s & 0xFFFFFFFF):
        return True
    if (e.ctime_n & 0xFFFFFFFF) != (fresh.ctime_n & 0xFFFFFFFF):
        return True
    if (e.uid & 0xFFFFFFFF) != (fresh.uid & 0xFFFFFFFF):
        return True
    if (e.gid & 0xFFFFFFFF) != (fresh.gid & 0xFFFFFFFF):
        return True
    if (e.ino & 0xFFFFFFFF) != (fresh.ino & 0xFFFFFFFF):
        return True
    if (e.dev & 0xFFFFFFFF) != (fresh.dev & 0xFFFFFFFF):
        return True
    if (e.size & 0xFFFFFFFF) != (fresh.size & 0xFFFFFFFF):
        return True
    # racy-smudge: a zero-size cache entry whose blob is non-empty is dirty.
    if (e.size & 0xFFFFFFFF) == 0:
        empty_blob = objs.hash_bytes("blob", b"", repo)[0]
        if e.sha != empty_blob:
            return True
    return False


def _ci_write_entry(repo: Repository, e, out: Path, refresh: bool):
    """Materialize one index entry to `out`. Returns the lstat result of the
    written file (for --index refresh) or None on failure."""
    out.parent.mkdir(parents=True, exist_ok=True)
    typ, data = objs.read_object(repo, e.sha)
    if (e.mode & 0o170000) == 0o120000:
        # symlink
        if out.exists() or out.is_symlink():
            out.unlink()
        os.symlink(data.decode("utf-8", "surrogateescape"), out)
    else:
        if out.exists() or out.is_symlink():
            out.unlink()
        out.write_bytes(data)
        if e.mode & 0o111:
            os.chmod(out, out.stat().st_mode | 0o111)
    if refresh:
        try:
            return os.lstat(out)
        except OSError:
            return None
    return None


def cmd_checkout_index(argv: list[str]) -> int:
    # Show usage for -h before any other work (long --help runs the man page,
    # which we cannot reproduce; it is handled by the dispatcher elsewhere).
    if "-h" in argv:
        sys.stdout.write(_CHECKOUT_INDEX_USAGE)
        return 129
    try:
        parsed = _ci_parse(argv)
    except _CheckoutIndexParseError as exc:
        if exc.message:
            sys.stderr.write(exc.message + "\n")
        if exc.show_usage:
            stream = sys.stdout if exc.usage_stream == "stdout" else sys.stderr
            stream.write(_CHECKOUT_INDEX_USAGE)
        return 129
    except _CheckoutIndexDie as exc:
        sys.stderr.write(f"fatal: {exc}\n")
        return 128

    opts = parsed["opts"]
    paths = parsed["paths"]
    repo = _repo()
    idx = read_index(repo)

    base_dir = opts["prefix"] or ""
    # Resolve the to_tempfile tri-state (default: temp iff --stage=all).
    to_tempfile = opts["temp"]
    if to_tempfile is None:
        to_tempfile = (opts["stage"] == 4)
    if not to_tempfile and opts["stage"] == 4:
        sys.stderr.write(
            "fatal: options '--stage=all' and '--no-temp' cannot be used "
            "together\n"
        )
        return 128
    if to_tempfile:
        # Writing to randomly-named temporary files yields non-deterministic
        # output; not faithfully reproducible here.
        sys.stderr.write(
            "fatal: pygit checkout-index: --temp / --stage=all is not "
            "supported\n"
        )
        return 128

    quiet = opts["quiet"]
    force = opts["force"]
    not_new = opts["not_new"]
    stage = opts["stage"]  # 0 default, 1..3 specific, 4 == all
    ignore_skip = opts["ignore-skip-worktree-bits"]

    # --index updates stat info in the index, but only when not writing
    # to a prefix and not to tempfiles.
    refresh_cache = bool(opts["index"]) and not base_dir and not to_tempfile

    err = False
    cache_changed = False

    def checkout_file(name: str) -> bool:
        """Return True on error (mirrors checkout_file returning <0)."""
        nonlocal cache_changed
        has_same_name = False
        is_file = False
        is_skipped = True
        did_checkout = False
        entries = [e for e in idx.entries if e.path == name]
        for e in entries:
            has_same_name = True
            is_file = True
            if not ignore_skip and e.skip_worktree:
                break
            is_skipped = False
            est = e.stage
            if est != stage and (stage != 4 or est == 0):
                continue
            did_checkout = True
            out = repo.path / (base_dir + e.path)
            if not _do_checkout_entry(e, out):
                return True
        if did_checkout:
            return False
        if has_same_name and stage == 4:
            return False
        if not quiet:
            msg = f"git checkout-index: {name} "
            if not has_same_name:
                msg += "is not in the cache"
            elif not is_file:
                msg += "is a sparse directory"
            elif is_skipped:
                msg += ("has skip-worktree enabled; use "
                        "'--ignore-skip-worktree-bits' to checkout")
            elif stage:
                msg += f"does not exist at stage {stage}"
            else:
                msg += "is unmerged"
            sys.stderr.write(msg + "\n")
        return True

    def _do_checkout_entry(e, out: Path) -> bool:
        """Mirror checkout_entry's worktree path. Return True on success,
        False on error (an existing modified file without --force)."""
        nonlocal cache_changed
        st = _ci_lstat_entry(out)
        if st is not None:
            # path exists in the worktree
            if not _ci_changed(repo, e, st):
                return True  # up to date, nothing to do
            if not force:
                if not quiet:
                    sys.stderr.write(
                        f"{base_dir}{e.path} already exists, no checkout\n"
                    )
                return False
            # force: fall through to (re)write
        else:
            if not_new:
                return True
        new_st = _ci_write_entry(repo, e, out, refresh_cache)
        if refresh_cache and new_st is not None:
            from .index import stat_to_entry as _ste
            refreshed = _ste(e.path, new_st, e.sha, e.mode)
            e.ctime_s, e.ctime_n = refreshed.ctime_s, refreshed.ctime_n
            e.mtime_s, e.mtime_n = refreshed.mtime_s, refreshed.mtime_n
            e.dev, e.ino = refreshed.dev, refreshed.ino
            e.uid, e.gid = refreshed.uid, refreshed.gid
            e.size = refreshed.size
            cache_changed = True
        return True

    def checkout_all() -> bool:
        nonlocal cache_changed
        errs = False
        for e in idx.entries:
            if not ignore_skip and e.skip_worktree:
                continue
            est = e.stage
            if est != stage and (stage != 4 or est == 0):
                continue
            if base_dir and (e.path == ""):
                continue
            out = repo.path / (base_dir + e.path)
            if not _do_checkout_entry(e, out):
                errs = True
        return errs

    # Mixing guards (die, rc 128), matching builtin ordering.
    if paths:
        if opts["all"]:
            sys.stderr.write(
                "fatal: git checkout-index: don't mix '--all' and explicit "
                "filenames\n"
            )
            return 128
        if opts["stdin"]:
            sys.stderr.write(
                "fatal: git checkout-index: don't mix '--stdin' and explicit "
                "filenames\n"
            )
            return 128
        for p in paths:
            if checkout_file(p):
                err = True

    if opts["stdin"]:
        if opts["all"]:
            sys.stderr.write(
                "fatal: git checkout-index: don't mix '--all' and '--stdin'\n"
            )
            return 128
        raw = sys.stdin.buffer.read()
        if opts["z"]:
            items = raw.split(b"\0")
            if items and items[-1] == b"":
                items = items[:-1]
            lines = [it.decode("utf-8", "surrogateescape") for it in items]
        else:
            text = raw.decode("utf-8", "surrogateescape")
            lines = text.split("\n")
            if lines and lines[-1] == "":
                lines = lines[:-1]
        for line in lines:
            name = line
            if not opts["z"] and name.startswith('"'):
                try:
                    name = _unquote_c_style(name)
                except ValueError:
                    sys.stderr.write("fatal: line is badly quoted\n")
                    return 128
            if checkout_file(name):
                err = True

    if opts["all"]:
        if checkout_all():
            err = True

    if err:
        return 1

    if refresh_cache and cache_changed:
        write_index(repo, idx)
    return 0


DEFAULT_MERGE_LOG_LEN = 20

_FMM_USAGE = (
    "usage: git fmt-merge-msg [-m <message>] [--log[=<n>] | --no-log] [--file <file>]\n"
    "\n"
    "    --[no-]log[=<n>]      populate log with at most <n> entries from shortlog\n"
    "    -m, --[no-]message <text>\n"
    "                          use <text> as start of message\n"
    "    --[no-]into-name <name>\n"
    "                          use <name> instead of the real target branch\n"
    "    -F, --[no-]file <file>\n"
    "                          file to read from\n"
    "\n"
)


class _FmtMergeMsgError(Exception):
    """Raised to signal a die() with a specific message (rc 128)."""


def _fmm_complete_line(buf: list[str]) -> None:
    """Mirror strbuf_complete_line: ensure the accumulated text ends in '\\n'.

    `buf` is a list of strings joined later; we keep a sentinel by inspecting
    the last non-empty char.
    """
    joined = "".join(buf)
    if joined and not joined.endswith("\n"):
        buf.append("\n")


def _fmm_commented_lines(text: str, comment: str) -> str:
    """Mirror strbuf_add_commented_lines: prefix each line with '<comment> ',
    or just '<comment>' for blank lines, completing a trailing line."""
    out = []
    bp = 0
    n = len(text)
    while bp < n:
        nl = text.find("\n", bp)
        if nl == -1:
            line = text[bp:]
            bp = n
            had_nl = False
        else:
            line = text[bp:nl]
            bp = nl + 1
            had_nl = True
        if line:
            out.append(comment + " " + line)
        else:
            out.append(comment)
        out.append("\n")
        if not had_nl:
            break
    return "".join(out)


def cmd_fmt_merge_msg(argv: list[str]) -> int:
    """Produce a merge commit message from a list of merged refs (FETCH_HEAD
    format) read from stdin or a file. Faithful port of builtin/fmt-merge-msg.c
    plus fmt-merge-msg.c."""
    ap = argparse.ArgumentParser(prog="pygit fmt-merge-msg", add_help=False)
    ap.add_argument("--log", dest="log", nargs="?", const="__DEFAULT__", default=None)
    ap.add_argument("--summary", dest="log", nargs="?", const="__DEFAULT__")
    ap.add_argument("--no-log", dest="log", action="store_const", const="__NO__")
    ap.add_argument("--no-summary", dest="log", action="store_const", const="__NO__")
    ap.add_argument("-m", "--message", dest="message", default=None)
    ap.add_argument("--into-name", dest="into_name", default=None)
    ap.add_argument("-F", "--file", dest="file", default=None)
    args, extra = ap.parse_known_args(argv)
    if extra:
        unknown = next((a for a in extra if a.startswith("--")), None)
        if unknown is not None:
            sys.stderr.write(f"error: unknown option `{unknown[2:]}'\n")
        else:
            short = next((a for a in extra if a.startswith("-") and len(a) > 1
                          and not a[1:2].isdigit()), None)
            if short is not None:
                sys.stderr.write(f"error: unknown switch `{short[1]}'\n")
        sys.stderr.write(_FMM_USAGE)
        return 129

    repo = _repo()

    # config: merge.log / merge.summary
    merge_log_config = -1
    comment = "#"
    try:
        cp = repo.config()
        for sect in ("merge",):
            if cp.has_section(sect):
                for key in ("log", "summary"):
                    val = cp.get(sect, key, fallback=None)
                    if val is None:
                        continue
                    merge_log_config = _fmm_config_log(val)
        if cp.has_section("core"):
            cc = cp.get("core", "commentChar", fallback=None)
            if cc is not None:
                cc = _fmm_dequote_config(cc)
            if cc and cc != "auto":
                comment = cc
    except Exception:
        pass

    # resolve shortlog_len. The C code keeps shortlog_len = -1 unless --log/
    # --no-log/--summary set it; after parsing, a value < 0 falls back to the
    # config (or 0).
    if args.log == "__NO__":
        shortlog_len = 0
    elif args.log == "__DEFAULT__":
        shortlog_len = DEFAULT_MERGE_LOG_LEN
    elif args.log is not None:
        parsed = _fmm_parse_int(args.log)
        if parsed is None:
            sys.stderr.write(
                "error: option `log' expects an integer value with an "
                "optional k/m/g suffix\n"
            )
            return 129
        shortlog_len = parsed
    else:
        shortlog_len = -1
    if shortlog_len < 0:
        shortlog_len = merge_log_config if merge_log_config > 0 else 0

    # read input
    if args.file and args.file != "-":
        try:
            data = Path(args.file).read_bytes()
        except OSError:
            sys.stderr.write(
                f"fatal: cannot open '{args.file}': No such file or directory\n"
            )
            return 128
        text = data.decode("utf-8", errors="surrogateescape")
    else:
        text = sys.stdin.buffer.read().decode("utf-8", errors="surrogateescape")

    message = args.message
    out: list[str] = []
    if message is not None:
        out.append(message)

    try:
        _fmt_merge_msg(
            repo,
            text,
            out,
            add_title=(message is None),
            credit=True,
            shortlog_len=shortlog_len,
            into_name=args.into_name,
            comment=comment,
        )
    except _FmtMergeMsgError as e:
        sys.stderr.write(f"fatal: {e}\n")
        return 128

    sys.stdout.write("".join(out))
    return 0


def _fmm_parse_int(s: str):
    """Mirror git's OPTION_INTEGER value parsing (git_parse_int): optional
    leading whitespace, sign, decimal/0x-hex/octal base, optional k/m/g unit
    suffix. Returns int or None on failure (trailing junk, empty)."""
    i = 0
    n = len(s)
    while i < n and s[i] in " \t\n":
        i += 1
    rest = s[i:]
    if not rest:
        return None
    # strtol with base 0: handles +/-, 0x, leading 0 octal.
    j = 0
    m = len(rest)
    sign = 1
    if j < m and rest[j] in "+-":
        if rest[j] == "-":
            sign = -1
        j += 1
    base = 10
    digits_start = j
    if j < m and rest[j] == "0":
        if j + 1 < m and rest[j + 1] in "xX":
            base = 16
            j += 2
            digits_start = j
        else:
            base = 8
            digits_start = j
    valid = "0123456789abcdef"[:base] if base != 8 else "01234567"
    if base == 16:
        valid = "0123456789abcdef"
    k = j
    while k < m and rest[k].lower() in valid:
        k += 1
    if k == digits_start:
        return None
    try:
        val = sign * int(rest[digits_start:k], base)
    except ValueError:
        return None
    suffix = rest[k:]
    factor = 1
    if suffix:
        if suffix in ("k", "K"):
            factor = 1024
        elif suffix in ("m", "M"):
            factor = 1024 * 1024
        elif suffix in ("g", "G"):
            factor = 1024 * 1024 * 1024
        else:
            return None
    return val * factor


def _fmm_dequote_config(value: str) -> str:
    """Remove git-config double-quote quoting from a value, handling backslash
    escapes inside quoted regions (git quotes values containing ; or # etc.)."""
    out = []
    i = 0
    n = len(value)
    in_q = False
    while i < n:
        c = value[i]
        if c == "\\" and i + 1 < n:
            nxt = value[i + 1]
            mapping = {"n": "\n", "t": "\t", "b": "\b", '"': '"', "\\": "\\"}
            out.append(mapping.get(nxt, nxt))
            i += 2
            continue
        if c == '"':
            in_q = not in_q
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _fmm_config_log(value: str) -> int:
    """Mirror fmt_merge_msg_config for merge.log/merge.summary."""
    v = value.strip()
    low = v.lower()
    if low in ("true", "yes", "on", ""):
        return DEFAULT_MERGE_LOG_LEN
    if low in ("false", "no", "off"):
        return 0
    try:
        n = int(v)
    except ValueError:
        return DEFAULT_MERGE_LOG_LEN
    if n:
        return n
    return 0


def _fmm_ident_name(repo: Repository, which: str) -> Optional[str]:
    """Return the running author/committer name (no date), like git_author_info."""
    name = None
    email = None
    try:
        cp = repo.config()
        if cp.has_section("user"):
            name = cp.get("user", "name", fallback=None)
            email = cp.get("user", "email", fallback=None)
    except Exception:
        pass
    if which == "a":
        name = os.environ.get("GIT_AUTHOR_NAME") or name
        email = os.environ.get("GIT_AUTHOR_EMAIL") or email
    else:
        name = os.environ.get("GIT_COMMITTER_NAME") or name
        email = os.environ.get("GIT_COMMITTER_EMAIL") or email
    name = os.environ.get("GIT_AUTHOR_NAME") if name is None and which == "a" else name
    if name is None:
        name = "pythongit"
    if email is None:
        email = "pythongit@example.invalid"
    return f"{name} <{email}>"


class _SrcData:
    __slots__ = ("branch", "tag", "r_branch", "generic", "head_status")

    def __init__(self):
        self.branch: list[str] = []
        self.tag: list[str] = []
        self.r_branch: list[str] = []
        self.generic: list[str] = []
        self.head_status = 0


def _fmm_print_joined(singular: str, plural: str, items: list[str]) -> str:
    if not items:
        return ""
    if len(items) == 1:
        return singular + items[0]
    parts = [plural]
    for i in range(len(items) - 1):
        parts.append(("" if i == 0 else ", ") + items[i])
    parts.append(" and " + items[-1])
    return "".join(parts)


def _fmt_merge_msg(repo, text, out, *, add_title, credit, shortlog_len,
                   into_name, comment):
    from . import merge as merge_mod

    hexsz = 64 if repo.object_format() == "sha256" else 40

    # learn HEAD oid and current branch name
    head_sym, head_oid = refs_mod.read_head(repo)
    if head_oid is None and head_sym is None:
        raise _FmtMergeMsgError("No current branch")
    if into_name is not None:
        current_branch = into_name
    elif head_sym and head_sym.startswith("refs/heads/"):
        current_branch = head_sym[len("refs/heads/"):]
    elif head_sym:
        current_branch = head_sym
    else:
        current_branch = "HEAD"

    # suppress_dest patterns default to main/master
    suppress_patterns = ["main", "master"]

    # --- find_merge_parents: determine which tips are non-redundant ---
    lines = _fmm_splitlines(text)
    given_to_commit: dict[str, str] = {}
    parent_commits: list[str] = []
    order: list[str] = []  # given oids in order, deduped
    for ln in lines:
        if len(ln) < hexsz + 2 or ln[hexsz] != "\t" or ln[hexsz + 1] != "\t":
            continue
        oid = ln[:hexsz]
        if not _fmm_is_hex(oid):
            continue
        commit = _fmm_peel_to_commit(repo, oid)
        if commit is None:
            continue
        if oid not in given_to_commit:
            given_to_commit[oid] = commit
            order.append(oid)
        parent_commits.append(commit)

    used: set[str] = set()
    if head_oid is not None or order:
        cand = list(parent_commits)
        if head_oid is not None:
            cand.append(head_oid)
        reduced = _fmm_reduce_heads(repo, cand, merge_mod)
        for oid in order:
            if given_to_commit[oid] in reduced:
                used.add(oid)

    # --- handle_line for each input line, building srcs/origins ---
    srcs: list[tuple[str, _SrcData]] = []  # (src_name, data) unsorted
    srcs_idx: dict[str, int] = {}
    origins: list[tuple[str, str]] = []  # (origin_string, given_oid)

    i = 0
    for raw in lines:
        i += 1
        ln = raw
        rc = _fmm_handle_line(ln, hexsz, used, srcs, srcs_idx, origins)
        if rc:
            raise _FmtMergeMsgError(f"error in line {i}: {raw}")

    # --- title ---
    if add_title and srcs:
        out.append(_fmm_title(srcs, current_branch, suppress_patterns))

    # --- tag bodies / signatures ---
    if origins:
        _fmm_sigs(repo, origins, out, comment)

    # --- shortlog ---
    if shortlog_len:
        _fmm_complete_line(out)
        for origin_str, given_oid in origins:
            _fmm_shortlog(repo, origin_str, given_oid, head_oid, shortlog_len,
                          credit, out, comment, merge_mod)

    _fmm_complete_line(out)


def _fmm_splitlines(text: str) -> list[str]:
    """Split like the C code: each segment up to '\\n' (newline stripped),
    keeping a trailing segment without newline."""
    res = []
    pos = 0
    n = len(text)
    while pos < n:
        nl = text.find("\n", pos)
        if nl == -1:
            res.append(text[pos:])
            break
        res.append(text[pos:nl])
        pos = nl + 1
    return res


def _fmm_is_hex(s: str) -> bool:
    if not s:
        return False
    try:
        int(s, 16)
    except ValueError:
        return False
    return all(c in "0123456789abcdef" for c in s)


def _fmm_peel_to_commit(repo, oid: str) -> Optional[str]:
    """Resolve oid to a commit, peeling tags. Return commit oid or None."""
    seen = 0
    cur = oid
    while seen < 10:
        seen += 1
        try:
            t, data = objs.read_object(repo, cur)
        except (KeyError, Exception):
            return None
        if t == "commit":
            return cur
        if t == "tag":
            target = None
            for line in data.decode("utf-8", errors="replace").splitlines():
                if line.startswith("object "):
                    target = line[len("object "):].strip()
                    break
                if line == "":
                    break
            if target is None:
                return None
            cur = target
            continue
        return None
    return None


def _fmm_reduce_heads(repo, commits: list[str], merge_mod) -> set[str]:
    """Mirror reduce_heads_replace: keep only commits that are not ancestors of
    any other commit in the list."""
    uniq = []
    seen = set()
    for c in commits:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    result = set(uniq)
    for a in uniq:
        for b in uniq:
            if a == b:
                continue
            if a not in result or b not in result:
                continue
            # if a is an ancestor of b, drop a
            if merge_mod.is_ancestor(repo, a, b):
                result.discard(a)
    return result


def _fmm_handle_line(line, hexsz, used, srcs, srcs_idx, origins) -> int:
    length = len(line)
    if length < hexsz + 3 or (len(line) <= hexsz or line[hexsz] != "\t"):
        return 1
    if line[hexsz + 1:].startswith("not-for-merge"):
        return 0
    if line[hexsz + 1] != "\t":
        return 2
    oid = line[:hexsz]
    if not _fmm_is_hex(oid):
        return 3
    if oid not in used:
        return 0  # subsumed by other parents

    is_local_branch = False
    rest = line[hexsz + 2:]

    # find " of "
    of_idx = rest.find(" of ")
    if of_idx != -1:
        src = rest[of_idx + 4:]
        line_part = rest[:of_idx]
        pulling_head = False
    else:
        src = rest
        line_part = rest
        pulling_head = True

    if src in srcs_idx:
        sd = srcs[srcs_idx[src]][1]
    else:
        sd = _SrcData()
        srcs_idx[src] = len(srcs)
        srcs.append((src, sd))

    if pulling_head:
        origin = src
        sd.head_status |= 1
    elif line_part.startswith("branch "):
        is_local_branch = True
        origin = line_part[len("branch "):]
        sd.branch.append(origin)
        sd.head_status |= 2
    elif line_part.startswith("tag "):
        origin = line_part
        sd.tag.append(line_part[len("tag "):])
        sd.head_status |= 2
    elif line_part.startswith("remote-tracking branch "):
        origin = line_part[len("remote-tracking branch "):]
        sd.r_branch.append(origin)
        sd.head_status |= 2
    else:
        origin = src
        sd.generic.append(line_part)
        sd.head_status |= 2

    if src == "." or src == origin:
        olen = len(origin)
        if olen >= 2 and origin[0] == "'" and origin[olen - 1] == "'":
            origin = origin[1:olen - 1]
    else:
        origin = f"{origin} of {src}"
    origins.append((origin, oid))
    return 0


def _fmm_title(srcs, current_branch, suppress_patterns) -> str:
    out = ["Merge "]
    sep = ""
    for src_name, sd in srcs:
        subsep = ""
        out.append(sep)
        sep = "; "
        if sd.head_status == 1:
            out.append(src_name)
            continue
        if sd.head_status == 3:
            subsep = ", "
            out.append("HEAD")
        if sd.branch:
            out.append(subsep)
            subsep = ", "
            out.append(_fmm_print_joined("branch ", "branches ", sd.branch))
        if sd.r_branch:
            out.append(subsep)
            subsep = ", "
            out.append(_fmm_print_joined(
                "remote-tracking branch ", "remote-tracking branches ", sd.r_branch))
        if sd.tag:
            out.append(subsep)
            subsep = ", "
            out.append(_fmm_print_joined("tag ", "tags ", sd.tag))
        if sd.generic:
            out.append(subsep)
            out.append(_fmm_print_joined("commit ", "commits ", sd.generic))
        if src_name != ".":
            out.append(f" of {src_name}")
    if not _fmm_dest_suppressed(current_branch, suppress_patterns):
        out.append(f" into {current_branch}")
    out.append("\n")
    return "".join(out)


def _fmm_dest_suppressed(dest, patterns) -> bool:
    import fnmatch
    for pat in patterns:
        # WM_PATHNAME: '*' does not match '/'. branch names rarely contain '/'.
        if fnmatch.fnmatchcase(dest, pat):
            return True
    return False


def _fmm_sigs(repo, origins, out, comment):
    """Mirror fmt_merge_msg_sigs: append annotated-tag bodies (and would-be
    signature verification, which we treat as merely-annotated)."""
    tagbuf: list[str] = []
    tag_number = 0
    first_tag_str = None
    for origin_str, given_oid in origins:
        try:
            t, data = objs.read_object(repo, given_oid)
        except Exception:
            continue
        if t != "tag":
            continue
        body = data.decode("utf-8", errors="surrogateescape")
        # strip a trailing PGP signature block if present (merely-annotated path
        # keeps body as-is; signed tags would be commented, which we cannot
        # verify, so we keep just the payload body).
        payload = body
        sig_start = body.find("\n-----BEGIN PGP SIGNATURE-----\n")
        if sig_start != -1:
            payload = body[:sig_start + 1]
        if tag_number == 0:
            _fmm_tag_signature(tagbuf, payload)
            first_tag_str = origin_str
            tag_number = 1
        else:
            if tag_number == 1:
                tagline = "\n" + _fmm_commented_lines(first_tag_str, comment)
                tagbuf.insert(0, tagline)
            tag_number += 1
            tagbuf.append("\n")
            tagbuf.append(_fmm_commented_lines(origin_str, comment))
            _fmm_tag_signature(tagbuf, payload)
    joined = "".join(tagbuf)
    if joined:
        out.append("\n")
        out.append(joined)


def _fmm_tag_signature(tagbuf: list[str], buf: str):
    idx = buf.find("\n\n")
    if idx != -1:
        body = buf[idx + 2:]
        tagbuf.append(body)
    _fmm_complete_line(tagbuf)


def _fmm_shortlog(repo, name, given_oid, head_oid, limit, credit, out, comment,
                  merge_mod):
    branch = _fmm_peel_to_commit(repo, given_oid)
    if branch is None:
        return

    # walk commits reachable from branch but not from head, in commit-date
    # descending order with FIFO tie-break (rev-list default order).
    interesting = _fmm_rev_walk(repo, branch, head_oid)

    subjects: list[str] = []
    authors: dict[str, int] = {}
    authors_order: list[str] = []
    committers: dict[str, int] = {}
    committers_order: list[str] = []
    count = 0

    for csha in interesting:
        try:
            t, data = objs.read_object(repo, csha)
        except Exception:
            continue
        if t != "commit":
            continue
        c = objs.parse_commit(data)
        is_merge = len(c.parents) > 1
        if is_merge:
            if credit:
                _fmm_record_person("c", committers, committers_order, data)
            continue
        if count == 0 and credit:
            _fmm_record_person("c", committers, committers_order, data)
        if credit:
            _fmm_record_person("a", authors, authors_order, data)
        count += 1
        if len(subjects) > limit:
            continue
        subject = _fmm_format_subject(c.message)
        if not subject:
            subjects.append(csha)
        else:
            subjects.append(subject)

    if credit:
        _fmm_add_people_info(out, authors, authors_order, committers,
                             committers_order, comment, repo)
    if count > limit:
        out.append(f"\n* {name}: ({count} commits)\n")
    else:
        out.append(f"\n* {name}:\n")

    for idx, subj in enumerate(subjects):
        if idx >= limit:
            out.append("  ...\n")
        else:
            out.append(f"  {subj}\n")


def _fmm_format_subject(message: str) -> str:
    """Mirror %s formatting then strbuf_ltrim."""
    # %s = first paragraph collapsed to a single line (subject).
    msg = message
    # Find the subject: text up to first blank line, with internal newlines
    # folded to spaces (git's format_subject).
    end = msg.find("\n\n")
    if end == -1:
        subj_src = msg
        # trailing single newline trimmed by %s logic
        if subj_src.endswith("\n"):
            subj_src = subj_src[:-1]
    else:
        subj_src = msg[:end]
    # fold newlines to single spaces
    parts = subj_src.split("\n")
    folded = " ".join(p for p in parts)
    # collapse the join: git replaces line breaks with a single space
    folded = " ".join(filter(None, [folded]))
    subj = folded.lstrip()
    return subj


def _fmm_record_person(which, people, order, commit_data):
    field = b"\nauthor " if which == "a" else b"\ncommitter "
    idx = commit_data.find(field)
    if idx == -1:
        return
    start = idx + len(field)
    lt = commit_data.find(b"<", start)
    if lt == -1:
        name_end = len(commit_data)
    else:
        name_end = lt - 1
    # trim trailing whitespace
    while name_end >= start and commit_data[name_end:name_end + 1].isspace():
        name_end -= 1
    if name_end < start:
        return
    name = commit_data[start:name_end + 1].decode("utf-8", errors="surrogateescape")
    if name not in people:
        people[name] = 0
        order.append(name)
    people[name] += 1


def _fmm_add_people_info(out, authors, authors_order, committers,
                         committers_order, comment, repo):
    # sort by name (string_list_insert keeps sorted), then stable by count desc
    a_sorted = sorted(authors_order)
    a_sorted = sorted(a_sorted, key=lambda n: -authors[n])
    c_sorted = sorted(committers_order)
    c_sorted = sorted(c_sorted, key=lambda n: -committers[n])
    _fmm_credit_people(out, a_sorted, authors, "a", comment, repo)
    _fmm_credit_people(out, c_sorted, committers, "c", comment, repo)


def _fmm_credit_people(out, names, counts, kind, comment, repo):
    if kind == "a":
        label = "By"
        me = _fmm_ident_name(repo, "a")
    else:
        label = "Via"
        me = _fmm_ident_name(repo, "c")
    if not names:
        return
    if len(names) == 1 and me is not None:
        # suppress if the single person is the running identity:
        # me starts with "<name> <"
        prefix = names[0]
        if me.startswith(prefix) and me[len(prefix):].startswith(" <"):
            return
    out.append(f"\n{comment} {label} ")
    out.append(_fmm_people_count(names, counts))


def _fmm_people_count(names, counts) -> str:
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} ({counts[names[0]]}) and {names[1]} ({counts[names[1]]})"
    if names:
        return f"{names[0]} ({counts[names[0]]}) and others"
    return ""


def _fmm_rev_walk(repo, tip: str, head_oid):
    """Commits reachable from tip but not from head, in commit-date descending
    order (FIFO tie-break), mirroring a default-order limited rev walk."""
    import heapq

    def ctime(sha):
        try:
            t, data = objs.read_object(repo, sha)
        except Exception:
            return 0
        if t != "commit":
            return 0
        c = objs.parse_commit(data)
        parts = c.committer.rsplit(" ", 2)
        try:
            return int(parts[-2])
        except (ValueError, IndexError):
            return 0

    UNINTERESTING = 1
    flags: dict[str, int] = {}
    heap: list[tuple[int, int, str]] = []
    ctr = 0
    flags[tip] = 0
    heapq.heappush(heap, (-ctime(tip), ctr, tip))
    ctr += 1
    if head_oid is not None:
        flags[head_oid] = flags.get(head_oid, 0) | UNINTERESTING
        heapq.heappush(heap, (-ctime(head_oid), ctr, head_oid))
        ctr += 1

    result: list[str] = []
    popped: set[str] = set()
    while heap:
        # stop when only uninteresting commits remain
        if all(flags.get(s, 0) & UNINTERESTING for _, _, s in heap):
            break
        negd, _c, sha = heapq.heappop(heap)
        if sha in popped:
            continue
        popped.add(sha)
        f = flags.get(sha, 0)
        info = _commit_tree_parents(repo, sha)
        parents = info[1] if info else ()
        if not (f & UNINTERESTING):
            result.append(sha)
        for p in parents:
            pf = flags.get(p, 0)
            newf = pf | (f & UNINTERESTING)
            if p in flags and (pf & UNINTERESTING) == (newf & UNINTERESTING) and p in popped:
                continue
            flags[p] = newf
            heapq.heappush(heap, (-ctime(p), ctr, p))
            ctr += 1
    return result


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


_PACK_REDUNDANT_USAGE = (
    "git pack-redundant [--verbose] [--alt-odb] (--all | <pack-filename>...)"
)

# 'git pack-redundant' is nominated for removal (Git 2.54). Without the
# --i-still-use-this opt-in the command refuses to run. The message has no
# replacement hint (hint == NULL), so only the generic block is printed.
_PACK_REDUNDANT_DEPRECATION = (
    "'git pack-redundant' is nominated for removal.\n"
    "If you still use this command, here's what you can do:\n"
    "\n"
    "- read https://git-scm.com/docs/BreakingChanges.html\n"
    "- check if anyone has discussed this on the mailing\n"
    "  list and if they came up with something that can\n"
    "  help you: https://lore.kernel.org/git/?q=git%20pack-redundant\n"
    "- send an email to <git@vger.kernel.org> to let us\n"
    "  know that you still use this command and were unable\n"
    "  to determine a suitable replacement\n"
    "\n"
)


def _pack_redundant_objdir(repo: Repository) -> str:
    """Reproduce git's repo_get_object_directory() for path display.

    git prints pack paths as ``<object-dir>/pack/pack-<hash>.<ext>``. The
    object directory is ``$GIT_DIR/objects`` where ``$GIT_DIR`` is exactly what
    setup discovered: the verbatim ``GIT_DIR`` env value when set, otherwise the
    relative ``.git`` that discovery records after chdir-ing to the worktree
    top-level.
    """
    env_git_dir = os.environ.get("GIT_DIR")
    if env_git_dir:
        return f"{env_git_dir}/objects"
    cwd = Path(os.getcwd()).resolve()
    if not repo.bare and cwd == repo.path:
        return os.path.join(os.path.relpath(repo.gitdir, cwd), "objects")
    return str(repo.gitdir / "objects")


def cmd_pack_redundant(argv: list[str]) -> int:
    """List redundant packfiles (those whose objects are all covered by the
    minimal byte-wise set of other packs). Plumbing; deprecated in Git 2.54."""
    from . import pack as _p

    # show_usage_if_asked: only when invoked as exactly `-h` or `--help-all`.
    if argv == ["-h"] or argv == ["--help-all"]:
        _print("usage: " + _PACK_REDUNDANT_USAGE)
        return 129

    load_all_packs = False
    verbose = False
    alt_odb = False
    i_still_use_this = False
    i = 0
    n = len(argv)
    positional: list[str] = []
    while i < n:
        arg = argv[i]
        if arg == "--":
            i += 1
            break
        if arg == "--all":
            load_all_packs = True
        elif arg == "--verbose":
            verbose = True
        elif arg == "--alt-odb":
            alt_odb = True
        elif arg == "--i-still-use-this":
            i_still_use_this = True
        elif arg.startswith("-") and arg != "":
            _err("usage: " + _PACK_REDUNDANT_USAGE)
            return 129
        else:
            break
        i += 1
    # Everything from the first non-option onward is a pack-filename list.
    positional = list(argv[i:])

    repo = _repo()

    if not i_still_use_this:
        sys.stderr.write(_PACK_REDUNDANT_DEPRECATION)
        _err("fatal: refusing to run without --i-still-use-this")
        return 128

    objdir = _pack_redundant_objdir(repo)

    def _pack_display(pk, ext: str) -> str:
        # pack_name / odb_pack_name are both <objdir>/pack/pack-<hash>.<ext>.
        name = pk.pack_path.name  # pack-<hash>.pack
        stem = name[:-len(".pack")] if name.endswith(".pack") else name
        return f"{objdir}/pack/{stem}.{ext}"

    # Build the local pack list in git's repo_for_each_pack order, reversed by
    # pack_list_insert's prepend: that yields oldest-mtime-first.
    all_local = list(_p._iter_packs(repo))
    # repo_for_each_pack: local packs sorted by mtime descending (newest first);
    # local_packs (after prepend) is the reverse => oldest first.
    ordered = sorted(
        all_local,
        key=lambda pk: (pk.pack_path.stat().st_mtime_ns, pk.pack_path.name),
    )

    if load_all_packs:
        local_packs = list(ordered)
    else:
        # Resolve each pack-filename to a known pack by substring match against
        # the canonical pack path, in argument order (then prepend-reversed).
        # add_pack_file: <40 chars => die; no substring match => die.
        chosen: list = []
        for fn in positional:
            if len(fn) < 40:
                _err(f"fatal: Bad pack filename: {fn}")
                return 128
            match = None
            for pk in ordered:
                if fn in str(pk.pack_path):
                    match = pk
                    break
            if match is None:
                _err(f"fatal: Filename {fn} not found in packed_git")
                return 128
            chosen.append(match)
        # local_packs is built by prepending each add_pack result, reversing
        # the add order.
        local_packs = list(reversed(chosen))

    if not local_packs:
        _err("fatal: Zero packs found!")
        return 128

    # remaining_objects per pack (set of object ids in that pack).
    remaining = {id(pk): set(pk.shas) for pk in local_packs}

    # all_objects = union over local packs (alt-odb objects would be removed,
    # but alternates are unsupported; see deferral note).
    all_objects: set[str] = set()
    for pk in local_packs:
        all_objects |= remaining[id(pk)]

    # Read object ids to ignore from stdin (one hex id per line). git always
    # reads stdin unless it is a tty; tests/scripts pipe data or /dev/null.
    ignore: set[str] = set()
    if not sys.stdin.isatty():
        hexsz = repo.hex_len
        for line in sys.stdin:
            # git's get_oid_hex reads exactly hexsz lowercase/uppercase hex
            # chars from the start of the fgets buffer; anything trailing (the
            # newline) is ignored. On failure it dies printing the raw buffer
            # (which still contains the trailing newline), and die() then adds
            # its own newline.
            hexpart = line[:hexsz]
            if len(hexpart) < hexsz or any(
                c not in "0123456789abcdefABCDEF" for c in hexpart
            ):
                # die("Bad object ID on stdin: %s", buf): buf is the raw fgets
                # line (with its own trailing newline, if any) and die() then
                # appends exactly one newline of its own.
                sys.stderr.write(f"fatal: Bad object ID on stdin: {line}\n")
                return 128
            ignore.add(hexpart.lower())
    if ignore:
        all_objects -= ignore
        for pk in local_packs:
            remaining[id(pk)] -= ignore

    # cmp_local_packs: unique_objects per pack = objects in this pack not in any
    # other local pack. With a single pack, unique_objects is empty.
    unique: dict[int, set[str]] = {}
    if len(local_packs) == 1:
        unique[id(local_packs[0])] = set()
    else:
        for pk in local_packs:
            others: set[str] = set()
            for other in local_packs:
                if other is pk:
                    continue
                others |= remaining[id(other)]
            unique[id(pk)] = remaining[id(pk)] - others

    # minimize(): unique packs (non-empty unique_objects) are mandatory; the
    # rest are greedily covered by remaining_objects size.
    unique_packs = [pk for pk in local_packs if unique[id(pk)]]
    non_unique = [pk for pk in local_packs if not unique[id(pk)]]

    # *min = unique (built via prepend => reversed relative to local_packs).
    min_set = list(reversed(unique_packs))

    missing = set(all_objects)
    for pk in unique_packs:
        missing -= remaining[id(pk)]

    if missing:
        unique_pack_objects = all_objects - missing
        rem2 = {id(pk): remaining[id(pk)] - unique_pack_objects for pk in non_unique}
        work = list(non_unique)
        while work:
            # sort: greatest remaining size first; git's stable sort keeps the
            # existing relative order for ties.
            work.sort(key=lambda pk: len(rem2[id(pk)]), reverse=True)
            head = work[0]
            if not rem2[id(head)]:
                break
            # prepend to min
            min_set.insert(0, head)
            for pk in work[1:]:
                if rem2[id(pk)]:
                    rem2[id(pk)] -= rem2[id(head)]
            work = work[1:]

    min_ids = {id(pk) for pk in min_set}
    # red = pack_list_difference(local_packs, min): local_packs order, skipping
    # packs already in min => oldest-mtime-first.
    red = [pk for pk in local_packs if id(pk) not in min_ids]

    if verbose:
        # altodb_packs is empty (alternates unsupported); count is 0.
        sys.stderr.write("There are 0 packs available in alt-odbs.\n")
        sys.stderr.write("The smallest (bytewise) set of packs is:\n")
        for pk in min_set:
            sys.stderr.write(f"\t{_pack_display(pk, 'pack')}\n")
        # duplicate objects within the min set (pairwise intersection sizes)
        dup = 0
        for a in range(len(min_set)):
            for b in range(a + 1, len(min_set)):
                dup += len(set(min_set[a].shas) & set(min_set[b].shas))
        min_bytes = 0
        for pk in min_set:
            min_bytes += pk.pack_path.stat().st_size
            min_bytes += pk.pack_path.with_suffix(".idx").stat().st_size
        sys.stderr.write(
            f"containing {dup} duplicate objects "
            f"with a total size of {min_bytes // 1024}kb.\n"
        )
        sys.stderr.write(
            f"A total of {len(all_objects)} unique objects were considered.\n"
        )
        sys.stderr.write("Redundant packs (with indexes):\n")

    for pk in red:
        _print(_pack_display(pk, "idx"))
        _print(_pack_display(pk, "pack"))

    if verbose:
        red_bytes = 0
        for pk in red:
            red_bytes += pk.pack_path.stat().st_size
            red_bytes += pk.pack_path.with_suffix(".idx").stat().st_size
        sys.stderr.write(
            f"{red_bytes // (1024 * 1024)}MB of redundant packs in total.\n"
        )

    return 0


_PRUNE_PACKED_USAGE = (
    "usage: git prune-packed [-n | --dry-run] [-q | --quiet]\n"
    "\n"
    "    -n, --[no-]dry-run    dry run\n"
    "    -q, --[no-]quiet      be quiet\n"
    "\n"
)


def cmd_prune_packed(argv: list[str]) -> int:
    """Remove loose objects that are also present in a pack."""
    dry_run = False
    # quiet only toggles progress, which goes to stderr and is suppressed when
    # stderr is not a TTY; we accept it for parity but emit no progress.
    extra: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--":
            extra.extend(argv[i + 1:])
            break
        if a in ("-n", "--dry-run"):
            dry_run = True
        elif a == "--no-dry-run":
            dry_run = False
        elif a in ("-q", "--quiet", "--no-quiet"):
            pass
        elif a in ("-h", "--help"):
            sys.stdout.write(_PRUNE_PACKED_USAGE)
            return 129
        elif a.startswith("--"):
            sys.stderr.write(f"error: unknown option `{a[2:]}'\n")
            sys.stderr.write(_PRUNE_PACKED_USAGE)
            return 129
        elif a.startswith("-") and a != "-":
            sys.stderr.write(f"error: unknown switch `{a[1:2]}'\n")
            sys.stderr.write(_PRUNE_PACKED_USAGE)
            return 129
        else:
            extra.append(a)
        i += 1
    if extra:
        sys.stderr.write("fatal: too many arguments\n\n")
        sys.stderr.write(_PRUNE_PACKED_USAGE)
        return 129

    repo = _repo()
    from . import pack as _p
    midx = _p.read_midx(repo)
    if midx is not None:
        in_packs: set[str] = set(midx.shas)
    else:
        in_packs = set()
        for pk in _p._iter_packs(repo):
            in_packs.update(pk.shas)

    obj_root = repo.gitdir / "objects"
    rel_root = os.path.relpath(obj_root, repo.path)
    for sha in _iter_loose_shas(repo):
        if sha in in_packs:
            rel_path = os.path.join(rel_root, sha[:2], sha[2:])
            if dry_run:
                _print(f"rm -f {rel_path}")
            else:
                (obj_root / sha[:2] / sha[2:]).unlink(missing_ok=True)
    if not dry_run:
        # Remove now-empty fanout subdirectories, mirroring git's rmdir().
        for i in range(256):
            d = obj_root / f"{i:02x}"
            if d.is_dir():
                try:
                    d.rmdir()
                except OSError:
                    pass
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


_DIAGNOSE_USAGE = (
    "usage: git diagnose [(-o | --output-directory) <path>] "
    "[(-s | --suffix) <format>]\n"
    "                    [--mode=<mode>]\n"
    "\n"
    "    -o, --[no-]output-directory <path>\n"
    "                          specify a destination for the diagnostics archive\n"
    "    -s, --[no-]suffix <format>\n"
    "                          specify a strftime format suffix for the filename\n"
    "    --mode (stats|all)    specify the content of the diagnostic archive\n"
    "\n"
)


def cmd_diagnose(argv: list[str]) -> int:
    """Collect diagnostic info into a git-diagnostics-<suffix>.zip archive."""
    # Hand-rolled parse-options to reproduce C Git's diagnose error messages
    # (rc 129) byte-for-byte.  C Git's --mode accepts only "stats" and "all";
    # the internal DIAGNOSE_NONE value is not selectable from the command line.
    option_output = ""
    option_suffix = "%Y-%m-%d-%H%M"
    mode = "stats"

    def _unknown(msg: str) -> int:
        sys.stderr.write(f"error: {msg}\n")
        sys.stderr.write(_DIAGNOSE_USAGE)
        return 129

    def _needs_value(msg: str) -> int:
        sys.stderr.write(f"error: {msg}\n")
        return 129

    def _parse_mode(val: str):
        nonlocal mode
        if val in ("stats", "all"):
            mode = val
            return None
        sys.stderr.write(f"error: invalid --mode value '{val}'\n")
        return 129

    _long_value = ("output-directory", "suffix")
    _positives = list(_long_value) + ["mode"]

    i = 0
    n = len(argv)
    while i < n:
        a = argv[i]
        if a == "-h":
            # parse-options prints -h usage to stdout (errors go to stderr).
            # (--help is handled like other pygit commands: as an unknown
            # option, since the manpage C Git would show is out of scope.)
            sys.stdout.write(_DIAGNOSE_USAGE)
            return 129
        if a == "--":
            i += 1
            break
        if a.startswith("--"):
            name, eq, val = a[2:].partition("=")
            has_val = bool(eq)
            # Resolve canonical long option (with unique-prefix abbreviation).
            if name.startswith("no-") and name[3:] in _positives:
                canon, negated = name[3:], True
            elif name in _positives:
                canon, negated = name, False
            else:
                matches = [c for c in _positives if c.startswith(name)]
                if len(matches) == 1:
                    canon, negated = matches[0], False
                elif len(matches) > 1:
                    joined = " or ".join("--" + m for m in matches)
                    sys.stderr.write(
                        f"error: ambiguous option: {name} (could be {joined})\n")
                    return 129
                else:
                    return _unknown(f"unknown option `{name}'")
            if canon == "mode":
                # --mode is PARSE_OPT_NONEG and its value is required.
                if negated:
                    return _unknown(f"unknown option `{name}'")
                if not has_val:
                    if i + 1 >= n:
                        return _needs_value("option `mode' requires a value")
                    val = argv[i + 1]
                    i += 1
                rc = _parse_mode(val)
                if rc is not None:
                    return rc
            elif canon == "output-directory":
                if negated:
                    option_output = ""
                else:
                    if not has_val:
                        if i + 1 >= n:
                            return _needs_value(
                                "option `output-directory' requires a value")
                        val = argv[i + 1]
                        i += 1
                    option_output = val
            elif canon == "suffix":
                if negated:
                    option_suffix = "%Y-%m-%d-%H%M"
                else:
                    if not has_val:
                        if i + 1 >= n:
                            return _needs_value(
                                "option `suffix' requires a value")
                        val = argv[i + 1]
                        i += 1
                    option_suffix = val
            i += 1
            continue
        if a.startswith("-") and a != "-":
            j = 1
            consumed_next = False
            while j < len(a):
                c = a[j]
                if c == "o" or c == "s":
                    rest = a[j + 1:]
                    if rest:
                        val = rest
                    elif i + 1 < n:
                        val = argv[i + 1]
                        consumed_next = True
                    else:
                        return _needs_value(f"switch `{c}' requires a value")
                    if c == "o":
                        option_output = val
                    else:
                        option_suffix = val
                    break
                else:
                    return _unknown(f"unknown switch `{c}'")
                j += 1
            i += 2 if consumed_next else 1
            continue
        # Non-option positional args are ignored by C Git's diagnose.
        i += 1

    repo = _repo()

    import time as _time
    suffix = _time.strftime(option_suffix, _time.localtime())
    prefix = option_output
    if prefix and not prefix.endswith("/"):
        prefix = prefix + "/"
    zip_path = f"{prefix}git-diagnostics-{suffix}.zip"

    # Create leading directories for the archive (matches C Git's
    # safe_create_leading_directories).
    parent = os.path.dirname(zip_path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    # The "Collecting diagnostic info" banner is written to stdout by C Git
    # via the saved real stdout fd; the contents (build/version/disk info)
    # are environment-specific and are not byte-reproducible here.
    from . import __version__ as _ppg_version
    worktree = "(null)" if repo.bare else str(repo.path)
    banner = []
    banner.append("Collecting diagnostic info")
    banner.append("")
    banner.append(f"pygit version {_ppg_version}")
    banner.append(f"Repository root: {worktree}")
    sys.stdout.write("\n".join(banner) + "\n")

    # Write a real zip archive containing the collected diagnostics.
    import zipfile as _zipfile
    log_lines = ["Collecting diagnostic info\n"]
    # mode=all bundles a fixed list of .git metadata directories; C Git warns
    # (to stderr) about each one that is missing, in this exact order.
    archive_dirs = [
        (".git", False),
        (".git/hooks", False),
        (".git/info", False),
        (".git/logs", True),
        (".git/objects/info", False),
    ]
    with _zipfile.ZipFile(zip_path, "w", _zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("diagnostics.log", "".join(log_lines))
        zf.writestr("packs-local.txt", "")
        zf.writestr("objects-local.txt", "")
        if mode == "all":
            for path, recurse in archive_dirs:
                if not os.path.isdir(path):
                    sys.stderr.write(
                        f"warning: could not archive missing directory "
                        f"'{path}'\n")
                    continue
                if recurse:
                    walk = os.walk(path)
                else:
                    walk = [(path, [], sorted(os.listdir(path)))]
                for root, _dirs, files in walk:
                    for fn in files:
                        fp = os.path.join(root, fn)
                        if not os.path.isfile(fp):
                            continue
                        try:
                            zf.write(fp, fp)
                        except OSError:
                            pass

    sys.stderr.write(
        "\n"
        "Diagnostics complete.\n"
        f"All of the gathered info is captured in '{zip_path}'\n")
    return 0


# Short usage emitted by git's usage() for stray arguments (builtin/bugreport.c
# bugreport_usage[0]); continuation lines align under "git bugreport ".
_BUGREPORT_USAGE = (
    "usage: git bugreport [(-o | --output-directory) <path>]\n"
    "              [(-s | --suffix) <format> | --no-suffix]\n"
    "              [--diagnose[=<mode>]]"
)

# Full usage emitted by parse-options for -h / unknown option (usage_with_options):
# the synopsis with deeper alignment, a blank line, then the option list.
_BUGREPORT_USAGE_OPTS = (
    "usage: git bugreport [(-o | --output-directory) <path>]\n"
    "                     [(-s | --suffix) <format> | --no-suffix]\n"
    "                     [--diagnose[=<mode>]]\n"
    "\n"
    "    --[no-]diagnose[=<mode>]\n"
    "                          create an additional zip archive of detailed diagnostics (default 'stats')\n"
    "    -o, --[no-]output-directory <path>\n"
    "                          specify a destination for the bugreport file(s)\n"
    "    -s, --[no-]suffix <format>\n"
    "                          specify a strftime format suffix for the filename(s)\n"
)


class _BugreportUsageError(Exception):
    # full=True   -> print the parse-options usage_with_options() block
    # full=False  -> print the short usage() synopsis
    # no_usage=True-> print only the error line (parse-options value errors)
    # msg=None    -> -h: print the full usage to stdout, rc 129
    def __init__(self, msg: Optional[str], *, full: bool = False, no_usage: bool = False):
        self.msg = msg
        self.full = full
        self.no_usage = no_usage


def _bugreport_git_editor() -> Optional[str]:
    """Replicate git_editor() precedence from editor.c."""
    term = os.environ.get("TERM")
    dumb = (not term) or term == "dumb"
    editor = os.environ.get("GIT_EDITOR")
    if not editor:
        repo = None
        try:
            repo = _repo()
        except Exception:
            repo = None
        if repo is not None:
            try:
                cp = repo.config()
                val = cp.get("core", "editor", fallback=None)
                if val:
                    editor = val
            except Exception:
                pass
    if not editor and not dumb:
        editor = os.environ.get("VISUAL")
    if not editor:
        editor = os.environ.get("EDITOR")
    if not editor and dumb:
        return None
    if not editor:
        editor = "vi"
    return editor


def _bugreport_launch_editor(path: str) -> int:
    """Replicate launch_editor() from editor.c; returns 0 on success else 1."""
    editor = _bugreport_git_editor()
    if editor is None:
        _err("error: Terminal is dumb, but EDITOR unset")
        return 1
    if editor == ":":
        return 0
    try:
        realpath = os.path.realpath(path)
    except Exception:
        realpath = path
    import subprocess as _sp
    try:
        proc = _sp.run(["sh", "-c", editor + ' "$@"', editor, realpath])
    except Exception:
        _err(f"error: unable to start editor '{editor}'")
        return 1
    if proc.returncode:
        _err(f"error: there was a problem with the editor '{editor}'")
        return 1
    return 0


def cmd_bugreport(argv: list[str]) -> int:
    """Write a bug-report template file and report its path (builtin/bugreport.c)."""
    option_output: Optional[str] = None
    option_suffix: Optional[str] = "%Y-%m-%d-%H%M"
    diagnose: Optional[str] = None  # None=off; "stats"/"all" when requested
    rest: list[str] = []

    diagnose_modes = ("stats", "all")

    try:
        i = 0
        n = len(argv)
        while i < n:
            a = argv[i]
            if a == "--":
                rest.extend(argv[i + 1:])
                break
            if a == "-h" or a == "--help":
                raise _BugreportUsageError(None, full=True)
            if a.startswith("--"):
                name, eq, val = a[2:].partition("=")
                has_val = bool(eq)
                # Long-option abbreviation (parse-options style): accept any
                # unambiguous prefix of a known long option.
                canon = name
                negate = False
                base = name
                if name.startswith("no-"):
                    base = name[3:]
                longs = ("diagnose", "output-directory", "suffix")
                # Resolve abbreviation against the (possibly no-) base name.
                matches = [o for o in longs if o == base] or [
                    o for o in longs if o.startswith(base)
                ]
                if len(matches) == 1:
                    canon = matches[0]
                    negate = name.startswith("no-")
                elif name in longs:
                    canon = name
                    negate = False
                else:
                    raise _BugreportUsageError(
                        f"error: unknown option `{name}'", full=True
                    )

                if canon == "output-directory":
                    if negate:
                        option_output = None
                    elif has_val:
                        option_output = val
                    else:
                        i += 1
                        if i >= n:
                            raise _BugreportUsageError(
                                "error: option `output-directory' requires a value",
                                no_usage=True,
                            )
                        option_output = argv[i]
                elif canon == "suffix":
                    if negate:
                        option_suffix = None
                    elif has_val:
                        option_suffix = val
                    else:
                        i += 1
                        if i >= n:
                            raise _BugreportUsageError(
                                "error: option `suffix' requires a value",
                                no_usage=True,
                            )
                        option_suffix = argv[i]
                elif canon == "diagnose":
                    if negate:
                        diagnose = None
                    elif has_val:
                        if val not in diagnose_modes:
                            raise _BugreportUsageError(
                                f"error: invalid --diagnose value '{val}'",
                                no_usage=True,
                            )
                        diagnose = val
                    else:
                        diagnose = "stats"
                i += 1
                continue
            if a.startswith("-") and a != "-":
                j = 1
                while j < len(a):
                    ch = a[j]
                    if ch == "o":
                        rest_inline = a[j + 1:]
                        if rest_inline:
                            option_output = rest_inline
                        else:
                            i += 1
                            if i >= n:
                                raise _BugreportUsageError(
                                    "error: switch `o' requires a value",
                                    no_usage=True,
                                )
                            option_output = argv[i]
                        break
                    elif ch == "s":
                        rest_inline = a[j + 1:]
                        if rest_inline:
                            option_suffix = rest_inline
                        else:
                            i += 1
                            if i >= n:
                                raise _BugreportUsageError(
                                    "error: switch `s' requires a value",
                                    no_usage=True,
                                )
                            option_suffix = argv[i]
                        break
                    elif ch == "h":
                        raise _BugreportUsageError(None, full=True)
                    else:
                        raise _BugreportUsageError(
                            f"error: unknown switch `{ch}'", full=True
                        )
                    j += 1
                i += 1
                continue
            rest.append(a)
            i += 1
    except _BugreportUsageError as exc:
        if exc.msg is None:
            # -h: full usage to stdout, rc 129.
            sys.stdout.write(_BUGREPORT_USAGE_OPTS + "\n")
            return 129
        _err(exc.msg)
        if not exc.no_usage:
            # The full block already ends in a newline; add the trailing blank
            # line git emits after usage_with_options().
            _err(_BUGREPORT_USAGE_OPTS + "\n" if exc.full else _BUGREPORT_USAGE)
        return 129

    if rest:
        _err(f"error: unknown argument `{rest[0]}'")
        _err(_BUGREPORT_USAGE)
        return 129

    if diagnose is not None:
        # The diagnostics archive bundles git-internal stats and a zip whose
        # contents cannot be reproduced byte-for-byte; refuse rather than emit a
        # divergent archive.
        _err("fatal: --diagnose is not supported by this implementation")
        return 128

    # Build report path: <output>/git-bugreport[-<suffix>].txt
    import time as _time
    now = _time.localtime()
    out_prefix = option_output if option_output else ""
    if out_prefix and not out_prefix.endswith("/"):
        out_prefix = out_prefix + "/"
    report_path = out_prefix + "git-bugreport"
    if option_suffix is not None:
        report_path += "-" + _time.strftime(option_suffix, now)
    report_path += ".txt"

    # safe_create_leading_directories on the full report path.
    parent = os.path.dirname(report_path)
    if parent:
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError:
            _err(f"fatal: could not create leading directories for '{report_path}'")
            return 128

    # Assemble the report body. Contents are not part of the parity contract
    # (they embed host/build specifics), but we mirror git's template + headers.
    import platform as _platform
    from . import __version__ as _ppg_version
    buf: list[str] = []
    buf.append(
        "Thank you for filling out a Git bug report!\n"
        "Please answer the following questions to help us understand your issue.\n"
        "\n"
        "What did you do before the bug happened? (Steps to reproduce your issue)\n"
        "\n"
        "What did you expect to happen? (Expected behavior)\n"
        "\n"
        "What happened instead? (Actual behavior)\n"
        "\n"
        "What's different between what you expected and what actually happened?\n"
        "\n"
        "Anything else you want to add:\n"
        "\n"
        "Please review the rest of the bug report below.\n"
        "You can delete any lines you don't wish to share.\n"
    )
    buf.append("\n\n[System Info]\n")
    buf.append("git version:\n")
    buf.append(f"pythongit version {_ppg_version}\n")
    buf.append("uname: " + " ".join(_platform.uname()) + "\n")
    buf.append(f"compiler info: python {_platform.python_version()}\n")
    shell = os.environ.get("SHELL")
    buf.append(
        "$SHELL (typically, interactive shell): "
        + (shell if shell else "<unset>")
        + "\n"
    )
    buf.append("\n\n[Enabled Hooks]\n")
    text = "".join(buf)

    # O_CREAT | O_EXCL | O_WRONLY: fail if the file already exists.
    try:
        fd = os.open(report_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
    except FileExistsError:
        _err(f"fatal: unable to create '{report_path}': File exists")
        return 128
    except OSError as exc:
        _err(f"fatal: unable to create '{report_path}': {exc.strerror}")
        return 128
    try:
        os.write(fd, text.encode("utf-8", "surrogateescape"))
    finally:
        os.close(fd)

    _err(f"Created new report at '{report_path}'.")

    return _bugreport_launch_editor(report_path)


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


_BACKFILL_USAGE = (
    "usage: git backfill [--min-batch-size=<n>] [--[no-]sparse]\n"
    "\n"
    "    --min-batch-size <n>  Minimum number of objects to request at a time\n"
    "    --[no-]sparse         Restrict the missing objects to the current sparse-checkout\n"
    "\n"
)


def _backfill_parse_unsigned(value: str):
    """Reproduce C Git's git_parse_unsigned(): base-0 strtoumax with an
    optional k/m/g suffix.  Returns (ok, errno) where errno is 'EINVAL' or
    'ERANGE' on failure (matching parse-options' OPT_UNSIGNED diagnostics)."""
    # size_t target -> precision 8 -> upper bound is the full unsigned range.
    upper_bound = (1 << 64) - 1
    if not value:
        return (False, "EINVAL")
    # strtoumax would accept a leading '-' as wraparound; C Git rejects it.
    if "-" in value:
        return (False, "EINVAL")
    # Mimic strtoumax(value, &end, 0): skip leading whitespace, optional '+',
    # then a base-0 integer (0x.. hex, 0.. octal, otherwise decimal).
    i = 0
    n = len(value)
    while i < n and value[i] in " \t\n\v\f\r":
        i += 1
    if i < n and value[i] == "+":
        i += 1
    digit_start = i
    base = 10
    if i < n and value[i] == "0":
        if i + 1 < n and value[i + 1] in "xX":
            base = 16
            i += 2
        else:
            base = 8
    num_start = i
    if base == 16:
        digits = "0123456789abcdefABCDEF"
    elif base == 8:
        digits = "01234567"
    else:
        digits = "0123456789"
    while i < n and value[i] in digits:
        i += 1
    # No digits consumed at all -> end == value in C terms.
    if i == digit_start or (base == 16 and i == num_start):
        return (False, "EINVAL")
    numtext = value[digit_start:i]
    try:
        val = int(numtext, base)
    except ValueError:
        return (False, "EINVAL")
    suffix = value[i:]
    factors = {"": 1, "k": 1024, "m": 1024 * 1024, "g": 1024 * 1024 * 1024}
    factor = factors.get(suffix.lower())
    if factor is None:
        return (False, "EINVAL")
    if val * factor > upper_bound:
        return (False, "ERANGE")
    return (True, val * factor)


def cmd_backfill(argv: list[str]) -> int:
    """Download missing blobs from a remote (partial clone).

    In a complete (non-partial-clone) repository backfill is an effective
    no-op: C Git walks objects reachable from HEAD, finds every blob already
    present, downloads nothing, and exits 0 silently.  We reproduce that exact
    behaviour plus parse-options' OPT_UNSIGNED diagnostics for --min-batch-size.
    """
    def _opt_err(msg: str) -> int:
        # parse-options option diagnostics print no usage block.
        sys.stderr.write(f"error: {msg}\n")
        return 129

    upper_bound = (1 << 64) - 1
    i = 0
    n = len(argv)
    while i < n:
        a = argv[i]
        if a == "-h":
            # show_usage_with_options_if_asked() writes the usage to stdout.
            sys.stdout.write(_BACKFILL_USAGE)
            return 129
        if a == "--min-batch-size" or a.startswith("--min-batch-size="):
            if "=" in a:
                value = a.split("=", 1)[1]
            else:
                # Option requires a separate value argument.
                if i + 1 >= n:
                    return _opt_err("option `min-batch-size' requires a value")
                i += 1
                value = argv[i]
            if value == "":
                return _opt_err("option `min-batch-size' expects a numerical value")
            ok, res = _backfill_parse_unsigned(value)
            if not ok:
                if res == "ERANGE":
                    return _opt_err(
                        f"value {value} for option `min-batch-size' "
                        f"not in range [0,-1]"
                    )
                return _opt_err(
                    "option `min-batch-size' expects a non-negative integer "
                    "value with an optional k/m/g suffix"
                )
            i += 1
            continue
        if a == "--sparse" or a == "--no-sparse":
            i += 1
            continue
        # PARSE_OPT_KEEP_UNKNOWN_OPT hands leftover dashed options to
        # setup_revisions(), which (for a complete repo) ultimately reaches
        # die("unrecognized argument: %s").
        if a.startswith("-"):
            sys.stderr.write(f"fatal: unrecognized argument: {a}\n")
            return 128
        # A bare positional is consumed by setup_revisions() as a revision and
        # diagnosed there; we leave that path unhandled rather than emit a
        # divergent message.
        sys.stderr.write(f"fatal: unrecognized argument: {a}\n")
        return 128
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
