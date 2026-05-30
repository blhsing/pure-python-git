"""Byte-for-byte parity tests for the pure-Python ort merge engine.

Each test builds three commits (base, side1, side2) and asserts that
``pythongit.ort.merge_tree`` produces exactly the same result tree oid and
conflicted index stages as a real ``git merge-tree --write-tree -z``.

All tests are skipped automatically when no real git binary is available.
"""
from __future__ import annotations

import subprocess

import pytest

from tests.conftest import real_git
from pythongit import ort
from pythongit.repo import Repository

GIT = real_git()
pytestmark = pytest.mark.skipif(not GIT, reason="needs a real git binary")


def _g(d, *args, **kw):
    return subprocess.run([GIT, "-C", str(d), *args], capture_output=True, **kw)


def _commit(d, files: dict):
    _g(d, "read-tree", "--empty")
    # clear worktree tracked files first
    for p in list((d).glob("**/*")):
        pass
    # write files
    import os
    for path, content in files.items():
        fp = d / path
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_bytes(content)
    _g(d, "add", "-A")
    tree = _g(d, "write-tree").stdout.decode().strip()
    sha = _g(d, "commit-tree", tree, input=b"m").stdout.decode().strip()
    # reset worktree to clean for next commit
    for path in list(files):
        fp = d / path
        if fp.exists():
            fp.unlink()
    return sha


def _git_merge_tree(d, base, s1, s2):
    p = _g(d, "merge-tree", "--write-tree", "--no-messages", "-z",
           "--merge-base", base, s1, s2)
    out = p.stdout
    parts = out.split(b"\0")
    if parts and parts[-1] == b"":
        parts.pop()
    tree = parts[0].decode()
    stages = []
    for rec in parts[1:]:
        if not rec:
            continue
        meta, _, path = rec.partition(b"\t")
        mode_s, oid, stage_s = meta.decode().split()
        stages.append((path.decode(), int(stage_s), int(mode_s, 8), oid))
    return tree, sorted(stages, key=lambda t: (t[0], t[1]))


def _py_merge_tree(repo, base, s1, s2):
    res = ort.merge_tree(repo, base, s1, s2)
    stages = []
    if res.conflict_index is not None:
        for e in res.conflict_index.entries:
            if e.stage:
                stages.append((e.path, e.stage, e.mode, e.sha))
    return res.tree, sorted(stages, key=lambda t: (t[0], t[1]))


def _check(tmprepo, base_files, s1_files, s2_files):
    repo, d = tmprepo
    base = _commit(d, base_files)
    s1 = _commit(d, s1_files)
    s2 = _commit(d, s2_files)
    g_tree, g_stages = _git_merge_tree(d, base, s1, s2)
    p_tree, p_stages = _py_merge_tree(repo, base, s1, s2)
    assert p_tree == g_tree, f"tree mismatch\ngit={g_tree}\npy ={p_tree}"
    assert p_stages == g_stages, f"stage mismatch\ngit={g_stages}\npy ={p_stages}"


def test_clean_three_way(tmprepo):
    _check(tmprepo,
           {"a": b"1\n"},
           {"a": b"1\n", "b": b"x\n"},
           {"a": b"1\n", "c": b"y\n"})


def test_content_conflict(tmprepo):
    _check(tmprepo,
           {"f": b"base\n"},
           {"f": b"ours\n"},
           {"f": b"theirs\n"})


def test_overlapping_clean_hunks(tmprepo):
    base = b"a\nb\nc\nd\ne\n"
    _check(tmprepo,
           {"f": base},
           {"f": b"A\nb\nc\nd\ne\n"},
           {"f": b"a\nb\nc\nd\nE\n"})


def test_no_trailing_newline(tmprepo):
    _check(tmprepo,
           {"f": b"a\nb\nc"},
           {"f": b"a\nB\nc"},
           {"f": b"a\nb\nc\nd"})


def test_modify_delete(tmprepo):
    _check(tmprepo,
           {"f": b"base\n"},
           {"f": b"modified\n"},
           {})


def test_add_add_conflict(tmprepo):
    _check(tmprepo,
           {},
           {"f": b"ours\n"},
           {"f": b"theirs\n"})


def test_rename_modify(tmprepo):
    _check(tmprepo,
           {"old.txt": b"line1\nline2\nline3\n"},
           {"old.txt": b"line1\nLINE2\nline3\n"},
           {"new.txt": b"line1\nline2\nline3\n"})


def test_rename_rename_1to2(tmprepo):
    _check(tmprepo,
           {"orig": b"a\nb\nc\nd\n"},
           {"one": b"a\nb\nc\nd\n"},
           {"two": b"a\nb\nc\nd\n"})


def test_rename_both_sides_same(tmprepo):
    _check(tmprepo,
           {"orig": b"a\nb\nc\nd\n"},
           {"renamed": b"a\nb\nc\nd\n"},
           {"renamed": b"a\nb\nc\nd\nE\n"})


def test_mode_change(tmprepo):
    repo, d = tmprepo
    # exec bit change on one side
    _check(tmprepo,
           {"s.sh": b"#!/bin/sh\necho hi\n"},
           {"s.sh": b"#!/bin/sh\necho hi\nmore\n"},
           {"s.sh": b"#!/bin/sh\necho hi\n"})


def test_directory_rename_add(tmprepo):
    _check(tmprepo,
           {"dir/a": b"a\n", "dir/b": b"b\n"},
           {"newdir/a": b"a\n", "newdir/b": b"b\n"},
           {"dir/a": b"a\n", "dir/b": b"b\n", "dir/c": b"c\n"})


def test_directory_rename_with_modify(tmprepo):
    _check(tmprepo,
           {"src/x": b"1\n2\n3\n", "src/y": b"y\n"},
           {"lib/x": b"1\n2\n3\n", "lib/y": b"y\n"},
           {"src/x": b"1\nTWO\n3\n", "src/y": b"y\n", "src/z": b"z\n"})


def test_nested_subdirs_clean(tmprepo):
    _check(tmprepo,
           {"a/b/c/f": b"hi\n", "a/b/g": b"g\n", "top": b"t\n"},
           {"a/b/c/f": b"HI\n", "a/b/g": b"g\n", "top": b"t\n"},
           {"a/b/c/f": b"hi\n", "a/b/g": b"G\n", "top": b"t\n", "a/new": b"n\n"})


def test_delete_delete(tmprepo):
    _check(tmprepo,
           {"f": b"x\n", "g": b"y\n"},
           {"g": b"y\n"},
           {"g": b"y\n", "h": b"z\n"})


def test_rename_rename_2to1(tmprepo):
    _check(tmprepo,
           {"a": b"aaa\nbbb\nccc\n", "b": b"xxx\nyyy\nzzz\n"},
           {"c": b"aaa\nbbb\nccc\n", "b": b"xxx\nyyy\nzzz\n"},
           {"a": b"aaa\nbbb\nccc\n", "c": b"xxx\nyyy\nzzz\n"})


def test_rename_delete(tmprepo):
    _check(tmprepo,
           {"a": b"l1\nl2\nl3\n"},
           {"b": b"l1\nL2\nl3\n"},
           {})


def test_type_change_file_symlink(tmprepo):
    # built via low-level cacheinfo so it works on Windows worktrees
    repo, d = tmprepo
    import subprocess

    def blob(content):
        return subprocess.run([GIT, "-C", str(d), "hash-object", "-w", "--stdin"],
                              input=content, capture_output=True).stdout.decode().strip()

    def tree_commit(entries):
        _g(d, "read-tree", "--empty")
        for mode, sha, path in entries:
            _g(d, "update-index", "--add", "--cacheinfo", f"{mode},{sha},{path}")
        t = _g(d, "write-tree").stdout.decode().strip()
        return _g(d, "commit-tree", t, input=b"m").stdout.decode().strip()

    fbase = blob(b"line1\nline2\n")
    fmod = blob(b"line1\nLINE2\n")
    link = blob(b"target-a")
    base = tree_commit([("100644", fbase, "item")])
    s1 = tree_commit([("120000", link, "item")])
    s2 = tree_commit([("100644", fmod, "item")])
    g_tree, g_stages = _git_merge_tree(d, base, s1, s2)
    p_tree, p_stages = _py_merge_tree(repo, base, s1, s2)
    assert p_tree == g_tree
    assert p_stages == g_stages
