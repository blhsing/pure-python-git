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


def _tree_blobs(repo: Repository, tree: str) -> dict[str, str]:
    return workdir.flatten_tree(repo, tree)


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


def _apply_patch(repo: Repository, base_tree: str, target_tree: str,
                 head_tree: str) -> tuple[str, list[str]]:
    """Three-way merge head_tree with target_tree using base_tree as base.

    Returns (new_tree_sha, conflicted_paths). When a path conflicts, the
    index records stages 1 (base), 2 (ours), 3 (theirs) instead of a
    single stage-0 entry; the merged-with-markers content is written to
    both worktree and used to build the returned tree placeholder.
    """
    base = _tree_blobs(repo, base_tree)
    target = _tree_blobs(repo, target_tree)
    head = _tree_blobs(repo, head_tree)
    paths = sorted(set(base) | set(target) | set(head))
    out: dict[str, str] = {}
    stage_entries: list[tuple[str, int, str]] = []  # (path, stage, sha)
    conflicts: list[str] = []
    from . import rerere as _rr
    for p in paths:
        b = base.get(p)
        t = target.get(p)
        h = head.get(p)
        if t == b:
            if h is not None:
                out[p] = h
            continue
        if h == b:
            if t is not None:
                out[p] = t
            continue
        if h == t:
            if h is not None:
                out[p] = h
            continue
        b_data = b and objs.read_object(repo, b)[1] or b""
        t_data = t and objs.read_object(repo, t)[1] or b""
        h_data = h and objs.read_object(repo, h)[1] or b""
        merged, conf = merge_mod.merge_blob(b_data, h_data, t_data)
        if conf:
            replay = _rr.replay(repo, merged.decode("utf-8", errors="replace"))
            if replay is not None:
                merged = replay.encode("utf-8")
                conf = False
            else:
                _rr.note_conflict(repo, p, merged.decode("utf-8", errors="replace"))
        merged_sha = objs.write_object(repo, "blob", merged)
        out[p] = merged_sha
        if conf:
            conflicts.append(p)
            if b is not None:
                stage_entries.append((p, 1, b))
            if h is not None:
                stage_entries.append((p, 2, h))
            if t is not None:
                stage_entries.append((p, 3, t))
    from .index import Index, IndexEntry, REG_MODE, write_index
    idx = Index()
    for path, sha in sorted(out.items()):
        if path in conflicts:
            # leave a stage-0 entry pointing at the merged-with-markers blob
            # so the worktree materialization still has content to write
            e = IndexEntry(mode=REG_MODE, sha=sha, path=path)
            e.stage = 0
            idx.entries.append(e)
        else:
            idx.entries.append(IndexEntry(mode=REG_MODE, sha=sha, path=path))
    for path, stage, sha in stage_entries:
        e = IndexEntry(mode=REG_MODE, sha=sha, path=path)
        e.stage = stage
        idx.entries.append(e)
    write_index(repo, idx)
    tree = workdir.write_tree(repo)
    return tree, conflicts


def cherry_pick(repo: Repository, target_sha: str) -> tuple[Optional[str], list[str]]:
    target = _commit_obj(repo, target_sha)
    if not target.parents:
        raise ValueError("cannot cherry-pick a root commit (no parent)")
    base_tree = _commit_obj(repo, target.parents[0]).tree
    head_sym, head_sha = refs_mod.read_head(repo)
    if not head_sha:
        raise ValueError("no HEAD")
    head_tree = _commit_obj(repo, head_sha).tree
    new_tree, conflicts = _apply_patch(repo, base_tree, target.tree, head_tree)
    if conflicts:
        # leave merged-with-markers in workdir, do not commit
        workdir.checkout_tree(repo, new_tree)
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
    new_tree, conflicts = _apply_patch(repo, base_tree, new_target_tree, head_tree)
    if conflicts:
        workdir.checkout_tree(repo, new_tree)
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
