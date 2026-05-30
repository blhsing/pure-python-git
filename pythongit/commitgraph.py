"""Reader for Git's binary commit-graph file.

The writer lives in ``cli.py`` because it is exposed as a command, but large
repositories also need the read side for cheap parent/tree lookups during
history walks.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import struct
from typing import Optional

from .repo import Repository


GRAPH_PARENT_NONE = 0x70000000
GRAPH_EXTRA_EDGES = 0x80000000


@dataclass(frozen=True)
class GraphCommit:
    tree: str
    parents: tuple[str, ...]
    generation: int
    commit_time: int
    bloom_filter: Optional[bytes] = None


class CommitGraph:
    def __init__(
        self,
        path: Path,
        hash_algo: str,
        shas: list[str],
        commits: list[GraphCommit],
    ):
        self.path = path
        self.hash_algo = hash_algo
        self.hash_len = 32 if hash_algo == "sha256" else 20
        self.shas = shas
        self.commits = commits
        self._pos = {sha: i for i, sha in enumerate(shas)}

    def get(self, sha: str) -> Optional[GraphCommit]:
        pos = self._pos.get(sha)
        if pos is None:
            return None
        return self.commits[pos]

    def maybe_changed(self, sha: str, path: str) -> bool:
        entry = self.get(sha)
        if entry is None or entry.bloom_filter is None:
            return True
        from . import bloom as bloom_mod

        normalized = path.replace("\\", "/").strip("/")
        return bloom_mod.bloom_maybe_contains(entry.bloom_filter, normalized)


_COMMIT_GRAPH_CACHE: dict[Path, tuple[tuple[int, int], CommitGraph]] = {}


def _hash_bytes(hash_algo: str, data: bytes) -> bytes:
    import hashlib

    h = hashlib.sha256() if hash_algo == "sha256" else hashlib.sha1()
    h.update(data)
    return h.digest()


def _commit_graph_path(repo: Repository) -> Path:
    return repo.gitdir / "objects" / "info" / "commit-graph"


def read_commit_graph(repo: Repository) -> Optional[CommitGraph]:
    path = _commit_graph_path(repo)
    if not path.exists():
        return None
    try:
        stat = path.stat()
        key = (stat.st_mtime_ns, stat.st_size)
        cached = _COMMIT_GRAPH_CACHE.get(path)
        if cached and cached[0] == key:
            return cached[1]
        graph = parse_commit_graph(repo, path)
    except (OSError, ValueError, struct.error, IndexError):
        return None
    _COMMIT_GRAPH_CACHE[path] = (key, graph)
    return graph


def clear_commit_graph_cache(repo: Repository) -> None:
    _COMMIT_GRAPH_CACHE.pop(_commit_graph_path(repo), None)


def parse_commit_graph(repo: Repository, path: Path) -> CommitGraph:
    raw = Path(path).read_bytes()
    if len(raw) < 8 + repo.hash_len or raw[:4] != b"CGPH":
        raise ValueError("not a commit-graph")
    version, hash_version, chunk_count, base_count = raw[4], raw[5], raw[6], raw[7]
    if version != 1 or base_count != 0:
        raise ValueError("unsupported commit-graph version")
    hash_algo = "sha256" if hash_version == 2 else "sha1" if hash_version == 1 else ""
    if not hash_algo or hash_algo != repo.object_format():
        raise ValueError("commit-graph hash version mismatch")
    hash_len = repo.hash_len
    if _hash_bytes(hash_algo, raw[:-hash_len]) != raw[-hash_len:]:
        raise ValueError("commit-graph checksum mismatch")

    toc_pos = 8
    entries: list[tuple[bytes, int]] = []
    for i in range(chunk_count + 1):
        start = toc_pos + i * 12
        cid = raw[start : start + 4]
        off = struct.unpack(">Q", raw[start + 4 : start + 12])[0]
        entries.append((cid, off))
    if not entries or entries[-1][0] != b"\0\0\0\0":
        raise ValueError("commit-graph missing terminating chunk")

    chunks: dict[bytes, bytes] = {}
    for (cid, off), (_next_cid, next_off) in zip(entries, entries[1:]):
        if cid == b"\0\0\0\0":
            break
        if off > next_off or next_off > len(raw) - hash_len:
            raise ValueError("invalid commit-graph chunk offsets")
        chunks[cid] = raw[off:next_off]
    for required in (b"OIDF", b"OIDL", b"CDAT"):
        if required not in chunks:
            raise ValueError(f"commit-graph missing {required.decode()} chunk")

    oidf = chunks[b"OIDF"]
    if len(oidf) != 256 * 4:
        raise ValueError("invalid OIDF chunk length")
    fanout = [struct.unpack(">I", oidf[i * 4 : i * 4 + 4])[0] for i in range(256)]
    if fanout != sorted(fanout):
        raise ValueError("non-monotonic OID fanout")
    commit_count = fanout[-1]

    oidl = chunks[b"OIDL"]
    if len(oidl) != commit_count * hash_len:
        raise ValueError("invalid OIDL chunk length")
    shas = [oidl[i * hash_len : i * hash_len + hash_len].hex() for i in range(commit_count)]
    if shas != sorted(shas) or len(set(shas)) != len(shas):
        raise ValueError("commit-graph OIDs are not sorted and unique")

    cdat = chunks[b"CDAT"]
    row_len = hash_len + 16
    if len(cdat) != commit_count * row_len:
        raise ValueError("invalid CDAT chunk length")
    edge = chunks.get(b"EDGE", b"")

    filters: list[Optional[bytes]] = [None] * commit_count
    if (b"BIDX" in chunks) != (b"BDAT" in chunks):
        raise ValueError("commit-graph Bloom chunks must include both BIDX and BDAT")
    if b"BIDX" in chunks:
        from . import bloom as bloom_mod

        parsed = bloom_mod.read_commit_graph_bloom_filters(chunks[b"BIDX"], chunks[b"BDAT"], commit_count)
        filters = list(parsed)

    commits: list[GraphCommit] = []
    for i in range(commit_count):
        row = i * row_len
        tree = cdat[row : row + hash_len].hex()
        p1, p2, top, bot = struct.unpack(">IIII", cdat[row + hash_len : row + hash_len + 16])
        parents: list[str] = []
        if p1 != GRAPH_PARENT_NONE:
            if p1 >= commit_count:
                raise ValueError("commit-graph parent position out of range")
            parents.append(shas[p1])
        if p2 == GRAPH_PARENT_NONE:
            pass
        elif p2 & GRAPH_EXTRA_EDGES:
            edge_pos = (p2 & ~GRAPH_EXTRA_EDGES) * 4
            while True:
                if edge_pos + 4 > len(edge):
                    raise ValueError("commit-graph extra edge out of range")
                raw_parent = struct.unpack(">I", edge[edge_pos : edge_pos + 4])[0]
                parent_pos = raw_parent & ~GRAPH_EXTRA_EDGES
                if parent_pos >= commit_count:
                    raise ValueError("commit-graph extra parent position out of range")
                parents.append(shas[parent_pos])
                edge_pos += 4
                if raw_parent & GRAPH_EXTRA_EDGES:
                    break
        else:
            if p2 >= commit_count:
                raise ValueError("commit-graph second parent position out of range")
            parents.append(shas[p2])
        generation = top >> 2
        commit_time = ((top & 0x3) << 32) | bot
        commits.append(GraphCommit(tree, tuple(parents), generation, commit_time, filters[i]))

    return CommitGraph(Path(path), hash_algo, shas, commits)
