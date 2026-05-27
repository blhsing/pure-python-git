"""Pack file v2 reader with REF_DELTA and OFS_DELTA support.

Pack format:
  header: 'PACK' + version(uint32) + count(uint32)
  objects: variable-length header, zlib-compressed data
    type bits: 1=commit, 2=tree, 3=blob, 4=tag, 6=ofs_delta, 7=ref_delta
  trailer: sha1 over the file.

Index format (.idx v2):
  header: \xff't\x4f\x63 + version(uint32=2)
  fanout: 256 * uint32
  sha1 list: N * 20 bytes
  crc32:    N * 4 bytes
  offsets:  N * 4 bytes (high bit = index into large-offset table)
  large offsets: 8-byte each, optional
  trailer.
"""
from __future__ import annotations

import hashlib
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


# ---------------------------------------------------------------------------
# .idx v2 parsing


def _read_idx(idx_path: Path) -> tuple[list[str], list[int]]:
    raw = idx_path.read_bytes()
    if raw[:4] != b"\xfftOc":
        # idx v1
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
    shas = [raw[pos + i * 20 : pos + i * 20 + 20].hex() for i in range(n)]
    pos += 20 * n
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


class Pack:
    def __init__(self, pack_path: Path):
        self.pack_path = pack_path
        self.idx_path = pack_path.with_suffix(".idx")
        self._mm: Optional[bytes] = None
        self._shas: Optional[list[str]] = None
        self._offsets: Optional[list[int]] = None

    def _load(self) -> None:
        if self._mm is None:
            self._mm = self.pack_path.read_bytes()
        if self._shas is None:
            self._shas, self._offsets = _read_idx(self.idx_path)

    @property
    def shas(self) -> list[str]:
        self._load()
        return self._shas  # type: ignore[return-value]

    def offset_of(self, sha: str) -> Optional[int]:
        self._load()
        try:
            i = self._shas.index(sha)  # type: ignore[union-attr]
        except ValueError:
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
            decomp = zlib.decompressobj()
            payload = decomp.decompress(data[pos:])
            return _TYPE_NAME[obj_type], payload
        if obj_type == OBJ_OFS_DELTA:
            neg, pos = _read_offset(data, pos)
            base_off = off - neg
            base_type, base_data = self._read_at(base_off)
            decomp = zlib.decompressobj()
            delta = decomp.decompress(data[pos:])
            return base_type, apply_delta(base_data, delta)
        if obj_type == OBJ_REF_DELTA:
            base_sha = data[pos : pos + 20].hex()
            pos += 20
            decomp = zlib.decompressobj()
            delta = decomp.decompress(data[pos:])
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


def _iter_packs(repo: Repository):
    pack_dir = repo.gitdir / "objects" / "pack"
    if not pack_dir.is_dir():
        return
    for p in pack_dir.glob("pack-*.pack"):
        yield Pack(p)


def find_in_packs(repo: Repository, sha: str):
    for pk in _iter_packs(repo):
        res = pk.get(sha)
        if res is not None:
            return res
    return None


def resolve_short(repo: Repository, prefix: str) -> Optional[str]:
    matches = []
    for pk in _iter_packs(repo):
        for s in pk.shas:
            if s.startswith(prefix):
                matches.append(s)
                if len(matches) > 1:
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
            base_sha = pack_bytes[pos : pos + 20].hex()
            pos += 20
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


def write_idx_v2(pack_bytes: bytes, shas_offsets: list[tuple[str, int, int]]) -> bytes:
    """Build a v2 idx for the given pack. shas_offsets: list of (sha, offset, crc32)."""
    import hashlib, struct
    shas_offsets = sorted(shas_offsets, key=lambda x: x[0])
    n = len(shas_offsets)
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
    pack_sha = pack_bytes[-20:]
    buf += pack_sha
    # idx sha trailer
    idx_sha = hashlib.sha1(buf).digest()
    buf += idx_sha
    return bytes(buf)


def build_pack(repo: Repository, shas: list[str]) -> tuple[bytes, list[tuple[str, int, int]]]:
    """Build a pack with OFS_DELTA compression when a better base exists.

    Strategy mirrors git/builtin/pack-objects.c at a high level: group objects
    by type, sort by size descending within each group, and try to delta each
    object against the previous same-type object in the window. We use a small
    window (5) and only accept the delta if it's at least 50% smaller than the
    raw payload.
    """
    import hashlib, struct, zlib, binascii
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

    body += hashlib.sha1(body).digest()
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
