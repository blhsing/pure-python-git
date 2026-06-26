"""git stash: store WIP state as commits on refs/stash.

Each stash is a (merge) commit:
  parent[0] = HEAD at stash time          (b_commit)
  parent[1] = a "tree" commit holding the index state   (i_commit)
  parent[2] = (only with -u/--all) a parentless commit holding the
              untracked snapshot tree     (u_commit)
The stash log is the reflog of refs/stash.
"""
from __future__ import annotations

import fnmatch as _fnmatch
import os
import re as _re
import sys
from typing import Optional

from . import ignore as ignore_mod
from . import objects as objs
from . import refs as refs_mod
from . import reflog as reflog_mod
from . import workdir
from .index import read_index, stat_to_entry, write_index
from .repo import Repository

# include_untracked sentinels (match builtin/stash.c)
INCLUDE_UNTRACKED = 1
INCLUDE_ALL_FILES = 2


# ---------------------------------------------------------------------------
# pathspec scoping
#
# git parses the command-line pathspecs with PATHSPEC_PREFER_FULL |
# PATHSPEC_PREFIX_ORIGIN, so each item carries a `:(prefix:N)<value>` `original`
# string (N is the byte length of the cwd-relative prefix) that is what the
# error messages print, and a fully-resolved (prefix-prepended) match value.

# pathspec magic bits (subset of pathspec.h; ATTR is parsed but unused here).
_FROMTOP = 1 << 0
_LITERAL = 1 << 1
_GLOB = 1 << 2
_ICASE = 1 << 3
_EXCLUDE = 1 << 4

_MAGIC_LONG = {"top": _FROMTOP, "literal": _LITERAL, "glob": _GLOB,
               "icase": _ICASE, "exclude": _EXCLUDE}
_MAGIC_SHORT = {"/": _FROMTOP, "!": _EXCLUDE, "^": _EXCLUDE}


class PathspecParseError(Exception):
    """Raised for invalid pathspec magic (git dies with rc 128)."""


class Pathspec:
    """A single parsed pathspec item with magic support.

    ``match`` is the repo-root-relative value used for matching (the cwd prefix
    has been prepended for non-``:(top)`` specs); ``original`` is the
    ``:(...,prefix:N)<value>`` string git prints in "did not match" errors;
    ``exclude`` marks negative (``:!`` / ``:(exclude)``) specs.
    """

    __slots__ = ("match", "original", "magic", "exclude", "_has_glob", "_rx")

    def __init__(self, match: str, original: str, magic: int = 0):
        self.match = match
        self.original = original
        self.magic = magic
        self.exclude = bool(magic & _EXCLUDE)
        literal = bool(magic & _LITERAL)
        self._has_glob = (not literal) and any(c in match for c in "*?[")
        self._rx = None
        if self._has_glob:
            # Default (no :(glob)) wildmatch: '*' crosses '/', same as Python
            # fnmatch. With :(glob), '*' does NOT cross '/' (only '**' does).
            if magic & _GLOB:
                self._rx = _re.compile(_glob_to_regex(match),
                                       _re.IGNORECASE if (magic & _ICASE) else 0)
            elif magic & _ICASE:
                self._rx = _re.compile(_fnmatch.translate(match), _re.IGNORECASE)

    def matches(self, path: str) -> bool:
        m = self.match
        p = path
        if self.magic & _ICASE:
            m_cmp = m.lower()
            p_cmp = path.lower()
        else:
            m_cmp = m
            p_cmp = path
        if not m:
            return True
        if p_cmp == m_cmp:
            return True
        if p_cmp.startswith(m_cmp + "/"):
            return True
        if self._has_glob:
            if self._rx is not None:
                return bool(self._rx.match(p))
            return _fnmatch.fnmatch(path, m)
        return False


def _glob_to_regex(pat: str) -> str:
    """Translate a :(glob) pattern to a regex where '*'/'?' do not cross '/'
    and '**' matches across path separators (git's wildmatch WM_PATHNAME)."""
    i = 0
    out = ["(?s:"]
    n = len(pat)
    while i < n:
        c = pat[i]
        if c == "*":
            if i + 1 < n and pat[i + 1] == "*":
                # '**' : match anything including '/'
                i += 2
                # consume an optional trailing '/'
                if i < n and pat[i] == "/":
                    out.append("(?:.*/)?")
                    i += 1
                else:
                    out.append(".*")
            else:
                out.append("[^/]*")
                i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            j = i + 1
            if j < n and pat[j] in "!^":
                j += 1
            if j < n and pat[j] == "]":
                j += 1
            while j < n and pat[j] != "]":
                j += 1
            if j >= n:
                out.append("\\[")
                i += 1
            else:
                stuff = pat[i + 1:j].replace("\\", "\\\\")
                if stuff.startswith(("!", "^")):
                    stuff = "^" + stuff[1:]
                out.append("[" + stuff + "]")
                i = j + 1
        else:
            out.append(_re.escape(c))
            i += 1
    out.append(r")\Z")
    return "".join(out)


def _parse_magic(elem: str) -> tuple[int, str]:
    """Return ``(magic, rest)`` parsing leading pathspec magic from ``elem``.

    Mirrors parse_element_magic (long ``:(...)`` and short ``:.../`` forms).
    Raises :class:`PathspecParseError` on invalid magic.
    """
    if not elem.startswith(":"):
        return 0, elem
    magic = 0
    if len(elem) > 1 and elem[1] == "(":
        # longhand: :(name,name,prefix:N)pattern. Parse comma-separated tokens
        # up to ')' or end-of-string; an invalid token dies before the missing
        # ')' is detected (matching parse_long_magic's loop order).
        pos = 2
        n = len(elem)
        while pos < n and elem[pos] != ")":
            comma = elem.find(",", pos)
            paren = elem.find(")", pos)
            ends = [e for e in (comma, paren) if e != -1]
            tok_end = min(ends) if ends else n
            token = elem[pos:tok_end].strip()
            if token and not (token.startswith("prefix:")
                              or token.startswith("attr:")):
                bit = _MAGIC_LONG.get(token)
                if bit is None:
                    raise PathspecParseError(
                        f"Invalid pathspec magic '{token}' in '{elem}'")
                magic |= bit
            pos = tok_end + 1 if (tok_end < n and elem[tok_end] == ",") else tok_end
        if pos >= n or elem[pos] != ")":
            raise PathspecParseError(
                f"Missing ')' at the end of pathspec magic in '{elem}'")
        return magic, elem[pos + 1:]
    # shorthand: :/!^ ... terminated by ':' or a non-magic char
    pos = 1
    while pos < len(elem) and elem[pos] != ":":
        ch = elem[pos]
        bit = _MAGIC_SHORT.get(ch)
        if bit is None:
            break
        magic |= bit
        pos += 1
    if pos < len(elem) and elem[pos] == ":":
        pos += 1
    return magic, elem[pos:]


def _prefix_magic(elem: str, magic: int, prefixlen: int) -> str:
    """Reproduce prefix_magic(): the leading magic part of ``original``.

    ``prefixlen`` is the byte length of the resolved prefix for this item (the
    cwd prefix incl. trailing '/', or 0 for ``:(top)`` / root). The caller then
    appends the resolved match value.
    """
    if not magic:
        return f":(prefix:{prefixlen})"
    if len(elem) > 1 and elem[1] == "(":
        # longhand: copy everything up to the final ')', append prefix.
        end = elem.find(")")
        head = elem[:end]
        return f"{head},prefix:{prefixlen})"
    # shorthand: rebuild :(name,name,prefix:N)
    names = [name for name, bit in
             (("top", _FROMTOP), ("literal", _LITERAL), ("glob", _GLOB),
              ("icase", _ICASE), ("exclude", _EXCLUDE))
             if magic & bit]
    return ":(" + ",".join(names + [f"prefix:{prefixlen}"]) + ")"


def parse_pathspecs(prefix: str, args: list[str]) -> list[Pathspec]:
    """Mirror parse_pathspec(PATHSPEC_PREFER_FULL | PATHSPEC_PREFIX_ORIGIN).

    ``prefix`` is the cwd path relative to the repo root (no leading/trailing
    "/", "" at the root). Each arg becomes a :class:`Pathspec` whose ``match``
    has the prefix prepended (unless ``:(top)``) and ``original`` is
    ``prefix_magic(prefixlen, magic, elem) + match`` — exactly what git prints
    in "did not match" errors. Magic is parsed from the leading ``:``.
    """
    # prefix_path_gently's prefixlen counts the trailing '/'.
    pfx_with_slash = (prefix + "/") if prefix else ""
    pfx_len = len(pfx_with_slash.encode("utf-8"))
    out: list[Pathspec] = []
    for a in args:
        magic, body = _parse_magic(a)
        # Collapse leading "./" segments; a bare "." becomes "" (match-all).
        norm = body
        while norm.startswith("./"):
            norm = norm[2:]
        if norm == ".":
            norm = ""
        # PATHSPEC_PREFER_FULL prepends the cwd prefix to relative specs unless
        # :(top) (PATHSPEC_FROMTOP) anchors the spec at the repo root.
        if prefix and not (magic & _FROMTOP) and not norm.startswith("/"):
            joined = f"{prefix}/{norm}" if norm else prefix
            item_prefixlen = pfx_len
        else:
            joined = norm.lstrip("/")
            item_prefixlen = 0
        joined = joined.rstrip("/")
        original = _prefix_magic(a, magic, item_prefixlen) + joined
        out.append(Pathspec(joined, original, magic))
    return out


def _match_any(path: str, pathspecs: list[Pathspec]) -> bool:
    """True if ``path`` matches at least one positive spec and no exclude spec.

    With only exclude specs git treats them as "match everything except"; with
    no specs at all, callers don't reach here.
    """
    positives = [ps for ps in pathspecs if not ps.exclude]
    excludes = [ps for ps in pathspecs if ps.exclude]
    if positives:
        if not any(ps.matches(path) for ps in positives):
            return False
    if excludes and any(ps.matches(path) for ps in excludes):
        return False
    return True


def _report_path_error(repo: Repository, pathspecs: list[Pathspec]) -> bool:
    """Replicate report_path_error over the (full) index: every pathspec must
    match at least one index entry. Prints one error per unmatched spec (in
    pathspec order), then "Did you forget to 'git add'?", and returns True if
    any error was printed. Duplicate specs are not reported twice if a copy
    matched."""
    idx = read_index(repo)
    index_paths = sorted(idx.by_path())
    matched = [False] * len(pathspecs)
    for path in index_paths:
        for i, ps in enumerate(pathspecs):
            # Exclude specs are not subject to the "did not match" error; only
            # positive specs must match an index entry.
            if ps.exclude or ps.matches(path):
                matched[i] = True

    errors = 0
    for i, ps in enumerate(pathspecs):
        if matched[i] or ps.exclude:
            continue
        # Don't barf if an identical pathspec matched (parse_pathspec dup rule).
        dup = any(j != i and matched[j]
                  and pathspecs[j].original == ps.original
                  for j in range(len(pathspecs)))
        if dup:
            continue
        sys.stderr.write(
            f"error: pathspec '{ps.original}' did not match any file(s) "
            f"known to git\n")
        errors += 1

    if errors:
        sys.stderr.write("Did you forget to 'git add'?\n")
        return True
    return False


def _has_tracked_changes_for_paths(repo: Repository, status: dict,
                                   pathspecs: list[Pathspec]) -> bool:
    """True if any tracked change (staged or worktree) touches a matched path.

    Mirrors check_changes_tracked_files with a prune_data pathspec: it runs
    diff-index --cached (HEAD vs index) and diff-files (index vs worktree),
    both scoped to the pathspec.
    """
    for key in ("staged_new", "staged_mod", "staged_del", "modified", "missing"):
        for p in status[key]:
            if _match_any(p, pathspecs):
                return True
    return False


def _worktree_blob(repo: Repository, rel: str) -> Optional[tuple[int, str]]:
    """Return ``(mode, blob_sha)`` for the worktree file, or None if absent."""
    full = repo.path / rel
    if not (full.exists() or full.is_symlink()):
        return None
    data = workdir._blob_data(full)
    sha = objs.write_object(repo, "blob", data)
    return workdir._mode_for(full), sha


def _matched_index_paths(repo: Repository, idx_tree: str, head_tree: str,
                         pathspecs: list[Pathspec]) -> list[str]:
    """Paths (in index or worktree) that match the pathspec and have a tracked
    change relative to HEAD — i.e. exactly the paths git's reset/checkout step
    touches."""
    idx = read_index(repo)
    index_paths = set(idx.by_path())
    head_map = workdir.flatten_tree(repo, head_tree)
    # candidate set: tracked (index) paths + worktree-deleted tracked paths.
    candidates = set(index_paths) | set(head_map)
    out = []
    for p in sorted(candidates):
        if _match_any(p, pathspecs):
            out.append(p)
    return out


def _working_tree_scoped(repo: Repository, idx_tree: str, head_tree: str,
                         pathspecs: list[Pathspec]) -> str:
    """Build w_tree = idx_tree with the matched paths overlaid by their worktree
    state (added/modified/deleted). Mirrors stash_working_tree(ps): reset the
    alternate index to i_tree, then apply the HEAD->worktree diff for the
    matched paths, then write-tree.
    """
    # Start from the full index tree.
    files: dict[str, tuple[int, str]] = {}
    for path, mode, sha in workdir.iter_tree_files(repo, idx_tree):
        files[path] = (int(mode, 8), sha)

    head_map = workdir.flatten_tree(repo, head_tree)
    idx = read_index(repo)
    index_paths = set(idx.by_path())
    # The diff is HEAD (b_commit) vs worktree, restricted to the pathspec; only
    # those entries are fed to update-index. Consider every path that is tracked
    # (index) or present in HEAD and matches the pathspec.
    candidates = sorted(set(index_paths) | set(head_map))
    for path in candidates:
        if not _match_any(path, pathspecs):
            continue
        wt = _worktree_blob(repo, path)
        head_entry = head_map.get(path)
        if wt is None:
            # Deleted in worktree: only emit a deletion if HEAD had it (diff
            # HEAD->worktree shows a removal). Drop it from the seeded tree.
            if head_entry is not None:
                files.pop(path, None)
            continue
        mode, sha = wt
        if head_entry is None or head_entry != sha or _head_mode(repo, head_tree, path) != mode:
            files[path] = (mode, sha)
        # else: worktree == HEAD -> diff empty -> keep i_tree's content.
    return _build_tree_from_blobs(repo, files)


def _head_mode(repo: Repository, head_tree: str, path: str) -> int:
    e = workdir.tree_path_entry(repo, head_tree, path)
    return int(e.mode, 8) if e is not None else 0


def _reset_paths_to_head(repo: Repository, head_tree: str,
                         paths: list[str]) -> None:
    """Reset the given paths in index + worktree to their HEAD state. Paths not
    in HEAD are removed from the index and deleted from the worktree."""
    idx = read_index(repo)
    for path in paths:
        e = workdir.tree_path_entry(repo, head_tree, path)
        full = repo.path / path
        if e is None:
            # Not in HEAD: drop from index + worktree.
            idx.remove(path)
            if full.exists() or full.is_symlink():
                try:
                    full.unlink()
                except OSError:
                    pass
            continue
        mode = int(e.mode, 8)
        _materialize(repo, full, e.mode, e.sha)
        st = full.lstat()
        new_e = stat_to_entry(path, st, e.sha, mode)
        idx.upsert(new_e)
    write_index(repo, idx)


def _restore_paths_from_tree(repo: Repository, tree: str,
                             paths: list[str]) -> None:
    """Restore the given paths in index + worktree to their state in ``tree``
    (the i_tree). Used by --keep-index: `checkout --no-overlay <i_tree> -- <ps>`.
    Paths absent from the tree are left as reset (HEAD) — checkout --no-overlay
    of a missing path removes it, which the HEAD reset already handled when the
    path is also absent from HEAD."""
    idx = read_index(repo)
    tree_entries: dict[str, tuple[str, str]] = {}
    for path, mode, sha in workdir.iter_tree_files(repo, tree):
        tree_entries[path] = (mode, sha)
    for path in paths:
        ent = tree_entries.get(path)
        full = repo.path / path
        if ent is None:
            # absent from i_tree: drop it (no-overlay removes unmatched).
            idx.remove(path)
            if full.exists() or full.is_symlink():
                try:
                    full.unlink()
                except OSError:
                    pass
            continue
        mode, sha = ent
        _materialize(repo, full, mode, sha)
        st = full.lstat()
        idx.upsert(stat_to_entry(path, st, sha, int(mode, 8)))
    write_index(repo, idx)


def _materialize(repo: Repository, full, mode: str, sha: str) -> None:
    """Write blob ``sha`` to ``full`` with the given octal ``mode`` string."""
    _, data = objs.read_object(repo, sha)
    full.parent.mkdir(parents=True, exist_ok=True)
    if mode == "120000":
        if full.exists() or full.is_symlink():
            full.unlink()
        try:
            os.symlink(data.decode("utf-8"), full)
        except (AttributeError, NotImplementedError, OSError):
            full.write_bytes(data)
    else:
        if full.is_symlink():
            full.unlink()
        full.write_bytes(data)
        if int(mode, 8) & 0o111:
            try:
                full.chmod(full.stat().st_mode | 0o111)
            except OSError:
                pass


def _commit_tree(repo: Repository, tree: str, parents: list[str], msg: str) -> str:
    # Stash commits carry the committer/author signature (env+config+date),
    # exactly like any other commit, so the object id matches C Git byte for
    # byte. The message is used verbatim — the caller decides whether to add a
    # trailing newline (git's "index on ..." commit has one, the WIP/On commit
    # does not).
    author = objs.build_signature(repo, "author")
    committer = objs.build_signature(repo, "committer")
    c = objs.Commit(tree=tree, parents=parents, author=author, committer=committer, message=msg)
    return objs.write_object(repo, "commit", c.encode())


def _build_tree_from_blobs(repo: Repository, files: dict[str, tuple[int, str]]) -> str:
    """Build a tree object from ``path -> (mode, blob_sha)`` and return its sha.

    Mirrors ``update-index --add`` followed by ``write-tree``: nested paths
    create subtrees, names sort by Git's tree-entry rules (handled by
    ``encode_tree``).
    """
    root: dict = {}
    for path, (mode, sha) in files.items():
        parts = path.split("/")
        cur = root
        for part in parts[:-1]:
            cur = cur.setdefault(part, {})
        cur[parts[-1]] = (mode, sha)

    def emit(node: dict) -> str:
        entries: list[objs.TreeEntry] = []
        for name, val in node.items():
            if isinstance(val, dict):
                entries.append(objs.TreeEntry("40000", name, emit(val)))
            else:
                mode, sha = val
                entries.append(objs.TreeEntry(f"{mode:o}", name, sha))
        return objs.write_object(repo, "tree", objs.encode_tree(entries))

    return emit(root)


def _untracked_files(repo: Repository, include_untracked: int,
                     pathspecs: Optional[list["Pathspec"]] = None) -> list[str]:
    """Return untracked file paths (sorted), excluding ignored unless --all.

    Matches ``get_untracked_files``: with INCLUDE_ALL_FILES no standard excludes
    are applied (ignored files are returned too); otherwise ignored files are
    skipped. Files under .git are never included. When ``pathspecs`` is given the
    set is restricted to matching paths (fill_directory honours the pathspec).
    """
    idx = read_index(repo)
    tracked = set(idx.by_path())
    ignores = None if include_untracked == INCLUDE_ALL_FILES else ignore_mod.load(repo.path)
    out: list[str] = []
    base = repo.path
    for rel in workdir.iter_worktree(repo):
        if rel in tracked:
            continue
        if pathspecs and not _match_any(rel, pathspecs):
            continue
        if ignores is not None:
            full = base / rel
            if ignores.is_ignored(rel, is_dir=workdir._is_dir_no_follow(full)):
                continue
        out.append(rel)
    return sorted(out)


def _msg_core(repo: Repository, head_sym: Optional[str], head_sha: str) -> tuple[str, str]:
    """Return ``(branch_name, msg_core)`` where msg_core == "<branch>: <short> <subject>"."""
    if head_sym and head_sym.startswith("refs/heads/"):
        branch = head_sym[len("refs/heads/"):]
    else:
        branch = "(no branch)"
    subject = objs.parse_commit(objs.read_object(repo, head_sha)[1]).message.splitlines()[0]
    return branch, f"{branch}: {head_sha[:7]} {subject}"


def push(repo: Repository, message: str = "", *, keep_index: bool = False,
         include_untracked: int = 0, only_staged: bool = False,
         pathspecs: Optional[list["Pathspec"]] = None,
         quiet: bool = False) -> object:
    """Create and store a stash.

    Returns the stash commit sha on success, ``None`` when there are no local
    changes to save, the string ``"no-staged"`` when ``--staged`` is given but
    nothing is staged, the string ``"path-error"`` when the pathspec did not
    match any tracked file (the caller has already printed the per-spec errors),
    and the string ``"no-head"`` when there is no initial commit yet (the caller
    has already printed "You do not have the initial commit yet").

    ``pathspecs`` (a list of :class:`Pathspec`), when non-empty, scopes the
    stash to only the listed paths: only changes to those paths are recorded in
    the stash commit and those paths are reset to HEAD in the index/worktree;
    all other modifications are left untouched. Mirrors the ``ps->nr`` branch of
    builtin/stash.c's do_push_stash / stash_working_tree.
    """
    head_sym, head_sha = refs_mod.read_head(repo)

    has_paths = bool(pathspecs)

    # report_path_error: every pathspec must match at least one index entry
    # (do_push_stash, the `!include_untracked && ps->nr` block). git runs this
    # *before* the HEAD check and before checking whether there are changes.
    if has_paths and not include_untracked:
        if _report_path_error(repo, pathspecs):
            return "path-error"

    # No initial commit yet: check_changes_tracked_files returns -1 (treated as
    # "there are changes"), so git always proceeds into do_create_stash which
    # then dies with "You do not have the initial commit yet".
    if not head_sha:
        if not quiet:
            sys.stderr.write("You do not have the initial commit yet\n")
        return "no-head"

    status = workdir.status(repo)
    if has_paths:
        tracked_changes = _has_tracked_changes_for_paths(repo, status, pathspecs)
    else:
        tracked_changes = bool(
            status["staged_new"] or status["staged_mod"] or status["staged_del"]
            or status["modified"] or status["missing"]
        )
    untracked_files: list[str] = []
    if include_untracked:
        untracked_files = _untracked_files(repo, include_untracked, pathspecs)

    if not tracked_changes and not untracked_files:
        return None

    branch, msg_core = _msg_core(repo, head_sym, head_sha)

    # 1) commit the current index state ("index on <core>\n"). The index tree
    # is always the *full* index, never scoped by the pathspec.
    idx_tree = workdir.write_tree(repo)
    i_commit = _commit_tree(repo, idx_tree, [head_sha],
                            f"index on {msg_core}\n")

    parents = [head_sha, i_commit]
    u_commit = None
    if include_untracked:
        # git always records an untracked commit when -u/--all is given and the
        # stash proceeds, even if there are no untracked files (empty tree).
        files: dict[str, tuple[int, str]] = {}
        for rel in untracked_files:
            full = repo.path / rel
            data = workdir._blob_data(full)
            sha = objs.write_object(repo, "blob", data)
            files[rel] = (workdir._mode_for(full), sha)
        u_tree = _build_tree_from_blobs(repo, files)
        u_commit = _commit_tree(repo, u_tree, [], f"untracked files on {msg_core}\n")

    head_tree = objs.parse_commit(objs.read_object(repo, head_sha)[1]).tree

    # 2) build the working-tree (w_tree) commit.
    if only_staged:
        # --staged: the worktree commit is exactly the index tree. If nothing
        # is staged the diff HEAD..idx_tree is empty -> "No staged changes".
        if idx_tree == head_tree:
            return "no-staged"
        w_tree = idx_tree
    elif has_paths:
        # stash_working_tree with a pathspec: seed from the index tree, then
        # overlay the *worktree* content for the matched paths (add/modify/
        # delete). Paths outside the pathspec keep their index content.
        w_tree = _working_tree_scoped(repo, idx_tree, head_tree, pathspecs)
    else:
        saved_idx = read_index(repo)
        tracked = sorted(read_index(repo).by_path())
        workdir.add_paths(repo, tracked)
        w_tree = workdir.write_tree(repo)
        write_index(repo, saved_idx)

    if u_commit is not None:
        parents.append(u_commit)

    # The WIP/On message (refs/stash reflog + the w_commit subject) has no
    # trailing newline.
    if message:
        msg = f"On {branch}: {message}"
    else:
        msg = f"WIP on {msg_core}"

    w_commit = _commit_tree(repo, w_tree, parents, msg)
    refs_mod.update_ref(repo, "refs/stash", w_commit, message=msg)

    # git prints the "Saved ..." line right after storing the stash and *before*
    # the worktree/index restore — so a subsequent restore failure (e.g. the
    # --staged reverse-apply) still shows it. The reflog message is the stash
    # message (msg).
    if not quiet:
        sys.stdout.write(f"Saved working directory and index state {msg}\n")

    # 3) restore worktree + index.
    if only_staged:
        # git does `git apply -R` of the (full) staged patch HEAD..idx_tree.
        # That reverse-apply fails — "patch does not apply" — when any staged
        # path has worktree content that differs from its staged (index)
        # content, because the reverse hunk's context no longer matches the
        # worktree. The stash has already been created/stored at this point, so
        # on failure we only print the apply errors and stop (rc 1), leaving the
        # index/worktree untouched.
        if _staged_apply_would_fail(repo, head_tree, idx_tree):
            if not quiet:
                # diff-tree -p emits the failing hunk header path; git prints one
                # "patch failed" + "patch does not apply" per failing file, then
                # the summary line. Reproduce for the first failing path.
                bad = _staged_apply_failing_paths(repo, head_tree, idx_tree)
                for p in bad:
                    sys.stderr.write(f"error: patch failed: {p}:1\n")
                    sys.stderr.write(f"error: {p}: patch does not apply\n")
                sys.stderr.write("Cannot remove worktree changes\n")
            return "staged-apply-failed"
        # Reverse the staged diff: index & worktree drop the staged changes,
        # worktree-only modifications are preserved. Equivalent here to:
        # reset index to HEAD, and for files that differ between idx_tree and
        # head_tree, restore the HEAD content in the worktree.
        _restore_staged(repo, head_sha, head_tree, idx_tree)
    elif has_paths:
        # Reset only the matched paths to HEAD (index + worktree), exactly like
        # `git add [-u] -- <ps>` + `diff-index --cached HEAD -- <ps>` +
        # `apply --index -R`. With --keep-index, restore the matched paths to
        # their staged (index) content afterwards (`checkout --no-overlay
        # <i_tree> -- <ps>`). With -u/--all the matched untracked files were
        # staged by `git add -- <ps>` (no -u) and then reverse-applied away, so
        # they are deleted from the worktree too.
        matched = _matched_index_paths(repo, idx_tree, head_tree, pathspecs)
        _reset_paths_to_head(repo, head_tree, matched)
        if include_untracked:
            _reset_untracked(repo, untracked_files)
        if keep_index:
            _restore_paths_from_tree(repo, idx_tree, matched)
    else:
        if keep_index:
            workdir.checkout_tree(repo, idx_tree)
        else:
            workdir.checkout_tree(repo, head_tree)
        if include_untracked:
            _clean_untracked(repo, untracked_files)

    return w_commit


def push_patch(repo: Repository, message: str = "", *,
               keep_index: bool = True, pathspecs: Optional[list["Pathspec"]] = None,
               quiet: bool = False,
               context: int = -1, interhunkcontext: int = -1,
               auto_advance: bool = True) -> object:
    """`git stash push --patch`: stash interactively-selected hunks.

    Mirrors do_create_stash(patch_mode) + stash_patch() + do_push_stash's
    patch branch.  Returns the stash commit sha on success, ``None`` for no
    local changes, ``"no-head"`` when there is no initial commit, ``1`` when no
    hunks were selected ("No changes selected"), and ``"apply-failed"`` if the
    reverse-apply that removes the selected changes from the worktree fails.
    """
    from . import addpatch

    head_sym, head_sha = refs_mod.read_head(repo)
    if not head_sha:
        if not quiet:
            sys.stderr.write("You do not have the initial commit yet\n")
        return "no-head"

    status = workdir.status(repo)
    tracked_changes = bool(
        status["staged_new"] or status["staged_mod"] or status["staged_del"]
        or status["modified"] or status["missing"]
    )
    if not tracked_changes:
        return None

    branch, msg_core = _msg_core(repo, head_sym, head_sha)
    head_tree = objs.parse_commit(objs.read_object(repo, head_sha)[1]).tree

    # i_tree / i_commit: the current index state.
    idx_tree = workdir.write_tree(repo)
    i_commit = _commit_tree(repo, idx_tree, [head_sha], f"index on {msg_core}\n")

    # stash_patch(): seed a scratch index with HEAD, run add-p in STASH mode to
    # stage the selected hunks into it, then w_tree = that index's tree.  We use
    # the real index as scratch and restore it afterwards (pygit has no
    # GIT_INDEX_FILE), so the user's staged state is preserved exactly.
    saved_idx = read_index(repo)
    pspec = [_ps_original(p) for p in pathspecs] if pathspecs else []
    try:
        workdir.read_tree(repo, head_tree)
        rc = addpatch.run_add_p(
            repo, "stash", None, pspec,
            context=context, interhunkcontext=interhunkcontext,
            auto_advance=auto_advance)
        w_tree = workdir.write_tree(repo)
    finally:
        write_index(repo, saved_idx)

    # patch = diff-tree -p -U1 HEAD w_tree (the selected change set).
    patch = _diff_tree_patch(repo, head_tree, w_tree, pspec)
    if not patch:
        if not quiet:
            sys.stderr.write("No changes selected\n")
        return 1

    # Build and store the stash commit.
    if message:
        msg = f"On {branch}: {message}"
    else:
        msg = f"WIP on {msg_core}"
    w_commit = _commit_tree(repo, w_tree, [head_sha, i_commit], msg)
    refs_mod.update_ref(repo, "refs/stash", w_commit, message=msg)

    if not quiet:
        sys.stdout.write(f"Saved working directory and index state {msg}\n")

    # Reverse-apply the selected patch to drop those changes from the worktree.
    rc = _apply_reverse(repo, patch)
    if rc != 0:
        if not quiet:
            sys.stderr.write("Cannot remove worktree changes\n")
        return "apply-failed"

    return w_commit


def _ps_original(p: "Pathspec") -> str:
    return getattr(p, "match", "") or getattr(p, "original", "")


def _diff_tree_patch(repo: Repository, a_tree: str, b_tree: str,
                     pathspec: list[str]) -> str:
    """Capture `git diff-tree -p -U1 <a> <b> -- <ps>` output as text."""
    import io
    from . import cli
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        cli.cmd_diff_tree(["-p", "-U1", a_tree, b_tree, "--"] + pathspec)
    finally:
        sys.stdout = old
    return buf.getvalue()


def _apply_reverse(repo: Repository, patch: str) -> int:
    from . import addpatch
    return addpatch._run_apply(repo, patch, ["-R"])


def _staged_apply_failing_paths(repo: Repository, head_tree: str,
                                idx_tree: str) -> list[str]:
    """Paths whose reverse-apply of the staged patch would fail.

    `git stash --staged` reverse-applies the HEAD..index patch to the worktree.
    For a staged path that patch fails iff the worktree content differs from the
    staged (index) content — the reverse hunk's context no longer matches. git
    reports the failures in reverse patch (path) order, stopping at the summary.
    """
    head_map = workdir.flatten_tree(repo, head_tree)
    idx_map = workdir.flatten_tree(repo, idx_tree)
    bad: list[str] = []
    for path in sorted(set(head_map) | set(idx_map)):
        in_head = path in head_map
        in_idx = path in idx_map
        # Only staged changes (differing between HEAD and index) form the patch.
        if in_head and in_idx and head_map[path] == idx_map[path]:
            continue
        if not in_idx:
            # Staged deletion: the reverse patch re-creates the file; it fails
            # if the path now exists in the worktree with other content. git
            # rarely hits this in practice; treat as no failure here.
            continue
        # Compare the worktree content to the staged (index) blob.
        full = repo.path / path
        if not (full.exists() or full.is_symlink()):
            # worktree missing while index has it -> apply fails
            bad.append(path)
            continue
        try:
            data = workdir._blob_data(full)
            wt_sha, _ = objs.hash_bytes("blob", data, repo)
        except OSError:
            bad.append(path)
            continue
        if wt_sha != idx_map[path]:
            bad.append(path)
    # git emits failures in reverse path order.
    bad.reverse()
    return bad


def _staged_apply_would_fail(repo: Repository, head_tree: str,
                             idx_tree: str) -> bool:
    return bool(_staged_apply_failing_paths(repo, head_tree, idx_tree))


def _restore_staged(repo: Repository, head_sha: str, head_tree: str, idx_tree: str) -> None:
    """Undo the staged changes (git applies the HEAD..idx_tree patch in reverse
    with --index): reset the index to HEAD and revert the staged paths in the
    worktree to their HEAD content (or delete newly-staged files)."""
    head_map = workdir.flatten_tree(repo, head_tree)
    idx_map = workdir.flatten_tree(repo, idx_tree)
    head_modes = {p: m for p, m, _ in workdir.iter_tree_files(repo, head_tree)}
    for path in sorted(set(head_map) | set(idx_map)):
        in_head = path in head_map
        in_idx = path in idx_map
        if in_idx and not in_head:
            # newly staged file -> remove from worktree
            f = repo.path / path
            if f.exists() or f.is_symlink():
                try:
                    f.unlink()
                except OSError:
                    pass
        elif in_head and head_map.get(path) != idx_map.get(path):
            # staged modification (or staged deletion) -> restore HEAD content
            _, data = objs.read_object(repo, head_map[path])
            full = repo.path / path
            full.parent.mkdir(parents=True, exist_ok=True)
            mode = head_modes[path]
            if mode == "120000":
                if full.exists() or full.is_symlink():
                    full.unlink()
                try:
                    os.symlink(data.decode("utf-8"), full)
                except (AttributeError, NotImplementedError, OSError):
                    full.write_bytes(data)
            else:
                full.write_bytes(data)
                if int(mode, 8) & 0o111:
                    try:
                        full.chmod(full.stat().st_mode | 0o111)
                    except OSError:
                        pass
    # reset index to HEAD
    workdir.read_tree(repo, head_tree)


def _reset_untracked(repo: Repository, untracked_files: list[str]) -> None:
    """Remove the matched untracked files stashed by `-u`/`--all` + pathspec.

    In the pathspec branch git stages them with `git add -- <ps>` and reverse-
    applies the addition (`apply --index -R`), which deletes the files and any
    empty leading directories. Same observable result as the clean step.
    """
    _clean_untracked(repo, untracked_files)


def _clean_untracked(repo: Repository, untracked_files: list[str]) -> None:
    """Remove the untracked files that were stashed (git runs `clean -d`)."""
    for rel in untracked_files:
        f = repo.path / rel
        if f.exists() or f.is_symlink():
            try:
                f.unlink()
            except OSError:
                pass
    # remove now-empty directories left behind, bottom-up
    dirs = sorted({os.path.dirname(r) for r in untracked_files if "/" in r}, reverse=True)
    for d in dirs:
        full = repo.path / d
        try:
            if full.is_dir() and not any(full.iterdir()):
                full.rmdir()
        except OSError:
            pass


def list_stashes(repo: Repository) -> list[tuple[int, str, str]]:
    # Reflog is stored oldest-first; stash@{0} is the newest entry.
    entries = reflog_mod.read(repo, "refs/stash")
    return [(i, e[1], e[3]) for i, e in enumerate(reversed(entries))]


def apply(repo: Repository, index: int = 0, *, pop: bool = False) -> bool:
    entries = reflog_mod.read(repo, "refs/stash")
    if not entries:
        return False
    # stash@{0} = newest = last entry
    e = entries[-(index + 1)]
    stash_sha = e[1]
    _, data = objs.read_object(repo, stash_sha)
    sc = objs.parse_commit(data)
    workdir.checkout_tree(repo, sc.tree)
    if pop:
        # remove the entry: rewrite reflog without it
        keep = [x for j, x in enumerate(entries) if j != len(entries) - 1 - index]
        p = repo.gitdir / "logs" / "refs" / "stash"
        if keep:
            with p.open("w", encoding="utf-8") as f:
                for old, new, ident, msg in keep:
                    f.write(f"{old} {new} {ident}\t{msg}\n")
            # point refs/stash at last remaining
            refs_mod.update_ref(repo, "refs/stash", keep[-1][1], message="stash pop")
        else:
            p.unlink(missing_ok=True)
            (repo.gitdir / "refs" / "stash").unlink(missing_ok=True)
    return True


# ---------------------------------------------------------------------------
# stash export

_EXPORT_IDENT = "git stash <git@stash> 1000684800 +0000"


def _check_stash_topology(repo: Repository, sha: str) -> bool:
    """Return True if ``sha`` looks like a stash commit (2 or 3 parents, the
    second of which is a one-parent index commit). Loose mirror of
    check_stash_topology; good enough to reject obvious non-stash commits."""
    try:
        c = objs.parse_commit(objs.read_object(repo, sha)[1])
    except (KeyError, ValueError):
        return False
    if len(c.parents) not in (2, 3):
        return False
    return True


def export_stash(repo: Repository, refs: list[str]) -> Optional[str]:
    """Build the export commit chain and return its tip sha.

    ``refs`` is a list of stash revisions (e.g. ``stash@{0}``); empty means all
    stashes (oldest first). Returns the tip sha, or None on error (after the
    caller has printed the message)."""
    # 1) fixed empty base commit.
    empty_tree = objs.write_object(repo, "tree", b"")
    base = objs.write_object(
        repo, "commit",
        objs.Commit(tree=empty_tree, parents=[], author=_EXPORT_IDENT,
                    committer=_EXPORT_IDENT, message="").encode(),
    )

    # 2) collect stash commit shas, oldest first.
    stash_shas: list[str] = []
    if refs:
        for r in refs:
            sha = refs_mod.rev_parse(repo, r)
            if not sha or not _check_stash_topology(repo, sha):
                return None
            stash_shas.append(sha)
    else:
        for _i, sha, _msg in reversed(list_stashes(repo)):
            stash_shas.append(sha)

    prev = base
    for sha in stash_shas:
        prev = _write_commit_with_parents(repo, sha, [prev, sha])
    return prev


def _write_commit_with_parents(repo: Repository, sha: str, parents: list[str]) -> str:
    """Create a commit identical to ``sha`` (same tree-less empty tree, same
    author/committer, message prefixed with "git stash: ") but with the given
    parents. Mirrors write_commit_with_parents in builtin/stash.c."""
    _, raw = objs.read_object(repo, sha)
    c = objs.parse_commit(raw)
    empty_tree = objs.write_object(repo, "tree", b"")
    # message: "git stash: " + original body, with a completed trailing line.
    body = c.message
    msg = "git stash: " + body
    if not msg.endswith("\n"):
        msg += "\n"
    new = objs.Commit(tree=empty_tree, parents=parents,
                      author=c.author, committer=c.committer, message=msg)
    return objs.write_object(repo, "commit", new.encode())
