"""git stash: store WIP state as commits on refs/stash.

Each stash is a merge commit:
  parent[0] = HEAD at stash time
  parent[1] = a "tree" commit holding the index state
  (parent[2] for untracked, omitted here)
The stash log is the reflog of refs/stash.
"""
from __future__ import annotations

import time
from typing import Optional

from . import objects as objs
from . import refs as refs_mod
from . import reflog as reflog_mod
from . import workdir
from .index import read_index, write_index
from .repo import Repository


def _commit_tree(repo: Repository, tree: str, parents: list[str], msg: str) -> str:
    name, email = repo.user()
    sig = objs.format_signature(name, email, when=int(time.time()))
    c = objs.Commit(tree=tree, parents=parents, author=sig, committer=sig, message=msg + "\n")
    return objs.write_object(repo, "commit", c.encode())


def push(repo: Repository, message: str = "") -> Optional[str]:
    head_sym, head_sha = refs_mod.read_head(repo)
    if not head_sha:
        return None
    # check there is anything to stash
    status = workdir.status(repo)
    if not (status["staged_new"] or status["staged_mod"] or status["staged_del"] or status["modified"] or status["missing"]):
        return None
    branch = head_sym[len("refs/heads/"):] if head_sym and head_sym.startswith("refs/heads/") else head_sha[:7]
    msg = message or f"WIP on {branch}"

    # 1) commit current index state
    idx_tree = workdir.write_tree(repo)
    i_commit = _commit_tree(repo, idx_tree, [head_sha], f"index on {branch}")

    # 2) snapshot worktree by add-all then write-tree
    saved_idx = read_index(repo)
    workdir.add_paths(repo, ["."])
    w_tree = workdir.write_tree(repo)
    write_index(repo, saved_idx)  # restore index for now (will reset later)

    w_commit = _commit_tree(repo, w_tree, [head_sha, i_commit], msg)
    refs_mod.update_ref(repo, "refs/stash", w_commit, message=msg)

    # 3) reset worktree+index to HEAD
    t, data = objs.read_object(repo, head_sha)
    head_tree = objs.parse_commit(data).tree
    workdir.checkout_tree(repo, head_tree)
    return w_commit


def list_stashes(repo: Repository) -> list[tuple[int, str, str]]:
    entries = reflog_mod.read(repo, "refs/stash")
    return [(i, e[1], e[3]) for i, e in enumerate(entries)]


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
