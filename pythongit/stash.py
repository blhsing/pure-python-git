"""git stash: store WIP state as commits on refs/stash.

Each stash is a (merge) commit:
  parent[0] = HEAD at stash time          (b_commit)
  parent[1] = a "tree" commit holding the index state   (i_commit)
  parent[2] = (only with -u/--all) a parentless commit holding the
              untracked snapshot tree     (u_commit)
The stash log is the reflog of refs/stash.
"""
from __future__ import annotations

import os
from typing import Optional

from . import ignore as ignore_mod
from . import objects as objs
from . import refs as refs_mod
from . import reflog as reflog_mod
from . import workdir
from .index import read_index, write_index
from .repo import Repository

# include_untracked sentinels (match builtin/stash.c)
INCLUDE_UNTRACKED = 1
INCLUDE_ALL_FILES = 2


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


def _untracked_files(repo: Repository, include_untracked: int) -> list[str]:
    """Return untracked file paths (sorted), excluding ignored unless --all.

    Matches ``get_untracked_files``: with INCLUDE_ALL_FILES no standard excludes
    are applied (ignored files are returned too); otherwise ignored files are
    skipped. Files under .git are never included.
    """
    idx = read_index(repo)
    tracked = set(idx.by_path())
    ignores = None if include_untracked == INCLUDE_ALL_FILES else ignore_mod.load(repo.path)
    out: list[str] = []
    base = repo.path
    for rel in workdir.iter_worktree(repo):
        if rel in tracked:
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
         include_untracked: int = 0, only_staged: bool = False) -> object:
    """Create and store a stash.

    Returns the stash commit sha on success, ``None`` when there are no local
    changes to save, and the string ``"no-staged"`` when ``--staged`` is given
    but nothing is staged.
    """
    head_sym, head_sha = refs_mod.read_head(repo)
    if not head_sha:
        return None

    status = workdir.status(repo)
    tracked_changes = bool(
        status["staged_new"] or status["staged_mod"] or status["staged_del"]
        or status["modified"] or status["missing"]
    )
    untracked_files: list[str] = []
    if include_untracked:
        untracked_files = _untracked_files(repo, include_untracked)

    if not tracked_changes and not untracked_files:
        return None

    branch, msg_core = _msg_core(repo, head_sym, head_sha)

    # 1) commit the current index state ("index on <core>\n").
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

    # 2) build the working-tree (w_tree) commit.
    if only_staged:
        # --staged: the worktree commit is exactly the index tree. If nothing
        # is staged the diff HEAD..idx_tree is empty -> "No staged changes".
        head_tree = objs.parse_commit(objs.read_object(repo, head_sha)[1]).tree
        if idx_tree == head_tree:
            return "no-staged"
        w_tree = idx_tree
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

    # 3) restore worktree + index.
    head_tree = objs.parse_commit(objs.read_object(repo, head_sha)[1]).tree
    if only_staged:
        # Reverse the staged diff: index & worktree drop the staged changes,
        # worktree-only modifications are preserved. Equivalent here to:
        # reset index to HEAD, and for files that differ between idx_tree and
        # head_tree, restore the HEAD content in the worktree.
        _restore_staged(repo, head_sha, head_tree, idx_tree)
    else:
        if keep_index:
            workdir.checkout_tree(repo, idx_tree)
        else:
            workdir.checkout_tree(repo, head_tree)
        if include_untracked:
            _clean_untracked(repo, untracked_files)

    return w_commit


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
