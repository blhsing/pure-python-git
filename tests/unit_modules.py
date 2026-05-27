"""Unit tests for diff, merge, patch, ignore, rerere."""
from __future__ import annotations

from pythongit import diff as diff_mod
from pythongit import merge as merge_mod
from pythongit import patch as patch_mod
from pythongit import ignore as ignore_mod
from pythongit import rerere as rerere_mod


# --- diff -----------------------------------------------------------------


def test_diff_identical_returns_empty():
    assert diff_mod.unified_diff("a\nb\n", "a\nb\n") == ""


def test_diff_simple_insertion():
    out = diff_mod.unified_diff("a\nb\n", "a\nb\nc\n")
    assert "@@" in out
    assert "+c" in out


def test_diff_replacement():
    out = diff_mod.unified_diff("a\nb\nc\n", "a\nB\nc\n")
    assert "-b" in out
    assert "+B" in out


def test_diff_handles_empty_to_nonempty():
    out = diff_mod.unified_diff("", "x\n")
    assert "+x" in out


# --- merge ----------------------------------------------------------------


def test_merge_blob_identical_sides_no_conflict():
    merged, conf = merge_mod.merge_blob(b"a\n", b"a\n", b"a\n")
    assert not conf
    assert merged == b"a\n"


def test_merge_blob_one_side_unchanged_takes_other():
    merged, conf = merge_mod.merge_blob(b"a\n", b"a\n", b"b\n")
    assert not conf
    assert merged == b"b\n"


def test_merge_blob_conflict_emits_markers():
    merged, conf = merge_mod.merge_blob(b"a\n", b"x\n", b"y\n")
    assert conf
    assert b"<<<<<<<" in merged
    assert b">>>>>>>" in merged


def test_merge_bases_chain(tmprepo):
    from tests.conftest import commit_one
    repo, _ = tmprepo
    a = commit_one(repo, "f", "1", "c1")
    b = commit_one(repo, "f", "2", "c2")
    c = commit_one(repo, "f", "3", "c3")
    # a is ancestor of c
    assert merge_mod.is_ancestor(repo, a, c)
    assert merge_mod.merge_bases(repo, a, c) == [a]


# --- patch ----------------------------------------------------------------


def test_patch_parses_unified_diff():
    text = (
        "diff --git a/x.txt b/x.txt\n"
        "--- a/x.txt\n+++ b/x.txt\n"
        "@@ -1,1 +1,2 @@\n"
        " line1\n+line2\n"
    )
    patches = patch_mod.parse_patch(text)
    assert len(patches) == 1
    assert patches[0].a_path == "x.txt"
    assert len(patches[0].hunks) == 1
    assert patches[0].hunks[0].a_count == 1
    assert patches[0].hunks[0].b_count == 2


def test_patch_apply_text_inserts():
    text = (
        "diff --git a/x b/x\n"
        "--- a/x\n+++ b/x\n@@ -1,1 +1,2 @@\n one\n+two\n"
    )
    patches = patch_mod.parse_patch(text)
    out = patch_mod.apply_to_text("one\n", patches[0].hunks)
    assert out == "one\ntwo\n"


def test_patch_reverse():
    text = (
        "diff --git a/x b/x\n"
        "--- a/x\n+++ b/x\n@@ -1,1 +1,2 @@\n one\n+two\n"
    )
    patches = patch_mod.parse_patch(text)
    out = patch_mod.apply_to_text("one\ntwo\n", patches[0].hunks, reverse=True)
    assert out == "one\n"


# --- ignore ---------------------------------------------------------------


def test_ignore_glob_star_extension(tmp_path):
    (tmp_path / ".gitignore").write_text("*.log\n")
    ig = ignore_mod.load(tmp_path)
    assert ig.is_ignored("foo.log")
    assert not ig.is_ignored("foo.txt")


def test_ignore_negation(tmp_path):
    (tmp_path / ".gitignore").write_text("*.log\n!keep.log\n")
    ig = ignore_mod.load(tmp_path)
    assert ig.is_ignored("foo.log")
    assert not ig.is_ignored("keep.log")


def test_ignore_directory_only(tmp_path):
    (tmp_path / ".gitignore").write_text("build/\n")
    ig = ignore_mod.load(tmp_path)
    assert ig.is_ignored("build", is_dir=True)
    assert not ig.is_ignored("build", is_dir=False)


# --- rerere ---------------------------------------------------------------


def test_rerere_normalize_replaces_branch_labels():
    text = (
        "before\n<<<<<<< HEAD\nA\n=======\nB\n>>>>>>> feature\nafter\n"
    )
    norm, chunks = rerere_mod._normalize(text)
    assert chunks == [("A\n", "B\n")]
    assert "<<<<<<<\n" in norm
    assert ">>>>>>>\n" in norm
    # branch labels stripped, so two conflicts with different branches collide
    text2 = (
        "before\n<<<<<<< main\nA\n=======\nB\n>>>>>>> other\nafter\n"
    )
    norm2, _ = rerere_mod._normalize(text2)
    assert norm == norm2


def test_rerere_replay_after_record(tmprepo):
    repo, _ = tmprepo
    pre = "<<<<<<< x\nA\n=======\nB\n>>>>>>> y\n"
    post = "RESOLVED\n"
    rerere_mod.record_resolution(repo, "f", pre, post)
    assert rerere_mod.replay(repo, pre) == post


def test_rerere_replay_for_unseen_preimage(tmprepo):
    repo, _ = tmprepo
    assert rerere_mod.replay(repo, "no such conflict\n") is None
