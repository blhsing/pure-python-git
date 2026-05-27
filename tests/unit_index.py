"""Unit tests for pythongit.index — DIRC v2 serialization and conflict stages."""
from __future__ import annotations

from pythongit.index import Index, IndexEntry, read_index, write_index, REG_MODE


def test_empty_index_roundtrip(tmprepo):
    repo, _ = tmprepo
    idx = Index()
    write_index(repo, idx)
    parsed = read_index(repo)
    assert parsed.entries == []


def test_one_entry_roundtrip(tmprepo):
    repo, _ = tmprepo
    idx = Index()
    idx.entries.append(IndexEntry(mode=REG_MODE, sha="a" * 40, path="a.txt"))
    write_index(repo, idx)
    parsed = read_index(repo)
    assert len(parsed.entries) == 1
    e = parsed.entries[0]
    assert e.path == "a.txt"
    assert e.sha == "a" * 40
    assert e.mode == REG_MODE


def test_long_path_uses_overflow_name_encoding(tmprepo):
    """Index entries with paths >= 0xFFF chars must store the full name."""
    repo, _ = tmprepo
    long_path = "a" * 0x1000 + ".txt"
    idx = Index()
    idx.entries.append(IndexEntry(mode=REG_MODE, sha="b" * 40, path=long_path))
    write_index(repo, idx)
    parsed = read_index(repo)
    assert parsed.entries[0].path == long_path


def test_stage_round_trip(tmprepo):
    repo, _ = tmprepo
    idx = Index()
    for stage in (0, 1, 2, 3):
        e = IndexEntry(mode=REG_MODE, sha=str(stage) * 40, path="x")
        e.stage = stage
        idx.entries.append(e)
    write_index(repo, idx)
    parsed = read_index(repo)
    assert len(parsed.entries) == 4
    by_stage = {e.stage: e for e in parsed.entries}
    assert by_stage[0].sha == "0" * 40
    assert by_stage[3].sha == "3" * 40


def test_has_conflicts_detection():
    idx = Index()
    e0 = IndexEntry(mode=REG_MODE, sha="a" * 40, path="x")
    idx.entries.append(e0)
    assert not idx.has_conflicts()
    e2 = IndexEntry(mode=REG_MODE, sha="b" * 40, path="x")
    e2.stage = 2
    idx.entries.append(e2)
    assert idx.has_conflicts()
    assert idx.conflicted_paths() == ["x"]


def test_remove_by_stage():
    idx = Index()
    for stage in (0, 1, 2, 3):
        e = IndexEntry(mode=REG_MODE, sha=str(stage) * 40, path="x")
        e.stage = stage
        idx.entries.append(e)
    idx.remove("x", stage=2)
    assert len(idx.entries) == 3
    assert all(e.stage != 2 for e in idx.entries)


def test_upsert_replaces_same_path_same_stage():
    idx = Index()
    idx.upsert(IndexEntry(mode=REG_MODE, sha="a" * 40, path="x"))
    idx.upsert(IndexEntry(mode=REG_MODE, sha="b" * 40, path="x"))
    assert len(idx.entries) == 1
    assert idx.entries[0].sha == "b" * 40


def test_real_git_reads_our_index_file(tmprepo):
    """Real git ls-files should read pythongit's index correctly."""
    import shutil, subprocess
    if not shutil.which("git"):
        return  # skip silently if git not installed
    repo, _ = tmprepo
    from pythongit import objects as objs
    blob_sha = objs.write_object(repo, "blob", b"hi\n")
    idx = Index()
    idx.entries.append(IndexEntry(mode=REG_MODE, sha=blob_sha, path="hi.txt"))
    write_index(repo, idx)
    r = subprocess.run(["git", "ls-files", "--stage"], cwd=repo.path,
                       capture_output=True, text=True)
    assert r.returncode == 0
    assert "hi.txt" in r.stdout
