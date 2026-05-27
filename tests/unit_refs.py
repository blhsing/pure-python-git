"""Unit tests for pythongit.refs."""
from __future__ import annotations

from pythongit import refs


def test_init_writes_head_pointing_at_main(tmprepo):
    repo, _ = tmprepo
    sym, sha = refs.read_head(repo)
    assert sym == "refs/heads/main"
    assert sha is None  # no commits yet


def test_update_and_read_ref(tmprepo):
    repo, _ = tmprepo
    refs.update_ref(repo, "refs/heads/feature", "0" * 40)
    assert refs.read_ref(repo, "refs/heads/feature") == "0" * 40


def test_update_ref_writes_reflog(tmprepo):
    repo, _ = tmprepo
    refs.update_ref(repo, "refs/heads/feature", "a" * 40, message="test")
    refs.update_ref(repo, "refs/heads/feature", "b" * 40, message="bump")
    entries = (repo.gitdir / "logs" / "refs" / "heads" / "feature").read_text(encoding="utf-8").splitlines()
    assert len(entries) == 2
    assert entries[0].startswith("0" * 40 + " " + "a" * 40)
    assert entries[1].startswith("a" * 40 + " " + "b" * 40)


def test_rev_parse_resolves_branch_and_tag(tmprepo):
    repo, _ = tmprepo
    refs.update_ref(repo, "refs/heads/main", "1" * 40)
    refs.update_ref(repo, "refs/tags/v1", "2" * 40)
    assert refs.rev_parse(repo, "main") == "1" * 40
    assert refs.rev_parse(repo, "v1") == "2" * 40
    assert refs.rev_parse(repo, "refs/heads/main") == "1" * 40


def test_rev_parse_abbreviated_loose_sha(tmprepo):
    from pythongit import objects as objs
    repo, _ = tmprepo
    sha = objs.write_object(repo, "blob", b"data")
    assert refs.rev_parse(repo, sha[:8]) == sha


def test_set_head_symbolic_vs_detached(tmprepo):
    repo, _ = tmprepo
    refs.set_head(repo, "refs/heads/feature")
    sym, _ = refs.read_head(repo)
    assert sym == "refs/heads/feature"
    refs.set_head(repo, "a" * 40)
    sym, sha = refs.read_head(repo)
    assert sym is None
    assert sha == "a" * 40


def test_delete_ref(tmprepo):
    repo, _ = tmprepo
    refs.update_ref(repo, "refs/heads/x", "f" * 40)
    refs.delete_ref(repo, "refs/heads/x")
    assert refs.read_ref(repo, "refs/heads/x") is None


def test_packed_refs_read(tmprepo):
    repo, _ = tmprepo
    (repo.gitdir / "packed-refs").write_text(
        "# pack-refs with: peeled\n"
        f"{'a' * 40} refs/heads/main\n"
        f"{'b' * 40} refs/tags/v1\n",
        encoding="utf-8",
    )
    assert refs.read_packed_refs(repo) == {
        "refs/heads/main": "a" * 40,
        "refs/tags/v1": "b" * 40,
    }
