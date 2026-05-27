"""Unit tests for pythongit.objects: hash_bytes, write/read, tree encode/decode, commit roundtrip."""
from __future__ import annotations

import pytest

from pythongit import objects as objs


def test_hash_bytes_blob_matches_git_sha():
    # Known git SHA-1 for empty blob
    sha, full = objs.hash_bytes("blob", b"")
    assert sha == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
    assert full == b"blob 0\x00"


def test_hash_bytes_hello_world_blob():
    sha, _ = objs.hash_bytes("blob", b"hello world\n")
    # echo "hello world" | git hash-object --stdin
    assert sha == "3b18e512dba79e4c8300dd08aeb37f8e728b8dad"


def test_write_then_read_roundtrip(tmprepo):
    repo, _ = tmprepo
    sha = objs.write_object(repo, "blob", b"payload\n")
    t, data = objs.read_object(repo, sha)
    assert t == "blob"
    assert data == b"payload\n"


def test_tree_encode_decode_roundtrip():
    entries = [
        objs.TreeEntry("100644", "a.txt", "0" * 40),
        objs.TreeEntry("100755", "run.sh", "1" * 40),
        objs.TreeEntry("40000", "sub", "2" * 40),
    ]
    enc = objs.encode_tree(entries)
    parsed = objs.parse_tree(enc)
    # tree entries sort with dir-suffix rule; check by name
    by_name = {e.name: e for e in parsed}
    assert by_name["a.txt"].sha == "0" * 40
    assert by_name["run.sh"].mode == "100755"
    assert by_name["sub"].is_dir()


def test_tree_sort_directory_before_filename_collision():
    # Tree entries sort byte-wise; a directory's name has an implicit trailing
    # '/' (0x2F), which is less than any letter (e.g. 'f' = 0x66). So "sub/"
    # sorts BEFORE "subfile.txt".
    entries = [
        objs.TreeEntry("40000", "sub", "2" * 40),
        objs.TreeEntry("100644", "subfile.txt", "0" * 40),
    ]
    enc = objs.encode_tree(entries)
    parsed = objs.parse_tree(enc)
    assert [e.name for e in parsed] == ["sub", "subfile.txt"]


def test_commit_encode_parse_roundtrip():
    c = objs.Commit(
        tree="a" * 40,
        parents=["b" * 40, "c" * 40],
        author="Test <t@e.com> 100 +0000",
        committer="Test <t@e.com> 100 +0000",
        message="hello\nbody line\n",
    )
    data = c.encode()
    parsed = objs.parse_commit(data)
    assert parsed.tree == c.tree
    assert parsed.parents == c.parents
    assert parsed.author == c.author
    assert parsed.message == c.message


def test_format_signature_format():
    s = objs.format_signature("X", "x@y", when=1000, tz_minutes=0)
    assert s == "X <x@y> 1000 +0000"
    s = objs.format_signature("X", "x@y", when=0, tz_minutes=-480)
    assert s.endswith(" -0800")


def test_read_object_missing(tmprepo):
    repo, _ = tmprepo
    with pytest.raises(KeyError):
        objs.read_object(repo, "deadbeef" * 5)


def test_gitlink_detection():
    e = objs.TreeEntry("160000", "submod", "a" * 40)
    assert e.is_gitlink()
    assert not e.is_dir()
