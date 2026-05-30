"""Pack file v2 reader with REF_DELTA and OFS_DELTA support.

Pack format:
  header: 'PACK' + version(uint32) + count(uint32)
  objects: variable-length header, zlib-compressed data
    type bits: 1=commit, 2=tree, 3=blob, 4=tag, 6=ofs_delta, 7=ref_delta
  trailer: repository hash over the file.

Index format (.idx v2):
  header: \xff't\x4f\x63 + version(uint32=2)
  fanout: 256 * uint32
  object id list: N * H bytes
  crc32:    N * 4 bytes
  offsets:  N * 4 bytes (high bit = index into large-offset table)
  large offsets: 8-byte each, optional
  trailer.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass
import hashlib
import mmap
import os
import struct
import zlib
from pathlib import Path
from typing import Optional

from .repo import Repository


OBJ_COMMIT = 1
OBJ_TREE = 2
OBJ_BLOB = 3
OBJ_TAG = 4
OBJ_OFS_DELTA = 6
OBJ_REF_DELTA = 7

_TYPE_NAME = {1: "commit", 2: "tree", 3: "blob", 4: "tag"}
_BITMAP_TYPE_ORDER = ("commit", "tree", "blob", "tag")


def _hash_len_for_algo(hash_algo: str) -> int:
    if hash_algo == "sha256":
        return 32
    if hash_algo == "sha1":
        return 20
    raise ValueError(f"unsupported hash algorithm {hash_algo}")


def _hash_bytes_for_algo(hash_algo: str, data: bytes) -> bytes:
    h = hashlib.sha256() if hash_algo == "sha256" else hashlib.sha1()
    h.update(data)
    return h.digest()


def _hash_hex_for_algo(hash_algo: str, data: bytes) -> str:
    return _hash_bytes_for_algo(hash_algo, data).hex()


# ---------------------------------------------------------------------------
# .idx v2 parsing


def _read_idx(idx_path: Path, hash_len: int = 20) -> tuple[list[str], list[int]]:
    raw = idx_path.read_bytes()
    if raw[:4] != b"\xfftOc":
        # idx v1
        if hash_len != 20:
            raise ValueError("idx v1 is only supported for SHA-1")
        fanout = [struct.unpack(">I", raw[i * 4 : i * 4 + 4])[0] for i in range(256)]
        n = fanout[255]
        shas, offsets = [], []
        pos = 1024
        for _ in range(n):
            off = struct.unpack(">I", raw[pos : pos + 4])[0]
            sha = raw[pos + 4 : pos + 24].hex()
            shas.append(sha)
            offsets.append(off)
            pos += 24
        return shas, offsets
    assert struct.unpack(">I", raw[4:8])[0] == 2
    fanout = [struct.unpack(">I", raw[8 + i * 4 : 8 + i * 4 + 4])[0] for i in range(256)]
    n = fanout[255]
    pos = 8 + 256 * 4
    shas = [raw[pos + i * hash_len : pos + i * hash_len + hash_len].hex() for i in range(n)]
    pos += hash_len * n
    pos += 4 * n  # crc32 table
    raw_offs = [struct.unpack(">I", raw[pos + i * 4 : pos + i * 4 + 4])[0] for i in range(n)]
    pos += 4 * n
    offsets = []
    large_off_base = pos
    for o in raw_offs:
        if o & 0x80000000:
            idx = o & 0x7FFFFFFF
            big = struct.unpack(">Q", raw[large_off_base + idx * 8 : large_off_base + idx * 8 + 8])[0]
            offsets.append(big)
        else:
            offsets.append(o)
    return shas, offsets


# ---------------------------------------------------------------------------
# pack data parsing


def _read_var_size(data: bytes, pos: int) -> tuple[int, int, int]:
    """Read the variable-length header of an object. Returns (type, size, new_pos)."""
    b = data[pos]
    pos += 1
    obj_type = (b >> 4) & 0x7
    size = b & 0x0F
    shift = 4
    while b & 0x80:
        b = data[pos]
        pos += 1
        size |= (b & 0x7F) << shift
        shift += 7
    return obj_type, size, pos


def _read_offset(data: bytes, pos: int) -> tuple[int, int]:
    """Read ofs-delta negative offset encoding."""
    b = data[pos]
    pos += 1
    off = b & 0x7F
    while b & 0x80:
        off += 1
        b = data[pos]
        pos += 1
        off = (off << 7) | (b & 0x7F)
    return off, pos


def _read_delta_size(delta: bytes, pos: int) -> tuple[int, int]:
    size = 0
    shift = 0
    while True:
        b = delta[pos]
        pos += 1
        size |= (b & 0x7F) << shift
        shift += 7
        if not (b & 0x80):
            break
    return size, pos


def apply_delta(base: bytes, delta: bytes) -> bytes:
    _src_size, pos = _read_delta_size(delta, 0)
    dst_size, pos = _read_delta_size(delta, pos)
    out = bytearray()
    while pos < len(delta):
        op = delta[pos]
        pos += 1
        if op & 0x80:  # copy
            cp_off = 0
            cp_size = 0
            for i in range(4):
                if op & (1 << i):
                    cp_off |= delta[pos] << (i * 8)
                    pos += 1
            for i in range(3):
                if op & (1 << (4 + i)):
                    cp_size |= delta[pos] << (i * 8)
                    pos += 1
            if cp_size == 0:
                cp_size = 0x10000
            out += base[cp_off : cp_off + cp_size]
        elif op:  # insert
            out += delta[pos : pos + op]
            pos += op
        else:
            raise ValueError("invalid delta opcode 0")
    if len(out) != dst_size:
        raise ValueError("delta size mismatch")
    return bytes(out)


def _decompress_from(data, pos: int) -> bytes:
    decomp = zlib.decompressobj()
    out = bytearray()
    step = 64 * 1024
    cur = pos
    while True:
        chunk = data[cur : cur + step]
        if not chunk:
            raise zlib.error("truncated compressed stream")
        out += decomp.decompress(chunk)
        if decomp.unused_data or decomp.eof:
            break
        cur += len(chunk)
    return bytes(out)


class Pack:
    def __init__(self, pack_path: Path, hash_algo: str = "sha1"):
        self.pack_path = pack_path
        self.idx_path = pack_path.with_suffix(".idx")
        self.hash_algo = hash_algo
        self.hash_len = _hash_len_for_algo(hash_algo)
        self._mm = None
        self._fh = None
        self._shas: Optional[list[str]] = None
        self._offsets: Optional[list[int]] = None

    def _load(self) -> None:
        if self._mm is None:
            self._fh = self.pack_path.open("rb")
            self._mm = mmap.mmap(self._fh.fileno(), 0, access=mmap.ACCESS_READ)
        if self._shas is None:
            self._shas, self._offsets = _read_idx(self.idx_path, self.hash_len)

    def close(self) -> None:
        if self._mm is not None:
            self._mm.close()
            self._mm = None
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    @property
    def shas(self) -> list[str]:
        self._load()
        return self._shas  # type: ignore[return-value]

    def offset_of(self, sha: str) -> Optional[int]:
        self._load()
        i = bisect.bisect_left(self._shas, sha)  # type: ignore[arg-type]
        if i == len(self._shas) or self._shas[i] != sha:  # type: ignore[index,union-attr]
            return None
        return self._offsets[i]  # type: ignore[index]

    def get(self, sha: str) -> Optional[tuple[str, bytes]]:
        off = self.offset_of(sha)
        if off is None:
            return None
        return self._read_at(off)

    def _read_at(self, off: int) -> tuple[str, bytes]:
        self._load()
        data = self._mm  # type: ignore[assignment]
        obj_type, size, pos = _read_var_size(data, off)
        if obj_type in (OBJ_COMMIT, OBJ_TREE, OBJ_BLOB, OBJ_TAG):
            payload = _decompress_from(data, pos)
            return _TYPE_NAME[obj_type], payload
        if obj_type == OBJ_OFS_DELTA:
            neg, pos = _read_offset(data, pos)
            base_off = off - neg
            base_type, base_data = self._read_at(base_off)
            delta = _decompress_from(data, pos)
            return base_type, apply_delta(base_data, delta)
        if obj_type == OBJ_REF_DELTA:
            base_sha = data[pos : pos + self.hash_len].hex()
            pos += self.hash_len
            delta = _decompress_from(data, pos)
            base = self.get(base_sha)
            if base is None:
                # cross-pack lookup
                from .objects import read_object  # local import to avoid cycle
                base = read_object_via_loose_only(self.pack_path.parents[1].parent, base_sha)
                if base is None:
                    raise KeyError(base_sha)
            base_type, base_data = base
            return base_type, apply_delta(base_data, delta)
        raise ValueError(f"unknown object type {obj_type}")


class MultiPackIndex:
    def __init__(
        self,
        pack_dir: Path,
        hash_algo: str,
        pack_names: list[str],
        shas: list[str],
        pack_ids: list[int],
        offsets: list[int],
        revindex: Optional[list[int]] = None,
        bitmapped_packs: Optional[list[tuple[int, int]]] = None,
        checksum: bytes = b"",
    ):
        self.pack_dir = pack_dir
        self.hash_algo = hash_algo
        self.hash_len = _hash_len_for_algo(hash_algo)
        self.pack_names = pack_names
        self.shas = shas
        self.pack_ids = pack_ids
        self.offsets = offsets
        self.revindex = revindex
        self.bitmapped_packs = bitmapped_packs
        self.checksum = checksum
        self._packs: dict[int, Pack] = {}

    def _pack(self, pack_id: int) -> Pack:
        pk = self._packs.get(pack_id)
        if pk is None:
            name = self.pack_names[pack_id]
            pack_name = name[:-4] + ".pack" if name.endswith(".idx") else name
            pk = Pack(self.pack_dir / pack_name, self.hash_algo)
            self._packs[pack_id] = pk
        return pk

    def locate(self, sha: str) -> Optional[tuple[int, int]]:
        i = bisect.bisect_left(self.shas, sha)
        if i == len(self.shas) or self.shas[i] != sha:
            return None
        return self.pack_ids[i], self.offsets[i]

    def get(self, sha: str) -> Optional[tuple[str, bytes]]:
        loc = self.locate(sha)
        if loc is None:
            return None
        pack_id, off = loc
        return self._pack(pack_id)._read_at(off)


def _midx_path(repo: Repository) -> Path:
    return repo.gitdir / "objects" / "pack" / "multi-pack-index"


_MIDX_CACHE: dict[Path, tuple[tuple[int, int], MultiPackIndex]] = {}


def read_midx(repo: Repository) -> Optional[MultiPackIndex]:
    path = _midx_path(repo)
    if not path.exists():
        return None
    try:
        stat = path.stat()
        key = (stat.st_mtime_ns, stat.st_size)
        cached = _MIDX_CACHE.get(path)
        if cached and cached[0] == key:
            return cached[1]
        midx = parse_midx(path)
    except (OSError, ValueError, struct.error, IndexError):
        return None
    _MIDX_CACHE[path] = (key, midx)
    return midx


def parse_midx(path: Path) -> MultiPackIndex:
    raw = path.read_bytes()
    if len(raw) < 12 + 20 or raw[:4] != b"MIDX":
        raise ValueError("not a multi-pack-index")
    version, oid_version, chunk_count, base_count = raw[4], raw[5], raw[6], raw[7]
    if version != 1 or oid_version not in (1, 2) or base_count != 0:
        raise ValueError("unsupported multi-pack-index version")
    hash_algo = "sha256" if oid_version == 2 else "sha1"
    hash_len = _hash_len_for_algo(hash_algo)
    if len(raw) < 12 + hash_len:
        raise ValueError("truncated multi-pack-index")
    if _hash_bytes_for_algo(hash_algo, raw[:-hash_len]) != raw[-hash_len:]:
        raise ValueError("multi-pack-index checksum mismatch")
    pack_count = struct.unpack(">I", raw[8:12])[0]
    toc_pos = 12
    chunks: dict[bytes, tuple[int, int]] = {}
    entries: list[tuple[bytes, int]] = []
    for i in range(chunk_count + 1):
        start = toc_pos + i * 12
        cid = raw[start : start + 4]
        off = struct.unpack(">Q", raw[start + 4 : start + 12])[0]
        entries.append((cid, off))
    for (cid, off), (next_cid, next_off) in zip(entries, entries[1:]):
        if cid == b"\0\0\0\0":
            break
        if off > next_off or next_off > len(raw) - hash_len:
            raise ValueError("invalid multi-pack-index chunk offsets")
        chunks[cid] = (off, next_off)
    if entries[-1][0] != b"\0\0\0\0":
        raise ValueError("multi-pack-index missing terminating chunk")
    for required in (b"PNAM", b"OIDF", b"OIDL", b"OOFF"):
        if required not in chunks:
            raise ValueError(f"multi-pack-index missing {required.decode()} chunk")

    def chunk(cid: bytes) -> bytes:
        start, end = chunks[cid]
        return raw[start:end]

    pnam = chunk(b"PNAM")
    pack_names = [p.decode("utf-8") for p in pnam.rstrip(b"\0").split(b"\0") if p]
    if len(pack_names) != pack_count:
        raise ValueError("multi-pack-index pack count mismatch")
    if pack_names != sorted(pack_names):
        raise ValueError("multi-pack-index pack names are not sorted")

    oidf = chunk(b"OIDF")
    if len(oidf) != 256 * 4:
        raise ValueError("invalid OIDF chunk length")
    fanout = [struct.unpack(">I", oidf[i * 4 : i * 4 + 4])[0] for i in range(256)]
    if fanout != sorted(fanout):
        raise ValueError("non-monotonic OID fanout")
    object_count = fanout[-1]

    oidl = chunk(b"OIDL")
    if len(oidl) != object_count * hash_len:
        raise ValueError("invalid OIDL chunk length")
    shas = [oidl[i * hash_len : i * hash_len + hash_len].hex() for i in range(object_count)]
    if shas != sorted(shas) or len(set(shas)) != len(shas):
        raise ValueError("multi-pack-index OIDs are not sorted and unique")

    ooff = chunk(b"OOFF")
    if len(ooff) != object_count * 8:
        raise ValueError("invalid OOFF chunk length")
    loff = chunk(b"LOFF") if b"LOFF" in chunks else b""
    pack_ids: list[int] = []
    offsets: list[int] = []
    for i in range(object_count):
        row = i * 8
        pack_id, raw_off = struct.unpack(">II", ooff[row : row + 8])
        if pack_id >= pack_count:
            raise ValueError("multi-pack-index pack id out of range")
        if raw_off & 0x80000000:
            large_i = raw_off & 0x7FFFFFFF
            start = large_i * 8
            if start + 8 > len(loff):
                raise ValueError("multi-pack-index large offset out of range")
            off = struct.unpack(">Q", loff[start : start + 8])[0]
        else:
            off = raw_off
        pack_ids.append(pack_id)
        offsets.append(off)
    revindex = None
    if b"RIDX" in chunks:
        ridx = chunk(b"RIDX")
        if len(ridx) != object_count * 4:
            raise ValueError("invalid RIDX chunk length")
        revindex = [struct.unpack(">I", ridx[i * 4 : i * 4 + 4])[0] for i in range(object_count)]
        if sorted(revindex) != list(range(object_count)):
            raise ValueError("invalid RIDX permutation")

    bitmapped_packs = None
    if b"BTMP" in chunks:
        btmp = chunk(b"BTMP")
        if len(btmp) != pack_count * 8:
            raise ValueError("invalid BTMP chunk length")
        bitmapped_packs = [
            struct.unpack(">II", btmp[i * 8 : i * 8 + 8])
            for i in range(pack_count)
        ]
    if (revindex is None) != (bitmapped_packs is None):
        raise ValueError("multi-pack-index bitmap chunks require both RIDX and BTMP")

    checksum = raw[-hash_len:]
    return MultiPackIndex(
        path.parent,
        hash_algo,
        pack_names,
        shas,
        pack_ids,
        offsets,
        revindex,
        bitmapped_packs,
        checksum,
    )


def write_midx(
    pack_dir: Path,
    hash_algo: str = "sha1",
    *,
    write_bitmap: bool = False,
    repo: Optional[Repository] = None,
) -> tuple[bytes, int, int]:
    hash_len = _hash_len_for_algo(hash_algo)
    if write_bitmap and repo is None:
        raise ValueError("repo is required when writing a multi-pack-index bitmap")
    pack_dir.mkdir(parents=True, exist_ok=True)
    idx_paths = [
        p for p in sorted(pack_dir.glob("pack-*.idx"))
        if p.with_suffix(".pack").exists()
    ]
    pack_names = [p.name for p in idx_paths]
    mtimes = [
        max(p.stat().st_mtime_ns, p.with_suffix(".pack").stat().st_mtime_ns)
        for p in idx_paths
    ]

    pack_objects: list[tuple[list[str], list[int]]] = []
    preferred_pack_id: Optional[int] = None
    if write_bitmap and idx_paths:
        nonempty_packs: list[tuple[int, int]] = []
        for pack_id, idx_path in enumerate(idx_paths):
            shas_i, offsets_i = _read_idx(idx_path, hash_len)
            pack_objects.append((shas_i, offsets_i))
            if shas_i:
                nonempty_packs.append((idx_path.with_suffix(".pack").stat().st_mtime_ns, pack_id))
        if nonempty_packs:
            preferred_pack_id = min(nonempty_packs)[1]
    else:
        for idx_path in idx_paths:
            pack_objects.append(_read_idx(idx_path, hash_len))

    selected: dict[str, tuple[int, int, int, bool]] = {}
    for pack_id, idx_path in enumerate(idx_paths):
        shas_i, offsets_i = pack_objects[pack_id]
        mtime = mtimes[pack_id]
        preferred = preferred_pack_id is not None and pack_id == preferred_pack_id
        for sha, off in zip(shas_i, offsets_i):
            prev = selected.get(sha)
            if (
                prev is None
                or (preferred and not prev[3])
                or (preferred == prev[3] and (mtime, pack_id) >= (prev[2], prev[0]))
            ):
                selected[sha] = (pack_id, off, mtime, preferred)

    shas = sorted(selected)
    pnam = bytearray()
    for name in pack_names:
        pnam += name.encode("utf-8") + b"\0"
    while len(pnam) % 4:
        pnam += b"\0"

    fanout = [0] * 256
    for sha in shas:
        fanout[int(sha[:2], 16)] += 1
    oidf = bytearray()
    cum = 0
    for count in fanout:
        cum += count
        oidf += struct.pack(">I", cum)

    oidl = b"".join(bytes.fromhex(sha) for sha in shas)
    large_offsets: list[int] = []
    ooff = bytearray()
    for sha in shas:
        pack_id, off, _mtime, _preferred = selected[sha]
        if off > 0x7FFFFFFF:
            large_i = len(large_offsets)
            large_offsets.append(off)
            encoded_off = 0x80000000 | large_i
        else:
            encoded_off = off
        ooff += struct.pack(">II", pack_id, encoded_off)

    chunks: list[tuple[bytes, bytes]] = [
        (b"PNAM", bytes(pnam)),
        (b"OIDF", bytes(oidf)),
        (b"OIDL", oidl),
        (b"OOFF", bytes(ooff)),
    ]
    if large_offsets:
        chunks.append((b"LOFF", b"".join(struct.pack(">Q", off) for off in large_offsets)))
    pseudo_order: Optional[list[int]] = None
    if write_bitmap and shas:
        pack_ids_for_order = [selected[sha][0] for sha in shas]
        offsets_for_order = [selected[sha][1] for sha in shas]
        pseudo_order, bitmapped_packs = _midx_pseudo_order(
            shas,
            pack_ids_for_order,
            offsets_for_order,
            preferred_pack_id,
            len(pack_names),
        )
        chunks.append((b"RIDX", b"".join(struct.pack(">I", pos) for pos in pseudo_order)))
        chunks.append((b"BTMP", b"".join(struct.pack(">II", start, count) for start, count in bitmapped_packs)))

    oid_version = 2 if hash_algo == "sha256" else 1
    header = b"MIDX" + bytes([1, oid_version, len(chunks), 0]) + struct.pack(">I", len(pack_names))
    toc_size = (len(chunks) + 1) * 12
    cur = len(header) + toc_size
    toc = bytearray()
    for cid, data in chunks:
        toc += cid + struct.pack(">Q", cur)
        cur += len(data)
    toc += b"\0\0\0\0" + struct.pack(">Q", cur)
    body = header + bytes(toc) + b"".join(data for _cid, data in chunks)
    out = body + _hash_bytes_for_algo(hash_algo, body)
    (pack_dir / "multi-pack-index").write_bytes(out)
    if write_bitmap and shas and repo is not None and pseudo_order is not None:
        _write_midx_bitmap(repo, pack_dir, shas, pseudo_order, out[-hash_len:])
    _MIDX_CACHE.pop(pack_dir / "multi-pack-index", None)
    return out, len(pack_names), len(shas)


def verify_midx(pack_dir: Path) -> tuple[int, int]:
    midx = parse_midx(pack_dir / "multi-pack-index")
    for name in midx.pack_names:
        idx = pack_dir / name
        pack = pack_dir / (name[:-4] + ".pack" if name.endswith(".idx") else name)
        if not idx.exists() or not pack.exists():
            raise ValueError(f"missing pack referenced by multi-pack-index: {name}")
    for sha, pack_id, off in zip(midx.shas, midx.pack_ids, midx.offsets):
        obj_type, data = midx._pack(pack_id)._read_at(off)
        actual = _hash_hex_for_algo(midx.hash_algo, f"{obj_type} {len(data)}".encode() + b"\0" + data)
        if actual != sha:
            raise ValueError(f"multi-pack-index object mismatch for {sha}")
    return len(midx.pack_names), len(midx.shas)


def read_object_via_loose_only(gitdir_or_repo_path: Path, sha: str):
    # Helper to break import cycle when resolving REF_DELTA bases across packs.
    obj = gitdir_or_repo_path / "objects" / sha[:2] / sha[2:]
    if obj.exists():
        raw = zlib.decompress(obj.read_bytes())
        nul = raw.index(b"\0")
        header = raw[:nul].decode()
        t, _, _ = header.partition(" ")
        return t, raw[nul + 1 :]
    return None


# ---------------------------------------------------------------------------
# helpers used by objects.read_object


_PACKS_CACHE: dict[Path, tuple[tuple[tuple[str, int, int, int, int], ...], list[Pack]]] = {}


def _pack_dir_signature(pack_dir: Path) -> tuple[tuple[str, int, int, int, int], ...]:
    if not pack_dir.is_dir():
        return tuple()
    sig = []
    for p in sorted(pack_dir.glob("pack-*.pack")):
        idx = p.with_suffix(".idx")
        if not idx.exists():
            continue
        ps = p.stat()
        is_ = idx.stat()
        sig.append((p.name, ps.st_mtime_ns, ps.st_size, is_.st_mtime_ns, is_.st_size))
    return tuple(sig)


def _iter_packs(repo: Repository):
    pack_dir = repo.gitdir / "objects" / "pack"
    sig = _pack_dir_signature(pack_dir)
    if not sig:
        return
    cached = _PACKS_CACHE.get(pack_dir)
    if cached and cached[0] == sig:
        for pk in cached[1]:
            yield pk
        return
    packs = [Pack(pack_dir / row[0], repo.object_format()) for row in sig]
    _PACKS_CACHE[pack_dir] = (sig, packs)
    for pk in packs:
        yield pk


def clear_pack_cache(repo: Repository) -> None:
    pack_dir = repo.gitdir / "objects" / "pack"
    cached = _PACKS_CACHE.pop(pack_dir, None)
    if cached:
        for pk in cached[1]:
            pk.close()
    midx_path = _midx_path(repo)
    cached_midx = _MIDX_CACHE.pop(midx_path, None)
    if cached_midx:
        for pk in cached_midx[1]._packs.values():
            pk.close()


def find_in_packs(repo: Repository, sha: str):
    midx = read_midx(repo)
    if midx is not None:
        try:
            res = midx.get(sha)
            if res is not None:
                return res
        except (OSError, KeyError, ValueError, zlib.error):
            pass
    for pk in _iter_packs(repo):
        res = pk.get(sha)
        if res is not None:
            return res
    return None


def resolve_short(repo: Repository, prefix: str) -> Optional[str]:
    matches = []
    def collect_from_sorted(shas: list[str]) -> Optional[str]:
        i = bisect.bisect_left(shas, prefix)
        while i < len(shas) and shas[i].startswith(prefix):
            matches.append(shas[i])
            if len(matches) > 1:
                return None
            i += 1
        return matches[0] if matches else ""

    midx = read_midx(repo)
    if midx is not None:
        found = collect_from_sorted(midx.shas)
        if found is None:
            return None
        if found:
            return found
    for pk in _iter_packs(repo):
        found = collect_from_sorted(pk.shas)
        if found is None:
            return None
    return matches[0] if len(matches) == 1 else None


# ---------------------------------------------------------------------------
# pack -> loose unpacker (used by clone)


def unpack_pack(repo: Repository, pack_bytes: bytes) -> int:
    """Decompose a pack into loose objects in the repo. Returns object count."""
    from . import objects as objs

    if pack_bytes[:4] != b"PACK":
        raise ValueError("not a pack")
    _, count = struct.unpack(">II", pack_bytes[4:12])
    pos = 12

    by_offset: dict[int, tuple[str, bytes]] = {}
    by_sha: dict[str, tuple[str, bytes]] = {}

    for _i in range(count):
        start = pos
        obj_type, _size, p = _read_var_size(pack_bytes, pos)
        pos = p
        if obj_type in (OBJ_COMMIT, OBJ_TREE, OBJ_BLOB, OBJ_TAG):
            decomp = zlib.decompressobj()
            payload = decomp.decompress(pack_bytes[pos:])
            consumed = len(pack_bytes) - pos - len(decomp.unused_data)
            pos += consumed
            t = _TYPE_NAME[obj_type]
            by_offset[start] = (t, payload)
            sha = objs.write_object(repo, t, payload)
            by_sha[sha] = (t, payload)
        elif obj_type == OBJ_OFS_DELTA:
            neg, p2 = _read_offset(pack_bytes, pos)
            pos = p2
            base_off = start - neg
            decomp = zlib.decompressobj()
            delta = decomp.decompress(pack_bytes[pos:])
            consumed = len(pack_bytes) - pos - len(decomp.unused_data)
            pos += consumed
            base = by_offset[base_off]
            full = apply_delta(base[1], delta)
            by_offset[start] = (base[0], full)
            sha = objs.write_object(repo, base[0], full)
            by_sha[sha] = (base[0], full)
        elif obj_type == OBJ_REF_DELTA:
            base_sha = pack_bytes[pos : pos + repo.hash_len].hex()
            pos += repo.hash_len
            decomp = zlib.decompressobj()
            delta = decomp.decompress(pack_bytes[pos:])
            consumed = len(pack_bytes) - pos - len(decomp.unused_data)
            pos += consumed
            base = by_sha.get(base_sha) or objs.read_object(repo, base_sha)
            full = apply_delta(base[1], delta)
            by_offset[start] = (base[0], full)
            sha = objs.write_object(repo, base[0], full)
            by_sha[sha] = (base[0], full)
        else:
            raise ValueError(f"unknown pack object type {obj_type}")

    return count


# ---------------------------------------------------------------------------
# pack writing and idx v2 generation


def write_idx_v2_from_checksum(
    pack_checksum: bytes,
    shas_offsets: list[tuple[str, int, int]],
    hash_algo: str = "sha1",
) -> bytes:
    """Build a v2 idx using the already-computed pack checksum."""
    shas_offsets = sorted(shas_offsets, key=lambda x: x[0])
    n = len(shas_offsets)
    hash_len = _hash_len_for_algo(hash_algo)
    if len(pack_checksum) != hash_len:
        raise ValueError("pack checksum length does not match hash algorithm")
    buf = bytearray(b"\xfftOc" + struct.pack(">I", 2))
    # fanout
    fanout = [0] * 256
    for sha, _, _ in shas_offsets:
        fanout[int(sha[:2], 16)] += 1
    cum = 0
    for i in range(256):
        cum += fanout[i]
        fanout[i] = cum
    for v in fanout:
        buf += struct.pack(">I", v)
    # sha table
    for sha, _, _ in shas_offsets:
        buf += bytes.fromhex(sha)
    # crc32
    for _, _, crc in shas_offsets:
        buf += struct.pack(">I", crc & 0xFFFFFFFF)
    # offsets (only 31-bit values supported here; assumes packs < 2 GiB)
    for _, off, _ in shas_offsets:
        if off >= 0x80000000:
            raise ValueError("pack >= 2GiB unsupported")
        buf += struct.pack(">I", off)
    # pack sha trailer copy
    buf += pack_checksum
    # idx sha trailer
    idx_sha = _hash_bytes_for_algo(hash_algo, buf)
    buf += idx_sha
    return bytes(buf)


def write_idx_v2(pack_bytes: bytes, shas_offsets: list[tuple[str, int, int]], hash_algo: str = "sha1") -> bytes:
    """Build a v2 idx for the given pack. shas_offsets: list of (sha, offset, crc32)."""
    hash_len = _hash_len_for_algo(hash_algo)
    return write_idx_v2_from_checksum(pack_bytes[-hash_len:], shas_offsets, hash_algo)


def _encode_pack_object_header(obj_type: int, size: int) -> bytes:
    first = ((obj_type & 0x7) << 4) | (size & 0x0F)
    size >>= 4
    hdr = bytearray()
    if size:
        hdr.append(first | 0x80)
        while True:
            b = size & 0x7F
            size >>= 7
            if size:
                hdr.append(b | 0x80)
            else:
                hdr.append(b)
                break
    else:
        hdr.append(first)
    return bytes(hdr)


def write_pack_stream(
    repo: Repository,
    shas: list[str],
    pack_path: Path,
) -> tuple[str, list[tuple[str, int, int]]]:
    """Write a non-delta pack without materializing the whole pack in memory."""
    import binascii

    from . import objects as objs

    type_id = {"commit": 1, "tree": 2, "blob": 3, "tag": 4}
    pack_path = Path(pack_path)
    pack_path.parent.mkdir(parents=True, exist_ok=True)
    entries: list[tuple[str, int, int]] = []
    hasher = repo.new_hash()
    offset = 0

    with pack_path.open("wb") as fh:
        def write(chunk: bytes) -> None:
            nonlocal offset
            fh.write(chunk)
            hasher.update(chunk)
            offset += len(chunk)

        write(b"PACK" + struct.pack(">II", 2, len(shas)))
        for sha in shas:
            obj_type, data = objs.read_object(repo, sha)
            if obj_type not in type_id:
                raise ValueError(f"unsupported object type {obj_type}")
            obj_offset = offset
            record = _encode_pack_object_header(type_id[obj_type], len(data)) + zlib.compress(data)
            write(record)
            entries.append((sha, obj_offset, binascii.crc32(record) & 0xFFFFFFFF))
        checksum = hasher.digest()
        fh.write(checksum)
    return checksum.hex(), entries


@dataclass
class BitmapEntry:
    object_pos: int
    flags: int
    bits: set[int]


@dataclass
class PackBitmap:
    version: int
    flags: int
    pack_checksum: bytes
    type_bitmaps: dict[str, set[int]]
    entries: list[BitmapEntry]


def _pack_order(shas_offsets: list[tuple[str, int, int]]) -> list[str]:
    return [sha for sha, _off, _crc in sorted(shas_offsets, key=lambda item: item[1])]


def _bits_to_words(bits: set[int], bit_count: int) -> list[int]:
    words = [0] * ((bit_count + 63) // 64)
    for bit in bits:
        if 0 <= bit < bit_count:
            words[bit // 64] |= 1 << (bit % 64)
    return words


def _encode_ewah(bits: set[int], bit_count: int) -> bytes:
    """Encode a bitmap in Git's EWAH container using literal words only."""
    literal_words = _bits_to_words(bits, bit_count)
    word_count = 1 + len(literal_words)
    # RLW layout: bit 0 = repeated bit, bits 1..32 = run length,
    # bits 33..63 = number of literal words. We use no run and all literals.
    rlw = len(literal_words) << 33
    out = bytearray(struct.pack(">IIQ", bit_count, word_count, rlw))
    for word in literal_words:
        out += struct.pack(">Q", word)
    out += struct.pack(">I", 0)  # current RLW word index
    return bytes(out)


def _decode_ewah(raw: bytes, pos: int) -> tuple[set[int], int]:
    if pos + 8 > len(raw):
        raise ValueError("truncated EWAH header")
    bit_count, word_count = struct.unpack(">II", raw[pos : pos + 8])
    pos += 8
    byte_count = word_count * 8
    if pos + byte_count + 4 > len(raw):
        raise ValueError("truncated EWAH words")
    words = [struct.unpack(">Q", raw[pos + i * 8 : pos + i * 8 + 8])[0] for i in range(word_count)]
    pos += byte_count
    _rlw_pos = struct.unpack(">I", raw[pos : pos + 4])[0]
    pos += 4

    plain_words: list[int] = []
    i = 0
    while i < len(words):
        rlw = words[i]
        i += 1
        repeated_bit = rlw & 1
        run_len = (rlw >> 1) & 0xFFFFFFFF
        literal_len = (rlw >> 33) & 0x7FFFFFFF
        plain_words.extend([0xFFFFFFFFFFFFFFFF if repeated_bit else 0] * run_len)
        if i + literal_len > len(words):
            raise ValueError("invalid EWAH literal length")
        plain_words.extend(words[i : i + literal_len])
        i += literal_len

    max_words = (bit_count + 63) // 64
    bits: set[int] = set()
    for word_i, word in enumerate(plain_words[:max_words]):
        base = word_i * 64
        while word:
            low = word & -word
            bit = low.bit_length() - 1
            absolute = base + bit
            if absolute < bit_count:
                bits.add(absolute)
            word ^= low
    return bits, pos


def _reachable_for_bitmap(repo: Repository, start: str, allowed: set[str]) -> set[str]:
    from . import objects as objs

    out: set[str] = set()
    seen: set[str] = set()
    stack = [start]
    while stack:
        sha = stack.pop()
        if sha in seen:
            continue
        seen.add(sha)
        if sha in allowed:
            out.add(sha)
        try:
            obj_type, data = objs.read_object(repo, sha)
        except KeyError:
            continue
        if obj_type == "commit":
            c = objs.parse_commit(data)
            stack.append(c.tree)
            stack.extend(c.parents)
        elif obj_type == "tree":
            for entry in objs.parse_tree(data, repo.hash_len):
                stack.append(entry.sha)
        elif obj_type == "tag":
            for line in data.decode("utf-8", errors="replace").splitlines():
                if line.startswith("object "):
                    stack.append(line[len("object "):].strip())
                    break
    return out


def write_pack_bitmap(
    repo: Repository,
    pack_path: Path,
    shas_offsets: list[tuple[str, int, int]],
) -> Optional[bytes]:
    """Write a v1 pack bitmap index next to `pack_path`.

    The reachability entries are intentionally emitted without XOR compression.
    Git accepts that representation and can still use the file; the EWAH
    containers use literal words only.
    """
    from . import objects as objs

    pack_path = Path(pack_path)
    shas = _pack_order(shas_offsets)
    if not shas:
        return None
    object_count = len(shas)
    sha_to_pos = {sha: i for i, sha in enumerate(shas)}
    sha_to_idx_pos = {sha: i for i, sha in enumerate(sorted(shas))}
    allowed = set(shas)

    type_bits = {name: set() for name in _BITMAP_TYPE_ORDER}
    commit_positions: list[int] = []
    for i, sha in enumerate(shas):
        try:
            obj_type, _data = objs.read_object(repo, sha)
        except KeyError:
            continue
        if obj_type in type_bits:
            type_bits[obj_type].add(i)
        if obj_type == "commit":
            commit_positions.append(i)
    if not commit_positions:
        return None

    pack_bytes = pack_path.read_bytes()
    pack_checksum = pack_bytes[-repo.hash_len:]
    flags = 0x1  # BITMAP_OPT_FULL_DAG
    header = b"BITM" + struct.pack(">HHI", 1, flags, len(commit_positions)) + pack_checksum
    body = bytearray(header)
    for obj_type in _BITMAP_TYPE_ORDER:
        body += _encode_ewah(type_bits[obj_type], object_count)

    for object_pos in commit_positions:
        commit_sha = shas[object_pos]
        reachable = _reachable_for_bitmap(repo, commit_sha, allowed)
        bits = {sha_to_pos[sha] for sha in reachable if sha in sha_to_pos}
        body += struct.pack(">IBB", sha_to_idx_pos[commit_sha], 0, 0x1)
        body += _encode_ewah(bits, object_count)

    out = bytes(body) + repo.hash_bytes(bytes(body))
    pack_path.with_suffix(".bitmap").write_bytes(out)
    return out


def parse_pack_bitmap(repo: Repository, bitmap_path: Path) -> PackBitmap:
    raw = Path(bitmap_path).read_bytes()
    if len(raw) < 12 + repo.hash_len * 2 or raw[:4] != b"BITM":
        raise ValueError("not a pack bitmap")
    if repo.hash_bytes(raw[:-repo.hash_len]) != raw[-repo.hash_len:]:
        raise ValueError("pack bitmap checksum mismatch")
    version, flags, entry_count = struct.unpack(">HHI", raw[4:12])
    if version != 1:
        raise ValueError(f"unsupported bitmap version {version}")
    pos = 12
    pack_checksum = raw[pos : pos + repo.hash_len]
    pos += repo.hash_len
    type_bitmaps: dict[str, set[int]] = {}
    for obj_type in _BITMAP_TYPE_ORDER:
        bits, pos = _decode_ewah(raw, pos)
        type_bitmaps[obj_type] = bits
    entries: list[BitmapEntry] = []
    for _ in range(entry_count):
        if pos + 6 > len(raw) - repo.hash_len:
            raise ValueError("truncated bitmap entry")
        object_pos, xor_offset, entry_flags = struct.unpack(">IBB", raw[pos : pos + 6])
        pos += 6
        bits, pos = _decode_ewah(raw, pos)
        if xor_offset:
            base_i = len(entries) - xor_offset
            if base_i < 0:
                raise ValueError("invalid bitmap XOR offset")
            bits = bits ^ entries[base_i].bits
        entries.append(BitmapEntry(object_pos, entry_flags, bits))
    return PackBitmap(version, flags, pack_checksum, type_bitmaps, entries)


def verify_pack_bitmap(repo: Repository, bitmap_path: Path) -> tuple[int, int]:
    bitmap_path = Path(bitmap_path)
    pack_path = bitmap_path.with_suffix(".pack")
    if not pack_path.exists():
        raise ValueError("missing pack for bitmap")
    idx_order, offsets = _read_idx(pack_path.with_suffix(".idx"), repo.hash_len)
    shas_offsets = [(sha, off, 0) for sha, off in zip(idx_order, offsets)]
    pack_order = _pack_order(shas_offsets)
    bitmap = parse_pack_bitmap(repo, bitmap_path)
    pack_checksum = pack_path.read_bytes()[-repo.hash_len:]
    if bitmap.pack_checksum != pack_checksum:
        raise ValueError("bitmap pack checksum mismatch")

    from . import objects as objs

    expected_type_bits = {name: set() for name in _BITMAP_TYPE_ORDER}
    for i, sha in enumerate(pack_order):
        obj_type, _data = objs.read_object(repo, sha)
        if obj_type in expected_type_bits:
            expected_type_bits[obj_type].add(i)
    if bitmap.type_bitmaps != expected_type_bits:
        raise ValueError("bitmap type index mismatch")

    allowed = set(pack_order)
    sha_to_pos = {sha: i for i, sha in enumerate(pack_order)}
    for entry in bitmap.entries:
        if entry.object_pos >= len(idx_order):
            raise ValueError("bitmap commit position out of range")
        commit_sha = idx_order[entry.object_pos]
        obj_type, _data = objs.read_object(repo, commit_sha)
        if obj_type != "commit":
            raise ValueError("bitmap entry does not point at a commit")
        expected = {
            sha_to_pos[sha]
            for sha in _reachable_for_bitmap(repo, commit_sha, allowed)
            if sha in sha_to_pos
        }
        if entry.bits != expected:
            raise ValueError(f"bitmap reachability mismatch for {commit_sha}")
    return len(pack_order), len(bitmap.entries)


def _midx_bitmap_path(pack_dir: Path, checksum: bytes) -> Path:
    return pack_dir / f"multi-pack-index-{checksum.hex()}.bitmap"


def _midx_pseudo_order(
    shas: list[str],
    pack_ids: list[int],
    offsets: list[int],
    preferred_pack_id: Optional[int],
    pack_count: Optional[int] = None,
) -> tuple[list[int], list[tuple[int, int]]]:
    rows = []
    for midx_pos, (pack_id, off) in enumerate(zip(pack_ids, offsets)):
        pack_sort = pack_id
        if preferred_pack_id is not None and pack_id != preferred_pack_id:
            pack_sort |= 0x80000000
        rows.append((pack_sort, off, midx_pos))
    rows.sort()
    pseudo_order = [midx_pos for _pack_sort, _off, midx_pos in rows]

    pack_total = pack_count if pack_count is not None else max(pack_ids, default=-1) + 1
    bitmapped_packs = [(0, 0) for _ in range(pack_total)]
    starts: list[Optional[int]] = [None] * len(bitmapped_packs)
    counts = [0] * len(bitmapped_packs)
    for pseudo_pos, midx_pos in enumerate(pseudo_order):
        pack_id = pack_ids[midx_pos]
        if starts[pack_id] is None:
            starts[pack_id] = pseudo_pos
        counts[pack_id] += 1
    bitmapped_packs = [
        (0 if starts[i] is None else starts[i], counts[i])
        for i in range(len(bitmapped_packs))
    ]
    return pseudo_order, bitmapped_packs


def _write_midx_bitmap(
    repo: Repository,
    pack_dir: Path,
    shas: list[str],
    pseudo_order: list[int],
    midx_checksum: bytes,
) -> Optional[bytes]:
    from . import objects as objs

    object_count = len(shas)
    if object_count == 0:
        return None
    pseudo_shas = [shas[midx_pos] for midx_pos in pseudo_order]
    sha_to_pseudo_pos = {sha: i for i, sha in enumerate(pseudo_shas)}
    sha_to_midx_pos = {sha: i for i, sha in enumerate(shas)}
    allowed = set(shas)

    type_bits = {name: set() for name in _BITMAP_TYPE_ORDER}
    commit_positions: list[int] = []
    for pseudo_pos, sha in enumerate(pseudo_shas):
        try:
            obj_type, _data = objs.read_object(repo, sha)
        except KeyError:
            continue
        if obj_type in type_bits:
            type_bits[obj_type].add(pseudo_pos)
        if obj_type == "commit":
            commit_positions.append(pseudo_pos)
    if not commit_positions:
        return None

    flags = 0x1 | 0x4  # FULL_DAG plus required MIDX hash-cache extension.
    header = b"BITM" + struct.pack(">HHI", 1, flags, len(commit_positions)) + midx_checksum
    body = bytearray(header)
    for obj_type in _BITMAP_TYPE_ORDER:
        body += _encode_ewah(type_bits[obj_type], object_count)

    for pseudo_pos in commit_positions:
        commit_sha = pseudo_shas[pseudo_pos]
        reachable = _reachable_for_bitmap(repo, commit_sha, allowed)
        bits = {sha_to_pseudo_pos[sha] for sha in reachable if sha in sha_to_pseudo_pos}
        body += struct.pack(">IBB", sha_to_midx_pos[commit_sha], 0, 0x1)
        body += _encode_ewah(bits, object_count)

    body += b"\0" * (object_count * 4)  # name-hash cache; zero means unknown.
    out = bytes(body) + repo.hash_bytes(bytes(body))
    for stale in pack_dir.glob("multi-pack-index-*.bitmap"):
        try:
            stale.unlink()
        except OSError:
            pass
    _midx_bitmap_path(pack_dir, midx_checksum).write_bytes(out)
    return out


def verify_midx_bitmap(repo: Repository, pack_dir: Optional[Path] = None) -> tuple[int, int]:
    pack_dir = pack_dir or (repo.gitdir / "objects" / "pack")
    midx = parse_midx(pack_dir / "multi-pack-index")
    if midx.revindex is None or midx.bitmapped_packs is None:
        raise ValueError("multi-pack-index has no bitmap chunks")
    bitmap_path = _midx_bitmap_path(pack_dir, midx.checksum)
    if not bitmap_path.exists():
        raise ValueError("missing multi-pack-index bitmap")
    bitmap = parse_pack_bitmap(repo, bitmap_path)
    if bitmap.pack_checksum != midx.checksum:
        raise ValueError("MIDX bitmap checksum mismatch")

    pseudo_order = midx.revindex
    pseudo_shas = [midx.shas[midx_pos] for midx_pos in pseudo_order]
    sha_to_pseudo_pos = {sha: i for i, sha in enumerate(pseudo_shas)}
    expected_btmp: list[tuple[int, int]] = [(0, 0) for _ in midx.pack_names]
    starts: list[Optional[int]] = [None] * len(midx.pack_names)
    counts = [0] * len(midx.pack_names)
    for pseudo_pos, midx_pos in enumerate(pseudo_order):
        pack_id = midx.pack_ids[midx_pos]
        if starts[pack_id] is None:
            starts[pack_id] = pseudo_pos
        counts[pack_id] += 1
    expected_btmp = [
        (0 if starts[i] is None else starts[i], counts[i])
        for i in range(len(midx.pack_names))
    ]
    if midx.bitmapped_packs != expected_btmp:
        raise ValueError("MIDX BTMP chunk mismatch")

    from . import objects as objs

    expected_type_bits = {name: set() for name in _BITMAP_TYPE_ORDER}
    for pseudo_pos, sha in enumerate(pseudo_shas):
        obj_type, _data = objs.read_object(repo, sha)
        if obj_type in expected_type_bits:
            expected_type_bits[obj_type].add(pseudo_pos)
    if bitmap.type_bitmaps != expected_type_bits:
        raise ValueError("MIDX bitmap type index mismatch")

    allowed = set(midx.shas)
    for entry in bitmap.entries:
        if entry.object_pos >= len(midx.shas):
            raise ValueError("MIDX bitmap commit position out of range")
        commit_sha = midx.shas[entry.object_pos]
        obj_type, _data = objs.read_object(repo, commit_sha)
        if obj_type != "commit":
            raise ValueError("MIDX bitmap entry does not point at a commit")
        expected = {
            sha_to_pseudo_pos[sha]
            for sha in _reachable_for_bitmap(repo, commit_sha, allowed)
            if sha in sha_to_pseudo_pos
        }
        if entry.bits != expected:
            raise ValueError(f"MIDX bitmap reachability mismatch for {commit_sha}")
    return len(midx.shas), len(bitmap.entries)


def _peel_to_bitmap_commit(repo: Repository, sha: str, extras: set[str]) -> Optional[str]:
    from . import objects as objs

    cur = sha
    seen: set[str] = set()
    while cur not in seen:
        seen.add(cur)
        try:
            obj_type, data = objs.read_object(repo, cur)
        except KeyError:
            return None
        if obj_type == "commit":
            return cur
        if obj_type != "tag":
            return None
        extras.add(cur)
        target = None
        for line in data.decode("utf-8", errors="replace").splitlines():
            if line.startswith("object "):
                target = line[len("object "):].strip()
                break
        if not target:
            return None
        cur = target
    return None


def _reachable_from_bitmap_entries(
    repo: Repository,
    starts: list[str],
    entry_bits: dict[str, set[int]],
    bit_order: list[str],
    type_bits: Optional[dict[str, set[int]]] = None,
    object_type: Optional[str] = None,
) -> Optional[set[str]]:
    extras: set[str] = set()
    result_bits: set[int] = set()
    for start in starts:
        commit_sha = _peel_to_bitmap_commit(repo, start, extras)
        if commit_sha is None:
            return None
        bits = entry_bits.get(commit_sha)
        if bits is None:
            return None
        result_bits |= bits
    if object_type is not None:
        if type_bits is None or object_type not in type_bits:
            return None
        result_bits &= type_bits[object_type]
    out = set() if object_type is not None else set(extras)
    for bit in result_bits:
        if 0 <= bit < len(bit_order):
            out.add(bit_order[bit])
    return out


def _reachable_from_midx_bitmap(
    repo: Repository,
    starts: list[str],
    object_type: Optional[str] = None,
) -> Optional[set[str]]:
    midx = read_midx(repo)
    if midx is None or midx.revindex is None:
        return None
    bitmap_path = _midx_bitmap_path(midx.pack_dir, midx.checksum)
    if not bitmap_path.exists():
        return None
    try:
        bitmap = parse_pack_bitmap(repo, bitmap_path)
    except (OSError, ValueError, struct.error, IndexError):
        return None
    if bitmap.pack_checksum != midx.checksum:
        return None
    bit_order = [midx.shas[midx_pos] for midx_pos in midx.revindex]
    entry_bits = {
        midx.shas[entry.object_pos]: entry.bits
        for entry in bitmap.entries
        if entry.object_pos < len(midx.shas)
    }
    return _reachable_from_bitmap_entries(repo, starts, entry_bits, bit_order, bitmap.type_bitmaps, object_type)


def _reachable_from_pack_bitmap(
    repo: Repository,
    starts: list[str],
    pk: Pack,
    object_type: Optional[str] = None,
) -> Optional[set[str]]:
    bitmap_path = pk.pack_path.with_suffix(".bitmap")
    if not bitmap_path.exists():
        return None
    try:
        bitmap = parse_pack_bitmap(repo, bitmap_path)
        pk._load()
    except (OSError, ValueError, struct.error, IndexError):
        return None
    pack_checksum = bytes(pk._mm[-pk.hash_len:])  # type: ignore[index]
    if bitmap.pack_checksum != pack_checksum:
        return None
    idx_order = pk._shas or []
    offsets = pk._offsets or []
    bit_order = [sha for sha, _off in sorted(zip(idx_order, offsets), key=lambda item: item[1])]
    entry_bits = {
        idx_order[entry.object_pos]: entry.bits
        for entry in bitmap.entries
        if entry.object_pos < len(idx_order)
    }
    return _reachable_from_bitmap_entries(repo, starts, entry_bits, bit_order, bitmap.type_bitmaps, object_type)


def reachable_from_bitmaps(
    repo: Repository,
    starts: list[str],
    object_type: Optional[str] = None,
) -> Optional[set[str]]:
    if not starts:
        return set()
    midx_res = _reachable_from_midx_bitmap(repo, starts, object_type)
    if midx_res is not None:
        return midx_res
    for pk in _iter_packs(repo):
        pack_res = _reachable_from_pack_bitmap(repo, starts, pk, object_type)
        if pack_res is not None:
            return pack_res
    return None


def build_pack(repo: Repository, shas: list[str]) -> tuple[bytes, list[tuple[str, int, int]]]:
    """Build a pack with OFS_DELTA compression when a better base exists.

    Strategy mirrors git/builtin/pack-objects.c at a high level: group objects
    by type, sort by size descending within each group, and try to delta each
    object against the previous same-type object in the window. We use a small
    window (5) and only accept the delta if it's at least 50% smaller than the
    raw payload.
    """
    import struct, zlib, binascii
    from . import objects as objs
    type_id = {"commit": 1, "tree": 2, "blob": 3, "tag": 4}
    # load + sort each object
    loaded: list[tuple[str, str, bytes]] = []  # (sha, type, data)
    for sha in shas:
        t, data = objs.read_object(repo, sha)
        loaded.append((sha, t, data))
    # stable sort: type then size desc — keeps similar things adjacent
    loaded.sort(key=lambda x: (x[1], -len(x[2])))

    body = bytearray()
    body += b"PACK" + struct.pack(">II", 2, len(loaded))
    entries: list[tuple[str, int, int]] = []
    # remember offsets for ofs-delta references
    offset_of: dict[str, int] = {}

    WINDOW = 5
    MIN_RATIO = 0.5  # accept delta if delta size <= MIN_RATIO * raw size

    for i, (sha, t, data) in enumerate(loaded):
        offset = len(body)
        offset_of[sha] = offset

        # try to delta against recent same-type objects
        best_delta: Optional[bytes] = None
        best_base_offset: Optional[int] = None
        if i > 0:
            for j in range(i - 1, max(-1, i - 1 - WINDOW), -1):
                base_sha, base_t, base_data = loaded[j]
                if base_t != t:
                    continue
                if len(base_data) == 0:
                    continue
                d = _compute_delta(base_data, data)
                if len(d) <= MIN_RATIO * len(data) and len(d) < (best_delta and len(best_delta) or 10**9):
                    best_delta = d
                    best_base_offset = offset_of[base_sha]

        if best_delta is not None and best_base_offset is not None:
            # emit OFS_DELTA
            ty = OBJ_OFS_DELTA
            size = len(best_delta)
            first = ((ty & 0x7) << 4) | (size & 0x0F)
            size >>= 4
            hdr = bytearray()
            if size:
                hdr.append(first | 0x80)
                while True:
                    b = size & 0x7F
                    size >>= 7
                    if size:
                        hdr.append(b | 0x80)
                    else:
                        hdr.append(b)
                        break
            else:
                hdr.append(first)
            body += bytes(hdr)
            # negative offset encoded as variable-length
            neg = offset - best_base_offset
            ofs_buf = bytearray()
            ofs_buf.append(neg & 0x7F)
            neg >>= 7
            while neg:
                neg -= 1
                ofs_buf.append(0x80 | (neg & 0x7F))
                neg >>= 7
            ofs_buf.reverse()
            body += bytes(ofs_buf)
            body += zlib.compress(best_delta)
        else:
            ty = type_id[t]
            size = len(data)
            first = ((ty & 0x7) << 4) | (size & 0x0F)
            size >>= 4
            hdr = bytearray()
            if size:
                hdr.append(first | 0x80)
                while True:
                    b = size & 0x7F
                    size >>= 7
                    if size:
                        hdr.append(b | 0x80)
                    else:
                        hdr.append(b)
                        break
            else:
                hdr.append(first)
            body += bytes(hdr)
            body += zlib.compress(data)

        crc = binascii.crc32(body[offset:])
        entries.append((sha, offset, crc))

    body += repo.hash_bytes(body)
    return bytes(body), entries


def _encode_size(n: int) -> bytes:
    """Variable-length size encoding used by delta headers."""
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _compute_delta(base: bytes, target: bytes) -> bytes:
    """Compute a git-style delta from `base` to `target`.

    Algorithm: index base by 16-byte rolling hashes (block size 16).
    For each position in target, look up matching base offsets, extend the
    longest match, emit a COPY op for matches >= 4 bytes; otherwise extend
    an INSERT op (max 127 bytes per insert).
    """
    out = bytearray()
    out += _encode_size(len(base))
    out += _encode_size(len(target))

    if not base or not target:
        # all insert
        i = 0
        while i < len(target):
            chunk = target[i : i + 127]
            out.append(len(chunk))
            out += chunk
            i += 127
        return bytes(out)

    # Build index of base 16-byte blocks; map first 16 bytes -> list of offsets.
    BLOCK = 16
    index: dict[bytes, list[int]] = {}
    # Index every byte position so target windows can match anywhere in base.
    # Cap candidates per key to keep this O(n).
    for j in range(len(base) - BLOCK + 1):
        key = bytes(base[j : j + BLOCK])
        bucket = index.setdefault(key, [])
        if len(bucket) < 8:
            bucket.append(j)

    insert_buf = bytearray()

    def flush_insert():
        i = 0
        while i < len(insert_buf):
            chunk = insert_buf[i : i + 127]
            out.append(len(chunk))
            out.extend(chunk)
            i += 127
        insert_buf.clear()

    i = 0
    n = len(target)
    while i < n:
        match_off = -1
        match_len = 0
        if i + BLOCK <= n:
            key = bytes(target[i : i + BLOCK])
            candidates = index.get(key)
            if candidates:
                # extend each candidate; pick longest
                for off in candidates:
                    # check left side already matched (it does because key matched)
                    ext = BLOCK
                    while (off + ext < len(base) and i + ext < n
                           and base[off + ext] == target[i + ext]):
                        ext += 1
                    if ext > match_len:
                        match_len = ext
                        match_off = off
        if match_len >= 4 and match_off >= 0:
            flush_insert()
            cp_off = match_off
            cp_size = match_len
            if cp_size > 0xFFFFFF:
                cp_size = 0xFFFFFF
            op = 0x80
            buf = bytearray()
            for k in range(4):
                b = (cp_off >> (k * 8)) & 0xFF
                if b:
                    op |= 1 << k
                    buf.append(b)
            for k in range(3):
                b = (cp_size >> (k * 8)) & 0xFF
                if b:
                    op |= 1 << (4 + k)
                    buf.append(b)
            out.append(op)
            out += bytes(buf)
            i += match_len
        else:
            insert_buf.append(target[i])
            i += 1
    flush_insert()
    return bytes(out)
