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
                 author: Optional[str] = None,
                 sign_key: Optional[str] = None) -> str:
    msg = "" if message == "" else (message if message.endswith("\n") else message + "\n")
    c = objs.Commit(
        tree=tree,
        parents=parents,
        author=author or objs.build_signature(repo, "author"),
        committer=objs.build_signature(repo, "committer"),
        message=msg,
    )
    commit_bytes = c.encode()
    if sign_key is not None:
        from . import gpgsign
        sig, errmsg = gpgsign.sign_buffer(repo, commit_bytes, sign_key)
        if sig is None:
            raise _SignFailure(errmsg)
        commit_bytes = gpgsign.add_header_signature(commit_bytes, sig)
    return objs.write_object(repo, "commit", commit_bytes)


class _SignFailure(Exception):
    """Raised when GPG-signing a sequencer commit fails (gpg error text)."""
    def __init__(self, errmsg: str):
        super().__init__(errmsg)
        self.errmsg = errmsg


def _apply_patch(
    repo: Repository,
    base_tree: str,
    target_tree: str,
    head_tree: str,
    *,
    ort_base: Optional[str] = None,
    ort_ours: Optional[str] = None,
    ort_theirs: Optional[str] = None,
    base_label: Optional[str] = None,
    ours_label: Optional[str] = None,
    theirs_label: Optional[str] = None,
):
    """Three-way merge head_tree with target_tree using base_tree as base.

    Runs the pure-Python ort engine (:mod:`pythongit.ort`) and returns
    (new_tree_sha, conflicted_paths, conflict_index, messages). When a path
    conflicts, the conflict index records stages 1 (base), 2 (ours), 3 (theirs);
    the merged-with-markers content is kept in the returned tree for checkout
    into the worktree.  ``messages`` is the ordered list of "Auto-merging"/
    "CONFLICT" lines the merge produced (for display on stdout).
    """
    from . import ort as ort_mod

    ort_result = ort_mod.merge_tree(
        repo,
        ort_base or base_tree,
        ort_ours or head_tree,
        ort_theirs or target_tree,
        base_label=base_label,
        ours_label=ours_label,
        theirs_label=theirs_label,
    )
    if ort_result.conflicts:
        _note_rerere_conflicts(repo, ort_result.tree, ort_result.conflicts)
    return (ort_result.tree, ort_result.conflicts, ort_result.conflict_index,
            ort_result.messages)


def cherry_pick(repo: Repository, target_sha: str,
                no_commit: bool = False,
                message: Optional[str] = None,
                allow_empty: bool = False,
                sign_key: Optional[str] = None
                ) -> tuple[Optional[str], list[str], list]:
    """Cherry-pick *target_sha* onto HEAD.  Returns (sha, conflicts, messages),
    where *messages* is the ordered Auto-merging/CONFLICT line list (for the
    caller's stdout) and *sha* is None on conflict / --no-commit.  An explicit
    *message* overrides the picked commit's message (for -x / --signoff)."""
    target = _commit_obj(repo, target_sha)
    if not target.parents:
        raise ValueError("cannot cherry-pick a root commit (no parent)")
    base_tree = _commit_obj(repo, target.parents[0]).tree
    head_sym, head_sha = refs_mod.read_head(repo)
    if not head_sha:
        raise ValueError("no HEAD")
    head_tree = _commit_obj(repo, head_sha).tree
    # Conflict-marker labels (sequencer.c do_pick_commit / get_message).
    # For cherry-pick: base = parent, next = the picked commit, so
    #   o.ancestor = base_label = msg.parent_label = "parent of <abbrev> (<subject>)"
    #   o.branch1  = "HEAD"     (ours)
    #   o.branch2  = next_label = msg.label        = "<abbrev> (<subject>)"
    from .mergeort import _abbrev as _merge_abbrev
    subj = target.message.splitlines()[0] if target.message.strip() else ""
    label = f"{_merge_abbrev(repo, target_sha)} ({subj})"
    new_tree, conflicts, conflict_idx, messages = _apply_patch(
        repo,
        base_tree,
        target.tree,
        head_tree,
        ort_base=target.parents[0],
        ort_ours="HEAD",
        ort_theirs=target_sha,
        base_label=f"parent of {label}",
        ours_label="HEAD",
        theirs_label=label,
    )
    msg = message if message is not None else target.message
    if conflicts:
        # leave merged-with-markers in workdir, do not commit; record
        # CHERRY_PICK_HEAD + MERGE_MSG so --continue/--abort and a later commit
        # work (sequencer.c do_pick_commit -> write_message + the conflict hint).
        workdir.checkout_tree(repo, new_tree)
        if conflict_idx is not None:
            from .index import write_index
            write_index(repo, conflict_idx)
        (repo.gitdir / "CHERRY_PICK_HEAD").write_text(target_sha + "\n", encoding="utf-8")
        hint = "\n# Conflicts:\n" + "".join(f"#\t{p}\n" for p in conflicts)
        (repo.gitdir / "MERGE_MSG").write_text(
            (msg if msg.endswith("\n") else msg + "\n") + hint, encoding="utf-8")
        return None, conflicts, messages
    workdir.checkout_tree(repo, new_tree)
    if no_commit:
        # --no-commit: stage the change (index+worktree already updated above),
        # leave a MERGE_MSG with the picked commit's message, and do NOT write
        # CHERRY_PICK_HEAD or create a commit (sequencer.c do_pick_commit).
        _write_pseudo_msg(repo, msg)
        return None, [], messages
    sha = _make_commit(repo, new_tree, [head_sha], msg, author=target.author,
                       sign_key=sign_key)
    if head_sym:
        refs_mod.update_ref(repo, head_sym, sha,
                            message=f"cherry-pick: {msg.splitlines()[0]}")
    else:
        refs_mod.set_head(repo, sha)
    return sha, [], messages


def _write_pseudo_msg(repo: Repository, msg: str) -> None:
    """Write .git/MERGE_MSG (the message a later `commit` would default to)."""
    text = msg if msg.endswith("\n") else msg + "\n"
    (repo.gitdir / "MERGE_MSG").write_text(text, encoding="utf-8")


def _revert_message(repo: Repository, target, target_sha: str,
                    signoff: bool) -> str:
    """The default revert commit message, with an optional Signed-off-by
    trailer for --signoff (sequencer.c append_signoff)."""
    msg = f'Revert "{target.message.splitlines()[0]}"\n\nThis reverts commit {target_sha}.\n'
    if signoff:
        committer = objs.build_signature(repo, "committer")
        # ident "Name <email> <ts> <tz>" -> "Name <email>".
        who = committer[:committer.index(">") + 1]
        msg = msg.rstrip("\n") + f"\n\nSigned-off-by: {who}\n"
    return msg


def revert(repo: Repository, target_sha: str,
           no_commit: bool = False, signoff: bool = False,
           sign_key=None) -> tuple[Optional[str], list[str], list]:
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
    # Conflict-marker labels (sequencer.c do_pick_commit / get_message).
    # For revert: base = the reverted commit, next = its parent, so
    #   o.ancestor = base_label = msg.label        = "<abbrev> (<subject>)"
    #   o.branch1  = "HEAD"     (ours)
    #   o.branch2  = next_label = msg.parent_label = "parent of <abbrev> (<subject>)"
    from .mergeort import _abbrev as _merge_abbrev
    subj = target.message.splitlines()[0] if target.message.strip() else ""
    label = f"{_merge_abbrev(repo, target_sha)} ({subj})"
    new_tree, conflicts, conflict_idx, messages = _apply_patch(
        repo,
        base_tree,
        new_target_tree,
        head_tree,
        ort_base=target_sha,
        ort_ours="HEAD",
        ort_theirs=target.parents[0],
        base_label=label,
        ours_label="HEAD",
        theirs_label=f"parent of {label}",
    )
    if conflicts:
        workdir.checkout_tree(repo, new_tree)
        if conflict_idx is not None:
            from .index import write_index
            write_index(repo, conflict_idx)
        # REVERT_HEAD + MERGE_MSG are written on conflict too (sequencer.c
        # do_pick_commit -> write_message(git_path_merge_msg) with the conflict
        # hint appended by append_conflicts_hint).
        (repo.gitdir / "REVERT_HEAD").write_text(target_sha + "\n", encoding="utf-8")
        rmsg = _revert_message(repo, target, target_sha, signoff)
        hint = "\n# Conflicts:\n" + "".join(f"#\t{p}\n" for p in conflicts)
        (repo.gitdir / "MERGE_MSG").write_text(rmsg + hint, encoding="utf-8")
        return None, conflicts, messages
    workdir.checkout_tree(repo, new_tree)
    msg = _revert_message(repo, target, target_sha, signoff)
    if no_commit:
        # --no-commit: stage the revert, write REVERT_HEAD (the reverted commit)
        # and MERGE_MSG, but do NOT create a commit (sequencer.c do_pick_commit:
        # REVERT_HEAD is written when no_commit && res == 0).
        (repo.gitdir / "REVERT_HEAD").write_text(target_sha + "\n", encoding="utf-8")
        _write_pseudo_msg(repo, msg)
        return None, [], []
    sha = _make_commit(repo, new_tree, [head_sha], msg, sign_key=sign_key)
    if head_sym:
        refs_mod.update_ref(repo, head_sym, sha,
                            message=f"revert: {msg.splitlines()[0]}")
    else:
        refs_mod.set_head(repo, sha)
    return sha, [], []


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

    # builtin/rebase.c can_fast_forward(): when onto (== upstream here) is the
    # single merge-base of upstream and HEAD and history is linear, the branch
    # is already based on upstream.  Without --force-rebase git fast-forwards
    # (a no-op when HEAD already contains upstream), prints "up to date", and
    # never replays/rewrites the commits.  Signal that with picked == 0 and
    # leave HEAD untouched.
    if len(base_list) == 1 and base == up_sha:
        return 0, []
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
        new, conf, _msgs = cherry_pick(repo, sha)
        if conf:
            return picked, conf
        picked += 1
    return picked, []
