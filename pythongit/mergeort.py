"""Pure-Python port of Git's ``merge-ort`` engine (merge-ort.c, v2.44.0).

Implements the non-recursive in-core merge used by ``git merge-tree
--write-tree``: a recursive three-way tree walk (collect_merge_info), file
rename detection + resolution (detect_and_process_renames / process_renames),
per-path resolution (process_entry), and result-tree assembly with conflicted
index stages.

Content merges go through :mod:`pythongit.xdiff` (histogram diff + zealous
3-way merge) so conflicted blobs are byte-for-byte identical to C Git.  File
rename detection goes through :mod:`pythongit.diffcore`.

Scope: directory-rename detection and recursive (virtual) merge bases are not
implemented; a single merge base is required (as provided by
``git merge-tree --merge-base``).  Submodule content conflicts fall back to a
conservative resolution.
"""
from __future__ import annotations

import fnmatch
import functools
from dataclasses import dataclass, field
from typing import Optional

from . import diffcore
from . import objects as objs
from . import xdiff
from .index import Index, IndexEntry
from .repo import Repository

# stat mode helpers
S_IFMT = 0o170000
S_IFREG = 0o100000
S_IFDIR = 0o040000
S_IFLNK = 0o120000
S_IFGITLINK = 0o160000

MODE_DIR = 0o040000


def s_isreg(mode: int) -> bool:
    return (mode & S_IFMT) == S_IFREG


def s_isgitlink(mode: int) -> bool:
    return (mode & S_IFMT) == S_IFGITLINK


def s_islnk(mode: int) -> bool:
    return (mode & S_IFMT) == S_IFLNK


class MergeAttributes:
    """Minimal .gitattributes evaluator for the ``merge`` and
    ``conflict-marker-size`` attributes, mirroring git's per-path attribute
    lookup for content merges."""

    def __init__(self, rules: list):
        # rules: list of (pattern, {attr_name: value}) in file order
        self.rules = rules

    @classmethod
    def parse(cls, text: str) -> "MergeAttributes":
        rules = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            pattern = parts[0]
            attrs: dict = {}
            for tok in parts[1:]:
                if tok.startswith("-"):
                    attrs[tok[1:]] = False
                elif tok.startswith("!"):
                    attrs[tok[1:]] = "unspecified"
                elif "=" in tok:
                    k, v = tok.split("=", 1)
                    attrs[k] = v
                else:
                    attrs[tok] = True
            rules.append((pattern, attrs))
        return cls(rules)

    def _lookup(self, path: str, attr: str):
        value = None
        for pattern, attrs in self.rules:
            if attr in attrs and _attr_match(pattern, path):
                value = attrs[attr]
        return value

    def driver(self, path: str) -> str:
        v = self._lookup(path, "merge")
        if v is False:
            return "binary"          # -merge
        if v in (None, True, "unspecified"):
            return "text"
        if v in ("text", "binary", "union"):
            return v
        return "text"                # custom/unknown driver -> built-in text

    def marker_size(self, path: str) -> int:
        v = self._lookup(path, "conflict-marker-size")
        if isinstance(v, str) and v.isdigit():
            n = int(v)
            return n if n > 0 else xdiff.DEFAULT_CONFLICT_MARKER_SIZE
        return xdiff.DEFAULT_CONFLICT_MARKER_SIZE


def _attr_match(pattern: str, path: str) -> bool:
    neg = False
    if pattern.startswith("/"):
        pattern = pattern[1:]
        return fnmatch.fnmatch(path, pattern)
    if "/" not in pattern:
        return fnmatch.fnmatch(path.rsplit("/", 1)[-1], pattern)
    return fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(path, "*/" + pattern)


@dataclass
class VersionInfo:
    mode: int = 0
    oid: str = ""


@dataclass
class CI:
    # merged result (merged_info)
    result_mode: int = 0
    result_oid: str = ""
    is_null: bool = False
    clean: bool = True
    directory_name: str = ""
    basename_offset: int = 0
    # conflict_info extras
    stages: list = field(default_factory=lambda: [VersionInfo(), VersionInfo(), VersionInfo()])
    pathnames: list = field(default_factory=lambda: ["", "", ""])
    filemask: int = 0
    dirmask: int = 0
    match_mask: int = 0
    df_conflict: bool = False
    path_conflict: bool = False


class Opt:
    def __init__(self, repo: Repository, ancestor: str, branch1: str, branch2: str,
                 *, conflict_style: int = 0, variant: int = 0,
                 xdl_flags: int = xdiff.XDF_HISTOGRAM_DIFF,
                 attributes: Optional["MergeAttributes"] = None,
                 rename_detection: bool = True, rename_limit: int = 7000,
                 detect_directory_renames: int = 1):
        self.repo = repo
        self.ancestor = ancestor
        self.branch1 = branch1
        self.branch2 = branch2
        self.paths: dict[str, CI] = {}
        self.conflicted: dict[str, CI] = {}
        self.call_depth = 0
        self.null_oid = "0" * (repo.hash_len * 2)
        self._tree_cache: dict[str, dict] = {}
        self.rename_limit = rename_limit
        # merge configuration (parity-preserving defaults)
        self.conflict_style = conflict_style       # 0 merge / 1 diff3 / 2 zdiff3
        self.variant = variant                     # 0 / FAVOR_OURS / FAVOR_THEIRS
        self.xdl_flags = xdl_flags                 # histogram (+ whitespace flags)
        self.rename_detection = rename_detection
        # 0 = false, 1 = conflict (default), 2 = true
        self.detect_directory_renames = detect_directory_renames
        self._attributes = attributes
        self._submodule_cache: dict[str, Optional[Repository]] = {}
        # rename-detection bookkeeping populated during collect_merge_info
        self.relevant_sources: dict[int, set] = {1: set(), 2: set()}
        self.dirs_removed_relevance: dict[int, dict] = {1: {}, 2: {}}

    def is_null(self, oid: str) -> bool:
        return oid == self.null_oid or set(oid) == {"0"}

    def merge_driver(self, path: str) -> str:
        """Resolve the built-in merge driver for a path: text/binary/union."""
        if self._attributes is None:
            return "text"
        return self._attributes.driver(path)

    def marker_size(self, path: str) -> int:
        if self._attributes is None:
            return xdiff.DEFAULT_CONFLICT_MARKER_SIZE
        return self._attributes.marker_size(path)


# ---------------------------------------------------------------------------
# tree reading


def _tree_entries(opt: Opt, oid: Optional[str]) -> dict:
    """Return {name: (mode_int, oid, is_dir)} for a tree oid, or {} for None."""
    if oid is None or opt.is_null(oid):
        return {}
    cached = opt._tree_cache.get(oid)
    if cached is not None:
        return cached
    try:
        t, data = objs.read_object(opt.repo, oid)
    except KeyError:
        opt._tree_cache[oid] = {}
        return {}
    out: dict = {}
    if t == "tree":
        for e in objs.parse_tree(data, opt.repo.hash_len):
            mode = int(e.mode, 8)
            out[e.name] = (mode, e.sha, e.is_dir())
    opt._tree_cache[oid] = out
    return out


# ---------------------------------------------------------------------------
# collect_merge_info


def _basename_offset(dirpath: str) -> int:
    return 0 if not dirpath else len(dirpath) + 1


def _setup_conflict(opt: Opt, fullpath: str, dirpath: str, names: list,
                    filemask: int, dirmask: int, df_conflict: bool) -> CI:
    ci = CI()
    ci.clean = False
    ci.directory_name = dirpath
    ci.basename_offset = _basename_offset(dirpath)
    ci.is_null = bool(dirmask)  # assume null until directory completes
    for i in range(3):
        ci.pathnames[i] = fullpath
        ci.stages[i] = VersionInfo(names[i][0], names[i][1])
    ci.filemask = filemask
    ci.dirmask = dirmask
    ci.df_conflict = df_conflict
    opt.paths[fullpath] = ci
    return ci


def _setup_resolved(opt: Opt, fullpath: str, dirpath: str, ver: tuple,
                    is_null: bool) -> CI:
    ci = CI()
    ci.clean = True
    ci.directory_name = dirpath
    ci.basename_offset = _basename_offset(dirpath)
    ci.result_mode = ver[0]
    ci.result_oid = ver[1]
    ci.is_null = is_null
    opt.paths[fullpath] = ci
    return ci


def collect_merge_info(opt: Opt, base: Optional[str], s1: Optional[str],
                       s2: Optional[str], dirpath: str = "",
                       dir_rename_mask: int = 0) -> None:
    e0 = _tree_entries(opt, base)
    e1 = _tree_entries(opt, s1)
    e2 = _tree_entries(opt, s2)
    names_all = sorted(set(e0) | set(e1) | set(e2))

    # Per-directory dir_rename_mask 0x07 flip: if a direct child is a file
    # present only on the dir-having side (filemask == dir_rename_mask), the
    # whole directory's rename-info is computed under directory-rename context.
    if dir_rename_mask in (2, 4):
        for name in names_all:
            a0, a1, a2 = e0.get(name), e1.get(name), e2.get(name)
            mask = (1 if a0 else 0) | (2 if a1 else 0) | (4 if a2 else 0)
            dmask = ((2 if (a1 and a1[2]) else 0) | (1 if (a0 and a0[2]) else 0)
                     | (4 if (a2 and a2[2]) else 0))
            if (mask & ~dmask) == dir_rename_mask:
                dir_rename_mask = 0x07
                break

    for name in names_all:
        _collect_one(opt, name, e0.get(name), e1.get(name), e2.get(name),
                     dirpath, dir_rename_mask)


def _collect_one(opt: Opt, name: str, a0, a1, a2, dirpath: str,
                 dir_rename_mask: int) -> None:
    null = opt.null_oid
    # names[i] = (mode, oid); is_dir[i]
    names = []
    is_dir = [False, False, False]
    mask = 0
    dirmask = 0
    for i, ent in enumerate((a0, a1, a2)):
        if ent is None:
            names.append((0, null))
        else:
            mode, oid, isd = ent
            names.append((mode, oid))
            mask |= (1 << i)
            if isd:
                dirmask |= (1 << i)
                is_dir[i] = True
    filemask = mask & ~dirmask

    mbase_null = not (mask & 1)
    side1_null = not (mask & 2)
    side2_null = not (mask & 4)
    side1_matches = (not side1_null and not mbase_null and
                     names[0][0] == names[1][0] and names[0][1] == names[1][1])
    side2_matches = (not side2_null and not mbase_null and
                     names[0][0] == names[2][0] and names[0][1] == names[2][1])
    sides_match = (not side1_null and not side2_null and
                   names[1][0] == names[2][0] and names[1][1] == names[2][1])

    df_conflict = (filemask != 0) and (dirmask != 0)

    match_mask = 0
    if side1_matches:
        match_mask = 7 if side2_matches else 3
    elif side2_matches:
        match_mask = 5
    elif sides_match:
        match_mask = 6

    fullpath = f"{dirpath}{name}" if not dirpath else f"{dirpath}/{name}"

    # all three match -> resolve via base (even for trees)
    if side1_matches and side2_matches:
        _setup_resolved(opt, fullpath, dirpath, names[0], mbase_null)
        return
    if sides_match and filemask == 0x07:
        _setup_resolved(opt, fullpath, dirpath, names[1], side1_null)
        return
    if side1_matches and filemask == 0x07:
        _setup_resolved(opt, fullpath, dirpath, names[2], side2_null)
        return
    if side2_matches and filemask == 0x07:
        _setup_resolved(opt, fullpath, dirpath, names[1], side1_null)
        return

    # collect_rename_info: track removed directories + relevance + relevant
    # rename sources, mirroring merge-ort.c:collect_rename_info().
    child_dir_rename_mask = dir_rename_mask
    if dir_rename_mask != 0x07 and dirmask in (3, 5):
        child_dir_rename_mask = dirmask & ~1
    _collect_rename_info(opt, fullpath, dirpath, match_mask, filemask, dirmask,
                         dir_rename_mask)

    ci = _setup_conflict(opt, fullpath, dirpath, names, filemask, dirmask, df_conflict)
    ci.match_mask = match_mask

    if dirmask:
        ci.match_mask &= filemask
        child = [None, None, None]
        for i in range(3):
            if is_dir[i]:
                child[i] = names[i][1]
        collect_merge_info(opt, child[0], child[1], child[2], fullpath,
                           child_dir_rename_mask)


def _collect_rename_info(opt: Opt, fullpath: str, dirpath: str, match_mask: int,
                         filemask: int, dirmask: int, dir_rename_mask: int) -> None:
    rel = opt.dirs_removed_relevance
    # Record removed directories and their relevance.
    if dirmask in (1, 3, 5):
        sides = (0x07 - dirmask) // 2
        relevance = (_RELEVANT_FOR_ANCESTOR if dir_rename_mask == 0x07
                     else _NOT_RELEVANT)
        if sides & 1 and rel[1].get(fullpath) != _RELEVANT_FOR_SELF:
            rel[1][fullpath] = relevance
        if sides & 2 and rel[2].get(fullpath) != _RELEVANT_FOR_SELF:
            rel[2][fullpath] = relevance
    # Upgrade a directory to RELEVANT_FOR_SELF when the dir-having side added a
    # file directly in it.
    if dir_rename_mask == 0x07 and filemask in (2, 4):
        side = 3 - (filemask >> 1)
        rel[side][dirpath] = _RELEVANT_FOR_SELF
    # Record relevant rename sources (deletions).
    if filemask in (0, 7):
        return
    for side in (1, 2):
        side_mask = 1 << side
        if (filemask & 1) and not (filemask & side_mask):
            content_relevant = ((match_mask & filemask) == 0)
            location_relevant = (dir_rename_mask == 0x07)
            if content_relevant or location_relevant:
                opt.relevant_sources[side].add(fullpath)


# ---------------------------------------------------------------------------
# content merge


def _read_blob(opt: Opt, oid: str) -> bytes:
    if opt.is_null(oid):
        return b""
    try:
        t, data = objs.read_object(opt.repo, oid)
    except KeyError:
        return b""
    return data


def _ll_merge(opt: Opt, orig: bytes, src1: bytes, src2: bytes, *,
              name1: str, name2: str, ancestor_name: str, marker_size: int,
              virtual_ancestor: bool, variant: int, driver: str) -> tuple[bytes, int]:
    """Returns (result_bytes, status) where status 0=clean, 1=conflict,
    2=binary conflict.  ``variant`` selects favor (0 normal / OURS / THEIRS /
    UNION); ``driver`` is the resolved merge driver (text/binary/union)."""
    if driver == "binary" or (
            driver != "union"
            and (diffcore.buffer_is_binary(orig) or diffcore.buffer_is_binary(src1)
                 or diffcore.buffer_is_binary(src2))):
        # binary merge: ancestor for virtual merges, else ours/theirs by variant
        if virtual_ancestor:
            return orig, 0
        if variant == xdiff.XDL_MERGE_FAVOR_THEIRS:
            return src2, 0
        if variant == xdiff.XDL_MERGE_FAVOR_OURS:
            return src1, 0
        return src1, 2
    favor = xdiff.XDL_MERGE_FAVOR_UNION if driver == "union" else variant
    result, nconf = xdiff.xdl_merge(
        orig, src1, src2,
        level=xdiff.XDL_MERGE_ZEALOUS, style=opt.conflict_style, favor=favor,
        flags=opt.xdl_flags, marker_size=marker_size,
        name1=name1, name2=name2, ancestor_name=ancestor_name)
    return result, (1 if nconf > 0 else 0)


def merge_3way(opt: Opt, path: str, o: VersionInfo, a: VersionInfo,
               b: VersionInfo, pathnames: list, extra_marker_size: int
               ) -> tuple[bytes, int]:
    if pathnames[0] == pathnames[1] == pathnames[2]:
        base = opt.ancestor
        name1 = opt.branch1
        name2 = opt.branch2
    else:
        base = f"{opt.ancestor}:{pathnames[0]}"
        name1 = f"{opt.branch1}:{pathnames[1]}"
        name2 = f"{opt.branch2}:{pathnames[2]}"

    virtual_ancestor = opt.call_depth > 0
    variant = 0 if virtual_ancestor else opt.variant
    two_way = (S_IFMT & o.mode) != (S_IFMT & a.mode)
    orig = b"" if two_way else _read_blob(opt, o.oid)
    src1 = _read_blob(opt, a.oid)
    src2 = _read_blob(opt, b.oid)
    marker_size = opt.marker_size(path) + extra_marker_size
    driver = opt.merge_driver(path)
    return _ll_merge(opt, orig, src1, src2, name1=name1, name2=name2,
                     ancestor_name=base, marker_size=marker_size,
                     virtual_ancestor=virtual_ancestor, variant=variant,
                     driver=driver)


def handle_content_merge(opt: Opt, path: str, o: VersionInfo, a: VersionInfo,
                         b: VersionInfo, pathnames: list,
                         extra_marker_size: int) -> tuple[int, VersionInfo]:
    """Returns (clean, result VersionInfo)."""
    result = VersionInfo()
    clean = 1

    # merge modes
    if a.mode == b.mode or a.mode == o.mode:
        result.mode = b.mode
    else:
        result.mode = a.mode
        clean = 1 if (b.mode == o.mode) else 0

    # trivial oid merge
    if a.oid == b.oid or a.oid == o.oid:
        result.oid = b.oid
    elif b.oid == o.oid:
        result.oid = a.oid
    elif s_isreg(a.mode):
        merged, status = merge_3way(opt, path, o, a, b, pathnames, extra_marker_size)
        result.oid = objs.write_object(opt.repo, "blob", merged)
        clean = clean & (1 if status == 0 else 0)
    elif s_isgitlink(a.mode):
        two_way = (S_IFMT & o.mode) != (S_IFMT & a.mode)
        clean, result.oid = merge_submodule(
            opt, pathnames[0], None if two_way else o.oid, a.oid, b.oid)
        if opt.call_depth and two_way and not clean:
            result.mode = o.mode
            result.oid = o.oid
    elif s_islnk(a.mode):
        if opt.call_depth:
            clean = 0
            result.mode = o.mode
            result.oid = o.oid
        else:
            if opt.variant == xdiff.XDL_MERGE_FAVOR_THEIRS:
                result.oid = b.oid
            elif opt.variant == xdiff.XDL_MERGE_FAVOR_OURS:
                result.oid = a.oid
            else:
                clean = 0
                result.oid = a.oid
    else:
        clean = 0
        result.oid = a.oid

    return clean, result


def merge_submodule(opt: Opt, path: str, o_oid: Optional[str], a_oid: str,
                    b_oid: str) -> tuple[int, str]:
    """Port of merge_submodule: fast-forward when one submodule commit
    contains the other; otherwise leave a conflict. Requires the submodule's
    object store to be available, matching git (which conflicts otherwise)."""
    # fallback answer in case we fail
    fallback = o_oid if (opt.call_depth and o_oid is not None) else a_oid

    subrepo = _open_submodule(opt, path)
    if subrepo is None:
        return 0, fallback
    if o_oid is None or opt.is_null(o_oid):
        return 0, fallback
    from . import merge as _merge

    def present(sha):
        return objs.object_exists(subrepo, sha)

    if not (present(o_oid) and present(a_oid) and present(b_oid)):
        return 0, fallback
    # both changes must be forward from base
    if not (_merge.is_ancestor(subrepo, o_oid, a_oid) and
            _merge.is_ancestor(subrepo, o_oid, b_oid)):
        return 0, fallback
    # case 1: a contained in b or vice versa -> fast-forward
    if _merge.is_ancestor(subrepo, a_oid, b_oid):
        return 1, b_oid
    if _merge.is_ancestor(subrepo, b_oid, a_oid):
        return 1, a_oid
    # case 2: a merge exists -> leave conflict (suggestion only)
    return 0, fallback


def _open_submodule(opt: Opt, path: str) -> Optional[Repository]:
    """Locate and open a submodule's repository, or None if unavailable."""
    cache = opt._submodule_cache
    if path in cache:
        return cache[path]
    repo = opt.repo
    candidates = [
        repo.gitdir / "modules" / path,
        repo.path / path / ".git",
        repo.path / path,
    ]
    sub: Optional[Repository] = None
    for cand in candidates:
        try:
            if cand.is_dir() and (cand / "objects").is_dir():
                sub = Repository(cand.parent if cand.name == ".git" else cand)
                break
            if cand.is_file():
                # gitlink file ".git" pointing to actual gitdir
                text = cand.read_text(encoding="utf-8", errors="replace")
                if text.startswith("gitdir:"):
                    target = (cand.parent / text.split(":", 1)[1].strip()).resolve()
                    if (target / "objects").is_dir():
                        sub = Repository(target)
                        break
        except (OSError, ValueError):
            continue
    cache[path] = sub
    return sub


# ---------------------------------------------------------------------------
# rename detection


def _leaf_map(opt: Opt, tree: Optional[str]) -> dict[str, tuple[int, str]]:
    """Flatten a tree into {path: (mode_int, oid)} for file-like leaves
    (regular files, symlinks, gitlinks)."""
    out: dict[str, tuple[int, str]] = {}
    if tree is None or opt.is_null(tree):
        return out
    stack = [("", tree)]
    while stack:
        prefix, toid = stack.pop()
        for name, (mode, oid, isd) in _tree_entries(opt, toid).items():
            path = f"{prefix}{name}"
            if isd:
                stack.append((path + "/", oid))
            else:
                out[path] = (mode, oid)
    return out


def _unique_path(opt: Opt, path: str, branch: str) -> str:
    flat = branch.replace("/", "_")
    base = f"{path}~{flat}"
    newpath = base
    suffix = 0
    while newpath in opt.paths:
        newpath = f"{base}_{suffix}"
        suffix += 1
    return newpath


def _parent(path: str) -> str:
    i = path.rfind("/")
    return path[:i] if i >= 0 else ""


def _basename_of(path: str) -> str:
    i = path.rfind("/")
    return path[i + 1:] if i >= 0 else path


class _Pair:
    __slots__ = ("status", "one_path", "two_path")

    def __init__(self, status, one_path, two_path):
        self.status = status
        self.one_path = one_path
        self.two_path = two_path


# relevance of a removed directory for directory-rename detection
_NOT_RELEVANT = 0
_RELEVANT_FOR_ANCESTOR = 1
_RELEVANT_FOR_SELF = 2


def _update_dir_rename_counts(counts: dict, removed_dirs: set, relevance: dict,
                              oldname: str, newname: str) -> None:
    old_dir = oldname
    new_dir = newname
    first = True
    while True:
        old_stripped = _basename_of(old_dir)
        old_dir = _parent(old_dir)
        if old_dir not in removed_dirs:
            break
        new_stripped = _basename_of(new_dir)
        new_dir = _parent(new_dir)
        if not first and old_stripped != new_stripped:
            break
        drd = relevance.get(old_dir, _NOT_RELEVANT)
        if drd == _RELEVANT_FOR_SELF or first:
            d = counts.setdefault(old_dir, {})
            d[new_dir] = d.get(new_dir, 0) + 1
        first = False
        if drd == _NOT_RELEVANT:
            break
        if old_dir == "" or new_dir == "":
            break


def _get_provisional_directory_renames(counts: dict) -> tuple[dict, bool]:
    dir_renames: dict[str, str] = {}
    clean = True
    for source_dir, targets in counts.items():
        max_count = 0
        bad_max = 0
        best = None
        for target_dir, count in targets.items():
            if count == max_count:
                bad_max = max_count
            elif count > max_count:
                max_count = count
                best = target_dir
        if max_count == 0:
            continue
        if bad_max == max_count:
            clean = False  # directory rename split
        else:
            dir_renames[source_dir] = best
    return dir_renames, clean


def _apply_dir_rename(old_dir: str, new_dir: str, old_path: str) -> str:
    oldlen = len(old_dir)
    if new_dir == "":
        oldlen += 1  # advance past the '/'
    return new_dir + old_path[oldlen:]


def _check_dir_renamed(path: str, dir_renames: dict) -> Optional[tuple[str, str]]:
    d = _parent(path)
    while True:
        if d in dir_renames:
            return (d, dir_renames[d])
        if d == "":
            return None
        d = _parent(d)


def _path_in_way(opt: Opt, path: str, side_mask: int) -> bool:
    mi = opt.paths.get(path)
    if mi is None:
        return False
    if mi.clean:
        return True
    return bool(side_mask & (mi.filemask | mi.dirmask))


def _compute_collisions(dir_renames_other: dict, pairs: list) -> dict:
    collisions: dict[str, dict] = {}
    if not dir_renames_other:
        return collisions
    for p in pairs:
        if p.status not in ("A", "R"):
            continue
        info = _check_dir_renamed(p.two_path, dir_renames_other)
        if info is None:
            continue
        new_path = _apply_dir_rename(info[0], info[1], p.two_path)
        c = collisions.get(new_path)
        if c is None:
            c = {"source_files": set(), "reported_already": False}
            collisions[new_path] = c
        c["source_files"].add(p.two_path)
    return collisions


def _handle_path_level_conflicts(opt: Opt, path: str, side_index: int,
                                 rename_info: tuple, collisions_side: dict
                                 ) -> Optional[str]:
    new_path = _apply_dir_rename(rename_info[0], rename_info[1], path)
    c_info = collisions_side.get(new_path)
    clean = True
    if c_info is None:
        c_info = {"source_files": {path}, "reported_already": False}
    if c_info["reported_already"]:
        clean = False
    elif _path_in_way(opt, new_path, 1 << side_index):
        c_info["reported_already"] = True
        clean = False
    elif len(c_info["source_files"]) > 1:
        c_info["reported_already"] = True
        clean = False
    if not clean:
        return None
    return new_path


def _check_for_directory_rename(opt: Opt, path: str, side_index: int,
                                dir_renames: dict, dir_rename_exclusions: dict,
                                collisions: dict) -> Optional[str]:
    other_side = 3 - side_index
    if not dir_renames:
        return None
    if path in collisions[other_side]:
        return None
    rename_info = _check_dir_renamed(path, dir_renames)
    if rename_info is None:
        return None
    new_dir = rename_info[1]
    if new_dir in dir_rename_exclusions:
        return None
    return _handle_path_level_conflicts(opt, path, side_index, rename_info,
                                        collisions[side_index])


def _apply_directory_rename_modifications(opt: Opt, pair: _Pair, new_path: str) -> None:
    old_path = pair.two_path
    ci = opt.paths[old_path]

    # ensure parent directories of new_path exist in opt.paths
    cur_path = new_path
    dirs_to_insert: list[str] = []
    parent_name = ""
    while True:
        last = cur_path.rfind("/")
        if last >= 0:
            parent_name = cur_path[:last]
        else:
            parent_name = ""
            break
        if parent_name in opt.paths:
            break
        dirs_to_insert.append(parent_name)
        cur_path = parent_name
    for cur_dir in reversed(dirs_to_insert):
        dir_ci = CI()
        dir_ci.clean = False
        dir_ci.directory_name = parent_name
        dir_ci.basename_offset = (len(parent_name) + 1) if parent_name else 0
        dir_ci.dirmask = ci.filemask
        dir_ci.is_null = True
        opt.paths[cur_dir] = dir_ci
        parent_name = cur_dir

    if ci.dirmask == 0:
        opt.paths.pop(old_path, None)
    else:
        new_ci = CI()
        _copy_ci(new_ci, ci)
        new_ci.dirmask = 0
        new_ci.stages[1] = VersionInfo(0, opt.null_oid)
        ci.filemask = 0
        ci.clean = True
        for i in range(3):
            if ci.dirmask & (1 << i):
                continue
            ci.stages[i] = VersionInfo(0, opt.null_oid)
        ci = new_ci

    ci.directory_name = parent_name
    ci.basename_offset = (len(parent_name) + 1) if parent_name else 0
    existing = opt.paths.get(new_path)
    if existing is None:
        opt.paths[new_path] = ci
    else:
        existing.filemask |= ci.filemask
        if existing.dirmask:
            existing.df_conflict = True
        index = ci.filemask >> 1
        existing.pathnames[index] = ci.pathnames[index]
        existing.stages[index] = VersionInfo(ci.stages[index].mode,
                                             ci.stages[index].oid)
        ci = existing

    # detect_directory_renames "conflict" (default) marks a path conflict;
    # "true" applies the rename silently (clean).
    if opt.detect_directory_renames != 2:
        ci.path_conflict = True
    pair.two_path = new_path


def detect_and_process_renames(opt: Opt, base: Optional[str], s1: Optional[str],
                               s2: Optional[str]) -> int:
    base_map = _leaf_map(opt, base)
    side_maps = {1: _leaf_map(opt, s1), 2: _leaf_map(opt, s2)}

    removed_dirs = {}
    pairs = {1: [], 2: []}
    dir_rename_count = {1: {}, 2: {}}
    dir_renames = {1: {}, 2: {}}
    clean = 1

    relevance = opt.dirs_removed_relevance
    for side in (1, 2):
        side_map = side_maps[side]
        rps = diffcore.detect_renames(opt.repo, base_map, side_map,
                                      rename_limit=opt.rename_limit,
                                      relevant_sources=opt.relevant_sources[side])
        renamed_dsts = {p.dst.path for p in rps}
        # removed dirs on this side come from collect_merge_info (with relevance)
        removed_dirs[side] = set(relevance[side])
        # only renames with a relevant source feed directory-rename counting
        # (exact renames of irrelevant sources still move the file, but must
        # not imply a directory rename)
        for p in rps:
            if p.src.path in opt.relevant_sources[side]:
                _update_dir_rename_counts(dir_rename_count[side],
                                          removed_dirs[side], relevance[side],
                                          p.src.path, p.dst.path)
            pairs[side].append(_Pair("R", p.src.path, p.dst.path))
        # add pairs: files added on this side, not rename dests
        for path in sorted(side_map):
            if path in base_map or path in renamed_dsts:
                continue
            pairs[side].append(_Pair("A", path, path))

    for side in (1, 2):
        dr, c = _get_provisional_directory_renames(dir_rename_count[side])
        dir_renames[side] = dr
        clean &= c

    # handle_directory_level_conflicts: drop dirs renamed identically on both
    dup = [k for k in dir_renames[1] if k in dir_renames[2]]
    for k in dup:
        del dir_renames[1][k]
        del dir_renames[2][k]

    collisions = {
        1: _compute_collisions(dir_renames[2], pairs[1]),
        2: _compute_collisions(dir_renames[1], pairs[2]),
    }

    # collect_renames: apply directory renames, build combined queue
    combined: list[tuple[str, str, int]] = []
    for side in (1, 2):
        other = 3 - side
        for p in pairs[side]:
            if p.status not in ("A", "R"):
                continue
            new_path = _check_for_directory_rename(
                opt, p.two_path, side, dir_renames[other], dir_renames[side],
                collisions)
            if p.status != "R" and not new_path:
                continue
            if new_path:
                _apply_directory_rename_modifications(opt, p, new_path)
            combined.append((p.one_path, p.two_path, side))

    combined.sort(key=lambda t: t[0])
    clean &= process_renames(opt, combined)
    return clean


def process_renames(opt: Opt, renames: list) -> int:
    clean_merge = 1
    i = 0
    n = len(renames)
    while i < n:
        oldpath, newpath, side = renames[i]
        oldinfo = opt.paths.get(oldpath)
        newinfo = opt.paths.get(newpath)
        actual_newpath = newpath

        if oldinfo is None or oldinfo.clean:
            i += 1
            continue

        # rename/rename(1to2) or (1to1): next pair shares oldpath
        if i + 1 < n and renames[i + 1][0] == oldpath:
            p0 = oldpath
            p1 = newpath
            p2 = renames[i + 1][1]
            base = opt.paths.get(p0)
            sidea = opt.paths.get(p1)
            sideb = opt.paths.get(p2)
            if p1 == p2:
                # both sides renamed the same way (1to1)
                sidea.stages[0] = VersionInfo(base.stages[0].mode, base.stages[0].oid)
                sidea.filemask |= 1
                base.is_null = True
                base.clean = True
                i += 2
                continue
            # rename/rename(1to2)
            pathnames = [p0, p1, p2]
            clean, merged = handle_content_merge(
                opt, oldpath, base.stages[0], sidea.stages[1], sideb.stages[2],
                pathnames, 1 + 2 * opt.call_depth)
            was_binary_blob = 0
            if (not clean and merged.mode == sidea.stages[1].mode and
                    merged.oid == sidea.stages[1].oid):
                was_binary_blob = 1
            sidea.stages[1] = VersionInfo(merged.mode, merged.oid)
            if was_binary_blob:
                merged = VersionInfo(sideb.stages[2].mode, sideb.stages[2].oid)
            sideb.stages[2] = VersionInfo(merged.mode, merged.oid)
            sidea.path_conflict = True
            sideb.path_conflict = True
            base.path_conflict = True
            clean_merge = clean_merge & clean
            i += 2
            continue

        target_index = side
        other_source_index = 3 - target_index
        old_sidemask = (1 << other_source_index)
        source_deleted = (oldinfo.filemask == 1)
        collision = ((newinfo.filemask & old_sidemask) != 0) if newinfo else False
        type_changed = (not source_deleted and newinfo is not None and
                        (s_isreg(oldinfo.stages[other_source_index].mode) !=
                         s_isreg(newinfo.stages[target_index].mode)))
        if type_changed and collision:
            collision = False

        rename_branch = delete_branch = None
        if source_deleted:
            if target_index == 1:
                rename_branch, delete_branch = opt.branch1, opt.branch2
            else:
                rename_branch, delete_branch = opt.branch2, opt.branch1

        if collision and not source_deleted:
            # rename/add or rename/rename(2to1)
            pathnames = [None, None, None]
            pathnames[0] = oldpath
            pathnames[other_source_index] = oldpath
            pathnames[target_index] = actual_newpath
            base = opt.paths.get(pathnames[0])
            sidea = opt.paths.get(pathnames[1])
            sideb = opt.paths.get(pathnames[2])
            clean, merged = handle_content_merge(
                opt, oldpath, base.stages[0], sidea.stages[1], sideb.stages[2],
                pathnames, 1 + 2 * opt.call_depth)
            newinfo.stages[target_index] = VersionInfo(merged.mode, merged.oid)
            clean_merge = clean_merge & clean
        elif collision and source_deleted:
            # rename/add/delete or rename/rename(2to1)/delete
            newinfo.path_conflict = True
        else:
            newinfo.stages[0] = VersionInfo(oldinfo.stages[0].mode, oldinfo.stages[0].oid)
            newinfo.filemask |= 1
            newinfo.pathnames[0] = oldpath
            if type_changed:
                oldinfo.stages[0] = VersionInfo(0, opt.null_oid)
                oldinfo.filemask &= 0x06
            elif source_deleted:
                newinfo.path_conflict = True
            else:
                newinfo.stages[other_source_index] = VersionInfo(
                    oldinfo.stages[other_source_index].mode,
                    oldinfo.stages[other_source_index].oid)
                newinfo.filemask |= (1 << other_source_index)
                newinfo.pathnames[other_source_index] = oldpath

        if not type_changed:
            oldinfo.is_null = True
            oldinfo.clean = True

        i += 1

    return clean_merge


# ---------------------------------------------------------------------------
# process_entry / process_entries


def process_entry(opt: Opt, path: str, ci: CI, dm: "DirVersions") -> None:
    df_file_index = 0

    if ci.dirmask:
        record_entry_for_tree(dm, path, ci)
        if ci.filemask == 0:
            return

    if ci.df_conflict and ci.result_mode == 0:
        ci.df_conflict = False
        ci.clean = False
        ci.is_null = False
        ci.match_mask = ci.match_mask & ~ci.dirmask
        ci.dirmask = 0
        for i in range(3):
            if ci.filemask & (1 << i):
                continue
            ci.stages[i] = VersionInfo(0, opt.null_oid)
    elif ci.df_conflict and ci.result_mode != 0:
        # directory remained; move the file out of the way
        if ci.filemask == 1:
            ci.filemask = 0
            return
        new_ci = CI()
        _copy_ci(new_ci, ci)
        new_ci.match_mask = new_ci.match_mask & ~new_ci.dirmask
        new_ci.dirmask = 0
        for i in range(3):
            if new_ci.filemask & (1 << i):
                continue
            new_ci.stages[i] = VersionInfo(0, opt.null_oid)
        df_file_index = 2 if (ci.dirmask & 2) else 1
        branch = opt.branch1 if df_file_index == 1 else opt.branch2
        path = _unique_path(opt, path, branch)
        opt.paths[path] = new_ci
        ci.filemask = 0
        ci = new_ci

    if ci.match_mask:
        ci.clean = not ci.df_conflict and not ci.path_conflict
        if ci.match_mask == 6:
            ci.result_mode = ci.stages[1].mode
            ci.result_oid = ci.stages[1].oid
        else:
            othermask = 7 & ~ci.match_mask
            sidei = 2 if othermask == 4 else 1
            ci.result_mode = ci.stages[sidei].mode
            ci.is_null = not ci.result_mode
            if ci.is_null:
                ci.clean = True
            ci.result_oid = ci.stages[sidei].oid
    elif ci.filemask >= 6 and (S_IFMT & ci.stages[1].mode) != (S_IFMT & ci.stages[2].mode):
        # two different items (file/submodule/symlink) on the two sides
        if opt.call_depth:
            # virtual ancestor: just use the merge-base version
            ci.clean = False
            ci.result_oid = ci.stages[0].oid
            ci.result_mode = ci.stages[0].mode
            ci.is_null = (ci.result_mode == 0)
        else:
            o_mode = ci.stages[0].mode
            a_mode = ci.stages[1].mode
            b_mode = ci.stages[2].mode
            rename_a = rename_b = 0
            if s_isreg(a_mode):
                rename_a = 1
            elif s_isreg(b_mode):
                rename_b = 1
            else:
                rename_a = rename_b = 1
            a_path = _unique_path(opt, path, opt.branch1) if rename_a else None
            b_path = _unique_path(opt, path, opt.branch2) if rename_b else None

            ci.clean = False
            new_ci = CI()
            _copy_ci(new_ci, ci)

            new_ci.result_mode = ci.stages[2].mode
            new_ci.result_oid = ci.stages[2].oid
            new_ci.stages[1] = VersionInfo(0, opt.null_oid)
            new_ci.filemask = 5
            if (S_IFMT & b_mode) != (S_IFMT & o_mode):
                new_ci.stages[0] = VersionInfo(0, opt.null_oid)
                new_ci.filemask = 4

            ci.result_mode = ci.stages[1].mode
            ci.result_oid = ci.stages[1].oid
            ci.stages[2] = VersionInfo(0, opt.null_oid)
            ci.filemask = 3
            if (S_IFMT & a_mode) != (S_IFMT & o_mode):
                ci.stages[0] = VersionInfo(0, opt.null_oid)
                ci.filemask = 2

            if rename_a:
                opt.paths[a_path] = ci
            if not rename_b:
                b_path = path
            opt.paths[b_path] = new_ci
            if rename_a and rename_b:
                opt.paths.pop(path, None)

            new_ci.clean = False
            opt.conflicted[b_path] = new_ci
            record_entry_for_tree(dm, b_path, new_ci)
            if a_path:
                path = a_path
    elif ci.filemask >= 6:
        # content merge (two-way or three-way)
        o = ci.stages[0]
        a = ci.stages[1]
        b = ci.stages[2]
        clean_merge, merged = handle_content_merge(
            opt, path, o, a, b, ci.pathnames, opt.call_depth * 2)
        ci.clean = bool(clean_merge) and not ci.df_conflict and not ci.path_conflict
        ci.result_mode = merged.mode
        ci.is_null = (merged.mode == 0)
        ci.result_oid = merged.oid
        if clean_merge and ci.df_conflict:
            ci.filemask = 1 << df_file_index
            ci.stages[df_file_index] = VersionInfo(merged.mode, merged.oid)
    elif ci.filemask in (3, 5):
        # modify/delete
        sidei = 2 if ci.filemask == 5 else 1
        index = 0 if opt.call_depth else sidei
        ci.result_mode = ci.stages[index].mode
        ci.result_oid = ci.stages[index].oid
        ci.clean = False
    elif ci.filemask in (2, 4):
        # added on one side
        sidei = 2 if ci.filemask == 4 else 1
        ci.result_mode = ci.stages[sidei].mode
        ci.result_oid = ci.stages[sidei].oid
        ci.clean = not ci.df_conflict and not ci.path_conflict
    elif ci.filemask == 1:
        # deleted on both sides
        ci.is_null = True
        ci.result_mode = 0
        ci.result_oid = opt.null_oid
        ci.clean = not ci.path_conflict

    if not ci.clean:
        opt.conflicted[path] = ci

    record_entry_for_tree(dm, path, ci)


def _copy_ci(dst: CI, src: CI) -> None:
    dst.result_mode = src.result_mode
    dst.result_oid = src.result_oid
    dst.is_null = src.is_null
    dst.clean = src.clean
    dst.directory_name = src.directory_name
    dst.basename_offset = src.basename_offset
    dst.stages = [VersionInfo(s.mode, s.oid) for s in src.stages]
    dst.pathnames = list(src.pathnames)
    dst.filemask = src.filemask
    dst.dirmask = src.dirmask
    dst.match_mask = src.match_mask
    dst.df_conflict = src.df_conflict
    dst.path_conflict = src.path_conflict


# --- streaming tree assembly (write_completed_directory / write_tree) ------


class DirVersions:
    __slots__ = ("versions", "offsets", "last_directory", "last_directory_len")

    def __init__(self):
        self.versions: list[tuple[str, CI]] = []
        self.offsets: list[tuple[str, int]] = []
        self.last_directory: Optional[str] = None
        self.last_directory_len = 0


def record_entry_for_tree(dm: DirVersions, path: str, ci: CI) -> None:
    if ci.is_null:
        return
    basename = path[ci.basename_offset:]
    dm.versions.append((basename, ci))


def _write_tree(opt: Opt, versions: list, offset: int) -> str:
    entries = []
    for basename, ci in versions[offset:]:
        mode_str = f"{ci.result_mode:o}"
        entries.append(objs.TreeEntry(mode_str, basename, ci.result_oid))
    data = objs.encode_tree(entries)
    return objs.write_object(opt.repo, "tree", data)


def write_completed_directory(opt: Opt, new_directory_name: str,
                              dm: DirVersions) -> None:
    if new_directory_name == dm.last_directory:
        return
    if (dm.last_directory is None or
            new_directory_name.startswith(dm.last_directory)):
        offset = len(dm.versions)
        dm.last_directory = new_directory_name
        dm.last_directory_len = len(new_directory_name)
        dm.offsets.append((dm.last_directory, offset))
        return
    dir_info = opt.paths[dm.last_directory]
    offset = dm.offsets[-1][1]
    if offset == len(dm.versions):
        dir_info.is_null = True
    else:
        dir_info.is_null = False
        dir_info.result_mode = S_IFDIR
        dir_info.result_oid = _write_tree(opt, dm.versions, offset)
    dm.offsets.pop()
    del dm.versions[offset:]
    prev_dir = dm.offsets[-1][0] if dm.offsets else None
    if new_directory_name != prev_dir:
        dm.offsets.append((new_directory_name, len(dm.versions)))
    dm.last_directory = new_directory_name
    dm.last_directory_len = len(new_directory_name)


def process_entries(opt: Opt) -> str:
    if not opt.paths:
        empty, _ = objs.hash_bytes("tree", b"", opt.repo)
        objs.write_object(opt.repo, "tree", b"")
        return empty

    dm = DirVersions()
    # iterate in reverse of the "dirs next to their children" order so paths
    # below a directory are handled before the directory itself.
    plist = sorted(opt.paths.keys(), key=_DirSortKey)
    for path in reversed(plist):
        ci = opt.paths.get(path)
        if ci is None:
            continue
        write_completed_directory(opt, ci.directory_name, dm)
        if ci.clean:
            record_entry_for_tree(dm, path, ci)
        else:
            process_entry(opt, path, ci, dm)

    return _write_tree(opt, dm.versions, 0)


@functools.total_ordering
class _DirSortKey:
    """Sort key implementing sort_dirs_next_to_their_children: a path sorts as
    though a '/' were appended, so directories sort immediately before their
    children."""
    __slots__ = ("s",)

    def __init__(self, s: str):
        self.s = s

    def __eq__(self, other):
        return self.s == other.s

    def __lt__(self, other):
        return _dir_cmp(self.s, other.s) < 0


def _dir_cmp(one: str, two: str) -> int:
    i = 0
    lo = len(one)
    lt = len(two)
    while i < lo and i < lt and one[i] == two[i]:
        i += 1
    c1 = ord(one[i]) if i < lo else ord("/")
    c2 = ord(two[i]) if i < lt else ord("/")
    if c1 == c2:
        # one is a leading directory of the other
        return 1 if i < lo else -1
    return c1 - c2


# ---------------------------------------------------------------------------
# public entry point


def conflicted_stages(opt: Opt) -> list[tuple[str, int, int, str]]:
    """Return sorted (path, stage, mode, oid) entries for all conflicted
    paths, matching ``git merge-tree`` conflicted-file output."""
    out: list[tuple[str, int, int, str]] = []
    for path, ci in opt.conflicted.items():
        for i in range(3):
            if ci.filemask & (1 << i):
                vi = ci.stages[i]
                out.append((path, i + 1, vi.mode, vi.oid))
    out.sort(key=lambda t: (t[0], t[1]))
    return out


def merge_incore_nonrecursive(opt: Opt, base: Optional[str], s1: Optional[str],
                              s2: Optional[str]) -> tuple[str, bool]:
    """Run the merge; return (tree_oid, clean)."""
    collect_merge_info(opt, base, s1, s2)
    clean = detect_and_process_renames(opt, base, s1, s2)
    tree = process_entries(opt)
    clean = clean and not opt.conflicted
    return tree, bool(clean)


# ---------------------------------------------------------------------------
# recursive merge (virtual merge base) — merge_ort_internal


@dataclass
class MergeConfig:
    """Merge configuration carried through the recursion (mirrors the subset
    of merge_options that affects results)."""
    conflict_style: int = 0          # 0 merge / 1 diff3 / 2 zdiff3
    variant: int = 0                 # 0 / FAVOR_OURS / FAVOR_THEIRS
    xdl_flags: int = xdiff.XDF_HISTOGRAM_DIFF
    attributes: Optional["MergeAttributes"] = None
    rename_detection: bool = True
    rename_limit: int = 7000
    detect_directory_renames: int = 1


def _commit_tree(repo: Repository, commit_sha: str) -> str:
    t, data = objs.read_object(repo, commit_sha)
    if t == "commit":
        return objs.parse_commit(data).tree
    if t == "tree":
        return commit_sha
    raise ValueError(f"{commit_sha} is not a commit or tree")


def _commit_time(repo: Repository, commit_sha: str) -> int:
    try:
        t, data = objs.read_object(repo, commit_sha)
        if t != "commit":
            return 0
        c = objs.parse_commit(data)
        return int(c.committer.rsplit(" ", 2)[-2])
    except (ValueError, IndexError, KeyError):
        return 0


def _empty_tree(repo: Repository) -> str:
    sha, _ = objs.hash_bytes("tree", b"", repo)
    objs.write_object(repo, "tree", b"")
    return sha


def _make_virtual_commit(repo: Repository, tree_sha: str, parents: list) -> str:
    # keep committer date monotonically newer than parents so merge-base
    # date heuristics behave; the sha is only used for ancestry computation.
    when = 1
    for p in parents:
        when = max(when, _commit_time(repo, p) + 1)
    sig = objs.format_signature("merge", "merge@ort", when=when)
    c = objs.Commit(tree=tree_sha, parents=list(parents), author=sig,
                    committer=sig, message="virtual merge base\n")
    return objs.write_object(repo, "commit", c.encode())


def _abbrev(repo: Repository, sha: str) -> str:
    # git uses repo_find_unique_abbrev (>= 7 chars, extended on collision).
    from . import loose as _loose
    for n in range(7, repo.hex_len):
        prefix = sha[:n]
        try:
            m = _loose.resolve_short(repo, prefix)
        except Exception:
            m = None
        # resolve_short returns "" for ambiguous in this codebase; treat any
        # ambiguity by extending. We only need uniqueness, so accept length 7
        # unless we can detect ambiguity cheaply.
        if m is None or m == sha:
            return prefix
        if m:
            return prefix
    return sha


def _merge_ort_internal(repo: Repository, merge_bases: list, h1: str, h2: str,
                        branch1: str, branch2: str, call_depth: int,
                        cfg: MergeConfig) -> tuple[str, bool, Optional[Opt]]:
    """Port of merge_ort_internal. merge_bases is a list of commit shas in the
    order git uses (already reversed). Returns (tree, clean, opt) where opt is
    only meaningful at call_depth 0 (for conflicted-stage extraction)."""
    bases = list(merge_bases)
    if not bases:
        merged_tree = _empty_tree(repo)
        merged_commit = _make_virtual_commit(repo, merged_tree, [])
        ancestor_name = "empty tree"
    elif len(bases) > 1:
        merged_commit = bases.pop(0)
        merged_tree = _commit_tree(repo, merged_commit)
        ancestor_name = "merged common ancestors"
    else:
        merged_commit = bases.pop(0)
        merged_tree = _commit_tree(repo, merged_commit)
        ancestor_name = _abbrev(repo, merged_commit)

    from . import merge as _merge
    for nxt in bases:
        prev = merged_commit
        inner = list(reversed(_merge.merge_bases(repo, prev, nxt)))
        inner_tree, _c, _o = _merge_ort_internal(
            repo, inner, prev, nxt,
            "Temporary merge branch 1", "Temporary merge branch 2",
            call_depth + 1, cfg)
        merged_commit = _make_virtual_commit(repo, inner_tree, [prev, nxt])
        merged_tree = inner_tree

    opt = Opt(repo, ancestor_name, branch1, branch2,
              conflict_style=cfg.conflict_style, variant=cfg.variant,
              xdl_flags=cfg.xdl_flags, attributes=cfg.attributes,
              rename_detection=cfg.rename_detection,
              rename_limit=cfg.rename_limit,
              detect_directory_renames=cfg.detect_directory_renames)
    opt.call_depth = call_depth
    tree, clean = merge_incore_nonrecursive(
        opt, merged_tree, _commit_tree(repo, h1), _commit_tree(repo, h2))
    return tree, clean, opt


def merge_recursive(repo: Repository, h1: str, h2: str, *, branch1: str,
                    branch2: str, merge_bases: Optional[list] = None,
                    cfg: Optional[MergeConfig] = None) -> tuple[str, bool, Opt]:
    """Merge two commits using the recursive (virtual merge base) strategy,
    matching `git merge-tree <h1> <h2>` (no --merge-base). Returns
    (tree, clean, opt)."""
    if cfg is None:
        cfg = MergeConfig()
    from . import merge as _merge
    if merge_bases is None:
        merge_bases = list(reversed(_merge.merge_bases(repo, h1, h2)))
    tree, clean, opt = _merge_ort_internal(
        repo, merge_bases, h1, h2, branch1, branch2, 0, cfg)
    return tree, clean, opt
