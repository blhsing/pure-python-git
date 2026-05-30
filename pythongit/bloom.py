"""Changed-path Bloom filters used by commit-graph files."""
from __future__ import annotations

import struct

from . import objects as objs
from . import workdir
from .repo import Repository


BLOOM_HASH_VERSION = 1
BLOOM_NUM_HASHES = 7
BLOOM_BITS_PER_ENTRY = 10
BLOOM_MAX_CHANGED_PATHS = 512
_BLOOM_SEED0 = 0x293AE76F
_BLOOM_SEED1 = 0x7E646E2C


def _murmur3_x86_32(data: bytes, seed: int) -> int:
    c1 = 0xCC9E2D51
    c2 = 0x1B873593
    h = seed & 0xFFFFFFFF
    rounded = len(data) & ~3
    for pos in range(0, rounded, 4):
        k = struct.unpack_from("<I", data, pos)[0]
        k = (k * c1) & 0xFFFFFFFF
        k = ((k << 15) | (k >> 17)) & 0xFFFFFFFF
        k = (k * c2) & 0xFFFFFFFF
        h ^= k
        h = ((h << 13) | (h >> 19)) & 0xFFFFFFFF
        h = (h * 5 + 0xE6546B64) & 0xFFFFFFFF

    k = 0
    tail = data[rounded:]
    if len(tail) == 3:
        k ^= tail[2] << 16
    if len(tail) >= 2:
        k ^= tail[1] << 8
    if len(tail) >= 1:
        k ^= tail[0]
        k = (k * c1) & 0xFFFFFFFF
        k = ((k << 15) | (k >> 17)) & 0xFFFFFFFF
        k = (k * c2) & 0xFFFFFFFF
        h ^= k

    h ^= len(data)
    h ^= h >> 16
    h = (h * 0x85EBCA6B) & 0xFFFFFFFF
    h ^= h >> 13
    h = (h * 0xC2B2AE35) & 0xFFFFFFFF
    h ^= h >> 16
    return h & 0xFFFFFFFF


def _collect_tree_paths(repo: Repository, tree_sha: str, prefix: str, out: set[str]) -> None:
    stack = [(prefix, tree_sha)]
    while stack:
        cur_prefix, cur_tree = stack.pop()
        try:
            entries = workdir._tree_entries(repo, cur_tree)
        except KeyError:
            continue
        for entry in entries:
            path = f"{cur_prefix}{entry.name}"
            if entry.is_dir():
                stack.append((path + "/", entry.sha))
            else:
                out.add(path)


def _diff_trees(repo: Repository, old_tree: str | None, new_tree: str | None, prefix: str, out: set[str]) -> None:
    if old_tree == new_tree:
        return
    if old_tree is None:
        if new_tree is not None:
            _collect_tree_paths(repo, new_tree, prefix, out)
        return
    if new_tree is None:
        _collect_tree_paths(repo, old_tree, prefix, out)
        return
    try:
        old_entries = {entry.name: entry for entry in workdir._tree_entries(repo, old_tree)}
        new_entries = {entry.name: entry for entry in workdir._tree_entries(repo, new_tree)}
    except KeyError:
        return
    for name in set(old_entries) | set(new_entries):
        old = old_entries.get(name)
        new = new_entries.get(name)
        path = f"{prefix}{name}"
        if old is None:
            if new is not None and new.is_dir():
                _collect_tree_paths(repo, new.sha, path + "/", out)
            else:
                out.add(path)
            continue
        if new is None:
            if old.is_dir():
                _collect_tree_paths(repo, old.sha, path + "/", out)
            else:
                out.add(path)
            continue
        if (old.mode, old.sha) == (new.mode, new.sha):
            continue
        if old.is_dir() and new.is_dir():
            _diff_trees(repo, old.sha, new.sha, path + "/", out)
        else:
            out.add(path)


def _with_parent_dirs(paths: set[str]) -> list[str]:
    expanded: set[str] = set()
    for path in paths:
        parts = path.split("/")
        for i in range(1, len(parts)):
            expanded.add("/".join(parts[:i]))
        expanded.add(path)
    return sorted(expanded)


def changed_paths_for_commit(repo: Repository, commit: objs.Commit) -> list[str]:
    previous_tree = None
    if commit.parents:
        try:
            parent_type, parent_data = objs.read_object(repo, commit.parents[0])
        except KeyError:
            parent_type, parent_data = "", b""
        if parent_type == "commit":
            previous_tree = objs.parse_commit(parent_data).tree
    changed: set[str] = set()
    _diff_trees(repo, previous_tree, commit.tree, "", changed)
    return _with_parent_dirs(changed)


def bloom_filter_for_paths(paths: list[str]) -> bytes:
    if not paths:
        return b"\0"
    if len(paths) > BLOOM_MAX_CHANGED_PATHS:
        return b"\xff"
    byte_count = max(1, (len(paths) * BLOOM_BITS_PER_ENTRY + 7) // 8)
    bit_count = byte_count * 8
    out = bytearray(byte_count)
    for path in paths:
        data = path.encode("utf-8")
        h0 = _murmur3_x86_32(data, _BLOOM_SEED0)
        h1 = _murmur3_x86_32(data, _BLOOM_SEED1)
        for i in range(BLOOM_NUM_HASHES):
            bit = (h0 + i * h1) % bit_count
            out[bit // 8] |= 1 << (bit % 8)
    return bytes(out)


def bloom_maybe_contains(filter_data: bytes, path: str) -> bool:
    if filter_data == b"\xff":
        return True
    if not filter_data or filter_data == b"\0":
        return False
    bit_count = len(filter_data) * 8
    data = path.encode("utf-8")
    h0 = _murmur3_x86_32(data, _BLOOM_SEED0)
    h1 = _murmur3_x86_32(data, _BLOOM_SEED1)
    for i in range(BLOOM_NUM_HASHES):
        bit = (h0 + i * h1) % bit_count
        if not (filter_data[bit // 8] & (1 << (bit % 8))):
            return False
    return True


def build_commit_graph_bloom_chunks(
    repo: Repository,
    shas: list[str],
    commits: dict[str, objs.Commit],
) -> tuple[bytes, bytes]:
    offsets = bytearray()
    filters = bytearray()
    end = 0
    for sha in shas:
        filter_data = bloom_filter_for_paths(changed_paths_for_commit(repo, commits[sha]))
        filters += filter_data
        end += len(filter_data)
        offsets += struct.pack(">I", end)
    bdat = struct.pack(">III", BLOOM_HASH_VERSION, BLOOM_NUM_HASHES, BLOOM_BITS_PER_ENTRY) + bytes(filters)
    return bytes(offsets), bdat


def read_commit_graph_bloom_filters(bidx: bytes, bdat: bytes, commit_count: int) -> list[bytes]:
    if len(bidx) != commit_count * 4:
        raise ValueError("invalid BIDX chunk length")
    if len(bdat) < 12:
        raise ValueError("invalid BDAT chunk length")
    version, hashes, bits_per_entry = struct.unpack(">III", bdat[:12])
    if (version, hashes, bits_per_entry) != (
        BLOOM_HASH_VERSION,
        BLOOM_NUM_HASHES,
        BLOOM_BITS_PER_ENTRY,
    ):
        raise ValueError("unsupported commit-graph Bloom settings")
    data = bdat[12:]
    filters: list[bytes] = []
    previous = 0
    for i in range(commit_count):
        end = struct.unpack(">I", bidx[i * 4 : i * 4 + 4])[0]
        if end < previous or end > len(data):
            raise ValueError("invalid BIDX offset")
        filters.append(data[previous:end])
        previous = end
    if previous != len(data):
        raise ValueError("unused BDAT filter bytes")
    return filters
