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


_CONFLICT_STYLES = {"merge": 0, "diff3": 1, "zdiff3": 2}


def _resolve_attributes(repo: Repository):
    """Read merge-relevant .gitattributes the way git's attr stack does for an
    in-repo merge: the working-tree top-level .gitattributes plus
    $GIT_DIR/info/attributes (merge-ort's attr_index itself stays empty unless
    renormalize is set, so the merge result's .gitattributes is not consulted)."""
    text = b""
    info_attr = repo.gitdir / "info" / "attributes"
    if info_attr.exists():
        text += info_attr.read_bytes() + b"\n"
    wt_attr = repo.path / ".gitattributes"
    try:
        if wt_attr.is_file():
            text += wt_attr.read_bytes()
    except OSError:
        pass
    if not text.strip():
        return None
    return mergeort.MergeAttributes.parse(text.decode("utf-8", "replace"))


def _build_config(repo: Repository, base_tree: str, ours_tree: str,
                  theirs_tree: str) -> "mergeort.MergeConfig":
    cp = repo.config()
    style_name = cp.get("merge", "conflictstyle", fallback="merge")
    style = _CONFLICT_STYLES.get(style_name.strip().lower(), 0)
    attrs = _resolve_attributes(repo)
    return mergeort.MergeConfig(conflict_style=style, attributes=attrs)


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


def _ort_result(repo: Repository, tree: str, opt) -> OrtResult:
    stages = mergeort.conflicted_stages(opt)
    conflicts = sorted({path for path, _stage, _mode, _sha in stages})
    conflict_index = _result_index(repo, tree, stages) if stages else None
    return OrtResult(tree, conflicts, conflict_index)


def merge_tree(
    repo: Repository,
    merge_base: str,
    ours: str,
    theirs: str,
    *,
    cfg: "Optional[mergeort.MergeConfig]" = None,
) -> OrtResult:
    """Run the ort merge for three tree-ish arguments with an explicit base.

    ``merge_base``, ``ours`` and ``theirs`` are used both to locate the trees
    to merge and (verbatim) as the conflict-marker labels, exactly mirroring
    ``git merge-tree --write-tree --merge-base <merge_base> <ours> <theirs>``.
    """
    base_tree = _peel_to_tree(repo, merge_base)
    ours_tree = _peel_to_tree(repo, ours)
    theirs_tree = _peel_to_tree(repo, theirs)

    if cfg is None:
        cfg = _build_config(repo, base_tree, ours_tree, theirs_tree)
    opt = mergeort.Opt(repo, merge_base, ours, theirs,
                       **_cfg_kwargs(cfg))
    tree, _clean = mergeort.merge_incore_nonrecursive(
        opt, base_tree, ours_tree, theirs_tree)
    return _ort_result(repo, tree, opt)


def merge_commits(
    repo: Repository,
    ours: str,
    theirs: str,
    *,
    cfg: "Optional[mergeort.MergeConfig]" = None,
    allow_unrelated: bool = True,
) -> OrtResult:
    """Recursive (virtual-merge-base) merge of two commits, mirroring
    ``git merge-tree --write-tree <ours> <theirs>`` (no --merge-base): all
    merge bases are computed and merged into a virtual ancestor."""
    from . import merge as merge_mod
    ours_sha = refs_mod.rev_parse(repo, ours) or ours
    theirs_sha = refs_mod.rev_parse(repo, theirs) or theirs
    bases = list(reversed(merge_mod.merge_bases(repo, ours_sha, theirs_sha)))
    if not bases and not allow_unrelated:
        raise RuntimeError("refusing to merge unrelated histories")
    if cfg is None:
        base_tree = (_peel_to_tree(repo, bases[0]) if bases
                     else objs.hash_bytes("tree", b"", repo)[0])
        cfg = _build_config(repo, base_tree, _peel_to_tree(repo, ours_sha),
                            _peel_to_tree(repo, theirs_sha))
    tree, _clean, opt = mergeort.merge_recursive(
        repo, ours_sha, theirs_sha, branch1=ours, branch2=theirs,
        merge_bases=bases, cfg=cfg)
    return _ort_result(repo, tree, opt)


def _cfg_kwargs(cfg) -> dict:
    if cfg is None:
        return {}
    return dict(conflict_style=cfg.conflict_style, variant=cfg.variant,
                xdl_flags=cfg.xdl_flags, attributes=cfg.attributes,
                rename_detection=cfg.rename_detection,
                rename_limit=cfg.rename_limit,
                detect_directory_renames=cfg.detect_directory_renames)
