"""Behavioral parity cases checked against a real C Git 2.54.0 oracle.

Each case builds two identical repositories (one driven by the oracle, one by
pythongit) under a hermetic, deterministic environment, runs the same probe
command on each, and asserts byte-identical return code, stdout, and stderr.

These run in the ``git-254-parity`` CI job (``--require-git-254-oracle``); they
skip locally when no ``PYGIT_PARITY_GIT`` oracle is configured.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.git_parity.support import assert_command_parity


# A small committed history shared by many cases.
BASE = [
    ("write", "a.txt", "alpha\n"),
    ("write", "b.txt", "beta\n"),
    ["add", "-A"],
    ["commit", "-m", "first"],
]

# A repo with one tag and a second commit for revision-walking cases.
TAGGED = BASE + [
    ("write", "a.txt", "alpha\nmore\n"),
    ["add", "-A"],
    ["commit", "-m", "second"],
    ["tag", "v1"],
]

# Every kind of pending change at once.
DIRTY = [
    ("write", "tracked.txt", "committed\n"),
    ("write", "mod_staged.txt", "old\n"),
    ("write", "deleted.txt", "del\n"),
    ("write", "mod_wt.txt", "wt\n"),
    ["add", "-A"],
    ["commit", "-m", "base"],
    ("write", "new_staged.txt", "NEW\n"),
    ["add", "new_staged.txt"],
    ("write", "mod_staged.txt", "changed\n"),
    ["add", "mod_staged.txt"],
    ("write", "mod_wt.txt", "wtchg\n"),
    ("rm", "deleted.txt"),
    ("write", "untracked.txt", "hi\n"),
]

REMOTE = BASE + [
    ["remote", "add", "origin", "https://example.com/r.git"],
    ["config", "--add", "foo.bar", "one"],
    ["config", "--add", "foo.bar", "two"],
]

CASES: list[tuple] = [
    # rev-parse repo info
    ("revparse-git-dir", [], ["rev-parse", "--git-dir"]),
    ("revparse-absolute-git-dir", [], ["rev-parse", "--absolute-git-dir"]),
    ("revparse-inside-work-tree", [], ["rev-parse", "--is-inside-work-tree"]),
    ("revparse-inside-git-dir", [], ["rev-parse", "--is-inside-git-dir"]),
    ("revparse-bare", [], ["rev-parse", "--is-bare-repository"]),
    ("revparse-show-toplevel", [], ["rev-parse", "--show-toplevel"]),
    ("revparse-show-prefix", [], ["rev-parse", "--show-prefix"]),
    ("revparse-head-unborn", [], ["rev-parse", "HEAD"]),
    ("revparse-verify-unborn", [], ["rev-parse", "--verify", "HEAD"]),
    ("revparse-q-verify-unborn", [], ["rev-parse", "-q", "--verify", "HEAD"]),
    ("revparse-abbrev-ref-unborn", [], ["rev-parse", "--abbrev-ref", "HEAD"]),
    ("revparse-unknown-dashed", [], ["rev-parse", "--frobnicate"]),
    # revision suffixes / path syntax
    ("revparse-head", TAGGED, ["rev-parse", "HEAD"]),
    ("revparse-tilde", TAGGED, ["rev-parse", "HEAD~1"]),
    ("revparse-caret", TAGGED, ["rev-parse", "HEAD^"]),
    ("revparse-tag-peel", TAGGED, ["rev-parse", "v1^{commit}"]),
    ("revparse-abbrev-ref-born", TAGGED, ["rev-parse", "--abbrev-ref", "HEAD"]),
    ("revparse-too-far", TAGGED, ["rev-parse", "HEAD~9"]),
    ("revparse-path", TAGGED, ["rev-parse", "HEAD:a.txt"]),
    # config
    ("config-get", REMOTE, ["config", "--get", "foo.bar"]),
    ("config-get-all", REMOTE, ["config", "--get-all", "foo.bar"]),
    ("config-get-missing", REMOTE, ["config", "--get", "no.such"]),
    ("config-list", REMOTE, ["config", "--list"]),
    ("config-bad-key", [], ["config", "nodot"]),
    ("config-remote-url", REMOTE, ["config", "--get", "remote.origin.url"]),
    ("remote-verbose", REMOTE, ["remote", "-v"]),
    # var
    ("var-author-ident", [], ["var", "GIT_AUTHOR_IDENT"]),
    ("var-committer-ident", [], ["var", "GIT_COMMITTER_IDENT"]),
    ("var-unknown", [], ["var", "BOGUS"]),
    ("var-no-arg", [], ["var"]),
    # symbolic-ref
    ("symref-short-unborn", [], ["symbolic-ref", "--short", "HEAD"]),
    ("symref-head", [], ["symbolic-ref", "HEAD"]),
    ("symref-missing", [], ["symbolic-ref", "refs/heads/nope"]),
    # branch
    ("branch-show-current-unborn", [], ["branch", "--show-current"]),
    ("branch-show-current", BASE, ["branch", "--show-current"]),
    ("branch-list", BASE + [["branch", "feature"]], ["branch"]),
    ("branch-all", BASE + [["branch", "feature"]], ["branch", "-a"]),
    ("branch-delete-missing", BASE, ["branch", "-d", "nope"]),
    # log
    ("log-unborn", [], ["log"]),
    ("log-oneline-unborn", [], ["log", "--oneline"]),
    ("log-bad-rev", BASE, ["log", "nonexistent"]),
    # show-ref
    ("showref-unborn", [], ["show-ref"]),
    ("showref-born", BASE + [["branch", "feature"]], ["show-ref"]),
    ("showref-head", BASE, ["show-ref", "--head"]),
    ("showref-heads", BASE + [["branch", "feature"]], ["show-ref", "--heads"]),
    ("showref-nomatch", BASE, ["show-ref", "zzz"]),
    # status
    ("status-clean-long", BASE, ["status"]),
    ("status-clean-porcelain", BASE, ["status", "--porcelain"]),
    ("status-clean-sb", BASE, ["status", "-s", "-b"]),
    ("status-dirty-porcelain", DIRTY, ["status", "--porcelain"]),
    ("status-dirty-porcelain-v2", DIRTY, ["status", "--porcelain=v2"]),
    ("status-dirty-short", DIRTY, ["status", "-s"]),
    ("status-dirty-long", DIRTY, ["status"]),
    ("status-untracked-only", BASE + [("write", "u.txt", "u\n")], ["status"]),
    # cat-file / ls-tree
    ("catfile-type", BASE, ["cat-file", "-t", "HEAD"]),
    ("catfile-pretty-tree", BASE, ["cat-file", "-p", "HEAD^{tree}"]),
    ("catfile-path", BASE, ["cat-file", "-p", "HEAD:a.txt"]),
    ("catfile-e-missing", BASE, ["cat-file", "-e", "deadbeef" * 5]),
    ("lstree", BASE, ["ls-tree", "HEAD"]),
    ("lstree-name-only", BASE, ["ls-tree", "--name-only", "HEAD"]),
    # commit output
    ("commit-root", [("write", "x.txt", "x\n"), ["add", "x.txt"]], ["commit", "-m", "msg"]),
    ("commit-second", BASE + [("write", "c.txt", "c\n"), ["add", "c.txt"]], ["commit", "-m", "two"]),
    # init
    ("init-default-branch", [], ["init", "fresh"]),
    # diff
    ("diff-modify", BASE + [("write", "a.txt", "alpha\nmore\n")], ["diff"]),
    ("diff-cached-new", BASE + [("write", "c.txt", "c\n"), ["add", "c.txt"]], ["diff", "--cached"]),
    ("diff-delete", BASE + [("rm", "b.txt")], ["diff"]),
    ("diff-no-newline", BASE + [("write", "a.txt", "x")], ["diff", "--", "a.txt"]),
    ("diff-two-rev", TAGGED, ["diff", "HEAD~1", "HEAD"]),
    ("diff-stat", BASE + [("write", "a.txt", "alpha\nmore\n"), ("rm", "b.txt")], ["diff", "--stat"]),
    ("diff-name-only", BASE + [("write", "a.txt", "X\n")], ["diff", "--name-only"]),
    # show / log medium format (Author/Date)
    ("show-patch", TAGGED, ["show"]),
    ("show-no-patch", TAGGED, ["show", "-s"]),
    ("show-stat", TAGGED, ["show", "--stat"]),
    ("show-root", BASE, ["show"]),
    ("log-medium", TAGGED, ["log"]),
    ("log-n1", TAGGED, ["log", "-1"]),
    # tag
    ("tag-annotated", BASE, ["tag", "-a", "v1", "-m", "release one"]),
    ("tag-message-implies-annotated", BASE, ["tag", "v2", "-m", "msg"]),
    ("tag-delete", BASE + [["tag", "v1"]], ["tag", "-d", "v1"]),
    ("tag-cat-annotated", BASE + [["tag", "-a", "v1", "-m", "ann"]], ["cat-file", "-p", "v1"]),
    # rm / add / mv
    ("rm-tracked", BASE, ["rm", "a.txt"]),
    ("rm-cached", BASE, ["rm", "--cached", "a.txt"]),
    ("rm-missing", BASE, ["rm", "nope.txt"]),
    ("add-missing", BASE, ["add", "nope.txt"]),
    ("add-dry-run", BASE + [("write", "n.txt", "n\n")], ["add", "-n", "n.txt"]),
    # checkout / switch / reset
    ("checkout-new-branch", BASE, ["checkout", "-b", "feature"]),
    ("switch-create", BASE, ["switch", "-c", "feature"]),
    ("checkout-existing", BASE + [["branch", "feature"]], ["checkout", "feature"]),
    ("reset-hard", BASE + [("write", "a.txt", "changed\n")], ["reset", "--hard", "HEAD"]),
    # ls-files selectors
    ("lsfiles", BASE, ["ls-files"]),
    ("lsfiles-modified", BASE + [("write", "a.txt", "changed\n")], ["ls-files", "-m"]),
    ("lsfiles-others", BASE + [("write", "u.txt", "u\n")], ["ls-files", "-o", "--exclude-standard"]),
    ("lsfiles-deleted", BASE + [("rm", "a.txt")], ["ls-files", "-d"]),
    # commit modes
    ("commit-all", BASE + [("write", "a.txt", "mod\n")], ["commit", "-a", "-m", "all"]),
    ("commit-amend", BASE, ["commit", "--amend", "-m", "reworded"]),
    ("commit-nothing", BASE, ["commit", "-m", "noop"]),
    ("commit-allow-empty", BASE, ["commit", "--allow-empty", "-m", "empty"]),
    # merge
    ("merge-ff",
     BASE + [["checkout", "-b", "topic"], ("write", "c.txt", "c\n"), ["add", "-A"],
             ["commit", "-m", "t1"], ["checkout", "main"]],
     ["merge", "topic"]),
    ("merge-up-to-date", BASE, ["merge", "HEAD"]),
    # clean
    ("clean-dry-run", BASE + [("write", "junk.txt", "j\n")], ["clean", "-n"]),
    ("clean-dirs", BASE + [("write", "jd/junk.txt", "j\n")], ["clean", "-n", "-d"]),
    # for-each-ref / rev-list
    ("for-each-ref", BASE + [["branch", "feature"], ["tag", "v1"]], ["for-each-ref"]),
    ("for-each-ref-short",
     BASE + [["branch", "feature"]], ["for-each-ref", "--format=%(refname:short)", "refs/heads"]),
    ("rev-list-all", BASE + [["branch", "feature"], ["tag", "v1"]], ["rev-list", "--all"]),
    # log pretty/format, rev-list --reverse
    ("log-format-H", TAGGED, ["log", "--format=%H"]),
    ("log-pretty-oneline", TAGGED, ["log", "--pretty=oneline"]),
    ("log-format-custom", TAGGED, ["log", "--format=%h %an %s"]),
    ("rev-list-reverse", TAGGED, ["rev-list", "--reverse", "HEAD"]),
    # reflog
    ("reflog", TAGGED, ["reflog"]),
    ("reflog-n1", TAGGED, ["reflog", "-1"]),
    # describe (annotated-only by default)
    ("describe-no-annotated", BASE + [["tag", "light"]], ["describe"]),
    ("describe-annotated", BASE + [["tag", "-a", "v1", "-m", "v1"]], ["describe"]),
    ("describe-no-tags", BASE, ["describe"]),
    ("describe-tags-flag", BASE + [["tag", "light"]], ["describe", "--tags"]),
    # cherry-pick / revert (author preservation -> identical object ids)
    ("cherry-pick",
     BASE + [["checkout", "-b", "topic"], ("write", "c.txt", "c\n"), ["add", "-A"],
             ["commit", "-m", "add c"], ["checkout", "main"]],
     ["cherry-pick", "topic"]),
    ("revert", TAGGED, ["revert", "--no-edit", "HEAD"]),
    # hash-object / name-rev
    ("hash-object-file", BASE + [("write", "f.txt", "hello\n")], ["hash-object", "f.txt"]),
    ("name-rev", BASE, ["name-rev", "HEAD"]),
    # branch move/copy
    ("branch-move", BASE + [["branch", "old"]], ["branch", "-m", "old", "new"]),
    ("branch-move-current", BASE, ["branch", "-m", "renamed"]),
    ("branch-copy", BASE + [["branch", "src"]], ["branch", "-c", "src", "dst"]),
    # checkout path restore
    ("checkout-path", BASE + [("write", "a.txt", "changed\n")], ["checkout", "--", "a.txt"]),
    ("restore-path", BASE + [("write", "a.txt", "changed\n")], ["restore", "a.txt"]),
    ("checkout-detached", TAGGED, ["checkout", "HEAD~1"]),
    # tag list with pattern
    ("tag-list-pattern", BASE + [["tag", "v1.0"], ["tag", "v2.0"], ["tag", "other"]], ["tag", "-l", "v*"]),
    # update-ref
    ("update-ref-create", BASE, ["update-ref", "refs/heads/newb", "HEAD"]),
    ("update-ref-delete", BASE + [["branch", "tmp"]], ["update-ref", "-d", "refs/heads/tmp"]),
    # diff-tree -p
    ("diff-tree-patch", TAGGED, ["diff-tree", "-p", "HEAD"]),
    # grep
    ("grep", BASE, ["grep", "alpha"]),
    ("grep-n", BASE, ["grep", "-n", "alpha"]),
    ("grep-l", BASE, ["grep", "-l", "beta"]),
    ("grep-c", BASE + [("write", "a.txt", "x\nx\ny\n"), ["add", "-A"], ["commit", "-m", "c2"]], ["grep", "-c", "x"]),
    ("grep-nomatch", BASE, ["grep", "zzzzz"]),
    # status untracked mode
    ("status-uno", BASE + [("write", "u.txt", "u\n")], ["status", "-uno"]),
    # ls-tree -l, cat-file --batch
    ("ls-tree-long", BASE, ["ls-tree", "-l", "HEAD"]),
    # log -p / --stat
    ("log-patch", TAGGED, ["log", "-p", "-1"]),
    ("log-stat", TAGGED, ["log", "--stat", "-1"]),
    # stash
    ("stash-push", BASE + [("write", "a.txt", "WIP\n")], ["stash"]),
    ("stash-list",
     BASE + [("write", "a.txt", "WIP\n"), ["stash"]], ["stash", "list"]),
]


CASES_WITH_STDIN: list[tuple] = [
    ("cat-file-batch-check", BASE, ["cat-file", "--batch-check"], "HEAD\n"),
    ("cat-file-batch", BASE, ["cat-file", "--batch-check"], "HEAD\nmissingobj\n"),
    ("cat-file-batch-content", BASE, ["cat-file", "--batch"], "HEAD:a.txt\n"),
]


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_behavior_parity(case, tmp_path: Path, git_254_oracle: str):
    _id, setup, probe = case
    assert_command_parity(git_254_oracle, tmp_path, setup, probe)


@pytest.mark.parametrize("case", CASES_WITH_STDIN, ids=[c[0] for c in CASES_WITH_STDIN])
def test_behavior_parity_stdin(case, tmp_path: Path, git_254_oracle: str):
    _id, setup, probe, stdin = case
    assert_command_parity(git_254_oracle, tmp_path, setup, probe, stdin=stdin)
