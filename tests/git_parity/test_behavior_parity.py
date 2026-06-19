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

# A non-linear history with a real merge commit (byte-identical SHAs because
# author/committer identity and dates are pinned by the deterministic env).
MERGED = BASE + [
    ["checkout", "-b", "feat"],
    ("write", "g.txt", "feature\n"),
    ["add", "-A"],
    ["commit", "-m", "feat-commit"],
    ["checkout", "main"],
    ("write", "h.txt", "mainline\n"),
    ["add", "-A"],
    ["commit", "-m", "main-commit"],
    ["merge", "--no-ff", "-m", "merge feat", "feat"],
]

# History with an annotated tag below a merge, for describe/name-rev/points-at.
DESC = BASE + [
    ["tag", "-a", "-m", "rel one", "v1"],
    ["checkout", "-b", "feat"], ("write", "g.txt", "x\n"), ["add", "-A"], ["commit", "-m", "fc"],
    ["checkout", "main"], ("write", "h.txt", "y\n"), ["add", "-A"], ["commit", "-m", "mc"],
    ["merge", "--no-ff", "-m", "merge feat", "feat"],
]
# A committed file modified in the working tree, plus a separately-staged file.
DIRTYIDX = BASE + [("write", "a.txt", "changed in wt\n"), ("write", "n.txt", "n\n"), ["add", "n.txt"]]
# Several branches and version-like tags, for --sort coverage.
REFSET = BASE + [["branch", "zzz"], ["branch", "aaa"], ["tag", "v2"], ["tag", "v10"], ["tag", "v1"]]
# A subdirectory with multiple entries, for ls-tree pathspec coverage.
SUBTREE = BASE + [("write", "sub/c.txt", "s\n"), ("write", "sub/d.txt", "s2\n"),
                  ["add", "-A"], ["commit", "-m", "c2"]]
# Linear history touching different paths, for rev-list pathspec coverage.
PATHHIST = BASE + [("write", "a.txt", "mod\n"), ["add", "-A"], ["commit", "-m", "c2"],
                   ("write", "c.txt", "x\n"), ["add", "-A"], ["commit", "-m", "c3"]]

# A commit with a single-digit day of month and a non-UTC offset, to exercise
# date rendering (unpadded day, ``+05:30`` style strict-ISO offsets).
DATED = [
    ("write", "a.txt", "alpha\n"), ["add", "-A"],
    ["commit", "-m", "dated", "--date", "2005-04-07T22:13:13 +0530"],
]

# A commit with a multi-paragraph message and punctuation, for the %b/%B/%f
# (body / raw body / sanitized subject) placeholders.
BODY = [
    ("write", "a.txt", "alpha\n"), ["add", "-A"],
    ["commit", "-m", "Subject: with punctuation!", "-m", "body para1\nbody para2"],
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
    # rev-parse ref expansion
    ("rev-parse-all", BASE + [["branch", "b1"], ["tag", "t1"]], ["rev-parse", "--all"]),
    ("rev-parse-branches", BASE + [["branch", "b1"]], ["rev-parse", "--branches"]),
    ("rev-parse-tags", BASE + [["tag", "t1"]], ["rev-parse", "--tags"]),
    # diff numstat/shortstat/name-status
    ("diff-numstat", BASE + [("write", "a.txt", "alpha\nmore\n")], ["diff", "--numstat"]),
    ("diff-shortstat", BASE + [("write", "a.txt", "alpha\nmore\nx\n")], ["diff", "--shortstat"]),
    ("diff-name-status", BASE + [("write", "c.txt", "c\n"), ["add", "-A"]], ["diff", "--cached", "--name-status"]),
    # log name flags
    ("log-name-status", TAGGED, ["log", "--name-status", "-1"]),
    ("log-name-only", TAGGED, ["log", "--name-only", "-1"]),
    # branch -v
    ("branch-verbose", BASE + [["branch", "feat"]], ["branch", "-v"]),
    # rebase up to date
    ("rebase-up-to-date", TAGGED, ["rebase", "HEAD"]),
    # rev-list --objects
    ("rev-list-objects", BASE, ["rev-list", "--objects", "HEAD"]),
    # annotated tag inspection
    ("cat-file-tag", BASE + [["tag", "-a", "v1", "-m", "ann msg"]], ["cat-file", "-p", "v1"]),
    # rev-parse object format / common dir
    ("rev-parse-object-format", BASE, ["rev-parse", "--show-object-format"]),
    ("rev-parse-common-dir", BASE, ["rev-parse", "--git-common-dir"]),
    # check-ref-format
    ("check-ref-format-valid", [], ["check-ref-format", "refs/heads/main"]),
    ("check-ref-format-invalid", [], ["check-ref-format", "refs/heads/..bad"]),
    ("check-ref-format-branch", [], ["check-ref-format", "--branch", "feature"]),
    # check-ignore
    ("check-ignore",
     BASE + [("write", ".gitignore", "*.log\n"), ("write", "x.log", "x\n")], ["check-ignore", "x.log"]),
    ("check-ignore-verbose",
     BASE + [("write", ".gitignore", "*.log\n"), ("write", "x.log", "x\n")], ["check-ignore", "-v", "x.log"]),
    # hash-object multiple, update-ref -m, worktree list
    ("hash-object-multi", [("write", "f1", "a\n"), ("write", "f2", "b\n")], ["hash-object", "f1", "f2"]),
    ("update-ref-message",
     BASE + [["update-ref", "-m", "test msg", "refs/heads/main", "HEAD"]], ["reflog"]),
    ("worktree-list", BASE, ["worktree", "list"]),
    # blame
    ("blame", BASE + [("write", "a.txt", "x\ny\nz\n"), ["add", "-A"], ["commit", "-m", "c2"]], ["blame", "a.txt"]),
    # merge-base
    ("merge-base", TAGGED, ["merge-base", "HEAD", "HEAD~1"]),
    ("merge-base-is-ancestor-yes", TAGGED, ["merge-base", "--is-ancestor", "HEAD~1", "HEAD"]),
    ("merge-base-is-ancestor-no", TAGGED, ["merge-base", "--is-ancestor", "HEAD", "HEAD~1"]),
    # rev-list / log
    ("rev-list-parents", TAGGED, ["rev-list", "--parents", "HEAD"]),
    ("log-reverse", TAGGED, ["log", "--oneline", "--reverse"]),
    ("rev-parse-parents", TAGGED, ["rev-parse", "HEAD^@"]),
    # name-rev / show tag / diff raw / cat-file type form
    ("name-rev-name-only", BASE, ["name-rev", "--name-only", "HEAD"]),
    ("show-tag", BASE + [["tag", "-a", "v1", "-m", "tagmsg"]], ["show", "v1"]),
    ("diff-raw", BASE + [("write", "a.txt", "X\n")], ["diff", "--raw"]),
    ("cat-file-type-form", BASE, ["cat-file", "commit", "HEAD"]),
    # rev-parse abbrev/path
    ("rev-parse-short4", BASE, ["rev-parse", "--short=4", "HEAD"]),
    ("rev-parse-index-path", BASE, ["rev-parse", ":a.txt"]),
    ("rev-parse-tree-peel", BASE, ["rev-parse", "HEAD^{tree}"]),
    # show --format
    ("show-format", BASE, ["show", "-s", "--format=%H"]),
    # ls-files --error-unmatch
    ("ls-files-error-unmatch-ok", BASE, ["ls-files", "--error-unmatch", "a.txt"]),
    ("ls-files-error-unmatch-fail", BASE, ["ls-files", "--error-unmatch", "nope.txt"]),
    # tag -n
    ("tag-n", BASE + [["tag", "-a", "v1", "-m", "ann line"]], ["tag", "-n"]),
    # check-attr
    ("check-attr",
     BASE + [("write", ".gitattributes", "*.txt text\n")], ["check-attr", "text", "a.txt"]),
    # config typed/regexp/name-only
    ("config-get-regexp",
     BASE + [["config", "user.name", "X"], ["config", "user.email", "x@e"]],
     ["config", "--get-regexp", "user.*"]),
    ("config-name-only-list",
     BASE + [["config", "user.name", "X"]], ["config", "--name-only", "--list"]),
    ("config-int", BASE + [["config", "core.x", "42"]], ["config", "--int", "core.x"]),
    ("config-bool", BASE + [["config", "core.y", "true"]], ["config", "--bool", "core.y"]),
    # show-ref --hash, symbolic-ref -d, update-index --chmod
    ("show-ref-hash", BASE + [["branch", "feat"]], ["show-ref", "--hash"]),
    ("symbolic-ref-delete-missing", BASE, ["symbolic-ref", "-d", "refs/heads/nope"]),
    ("for-each-ref-count",
     BASE + [["branch", "a"], ["branch", "b"]], ["for-each-ref", "--count=1", "--format=%(refname)"]),
    # notes
    ("notes-show", BASE + [["notes", "add", "-m", "a note"]], ["notes", "show"]),
    # diff-files / diff-index raw, diff --quiet
    ("diff-files", BASE + [("write", "a.txt", "CHANGED\n")], ["diff-files"]),
    ("diff-files-name-only", BASE + [("write", "a.txt", "X\n")], ["diff-files", "--name-only"]),
    ("diff-index-cached", BASE + [("write", "b.txt", "b\n"), ["add", "b.txt"]], ["diff-index", "--cached", "HEAD"]),
    ("diff-quiet-clean", BASE, ["diff", "--quiet"]),
    ("diff-quiet-dirty", BASE + [("write", "a.txt", "x\n")], ["diff", "--quiet"]),
    # rev-parse --verify guard, rev-list --no-walk, log --first-parent
    ("rev-parse-verify-multi", BASE, ["rev-parse", "--verify", "HEAD", "HEAD"]),
    ("rev-list-no-walk", TAGGED, ["rev-list", "--no-walk", "HEAD"]),
    ("log-first-parent", TAGGED, ["log", "--first-parent", "--oneline"]),
    # cherry, tag --sort
    ("cherry",
     BASE + [["checkout", "-b", "topic"], ("write", "c.txt", "c\n"), ["add", "-A"],
             ["commit", "-m", "t"]],
     ["cherry", "main"]),
    ("tag-sort-version",
     BASE + [["tag", "v2"], ["tag", "v1"], ["tag", "v10"]],
     ["tag", "--sort=version:refname"]),
    # rename detection (exact and partial similarity via the spanhash estimator)
    ("status-rename-porcelain",
     BASE + [("write", "r.txt", "L1\nL2\nL3\nL4\n"), ["add", "-A"], ["commit", "-m", "c2"],
             ("rm", "r.txt"), ("write", "moved.txt", "L1\nL2\nL3\nL4\n"), ["add", "-A"]],
     ["status", "--porcelain"]),
    ("status-rename-long",
     BASE + [("write", "r.txt", "L1\nL2\nL3\nL4\n"), ["add", "-A"], ["commit", "-m", "c2"],
             ("rm", "r.txt"), ("write", "moved.txt", "L1\nL2\nL3\nL4\n"), ["add", "-A"]],
     ["status"]),
    ("diff-rename-name-status",
     BASE + [("write", "r.txt", "L1\nL2\nL3\nL4\nL5\n"), ["add", "-A"], ["commit", "-m", "c2"],
             ("rm", "r.txt"), ("write", "moved.txt", "L1\nL2\nL3X\nL4\nL5\n"), ["add", "-A"]],
     ["diff", "--cached", "-M", "--name-status"]),
    ("diff-rename-patch",
     BASE + [("write", "r.txt", "L1\nL2\nL3\nL4\n"), ["add", "-A"], ["commit", "-m", "c2"],
             ("rm", "r.txt"), ("write", "moved.txt", "L1\nL2\nL3\nL4\n"), ["add", "-A"]],
     ["diff", "--cached", "-M"]),
    ("diff-rename-stat",
     BASE + [("write", "r.txt", "L1\nL2\nL3\nL4\n"), ["add", "-A"], ["commit", "-m", "c2"],
             ("rm", "r.txt"), ("write", "moved.txt", "L1\nL2\nL3\nL4\n"), ["add", "-A"]],
     ["diff", "--cached", "-M", "--stat"]),
    # reset pathspec (unstage)
    ("reset-path",
     BASE + [("write", "b.txt", "b\n"), ["add", "b.txt"]], ["reset", "HEAD", "b.txt"]),
    ("reset-path-status",
     BASE + [("write", "b.txt", "b\n"), ["add", "b.txt"], ["reset", "HEAD", "b.txt"]],
     ["status", "--porcelain"]),
    # commit --amend --no-edit, rm -r, merge --abort
    ("commit-amend-no-edit",
     BASE + [("write", "c.txt", "c\n"), ["add", "c.txt"]], ["commit", "--amend", "--no-edit"]),
    ("rm-recursive",
     BASE + [("write", "dir/x.txt", "x\n"), ["add", "-A"], ["commit", "-m", "c2"]], ["rm", "-r", "dir"]),
    ("merge-abort-none", BASE, ["merge", "--abort"]),
    ("branch-contains", TAGGED + [["branch", "feat"]], ["branch", "--contains", "HEAD~1"]),
    # log range and path limiting
    ("log-range", TAGGED, ["log", "--oneline", "HEAD~1..HEAD"]),
    ("log-path",
     BASE + [("write", "a.txt", "changed\n"), ["add", "-A"], ["commit", "-m", "c2"]],
     ["log", "--oneline", "--", "a.txt"]),
    ("log-path-other",
     BASE + [("write", "a.txt", "changed\n"), ["add", "-A"], ["commit", "-m", "c2"]],
     ["log", "--oneline", "--", "b.txt"]),
    # misc
    ("commit-multi-message",
     BASE + [("write", "x.txt", "x\n"), ["add", "x.txt"]], ["commit", "-m", "subject", "-m", "body"]),
    ("branch-delete-current", BASE, ["branch", "-d", "main"]),
    ("log-max-count-zero", TAGGED, ["log", "--oneline", "-0"]),
    ("diff-tree-root", BASE, ["diff-tree", "--root", "-r", "HEAD"]),
    ("rev-parse-abbrev-ref-at", BASE, ["rev-parse", "--abbrev-ref", "@"]),
    ("status-after-rm-cached", BASE + [["rm", "--cached", "a.txt"]], ["status", "--porcelain"]),
    # format-patch (single patch --stdout is deterministic)
    ("format-patch-stdout", TAGGED, ["format-patch", "-1", "--stdout"]),
    ("format-patch-newfile",
     BASE + [("write", "n.txt", "fresh\n"), ["add", "-A"], ["commit", "-m", "add new"]],
     ["format-patch", "-1", "--stdout"]),
    # apply
    ("apply-check",
     BASE + [("write", "p.diff",
              "--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-alpha\n+ALPHA\n")],
     ["apply", "--check", "p.diff"]),
    ("apply-numstat",
     BASE + [("write", "p.diff",
              "--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-alpha\n+ALPHA\n")],
     ["apply", "--numstat", "p.diff"]),
    ("apply-bad",
     BASE + [("write", "p.diff",
              "--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-WRONG\n+ALPHA\n")],
     ["apply", "--check", "p.diff"]),
    # am (applies a patch and records a commit; "Applying:" line + exit code)
    ("am",
     BASE + [("write", "patch.mbox",
              "From 0000000000000000000000000000000000000000 Mon Sep 17 00:00:00 2001\n"
              "From: Parity <parity@example.com>\n"
              "Date: Tue, 14 Nov 2023 22:13:20 +0000\n"
              "Subject: [PATCH] patched\n\n---\n a.txt | 2 +-\n"
              " 1 file changed, 1 insertion(+), 1 deletion(-)\n\n"
              "diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n"
              "@@ -1 +1 @@\n-alpha\n+ALPHA\n-- \n2.54.0\n\n")],
     ["am", "patch.mbox"]),
    # fast-export stream
    ("fast-export", TAGGED, ["fast-export", "HEAD"]),
    # plumbing
    ("ls-tree-full-tree",
     BASE + [("write", "d/x.txt", "y\n"), ["add", "-A"], ["commit", "-m", "c2"]],
     ["ls-tree", "--full-tree", "-r", "HEAD"]),
    ("rev-parse-git-path", BASE, ["rev-parse", "--git-path", "objects"]),
    # ls-tree --abbrev: bare flag leaves the treeish alone; '=' attaches a width.
    ("ls-tree-abbrev", BASE, ["ls-tree", "--abbrev", "HEAD"]),
    ("ls-tree-abbrev-n", BASE, ["ls-tree", "--abbrev=4", "HEAD"]),
    ("ls-tree-abbrev-long", BASE, ["ls-tree", "--abbrev", "-l", "HEAD"]),
    # rev-parse with a caret-prefixed revision is echoed with the '^'.
    ("rev-parse-caret", TAGGED, ["rev-parse", "^HEAD"]),
    # rev-list ranges and explicit exclusions.
    ("rev-list-range", TAGGED, ["rev-list", "HEAD~1..HEAD"]),
    ("rev-list-exclude", TAGGED, ["rev-list", "HEAD", "^v1~1"]),
    # show --raw
    ("show-raw", TAGGED, ["show", "--raw", "HEAD"]),
    # log --raw and --no-merges
    ("log-raw", TAGGED, ["log", "--raw"]),
    # whatchanged is removed in 2.54 without an explicit opt-in.
    ("whatchanged-refused", TAGGED, ["whatchanged"]),
    ("whatchanged-opt-in", TAGGED, ["whatchanged", "--i-still-use-this"]),
    # commit --date: single-digit day exercises the unpadded Date: formatting.
    ("commit-date-single-digit-day",
     [("write", "f.txt", "x\n"), ["add", "-A"],
      ["commit", "-m", "dated", "--date", "2005-04-07T22:13:13 +0000"]],
     ["log", "-1"]),
    ("commit-amend-date",
     BASE + [["commit", "--amend", "--no-edit", "--date", "2005-04-07T22:13:13 +0000"]],
     ["log", "-1", "--format=%ad|%cd"]),
    # RFC2822 dates (format-patch) also leave the day of month unpadded.
    ("format-patch-single-digit-day",
     [("write", "f.txt", "x\n"), ["add", "-A"],
      ["commit", "-m", "dated", "--date", "2005-04-07T22:13:13 +0000"]],
     ["format-patch", "-1", "--stdout"]),
    # log --format date placeholders (fixed styles + --date-driven %ad/%cd).
    ("log-fmt-aD", DATED, ["log", "-1", "--format=%aD"]),
    ("log-fmt-ai", DATED, ["log", "-1", "--format=%ai"]),
    ("log-fmt-aI", DATED, ["log", "-1", "--format=%aI"]),
    ("log-fmt-at", DATED, ["log", "-1", "--format=%at"]),
    ("log-fmt-cI", DATED, ["log", "-1", "--format=%cI"]),
    # log --date=<mode> drives both %ad and the medium "Date:" line.
    ("log-date-iso", DATED, ["log", "-1", "--date=iso", "--format=%ad"]),
    ("log-date-iso-strict", DATED, ["log", "-1", "--date=iso-strict", "--format=%ad"]),
    ("log-date-rfc", DATED, ["log", "-1", "--date=rfc", "--format=%ad"]),
    ("log-date-short", DATED, ["log", "-1", "--date=short", "--format=%ad"]),
    ("log-date-raw", DATED, ["log", "-1", "--date=raw", "--format=%ad"]),
    ("log-date-unix", DATED, ["log", "-1", "--date=unix", "--format=%ad"]),
    ("log-date-short-medium", DATED, ["log", "-1", "--date=short"]),
    # Body/raw-body/sanitized-subject placeholders.
    ("log-fmt-body", BODY, ["log", "-1", "--format=%b"]),
    ("log-fmt-rawbody", BODY, ["log", "-1", "--format=%B"]),
    ("log-fmt-sanitized", BODY, ["log", "-1", "--format=%f"]),
    ("log-fmt-subject-body", BODY, ["log", "-1", "--format=%s%n%b"]),
    # format: separates entries; tformat:/--format terminate each entry.
    ("log-pretty-format-sep", TAGGED, ["log", "--pretty=format:%s"]),
    ("log-pretty-tformat", TAGGED, ["log", "--pretty=tformat:%s"]),
    # full/fuller pretty styles (Author/Commit; AuthorDate/CommitDate).
    ("log-pretty-full", DATED, ["log", "-1", "--pretty=full"]),
    ("log-pretty-fuller", DATED, ["log", "-1", "--pretty=fuller"]),
    ("show-pretty-fuller", DATED, ["show", "-s", "--pretty=fuller", "HEAD"]),
    ("show-pretty-full", DATED, ["show", "-s", "--pretty=full", "HEAD"]),
    ("show-date-short", DATED, ["show", "-s", "--date=short", "HEAD"]),
    ("show-fuller-date-iso", DATED, ["show", "-s", "--pretty=fuller", "--date=iso", "HEAD"]),
    # Remaining --date=<mode> styles (relative is time-based but both binaries
    # evaluate it within the same second, so the rendered string still matches).
    ("log-date-relative", DATED, ["log", "-1", "--date=relative", "--format=%ad"]),
    ("log-date-relative-ar", DATED, ["log", "-1", "--format=%ar"]),
    ("log-date-human", DATED, ["log", "-1", "--date=human", "--format=%ad"]),
    ("log-date-local", DATED, ["log", "-1", "--date=local", "--format=%ad"]),
    ("log-date-iso-local", DATED, ["log", "-1", "--date=iso-local", "--format=%ad"]),
    ("log-date-format", DATED, ["log", "-1", "--date=format:%Y/%m/%d %H:%M", "--format=%ad"]),
    # short/raw/reference pretty styles.
    ("log-pretty-short", DATED, ["log", "-1", "--pretty=short"]),
    ("log-pretty-raw", DATED, ["log", "-1", "--pretty=raw"]),
    ("log-pretty-reference", TAGGED, ["log", "--pretty=reference"]),
    ("show-pretty-raw", DATED, ["show", "-s", "--pretty=raw", "HEAD"]),
    # diff --summary (create/delete/mode-change).
    ("diff-summary",
     BASE + [("write", "n.txt", "new\n"), ("rm", "b.txt"), ["add", "-A"], ["commit", "-m", "c2"]],
     ["diff", "--summary", "HEAD~1", "HEAD"]),
    ("diff-stat-summary",
     BASE + [("write", "n.txt", "new\n"), ("rm", "b.txt"), ["add", "-A"], ["commit", "-m", "c2"]],
     ["diff", "--stat", "--summary", "HEAD~1", "HEAD"]),
    # --all walks every ref; --topo-order / --graph reorder topologically.
    ("log-all-oneline", MERGED + [["tag", "v1", "HEAD~1"]], ["log", "--all", "--oneline"]),
    ("log-topo-order", MERGED, ["log", "--topo-order", "--oneline"]),
    ("log-graph-linear", TAGGED, ["log", "--graph", "--oneline"]),
    ("log-graph-linear-medium", TAGGED, ["log", "--graph"]),
    ("log-graph-merge", MERGED, ["log", "--graph", "--oneline"]),
    ("log-graph-merge-medium", MERGED, ["log", "--graph"]),
    ("log-graph-merge-all", MERGED, ["log", "--graph", "--oneline", "--all"]),
    ("log-graph-reverse-error", TAGGED, ["log", "--graph", "--reverse"]),
    # Unsigned commits report %G? as "N" with empty detail fields.
    ("log-fmt-sig", TAGGED, ["log", "-1", "--format=%G?|%GG|%GS|%GK"]),
    # shortlog -s pads the per-author count to a 6-column field.
    ("shortlog-summary", TAGGED, ["shortlog", "-s", "-n", "HEAD"]),
    # log --shortstat, --no-color (accepted/ignored), --abbrev-commit.
    ("log-shortstat", TAGGED, ["log", "--shortstat"]),
    ("log-no-color", TAGGED, ["log", "-1", "--no-color"]),
    ("log-abbrev-commit-oneline", TAGGED, ["log", "--abbrev-commit", "--pretty=oneline"]),
    ("log-abbrev-commit-medium", TAGGED, ["log", "-1", "--abbrev-commit"]),
    # Decorations: annotated tags peel to the commit; git orders them
    # reverse-alphabetically with the HEAD branch hoisted to the front. %d/%D
    # expand even without --decorate.
    ("log-decorate-annotated-tag",
     BASE + [["tag", "-a", "-m", "rel", "v1"], ["branch", "zeta"], ["branch", "alpha"]],
     ["log", "-1", "--oneline", "--decorate"]),
    ("log-fmt-decoration",
     BASE + [["tag", "-a", "-m", "rel", "v1"], ["branch", "zeta"]],
     ["log", "-1", "--format=%d"]),
    ("log-fmt-decoration-bare",
     BASE + [["tag", "v1"], ["branch", "zeta"]],
     ["log", "-1", "--format=%D"]),
    # --format=<builtin> is an alias for --pretty=<builtin>, not a literal.
    ("log-format-builtin-fuller", DATED, ["log", "-1", "--format=fuller"]),
    ("log-format-builtin-oneline", TAGGED, ["log", "--format=oneline"]),
    # Merge-commit rendering: combined diff is empty for a clean merge, so the
    # default patch/raw output is suppressed; --stat reports vs the first parent.
    ("merge-log-raw", MERGED, ["log", "--raw"]),
    ("merge-log-patch", MERGED, ["log", "-p"]),
    ("merge-log-stat", MERGED, ["log", "--stat"]),
    ("merge-log-no-merges", MERGED, ["log", "--oneline", "--no-merges"]),
    ("merge-show", MERGED, ["show", "HEAD"]),
    ("merge-show-stat", MERGED, ["show", "--stat", "HEAD"]),
    ("merge-show-raw", MERGED, ["show", "--raw", "HEAD"]),
    ("merge-whatchanged", MERGED, ["whatchanged", "--i-still-use-this"]),
    # Merge commit object is byte-identical (rev-parse exposes the SHA).
    ("merge-head-sha", MERGED, ["rev-parse", "HEAD"]),
    ("merge-default-message", BASE + [
        ["checkout", "-b", "feat"], ("write", "g.txt", "f\n"), ["add", "-A"],
        ["commit", "-m", "fc"], ["checkout", "main"], ("write", "h.txt", "m\n"),
        ["add", "-A"], ["commit", "-m", "mc"], ["merge", "--no-ff", "feat"],
    ], ["log", "-1", "--format=%s"]),
    # Default merge subject names a tag and appends "into <dest>" for non-default
    # destination branches (main/master are suppressed).
    ("merge-tag-message", BASE + [
        ["checkout", "-b", "feat"], ("write", "g.txt", "f\n"), ["add", "-A"],
        ["commit", "-m", "fc"], ["tag", "tg"],
        ["checkout", "main"], ("write", "h.txt", "m\n"),
        ["add", "-A"], ["commit", "-m", "mc"], ["merge", "--no-ff", "tg"],
    ], ["log", "-1", "--format=%s"]),
    # checkout -b from an unborn HEAD repoints HEAD without creating a ref.
    ("checkout-b-unborn", [["checkout", "-b", "develop"]],
     ["rev-parse", "--abbrev-ref", "HEAD"]),
    ("checkout-b-unborn-status", [["checkout", "-b", "develop"]],
     ["status", "--porcelain=v2", "--branch"]),
    ("switch-c-unborn", [["switch", "-c", "topic"]],
     ["symbolic-ref", "HEAD"]),
    # An explicit invalid start-point still fails.
    ("checkout-b-bad-start", BASE, ["checkout", "-b", "x", "nope"]),
    ("merge-into-nondefault", BASE + [
        ["checkout", "-b", "develop"],
        ["checkout", "-b", "feat"], ("write", "g.txt", "f\n"), ["add", "-A"],
        ["commit", "-m", "fc"], ["checkout", "develop"], ("write", "h.txt", "m\n"),
        ["add", "-A"], ["commit", "-m", "mc"], ["merge", "--no-ff", "feat"],
    ], ["log", "-1", "--format=%s"]),
    # describe counts every commit since the tag (including a merge's 2nd parent).
    ("describe", DESC, ["describe"]),
    ("describe-tags", DESC, ["describe", "--tags"]),
    ("describe-exact", DESC, ["describe", "v1"]),
    ("describe-always", DESC, ["describe", "--always", "v1"]),
    ("describe-bad-rev", DESC, ["describe", "nonexistent"]),
    ("describe-no-tags", BASE, ["describe"]),
    # name-rev tip selection and --tags labelling.
    ("name-rev-head", DESC, ["name-rev", "HEAD"]),
    ("name-rev-tags", DESC, ["name-rev", "--tags", "HEAD~1"]),
    ("name-rev-tags-nameonly", DESC, ["name-rev", "--tags", "--name-only", "HEAD~1"]),
    ("name-rev-bad", BASE, ["name-rev", "nope"]),
    # verify-commit/verify-tag fail on unsigned/non-tag objects.
    ("verify-commit-unsigned", BASE, ["verify-commit", "HEAD"]),
    ("verify-tag-unsigned", BASE + [["tag", "-a", "-m", "x", "v1"]], ["verify-tag", "v1"]),
    ("verify-tag-lightweight", BASE + [["tag", "lw"]], ["verify-tag", "lw"]),
    # diff-tree suppresses a merge's (combined) diff entirely.
    ("diff-tree-merge", MERGED, ["diff-tree", "HEAD"]),
    ("diff-tree-nonmerge",
     BASE + [("write", "a.txt", "z\n"), ["add", "-A"], ["commit", "-m", "c2"]],
     ["diff-tree", "-r", "HEAD"]),
    # diff-index compares the tree to the work tree (dirty blob ids zeroed).
    ("diff-index-worktree", DIRTYIDX, ["diff-index", "HEAD"]),
    ("diff-index-cached", DIRTYIDX, ["diff-index", "--cached", "HEAD"]),
    ("diff-index-name-only", DIRTYIDX, ["diff-index", "--name-only", "HEAD"]),
    # for-each-ref --sort (refname, reverse, version).
    ("fer-sort-refname", REFSET, ["for-each-ref", "--sort=refname"]),
    ("fer-sort-rev", REFSET, ["for-each-ref", "--sort=-refname"]),
    ("fer-sort-version", REFSET, ["for-each-ref", "--sort=v:refname", "refs/tags"]),
    # ls-tree pathspec filtering.
    ("ls-tree-path-dir", SUBTREE, ["ls-tree", "HEAD", "sub"]),
    ("ls-tree-path-recurse", SUBTREE, ["ls-tree", "-r", "HEAD", "sub/"]),
    ("ls-tree-path-file", SUBTREE, ["ls-tree", "HEAD", "sub/c.txt"]),
    # rev-list pathspec, dates, and pretty/oneline/format output.
    ("rev-list-path", PATHHIST, ["rev-list", "HEAD", "--", "a.txt"]),
    ("rev-list-pretty-oneline", TAGGED, ["rev-list", "--pretty=oneline", "HEAD"]),
    ("rev-list-oneline", TAGGED, ["rev-list", "--oneline", "HEAD"]),
    ("rev-list-format", TAGGED, ["rev-list", "--format=%H", "HEAD"]),
    ("rev-list-since", TAGGED, ["rev-list", "--since=2000-01-01", "HEAD"]),
    # rev-parse output filters.
    ("rev-parse-no-revs", BASE, ["rev-parse", "--no-revs", "HEAD"]),
    ("rev-parse-revs-only", BASE, ["rev-parse", "--revs-only", "--foo", "HEAD"]),
    # log --pretty=<format-with-%> and --parents.
    ("log-pretty-pct", TAGGED, ["log", "-1", "--pretty=%H %s"]),
    ("log-parents-oneline", MERGED, ["log", "--oneline", "--parents"]),
    ("log-parents-medium", MERGED, ["log", "-1", "--parents"]),
    # tag --points-at and branch --sort.
    ("tag-points-at", DESC, ["tag", "--points-at", "HEAD~1"]),
    ("branch-sort", BASE + [["branch", "zzz"], ["branch", "aaa"]], ["branch", "--sort=refname"]),
    ("branch-sort-rev", BASE + [["branch", "zzz"], ["branch", "aaa"]], ["branch", "--sort=-refname"]),
    # log commit filters: --grep/--author/-i/--merges/--min-parents/-S/-G/--since.
    ("log-grep", TAGGED, ["log", "--grep=second", "--oneline"]),
    ("log-grep-none", TAGGED, ["log", "--grep=zzzznope", "--oneline"]),
    ("log-grep-ignorecase", TAGGED, ["log", "-i", "--grep=SECOND", "--oneline"]),
    ("log-author", TAGGED, ["log", "--author=Parity", "--oneline"]),
    ("log-author-none", TAGGED, ["log", "--author=nobody", "--oneline"]),
    ("log-merges", MERGED, ["log", "--merges", "--oneline"]),
    ("log-min-parents", MERGED, ["log", "--min-parents=2", "--oneline"]),
    ("log-max-parents", MERGED, ["log", "--max-parents=1", "--oneline"]),
    ("log-grep-maxcount", TAGGED, ["log", "-n", "1", "--no-merges", "--oneline"]),
    ("log-pickaxe-s", PATHHIST, ["log", "-S", "mod", "--oneline"]),
    ("log-pickaxe-g", PATHHIST, ["log", "-G", "mod", "--oneline"]),
    ("log-since", TAGGED, ["log", "--since=2000-01-01", "--oneline"]),
    ("log-until", TAGGED, ["log", "--until=2000-01-01", "--oneline"]),
    # rev-list shares the commit filters (but not the -S/-G diff options).
    ("rev-list-grep", TAGGED, ["rev-list", "--grep=second", "HEAD"]),
    ("rev-list-author", TAGGED, ["rev-list", "--author=Parity", "HEAD"]),
    ("rev-list-merges", MERGED, ["rev-list", "--merges", "HEAD"]),
    ("rev-list-max-parents-0", MERGED, ["rev-list", "--max-parents=0", "HEAD"]),
    # show file-list and oneline output.
    ("show-name-only",
     BASE + [("write", "a.txt", "x\n"), ["add", "-A"], ["commit", "-m", "c2"]],
     ["show", "--name-only", "HEAD"]),
    ("show-name-status",
     BASE + [("write", "a.txt", "x\n"), ["add", "-A"], ["commit", "-m", "c2"]],
     ["show", "--name-status", "HEAD"]),
    ("show-oneline", TAGGED, ["show", "--oneline", "-s", "HEAD"]),
    # status -uno notes hidden untracked files when staged changes exist.
    ("status-uno-staged",
     BASE + [("write", "a.txt", "st\n"), ["add", "a.txt"], ("write", "u.txt", "x\n")],
     ["status", "-uno"]),
    ("status-uno-unstaged",
     BASE + [("write", "a.txt", "wt\n"), ("write", "u.txt", "x\n")],
     ["status", "-uno"]),
    ("status-uno-clean", BASE + [("write", "u.txt", "x\n")], ["status", "-uno"]),
    # rev-parse output filter for a flag with no value.
    ("rev-parse-shared-index", BASE, ["rev-parse", "--shared-index-path"]),
    # rev-list symmetric difference and --left-right marking.
    ("rev-list-symmetric", MERGED, ["rev-list", "HEAD...HEAD~1"]),
    ("rev-list-left-right", MERGED, ["rev-list", "--left-right", "HEAD...HEAD~1"]),
    # %xHH byte escapes in format strings.
    ("log-format-hex", TAGGED, ["log", "-1", "--format=%h%x09%s"]),
    # config --default / --type.
    ("config-default", BASE, ["config", "--default", "X", "--get", "no.such"]),
    ("config-default-int", BASE, ["config", "--default", "5", "--type=int", "--get", "no.such"]),
    ("config-type-bool",
     BASE + [["config", "core.somebool", "yes"]],
     ["config", "--type=bool", "--get", "core.somebool"]),
    # describe --long / --abbrev.
    ("describe-long", DESC, ["describe", "--long"]),
    ("describe-abbrev4", DESC, ["describe", "--abbrev=4"]),
    ("describe-abbrev0", DESC, ["describe", "--abbrev=0"]),
    # ls-files --full-name / -t / --abbrev.
    ("ls-files-full-name", SUBTREE, ["ls-files", "--full-name"]),
    ("ls-files-t", SUBTREE, ["ls-files", "-t"]),
    ("ls-files-abbrev", BASE, ["ls-files", "-s", "--abbrev=8"]),
    # reflog default/oneline and per-branch.
    ("reflog-oneline", TAGGED, ["reflog", "--oneline"]),
    ("reflog-show-branch", TAGGED, ["reflog", "show", "main"]),
    # check-ref-format --normalize.
    ("crf-normalize", BASE, ["check-ref-format", "--normalize", "refs/heads//x"]),
    ("crf-normalize-lead", BASE, ["check-ref-format", "--normalize", "//refs/heads/x"]),
    ("crf-normalize-bad", BASE, ["check-ref-format", "--normalize", "refs/heads/x/"]),
    # diff-files --stat / --numstat / --shortstat (index vs work tree).
    ("diff-files-stat", BASE + [("write", "a.txt", "x\ny\n")], ["diff-files", "--stat"]),
    ("diff-files-numstat", BASE + [("write", "a.txt", "x\ny\n")], ["diff-files", "--numstat"]),
    # merge-tree (modern 2-arg real merge prints the result tree id).
    ("merge-tree-2arg", MERGED, ["merge-tree", "HEAD~1", "HEAD"]),
    ("merge-tree-branches", MERGED, ["merge-tree", "main", "feat"]),
    # rev-parse repo-introspection flags.
    ("rev-parse-is-shallow", BASE, ["rev-parse", "--is-shallow-repository"]),
    ("rev-parse-path-format-abs", BASE, ["rev-parse", "--path-format=absolute", "--git-dir"]),
    # symbolic-ref on a real (non-symbolic) ref.
    ("symbolic-ref-nonsym", BASE, ["symbolic-ref", "refs/heads/main"]),
    ("symbolic-ref-nonsym-q", BASE, ["symbolic-ref", "-q", "refs/heads/main"]),
    # log --abbrev sets the %h / oneline abbreviation length.
    ("log-abbrev-len", TAGGED, ["log", "--format=%h", "--abbrev=10"]),
    ("log-abbrev-oneline", TAGGED, ["log", "--oneline", "--abbrev=12"]),
    # diff -R reverses the diff (content and a//b prefixes).
    ("diff-reverse",
     BASE + [("write", "a.txt", "alpha\nmore\n"), ["add", "-A"], ["commit", "-m", "c2"]],
     ["diff", "-R", "HEAD~1", "HEAD"]),
    ("diff-reverse-stat",
     BASE + [("write", "a.txt", "alpha\nmore\n"), ["add", "-A"], ["commit", "-m", "c2"]],
     ["diff", "-R", "--stat", "HEAD~1", "HEAD"]),
    ("diff-reverse-newfile",
     BASE + [("write", "n.txt", "fresh\n"), ["add", "-A"], ["commit", "-m", "c2"]],
     ["diff", "-R", "HEAD~1", "HEAD"]),
    # branch --merged / --no-merged.
    ("branch-merged", MERGED, ["branch", "--merged"]),
    ("branch-no-merged",
     BASE + [["branch", "other"], ["checkout", "-b", "wip"], ("write", "w.txt", "w\n"),
             ["add", "-A"], ["commit", "-m", "wip"], ["checkout", "main"]],
     ["branch", "--no-merged"]),
    # diff --diff-filter and --stat=<width>.
    ("diff-filter-added",
     BASE + [("write", "n.txt", "x\n"), ("write", "a.txt", "alpha\nz\n"), ["add", "-A"],
             ["commit", "-m", "c2"]],
     ["diff", "--diff-filter=A", "--name-only", "HEAD~1", "HEAD"]),
    ("diff-stat-width",
     BASE + [("write", "a.txt", "alpha\nmore\n"), ["add", "-A"], ["commit", "-m", "c2"]],
     ["diff", "--stat=80", "HEAD~1", "HEAD"]),
    # worktree list --porcelain.
    ("worktree-list-porcelain", BASE, ["worktree", "list", "--porcelain"]),
]


CASES_WITH_STDIN: list[tuple] = [
    ("cat-file-batch-check", BASE, ["cat-file", "--batch-check"], "HEAD\n"),
    ("cat-file-batch", BASE, ["cat-file", "--batch-check"], "HEAD\nmissingobj\n"),
    ("cat-file-batch-content", BASE, ["cat-file", "--batch"], "HEAD:a.txt\n"),
    ("patch-id", BASE, ["patch-id"],
     "diff --git a/a.txt b/a.txt\nindex 814f4a4..ddc897f 100644\n--- a/a.txt\n+++ b/a.txt\n"
     "@@ -1,2 +1,3 @@\n one\n-two\n+TWO\n+three\n"),
    ("stripspace", BASE, ["stripspace"], "  hello  \n\n\n\nworld\n\n"),
    ("stripspace-comments", BASE, ["stripspace", "-s"], "# comment\nkeep\n"),
    ("hash-object-stdin-paths", BASE, ["hash-object", "--stdin-paths"], "a.txt\n"),
    ("cat-file-batch-command", BASE, ["cat-file", "--batch-command"],
     "info HEAD\ncontents HEAD:a.txt\n"),
]


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_behavior_parity(case, tmp_path: Path, git_254_oracle: str):
    _id, setup, probe = case
    assert_command_parity(git_254_oracle, tmp_path, setup, probe)


@pytest.mark.parametrize("case", CASES_WITH_STDIN, ids=[c[0] for c in CASES_WITH_STDIN])
def test_behavior_parity_stdin(case, tmp_path: Path, git_254_oracle: str):
    _id, setup, probe, stdin = case
    assert_command_parity(git_254_oracle, tmp_path, setup, probe, stdin=stdin)


def test_archive_tar_byte_identical(tmp_path: Path, git_254_oracle: str):
    """`git archive --format=tar` is deterministic; assert byte-for-byte parity."""
    import subprocess
    from tests.git_parity.support import DETERMINISTIC_ENV, ROOT, pygit_cmd

    setup = [
        ("write", "a.txt", "hello\n"),
        ("write", "run.sh", "#!/bin/sh\necho hi\n", 0o755),
        ("write", "d/b.txt", "world\n"),
        ("write", "d/nested/c.txt", "deep\n"),
    ]
    env = dict(__import__("os").environ)
    env.update(DETERMINISTIC_ENV)
    env["PYTHONPATH"] = str(ROOT)
    outputs = {}
    for tool, base in (("oracle", [git_254_oracle]), ("pygit", pygit_cmd())):
        repo = tmp_path / tool
        repo.mkdir()
        subprocess.run([*base, "init", "-b", "main", "."], cwd=repo, env=env, capture_output=True)
        for step in setup:
            path = repo / step[1]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(step[2])
            if len(step) > 3:
                path.chmod(step[3])
        subprocess.run([*base, "add", "-A"], cwd=repo, env=env, capture_output=True)
        subprocess.run([*base, "commit", "-m", "c"], cwd=repo, env=env, capture_output=True)
        outputs[tool] = subprocess.run(
            [*base, "archive", "--format=tar", "HEAD"], cwd=repo, env=env, capture_output=True
        ).stdout
    assert outputs["pygit"] == outputs["oracle"]
