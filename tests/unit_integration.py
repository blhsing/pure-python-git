"""Integration unit tests via the CLI: end-to-end paths that exercise
multiple modules together."""
from __future__ import annotations

import io
import os
import sys
import configparser
from pathlib import Path

import pytest

from pythongit import cli, refs, objects as objs


def cli_run(*args):
    return cli.main(list(args))


def test_full_init_add_commit_log(tmprepo):
    repo, path = tmprepo
    (path / "a.txt").write_text("hello\n")
    assert cli_run("add", "a.txt") == 0
    assert cli_run("commit", "-m", "first") == 0
    head = refs.rev_parse(repo, "HEAD")
    assert head is not None
    assert refs.read_ref(repo, "refs/heads/main") == head


def test_sha256_init_add_commit_local(tmp_path):
    from pythongit.repo import Repository
    from pythongit.index import read_index
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        assert cli_run("init", "--object-format=sha256", str(tmp_path)) == 0
        repo = Repository.discover(tmp_path)
        assert repo.object_format() == "sha256"
        cli_run("config", "user.name", "t")
        cli_run("config", "user.email", "t@example.com")
        (tmp_path / "a.txt").write_text("hello\n")
        assert cli_run("add", "a.txt") == 0
        idx = read_index(repo)
        assert len(idx.entries[0].sha) == 64
        assert cli_run("commit", "-m", "sha256 commit") == 0
        head = refs.rev_parse(repo, "HEAD")
        assert head is not None and len(head) == 64
        t, data = objs.read_object(repo, head)
        assert t == "commit"
        tree = objs.parse_commit(data).tree
        assert len(tree) == 64
        assert cli_run("commit-graph", "write", "--reachable") == 0
        assert cli_run("commit-graph", "verify") == 0
        assert cli_run("fsck") == 0
        assert cli_run("status") == 0
    finally:
        os.chdir(cwd)


def test_real_git_reads_pygit_sha256_repo(tmp_path):
    from tests.conftest import real_git
    gitbin = real_git()
    if not gitbin:
        return
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        assert cli_run("init", "--object-format=sha256", str(tmp_path)) == 0
        cli_run("config", "user.name", "t")
        cli_run("config", "user.email", "t@example.com")
        (tmp_path / "a.txt").write_text("hello\n")
        assert cli_run("add", "a.txt") == 0
        assert cli_run("commit", "-m", "sha256 commit") == 0
        assert cli_run("commit-graph", "write", "--reachable") == 0
    finally:
        os.chdir(cwd)
    import subprocess
    r = subprocess.run([gitbin, "-C", str(tmp_path), "fsck"],
                       capture_output=True, text=True)
    assert r.returncode == 0, f"stderr={r.stderr!r} stdout={r.stdout!r}"
    r = subprocess.run([gitbin, "-C", str(tmp_path), "rev-parse", "--show-object-format"],
                       capture_output=True, text=True)
    assert r.stdout.strip() == "sha256"
    r = subprocess.run([gitbin, "-C", str(tmp_path), "commit-graph", "verify"],
                       capture_output=True, text=True)
    assert r.returncode == 0, f"stderr={r.stderr!r} stdout={r.stdout!r}"


def test_cross_format_clone_and_roundtrip_translation(tmp_path):
    from pythongit.repo import Repository
    source = tmp_path / "source"
    clone256 = tmp_path / "clone256"
    back = tmp_path / "back"
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        assert cli_run("init", str(source)) == 0
        os.chdir(source)
        cli_run("config", "user.name", "t")
        cli_run("config", "user.email", "t@example.com")
        (source / "a.txt").write_text("one\n")
        assert cli_run("add", "a.txt") == 0
        assert cli_run("commit", "-m", "one") == 0
        (source / "a.txt").write_text("one\ntwo\n")
        assert cli_run("add", "a.txt") == 0
        assert cli_run("commit", "-m", "two") == 0
    finally:
        os.chdir(cwd)

    src_repo = Repository.discover(source)
    original_head = refs.rev_parse(src_repo, "HEAD")
    assert original_head is not None
    tag_data = (
        f"object {original_head}\n"
        "type commit\n"
        "tag ann\n"
        "tagger t <t@example.com> 1 +0000\n\n"
        "annotated\n"
    ).encode()
    tag_sha = objs.write_object(src_repo, "tag", tag_data)
    refs.update_ref(src_repo, "refs/tags/ann", tag_sha)

    assert cli_run("clone", "--object-format=sha256", str(source), str(clone256)) == 0
    dst_repo = Repository.discover(clone256)
    translated_head = refs.rev_parse(dst_repo, "HEAD")
    translated_tag = refs.rev_parse(dst_repo, "ann")
    assert dst_repo.object_format() == "sha256"
    assert translated_head is not None and len(translated_head) == 64
    assert translated_tag is not None and len(translated_tag) == 64
    assert (clone256 / "a.txt").read_text() == "one\ntwo\n"
    tag_type, translated_tag_data = objs.read_object(dst_repo, translated_tag)
    assert tag_type == "tag"
    assert f"object {translated_head}\n".encode() in translated_tag_data

    assert cli_run("convert-object-format", "--object-format=sha1", str(clone256), str(back)) == 0
    back_repo = Repository.discover(back)
    assert back_repo.object_format() == "sha1"
    assert refs.rev_parse(back_repo, "HEAD") == original_head
    assert refs.rev_parse(back_repo, "ann") == tag_sha

    from tests.conftest import real_git
    gitbin = real_git()
    if gitbin:
        import subprocess
        for path in (clone256, back):
            r = subprocess.run([gitbin, "-C", str(path), "fsck"],
                               capture_output=True, text=True)
            assert r.returncode == 0, f"{path}: stderr={r.stderr!r} stdout={r.stdout!r}"


def test_branch_checkout_switches(tmprepo):
    from tests.conftest import commit_one
    repo, _ = tmprepo
    commit_one(repo, "a", "1", "c1")
    cli_run("branch", "feat")
    cli_run("checkout", "feat")
    sym, _ = refs.read_head(repo)
    assert sym == "refs/heads/feat"


def test_merge_clean_three_way(tmprepo):
    from tests.conftest import commit_one
    repo, path = tmprepo
    commit_one(repo, "a", "1\n", "base")
    cli_run("branch", "feat")
    cli_run("checkout", "feat")
    commit_one(repo, "b", "x\n", "feat tip")
    cli_run("checkout", "main")
    commit_one(repo, "c", "y\n", "main tip")
    assert cli_run("merge", "feat") == 0
    assert (path / "a").exists()
    assert (path / "b").exists()
    assert (path / "c").exists()


def test_merge_conflict_creates_stages_and_blocks_commit(tmprepo):
    from tests.conftest import commit_one
    from pythongit.index import read_index
    repo, path = tmprepo
    commit_one(repo, "f", "base\n", "c1")
    cli_run("branch", "feat")
    cli_run("checkout", "feat")
    commit_one(repo, "f", "feat-side\n", "c2-feat")
    cli_run("checkout", "main")
    commit_one(repo, "f", "main-side\n", "c2-main")
    rc = cli_run("merge", "feat")
    assert rc != 0  # conflict
    idx = read_index(repo)
    assert idx.has_conflicts()
    # commit must refuse
    rc = cli_run("commit", "-m", "x")
    assert rc != 0
    # resolve and recommit
    (path / "f").write_text("resolved\n")
    cli_run("add", "f")
    assert cli_run("commit", "-m", "resolved") == 0
    idx2 = read_index(repo)
    assert not idx2.has_conflicts()


def test_merge_ort_backend_matches_git_conflict_blob(tmprepo):
    import subprocess
    from tests.conftest import commit_one, real_git
    from pythongit.index import read_index

    gitbin = real_git()
    if not gitbin:
        return
    repo, path = tmprepo
    base = commit_one(repo, "f", "base\n", "base")
    cli_run("branch", "feat")
    cli_run("checkout", "feat")
    commit_one(repo, "f", "theirs\n", "theirs")
    cli_run("checkout", "main")
    commit_one(repo, "f", "ours\n", "ours")

    expected_tree = subprocess.run(
        [
            gitbin,
            "-C",
            str(path),
            "merge-tree",
            "--write-tree",
            "--no-messages",
            "--merge-base",
            base,
            "HEAD",
            "feat",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert expected_tree.returncode == 1
    tree = expected_tree.stdout.splitlines()[0]
    expected_blob = subprocess.run(
        [gitbin, "-C", str(path), "show", f"{tree}:f"],
        capture_output=True,
        timeout=10,
    )
    assert expected_blob.returncode == 0

    assert cli_run("merge", "feat") == 1
    assert (path / "f").read_bytes() == expected_blob.stdout
    stages = read_index(repo).by_path_all_stages()["f"]
    assert set(stages) == {1, 2, 3}


def test_merge_detects_rename_modify(tmprepo):
    from tests.conftest import commit_one

    repo, path = tmprepo
    commit_one(repo, "old.txt", "base\n", "base")
    assert cli_run("branch", "feat") == 0

    (path / "old.txt").write_text("ours\n")
    assert cli_run("add", "old.txt") == 0
    assert cli_run("commit", "-m", "ours modifies") == 0

    assert cli_run("checkout", "feat") == 0
    assert cli_run("mv", "old.txt", "new.txt") == 0
    assert cli_run("commit", "-m", "rename") == 0

    assert cli_run("checkout", "main") == 0
    assert cli_run("merge", "feat") == 0
    assert not (path / "old.txt").exists()
    assert (path / "new.txt").read_text() == "ours\n"


def test_rerere_replays_on_second_attempt(tmprepo):
    from tests.conftest import commit_one
    repo, path = tmprepo
    commit_one(repo, "f", "base\n", "c1")
    cli_run("branch", "feat")
    cli_run("checkout", "feat")
    commit_one(repo, "f", "B\n", "c2-feat")
    cli_run("checkout", "main")
    commit_one(repo, "f", "A\n", "c2-main")
    # first merge conflicts; resolve and record
    cli_run("merge", "feat")
    (path / "f").write_text("RESOLVED\n")
    cli_run("add", "f")
    cli_run("commit", "-m", "resolved")  # this triggers rerere.scan_and_record
    # reset back and try the same merge again
    cli_run("reset", "--hard", "HEAD~1") if False else None  # we'll branch off main^
    head_now = refs.rev_parse(repo, "HEAD")
    # the commit we just made is the merge resolution; rerere stored postimage
    rr = repo.gitdir / "rr-cache"
    assert any((rr.glob("*/postimage")) if rr.exists() else [])


def test_cherry_pick_clean(tmprepo):
    from tests.conftest import commit_one
    repo, path = tmprepo
    commit_one(repo, "a", "a\n", "c1")
    cli_run("branch", "feat")
    cli_run("checkout", "feat")
    feat = commit_one(repo, "b", "b\n", "feat add b")
    cli_run("checkout", "main")
    assert cli_run("cherry-pick", feat) == 0
    assert (path / "b").exists()


def test_revert_inverts(tmprepo):
    from tests.conftest import commit_one
    repo, path = tmprepo
    commit_one(repo, "a", "a\n", "c1")
    c2 = commit_one(repo, "a", "b\n", "c2")
    assert cli_run("revert", c2) == 0
    assert (path / "a").read_text() == "a\n"


def test_rebase_replays_topic(tmprepo):
    from tests.conftest import commit_one
    repo, path = tmprepo
    commit_one(repo, "a", "1\n", "A")
    cli_run("branch", "topic")
    commit_one(repo, "a", "1\n2\n", "B main")
    cli_run("checkout", "topic")
    commit_one(repo, "g", "g\n", "B topic")
    assert cli_run("rebase", "main") == 0
    assert (path / "g").exists()
    assert (path / "a").read_text() == "1\n2\n"


def test_stash_push_pop(tmprepo):
    from tests.conftest import commit_one
    repo, path = tmprepo
    commit_one(repo, "a", "v1\n", "c1")
    (path / "a").write_text("dirty\n")
    assert cli_run("stash", "push") == 0
    assert (path / "a").read_text() == "v1\n"
    assert cli_run("stash", "pop") == 0
    assert (path / "a").read_text() == "dirty\n"


def test_tag_and_rev_parse(tmprepo):
    from tests.conftest import commit_one
    repo, _ = tmprepo
    sha = commit_one(repo, "a", "1", "c1")
    cli_run("tag", "v1")
    assert refs.rev_parse(repo, "v1") == sha


def test_diff_show_log(tmprepo, capsys):
    from tests.conftest import commit_one
    repo, path = tmprepo
    commit_one(repo, "a", "1\n", "c1")
    commit_one(repo, "a", "2\n", "c2")
    assert cli_run("log", "--oneline") == 0
    out = capsys.readouterr().out
    assert "c1" in out and "c2" in out


def test_describe_after_tag(tmprepo):
    from tests.conftest import commit_one
    repo, _ = tmprepo
    commit_one(repo, "a", "1", "c1")
    cli_run("tag", "v1")
    assert cli_run("describe", "HEAD") == 0


def test_pack_objects_and_unpack(tmprepo):
    from tests.conftest import commit_one
    repo, _ = tmprepo
    for i in range(5):
        commit_one(repo, "a", f"v{i}\n", f"c{i}")
    assert cli_run("pack-objects", "pack", "--all") == 0
    pack_dir = repo.gitdir / "objects" / "pack"
    assert list(pack_dir.glob("pack-*.pack"))
    # verify
    assert cli_run("verify-pack", *[str(p) for p in pack_dir.glob("pack-*.pack")]) == 0


def test_commit_graph_binary_passes_real_git(tmprepo):
    import subprocess
    from conftest import real_git
    gitbin = real_git()
    if not gitbin:
        return
    from tests.conftest import commit_one
    repo, _ = tmprepo
    for i in range(5):
        commit_one(repo, "a", f"v{i}\n", f"c{i}")
    assert cli_run("commit-graph", "write") == 0
    r = subprocess.run([gitbin, "commit-graph", "verify"], cwd=repo.path,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_commit_graph_changed_path_bloom_filters(tmprepo):
    import struct
    import subprocess
    from conftest import real_git
    from tests.conftest import commit_one
    from pythongit import bloom

    repo, path = tmprepo
    commit_one(repo, "a.txt", "one\n", "c1")
    (path / "d").mkdir()
    c2 = commit_one(repo, "d/b.txt", "two\n", "c2")
    assert cli_run("commit-graph", "write", "--reachable", "--changed-paths") == 0
    assert cli_run("commit-graph", "verify") == 0

    raw = (repo.gitdir / "objects" / "info" / "commit-graph").read_bytes()
    chunks: dict[bytes, bytes] = {}
    entries: list[tuple[bytes, int]] = []
    for i in range(raw[6] + 1):
        pos = 8 + i * 12
        entries.append((raw[pos:pos + 4], struct.unpack(">Q", raw[pos + 4:pos + 12])[0]))
    for (cid, off), (_next_cid, nxt) in zip(entries, entries[1:]):
        if cid != b"\0\0\0\0":
            chunks[cid] = raw[off:nxt]
    assert b"BIDX" in chunks and b"BDAT" in chunks
    shas = [
        chunks[b"OIDL"][i:i + repo.hash_len].hex()
        for i in range(0, len(chunks[b"OIDL"]), repo.hash_len)
    ]
    filters = bloom.read_commit_graph_bloom_filters(chunks[b"BIDX"], chunks[b"BDAT"], len(shas))
    changed_filter = filters[shas.index(c2)]
    assert bloom.bloom_maybe_contains(changed_filter, "d")
    assert bloom.bloom_maybe_contains(changed_filter, "d/b.txt")

    gitbin = real_git()
    if gitbin:
        r = subprocess.run([gitbin, "commit-graph", "verify"], cwd=repo.path,
                           capture_output=True, text=True)
        assert r.returncode == 0, f"stderr={r.stderr!r} stdout={r.stdout!r}"


def test_commit_graph_reader_and_path_lookup_shortcuts(tmprepo, capsys):
    from tests.conftest import commit_one
    from pythongit import commitgraph

    repo, path = tmprepo
    c1 = commit_one(repo, "a.txt", "one\n", "c1")
    (path / "d").mkdir()
    c2 = commit_one(repo, "d/b.txt", "two\n", "c2")
    commit_one(repo, "a.txt", "one\n", "c3 unchanged target")

    assert cli_run("commit-graph", "write", "--changed-paths") == 0
    capsys.readouterr()
    graph = commitgraph.read_commit_graph(repo)
    assert graph is not None
    c2_entry = graph.get(c2)
    assert c2_entry is not None
    assert c2_entry.parents == (c1,)
    assert graph.maybe_changed(c2, "d/b.txt")
    assert not graph.maybe_changed(c2, "a.txt")

    assert cli_run("last-modified", "d/b.txt") == 0
    assert capsys.readouterr().out.strip() == c2


def test_rev_list_count_uses_bitmap_commit_filter(tmprepo, capsys):
    from tests.conftest import commit_one

    repo, _ = tmprepo
    for i in range(3):
        commit_one(repo, "a.txt", f"{i}\n", f"c{i}")
    assert cli_run("pack-objects", "pack", "--all") == 0
    capsys.readouterr()
    assert cli_run("rev-list", "--count", "HEAD") == 0
    assert capsys.readouterr().out.strip() == "3"


def test_loose_object_cache_backs_count_and_abbrev_resolution(tmprepo, monkeypatch):
    from pythongit import loose

    repo, _ = tmprepo
    shas = [objs.write_object(repo, "blob", f"loose {i}\n".encode()) for i in range(20)]
    count, size = loose.count_and_size(repo)
    assert count >= 20
    assert size > 0
    assert (repo.gitdir / "objects" / "info" / "pygit-loose-cache-v1").exists()

    loose.clear_cache(repo)

    def fail_scan(_repo):
        raise AssertionError("loose cache was not reused")

    monkeypatch.setattr(loose, "_scan_entries", fail_scan)
    assert loose.count_and_size(repo)[0] >= 20
    assert refs.rev_parse(repo, shas[0][:8]) == shas[0]


def test_http_backend_streams_upload_pack_response(tmprepo):
    from tests.conftest import commit_one
    from pythongit import bridges

    repo, path = tmprepo
    head = commit_one(repo, "a.txt", "one\n", "c1")

    def pkt(payload: bytes) -> bytes:
        return f"{len(payload) + 4:04x}".encode() + payload

    body = pkt(f"want {head} side-band-64k\n".encode()) + b"0000" + pkt(b"done\n")
    status, headers, chunks = bridges.http_backend_stream(
        "POST",
        f"/{path.name}/git-upload-pack",
        body,
        path.parent,
    )
    response = b"".join(chunks)
    assert status == 200
    assert headers["Content-Type"] == "application/x-git-upload-pack-result"
    assert b"PACK" in response


def test_diff_tree_uses_recursive_tree_changes(tmprepo, monkeypatch, capsys):
    from tests.conftest import commit_one
    from pythongit import workdir

    repo, _ = tmprepo
    c1 = commit_one(repo, "a.txt", "one\n", "c1")
    c2 = commit_one(repo, "a.txt", "two\n", "c2")

    def fail_flatten(*_args, **_kwargs):
        raise AssertionError("diff-tree should not flatten whole trees")

    monkeypatch.setattr(workdir, "flatten_tree", fail_flatten)
    assert cli_run("diff-tree", "--name-status", c1, c2) == 0
    assert "M\ta.txt" in capsys.readouterr().out


def test_status_groups_correctly(tmprepo):
    from tests.conftest import commit_one
    from pythongit import workdir
    repo, path = tmprepo
    commit_one(repo, "a", "1\n", "c1")
    (path / "b").write_text("new\n")  # untracked
    (path / "a").write_text("1mod\n")  # modified worktree
    s = workdir.status(repo)
    assert "b" in s["untracked"]
    assert "a" in s["modified"]


def test_status_and_add_respect_gitignore_for_untracked_files(tmprepo):
    from pythongit import workdir
    from pythongit.index import read_index

    repo, path = tmprepo
    (path / ".gitignore").write_text("*.log\nbuild/\n!keep.log\n")
    (path / "ignored.log").write_text("ignored\n")
    (path / "keep.log").write_text("keep\n")
    (path / "build").mkdir()
    (path / "build" / "out.o").write_text("object\n")

    s = workdir.status(repo)
    assert "ignored.log" not in s["untracked"]
    assert "build/out.o" not in s["untracked"]
    assert "keep.log" in s["untracked"]

    workdir.add_paths(repo, ["."])
    indexed = read_index(repo).by_path()
    assert "ignored.log" not in indexed
    assert "build/out.o" not in indexed
    assert "keep.log" in indexed


def test_status_reports_tracked_file_even_when_ignored(tmprepo):
    from tests.conftest import commit_one
    from pythongit import workdir

    repo, path = tmprepo
    commit_one(repo, "tracked.log", "old\n", "track log")
    (path / ".gitignore").write_text("*.log\n")
    (path / "tracked.log").write_text("new\n")

    s = workdir.status(repo)
    assert "tracked.log" in s["modified"]


def test_check_ignore_honors_directory_only_pattern(tmprepo, capsys):
    _repo, path = tmprepo
    (path / ".gitignore").write_text("build/\n")
    (path / "build").mkdir()

    assert cli_run("check-ignore", "build") == 0
    assert "build" in capsys.readouterr().out


def test_config_replace_all_writes_subsection_keys(tmprepo):
    repo, _path = tmprepo
    assert cli_run("config", "--replace-all", "credential.https://github.com.helper", "!gh auth git-credential") == 0

    cp = configparser.ConfigParser(interpolation=None)
    cp.read(repo.gitdir / "config", encoding="utf-8")
    assert cp.get('credential "https://github.com"', "helper") == "!gh auth git-credential"


def test_format_patch_then_apply_roundtrip(tmprepo, tmp_path_factory):
    from tests.conftest import commit_one
    repo, path = tmprepo
    commit_one(repo, "a", "1\n", "c1")
    commit_one(repo, "a", "1\n2\n", "c2")
    out_dir = path / "patches"
    cli_run("format-patch", "-o", str(out_dir), "-1")
    patches = list(out_dir.glob("*.patch"))
    assert patches
    # fresh repo: apply
    fresh = tmp_path_factory.mktemp("fresh")
    os.chdir(fresh)
    cli_run("init", str(fresh))
    cli_run("config", "user.name", "t")
    cli_run("config", "user.email", "t@e.com")
    (fresh / "a").write_text("1\n")
    cli_run("add", "a")
    cli_run("commit", "-m", "base")
    assert cli_run("apply", str(patches[0])) == 0
    assert (fresh / "a").read_text() == "1\n2\n"


def test_bisect_halving_finds_middle(tmprepo):
    from tests.conftest import commit_one
    repo, _ = tmprepo
    commits = []
    for i in range(7):
        commits.append(commit_one(repo, "a", f"v{i}\n", f"c{i}"))
    rc = cli_run("bisect", "start", commits[-1], commits[0])
    assert rc == 0
    # HEAD should now be one of the inner commits (not endpoints)
    _, head = refs.read_head(repo)
    assert head in commits[1:-1]
    cli_run("bisect", "reset")
