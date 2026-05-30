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
    return {path: sha for path, _mode, sha in workdir.iter_tree_files(repo, tree)}


def _blob_similarity(repo: Repository, a_sha: str, b_sha: str, *, max_bytes: int = 1024 * 1024) -> float:
    from difflib import SequenceMatcher

    try:
        a_type, a_data = objs.read_object(repo, a_sha)
        b_type, b_data = objs.read_object(repo, b_sha)
    except KeyError:
        return 0.0
    if a_type != "blob" or b_type != "blob":
        return 0.0
    if len(a_data) > max_bytes or len(b_data) > max_bytes:
        return 1.0 if a_sha == b_sha else 0.0
    if not a_data and not b_data:
        return 1.0
    return SequenceMatcher(None, a_data, b_data, autojunk=False).ratio()


def _detect_renames(repo: Repository, base: dict[str, str], side: dict[str, str]) -> dict[str, str]:
    deleted = [p for p in base if p not in side]
    added = [p for p in side if p not in base]
    if not deleted or not added:
        return {}
    added_by_sha: dict[str, list[str]] = {}
    for path in added:
        added_by_sha.setdefault(side[path], []).append(path)
    renames: dict[str, str] = {}
    used_added: set[str] = set()
    for old in deleted:
        matches = [p for p in added_by_sha.get(base[old], []) if p not in used_added]
        if matches:
            new = sorted(matches)[0]
            renames[old] = new
            used_added.add(new)
    remaining_deleted = [p for p in deleted if p not in renames]
    remaining_added = [p for p in added if p not in used_added]
    if len(remaining_deleted) * len(remaining_added) > 4096:
        return renames
    for old in remaining_deleted:
        best_path = ""
        best_score = 0.0
        for new in remaining_added:
            score = _blob_similarity(repo, base[old], side[new])
            if score > best_score:
                best_score = score
                best_path = new
        if best_path and best_score >= 0.60:
            renames[old] = best_path
            used_added.add(best_path)
            remaining_added.remove(best_path)
    return renames


def _apply_side_renames(
    base: dict[str, str],
    side: dict[str, str],
    other: dict[str, str],
    renames: dict[str, str],
) -> None:
    for old, new in sorted(renames.items()):
        if old not in base or new not in side:
            continue
        base[new] = base.pop(old)
        if old in other and new not in other:
            other[new] = other.pop(old)


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
                 head_tree: str):
    """Three-way merge head_tree with target_tree using base_tree as base.

    Returns (new_tree_sha, conflicted_paths). When a path conflicts, the
    index records stages 1 (base), 2 (ours), 3 (theirs) instead of a
    single stage-0 entry; the merged-with-markers content is written to
    both worktree and used to build the returned tree placeholder.
    """
    base_orig = _tree_blobs(repo, base_tree)
    target_orig = _tree_blobs(repo, target_tree)
    head_orig = _tree_blobs(repo, head_tree)
    target_renames = _detect_renames(repo, base_orig, target_orig)
    head_renames = _detect_renames(repo, base_orig, head_orig)
    base = dict(base_orig)
    target = dict(target_orig)
    head = dict(head_orig)
    rename_conflicts: dict[str, tuple[str, str]] = {}
    for old, target_new in target_renames.items():
        head_new = head_renames.get(old)
        if head_new is not None and head_new != target_new:
            rename_conflicts[old] = (head_new, target_new)
    _apply_side_renames(base, target, head, {k: v for k, v in target_renames.items() if k not in rename_conflicts})
    _apply_side_renames(base, head, target, {k: v for k, v in head_renames.items() if k not in rename_conflicts})
    paths = sorted(set(base) | set(target) | set(head))
    out: dict[str, str] = {}
    stage_entries: list[tuple[str, int, str]] = []  # (path, stage, sha)
    conflicts: list[str] = []
    from . import rerere as _rr
    for old, (head_new, target_new) in sorted(rename_conflicts.items()):
        base_sha = base_orig[old]
        head_sha = head_orig.get(head_new)
        target_sha = target_orig.get(target_new)
        if head_sha:
            out[head_new] = head_sha
        if target_sha:
            out[target_new] = target_sha
        conflicts.extend([head_new, target_new])
        stage_entries.append((old, 1, base_sha))
        if head_sha:
            stage_entries.append((head_new, 2, head_sha))
        if target_sha:
            stage_entries.append((target_new, 3, target_sha))
    for p in paths:
        if any(p in pair for pair in rename_conflicts.values()):
            continue
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
    from .index import Index, IndexEntry, REG_MODE, read_index, write_index
    tree_idx = Index()
    for path, sha in sorted(out.items()):
        tree_idx.entries.append(IndexEntry(mode=REG_MODE, sha=sha, path=path))
    saved_idx = read_index(repo) if (repo.gitdir / "index").exists() else None
    try:
        write_index(repo, tree_idx)
        tree = workdir.write_tree(repo)
    finally:
        if saved_idx is not None:
            write_index(repo, saved_idx)
        else:
            (repo.gitdir / "index").unlink(missing_ok=True)
    conflict_idx = None
    if stage_entries:
        conflict_idx = Index()
        conflict_idx.entries.extend(tree_idx.entries)
        for path, stage, sha in stage_entries:
            e = IndexEntry(mode=REG_MODE, sha=sha, path=path)
            e.stage = stage
            conflict_idx.entries.append(e)
    return tree, conflicts, conflict_idx


def cherry_pick(repo: Repository, target_sha: str) -> tuple[Optional[str], list[str]]:
    target = _commit_obj(repo, target_sha)
    if not target.parents:
        raise ValueError("cannot cherry-pick a root commit (no parent)")
    base_tree = _commit_obj(repo, target.parents[0]).tree
    head_sym, head_sha = refs_mod.read_head(repo)
    if not head_sha:
        raise ValueError("no HEAD")
    head_tree = _commit_obj(repo, head_sha).tree
    new_tree, conflicts, conflict_idx = _apply_patch(repo, base_tree, target.tree, head_tree)
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
    new_tree, conflicts, conflict_idx = _apply_patch(repo, base_tree, new_target_tree, head_tree)
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
