"""High-level merge: fast-forward and three-way."""
from __future__ import annotations

import time
from typing import Optional

from . import merge as merge_mod
from . import objects as objs
from . import refs as refs_mod
from . import workdir
from .repo import Repository


def merge(repo: Repository, other_rev: str, *, message: Optional[str] = None,
          allow_ff: bool = True, no_ff: bool = False) -> tuple[str, list[str]]:
    """Return (result_sha, conflicts). result_sha is "" on conflicts."""
    head_sym, head = refs_mod.read_head(repo)
    if not head:
        raise RuntimeError("no HEAD")
    other = refs_mod.rev_parse(repo, other_rev)
    if not other:
        raise RuntimeError(f"bad ref: {other_rev}")
    if head == other:
        return head, []

    bases = merge_mod.merge_bases(repo, head, other)
    if not bases:
        raise RuntimeError("no common ancestor")
    base = bases[0]

    # already up-to-date
    if base == other:
        return head, []
    # fast-forward
    if base == head and allow_ff and not no_ff:
        if head_sym:
            refs_mod.update_ref(repo, head_sym, other, message=f"merge {other_rev}: fast-forward")
        else:
            refs_mod.set_head(repo, other)
        tree = objs.parse_commit(objs.read_object(repo, other)[1]).tree
        workdir.checkout_tree(repo, tree)
        return other, []

    # three-way merge
    base_tree = objs.parse_commit(objs.read_object(repo, base)[1]).tree
    head_tree = objs.parse_commit(objs.read_object(repo, head)[1]).tree
    other_tree = objs.parse_commit(objs.read_object(repo, other)[1]).tree
    from .sequencer import _apply_patch
    new_tree, conflicts, conflict_idx = _apply_patch(
        repo,
        base_tree,
        other_tree,
        head_tree,
        ort_base=base,
        ort_ours="HEAD",
        ort_theirs=other_rev,
    )
    workdir.checkout_tree(repo, new_tree)
    if conflicts:
        if conflict_idx is not None:
            from .index import write_index
            write_index(repo, conflict_idx)
        (repo.gitdir / "MERGE_HEAD").write_text(other + "\n", encoding="utf-8")
        (repo.gitdir / "MERGE_MSG").write_text(message or f"Merge: {other_rev}\n", encoding="utf-8")
        return "", conflicts

    msg = message or f"Merge branch '{other_rev}'\n"
    name, email = repo.user()
    sig = objs.format_signature(name, email, when=int(time.time()))
    c = objs.Commit(tree=new_tree, parents=[head, other], author=sig, committer=sig,
                    message=msg if msg.endswith("\n") else msg + "\n")
    sha = objs.write_object(repo, "commit", c.encode())
    if head_sym:
        refs_mod.update_ref(repo, head_sym, sha, message=f"merge {other_rev}")
    else:
        refs_mod.set_head(repo, sha)
    return sha, []
