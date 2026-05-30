"""Sequencer operations: cherry-pick, revert, rebase.

These all reduce to applying the tree difference between two commits onto
the current HEAD via a three-way merge (with HEAD as the merge base for
cherry-pick of a non-merge commit).
"""
from __future__ import annotations

import time
from typing import Optional

from . import merge as merge_mod
from . import objects as objs
from . import refs as refs_mod
from . import workdir
from .repo import Repository


def _commit_obj(repo: Repository, sha: str) -> objs.Commit:
    t, data = objs.read_object(repo, sha)
    if t != "commit":
        raise ValueError(f"{sha} is not a commit")
    return objs.parse_commit(data)


def _note_rerere_conflicts(repo: Repository, tree: str, paths: list[str]) -> None:
    from . import rerere as _rr

    for path in paths:
        entry = workdir.tree_path_entry(repo, tree, path)
        if entry is None or entry.is_dir() or entry.is_gitlink():
            continue
        try:
            obj_type, data = objs.read_object(repo, entry.sha)
        except KeyError:
            continue
        if obj_type != "blob":
            continue
        text = data.decode("utf-8", errors="replace")
        if "<<<<<<<" in text:
            _rr.note_conflict(repo, path, text)


def _make_commit(repo: Repository, tree: str, parents: list[str], message: str,
                 author: Optional[str] = None) -> str:
    name, email = repo.user()
    when = int(time.time())
    sig = objs.format_signature(name, email, when=when)
    c = objs.Commit(
        tree=tree,
        parents=parents,
        author=author or sig,
        committer=sig,
        message=message if message.endswith("\n") else message + "\n",
    )
    return objs.write_object(repo, "commit", c.encode())


def _apply_patch(
    repo: Repository,
    base_tree: str,
    target_tree: str,
    head_tree: str,
    *,
    ort_base: Optional[str] = None,
    ort_ours: Optional[str] = None,
    ort_theirs: Optional[str] = None,
):
    """Three-way merge head_tree with target_tree using base_tree as base.

    Runs the pure-Python ort engine (:mod:`pythongit.ort`) and returns
    (new_tree_sha, conflicted_paths, conflict_index). When a path conflicts,
    the conflict index records stages 1 (base), 2 (ours), 3 (theirs); the
    merged-with-markers content is kept in the returned tree for checkout into
    the worktree.
    """
    from . import ort as ort_mod

    ort_result = ort_mod.merge_tree(
        repo,
        ort_base or base_tree,
        ort_ours or head_tree,
        ort_theirs or target_tree,
    )
    if ort_result.conflicts:
        _note_rerere_conflicts(repo, ort_result.tree, ort_result.conflicts)
    return ort_result.tree, ort_result.conflicts, ort_result.conflict_index


def cherry_pick(repo: Repository, target_sha: str) -> tuple[Optional[str], list[str]]:
    target = _commit_obj(repo, target_sha)
    if not target.parents:
        raise ValueError("cannot cherry-pick a root commit (no parent)")
    base_tree = _commit_obj(repo, target.parents[0]).tree
    head_sym, head_sha = refs_mod.read_head(repo)
    if not head_sha:
        raise ValueError("no HEAD")
    head_tree = _commit_obj(repo, head_sha).tree
    new_tree, conflicts, conflict_idx = _apply_patch(
        repo,
        base_tree,
        target.tree,
        head_tree,
        ort_base=target.parents[0],
        ort_ours="HEAD",
        ort_theirs=target_sha,
    )
    if conflicts:
        # leave merged-with-markers in workdir, do not commit
        workdir.checkout_tree(repo, new_tree)
        if conflict_idx is not None:
            from .index import write_index
            write_index(repo, conflict_idx)
        return None, conflicts
    workdir.checkout_tree(repo, new_tree)
    msg = target.message
    sha = _make_commit(repo, new_tree, [head_sha], msg, author=target.author)
    if head_sym:
        refs_mod.update_ref(repo, head_sym, sha, message="cherry-pick")
    else:
        refs_mod.set_head(repo, sha)
    return sha, []


def revert(repo: Repository, target_sha: str) -> tuple[Optional[str], list[str]]:
    target = _commit_obj(repo, target_sha)
    if not target.parents:
        raise ValueError("cannot revert a root commit")
    # invert: base = target, "target" = parent
    base_tree = target.tree
    new_target_tree = _commit_obj(repo, target.parents[0]).tree
    head_sym, head_sha = refs_mod.read_head(repo)
    if not head_sha:
        raise ValueError("no HEAD")
    head_tree = _commit_obj(repo, head_sha).tree
    new_tree, conflicts, conflict_idx = _apply_patch(
        repo,
        base_tree,
        new_target_tree,
        head_tree,
        ort_base=target_sha,
        ort_ours="HEAD",
        ort_theirs=target.parents[0],
    )
    if conflicts:
        workdir.checkout_tree(repo, new_tree)
        if conflict_idx is not None:
            from .index import write_index
            write_index(repo, conflict_idx)
        return None, conflicts
    workdir.checkout_tree(repo, new_tree)
    msg = f'Revert "{target.message.splitlines()[0]}"\n\nThis reverts commit {target_sha}.\n'
    sha = _make_commit(repo, new_tree, [head_sha], msg)
    if head_sym:
        refs_mod.update_ref(repo, head_sym, sha, message="revert")
    else:
        refs_mod.set_head(repo, sha)
    return sha, []


def rebase_onto(repo: Repository, upstream: str) -> tuple[int, list[str]]:
    """Replay commits HEAD..(head) that aren't reachable from upstream onto upstream.

    Returns (count_picked, conflicted_paths_on_stop).
    """
    head_sym, head_sha = refs_mod.read_head(repo)
    if not head_sha:
        raise ValueError("no HEAD")
    up_sha = refs_mod.rev_parse(repo, upstream)
    if not up_sha:
        raise ValueError(f"bad upstream: {upstream}")
    # commits to replay: walk from HEAD until hitting upstream or one of its ancestors
    base_list = merge_mod.merge_bases(repo, head_sha, up_sha)
    if not base_list:
        raise RuntimeError("no common ancestor")
    base = base_list[0]
    # collect commits from base..HEAD in order
    chain: list[str] = []
    cur = head_sha
    while cur and cur != base:
        c = _commit_obj(repo, cur)
        chain.append(cur)
        if not c.parents:
            break
        cur = c.parents[0]
    chain.reverse()

    # move HEAD to upstream
    if head_sym:
        refs_mod.update_ref(repo, head_sym, up_sha, message="rebase: onto " + upstream)
    else:
        refs_mod.set_head(repo, up_sha)
    target_tree = _commit_obj(repo, up_sha).tree
    workdir.checkout_tree(repo, target_tree)

    picked = 0
    for sha in chain:
        new, conf = cherry_pick(repo, sha)
        if conf:
            return picked, conf
        picked += 1
    return picked, []
