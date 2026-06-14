"""Unit tests for diff, merge, patch, ignore, rerere."""
from __future__ import annotations

from pythongit import diff as diff_mod
from pythongit import merge as merge_mod
from pythongit import patch as patch_mod
from pythongit import ignore as ignore_mod
from pythongit import rerere as rerere_mod
from pythongit import bridges as bridges_mod
from pythongit import cli as cli_mod


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
    assert ig.is_ignored("build/out.o")
    assert not ig.is_ignored("build", is_dir=False)


def test_ignore_nested_gitignore_scoped_to_its_directory(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / ".gitignore").write_text("*.html\n")
    ig = ignore_mod.load(tmp_path)
    assert ig.is_ignored("docs/index.html")
    assert not ig.is_ignored("index.html")
    assert not ig.is_ignored("other/index.html")


def test_ignore_path_patterns_are_relative_not_suffix_matches(tmp_path):
    (tmp_path / ".gitignore").write_text("doc/frotz/\n")
    ig = ignore_mod.load(tmp_path)
    assert ig.is_ignored("doc/frotz/file")
    assert not ig.is_ignored("a/doc/frotz/file")


def test_ignore_leading_slash_anchors_to_gitignore_directory(tmp_path):
    (tmp_path / ".gitignore").write_text("/root.log\n")
    ig = ignore_mod.load(tmp_path)
    assert ig.is_ignored("root.log")
    assert not ig.is_ignored("sub/root.log")


def test_ignore_double_star_pathname_forms(tmp_path):
    (tmp_path / ".gitignore").write_text("**/foo\na/**/b\nabc/**\n")
    ig = ignore_mod.load(tmp_path)
    assert ig.is_ignored("foo")
    assert ig.is_ignored("x/y/foo")
    assert ig.is_ignored("a/b")
    assert ig.is_ignored("a/x/y/b")
    assert ig.is_ignored("abc/x/y")
    assert not ig.is_ignored("abc", is_dir=True)


def test_ignore_negative_cannot_reinclude_inside_ignored_parent(tmp_path):
    (tmp_path / ".gitignore").write_text("build/\n!build/keep.o\n")
    ig = ignore_mod.load(tmp_path)
    assert ig.is_ignored("build/keep.o")


def test_ignore_info_exclude_lower_precedence_than_gitignore(tmp_path):
    (tmp_path / ".git" / "info").mkdir(parents=True)
    (tmp_path / ".git" / "info" / "exclude").write_text("*.log\n")
    (tmp_path / ".gitignore").write_text("!keep.log\n")
    ig = ignore_mod.load(tmp_path)
    assert ig.is_ignored("drop.log")
    assert not ig.is_ignored("keep.log")


def test_ignore_escaped_comment_and_negation_prefixes(tmp_path):
    (tmp_path / ".gitignore").write_text("\\#literal\n\\!literal\n")
    ig = ignore_mod.load(tmp_path)
    assert ig.is_ignored("#literal")
    assert ig.is_ignored("!literal")


def test_ignore_trailing_space_requires_escape(tmp_path):
    (tmp_path / ".gitignore").write_text("plain   \nwith\\ \n")
    ig = ignore_mod.load(tmp_path)
    assert ig.is_ignored("plain")
    assert not ig.is_ignored("plain   ")
    assert ig.is_ignored("with ")
    assert not ig.is_ignored("with")


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


# --- bridges ---------------------------------------------------------------


def test_send_email_uses_smtp_ssl(tmp_path, monkeypatch):
    import smtplib
    calls = []

    class FakeSMTPSSL:
        def __init__(self, host, port, **kwargs):
            calls.append(("connect_ssl", host, port, "context" in kwargs))

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def login(self, user, password):
            calls.append(("login", user, password))

        def send_message(self, msg):
            calls.append(("send", msg["To"], msg["Subject"]))

    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTPSSL)
    mbox = tmp_path / "patch.mbox"
    mbox.write_text("From x\nSubject: test\nFrom: a@example.com\n\nbody\n", encoding="utf-8")

    rc = bridges_mod.send_email(str(mbox), to=["b@example.com"],
                                smtp_host="smtp.example.com", smtp_port=465,
                                smtp_user="user", smtp_pass="pass",
                                smtp_encryption="ssl")

    assert rc == 0
    assert calls[0] == ("connect_ssl", "smtp.example.com", 465, True)
    assert ("login", "user", "pass") in calls
    assert ("send", "b@example.com", "test") in calls


def test_send_email_uses_explicit_starttls(tmp_path, monkeypatch):
    import smtplib
    calls = []

    class FakeSMTP:
        def __init__(self, host, port):
            calls.append(("connect", host, port))

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def starttls(self, **kwargs):
            calls.append(("starttls", "context" in kwargs))

        def send_message(self, msg):
            calls.append(("send", msg["To"], msg["Subject"]))

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    mbox = tmp_path / "patch.mbox"
    mbox.write_text("From x\nSubject: test\n\nbody\n", encoding="utf-8")

    rc = bridges_mod.send_email(str(mbox), to=["b@example.com"],
                                smtp_encryption="tls")

    assert rc == 0
    assert calls[:2] == [("connect", "localhost", 25), ("starttls", True)]
    assert ("send", "b@example.com", "test") in calls


def test_send_email_uses_credential_helper_and_xoauth2(tmp_path, monkeypatch):
    import base64
    import subprocess
    import smtplib
    calls = []

    class FakeSMTP:
        def __init__(self, host, port):
            calls.append(("connect", host, port))

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def starttls(self, **kwargs):
            calls.append(("starttls",))

        def docmd(self, cmd, arg):
            calls.append(("docmd", cmd, arg))
            return 235, b"ok"

        def send_message(self, msg):
            calls.append(("send", msg["To"], msg["Subject"]))

    def fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            ["git", "credential", "fill"],
            0,
            stdout="username=user@example.com\npassword=oauth-token\n",
            stderr="",
        )

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(subprocess, "run", fake_run)
    mbox = tmp_path / "patch.mbox"
    mbox.write_text("From x\nSubject: test\n\nbody\n", encoding="utf-8")

    rc = bridges_mod.send_email(
        str(mbox),
        to=["b@example.com"],
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_encryption="tls",
        smtp_auth="xoauth2",
    )

    assert rc == 0
    auth_call = next(c for c in calls if c[0] == "docmd")
    assert auth_call[1] == "AUTH"
    encoded = auth_call[2].split(" ", 1)[1]
    decoded = base64.b64decode(encoded).decode()
    assert "user=user@example.com" in decoded
    assert "auth=Bearer oauth-token" in decoded


def test_fsmonitor_auto_uses_polling_for_finite_iterations(tmprepo, monkeypatch):
    calls = []

    def fake_poll(repo, interval=1.0, iterations=0):
        calls.append((interval, iterations))
        return 0

    monkeypatch.setattr(bridges_mod, "_fsmonitor_run_polling", fake_poll)
    repo, _ = tmprepo
    assert bridges_mod.fsmonitor_run(repo, interval=0.01, iterations=1, backend="auto") == 0
    assert calls == [(0.01, 1)]


def test_fsmonitor_explicit_native_backend_must_match_platform(tmprepo, monkeypatch, capsys):
    repo, _ = tmprepo
    monkeypatch.setattr(bridges_mod.sys, "platform", "darwin")

    assert bridges_mod.fsmonitor_run(repo, backend="inotify") == 1

    captured = capsys.readouterr()
    assert "not available" in captured.err


def test_bisect_exact_scorer_handles_large_linear_history():
    count = 25001
    commits = [f"{i:040x}" for i in range(count)]
    candidates = set(commits)
    parents = {commits[0]: []}
    for i in range(1, count):
        parents[commits[i]] = [commits[i - 1]]

    best, distance = cli_mod._best_bisection_candidate(candidates, parents)

    assert best == commits[(count // 2) - 1]
    assert distance == count // 2


def test_bisect_exact_scorer_counts_merge_ancestors_once():
    root = "0" * 40
    left = "1" * 40
    right = "2" * 40
    merge = "3" * 40
    tip = "4" * 40
    candidates = {root, left, right, merge, tip}
    parents = {
        root: [],
        left: [root],
        right: [root],
        merge: [left, right],
        tip: [merge],
    }

    best, distance = cli_mod._best_bisection_candidate(candidates, parents)

    assert best == left
    assert distance == 2
