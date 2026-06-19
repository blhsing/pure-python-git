"""git stash: store WIP state as commits on refs/stash.

Each stash is a merge commit:
  parent[0] = HEAD at stash time
  parent[1] = a "tree" commit holding the index state
  (parent[2] for untracked, omitted here)
The stash log is the reflog of refs/stash.
"""
from __future__ import annotations

from typing import Optional

from . import objects as objs
from . import refs as refs_mod
from . import reflog as reflog_mod
from . import workdir
from .index import read_index, write_index
from .repo import Repository


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


def push(repo: Repository, message: str = "") -> Optional[str]:
    head_sym, head_sha = refs_mod.read_head(repo)
    if not head_sha:
        return None
    # check there is anything to stash
    status = workdir.status(repo)
    if not (status["staged_new"] or status["staged_mod"] or status["staged_del"] or status["modified"] or status["missing"]):
        return None
    branch = head_sym[len("refs/heads/"):] if head_sym and head_sym.startswith("refs/heads/") else head_sha[:7]
    subject = objs.parse_commit(objs.read_object(repo, head_sha)[1]).message.splitlines()[0]
    # The stash subject (= refs/stash reflog message and the WIP commit message)
    # has no trailing newline, matching git's create_stash.
    if message:
        msg = f"On {branch}: {message}"
    else:
        msg = f"WIP on {branch}: {head_sha[:7]} {subject}"

    # 1) commit the current index state ("index on <branch>: <sha> <subject>\n").
    idx_tree = workdir.write_tree(repo)
    i_commit = _commit_tree(repo, idx_tree, [head_sha],
                            f"index on {branch}: {head_sha[:7]} {subject}\n")

    # 2) snapshot the worktree. git stash records *tracked* files only (untracked
    # need -u), so update just the already-tracked index entries from the working
    # tree before write-tree, then restore the real index.
    saved_idx = read_index(repo)
    tracked = sorted(read_index(repo).by_path())
    workdir.add_paths(repo, tracked)
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
