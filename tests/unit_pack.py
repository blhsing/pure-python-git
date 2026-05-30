"""Unit tests for pythongit.pack — delta apply, idx v2, build_pack, real-git interop."""
from __future__ import annotations

from pythongit import pack


def _write_pack(repo, shas):
    pack_bytes, entries = pack.build_pack(repo, shas)
    idx_bytes = pack.write_idx_v2(pack_bytes, entries, repo.object_format())
    pack_sha = repo.hash_hex(pack_bytes[:-repo.hash_len])
    pack_dir = repo.gitdir / "objects" / "pack"
    pack_dir.mkdir(parents=True, exist_ok=True)
    (pack_dir / f"pack-{pack_sha}.pack").write_bytes(pack_bytes)
    (pack_dir / f"pack-{pack_sha}.idx").write_bytes(idx_bytes)
    return pack_dir / f"pack-{pack_sha}.pack"


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


def test_streaming_pack_writer_keeps_bounded_delta_compression(tmprepo):
    repo, _ = tmprepo
    from pythongit import objects as objs
    blobs = []
    text = "base\n"
    for i in range(12):
        text += f"line {i}\n"
        blobs.append(objs.write_object(repo, "blob", text.encode()))
    pack_dir = repo.gitdir / "objects" / "pack"
    pack_path = pack_dir / "pack-stream.pack"
    _pack_sha, entries = pack.write_pack_stream(repo, blobs, pack_path, batch_size=4, window=4)
    idx_bytes = pack.write_idx_v2_from_checksum(pack_path.read_bytes()[-repo.hash_len:], entries, repo.object_format())
    pack_path.with_suffix(".idx").write_bytes(idx_bytes)

    raw = pack_path.read_bytes()
    assert any(pack._read_var_size(raw, off)[0] == pack.OBJ_OFS_DELTA for _sha, off, _crc in entries)
    pk = pack.Pack(pack_path, repo.object_format())
    try:
        for sha in blobs:
            assert pk.get(sha)[0] == "blob"
    finally:
        pk.close()


def test_install_pack_file_indexes_incoming_pack_without_loose_unpack(tmprepo, tmp_path):
    from pythongit import objects as objs
    from pythongit.repo import Repository

    src, _ = tmprepo
    dest = Repository.init(tmp_path / "dest")
    blobs = [objs.write_object(src, "blob", f"incoming {i}\n".encode()) for i in range(8)]
    raw, _entries = pack.build_pack(src, blobs)
    incoming = tmp_path / "incoming.pack"
    incoming.write_bytes(raw)

    pack_sha, pack_path, count = pack.install_pack_file(dest, incoming)

    assert count == len(blobs)
    assert pack_path.exists()
    assert pack_path.with_suffix(".idx").exists()
    assert pack_path.name == f"pack-{pack_sha}.pack"
    for sha in blobs:
        assert objs.read_object(dest, sha)[0] == "blob"
        assert not (dest.gitdir / "objects" / sha[:2] / sha[2:]).exists()


def test_install_pack_file_fixes_thin_ref_delta_pack(tmprepo, tmp_path):
    import struct
    import zlib
    from pythongit import objects as objs

    repo, _ = tmprepo
    base_data = b"hello\n"
    target_data = b"hello\nworld\n"
    base_sha = objs.write_object(repo, "blob", base_data)
    target_sha, _serialized = objs.hash_bytes("blob", target_data, repo)
    delta = pack._compute_delta(base_data, target_data)
    record = (
        pack._encode_pack_object_header(pack.OBJ_REF_DELTA, len(delta))
        + bytes.fromhex(base_sha)
        + zlib.compress(delta)
    )
    body = b"PACK" + struct.pack(">II", 2, 1) + record
    raw = body + repo.hash_bytes(body)
    incoming = tmp_path / "thin.pack"
    incoming.write_bytes(raw)

    _pack_sha, _pack_path, count = pack.install_pack_file(repo, incoming)
    (repo.gitdir / "objects" / base_sha[:2] / base_sha[2:]).unlink()
    pack.clear_pack_cache(repo)

    assert count == 2
    assert objs.read_object(repo, base_sha) == ("blob", base_data)
    assert objs.read_object(repo, target_sha) == ("blob", target_data)


def test_real_git_verifies_our_pack(tmprepo):
    import subprocess, hashlib
    from conftest import real_git
    gitbin = real_git()
    if not gitbin:
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
    r = subprocess.run([gitbin, "verify-pack", "-v", str(p)], capture_output=True, text=True)
    assert r.returncode == 0, f"stderr={r.stderr!r} stdout={r.stdout!r}"
    # Every blob sha we wrote must appear in the verify output.
    for sha in blobs:
        assert sha in r.stdout, f"sha {sha} missing from verify-pack output"


def test_pack_bitmap_written_and_real_git_uses_it(tmprepo):
    import subprocess
    from conftest import real_git
    from tests.conftest import commit_one
    from pythongit import cli

    repo, path = tmprepo
    for i in range(4):
        commit_one(repo, "a.txt", f"v{i}\n", f"c{i}")

    assert cli.main(["pack-objects", "pack", "--all"]) == 0
    pack_dir = repo.gitdir / "objects" / "pack"
    bitmaps = list(pack_dir.glob("pack-*.bitmap"))
    assert len(bitmaps) == 1
    objects, commits = pack.verify_pack_bitmap(repo, bitmaps[0])
    assert objects >= 4
    assert commits == 4

    gitbin = real_git()
    if not gitbin:
        return
    r = subprocess.run([gitbin, "rev-list", "--test-bitmap", "HEAD"],
                       cwd=path, capture_output=True, text=True)
    assert r.returncode == 0, f"stderr={r.stderr!r} stdout={r.stdout!r}"


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


def test_multi_pack_index_binary_lookup(tmprepo):
    repo, _ = tmprepo
    from pythongit import objects as objs
    blobs1 = [objs.write_object(repo, "blob", f"a{i}\n".encode()) for i in range(3)]
    blobs2 = [objs.write_object(repo, "blob", f"b{i}\n".encode()) for i in range(3)]
    _write_pack(repo, blobs1)
    _write_pack(repo, blobs2)
    pack_dir = repo.gitdir / "objects" / "pack"

    data, pack_count, object_count = pack.write_midx(pack_dir)

    assert data[:4] == b"MIDX"
    assert pack_count == 2
    assert object_count == 6
    assert pack.verify_midx(pack_dir) == (2, 6)
    midx = pack.read_midx(repo)
    assert midx is not None
    assert midx.get(blobs2[1]) == ("blob", b"b1\n")


def test_real_git_verifies_our_multi_pack_index(tmprepo):
    import subprocess
    from conftest import real_git
    gitbin = real_git()
    if not gitbin:
        return
    repo, _ = tmprepo
    from pythongit import objects as objs
    blobs1 = [objs.write_object(repo, "blob", f"x{i}\n".encode()) for i in range(2)]
    blobs2 = [objs.write_object(repo, "blob", f"y{i}\n".encode()) for i in range(2)]
    _write_pack(repo, blobs1)
    _write_pack(repo, blobs2)
    pack.write_midx(repo.gitdir / "objects" / "pack")

    r = subprocess.run([gitbin, "multi-pack-index", "verify"],
                       cwd=repo.path, capture_output=True, text=True)
    assert r.returncode == 0, f"stderr={r.stderr!r} stdout={r.stdout!r}"


def test_multi_pack_index_bitmap_interop(tmprepo):
    import subprocess
    from conftest import real_git
    from tests.conftest import commit_one
    from pythongit import cli as cli_mod
    from pythongit import refs

    repo, path = tmprepo
    packed: set[str] = set()
    for i in range(3):
        commit_one(repo, f"f{i}.txt", f"v{i}\n", f"c{i}")
        reachable = cli_mod._reachable(repo)
        new_objects = sorted(reachable - packed)
        assert new_objects
        _write_pack(repo, new_objects)
        packed.update(new_objects)

    pack_dir = repo.gitdir / "objects" / "pack"
    expected_reachable = cli_mod._reachable(repo)
    assert cli_mod.main(["multi-pack-index", "write", "--bitmap"]) == 0
    midx = pack.parse_midx(pack_dir / "multi-pack-index")
    assert len(midx.pack_names) == 3
    assert len(midx.shas) == len(packed)
    assert midx.revindex is not None
    assert midx.bitmapped_packs is not None
    bitmaps = list(pack_dir.glob("multi-pack-index-*.bitmap"))
    assert len(bitmaps) == 1
    assert bitmaps[0].name == f"multi-pack-index-{midx.checksum.hex()}.bitmap"
    assert pack.verify_midx(pack_dir) == (3, len(packed))
    assert pack.verify_midx_bitmap(repo, pack_dir) == (len(packed), 3)
    head = refs.rev_parse(repo, "HEAD")
    assert head is not None
    assert pack.reachable_from_bitmaps(repo, [head]) == expected_reachable

    gitbin = real_git()
    if not gitbin:
        return
    r = subprocess.run([gitbin, "multi-pack-index", "verify"],
                       cwd=path, capture_output=True, text=True)
    assert r.returncode == 0, f"stderr={r.stderr!r} stdout={r.stdout!r}"
    r = subprocess.run([gitbin, "rev-list", "--test-bitmap", "HEAD"],
                       cwd=path, capture_output=True, text=True)
    assert r.returncode == 0, f"stderr={r.stderr!r} stdout={r.stdout!r}"


def test_sha256_pack_and_midx_interop(tmp_path):
    import subprocess
    from conftest import real_git
    from pythongit import objects as objs
    from pythongit.repo import Repository
    gitbin = real_git()
    if not gitbin:
        return
    repo = Repository.init(tmp_path, object_format="sha256")
    blobs1 = [objs.write_object(repo, "blob", f"sx{i}\n".encode()) for i in range(2)]
    blobs2 = [objs.write_object(repo, "blob", f"sy{i}\n".encode()) for i in range(2)]
    p1 = _write_pack(repo, blobs1)
    _write_pack(repo, blobs2)
    pack_dir = repo.gitdir / "objects" / "pack"

    r = subprocess.run([gitbin, "verify-pack", "-v", str(p1)],
                       cwd=repo.path, capture_output=True, text=True)
    assert r.returncode == 0, f"stderr={r.stderr!r} stdout={r.stdout!r}"
    data, packs, objects = pack.write_midx(pack_dir, repo.object_format())
    assert data[5] == 2
    assert packs == 2
    assert objects == 4
    assert pack.verify_midx(pack_dir) == (2, 4)
    r = subprocess.run([gitbin, "-C", str(repo.path), "multi-pack-index", "verify"],
                       capture_output=True, text=True)
    assert r.returncode == 0, f"stderr={r.stderr!r} stdout={r.stdout!r}"
