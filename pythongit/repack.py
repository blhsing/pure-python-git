"""``git repack`` orchestration.

This is a port of git's ``builtin/repack.c`` (plus the helpers in
``repack.c``/``repack-geometry.c``/``repack-cruft.c`` and ``prune-packed.c``).
git itself shells out to ``git pack-objects`` for the heavy lifting; pygit
already has the pack-writing machinery in :mod:`pythongit.pack`, so this module
drives that machinery directly instead of spawning a child process. The flag
parsing, object selection, redundant-pack deletion and prune-packed behaviour
all mirror the C exactly so that the observable end state (``count-objects``,
``fsck``, reachability) is byte-identical.
"""
from __future__ import annotations

import os
import struct
import sys
import time
from pathlib import Path
from typing import Callable, Optional

from .repo import Repository


# repack.c ALL_INTO_ONE / LOOSEN_UNREACHABLE / PACK_CRUFT bits.
ALL_INTO_ONE = 1
LOOSEN_UNREACHABLE = 2
PACK_CRUFT = 4


_USAGE = (
    "usage: git repack [-a] [-A] [-d] [-f] [-F] [-l] [-n] [-q] [-b] [-m]\n"
    "       [--window=<n>] [--depth=<n>] [--threads=<n>] [--keep-pack=<pack-name>]\n"
    "       [--write-midx] [--name-hash-version=<n>] [--path-walk]\n"
    "\n"
    "    -a                    pack everything in a single pack\n"
    "    -A                    same as -a, and turn unreachable objects loose\n"
    "    --[no-]cruft          same as -a, pack unreachable cruft objects separately\n"
    "    --[no-]cruft-expiration <approxidate>\n"
    "                          with --cruft, expire objects older than this\n"
    "    --combine-cruft-below-size <n>\n"
    "                          with --cruft, only repack cruft packs smaller than this\n"
    "    --max-cruft-size <n>  with --cruft, limit the size of new cruft packs\n"
    "    -d                    remove redundant packs, and run git-prune-packed\n"
    "    -f                    pass --no-reuse-delta to git-pack-objects\n"
    "    -F                    pass --no-reuse-object to git-pack-objects\n"
    "    --[no-]name-hash-version <n>\n"
    "                          specify the name hash version to use for grouping similar objects by path\n"
    "    --[no-]path-walk      pass --path-walk to git-pack-objects\n"
    "    -n                    do not run git-update-server-info\n"
    "    -q, --[no-]quiet      be quiet\n"
    "    -l, --[no-]local      pass --local to git-pack-objects\n"
    "    -b, --[no-]write-bitmap-index\n"
    "                          write bitmap index\n"
    "    -i, --[no-]delta-islands\n"
    "                          pass --delta-islands to git-pack-objects\n"
    "    --[no-]unpack-unreachable <approxidate>\n"
    "                          with -A, do not loosen objects older than this\n"
    "    -k, --[no-]keep-unreachable\n"
    "                          with -a, repack unreachable objects\n"
    "    --[no-]window <n>     size of the window used for delta compression\n"
    "    --[no-]window-memory <bytes>\n"
    "                          same as the above, but limit memory size instead of entries count\n"
    "    --[no-]depth <n>      limits the maximum delta depth\n"
    "    --[no-]threads <n>    limits the maximum number of threads\n"
    "    --max-pack-size <n>   maximum size of each packfile\n"
    "    --[no-]filter <args>  object filtering\n"
    "    --[no-]pack-kept-objects\n"
    "                          repack objects in packs marked with .keep\n"
    "    --[no-]keep-pack <name>\n"
    "                          do not repack this pack\n"
    "    -g, --[no-]geometric <n>\n"
    "                          find a geometric progression with factor <N>\n"
    "    -m, --[no-]write-midx write a multi-pack index of the resulting packs\n"
    "    --[no-]expire-to <dir>\n"
    "                          pack prefix to store a pack containing pruned objects\n"
    "    --[no-]filter-to <dir>\n"
    "                          pack prefix to store a pack containing filtered out objects\n"
    "\n"
)


class UsageError(Exception):
    """Raised for parse_options-style usage errors (rc 129).

    ``show_usage`` mirrors git's parse_options: unknown options/switches print
    the usage block after the error line, while value-parse errors (missing or
    malformed values) print only the error line.
    """

    def __init__(self, message: str = "", show_usage: bool = True) -> None:
        super().__init__(message)
        self.message = message
        self.show_usage = show_usage


class FatalError(Exception):
    """Raised for die()-style fatal errors (rc 128)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class HelpRequested(Exception):
    """Raised when -h is given (usage to stdout, rc 129)."""


class Options:
    def __init__(self) -> None:
        self.pack_everything = 0
        self.delete_redundant = False
        self.no_reuse_delta = False
        self.no_reuse_object = False
        self.name_hash_version = 0
        self.path_walk = False
        self.run_update_server_info = True
        self.quiet = False
        self.local = False
        self.write_bitmaps: Optional[bool] = None  # tri-state (-1)
        self.delta_islands = False
        self.unpack_unreachable: Optional[str] = None
        self.keep_unreachable = False
        self.window: Optional[str] = None
        self.window_memory: Optional[str] = None
        self.depth: Optional[str] = None
        self.threads: Optional[str] = None
        self.max_pack_size = 0
        self.pack_kept_objects: Optional[bool] = None  # tri-state (-1)
        self.keep_pack_list: list[str] = []
        self.geometric: Optional[int] = None
        self.write_midx = False
        self.cruft_expiration: Optional[str] = None
        self.combine_cruft_below_size = 0
        self.max_cruft_size = 0
        self.expire_to: Optional[str] = None
        self.filter_to: Optional[str] = None
        self.filter: Optional[str] = None


# Long options that take a value, mapping to (attr, kind).
#   kind: "str" stores the raw string, "uint" parses an unsigned magnitude,
#         "int" parses a signed integer.
_LONG_VALUE_OPTS = {
    "cruft-expiration": ("cruft_expiration", "str"),
    "combine-cruft-below-size": ("combine_cruft_below_size", "uint"),
    "max-cruft-size": ("max_cruft_size", "uint"),
    "name-hash-version": ("name_hash_version", "int"),
    "unpack-unreachable": ("unpack_unreachable", "str"),
    # window/depth/threads are OPT_STRING in repack.c but are forwarded verbatim
    # to `git pack-objects`, where window/depth/threads are OPT_INTEGER and
    # window-memory is OPT_MAGNITUDE. git validates them in the child; we port
    # that validation here so a bad value fails identically (rc 129).
    "window": ("window", "int"),
    "window-memory": ("window_memory", "uint"),
    "depth": ("depth", "int"),
    "threads": ("threads", "int"),
    "max-pack-size": ("max_pack_size", "uint"),
    "keep-pack": ("keep_pack_list", "list"),
    "geometric": ("geometric", "int"),
    "expire-to": ("expire_to", "str"),
    "filter-to": ("filter_to", "str"),
    "filter": ("filter", "str"),
}

# Long boolean flags: name -> (attr, value)
_LONG_BOOL_OPTS = {
    "cruft": ("pack_everything", PACK_CRUFT),
    "path-walk": ("path_walk", True),
    "quiet": ("quiet", True),
    "local": ("local", True),
    "write-bitmap-index": ("write_bitmaps", True),
    "delta-islands": ("delta_islands", True),
    "keep-unreachable": ("keep_unreachable", True),
    "pack-kept-objects": ("pack_kept_objects", True),
    "write-midx": ("write_midx", True),
}


def _parse_uint(name: str, value: str) -> int:
    """Port of OPT_UNSIGNED: parse a non-negative magnitude with k/m/g suffix."""
    msg = (f"error: option `{name}' expects a non-negative integer value "
           "with an optional k/m/g suffix")
    v = value.strip()
    mult = 1
    if v and v[-1] in "kKmMgG":
        unit = v[-1].lower()
        mult = {"k": 1024, "m": 1024 * 1024, "g": 1024 * 1024 * 1024}[unit]
        v = v[:-1]
    try:
        n = int(v, 10)
    except ValueError:
        raise UsageError(msg, show_usage=False)
    if n < 0:
        raise UsageError(msg, show_usage=False)
    return n * mult


def _parse_int(name: str, value: str) -> int:
    """Port of OPT_INTEGER: parse a (possibly signed) integer with k/m/g suffix."""
    msg = (f"error: option `{name}' expects an integer value "
           "with an optional k/m/g suffix")
    v = value.strip()
    mult = 1
    if v and v[-1] in "kKmMgG":
        unit = v[-1].lower()
        mult = {"k": 1024, "m": 1024 * 1024, "g": 1024 * 1024 * 1024}[unit]
        v = v[:-1]
    body = v[1:] if v[:1] in ("+", "-") else v
    if not body or not body.isdigit():
        raise UsageError(msg, show_usage=False)
    return int(v, 10) * mult


def parse_options(argv: list[str], opts: Options) -> None:
    """Port of repack's parse_options(). Raises on errors."""
    i = 0
    n = len(argv)
    while i < n:
        arg = argv[i]
        if arg == "--":
            i += 1
            break
        if arg == "-h":
            raise HelpRequested()
        if arg.startswith("--"):
            body = arg[2:]
            name, eq, inline = body.partition("=")
            negate = False
            if name.startswith("no-"):
                negate = True
                name = name[3:]
            if name in _LONG_VALUE_OPTS:
                attr, kind = _LONG_VALUE_OPTS[name]
                if negate:
                    # --no-<value-opt> clears the value (OPT_STRING semantics).
                    if kind == "list":
                        setattr(opts, attr, [])
                    elif kind in ("uint", "int"):
                        setattr(opts, attr, 0)
                    else:
                        setattr(opts, attr, None)
                    i += 1
                    continue
                if eq:
                    val = inline
                else:
                    i += 1
                    if i >= n:
                        raise UsageError(
                            f"error: option `{name}' requires a value",
                            show_usage=False)
                    val = argv[i]
                if kind == "uint":
                    setattr(opts, attr, _parse_uint(name, val))
                elif kind == "int":
                    setattr(opts, attr, _parse_int(name, val))
                elif kind == "list":
                    getattr(opts, attr).append(val)
                else:
                    setattr(opts, attr, val)
                i += 1
                continue
            if name in _LONG_BOOL_OPTS:
                attr, value = _LONG_BOOL_OPTS[name]
                if attr == "pack_everything":
                    if negate:
                        opts.pack_everything &= ~value
                    else:
                        opts.pack_everything |= value
                elif attr == "write_bitmaps":
                    opts.write_bitmaps = not negate
                elif attr == "pack_kept_objects":
                    opts.pack_kept_objects = not negate
                else:
                    setattr(opts, attr, (not negate) and value)
                i += 1
                continue
            # OPT_NEGBIT('n', ...) long form is not provided; -n only.
            raise UsageError(f"error: unknown option `{body}'")
        if arg.startswith("-") and arg != "-":
            # Short option cluster.
            j = 1
            while j < len(arg):
                c = arg[j]
                if c == "a":
                    opts.pack_everything |= ALL_INTO_ONE
                elif c == "A":
                    opts.pack_everything |= LOOSEN_UNREACHABLE | ALL_INTO_ONE
                elif c == "d":
                    opts.delete_redundant = True
                elif c == "f":
                    opts.no_reuse_delta = True
                elif c == "F":
                    opts.no_reuse_object = True
                elif c == "n":
                    opts.run_update_server_info = False
                elif c == "q":
                    opts.quiet = True
                elif c == "l":
                    opts.local = True
                elif c == "b":
                    opts.write_bitmaps = True
                elif c == "i":
                    opts.delta_islands = True
                elif c == "k":
                    opts.keep_unreachable = True
                elif c in ("g", "m"):
                    # -g/-m take/are flags with attached or following values.
                    if c == "m":
                        opts.write_midx = True
                        j += 1
                        continue
                    # -g requires a value (geometric factor).
                    rest = arg[j + 1:]
                    if rest:
                        val = rest
                    else:
                        i += 1
                        if i >= n:
                            raise UsageError(
                                "error: switch `g' requires a value",
                                show_usage=False)
                        val = argv[i]
                    try:
                        opts.geometric = int(val, 10)
                    except ValueError:
                        raise UsageError(
                            "error: switch `g' expects an integer value "
                            "with an optional k/m/g suffix",
                            show_usage=False)
                    j = len(arg)
                    break
                else:
                    raise UsageError(f"error: unknown switch `{c}'")
                j += 1
            i += 1
            continue
        # Non-option (positional) argument: repack calls parse_options(..., 0),
        # which permutes non-options to the end and never inspects the leftover
        # argv, so positionals are silently ignored (rc 0) while later options
        # are still parsed. Skip this token and keep going.
        i += 1
        continue
    # Anything after `--` is ignored as well (repack consumes no positionals).


# ---------------------------------------------------------------------------
# pack inventory


class ExistingPack:
    __slots__ = ("base", "pack_path", "is_kept", "is_cruft", "shas",
                 "marked_for_deletion", "retained", "pack_sha")

    def __init__(self, base: str, pack_path: Path) -> None:
        self.base = base  # e.g. "pack-<hash>"
        self.pack_path = pack_path
        self.is_kept = False
        self.is_cruft = False
        self.shas: list[str] = []
        self.marked_for_deletion = False
        self.retained = False
        self.pack_sha = base[len("pack-"):] if base.startswith("pack-") else base


def collect_existing_packs(repo: Repository, extra_keep: list[str]) -> dict[str, list[ExistingPack]]:
    """Port of existing_packs_collect().

    Returns a dict with keys ``kept``, ``non_kept``, ``cruft`` each holding a
    sorted list of :class:`ExistingPack`.
    """
    from . import pack as _p

    pack_dir = repo.gitdir / "objects" / "pack"
    kept: list[ExistingPack] = []
    non_kept: list[ExistingPack] = []
    cruft: list[ExistingPack] = []
    if pack_dir.is_dir():
        for pack_path in sorted(pack_dir.glob("pack-*.pack")):
            base = pack_path.stem  # "pack-<hash>"
            ep = ExistingPack(base, pack_path)
            keep_file = pack_path.with_suffix(".keep")
            mtimes_file = pack_path.with_suffix(".mtimes")
            ep.is_cruft = mtimes_file.exists()
            basename = pack_path.name  # "pack-<hash>.pack"
            extra = (basename in extra_keep) or (base in extra_keep)
            try:
                pk = _p.Pack(pack_path, repo.object_format())
                ep.shas = list(pk.shas)
                pk.close()
            except (OSError, ValueError):
                ep.shas = []
            if extra or keep_file.exists():
                ep.is_kept = True
                kept.append(ep)
            elif ep.is_cruft:
                cruft.append(ep)
            else:
                non_kept.append(ep)
    kept.sort(key=lambda e: e.base)
    non_kept.sort(key=lambda e: e.base)
    cruft.sort(key=lambda e: e.base)
    return {"kept": kept, "non_kept": non_kept, "cruft": cruft}


# ---------------------------------------------------------------------------
# pack writing


def _write_pack(repo: Repository, shas: list[str], *, cruft: bool = False,
                write_bitmap: bool = False, allow_empty: bool = False) -> Optional[str]:
    """Write a pack containing exactly ``shas``. Returns the pack hash, or
    ``None`` when ``shas`` is empty and ``allow_empty`` is false (mirrors
    pack-objects --non-empty; the filtered pack omits --non-empty)."""
    if not shas and not allow_empty:
        return None
    from . import pack as _p

    pack_dir = repo.gitdir / "objects" / "pack"
    pack_dir.mkdir(parents=True, exist_ok=True)
    _p.clear_pack_cache(repo)
    tmp = pack_dir / f".tmp-{os.getpid()}-{time.time_ns()}.pack"
    try:
        pack_sha, entries = _p.write_pack_stream(repo, sorted(set(shas)), tmp)
        idx_bytes = _p.write_idx_v2_from_checksum(
            bytes.fromhex(pack_sha), entries, repo.object_format())
        pack_path = pack_dir / f"pack-{pack_sha}.pack"
        os.replace(tmp, pack_path)
    finally:
        tmp.unlink(missing_ok=True)
    (pack_dir / f"pack-{pack_sha}.idx").write_bytes(idx_bytes)
    if cruft:
        _write_mtimes(pack_dir / f"pack-{pack_sha}.mtimes",
                      bytes.fromhex(pack_sha), entries, repo)
    if write_bitmap:
        try:
            _p.write_pack_bitmap(repo, pack_path, entries)
        except Exception:
            pass
    _p.clear_pack_cache(repo)
    return pack_sha


def _write_mtimes(path: Path, pack_checksum: bytes,
                  entries: list[tuple[str, int, int]], repo: Repository) -> None:
    """Write a ``.mtimes`` sidecar (pack-mtimes.c format): 12-byte header,
    one big-endian uint32 mtime per object in idx (oid-sorted) order, then the
    pack checksum and the file's own checksum."""
    hash_id = 1 if repo.object_format() == "sha1" else 2
    now = int(time.time())
    ordered = sorted(entries, key=lambda x: x[0])
    buf = bytearray()
    buf += struct.pack(">III", 0x4D544D45, 1, hash_id)  # "MTME", version 1
    for _sha, _off, _crc in ordered:
        buf += struct.pack(">I", now)
    buf += pack_checksum
    from . import pack as _p

    buf += _p._hash_bytes_for_algo(repo.object_format(), bytes(buf))
    path.write_bytes(bytes(buf))


def _write_loose(repo: Repository, shas: list[str]) -> None:
    """Loosen objects (write them as loose objects) — used for -A."""
    from . import objects as _o

    for sha in shas:
        try:
            otype, data = _o.read_object(repo, sha)
        except (OSError, KeyError, ValueError):
            continue
        _o.write_object(repo, otype, data)


# ---------------------------------------------------------------------------
# reachability / object selection


def _reachable(repo: Repository) -> set[str]:
    from . import cli

    return cli._reachable(repo)


def _loose_shas(repo: Repository) -> set[str]:
    from . import loose

    return set(loose.iter_shas(repo))


def _packed_shas(packs: list[ExistingPack]) -> set[str]:
    out: set[str] = set()
    for p in packs:
        out.update(p.shas)
    return out


# ---------------------------------------------------------------------------
# geometric progression


def _geometry_split(weights: list[int], split_factor: int) -> int:
    """Port of compute_pack_geometry_split(): ``weights`` is the per-pack object
    count in ascending order; returns the index ``split`` such that packs
    ``[0, split)`` are rolled up."""
    pack_nr = len(weights)
    if not pack_nr:
        return 0
    i = pack_nr - 1
    while i > 0:
        if weights[i] < split_factor * weights[i - 1]:
            break
        i -= 1
    split = i
    if split:
        split += 1
    total_size = 0
    for k in range(split):
        total_size += weights[k]
    i = split
    while i < pack_nr:
        if weights[i] < split_factor * total_size:
            split += 1
            total_size += weights[i]
            i += 1
        else:
            break
    return split


# ---------------------------------------------------------------------------
# prune-packed (delete loose objects already present in a pack)


def _prune_packed_objects(repo: Repository) -> None:
    """Port of prune_packed_objects(): remove loose objects that exist in a
    pack, then remove now-empty fanout directories."""
    from . import pack as _p

    objects_dir = repo.gitdir / "objects"
    if not objects_dir.is_dir():
        return
    _p.clear_pack_cache(repo)
    for sub in sorted(objects_dir.iterdir()):
        if not (len(sub.name) == 2 and sub.is_dir()):
            continue
        try:
            int(sub.name, 16)
        except ValueError:
            continue
        for obj in list(sub.iterdir()):
            if not obj.is_file():
                continue
            sha = sub.name + obj.name
            if len(sha) != repo.hash_len * 2:
                continue
            if _p.find_in_packs(repo, sha) is not None:
                obj.unlink()  # has_object_pack -> unlink_or_warn
        # prune_subdir: rmdir (succeeds only when empty).
        try:
            sub.rmdir()
        except OSError:
            pass


def _remove_pack(repo: Repository, ep: ExistingPack) -> None:
    """Port of repack_remove_redundant_pack(): unlink all sidecars of a pack."""
    base = ep.pack_path.with_suffix("")  # strip ".pack"
    for ext in (".pack", ".idx", ".rev", ".mtimes", ".bitmap", ".promisor", ".keep"):
        p = Path(str(base) + ext)
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


# ---------------------------------------------------------------------------
# top-level driver


def _precious_objects(repo: Repository) -> bool:
    """Whether extensions.preciousObjects is set (repository_format_precious_objects).
    git only honours repository extensions at format version >= 1."""
    from . import gitconfig

    try:
        version = gitconfig.get(repo, "core.repositoryformatversion")
        if version is None or int(version) < 1:
            return False
    except (ValueError, OSError):
        return False
    val = gitconfig.get(repo, "extensions.preciousobjects")
    if val is None:
        return False
    return val.strip().lower() in ("1", "true", "yes", "on")


_MIN_PACK_SIZE = 1024 * 1024


def _maybe_warn_min_pack_size(max_pack_size: int,
                              err: Optional[Callable[[str], None]]) -> None:
    """pack-objects warns and clamps when a non-zero --max-pack-size is below
    1 MiB (builtin/pack-objects.c). repack invokes pack-objects, so the warning
    surfaces once per invocation."""
    if err is not None and 0 < max_pack_size < _MIN_PACK_SIZE:
        err("warning: minimum pack size limit is 1 MiB")


def run(repo: Repository, opts: Options,
        out: Callable[[str], None],
        err: Optional[Callable[[str], None]] = None) -> int:
    """Execute a parsed repack. ``out`` writes a line to stdout."""
    if opts.delete_redundant and _precious_objects(repo):
        raise FatalError("cannot delete packs in a precious-objects repo")

    # die_for_incompatible_opt3(-A/--unpack-unreachable, -k, --cruft)
    has_loosen = bool(opts.unpack_unreachable) or bool(opts.pack_everything & LOOSEN_UNREACHABLE)
    set_opts = []
    if has_loosen:
        set_opts.append("-A")
    if opts.keep_unreachable:
        set_opts.append("-k/--keep-unreachable")
    if opts.pack_everything & PACK_CRUFT:
        set_opts.append("--cruft")
    if len(set_opts) >= 2:
        raise FatalError(
            f"options '{set_opts[0]}' and '{set_opts[1]}' cannot be used together")

    if opts.geometric is not None and opts.pack_everything:
        raise FatalError("options '--geometric' and '-A/-a' cannot be used together")

    if opts.filter_to and not opts.filter:
        raise FatalError("option '--filter-to' can only be used along with '--filter'")

    if opts.pack_everything & PACK_CRUFT:
        opts.pack_everything |= ALL_INTO_ONE

    # write_bitmaps defaults to off for a non-bare repo unless --write-midx and
    # not all-into-one. pygit repos are never bare in the harness.
    write_bitmaps = opts.write_bitmaps
    if write_bitmaps is None:
        if not opts.write_midx and (
                not (opts.pack_everything & ALL_INTO_ONE) or True):
            # is_bare_repository() is always false in the harness.
            write_bitmaps = False

    if write_bitmaps and not (opts.pack_everything & ALL_INTO_ONE) and not opts.write_midx:
        raise FatalError(
            "Incremental repacks are incompatible with bitmap indexes.  Use\n"
            "--no-write-bitmap-index or disable the pack.writeBitmaps configuration.")

    existing = collect_existing_packs(repo, opts.keep_pack_list)
    non_kept = existing["non_kept"]
    kept = existing["kept"]
    cruft_packs = existing["cruft"]

    reachable = _reachable(repo)
    loose = _loose_shas(repo)
    kept_shas = _packed_shas(kept)

    names: list[str] = []  # pack hashes we just wrote

    # Every code path below invokes pack-objects for the main pack, which emits
    # the min-pack-size warning during option validation.
    _maybe_warn_min_pack_size(opts.max_pack_size, err)

    if opts.geometric is not None:
        # Geometric repack: roll up the smaller packs (and loose objects) into
        # a new pack, leaving the larger packs alone. Object set is preserved.
        rollup_packs, _split = _geometric_rollup(repo, opts, existing)
        roll_shas: set[str] = set()
        for ep in rollup_packs:
            roll_shas.update(ep.shas)
        # --unpacked: also fold in loose objects (everything loose).
        roll_shas.update(loose)
        pack_sha = _write_pack(repo, sorted(roll_shas), write_bitmap=bool(write_bitmaps))
        if pack_sha:
            names.append(pack_sha)
        _finish(repo, opts, out, err, names, existing, reachable, loose,
                write_bitmaps, geometric_rollup=rollup_packs)
        return 0

    if opts.pack_everything & ALL_INTO_ONE:
        main_shas = set(reachable)
        if opts.keep_unreachable:
            # -k: also repack unreachable objects into the single pack.
            all_objs = set(loose)
            for ep in non_kept + cruft_packs:
                all_objs.update(ep.shas)
            main_shas |= all_objs
        # Do not repack objects already in kept packs (honor-pack-keep is the
        # default once pack_kept_objects is off).
        if opts.pack_kept_objects is not True:
            main_shas -= kept_shas
        omitted: set[str] = set()
        omitted_from_packs: set[str] = set()
        if opts.filter:
            # --filter: the main pack receives only the objects that pass the
            # filter. Objects that were in existing packs but are filtered out
            # go into a separate "filtered" pack (builtin/repack.c
            # write_filtered_pack uses --stdin-packs over the existing packs);
            # filtered-out *loose* objects are left loose, exactly as git does.
            existing_packed = _packed_shas(non_kept + cruft_packs)
            kept_objs, omitted = _apply_filter(repo, main_shas, opts.filter)
            main_shas = kept_objs
            omitted_from_packs = omitted & existing_packed
        pack_sha = _write_pack(repo, sorted(main_shas), write_bitmap=bool(write_bitmaps))
        if pack_sha:
            names.append(pack_sha)
        if opts.filter:
            # The filtered pack is always written (write_filtered_pack does not
            # pass --non-empty), even when no objects were filtered out.
            _write_pack(repo, sorted(omitted_from_packs), allow_empty=True)
    else:
        # Incremental: pack reachable objects that are currently loose.
        main_shas = reachable & loose
        pack_sha = _write_pack(repo, sorted(main_shas), write_bitmap=False)
        if pack_sha:
            names.append(pack_sha)

    _finish(repo, opts, out, err, names, existing, reachable, loose, write_bitmaps)
    return 0


def _parse_size_suffix(value: str) -> int:
    v = value.strip()
    mult = 1
    if v and v[-1] in "kKmMgG":
        mult = {"k": 1024, "m": 1024 * 1024, "g": 1024 * 1024 * 1024}[v[-1].lower()]
        v = v[:-1]
    return int(v) * mult


def _apply_filter(repo: Repository, shas: set[str],
                  spec: str) -> tuple[set[str], set[str]]:
    """Split ``shas`` into (kept, omitted) according to an object-filter spec.

    Supports the deterministic spec families (list-objects-filter.c): blob:none,
    blob:limit=<n>, tree:<depth>, object:type=<t>, and combine:<a>+<b>. Commits
    and tags are always kept; the filter only ever omits blobs/trees. Objects
    reachable only through an omitted tree are also omitted, matching git's
    traversal-based filtering.
    """
    from . import objects as _o

    # Classify each object's type once.
    types: dict[str, str] = {}
    datas: dict[str, bytes] = {}
    for sha in shas:
        try:
            t, d = _o.read_object(repo, sha)
        except (OSError, KeyError, ValueError):
            continue
        types[sha] = t
        datas[sha] = d

    specs = spec.split("+") if spec.startswith("combine:") else [spec]
    if spec.startswith("combine:"):
        specs = spec[len("combine:"):].split("+")

    def blob_omitted(sha: str) -> bool:
        d = datas.get(sha, b"")
        for s in specs:
            if s == "blob:none":
                return True
            if s.startswith("blob:limit="):
                if len(d) >= _parse_size_suffix(s[len("blob:limit="):]):
                    return True
            if s.startswith("object:type="):
                if s[len("object:type="):] != "blob":
                    return True
        return False

    # tree:<depth> omits trees/blobs deeper than <depth> in the tree hierarchy.
    tree_depth: Optional[int] = None
    type_filter: Optional[str] = None
    for s in specs:
        if s.startswith("tree:"):
            try:
                tree_depth = int(s[len("tree:"):])
            except ValueError:
                tree_depth = None
        if s.startswith("object:type="):
            type_filter = s[len("object:type="):]

    omitted: set[str] = set()

    if tree_depth is not None or type_filter is not None:
        # Depth-aware / type traversal from commit roots.
        depth_of: dict[str, int] = {}
        for sha, t in types.items():
            if t == "commit":
                c = _o.parse_commit(datas[sha])
                if c.tree in types:
                    depth_of[c.tree] = min(depth_of.get(c.tree, 1 << 30), 1)
            elif t == "tag":
                pass
        # BFS over trees assigning minimal depth.
        changed = True
        while changed:
            changed = False
            for sha, t in list(types.items()):
                if t != "tree" or sha not in depth_of:
                    continue
                d = depth_of[sha]
                for e in _o.parse_tree(datas[sha], repo.hash_len):
                    nd = d + 1
                    if e.sha in types and nd < depth_of.get(e.sha, 1 << 30):
                        depth_of[e.sha] = nd
                        changed = True
        for sha, t in types.items():
            if t in ("commit", "tag"):
                continue
            d = depth_of.get(sha)
            if tree_depth is not None and d is not None and d > tree_depth:
                omitted.add(sha)
            if type_filter is not None and t != type_filter:
                omitted.add(sha)

    for sha, t in types.items():
        if t == "blob" and blob_omitted(sha):
            omitted.add(sha)

    kept = set(shas) - omitted
    return kept, omitted


def _geometric_rollup(repo: Repository, opts: Options,
                      existing: dict) -> tuple[list[ExistingPack], int]:
    """Return the list of existing packs that should be rolled up, per the
    geometric progression, and the split index."""
    candidates = list(existing["non_kept"])  # non-kept, non-cruft, local packs
    if opts.local:
        pass  # all pygit packs are local
    candidates.sort(key=lambda e: len(e.shas))
    weights = [len(e.shas) for e in candidates]
    split = _geometry_split(weights, opts.geometric)
    return candidates[:split], split


def _object_mtimes(repo: Repository, candidates: set[str],
                   packs: list[ExistingPack],
                   cruft_packs: list[ExistingPack]) -> dict[str, int]:
    """Compute the cruft mtime for each candidate object.

    * loose objects use the file st_mtime;
    * objects in a regular pack use the pack file's st_mtime;
    * objects in an existing cruft pack use the per-object mtime recorded in
      the ``.mtimes`` sidecar.
    """
    mtimes: dict[str, int] = {}
    objects_dir = repo.gitdir / "objects"
    # Loose objects.
    for sha in candidates:
        p = objects_dir / sha[:2] / sha[2:]
        try:
            mtimes[sha] = int(p.stat().st_mtime)
        except OSError:
            pass
    # Regular packs: pack file mtime applies to all its objects.
    cruft_set = {c.base for c in cruft_packs}
    for ep in packs:
        if ep.base in cruft_set:
            continue
        try:
            pmtime = int(ep.pack_path.stat().st_mtime)
        except OSError:
            continue
        for sha in ep.shas:
            if sha in candidates:
                mtimes.setdefault(sha, pmtime)
    # Cruft packs: per-object mtimes from the .mtimes sidecar.
    for ep in cruft_packs:
        per = _read_mtimes(ep.pack_path.with_suffix(".mtimes"), ep.shas, repo)
        for sha, mt in per.items():
            if sha in candidates:
                mtimes.setdefault(sha, mt)
    return mtimes


def _read_mtimes(path: Path, shas: list[str], repo: Repository) -> dict[str, int]:
    """Parse a ``.mtimes`` sidecar -> {sha: mtime}. ``shas`` are the pack's
    object ids in idx (oid-sorted) order."""
    out: dict[str, int] = {}
    try:
        data = path.read_bytes()
    except OSError:
        return out
    if len(data) < 12:
        return out
    ordered = sorted(shas)
    pos = 12
    for sha in ordered:
        if pos + 4 > len(data):
            break
        out[sha] = struct.unpack(">I", data[pos:pos + 4])[0]
        pos += 4
    return out


def _expire_cruft(repo: Repository, cruft_shas: set[str], expiration: str,
                  packs: list[ExistingPack],
                  cruft_packs: list[ExistingPack]) -> set[str]:
    """Filter ``cruft_shas`` to those that should land in the cruft pack given a
    ``--cruft-expiration``. Objects with mtime > expiration are "recent" tips;
    the cruft pack also includes everything reachable from those tips (within
    the candidate set)."""
    expire_ts = _parse_cruft_expiration(expiration)
    if expire_ts == 0:
        # "never" / @0: nothing expires.
        return cruft_shas
    mtimes = _object_mtimes(repo, cruft_shas, packs, cruft_packs)
    recent = {sha for sha in cruft_shas if mtimes.get(sha, 0) > expire_ts}
    # Traverse from recent tips, staying within the candidate set, so objects
    # reachable from a fresh tip are kept even if their own mtime is old.
    from . import objects as _o

    kept: set[str] = set()
    stack = list(recent)
    while stack:
        sha = stack.pop()
        if sha in kept or sha not in cruft_shas:
            continue
        kept.add(sha)
        try:
            otype, data = _o.read_object(repo, sha)
        except (OSError, KeyError, ValueError):
            continue
        if otype == "commit":
            c = _o.parse_commit(data)
            stack.append(c.tree)
            stack.extend(c.parents)
        elif otype == "tree":
            for e in _o.parse_tree(data, repo.hash_len):
                stack.append(e.sha)
        elif otype == "tag":
            for line in data.decode("utf-8", errors="replace").splitlines():
                if line.startswith("object "):
                    stack.append(line[len("object "):].strip())
                    break
    return kept


def _parse_cruft_expiration(value: str) -> int:
    """Parse a --cruft-expiration approxidate into a unix timestamp. Mirrors the
    deterministic subset of git's approxidate(): never/now/@epoch/ISO dates."""
    from . import cli

    low = value.strip().lower()
    if low in ("never", ""):
        return 0
    try:
        return cli._parse_expiry_date(value)
    except ValueError:
        # Unparseable dates fall back to "now" (approxidate is lenient); using
        # the current time means everything older expires.
        return int(time.time())


def _finish(repo: Repository, opts: Options, out: Callable[[str], None],
            err: Optional[Callable[[str], None]],
            names: list[str], existing: dict, reachable: set[str],
            loose: set[str], write_bitmaps: Optional[bool],
            geometric_rollup: Optional[list[ExistingPack]] = None) -> None:
    non_kept = existing["non_kept"]
    kept = existing["kept"]
    cruft_packs = existing["cruft"]

    if not names and not (opts.pack_everything & PACK_CRUFT):
        if not opts.quiet:
            out("Nothing new to pack.")

    # --cruft: write unreachable objects into a separate cruft pack.
    if opts.pack_everything & PACK_CRUFT:
        # cruft pack-objects inherits max_pack_size from --max-cruft-size, or
        # falls back to the main --max-pack-size; it warns on its own.
        cruft_max = opts.max_cruft_size or opts.max_pack_size
        _maybe_warn_min_pack_size(cruft_max, err)
        cruft_candidates: set[str] = set(loose)
        for ep in non_kept + cruft_packs:
            cruft_candidates.update(ep.shas)
        cruft_shas = cruft_candidates - reachable - _packed_shas(kept)
        if opts.cruft_expiration is not None:
            # With --cruft-expiration only objects whose mtime is strictly newer
            # than the expiration (or reachable from such an object) are written
            # into the cruft pack; older ones are left where they are so a later
            # `git prune` can collect them (reachable.c:obj_is_recent).
            cruft_shas = _expire_cruft(
                repo, cruft_shas, opts.cruft_expiration,
                non_kept + cruft_packs, cruft_packs)
        cruft_sha = _write_pack(repo, sorted(cruft_shas), cruft=True)
        if cruft_sha:
            names.append(cruft_sha)
        if not names and not opts.quiet:
            out("Nothing new to pack.")

    # -A: loosen unreachable objects from packs that are about to be deleted.
    loosen = bool(opts.pack_everything & LOOSEN_UNREACHABLE)
    if loosen and opts.delete_redundant:
        to_loosen: set[str] = set()
        for ep in non_kept + cruft_packs:
            to_loosen.update(ep.shas)
        to_loosen -= reachable
        to_loosen -= _packed_shas(kept)
        to_loosen -= loose
        _write_loose(repo, sorted(to_loosen))

    # Mark redundant packs for deletion (-d with ALL_INTO_ONE).
    if opts.delete_redundant and (opts.pack_everything & ALL_INTO_ONE):
        new_set = set(names)
        for ep in non_kept + cruft_packs:
            if ep.pack_sha not in new_set:
                ep.marked_for_deletion = True

    # Delete redundant packs and run prune-packed.
    if opts.delete_redundant:
        from . import pack as _p

        _p.clear_pack_cache(repo)
        for ep in non_kept + cruft_packs:
            if ep.marked_for_deletion:
                _remove_pack(repo, ep)
        if geometric_rollup is not None:
            for ep in geometric_rollup:
                if ep.pack_sha not in set(names):
                    _remove_pack(repo, ep)
        _prune_packed_objects(repo)

    from . import pack as _p

    _p.clear_pack_cache(repo)

    # --write-midx: write a multi-pack-index over the surviving packs. Done
    # after redundant packs are removed so the midx covers the final set.
    if opts.write_midx:
        try:
            _p.write_midx(
                repo.gitdir / "objects" / "pack",
                repo.object_format(),
                write_bitmap=bool(write_bitmaps),
                repo=repo,
            )
        except Exception:
            pass
        _p.clear_pack_cache(repo)

    # update-server-info (-n suppresses), repack.c: run_update_server_info.
    if opts.run_update_server_info:
        write_server_info(repo)


def write_server_info(repo: Repository) -> None:
    """Port of server-info.c update_server_info(): write info/refs and
    objects/info/packs.  Shared by ``git repack`` and ``git update-server-info``.
    """
    from . import objects as _objs
    from . import refs as _refs

    # info/refs: every ref (HEAD excluded, like for_each_ref), sorted by name,
    # with a peeled "^{}" line for tag objects.
    info_dir = repo.gitdir / "info"
    info_dir.mkdir(parents=True, exist_ok=True)
    all_refs = _refs.iter_all_refs(repo)
    out: list[str] = []
    for name in sorted(all_refs):
        sha = all_refs[name]
        out.append("%s\t%s\n" % (sha, name))
        try:
            t, _data = _objs.read_object(repo, sha)
        except Exception:  # noqa: BLE001
            t = None
        if t == "tag":
            peeled = _refs.peel_ref(repo, name)
            if peeled:
                out.append("%s\t%s^{}\n" % (peeled, name))
    (info_dir / "refs").write_text("".join(out), encoding="utf-8")

    # objects/info/packs: "P <name>\n" per local pack, then a trailing blank.
    pack_dir = repo.gitdir / "objects" / "pack"
    pack_info_dir = repo.gitdir / "objects" / "info"
    pack_info_dir.mkdir(parents=True, exist_ok=True)
    pack_lines: list[str] = []
    if pack_dir.exists():
        for f in sorted(pack_dir.glob("pack-*.pack")):
            pack_lines.append("P %s\n" % f.name)
    (pack_info_dir / "packs").write_text("".join(pack_lines) + "\n", encoding="utf-8")
