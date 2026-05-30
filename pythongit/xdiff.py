"""Faithful pure-Python port of Git's xdiff library (the parts the ort merge
engine relies on): record preparation/classification, the histogram diff
algorithm, the classic Myers diff used as a histogram fallback, change
compaction, edit-script construction, and the three-way ``xdl_merge`` engine
that produces conflicted blobs with ``<<<<<<<`` / ``=======`` / ``>>>>>>>``
markers byte-for-byte identically to C Git.

Sources ported (git v2.44.0):
  xdiff/xutils.c, xdiff/xprepare.c, xdiff/xdiffi.c,
  xdiff/xhistogram.c, xdiff/xmerge.c

Only the code paths exercised by ``merge-ort`` content merges are ported, which
means histogram diff with the classic Myers algorithm as the documented
fallback.  Whitespace-ignoring flags are not modelled (merge-ort never sets
them); records compare by exact byte content, matching ``xdl_recmatch`` with
``flags == 0``.
"""
from __future__ import annotations

from typing import Optional

# ---- xdiff.h flag/constant subset -----------------------------------------

XDF_NEED_MINIMAL = 1 << 0
XDF_PATIENCE_DIFF = 1 << 14
XDF_HISTOGRAM_DIFF = 1 << 15
XDF_DIFF_ALGORITHM_MASK = XDF_PATIENCE_DIFF | XDF_HISTOGRAM_DIFF
XDF_INDENT_HEURISTIC = 1 << 23

XDL_MERGE_MINIMAL = 0
XDL_MERGE_EAGER = 1
XDL_MERGE_ZEALOUS = 2
XDL_MERGE_ZEALOUS_ALNUM = 3

XDL_MERGE_FAVOR_OURS = 1
XDL_MERGE_FAVOR_THEIRS = 2
XDL_MERGE_FAVOR_UNION = 3

XDL_MERGE_DIFF3 = 1
XDL_MERGE_ZEALOUS_DIFF3 = 2

DEFAULT_CONFLICT_MARKER_SIZE = 7

# xdiffi.c tuning constants
XDL_MAX_COST_MIN = 256
XDL_HEUR_MIN_COST = 256
XDL_SNAKE_CNT = 20
XDL_K_HEUR = 4
XDL_LINE_MAX = (1 << 62) - 1  # "infinity" sentinel; far above any line count

# xprepare.c constants
XDL_KPDIS_RUN = 4
XDL_MAX_EQLIMIT = 1024
XDL_SIMSCAN_WINDOW = 100


def DIFF_WITH_ALG(flags: int, alg: int) -> int:
    return (flags & ~XDF_DIFF_ALGORITHM_MASK) | alg


# ---------------------------------------------------------------------------
# record splitting / hashing helpers (xutils.c)


def split_records(data: bytes) -> list[bytes]:
    """Split a buffer into xdiff records: each line keeps its trailing '\\n';
    a final line without a newline becomes its own record."""
    recs: list[bytes] = []
    i = 0
    n = len(data)
    while i < n:
        j = data.find(b"\n", i)
        if j == -1:
            recs.append(data[i:])
            i = n
        else:
            recs.append(data[i : j + 1])
            i = j + 1
    return recs


def xdl_bogosqrt(n: int) -> int:
    i = 1
    while n > 0:
        i <<= 1
        n >>= 2
    return i


def xdl_recmatch(l1: bytes, l2: bytes, flags: int = 0) -> bool:
    """Port of xdl_recmatch for flags == 0 (no whitespace handling): exact
    byte equality including the trailing newline."""
    return l1 == l2


# ---------------------------------------------------------------------------
# rchg array with the C "rchg = alloc + 1" sentinel convention.


class Rchg:
    """Change flags indexed logically from -1..nrec, with 0 sentinels just
    outside the [0, nrec-1] range (mirrors xdiff's ``rchg`` pointer offset)."""

    __slots__ = ("buf", "nrec")

    def __init__(self, nrec: int):
        self.nrec = nrec
        self.buf = bytearray(nrec + 2)  # logical i stored at i+1

    def __getitem__(self, i: int) -> int:
        j = i + 1
        if 0 <= j < len(self.buf):
            return self.buf[j]
        return 0

    def __setitem__(self, i: int, v: int) -> None:
        j = i + 1
        if 0 <= j < len(self.buf):
            self.buf[j] = v & 0xFF


# ---------------------------------------------------------------------------
# prepared file (xdfile_t) and environment (xdfenv_t)


class Xdfile:
    __slots__ = ("recs", "nrec", "ha", "rchg", "rindex", "ha_eff", "nreff",
                 "dstart", "dend")

    def __init__(self, recs: list[bytes], ha: list[int]):
        self.recs = recs
        self.nrec = len(recs)
        self.ha = ha                 # class id per record (len nrec)
        self.rchg = Rchg(self.nrec)
        self.rindex: list[int] = []  # effective-record index -> real index
        self.ha_eff: list[int] = []  # class ids of effective records
        self.nreff = 0
        self.dstart = 0
        self.dend = self.nrec - 1


class Xdfenv:
    __slots__ = ("xdf1", "xdf2")

    def __init__(self, xdf1: Xdfile, xdf2: Xdfile):
        self.xdf1 = xdf1
        self.xdf2 = xdf2


def _classify(recs1: list[bytes], recs2: list[bytes], flags: int):
    """Assign each record an equivalence-class id (first-appearance order over
    file1 then file2) and count per-side occurrences.  Returns (ha1, ha2,
    len1_by_class, len2_by_class)."""
    class_id: dict[bytes, int] = {}
    len1: list[int] = []
    len2: list[int] = []
    ha1: list[int] = []
    ha2: list[int] = []
    for r in recs1:
        cid = class_id.get(r)
        if cid is None:
            cid = len(len1)
            class_id[r] = cid
            len1.append(0)
            len2.append(0)
        len1[cid] += 1
        ha1.append(cid)
    for r in recs2:
        cid = class_id.get(r)
        if cid is None:
            cid = len(len1)
            class_id[r] = cid
            len1.append(0)
            len2.append(0)
        len2[cid] += 1
        ha2.append(cid)
    return ha1, ha2, len1, len2


def xdl_prepare_env(a: bytes, b: bytes, flags: int) -> tuple[Xdfenv, list[int], list[int]]:
    recs1 = split_records(a)
    recs2 = split_records(b)
    ha1, ha2, len1, len2 = _classify(recs1, recs2, flags)
    xe = Xdfenv(Xdfile(recs1, ha1), Xdfile(recs2, ha2))
    if (flags & XDF_DIFF_ALGORITHM_MASK) not in (XDF_PATIENCE_DIFF, XDF_HISTOGRAM_DIFF):
        _xdl_optimize_ctxs(xe.xdf1, xe.xdf2, len1, len2)
    return xe, len1, len2


# ---------------------------------------------------------------------------
# xdl_trim_ends + xdl_cleanup_records (xprepare.c) — Myers path only


def _xdl_trim_ends(xdf1: Xdfile, xdf2: Xdfile) -> None:
    recs1, recs2 = xdf1.ha, xdf2.ha
    lim = min(xdf1.nrec, xdf2.nrec)
    i = 0
    while i < lim and recs1[i] == recs2[i]:
        i += 1
    xdf1.dstart = xdf2.dstart = i

    lim -= i
    i = 0
    while i < lim and recs1[xdf1.nrec - 1 - i] == recs2[xdf2.nrec - 1 - i]:
        i += 1
    xdf1.dend = xdf1.nrec - i - 1
    xdf2.dend = xdf2.nrec - i - 1


def _xdl_clean_mmatch(dis: bytearray, i: int, s: int, e: int) -> int:
    if i - s > XDL_SIMSCAN_WINDOW:
        s = i - XDL_SIMSCAN_WINDOW
    if e - i > XDL_SIMSCAN_WINDOW:
        e = i + XDL_SIMSCAN_WINDOW

    rdis0 = 0
    rpdis0 = 1
    r = 1
    while (i - r) >= s:
        if not dis[i - r]:
            rdis0 += 1
        elif dis[i - r] == 2:
            rpdis0 += 1
        else:
            break
        r += 1
    if rdis0 == 0:
        return 0
    rdis1 = 0
    rpdis1 = 1
    r = 1
    while (i + r) <= e:
        if not dis[i + r]:
            rdis1 += 1
        elif dis[i + r] == 2:
            rpdis1 += 1
        else:
            break
        r += 1
    if rdis1 == 0:
        return 0
    rdis1 += rdis0
    rpdis1 += rpdis0
    return 1 if rpdis1 * XDL_KPDIS_RUN < (rpdis1 + rdis1) else 0


def _xdl_cleanup_records(xdf1: Xdfile, xdf2: Xdfile, len1: list[int], len2: list[int]) -> None:
    dis1 = bytearray(xdf1.nrec + 1)
    dis2 = bytearray(xdf2.nrec + 1)

    mlim = xdl_bogosqrt(xdf1.nrec)
    if mlim > XDL_MAX_EQLIMIT:
        mlim = XDL_MAX_EQLIMIT
    for i in range(xdf1.dstart, xdf1.dend + 1):
        nm = len2[xdf1.ha[i]]
        dis1[i] = 0 if nm == 0 else (2 if nm >= mlim else 1)

    mlim = xdl_bogosqrt(xdf2.nrec)
    if mlim > XDL_MAX_EQLIMIT:
        mlim = XDL_MAX_EQLIMIT
    for i in range(xdf2.dstart, xdf2.dend + 1):
        nm = len1[xdf2.ha[i]]
        dis2[i] = 0 if nm == 0 else (2 if nm >= mlim else 1)

    rindex: list[int] = []
    ha_eff: list[int] = []
    for i in range(xdf1.dstart, xdf1.dend + 1):
        if dis1[i] == 1 or (dis1[i] == 2 and not _xdl_clean_mmatch(dis1, i, xdf1.dstart, xdf1.dend)):
            rindex.append(i)
            ha_eff.append(xdf1.ha[i])
        else:
            xdf1.rchg[i] = 1
    xdf1.rindex = rindex
    xdf1.ha_eff = ha_eff
    xdf1.nreff = len(rindex)

    rindex = []
    ha_eff = []
    for i in range(xdf2.dstart, xdf2.dend + 1):
        if dis2[i] == 1 or (dis2[i] == 2 and not _xdl_clean_mmatch(dis2, i, xdf2.dstart, xdf2.dend)):
            rindex.append(i)
            ha_eff.append(xdf2.ha[i])
        else:
            xdf2.rchg[i] = 1
    xdf2.rindex = rindex
    xdf2.ha_eff = ha_eff
    xdf2.nreff = len(rindex)


def _xdl_optimize_ctxs(xdf1: Xdfile, xdf2: Xdfile, len1: list[int], len2: list[int]) -> None:
    _xdl_trim_ends(xdf1, xdf2)
    _xdl_cleanup_records(xdf1, xdf2, len1, len2)


# ---------------------------------------------------------------------------
# classic Myers diff (xdiffi.c): xdl_split + xdl_recs_cmp


class _AlgoEnv:
    __slots__ = ("mxcost", "snake_cnt", "heur_min")


class _Split:
    __slots__ = ("i1", "i2", "min_lo", "min_hi")

    def __init__(self):
        self.i1 = 0
        self.i2 = 0
        self.min_lo = 0
        self.min_hi = 0


def _xdl_split(ha1, off1, lim1, ha2, off2, lim2, need_min, spl, xenv) -> int:
    dmin = off1 - lim2
    dmax = lim1 - off2
    fmid = off1 - off2
    bmid = lim1 - lim2
    odd = (fmid - bmid) & 1
    fmin = fmax = fmid
    bmin = bmax = bmid

    kvdf: dict[int, int] = {fmid: off1}
    kvdb: dict[int, int] = {bmid: lim1}

    ec = 0
    while True:
        ec += 1
        got_snake = 0

        if fmin > dmin:
            fmin -= 1
            kvdf[fmin - 1] = -1
        else:
            fmin += 1
        if fmax < dmax:
            fmax += 1
            kvdf[fmax + 1] = -1
        else:
            fmax -= 1

        d = fmax
        while d >= fmin:
            if kvdf.get(d - 1, -1) >= kvdf.get(d + 1, -1):
                i1 = kvdf[d - 1] + 1
            else:
                i1 = kvdf[d + 1]
            prev1 = i1
            i2 = i1 - d
            while i1 < lim1 and i2 < lim2 and ha1[i1] == ha2[i2]:
                i1 += 1
                i2 += 1
            if i1 - prev1 > xenv.snake_cnt:
                got_snake = 1
            kvdf[d] = i1
            if odd and bmin <= d <= bmax and kvdb.get(d, XDL_LINE_MAX) <= i1:
                spl.i1 = i1
                spl.i2 = i2
                spl.min_lo = spl.min_hi = 1
                return ec
            d -= 2

        if bmin > dmin:
            bmin -= 1
            kvdb[bmin - 1] = XDL_LINE_MAX
        else:
            bmin += 1
        if bmax < dmax:
            bmax += 1
            kvdb[bmax + 1] = XDL_LINE_MAX
        else:
            bmax -= 1

        d = bmax
        while d >= bmin:
            if kvdb.get(d - 1, XDL_LINE_MAX) < kvdb.get(d + 1, XDL_LINE_MAX):
                i1 = kvdb[d - 1]
            else:
                i1 = kvdb[d + 1] - 1
            prev1 = i1
            i2 = i1 - d
            while i1 > off1 and i2 > off2 and ha1[i1 - 1] == ha2[i2 - 1]:
                i1 -= 1
                i2 -= 1
            if prev1 - i1 > xenv.snake_cnt:
                got_snake = 1
            kvdb[d] = i1
            if not odd and fmin <= d <= fmax and i1 <= kvdf.get(d, -1):
                spl.i1 = i1
                spl.i2 = i2
                spl.min_lo = spl.min_hi = 1
                return ec
            d -= 2

        if need_min:
            continue

        if got_snake and ec > xenv.heur_min:
            best = 0
            d = fmax
            while d >= fmin:
                dd = d - fmid if d > fmid else fmid - d
                i1 = kvdf[d]
                i2 = i1 - d
                v = (i1 - off1) + (i2 - off2) - dd
                if (v > XDL_K_HEUR * ec and v > best and
                        off1 + xenv.snake_cnt <= i1 < lim1 and
                        off2 + xenv.snake_cnt <= i2 < lim2):
                    k = 1
                    while ha1[i1 - k] == ha2[i2 - k]:
                        if k == xenv.snake_cnt:
                            best = v
                            spl.i1 = i1
                            spl.i2 = i2
                            break
                        k += 1
                d -= 2
            if best > 0:
                spl.min_lo = 1
                spl.min_hi = 0
                return ec

            best = 0
            d = bmax
            while d >= bmin:
                dd = d - bmid if d > bmid else bmid - d
                i1 = kvdb[d]
                i2 = i1 - d
                v = (lim1 - i1) + (lim2 - i2) - dd
                if (v > XDL_K_HEUR * ec and v > best and
                        off1 < i1 <= lim1 - xenv.snake_cnt and
                        off2 < i2 <= lim2 - xenv.snake_cnt):
                    k = 0
                    while ha1[i1 + k] == ha2[i2 + k]:
                        if k == xenv.snake_cnt - 1:
                            best = v
                            spl.i1 = i1
                            spl.i2 = i2
                            break
                        k += 1
                d -= 2
            if best > 0:
                spl.min_lo = 0
                spl.min_hi = 1
                return ec

        if ec >= xenv.mxcost:
            fbest = fbest1 = -1
            d = fmax
            while d >= fmin:
                i1 = min(kvdf[d], lim1)
                i2 = i1 - d
                if lim2 < i2:
                    i1 = lim2 + d
                    i2 = lim2
                if fbest < i1 + i2:
                    fbest = i1 + i2
                    fbest1 = i1
                d -= 2

            bbest = bbest1 = XDL_LINE_MAX
            d = bmax
            while d >= bmin:
                i1 = max(off1, kvdb[d])
                i2 = i1 - d
                if i2 < off2:
                    i1 = off2 + d
                    i2 = off2
                if i1 + i2 < bbest:
                    bbest = i1 + i2
                    bbest1 = i1
                d -= 2

            if (lim1 + lim2) - bbest < fbest - (off1 + off2):
                spl.i1 = fbest1
                spl.i2 = fbest - fbest1
                spl.min_lo = 1
                spl.min_hi = 0
            else:
                spl.i1 = bbest1
                spl.i2 = bbest - bbest1
                spl.min_lo = 0
                spl.min_hi = 1
            return ec


def _xdl_recs_cmp(ha1, rindex1, rchg1, off1, lim1,
                  ha2, rindex2, rchg2, off2, lim2, need_min, xenv) -> None:
    while off1 < lim1 and off2 < lim2 and ha1[off1] == ha2[off2]:
        off1 += 1
        off2 += 1
    while off1 < lim1 and off2 < lim2 and ha1[lim1 - 1] == ha2[lim2 - 1]:
        lim1 -= 1
        lim2 -= 1

    if off1 == lim1:
        while off2 < lim2:
            rchg2[rindex2[off2]] = 1
            off2 += 1
    elif off2 == lim2:
        while off1 < lim1:
            rchg1[rindex1[off1]] = 1
            off1 += 1
    else:
        spl = _Split()
        _xdl_split(ha1, off1, lim1, ha2, off2, lim2, need_min, spl, xenv)
        _xdl_recs_cmp(ha1, rindex1, rchg1, off1, spl.i1,
                      ha2, rindex2, rchg2, off2, spl.i2, spl.min_lo, xenv)
        _xdl_recs_cmp(ha1, rindex1, rchg1, spl.i1, lim1,
                      ha2, rindex2, rchg2, spl.i2, lim2, spl.min_hi, xenv)


def _myers_diff(xe: Xdfenv, flags: int) -> None:
    ndiags = xe.xdf1.nreff + xe.xdf2.nreff + 3
    xenv = _AlgoEnv()
    xenv.mxcost = xdl_bogosqrt(ndiags)
    if xenv.mxcost < XDL_MAX_COST_MIN:
        xenv.mxcost = XDL_MAX_COST_MIN
    xenv.snake_cnt = XDL_SNAKE_CNT
    xenv.heur_min = XDL_HEUR_MIN_COST
    _xdl_recs_cmp(xe.xdf1.ha_eff, xe.xdf1.rindex, xe.xdf1.rchg, 0, xe.xdf1.nreff,
                  xe.xdf2.ha_eff, xe.xdf2.rindex, xe.xdf2.rchg, 0, xe.xdf2.nreff,
                  (flags & XDF_NEED_MINIMAL) != 0, xenv)


# ---------------------------------------------------------------------------
# histogram diff (xhistogram.c)

_MAX_CHAIN_LENGTH = 64


class _HistIndex:
    __slots__ = ("recs_by_table", "line_map", "next_ptrs", "ptr_shift",
                 "cnt", "has_common", "max_chain_length")


def _hist_record(env_ha1, env_ha2, side, line):
    # REC(env, s, l) = recs[l-1]; returns the class id (ha)
    return env_ha1[line - 1] if side == 1 else env_ha2[line - 1]


def _scanA(index: _HistIndex, ha1, line1: int, count1: int) -> int:
    line_end1 = line1 + count1 - 1
    ptr = line_end1
    while line1 <= ptr:
        key = ha1[ptr - 1]
        chain = index.recs_by_table.setdefault(key, [])
        # chain is a list of "record" dicts: {'ptr':..., 'cnt':...}
        found = None
        chain_len = 0
        for rec in chain:
            if ha1[rec["ptr"] - 1] == ha1[ptr - 1]:
                found = rec
                break
            chain_len += 1
        if found is not None:
            index.next_ptrs[ptr - index.ptr_shift] = found["ptr"]
            found["ptr"] = ptr
            found["cnt"] = min(_MAX_CHAIN_LENGTH + 0xFFFFFFFF, found["cnt"] + 1)
            index.line_map[ptr - index.ptr_shift] = found
        else:
            if chain_len == index.max_chain_length:
                return -1
            rec = {"ptr": ptr, "cnt": 1}
            chain.insert(0, rec)
            index.line_map[ptr - index.ptr_shift] = rec
        ptr -= 1
    return 0


def _try_lcs(index: _HistIndex, ha1, ha2, lcs, b_ptr, line1, count1, line2, count2):
    line_end1 = line1 + count1 - 1
    line_end2 = line2 + count2 - 1
    b_next = b_ptr + 1
    key = ha2[b_ptr - 1]
    chain = index.recs_by_table.get(key, [])

    def CMP(s1, l1, s2, l2):
        v1 = ha1[l1 - 1] if s1 == 1 else ha2[l1 - 1]
        v2 = ha1[l2 - 1] if s2 == 1 else ha2[l2 - 1]
        return v1 == v2

    def CNT(ptr):
        return index.line_map[ptr - index.ptr_shift]["cnt"]

    for rec in chain:
        if rec["cnt"] > index.cnt:
            if not index.has_common:
                index.has_common = CMP(1, rec["ptr"], 2, b_ptr)
            continue
        as_ = rec["ptr"]
        if not CMP(1, as_, 2, b_ptr):
            continue
        index.has_common = 1
        while True:
            np = index.next_ptrs[as_ - index.ptr_shift]
            bs = b_ptr
            ae = as_
            be = bs
            rc = rec["cnt"]

            while line1 < as_ and line2 < bs and CMP(1, as_ - 1, 2, bs - 1):
                as_ -= 1
                bs -= 1
                if 1 < rc:
                    rc = min(rc, CNT(as_))
            while ae < line_end1 and be < line_end2 and CMP(1, ae + 1, 2, be + 1):
                ae += 1
                be += 1
                if 1 < rc:
                    rc = min(rc, CNT(ae))

            if b_next <= be:
                b_next = be + 1
            if lcs.end1 - lcs.begin1 < ae - as_ or rc < index.cnt:
                lcs.begin1 = as_
                lcs.begin2 = bs
                lcs.end1 = ae
                lcs.end2 = be
                index.cnt = rc

            if np == 0:
                break
            should_break = False
            while np <= ae:
                np = index.next_ptrs[np - index.ptr_shift]
                if np == 0:
                    should_break = True
                    break
            if should_break:
                break
            as_ = np
    return b_next


class _Region:
    __slots__ = ("begin1", "end1", "begin2", "end2")

    def __init__(self):
        self.begin1 = self.end1 = self.begin2 = self.end2 = 0


def _find_lcs(xe: Xdfenv, lcs, line1, count1, line2, count2) -> int:
    index = _HistIndex()
    index.recs_by_table = {}
    index.line_map = [None] * count1
    index.next_ptrs = [0] * count1
    index.ptr_shift = line1
    index.max_chain_length = _MAX_CHAIN_LENGTH
    ha1 = xe.xdf1.ha
    ha2 = xe.xdf2.ha
    index.cnt = 0
    index.has_common = 0

    if _scanA(index, ha1, line1, count1):
        return -1
    index.cnt = index.max_chain_length + 1
    b_ptr = line2
    line_end2 = line2 + count2 - 1
    while b_ptr <= line_end2:
        b_ptr = _try_lcs(index, ha1, ha2, lcs, b_ptr, line1, count1, line2, count2)
    if index.has_common and index.max_chain_length < index.cnt:
        return 1
    return 0


def _histogram_diff(xe: Xdfenv, flags: int, line1, count1, line2, count2) -> None:
    # iterative tail-recursion as in C
    while True:
        if count1 <= 0 and count2 <= 0:
            return
        if not count1:
            while count2:
                xe.xdf2.rchg[line2 - 1] = 1
                line2 += 1
                count2 -= 1
            return
        if not count2:
            while count1:
                xe.xdf1.rchg[line1 - 1] = 1
                line1 += 1
                count1 -= 1
            return

        lcs = _Region()
        lcs_found = _find_lcs(xe, lcs, line1, count1, line2, count2)
        if lcs_found < 0:
            return
        if lcs_found:
            _fall_back_to_classic_diff(xe, flags, line1, count1, line2, count2)
            return
        if lcs.begin1 == 0 and lcs.begin2 == 0:
            while count1:
                xe.xdf1.rchg[line1 - 1] = 1
                line1 += 1
                count1 -= 1
            while count2:
                xe.xdf2.rchg[line2 - 1] = 1
                line2 += 1
                count2 -= 1
            return
        # left part
        _histogram_diff(xe, flags, line1, lcs.begin1 - line1, line2, lcs.begin2 - line2)
        # right part via tail-loop
        line_end1 = line1 + count1 - 1
        line_end2 = line2 + count2 - 1
        count1 = line_end1 - lcs.end1
        line1 = lcs.end1 + 1
        count2 = line_end2 - lcs.end2
        line2 = lcs.end2 + 1


def _fall_back_to_classic_diff(xe: Xdfenv, flags: int, line1, count1, line2, count2) -> None:
    # build subfiles from the records and run a fresh classic (Myers) diff,
    # then copy resulting change flags back (xdl_fall_back_diff)
    sub_flags = flags & ~XDF_DIFF_ALGORITHM_MASK
    recs1 = xe.xdf1.recs[line1 - 1 : line1 - 1 + count1]
    recs2 = xe.xdf2.recs[line2 - 1 : line2 - 1 + count2]
    sub_a = b"".join(recs1)
    sub_b = b"".join(recs2)
    sub_xe, _l1, _l2 = xdl_prepare_env(sub_a, sub_b, sub_flags)
    _myers_diff(sub_xe, sub_flags)
    for k in range(count1):
        if sub_xe.xdf1.rchg[k]:
            xe.xdf1.rchg[line1 - 1 + k] = 1
    for k in range(count2):
        if sub_xe.xdf2.rchg[k]:
            xe.xdf2.rchg[line2 - 1 + k] = 1


def xdl_do_diff(a: bytes, b: bytes, flags: int) -> Xdfenv:
    xe, len1, len2 = xdl_prepare_env(a, b, flags)
    alg = flags & XDF_DIFF_ALGORITHM_MASK
    if alg == XDF_HISTOGRAM_DIFF:
        _histogram_diff(xe, flags, xe.xdf1.dstart + 1,
                        xe.xdf1.dend - xe.xdf1.dstart + 1,
                        xe.xdf2.dstart + 1,
                        xe.xdf2.dend - xe.xdf2.dstart + 1)
    else:
        _myers_diff(xe, flags)
    return xe


# ---------------------------------------------------------------------------
# change compaction (xdiffi.c) — default heuristics (no indent heuristic for
# the merge path, but the indent branch is ported for completeness)


def _recs_match(xdf: Xdfile, a: int, b: int) -> bool:
    return xdf.ha[a] == xdf.ha[b]


class _Group:
    __slots__ = ("start", "end")

    def __init__(self):
        self.start = 0
        self.end = 0


def _group_init(xdf: Xdfile, g: _Group) -> None:
    g.start = g.end = 0
    while xdf.rchg[g.end]:
        g.end += 1


def _group_next(xdf: Xdfile, g: _Group) -> int:
    if g.end == xdf.nrec:
        return -1
    g.start = g.end + 1
    g.end = g.start
    while xdf.rchg[g.end]:
        g.end += 1
    return 0


def _group_previous(xdf: Xdfile, g: _Group) -> int:
    if g.start == 0:
        return -1
    g.end = g.start - 1
    g.start = g.end
    while xdf.rchg[g.start - 1]:
        g.start -= 1
    return 0


def _group_slide_down(xdf: Xdfile, g: _Group) -> int:
    if g.end < xdf.nrec and _recs_match(xdf, g.start, g.end):
        xdf.rchg[g.start] = 0
        g.start += 1
        xdf.rchg[g.end] = 1
        g.end += 1
        while xdf.rchg[g.end]:
            g.end += 1
        return 0
    return -1


def _group_slide_up(xdf: Xdfile, g: _Group) -> int:
    if g.start > 0 and _recs_match(xdf, g.start - 1, g.end - 1):
        g.start -= 1
        xdf.rchg[g.start] = 1
        g.end -= 1
        xdf.rchg[g.end] = 0
        while xdf.rchg[g.start - 1]:
            g.start -= 1
        return 0
    return -1


# indent-heuristic scoring (only used if XDF_INDENT_HEURISTIC set)
MAX_INDENT = 200
MAX_BLANKS = 20
START_OF_FILE_PENALTY = 1
END_OF_FILE_PENALTY = 21
TOTAL_BLANK_WEIGHT = -30
POST_BLANK_WEIGHT = 6
RELATIVE_INDENT_PENALTY = -4
RELATIVE_INDENT_WITH_BLANK_PENALTY = 10
RELATIVE_OUTDENT_PENALTY = 24
RELATIVE_OUTDENT_WITH_BLANK_PENALTY = 17
RELATIVE_DEDENT_PENALTY = 23
RELATIVE_DEDENT_WITH_BLANK_PENALTY = 17
INDENT_WEIGHT = 60
INDENT_HEURISTIC_MAX_SLIDING = 100


def _is_space(c: int) -> bool:
    # XDL_ISSPACE: ' ', '\t', '\n', '\v', '\f', '\r'
    return c in (0x20, 0x09, 0x0A, 0x0B, 0x0C, 0x0D)


def _get_indent(rec: bytes) -> int:
    ret = 0
    for c in rec:
        if not _is_space(c):
            return ret
        if c == 0x20:
            ret += 1
        elif c == 0x09:
            ret += 8 - ret % 8
        if ret >= MAX_INDENT:
            return MAX_INDENT
    return -1


class _SplitMeasure:
    __slots__ = ("end_of_file", "indent", "pre_blank", "pre_indent",
                 "post_blank", "post_indent")


def _measure_split(xdf: Xdfile, split: int, m: _SplitMeasure) -> None:
    if split >= xdf.nrec:
        m.end_of_file = 1
        m.indent = -1
    else:
        m.end_of_file = 0
        m.indent = _get_indent(xdf.recs[split])
    m.pre_blank = 0
    m.pre_indent = -1
    i = split - 1
    while i >= 0:
        m.pre_indent = _get_indent(xdf.recs[i])
        if m.pre_indent != -1:
            break
        m.pre_blank += 1
        if m.pre_blank == MAX_BLANKS:
            m.pre_indent = 0
            break
        i -= 1
    m.post_blank = 0
    m.post_indent = -1
    i = split + 1
    while i < xdf.nrec:
        m.post_indent = _get_indent(xdf.recs[i])
        if m.post_indent != -1:
            break
        m.post_blank += 1
        if m.post_blank == MAX_BLANKS:
            m.post_indent = 0
            break
        i += 1


def _score_add_split(m: _SplitMeasure, s: list) -> None:
    # s = [effective_indent, penalty]
    if m.pre_indent == -1 and m.pre_blank == 0:
        s[1] += START_OF_FILE_PENALTY
    if m.end_of_file:
        s[1] += END_OF_FILE_PENALTY
    post_blank = 1 + m.post_blank if m.indent == -1 else 0
    total_blank = m.pre_blank + post_blank
    s[1] += TOTAL_BLANK_WEIGHT * total_blank
    s[1] += POST_BLANK_WEIGHT * post_blank
    indent = m.indent if m.indent != -1 else m.post_indent
    any_blanks = (total_blank != 0)
    s[0] += indent
    if indent == -1:
        pass
    elif m.pre_indent == -1:
        pass
    elif indent > m.pre_indent:
        s[1] += RELATIVE_INDENT_WITH_BLANK_PENALTY if any_blanks else RELATIVE_INDENT_PENALTY
    elif indent == m.pre_indent:
        pass
    else:
        if m.post_indent != -1 and m.post_indent > indent:
            s[1] += RELATIVE_OUTDENT_WITH_BLANK_PENALTY if any_blanks else RELATIVE_OUTDENT_PENALTY
        else:
            s[1] += RELATIVE_DEDENT_WITH_BLANK_PENALTY if any_blanks else RELATIVE_DEDENT_PENALTY


def _score_cmp(s1: list, s2: list) -> int:
    cmp_indents = (s1[0] > s2[0]) - (s1[0] < s2[0])
    return INDENT_WEIGHT * cmp_indents + (s1[1] - s2[1])


def xdl_change_compact(xdf: Xdfile, xdfo: Xdfile, flags: int) -> None:
    g = _Group()
    go = _Group()
    _group_init(xdf, g)
    _group_init(xdfo, go)

    while True:
        if g.end == g.start:
            if _group_next(xdf, g):
                break
            if _group_next(xdfo, go):
                raise AssertionError("group sync broken moving to next group")
            continue

        while True:
            groupsize = g.end - g.start
            end_matching_other = -1

            while not _group_slide_up(xdf, g):
                if _group_previous(xdfo, go):
                    raise AssertionError("group sync broken sliding up")

            earliest_end = g.end
            if go.end > go.start:
                end_matching_other = g.end

            while True:
                if _group_slide_down(xdf, g):
                    break
                if _group_next(xdfo, go):
                    raise AssertionError("group sync broken sliding down")
                if go.end > go.start:
                    end_matching_other = g.end

            if groupsize == g.end - g.start:
                break

        if g.end == earliest_end:
            pass
        elif end_matching_other != -1:
            while go.end == go.start:
                if _group_slide_up(xdf, g):
                    raise AssertionError("match disappeared")
                if _group_previous(xdfo, go):
                    raise AssertionError("group sync broken sliding to match")
        elif flags & XDF_INDENT_HEURISTIC:
            best_shift = -1
            best_score = [0, 0]
            shift = earliest_end
            if g.end - groupsize - 1 > shift:
                shift = g.end - groupsize - 1
            if g.end - INDENT_HEURISTIC_MAX_SLIDING > shift:
                shift = g.end - INDENT_HEURISTIC_MAX_SLIDING
            while shift <= g.end:
                m = _SplitMeasure()
                score = [0, 0]
                _measure_split(xdf, shift, m)
                _score_add_split(m, score)
                _measure_split(xdf, shift - groupsize, m)
                _score_add_split(m, score)
                if best_shift == -1 or _score_cmp(score, best_score) <= 0:
                    best_score[0] = score[0]
                    best_score[1] = score[1]
                    best_shift = shift
                shift += 1
            while g.end > best_shift:
                if _group_slide_up(xdf, g):
                    raise AssertionError("best shift unreached")
                if _group_previous(xdfo, go):
                    raise AssertionError("group sync broken sliding to blank line")

        if _group_next(xdf, g):
            break
        if _group_next(xdfo, go):
            raise AssertionError("group sync broken moving to next group")

    if not _group_next(xdfo, go):
        raise AssertionError("group sync broken at end of file")


# ---------------------------------------------------------------------------
# edit script (xdiffi.c xdl_build_script)


class Xdchange:
    __slots__ = ("next", "i1", "i2", "chg1", "chg2", "ignore")

    def __init__(self, nxt, i1, i2, chg1, chg2):
        self.next = nxt
        self.i1 = i1
        self.i2 = i2
        self.chg1 = chg1
        self.chg2 = chg2
        self.ignore = 0


def xdl_build_script(xe: Xdfenv) -> Optional[Xdchange]:
    rchg1 = xe.xdf1.rchg
    rchg2 = xe.xdf2.rchg
    cscr: Optional[Xdchange] = None
    i1 = xe.xdf1.nrec
    i2 = xe.xdf2.nrec
    while i1 >= 0 or i2 >= 0:
        if rchg1[i1 - 1] or rchg2[i2 - 1]:
            l1 = i1
            while rchg1[i1 - 1]:
                i1 -= 1
            l2 = i2
            while rchg2[i2 - 1]:
                i2 -= 1
            cscr = Xdchange(cscr, i1, i2, l1 - i1, l2 - i2)
        i1 -= 1
        i2 -= 1
    return cscr


# ---------------------------------------------------------------------------
# three-way merge (xmerge.c)


class _Xdmerge:
    __slots__ = ("next", "mode", "i1", "i2", "chg1", "chg2", "i0", "chg0")

    def __init__(self):
        self.next = None
        self.mode = 0
        self.i1 = self.i2 = self.chg1 = self.chg2 = 0
        self.i0 = self.chg0 = 0


def _xdl_append_merge(c: Optional[_Xdmerge], mode, i0, chg0, i1, chg1, i2, chg2):
    """Returns (head_changes, current). Mirrors xdl_append_merge where *merge
    is the running tail pointer; we track and return it."""
    m = c
    if m and (i1 <= m.i1 + m.chg1 or i2 <= m.i2 + m.chg2):
        if mode != m.mode:
            m.mode = 0
        m.chg0 = i0 + chg0 - m.i0
        m.chg1 = i1 + chg1 - m.i1
        m.chg2 = i2 + chg2 - m.i2
        return m
    nm = _Xdmerge()
    nm.next = None
    nm.mode = mode
    nm.i0 = i0
    nm.chg0 = chg0
    nm.i1 = i1
    nm.chg1 = chg1
    nm.i2 = i2
    nm.chg2 = chg2
    if c is not None:
        c.next = nm
    return nm


def _is_eol_crlf(xdf: Xdfile, i: int) -> int:
    if i < xdf.nrec - 1:
        size = len(xdf.recs[i])
        return 1 if size > 1 and xdf.recs[i][size - 2] == 0x0D else 0
    if not xdf.nrec:
        return -1
    size = len(xdf.recs[i])
    if size and xdf.recs[i][size - 1] == 0x0A:
        return 1 if size > 1 and xdf.recs[i][size - 2] == 0x0D else 0
    if not i:
        return -1
    size = len(xdf.recs[i - 1])
    return 1 if size > 1 and xdf.recs[i - 1][size - 2] == 0x0D else 0


def _is_cr_needed(xe1: Xdfenv, xe2: Xdfenv, m: _Xdmerge) -> int:
    needs_cr = _is_eol_crlf(xe1.xdf2, m.i1 - 1 if m.i1 else 0)
    if needs_cr:
        needs_cr = _is_eol_crlf(xe2.xdf2, m.i2 - 1 if m.i2 else 0)
    if needs_cr:
        needs_cr = _is_eol_crlf(xe1.xdf1, 0)
    return 0 if needs_cr < 0 else needs_cr


def _recs_copy_0(use_orig: int, xe: Xdfenv, i: int, count: int, needs_cr: int,
                 add_nl: int, dest: bytearray) -> int:
    recs = (xe.xdf1.recs if use_orig else xe.xdf2.recs)
    size = 0
    if count < 1:
        return 0
    for k in range(count):
        dest.extend(recs[i + k])
        size += len(recs[i + k])
    if add_nl:
        last = recs[i + count - 1]
        ln = len(last)
        if ln == 0 or last[ln - 1] != 0x0A:
            if needs_cr:
                dest.append(0x0D)
                size += 1
            dest.append(0x0A)
            size += 1
    return size


def _recs_copy(xe, i, count, needs_cr, add_nl, dest):
    return _recs_copy_0(0, xe, i, count, needs_cr, add_nl, dest)


def _orig_copy(xe, i, count, needs_cr, add_nl, dest):
    return _recs_copy_0(1, xe, i, count, needs_cr, add_nl, dest)


def _fill_conflict_hunk(xe1, name1, xe2, name2, name3, i, style, m,
                        dest: bytearray, marker_size: int) -> None:
    needs_cr = _is_cr_needed(xe1, xe2, m)
    if marker_size <= 0:
        marker_size = DEFAULT_CONFLICT_MARKER_SIZE

    # before conflicting part
    _recs_copy(xe1, i, m.i1 - i, 0, 0, dest)

    dest.extend(b"<" * marker_size)
    if name1:
        dest.append(0x20)
        dest.extend(name1.encode("utf-8") if isinstance(name1, str) else name1)
    if needs_cr:
        dest.append(0x0D)
    dest.append(0x0A)

    # postimage from side #1
    _recs_copy(xe1, m.i1, m.chg1, needs_cr, 1, dest)

    if style in (XDL_MERGE_DIFF3, XDL_MERGE_ZEALOUS_DIFF3):
        dest.extend(b"|" * marker_size)
        if name3:
            dest.append(0x20)
            dest.extend(name3.encode("utf-8") if isinstance(name3, str) else name3)
        if needs_cr:
            dest.append(0x0D)
        dest.append(0x0A)
        _orig_copy(xe1, m.i0, m.chg0, needs_cr, 1, dest)

    dest.extend(b"=" * marker_size)
    if needs_cr:
        dest.append(0x0D)
    dest.append(0x0A)

    # postimage from side #2
    _recs_copy(xe2, m.i2, m.chg2, needs_cr, 1, dest)

    dest.extend(b">" * marker_size)
    if name2:
        dest.append(0x20)
        dest.extend(name2.encode("utf-8") if isinstance(name2, str) else name2)
    if needs_cr:
        dest.append(0x0D)
    dest.append(0x0A)


def _fill_merge_buffer(xe1, name1, xe2, name2, ancestor_name, favor,
                       changes: Optional[_Xdmerge], style, marker_size) -> bytes:
    dest = bytearray()
    i = 0
    m = changes
    while m:
        if favor and not m.mode:
            m.mode = favor
        if m.mode == 0:
            _fill_conflict_hunk(xe1, name1, xe2, name2, ancestor_name,
                                i, style, m, dest, marker_size)
        elif m.mode & 3:
            _recs_copy(xe1, i, m.i1 - i, 0, 0, dest)
            if m.mode & 1:
                needs_cr = _is_cr_needed(xe1, xe2, m)
                _recs_copy(xe1, m.i1, m.chg1, needs_cr, (m.mode & 2), dest)
            if m.mode & 2:
                _recs_copy(xe2, m.i2, m.chg2, 0, 0, dest)
        else:
            m = m.next
            continue
        i = m.i1 + m.chg1
        m = m.next
    _recs_copy(xe1, i, xe1.xdf2.nrec - i, 0, 0, dest)
    return bytes(dest)


def _xdl_merge_cmp_lines(xe1, i1, xe2, i2, line_count, flags) -> int:
    rec1 = xe1.xdf2.recs
    rec2 = xe2.xdf2.recs
    for k in range(line_count):
        if not xdl_recmatch(rec1[i1 + k], rec2[i2 + k], flags):
            return -1
    return 0


def _refine_zdiff3_conflicts(xe1, xe2, changes, flags) -> None:
    rec1 = xe1.xdf2.recs
    rec2 = xe2.xdf2.recs
    m = changes
    while m:
        if m.mode:
            m = m.next
            continue
        while m.chg1 and m.chg2 and xdl_recmatch(rec1[m.i1], rec2[m.i2], flags):
            m.chg1 -= 1
            m.chg2 -= 1
            m.i1 += 1
            m.i2 += 1
        while (m.chg1 and m.chg2 and
               xdl_recmatch(rec1[m.i1 + m.chg1 - 1], rec2[m.i2 + m.chg2 - 1], flags)):
            m.chg1 -= 1
            m.chg2 -= 1
        m = m.next


def _refine_conflicts(xe1, xe2, changes, flags) -> int:
    m = changes
    while m:
        if m.mode:
            m = m.next
            continue
        if m.chg1 == 0 or m.chg2 == 0:
            m = m.next
            continue
        i1 = m.i1
        i2 = m.i2
        t1 = b"".join(xe1.xdf2.recs[m.i1 : m.i1 + m.chg1])
        t2 = b"".join(xe2.xdf2.recs[m.i2 : m.i2 + m.chg2])
        xe = xdl_do_diff(t1, t2, flags)
        xdl_change_compact(xe.xdf1, xe.xdf2, flags)
        xdl_change_compact(xe.xdf2, xe.xdf1, flags)
        xscr = xdl_build_script(xe)
        if not xscr:
            m.mode = 4
            m = m.next
            continue
        x = xscr
        m.i1 = xscr.i1 + i1
        m.chg1 = xscr.chg1
        m.i2 = xscr.i2 + i2
        m.chg2 = xscr.chg2
        while xscr.next:
            m2 = _Xdmerge()
            xscr = xscr.next
            m2.next = m.next
            m.next = m2
            m = m2
            m.mode = 0
            m.i1 = xscr.i1 + i1
            m.chg1 = xscr.chg1
            m.i2 = xscr.i2 + i2
            m.chg2 = xscr.chg2
        m = m.next
    return 0


def _line_contains_alnum(rec: bytes) -> int:
    for c in rec:
        if (0x30 <= c <= 0x39) or (0x41 <= c <= 0x5A) or (0x61 <= c <= 0x7A):
            return 1
    return 0


def _lines_contain_alnum(xe, i, chg) -> int:
    for k in range(chg):
        if _line_contains_alnum(xe.xdf2.recs[i + k]):
            return 1
    return 0


def _merge_two_conflicts(m: _Xdmerge) -> None:
    next_m = m.next
    m.chg1 = next_m.i1 + next_m.chg1 - m.i1
    m.chg2 = next_m.i2 + next_m.chg2 - m.i2
    m.next = next_m.next


def _simplify_non_conflicts(xe1, changes, simplify_if_no_alnum) -> int:
    result = 0
    m = changes
    if not m:
        return result
    while True:
        next_m = m.next
        if not next_m:
            return result
        begin = m.i1 + m.chg1
        end = next_m.i1
        if (m.mode != 0 or next_m.mode != 0 or
                (end - begin > 3 and
                 (not simplify_if_no_alnum or _lines_contain_alnum(xe1, begin, end - begin)))):
            m = next_m
        else:
            result += 1
            _merge_two_conflicts(m)


def _xdl_do_merge(xe1, xscr1, xe2, xscr2, *, level, style, favor, flags,
                  name1, name2, ancestor_name, marker_size) -> tuple[bytes, int]:
    if style in (XDL_MERGE_DIFF3, XDL_MERGE_ZEALOUS_DIFF3):
        if XDL_MERGE_EAGER < level:
            level = XDL_MERGE_EAGER

    changes = None
    c = None

    while xscr1 and xscr2:
        if changes is None:
            changes = c
        if xscr1.i1 + xscr1.chg1 < xscr2.i1:
            i0 = xscr1.i1
            i1 = xscr1.i2
            i2 = xscr2.i2 - xscr2.i1 + xscr1.i1
            chg0 = xscr1.chg1
            chg1 = xscr1.chg2
            chg2 = xscr1.chg1
            c = _xdl_append_merge(c, 1, i0, chg0, i1, chg1, i2, chg2)
            if changes is None:
                changes = c
            xscr1 = xscr1.next
            continue
        if xscr2.i1 + xscr2.chg1 < xscr1.i1:
            i0 = xscr2.i1
            i1 = xscr1.i2 - xscr1.i1 + xscr2.i1
            i2 = xscr2.i2
            chg0 = xscr2.chg1
            chg1 = xscr2.chg1
            chg2 = xscr2.chg2
            c = _xdl_append_merge(c, 2, i0, chg0, i1, chg1, i2, chg2)
            if changes is None:
                changes = c
            xscr2 = xscr2.next
            continue
        if (level == XDL_MERGE_MINIMAL or xscr1.i1 != xscr2.i1 or
                xscr1.chg1 != xscr2.chg1 or xscr1.chg2 != xscr2.chg2 or
                _xdl_merge_cmp_lines(xe1, xscr1.i2, xe2, xscr2.i2, xscr1.chg2, flags)):
            off = xscr1.i1 - xscr2.i1
            ffo = off + xscr1.chg1 - xscr2.chg1
            i0 = xscr1.i1
            i1 = xscr1.i2
            i2 = xscr2.i2
            if off > 0:
                i0 -= off
                i1 -= off
            else:
                i2 += off
            chg0 = xscr1.i1 + xscr1.chg1 - i0
            chg1 = xscr1.i2 + xscr1.chg2 - i1
            chg2 = xscr2.i2 + xscr2.chg2 - i2
            if ffo < 0:
                chg0 -= ffo
                chg1 -= ffo
            else:
                chg2 += ffo
            c = _xdl_append_merge(c, 0, i0, chg0, i1, chg1, i2, chg2)
            if changes is None:
                changes = c

        i1 = xscr1.i1 + xscr1.chg1
        i2 = xscr2.i1 + xscr2.chg1
        if i1 >= i2:
            xscr2 = xscr2.next
        if i2 >= i1:
            xscr1 = xscr1.next

    while xscr1:
        if changes is None:
            changes = c
        i0 = xscr1.i1
        i1 = xscr1.i2
        i2 = xscr1.i1 + xe2.xdf2.nrec - xe2.xdf1.nrec
        chg0 = xscr1.chg1
        chg1 = xscr1.chg2
        chg2 = xscr1.chg1
        c = _xdl_append_merge(c, 1, i0, chg0, i1, chg1, i2, chg2)
        if changes is None:
            changes = c
        xscr1 = xscr1.next

    while xscr2:
        if changes is None:
            changes = c
        i0 = xscr2.i1
        i1 = xscr2.i1 + xe1.xdf2.nrec - xe1.xdf1.nrec
        i2 = xscr2.i2
        chg0 = xscr2.chg1
        chg1 = xscr2.chg1
        chg2 = xscr2.chg2
        c = _xdl_append_merge(c, 2, i0, chg0, i1, chg1, i2, chg2)
        if changes is None:
            changes = c
        xscr2 = xscr2.next

    if changes is None:
        changes = c

    if style == XDL_MERGE_ZEALOUS_DIFF3:
        _refine_zdiff3_conflicts(xe1, xe2, changes, flags)
    elif XDL_MERGE_ZEALOUS <= level:
        _refine_conflicts(xe1, xe2, changes, flags)
        _simplify_non_conflicts(xe1, changes, XDL_MERGE_ZEALOUS < level)

    result = _fill_merge_buffer(xe1, name1, xe2, name2, ancestor_name,
                                favor, changes, style, marker_size)
    # count conflicts
    count = 0
    m = changes
    while m:
        if m.mode == 0:
            count += 1
        m = m.next
    return result, count


def xdl_merge(orig: bytes, mf1: bytes, mf2: bytes, *,
              level: int = XDL_MERGE_ZEALOUS, style: int = 0, favor: int = 0,
              flags: int = XDF_HISTOGRAM_DIFF, marker_size: int = DEFAULT_CONFLICT_MARKER_SIZE,
              name1: Optional[str] = None, name2: Optional[str] = None,
              ancestor_name: Optional[str] = None) -> tuple[bytes, int]:
    """Port of xdl_merge.  Returns (result_bytes, num_conflicts)."""
    xe1 = xdl_do_diff(orig, mf1, flags)
    xe2 = xdl_do_diff(orig, mf2, flags)

    xdl_change_compact(xe1.xdf1, xe1.xdf2, flags)
    xdl_change_compact(xe1.xdf2, xe1.xdf1, flags)
    xscr1 = xdl_build_script(xe1)

    xdl_change_compact(xe2.xdf1, xe2.xdf2, flags)
    xdl_change_compact(xe2.xdf2, xe2.xdf1, flags)
    xscr2 = xdl_build_script(xe2)

    if xscr1 is None:
        return mf2, 0
    if xscr2 is None:
        return mf1, 0
    return _xdl_do_merge(xe1, xscr1, xe2, xscr2, level=level, style=style,
                         favor=favor, flags=flags, name1=name1, name2=name2,
                         ancestor_name=ancestor_name, marker_size=marker_size)
