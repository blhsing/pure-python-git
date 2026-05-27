"""Unit tests for pythongit.pack — delta apply, idx v2, build_pack, real-git interop."""
from __future__ import annotations

from pythongit import pack


def test_apply_delta_simple_copy_and_insert():
    base = b"hello world"
    # delta: src_size=11, dst_size=11, copy from base offset=0 len=5, insert "world"
    # var-size encoding: 11 = 0x0B
    delta = bytearray()
    delta.append(11)  # src size
    delta.append(11)  # dst size
    # copy op: 0x80 | 0x01 (offset byte 0) | 0x10 (size byte 0) = 0x91
    # offset = 0 -> no byte
    # offset 0: high bit only is set for size? Actually 0x10 = size bit 0
    # copy op layout: 0x80 | offset_bits[0..3] | size_bits[4..6]
    # copy 5 bytes from offset 0: offset=0 means no offset bytes; size=5
    # op = 0x80 | 0x10 (size bit 0) ; then size byte = 5
    delta.append(0x80 | 0x10)
    delta.append(5)
    # insert " world" (6 bytes)
    delta.append(6)
    delta += b" world"
    out = pack.apply_delta(base, bytes(delta))
    assert out == b"hello world"


def test_apply_delta_roundtrip_via_compute(tmprepo):
    base = b"the quick brown fox jumps over the lazy dog\n"
    target = b"the quick brown fox jumps over the sleeping cat\n"
    delta = pack._compute_delta(base, target)
    out = pack.apply_delta(base, delta)
    assert out == target


def test_compute_delta_shrinks_when_similar():
    base = b"x" * 1000
    target = b"x" * 999 + b"Y"  # differs in last byte
    delta = pack._compute_delta(base, target)
    assert len(delta) < len(target)


def test_build_pack_idx_v2(tmprepo):
    repo, _ = tmprepo
    from pythongit import objects as objs
    shas = [objs.write_object(repo, "blob", f"content {i}\n".encode()) for i in range(5)]
    pack_bytes, entries = pack.build_pack(repo, shas)
    assert pack_bytes[:4] == b"PACK"
    assert len(entries) == 5
    idx_bytes = pack.write_idx_v2(pack_bytes, entries)
    assert idx_bytes[:4] == b"\xfftOc"


def test_build_pack_then_read_back(tmprepo):
    repo, _ = tmprepo
    from pythongit import objects as objs
    import hashlib
    blobs = [objs.write_object(repo, "blob", f"v{i}\n".encode() * 50) for i in range(10)]
    pack_bytes, entries = pack.build_pack(repo, blobs)
    idx_bytes = pack.write_idx_v2(pack_bytes, entries)
    pack_sha = hashlib.sha1(pack_bytes[:-20]).hexdigest()
    pack_dir = repo.gitdir / "objects" / "pack"
    pack_dir.mkdir(parents=True, exist_ok=True)
    (pack_dir / f"pack-{pack_sha}.pack").write_bytes(pack_bytes)
    (pack_dir / f"pack-{pack_sha}.idx").write_bytes(idx_bytes)
    pk = pack.Pack(pack_dir / f"pack-{pack_sha}.pack")
    for sha in blobs:
        t, data = pk.get(sha)
        assert t == "blob"


def test_real_git_verifies_our_pack(tmprepo):
    import shutil, subprocess, hashlib
    if not shutil.which("git"):
        return
    repo, _ = tmprepo
    from pythongit import objects as objs
    # similar blobs to trigger deltas
    chain = "line\n"
    blobs = []
    for i in range(15):
        chain += f"line {i}\n"
        blobs.append(objs.write_object(repo, "blob", chain.encode()))
    pack_bytes, entries = pack.build_pack(repo, blobs)
    idx_bytes = pack.write_idx_v2(pack_bytes, entries)
    pack_sha = hashlib.sha1(pack_bytes[:-20]).hexdigest()
    pack_dir = repo.gitdir / "objects" / "pack"
    pack_dir.mkdir(parents=True, exist_ok=True)
    p = pack_dir / f"pack-{pack_sha}.pack"
    i = pack_dir / f"pack-{pack_sha}.idx"
    p.write_bytes(pack_bytes)
    i.write_bytes(idx_bytes)
    # Git verify-pack must succeed — output format varies across git versions
    # so we only assert on the exit code, not on the summary text.
    r = subprocess.run(["git", "verify-pack", "-v", str(p)], capture_output=True, text=True)
    assert r.returncode == 0, f"stderr={r.stderr!r} stdout={r.stdout!r}"
    # Every blob sha we wrote must appear in the verify output.
    for sha in blobs:
        assert sha in r.stdout, f"sha {sha} missing from verify-pack output"


def test_pack_var_size_roundtrip():
    """Variable-length size encoding used in pack headers."""
    # _read_var_size expects a tagged byte followed by continuation bytes.
    # We reconstruct a tag-3 (blob) header of size 1000.
    # Layout: first byte type<<4 | size&0xF, MSB set if more; then size>>4 chunks.
    ty = 3
    size = 1000
    first = ((ty & 0x7) << 4) | (size & 0x0F)
    rest = size >> 4
    buf = bytearray()
    if rest:
        buf.append(first | 0x80)
        while True:
            b = rest & 0x7F
            rest >>= 7
            if rest:
                buf.append(b | 0x80)
            else:
                buf.append(b)
                break
    else:
        buf.append(first)
    t, sz, _ = pack._read_var_size(bytes(buf), 0)
    assert t == 3
    assert sz == 1000
