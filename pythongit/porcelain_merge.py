"""High-level merge: fast-forward and three-way."""
from __future__ import annotations

from typing import Optional

from . import merge as merge_mod
from . import objects as objs
from . import refs as refs_mod
from . import workdir
from .repo import Repository


class MergeSignFailure(Exception):
    """Raised by merge() when GPG-signing the merge commit fails, mirroring
    builtin/merge.c's abort-before-write.  Carries the gpg error text and the
    auto-merge notices so the caller can reproduce git's exact output."""

    def __init__(self, errmsg: str, auto_merged: list):
        super().__init__(errmsg)
        self.errmsg = errmsg
        self.auto_merged = auto_merged


def _default_merge_message(repo: Repository, other_rev: str, head_sym: Optional[str]) -> str:
    """Compose the default merge commit subject the way ``git merge`` names it.

    The merged ref is classified (local branch / tag / remote-tracking branch /
    bare commit) and, unless the destination branch is the default (main or
    master), an `` into <branch>`` suffix is appended.
    """
    name = other_rev
    if refs_mod.read_ref(repo, f"refs/heads/{other_rev}"):
        subject = f"Merge branch '{name}'"
    elif refs_mod.read_ref(repo, f"refs/tags/{other_rev}"):
        subject = f"Merge tag '{name}'"
    elif refs_mod.read_ref(repo, f"refs/remotes/{other_rev}"):
        subject = f"Merge remote-tracking branch '{name}'"
    else:
        subject = f"Merge commit '{name}'"
    if head_sym and head_sym.startswith("refs/heads/"):
        dest = head_sym[len("refs/heads/"):]
        if dest not in ("main", "master"):
            subject += f" into {dest}"
    return subject + "\n"


def merge(repo: Repository, other_rev: str, *, message: Optional[str] = None,
          allow_ff: bool = True, no_ff: bool = False, favor: int = 0,
          sign_key: Optional[str] = None) -> tuple[str, list[str], list[str]]:
    """Return (result_sha, conflicts, auto_merged). result_sha is "" on conflicts.

    When *sign_key* is not None the merge commit is GPG-signed with that key
    (``""`` resolves to the default signing key), mirroring builtin/merge.c's
    sign_commit path so ``git merge -S`` produces a verifiable merge commit."""
    head_sym, head = refs_mod.read_head(repo)
    if not head:
        raise RuntimeError("no HEAD")
    other = refs_mod.rev_parse(repo, other_rev)
    if not other:
        raise RuntimeError(f"bad ref: {other_rev}")
    if head == other:
        return head, []

    bases = merge_mod.merge_bases(repo, head, other)
    # With no common ancestor, the caller (cmd_merge) has already enforced the
    # --allow-unrelated-histories gate; here we fall straight through to the
    # 3-way merge, whose engine builds a virtual empty-tree base.
    base = bases[0] if bases else None

    if base is not None:
        # already up-to-date
        if base == other:
            return head, []
        # fast-forward
        if base == head and allow_ff and not no_ff:
            if head_sym:
                refs_mod.update_ref(repo, head_sym, other, message=f"merge {other_rev}: Fast-forward")
            else:
                refs_mod.set_head(repo, other)
            tree = objs.parse_commit(objs.read_object(repo, other)[1]).tree
            workdir.checkout_tree(repo, tree)
            return other, []

    # three-way merge — recursive ort (handles 1, 2+, or 0 merge bases via a
    # virtual ancestor), matching `git merge`.
    from . import ort as ort_mod
    from .sequencer import _note_rerere_conflicts
    ort_result = ort_mod.merge_commits(repo, "HEAD", other_rev, favor=favor)
    new_tree = ort_result.tree
    conflicts = ort_result.conflicts
    conflict_idx = ort_result.conflict_index
    auto_merged = ort_result.auto_merged
    if conflicts:
        _note_rerere_conflicts(repo, new_tree, conflicts)
    workdir.checkout_tree(repo, new_tree)
    if conflicts:
        if conflict_idx is not None:
            from .index import write_index
            write_index(repo, conflict_idx)
        (repo.gitdir / "MERGE_HEAD").write_text(other + "\n", encoding="utf-8")
        (repo.gitdir / "MERGE_MSG").write_text(message or f"Merge: {other_rev}\n", encoding="utf-8")
        return "", conflicts, auto_merged

    msg = message or _default_merge_message(repo, other_rev, head_sym)
    author_sig = objs.build_signature(repo, "author")
    committer_sig = objs.build_signature(repo, "committer")
    c = objs.Commit(tree=new_tree, parents=[head, other], author=author_sig, committer=committer_sig,
                    message=msg if msg.endswith("\n") else msg + "\n")
    commit_bytes = c.encode()
    if sign_key is not None:
        from . import gpgsign
        sig, errmsg = gpgsign.sign_buffer(repo, commit_bytes, sign_key)
        if sig is None:
            # builtin/merge.c aborts before writing the commit when signing
            # fails; the worktree is already merged (checkout above) but HEAD
            # is left unchanged.  Carry the gpg errmsg + the auto-merge notices
            # so cmd_merge can reproduce git's exact output ordering.
            raise MergeSignFailure(errmsg, auto_merged)
        commit_bytes = gpgsign.add_header_signature(commit_bytes, sig)
    sha = objs.write_object(repo, "commit", commit_bytes)
    if head_sym:
        refs_mod.update_ref(repo, head_sym, sha, message=f"merge {other_rev}: Merge made by the 'ort' strategy.")
    else:
        refs_mod.set_head(repo, sha)
    return sha, [], auto_merged
