"""Pure-Python ``ort`` merge engine.

This module runs Git's ``ort`` three-way merge entirely in Python — no ``git``
binary and no fallback engine.  The heavy lifting lives in
:mod:`pythongit.mergeort` (the merge-ort tree engine), :mod:`pythongit.xdiff`
(histogram diff + zealous 3-way content merge), and :mod:`pythongit.diffcore`
(rename detection).  Output (result tree, conflicted blobs, and conflicted
index stages) is byte-for-byte identical to ``git merge-tree --write-tree``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from . import mergeort
from . import objects as objs
from . import refs as refs_mod
from . import workdir
from .index import Index, IndexEntry
from .repo import Repository


@dataclass(frozen=True)
class OrtResult:
    tree: str
    conflicts: list[str]
    conflict_index: Optional[Index]


def _peel_to_tree(repo: Repository, rev: str) -> str:
    """Resolve a tree-ish (commit/tree sha, ref, or HEAD) to a tree sha."""
    sha = refs_mod.rev_parse(repo, rev)
    if sha is None:
        # maybe it is already a raw tree/commit sha not known to rev_parse
        sha = rev
    obj_type, data = objs.read_object(repo, sha)
    if obj_type == "commit":
        return objs.parse_commit(data).tree
    if obj_type == "tag":
        # peel annotated tag to its commit then tree
        for line in data.decode("utf-8", "replace").splitlines():
            if line.startswith("object "):
                return _peel_to_tree(repo, line.split(" ", 1)[1].strip())
        raise ValueError(f"cannot peel tag {sha}")
    if obj_type == "tree":
        return sha
    raise ValueError(f"{rev} is not a tree-ish")


def _result_index(repo: Repository, tree: str,
                  stages: list[tuple[str, int, int, str]]) -> Index:
    conflicted = {path for path, _stage, _mode, _sha in stages}
    idx = Index()
    for path, mode, sha in workdir.iter_tree_files(repo, tree):
        if path in conflicted:
            continue
        idx.entries.append(IndexEntry(mode=int(mode, 8), sha=sha, path=path))
    for path, stage, mode, sha in stages:
        e = IndexEntry(mode=mode, sha=sha, path=path)
        e.stage = stage
        idx.entries.append(e)
    return idx


def merge_tree(
    repo: Repository,
    merge_base: str,
    ours: str,
    theirs: str,
) -> OrtResult:
    """Run the ort merge for three tree-ish arguments.

    ``merge_base``, ``ours`` and ``theirs`` are used both to locate the trees
    to merge and (verbatim) as the conflict-marker labels, exactly mirroring
    ``git merge-tree --write-tree --merge-base <merge_base> <ours> <theirs>``.
    """
    base_tree = _peel_to_tree(repo, merge_base)
    ours_tree = _peel_to_tree(repo, ours)
    theirs_tree = _peel_to_tree(repo, theirs)

    opt = mergeort.Opt(repo, merge_base, ours, theirs)
    tree, _clean = mergeort.merge_incore_nonrecursive(
        opt, base_tree, ours_tree, theirs_tree)

    stages = mergeort.conflicted_stages(opt)
    conflicts = sorted({path for path, _stage, _mode, _sha in stages})
    conflict_index = _result_index(repo, tree, stages) if stages else None
    return OrtResult(tree, conflicts, conflict_index)
