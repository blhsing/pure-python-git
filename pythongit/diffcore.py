"""Pure-Python port of Git's rename detection (diffcore-rename.c) plus the
content-similarity estimator it relies on (diffcore-delta.c spanhash).

This reproduces the same rename pairings (exact, basename-driven, and the
inexact NxM similarity matrix) and the same similarity scores that
``diffcore_rename_extended`` computes, so that merge-ort rename handling
matches C Git.  Directory-rename detection is handled by the caller
(merge-ort); this module only finds file-level renames.

Sources ported (git v2.44.0): diffcore-delta.c, diffcore-rename.c.
"""
from __future__ import annotations

import functools
from typing import Optional

from . import objects as objs
from .repo import Repository

MAX_SCORE = 60000.0
DEFAULT_RENAME_SCORE = 30000  # 50%
DEFAULT_BREAK_SCORE = 30000   # 50%
DEFAULT_MERGE_SCORE = 36000   # 60%
MINIMUM_BREAK_SIZE = 400      # do not break a file smaller than this
NUM_CANDIDATE_PER_DST = 4
HASHBASE = 107927
INITIAL_HASH_SIZE = 9
FIRST_FEW_BYTES = 8000
S_IFMT = 0o170000
S_IFREG = 0o100000


def buffer_is_binary(data: bytes) -> bool:
    return b"\x00" in data[:FIRST_FEW_BYTES]


def _basename(path: str) -> str:
    i = path.rfind("/")
    return path[i + 1 :] if i >= 0 else path


def s_isreg(mode: int) -> bool:
    return (mode & S_IFMT) == S_IFREG


class Filespec:
    __slots__ = ("path", "oid", "mode", "_data", "_size", "_binary",
                 "_spanhash", "rename_used", "repo")

    def __init__(self, repo: Repository, path: str, oid: str, mode: int,
                 data: Optional[bytes] = None):
        self.repo = repo
        self.path = path
        self.oid = oid
        self.mode = mode
        # ``data`` may be supplied directly for blobs that exist only in the
        # working tree (diff-files destinations, whose oid is not in the store).
        self._data: Optional[bytes] = data
        self._size: Optional[int] = None
        self._binary: Optional[bool] = None
        self._spanhash: Optional[list[tuple[int, int]]] = None
        self.rename_used = 0

    @property
    def data(self) -> bytes:
        if self._data is None:
            try:
                t, d = objs.read_object(self.repo, self.oid)
            except KeyError:
                t, d = "blob", b""
            self._data = d
        return self._data

    @property
    def size(self) -> int:
        if self._size is None:
            self._size = len(self.data)
        return self._size

    @property
    def is_binary(self) -> bool:
        if self._binary is None:
            self._binary = buffer_is_binary(self.data)
        return self._binary

    @property
    def spanhash(self) -> list[tuple[int, int]]:
        if self._spanhash is None:
            self._spanhash = _hash_chars(self)
        return self._spanhash


def _hash_chars(fs: Filespec) -> list[tuple[int, int]]:
    """Port of diffcore-delta.c hash_chars: returns a list of (hashval, cnt)
    sorted ascending by hashval (counts merged per hashval)."""
    buf = fs.data
    is_text = not fs.is_binary
    counts: dict[int, int] = {}
    accum1 = 0
    accum2 = 0
    n = 0
    i = 0
    ln = len(buf)
    while i < ln:
        c = buf[i]
        old_1 = accum1
        i += 1
        if is_text and c == 0x0D and i < ln and buf[i] == 0x0A:
            continue
        accum1 = ((accum1 << 7) ^ (accum2 >> 25)) & 0xFFFFFFFF
        accum2 = ((accum2 << 7) ^ (old_1 >> 25)) & 0xFFFFFFFF
        accum1 = (accum1 + c) & 0xFFFFFFFF
        n += 1
        if n < 64 and c != 0x0A:
            continue
        hashval = (accum1 + accum2 * 0x61) % HASHBASE
        counts[hashval] = counts.get(hashval, 0) + n
        n = 0
        accum1 = accum2 = 0
    if n > 0:
        hashval = (accum1 + accum2 * 0x61) % HASHBASE
        counts[hashval] = counts.get(hashval, 0) + n
    return sorted(counts.items())


def _count_changes(src: Filespec, dst: Filespec) -> tuple[int, int]:
    """Port of diffcore_count_changes: returns (src_copied, literal_added)."""
    s = src.spanhash
    d = dst.spanhash
    sc = 0
    la = 0
    i_s = 0
    i_d = 0
    ns = len(s)
    nd = len(d)
    while i_s < ns:
        s_hashval, s_cnt = s[i_s]
        while i_d < nd and d[i_d][0] < s_hashval:
            la += d[i_d][1]
            i_d += 1
        src_cnt = s_cnt
        dst_cnt = 0
        if i_d < nd and d[i_d][0] == s_hashval:
            dst_cnt = d[i_d][1]
            i_d += 1
        if src_cnt < dst_cnt:
            la += dst_cnt - src_cnt
            sc += src_cnt
        else:
            sc += dst_cnt
        i_s += 1
    while i_d < nd:
        la += d[i_d][1]
        i_d += 1
    return sc, la


def _estimate_similarity(src: Filespec, dst: Filespec, minimum_score: int) -> int:
    if not s_isreg(src.mode) or not s_isreg(dst.mode):
        return 0
    src_size = src.size
    dst_size = dst.size
    max_size = max(src_size, dst_size)
    base_size = min(src_size, dst_size)
    delta_size = max_size - base_size
    if max_size * (MAX_SCORE - minimum_score) < delta_size * MAX_SCORE:
        return 0
    src_copied, literal_added = _count_changes(src, dst)
    if not dst_size:
        return 0
    return int(src_copied * MAX_SCORE / max_size)


def _basename_same(a: Filespec, b: Filespec) -> int:
    return 1 if _basename(a.path) == _basename(b.path) else 0


class _Score:
    __slots__ = ("src", "dst", "score", "name_score")

    def __init__(self, src=-1, dst=-1, score=0, name_score=0):
        self.src = src
        self.dst = dst
        self.score = score
        self.name_score = name_score


def _score_compare(a: _Score, b: _Score) -> int:
    if a.dst < 0:
        return 1 if b.dst >= 0 else 0
    elif b.dst < 0:
        return -1
    if a.score == b.score:
        return b.name_score - a.name_score
    return 1 if b.score > a.score else (-1 if b.score < a.score else 0)


class RenamePair:
    """A detected rename: src_path -> dst_path with similarity score."""
    __slots__ = ("src", "dst", "score", "is_copy")

    def __init__(self, src: Filespec, dst: Filespec, score: int):
        self.src = src
        self.dst = dst
        self.score = score
        self.is_copy = False


def _is_empty_blob(repo: Repository, oid: str) -> bool:
    empty, _ = objs.hash_bytes("blob", b"", repo)
    return oid == empty


def detect_renames(
    repo: Repository,
    base_map: dict[str, tuple[int, str]],
    side_map: dict[str, tuple[int, str]],
    *,
    rename_limit: int = 7000,
    minimum_score: int = 0,
    rename_empty: bool = False,
    relevant_sources: Optional[set] = None,
) -> list[RenamePair]:
    """Detect file renames between two trees represented as path->(mode, oid).

    Sources are paths present in base but absent in side (deletions);
    destinations are paths present in side but absent in base (additions).
    Returns the list of detected rename pairs.

    ``relevant_sources`` (if given) limits inexact/basename rename detection to
    those source paths (exact renames still consider all sources), mirroring
    merge-ort's relevant_sources culling in diffcore_rename_extended.
    """
    if minimum_score == 0:
        minimum_score = DEFAULT_RENAME_SCORE
    if minimum_score > MAX_SCORE:
        minimum_score = int(MAX_SCORE)

    srcs: list[Filespec] = []
    dsts: list[Filespec] = []
    for path in sorted(base_map):
        if path in side_map:
            continue
        mode, oid = base_map[path]
        if not rename_empty and _is_empty_blob(repo, oid):
            continue
        srcs.append(Filespec(repo, path, oid, mode))
    for path in sorted(side_map):
        if path in base_map:
            continue
        mode, oid = side_map[path]
        if not rename_empty and _is_empty_blob(repo, oid):
            continue
        dsts.append(Filespec(repo, path, oid, mode))

    if not srcs or not dsts:
        return []

    dst_is_rename = [False] * len(dsts)
    dst_match: list[Optional[Filespec]] = [None] * len(dsts)
    dst_score: list[int] = [0] * len(dsts)

    # --- exact renames (find_exact_renames / find_identical_files) ---
    srcs_by_oid: dict[str, list[int]] = {}
    for i, s in enumerate(srcs):
        srcs_by_oid.setdefault(s.oid, []).append(i)

    def record(dst_i: int, src_i: int, score: int) -> None:
        srcs[src_i].rename_used += 1
        dst_is_rename[dst_i] = True
        dst_match[dst_i] = srcs[src_i]
        dst_score[dst_i] = score

    for di, dst in enumerate(dsts):
        cands = srcs_by_oid.get(dst.oid)
        if not cands:
            continue
        best = -1
        best_score = -1
        for si in cands:
            src = srcs[si]
            if not s_isreg(src.mode) or not s_isreg(dst.mode):
                if src.mode != dst.mode:
                    continue
            if src.rename_used:
                continue
            score = 1 + _basename_same(src, dst)
            if score > best_score:
                best = si
                best_score = score
                if score == 2:
                    break
        if best >= 0:
            record(di, best, int(MAX_SCORE))

    # Did we only want exact renames?
    if minimum_score == int(MAX_SCORE):
        return _collect(dsts, dst_is_rename, dst_match, dst_score)

    def remaining_srcs() -> list[int]:
        return [i for i, s in enumerate(srcs) if not s.rename_used]

    # --- basename matches (find_basename_matches) ---
    min_basename_score = minimum_score + int(0.5 * (MAX_SCORE - minimum_score))
    src_base: dict[str, int] = {}
    for i in remaining_srcs():
        base = _basename(srcs[i].path)
        src_base[base] = -1 if base in src_base else i
    dst_base: dict[str, int] = {}
    for i in range(len(dsts)):
        if dst_is_rename[i]:
            continue
        base = _basename(dsts[i].path)
        dst_base[base] = -1 if base in dst_base else i

    for i in range(len(srcs)):
        if srcs[i].rename_used:
            continue
        if relevant_sources is not None and srcs[i].path not in relevant_sources:
            continue
        base = _basename(srcs[i].path)
        src_index = src_base.get(base, -1)
        if base in dst_base:
            dst_index = dst_base.get(base, -1)
            if src_index == -1 or dst_index == -1:
                # would need directory-rename heuristics; skip
                continue
            if dst_is_rename[dst_index]:
                continue
            one = srcs[src_index]
            two = dsts[dst_index]
            score = _estimate_similarity(one, two, minimum_score)
            if score < min_basename_score:
                continue
            record(dst_index, src_index, score)

    # --- inexact matrix (NxM similarity) ---
    # cull sources not in relevant_sources (remove_unneeded_paths_from_src)
    src_idx = [i for i in remaining_srcs()
               if relevant_sources is None or srcs[i].path in relevant_sources]
    num_sources = len(src_idx)
    num_destinations = sum(1 for i in range(len(dsts)) if not dst_is_rename[i])
    if not num_sources or not num_destinations:
        return _collect(dsts, dst_is_rename, dst_match, dst_score)

    # too_many_rename_candidates
    if rename_limit > 0 and num_destinations * num_sources > rename_limit * rename_limit:
        return _collect(dsts, dst_is_rename, dst_match, dst_score)

    mx: list[_Score] = []
    for i in range(len(dsts)):
        if dst_is_rename[i]:
            continue
        two = dsts[i]
        m = [_Score() for _ in range(NUM_CANDIDATE_PER_DST)]
        for sj in src_idx:
            one = srcs[sj]
            this = _Score(src=sj, dst=i,
                          score=_estimate_similarity(one, two, minimum_score),
                          name_score=_basename_same(one, two))
            _record_if_better(m, this)
        mx.extend(m)

    mx.sort(key=functools.cmp_to_key(_score_compare))

    for sc in mx:
        if sc.dst < 0 or sc.score < minimum_score:
            break
        if dst_is_rename[sc.dst]:
            continue
        if srcs[sc.src].rename_used:
            continue
        record(sc.dst, sc.src, sc.score)

    return _collect(dsts, dst_is_rename, dst_match, dst_score)


def _record_if_better(m: list[_Score], o: _Score) -> None:
    worst = 0
    for i in range(1, NUM_CANDIDATE_PER_DST):
        if _score_compare(m[i], m[worst]) > 0:
            worst = i
    if _score_compare(m[worst], o) > 0:
        m[worst] = o


def _collect(dsts, dst_is_rename, dst_match, dst_score) -> list[RenamePair]:
    out: list[RenamePair] = []
    for i, dst in enumerate(dsts):
        if dst_is_rename[i] and dst_match[i] is not None:
            out.append(RenamePair(dst_match[i], dst, dst_score[i]))
    return out


def should_break(repo: Repository, src: Filespec, dst: Filespec,
                 break_score: int) -> tuple[bool, int]:
    """Port of diffcore-break.c should_break: decide whether the in-place edit
    of ``src`` -> ``dst`` is a complete rewrite that should be broken into a
    delete+create pair. Returns (do_break, merge_score) where merge_score is
    src_removed*MAX_SCORE/src_size (the amount removed from the source)."""
    merge_score = 0
    if s_isreg(src.mode) != s_isreg(dst.mode):
        return True, int(MAX_SCORE)  # even their types are different
    if src.oid == dst.oid:
        return False, 0  # they are the same
    max_size = max(src.size, dst.size)
    if max_size < MINIMUM_BREAK_SIZE:
        return False, 0  # we do not break too small filepair
    if not src.size:
        return False, 0  # we do not let empty files get renamed
    src_copied, literal_added = _count_changes(src, dst)
    # sanity
    if src.size < src_copied:
        src_copied = src.size
    if dst.size < literal_added + src_copied:
        if src_copied < dst.size:
            literal_added = dst.size - src_copied
        else:
            literal_added = 0
    src_removed = src.size - src_copied
    # Compute merge-score: "how much is removed from the source material".
    merge_score = int(src_removed * MAX_SCORE / src.size)
    if merge_score > break_score:
        return True, merge_score
    # Extent of damage, which counts both inserts and deletes.
    delta_size = src_removed + literal_added
    if delta_size * MAX_SCORE / max_size < break_score:
        return False, merge_score
    # If you removed a lot without adding new material, that is not a rewrite.
    if (src.size * break_score < src_removed * MAX_SCORE and
            literal_added * 20 < src_removed and
            literal_added * 20 < src_copied):
        return False, merge_score
    return True, merge_score


def parse_break_opt(arg: Optional[str]) -> tuple[Optional[int], Optional[str]]:
    """Parse a -B/--break-rewrites argument '<n>[/<m>]' into a combined
    break_opt int (merge_score<<16 | break_score). n is the break threshold,
    m the merge threshold; both as fractions scaled to MAX_SCORE. Returns
    (break_opt, error_message). break_opt of 0 means "use defaults"."""
    if not arg:
        return 0, None
    # may be 'n', 'n/m', '/m', or with trailing '%'
    if "/" in arg:
        n_str, _, m_str = arg.partition("/")
    else:
        n_str, m_str = arg, ""
    def _frac(s: str) -> Optional[int]:
        s = s.strip()
        if not s:
            return 0
        pct = s.endswith("%")
        if pct:
            s = s[:-1]
        try:
            if "." in s and not pct:
                num = float(s)
                return int(num * MAX_SCORE)
            val = float(s)
        except ValueError:
            return None
        if pct:
            return int(val * MAX_SCORE / 100)
        # Bare number: a value >= 1 with no '%' that's > some scale is treated
        # as a percentage 0..100 in git's parse_rename_score for M/C; for break
        # the convention matches: 0..100 scaled, fractions 0..1 scaled directly.
        if val <= 1.0:
            return int(val * MAX_SCORE)
        return int(val * MAX_SCORE / 100)
    break_score = _frac(n_str)
    merge_score = _frac(m_str) if m_str or "/" in arg else 0
    if break_score is None or merge_score is None:
        return None, "break-rewrites expects <n>/<m> form"
    return ((merge_score or 0) << 16) | (break_score or 0), None


def parse_rename_score(arg: Optional[str]) -> tuple[Optional[int], Optional[str]]:
    """Parse a -M/-C argument '<n>' (a similarity threshold) into a 0..MAX_SCORE
    minimum_score. Returns (score, error_message); score 0 means use default."""
    if not arg:
        return 0, None
    s = arg.strip()
    pct = s.endswith("%")
    if pct:
        s = s[:-1]
    try:
        val = float(s)
    except ValueError:
        return None, None
    if pct:
        score = int(val * MAX_SCORE / 100)
    elif val <= 1.0 and "." in arg:
        score = int(val * MAX_SCORE)
    else:
        score = int(val * MAX_SCORE / 100)
    if score < 0:
        return None, None
    if score > MAX_SCORE:
        score = int(MAX_SCORE)
    return score, None


def detect_renames_copies(
    repo: Repository,
    src_map: dict[str, tuple[int, str]],
    dst_map: dict[str, tuple[int, str]],
    *,
    copy_sources: Optional[dict[str, tuple[int, str]]] = None,
    data_map: Optional[dict[str, bytes]] = None,
    want_copies: bool = False,
    rename_limit: int = 1000,
    minimum_score: int = 0,
) -> tuple[list[RenamePair], int]:
    """Detect rename and copy pairs for ``git diff -C``.

    ``src_map`` are deletion sources (consumable), ``dst_map`` are addition
    destinations. ``copy_sources`` are extra sources usable as copy origins
    that are NOT consumed (modified files for -C, all files for
    --find-copies-harder). ``data_map`` supplies blob content for paths whose
    oid is not in the object store (working-tree destinations). Returns
    (pairs, needed_rename_limit); needed_rename_limit > 0 means the inexact
    matrix was skipped due to the rename limit.
    """
    if minimum_score == 0:
        minimum_score = DEFAULT_RENAME_SCORE
    if minimum_score > MAX_SCORE:
        minimum_score = int(MAX_SCORE)
    copy_sources = copy_sources or {}
    data_map = data_map or {}

    def _spec(path: str, mode: int, oid: str) -> Filespec:
        return Filespec(repo, path, oid, mode, data=data_map.get(path))

    # Build source list: consumable deletion sources first, then copy sources.
    srcs: list[Filespec] = []
    src_consumable: list[bool] = []
    for path in sorted(src_map):
        if path in dst_map:
            continue
        mode, oid = src_map[path]
        if _is_empty_blob(repo, oid):
            continue
        srcs.append(_spec(path, mode, oid))
        src_consumable.append(True)
    for path in sorted(copy_sources):
        if path in src_map and path not in dst_map:
            continue  # already a deletion source
        mode, oid = copy_sources[path]
        srcs.append(_spec(path, mode, oid))
        src_consumable.append(False)

    dsts: list[Filespec] = []
    for path in sorted(dst_map):
        if path in src_map:
            continue
        mode, oid = dst_map[path]
        if _is_empty_blob(repo, oid):
            continue
        dsts.append(_spec(path, mode, oid))

    if not srcs or not dsts:
        return [], 0

    dst_is_rename = [False] * len(dsts)
    dst_src_idx: list[int] = [-1] * len(dsts)
    dst_score: list[int] = [0] * len(dsts)
    # rename_used accounting mirrors diffcore-rename.c: each copy source (one
    # that stays in the tree) is pre-incremented at registration so the final
    # resolve step (--rename_used > 0 => COPY) classifies it as a copy.
    rename_used = [0 if src_consumable[i] else 1 for i in range(len(srcs))]

    srcs_by_oid: dict[str, list[int]] = {}
    for i, s in enumerate(srcs):
        srcs_by_oid.setdefault(s.oid, []).append(i)

    def record(dst_i: int, src_i: int, score: int) -> None:
        rename_used[src_i] += 1
        dst_is_rename[dst_i] = True
        dst_src_idx[dst_i] = src_i
        dst_score[dst_i] = score

    # exact matches
    for di, dst in enumerate(dsts):
        cands = srcs_by_oid.get(dst.oid)
        if not cands:
            continue
        best = -1
        best_score = -1
        for si in cands:
            src = srcs[si]
            if not s_isreg(src.mode) or not s_isreg(dst.mode):
                if src.mode != dst.mode:
                    continue
            # higher score for unused sources; a used source is skipped unless
            # we are detecting copies (where sources may be reused).
            score = 0 if rename_used[si] else 1
            if rename_used[si] and not want_copies:
                continue
            score += _basename_same(src, dst)
            if score > best_score:
                best = si
                best_score = score
                if score == 2:
                    break
        if best >= 0:
            record(di, best, int(MAX_SCORE))

    needed = 0
    if minimum_score != int(MAX_SCORE):
        # inexact matrix (copies allow reusing sources)
        num_destinations = sum(1 for i in range(len(dsts)) if not dst_is_rename[i])
        num_sources = len(srcs)
        if num_destinations and num_sources:
            if rename_limit > 0 and num_destinations * num_sources > rename_limit * rename_limit:
                needed = max(num_sources, num_destinations)
            else:
                mx: list[_Score] = []
                for i in range(len(dsts)):
                    if dst_is_rename[i]:
                        continue
                    two = dsts[i]
                    m = [_Score() for _ in range(NUM_CANDIDATE_PER_DST)]
                    for sj in range(len(srcs)):
                        one = srcs[sj]
                        this = _Score(src=sj, dst=i,
                                      score=_estimate_similarity(one, two, minimum_score),
                                      name_score=_basename_same(one, two))
                        _record_if_better(m, this)
                    mx.extend(m)
                mx.sort(key=functools.cmp_to_key(_score_compare))
                for sc in mx:
                    if sc.dst < 0 or sc.score < minimum_score:
                        break
                    if dst_is_rename[sc.dst]:
                        continue
                    if rename_used[sc.src] and not want_copies:
                        continue
                    record(sc.dst, sc.src, sc.score)

    # Resolve rename vs copy: for each matched dst, decrement the source's
    # rename_used; if still > 0 the source stays in the tree (a copy).
    out: list[RenamePair] = []
    for i, dst in enumerate(dsts):
        if not dst_is_rename[i] or dst_src_idx[i] < 0:
            continue
        si = dst_src_idx[i]
        rename_used[si] -= 1
        rp = RenamePair(srcs[si], dst, dst_score[i])
        rp.is_copy = rename_used[si] > 0
        out.append(rp)
    return out, needed
