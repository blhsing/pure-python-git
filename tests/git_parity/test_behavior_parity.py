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

# Branch topologies for show-branch: a fork (feat ahead of main) and a
# three-way fan-out, exercising the legend, separator, marker matrix
# (+/*/!/-), dense-merge omission, and the commit-naming (^/~) algorithm.
SB_FORK = BASE + [("write", "a.txt", "alpha\nx\n"), ["add", "-A"], ["commit", "-m", "c2"],
                  ["checkout", "-b", "feat"], ("write", "a.txt", "alpha\nx\ny\n"),
                  ["add", "-A"], ["commit", "-m", "c3"], ["checkout", "main"]]
SB_THREE = BASE + [
    ["checkout", "-b", "feat"], ("write", "g.txt", "g\n"), ["add", "-A"], ["commit", "-m", "fc1"],
    ["checkout", "main"], ("write", "h.txt", "h\n"), ["add", "-A"], ["commit", "-m", "mc1"],
    ["checkout", "-b", "third", "main"], ("write", "i.txt", "i\n"), ["add", "-A"], ["commit", "-m", "tc1"],
    ["checkout", "main"]]
SB_MERGE = MERGED  # a real merge commit, for the '-' merge marker column

# Two divergent versions of a topic branch, for range-diff (= / ! / < / >).
RANGEDIFF = BASE + [
    ["checkout", "-b", "v1"],
    ("write", "a.txt", "alpha\nb\n"), ["add", "-A"], ["commit", "-m", "add b"],
    ("write", "a.txt", "alpha\nb\nc\n"), ["add", "-A"], ["commit", "-m", "add c"],
    ["checkout", "-b", "v2", "main"],
    ("write", "a.txt", "alpha\nb\n"), ["add", "-A"], ["commit", "-m", "add b"],
    ("write", "a.txt", "alpha\nb\nc\nd\n"), ["add", "-A"], ["commit", "-m", "add c"],
    ["checkout", "main"],
]

# A base commit plus a modified tracked file and a new staged file, for
# exercising commit's flag set (author/signoff/trailer/only/include/...).
COMMITSTAGE = BASE + [("write", "a.txt", "alpha\nMOD\n"), ("write", "n.txt", "new\n"), ["add", "-A"]]

# One staged new file on top of BASE, for commit message-source flags.
CMSTAGE = BASE + [("write", "c.txt", "c\n"), ["add", "c.txt"]]
# Staged + unstaged + untracked at once, for commit --dry-run output formats.
CMMIX = BASE + [("write", "c.txt", "c\n"), ["add", "c.txt"],
                ("write", "a.txt", "alpha-mod\n"), ("write", "u.txt", "u\n")]
# Two distinct commits with multi-paragraph messages, for --squash <X> -C <Y>.
CMTWO = [("write", "a.txt", "alpha\n"), ["add", "-A"],
         ["commit", "-m", "tgt subject", "-m", "tgt body"],
         ["commit", "--allow-empty", "-m", "reuse subj", "-m", "reuse body"],
         ("write", "c.txt", "c\n"), ["add", "c.txt"]]
# A multi-paragraph commit, for the same-commit --squash/-C subject-strip rule.
CMML = [("write", "a.txt", "alpha\n"), ["add", "-A"],
        ["commit", "-m", "P-subj", "-m", "P-body"],
        ("write", "c.txt", "c\n"), ["add", "c.txt"]]

# A branch, a lightweight tag and an annotated tag, for show-ref flag coverage.
SHOWREF = BASE + [["branch", "dev"], ["tag", "v1"], ["tag", "-a", "-m", "anno", "v2"]]

# A committed "hello\n" blob (content-addressed sha, env-independent) for mktree.
MKTREE = [("write", "h", "hello\n"), ["add", "-A"], ["commit", "-m", "h"]]
HELLO_BLOB = "ce013625030ba8dba906f756967f9e9ca394464a"

# A staged 100%-identical rename, for status rename detection + --no-renames.
RENAME_EXACT = [
    ("write", "orig.txt", "aaa\nbbb\nccc\nddd\neee\n"), ["add", "-A"], ["commit", "-m", "first"],
    ["mv", "orig.txt", "renamed.txt"], ["add", "-A"],
]
# A staged ~80%-similar rename (one of five lines changed), for -M thresholds.
RENAME_PARTIAL = [
    ("write", "orig.txt", "aaa\nbbb\nccc\nddd\neee\n"), ["add", "-A"], ["commit", "-m", "first"],
    ("rm", "orig.txt"), ("write", "new.txt", "aaa\nbbb\nccc\nddd\nZZZ\n"), ["add", "-A"],
]

# Distinct authors (committer stays the pinned identity) plus a long subject,
# for shortlog grouping (-c), record formats (--pretty/--format) and -w wrapping.
SHORTLOG_MULTI = [
    ("write", "a.txt", "1\n"), ["add", "-A"],
    ["commit", "--author=Alice <alice@x>", "-m", "short one"],
    ("write", "a.txt", "2\n"), ["add", "-A"],
    ["commit", "--author=Bob <bob@y>", "-m",
     "this is a considerably longer subject line that should wrap when width limited"],
    ("write", "a.txt", "3\n"), ["add", "-A"],
    ["commit", "--author=Alice <alice@x>", "-m", "second from alice"],
]

# Commits by four identities plus a .mailmap exercising all four mapping forms,
# for --use-mailmap / %aN / check-mailmap. Committer stays the pinned identity.
MAILMAP_SETUP = [
    ["commit", "--allow-empty", "--author=Joe D <joe@old.com>", "-m", "c1"],
    ["commit", "--allow-empty", "--author=Jane <jane@work.com>", "-m", "c2"],
    ["commit", "--allow-empty", "--author=Bob <bob@x>", "-m", "c3"],
    ["commit", "--allow-empty", "--author=Typo <real@x>", "-m", "c4"],
    ("write", ".mailmap",
     "Joe Proper <joe@old.com>\n"
     "<jane@new.com> <jane@work.com>\n"
     "Robert <bob@new.com> <bob@x>\n"
     "Real Name <real@x> Typo <real@x>\n"),
]

# Tags on the base commit plus a tag on a diverged branch tip, for tag
# --merged/--no-merged/--column listing filters.
TAGREPO = BASE + [["tag", "v1"], ["tag", "v2"],
                  ["checkout", "-b", "feat"], ("write", "b.txt", "b\n"), ["add", "-A"],
                  ["commit", "-m", "c2"], ["tag", "ontip"], ["checkout", "main"]]
# A message file staged in the working tree, for `tag -a -F <file>`.
TAGMSGFILE = BASE + [("write", "tmsg.txt", "from file\nsecond line\n")]

# A commit that introduces whitespace errors (trailing space, space-before-tab),
# for `diff --check`.
WSERR = BASE + [("write", "a.txt", "clean\ntrailing \n\tgood\n \tspacetab\n"),
                ["add", "-A"], ["commit", "-m", "ws"]]
# In-line word changes plus an end-of-line addition, for `diff --word-diff`.
WORDDIFF = [("write", "p.txt", "the quick brown fox\nsecond line here\nunchanged\n"),
            ["add", "-A"], ["commit", "-m", "c1"],
            ("write", "p.txt", "the slow brown fox jumps\nsecond line here\nunchanged\nnew tail\n"),
            ["add", "-A"], ["commit", "-m", "c2"]]
# A whole-line deletion immediately adjacent to a modified line — the case where
# the inter-line newline placement is subtle in word-diff.
WORDDIFF2 = [("write", "p.txt", "L1 a\nL2 b\nL3 c\nL4 d\n"),
             ["add", "-A"], ["commit", "-m", "c1"],
             ("write", "p.txt", "L1 a\nL3 changed\nL4 d\nL5 e\n"),
             ["add", "-A"], ["commit", "-m", "c2"]]

# A file renamed across commits, for `log --follow`.
RENAME = [("write", "orig.txt", "alpha\n"), ["add", "-A"], ["commit", "-m", "c1"],
          ["mv", "orig.txt", "renamed.txt"], ["commit", "-m", "c2"],
          ("write", "renamed.txt", "alpha\nbeta\n"), ["add", "-A"], ["commit", "-m", "c3"]]

# A stash created from a modified tracked file plus a separately-staged new
# file, for stash commit/show/selector parity.
STASH = BASE + [("write", "a.txt", "alpha\nmod\n"), ("write", "n.txt", "new\n"),
                ["add", "n.txt"], ["stash", "push", "-m", "wip"]]
# Two stacked stashes, for stash@{N} selector ordering.
MULTISTASH = BASE + [("write", "a.txt", "alpha\nx\n"), ["stash", "push", "-m", "first"],
                     ("write", "a.txt", "alpha\ny\n"), ["stash", "push", "-m", "second"]]

REMOTE = BASE + [
    ["remote", "add", "origin", "https://example.com/r.git"],
    ["config", "--add", "foo.bar", "one"],
    ["config", "--add", "foo.bar", "two"],
]

# A tracked file, a .gitignore, plus untracked tracked/ignored worktree files,
# for ls-files -o/-i/-x exclusion behavior.
LSIGNORE = [("write", "a.txt", "a\n"), ("write", ".gitignore", "*.log\n"), ["add", "-A"],
            ["commit", "-m", "c1"],
            ("write", "b.txt", "b\n"), ("write", "debug.log", "x\n"), ("write", "other.txt", "y\n")]
# A committed pair, then a modified tracked file, a deleted tracked file, and an
# untracked file — for `add -u`/`--no-all`/`--pathspec-from-file` staging modes.
ADDSETUP = [("write", "a.txt", "a\n"), ("write", "b.txt", "b\n"), ["add", "-A"],
            ["commit", "-m", "c1"],
            ("write", "a.txt", "MOD\n"), ("rm", "b.txt"), ("write", "c.txt", "NEW\n")]
# Diverged branches (feat adds g.txt, main adds h.txt) with no overlap, for
# clean non-ff merge flag coverage.
MERGEDIV = [("write", "f.txt", "base\n"), ["add", "-A"], ["commit", "-m", "c1"],
            ["checkout", "-b", "feat"], ("write", "g.txt", "feat\n"), ["add", "-A"], ["commit", "-m", "fc1"],
            ["checkout", "main"], ("write", "h.txt", "main\n"), ["add", "-A"], ["commit", "-m", "mc1"]]
# Both branches edit the same file in conflicting regions, for merge conflicts
# and -X ours/theirs.
MERGECONF = [("write", "f.txt", "l1\nl2\nl3\n"), ["add", "-A"], ["commit", "-m", "c1"],
             ["checkout", "-b", "feat"], ("write", "f.txt", "l1\nFEAT\nl3\n"), ["add", "-A"], ["commit", "-m", "fc1"],
             ["checkout", "main"], ("write", "f.txt", "MAIN\nl2\nl3\n"), ["add", "-A"], ["commit", "-m", "mc1"]]
# Both branches edit the same file in non-overlapping regions (clean content
# merge → the "Auto-merging" notice without a conflict).
MERGEBOTH = [("write", "f.txt", "l1\nl2\nl3\nl4\nl5\n"), ["add", "-A"], ["commit", "-m", "c1"],
             ["checkout", "-b", "feat"], ("write", "f.txt", "TOP\nl2\nl3\nl4\nl5\n"), ["add", "-A"], ["commit", "-m", "fc1"],
             ["checkout", "main"], ("write", "f.txt", "l1\nl2\nl3\nl4\nBOT\n"), ["add", "-A"], ["commit", "-m", "mc1"]]

# Two commits where the second changes only a.txt (b.txt unchanged across
# HEAD~1..HEAD), for reset --merge/--keep/--mixed semantics.
RESETBASE = [("write", "a.txt", "a1\n"), ("write", "b.txt", "b1\n"), ["add", "-A"],
             ["commit", "-m", "c1"],
             ("write", "a.txt", "a2\n"), ["add", "-A"], ["commit", "-m", "c2"]]
# A merge conflict leaving unmerged (stage 1/2/3) index entries, for ls-files -u.
LSCONFLICT = [("write", "f.txt", "base\n"), ["add", "-A"], ["commit", "-m", "c1"],
              ["checkout", "-b", "feat"], ("write", "f.txt", "feat\n"), ["add", "-A"], ["commit", "-m", "c2"],
              ["checkout", "main"], ("write", "f.txt", "main\n"), ["add", "-A"], ["commit", "-m", "c3"],
              ["merge", "feat"]]

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
    # show-ref --abbrev (=N clamps to [4,40]; 0 means full; a token is a pattern).
    ("showref-abbrev", SHOWREF, ["show-ref", "--abbrev"]),
    ("showref-abbrev-8", SHOWREF, ["show-ref", "--abbrev=8", "--heads"]),
    ("showref-abbrev-0", SHOWREF, ["show-ref", "--abbrev=0", "--heads"]),
    ("showref-abbrev-clamp-lo", SHOWREF, ["show-ref", "--abbrev=2", "--heads"]),
    ("showref-abbrev-clamp-hi", SHOWREF, ["show-ref", "--abbrev=100", "--heads"]),
    ("showref-abbrev-token-pattern", SHOWREF, ["show-ref", "--abbrev", "8"]),
    # --branches is the 2.54 name for --heads; both can combine with --tags.
    ("showref-branches", SHOWREF, ["show-ref", "--branches"]),
    ("showref-branches-tags", SHOWREF, ["show-ref", "--branches", "--tags"]),
    ("showref-tags", SHOWREF, ["show-ref", "--tags"]),
    # -d dereferences annotated tags into a trailing ^{} line.
    ("showref-deref", SHOWREF, ["show-ref", "-d"]),
    ("showref-deref-tags", SHOWREF, ["show-ref", "-d", "--tags"]),
    # -s/--hash (with optional attached <n>); -q sets only the exit code.
    ("showref-hash", SHOWREF, ["show-ref", "-s"]),
    ("showref-hash-n", SHOWREF, ["show-ref", "-s8", "--heads"]),
    ("showref-quiet-hit", SHOWREF, ["show-ref", "-q", "refs/heads/main"]),
    ("showref-quiet-miss", SHOWREF, ["show-ref", "-q", "refs/heads/nope"]),
    ("showref-pattern-tail", SHOWREF, ["show-ref", "heads/main"]),
    # --verify: exact ref path (refs/* or a safe pseudo-ref like HEAD).
    ("showref-verify-ok", SHOWREF, ["show-ref", "--verify", "refs/heads/main"]),
    ("showref-verify-head", SHOWREF, ["show-ref", "--verify", "HEAD"]),
    ("showref-verify-bad", SHOWREF, ["show-ref", "--verify", "main"]),
    ("showref-verify-missing", SHOWREF, ["show-ref", "--verify", "refs/heads/nope"]),
    ("showref-verify-none", SHOWREF, ["show-ref", "--verify"]),
    ("showref-verify-quiet-miss", SHOWREF, ["show-ref", "-q", "--verify", "refs/heads/nope"]),
    ("showref-verify-deref", SHOWREF, ["show-ref", "--verify", "-d", "refs/tags/v2"]),
    # --exists: literal existence check (rc 0 / 2, never resolves/DWIMs).
    ("showref-exists-ok", SHOWREF, ["show-ref", "--exists", "refs/heads/main"]),
    ("showref-exists-head", SHOWREF, ["show-ref", "--exists", "HEAD"]),
    ("showref-exists-miss", SHOWREF, ["show-ref", "--exists", "refs/heads/nope"]),
    ("showref-exists-not-full", SHOWREF, ["show-ref", "--exists", "main"]),
    ("showref-exists-none", SHOWREF, ["show-ref", "--exists"]),
    ("showref-exists-two", SHOWREF, ["show-ref", "--exists", "refs/heads/main", "refs/heads/dev"]),
    # status
    ("status-clean-long", BASE, ["status"]),
    ("status-clean-porcelain", BASE, ["status", "--porcelain"]),
    ("status-clean-sb", BASE, ["status", "-s", "-b"]),
    ("status-dirty-porcelain", DIRTY, ["status", "--porcelain"]),
    ("status-dirty-porcelain-v2", DIRTY, ["status", "--porcelain=v2"]),
    ("status-dirty-short", DIRTY, ["status", "-s"]),
    ("status-dirty-long", DIRTY, ["status"]),
    ("status-untracked-only", BASE + [("write", "u.txt", "u\n")], ["status"]),
    # rename detection is on by default; --no-renames shows delete+add instead.
    ("status-rename-default", RENAME_EXACT, ["status"]),
    ("status-rename-no-renames", RENAME_EXACT, ["status", "--no-renames"]),
    ("status-rename-renames", RENAME_EXACT, ["status", "--renames"]),
    ("status-rename-short", RENAME_EXACT, ["status", "-s"]),
    ("status-rename-short-no-renames", RENAME_EXACT, ["status", "-s", "--no-renames"]),
    ("status-rename-porcelain", RENAME_EXACT, ["status", "--porcelain"]),
    ("status-rename-porcelain-no-renames", RENAME_EXACT, ["status", "--porcelain", "--no-renames"]),
    # -M/--find-renames force detection on even after --no-renames (builtin/commit.c).
    ("status-rename-M-forces-on", RENAME_EXACT, ["status", "-M", "--no-renames"]),
    ("status-rename-no-then-yes", RENAME_EXACT, ["status", "--no-renames", "--renames"]),
    # -M<n> threshold: a partial rename is detected at 50% but not at 90%.
    ("status-partial-default", RENAME_PARTIAL, ["status"]),
    ("status-partial-M50", RENAME_PARTIAL, ["status", "-M50%"]),
    ("status-partial-M90", RENAME_PARTIAL, ["status", "-M90%"]),
    ("status-partial-M9", RENAME_PARTIAL, ["status", "-M9"]),
    ("status-partial-find-renames", RENAME_PARTIAL, ["status", "--find-renames=90%"]),
    ("status-partial-short-M90", RENAME_PARTIAL, ["status", "-s", "-M90%"]),
    # -v appends the staged diff (a/b, rename-aware); only when something is staged.
    ("status-v-clean", BASE, ["status", "-v"]),
    ("status-v-staged-mod",
     BASE + [("write", "a.txt", "alpha\nMM\n"), ["add", "a.txt"]], ["status", "-v"]),
    ("status-v-staged-new",
     BASE + [("write", "c.txt", "new\n"), ["add", "c.txt"]], ["status", "-v"]),
    ("status-v-only-unstaged", BASE + [("write", "a.txt", "alpha\nWW\n")], ["status", "-v"]),
    ("status-v-staged-untracked",
     BASE + [("write", "a.txt", "alpha\nMM\n"), ["add", "a.txt"], ("write", "u.txt", "u\n")],
     ["status", "-v"]),
    ("status-v-rename", RENAME_EXACT, ["status", "-v"]),
    ("status-v-rename-no-renames", RENAME_EXACT, ["status", "-v", "--no-renames"]),
    ("status-v-short-ignored",
     BASE + [("write", "a.txt", "alpha\nMM\n"), ["add", "a.txt"]], ["status", "-s", "-v"]),
    # -vv adds the worktree diff under c/i and i/w prefixes with a 50-dash separator.
    ("status-vv-staged-only",
     BASE + [("write", "a.txt", "alpha\nMM\n"), ["add", "a.txt"]], ["status", "-vv"]),
    ("status-vv-only-unstaged", BASE + [("write", "a.txt", "alpha\nWW\n")], ["status", "-vv"]),
    ("status-vv-both",
     BASE + [("write", "a.txt", "alpha\nSS\n"), ["add", "a.txt"], ("write", "a.txt", "alpha\nSS\nWW\n")],
     ["status", "-vv"]),
    ("status-vv-distinct-files",
     BASE + [("write", "a.txt", "alpha\nMM\n"), ["add", "a.txt"], ("write", "b.txt", "CHG\n")],
     ["status", "-vv"]),
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
    # diff-files output modes (modified + deleted tracked file): full-id raw,
    # -p patch, --name-status, -z, -R, --abbrev, --full-index, -U, -q no-op.
    ("diff-files-mod-del", BASE + [("write", "a.txt", "X\n"), ("rm", "b.txt")], ["diff-files"]),
    ("diff-files-p", BASE + [("write", "a.txt", "X\n"), ("rm", "b.txt")], ["diff-files", "-p"]),
    ("diff-files-name-status", BASE + [("write", "a.txt", "X\n"), ("rm", "b.txt")],
     ["diff-files", "--name-status"]),
    ("diff-files-z", BASE + [("write", "a.txt", "X\n"), ("rm", "b.txt")], ["diff-files", "-z"]),
    ("diff-files-R", BASE + [("write", "a.txt", "X\n"), ("rm", "b.txt")], ["diff-files", "-R"]),
    ("diff-files-R-p", BASE + [("write", "a.txt", "X\n")], ["diff-files", "-R", "-p"]),
    ("diff-files-abbrev", BASE + [("write", "a.txt", "X\n")], ["diff-files", "--abbrev=8"]),
    ("diff-files-full-index", BASE + [("write", "a.txt", "X\n")], ["diff-files", "--full-index"]),
    ("diff-files-U1", BASE + [("write", "a.txt", "1\n2\n3\n4\n")], ["diff-files", "-U1", "-p"]),
    ("diff-files-q", BASE + [("write", "a.txt", "X\n")], ["diff-files", "-q"]),
    ("diff-files-patch-with-raw", BASE + [("write", "a.txt", "X\n")],
     ["diff-files", "--patch-with-raw"]),
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
    # apply --summary / --exclude / --include / -v, and new-file application.
    ("apply-summary",
     BASE + [("write", "p.diff",
              "diff --git a/new.txt b/new.txt\nnew file mode 100644\n"
              "--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1 @@\n+hello\n")],
     ["apply", "--summary", "p.diff"]),
    ("apply-verbose",
     BASE + [("write", "p.diff",
              "--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-alpha\n+ALPHA\n")],
     ["apply", "-v", "p.diff"]),
    ("apply-exclude",
     BASE + [("write", "p.diff",
              "diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-alpha\n+ALPHA\n"
              "diff --git a/new.txt b/new.txt\nnew file mode 100644\n--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1 @@\n+x\n")],
     ["apply", "--exclude=new.txt", "--summary", "p.diff"]),
    ("apply-include",
     BASE + [("write", "p.diff",
              "diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-alpha\n+ALPHA\n"
              "diff --git a/new.txt b/new.txt\nnew file mode 100644\n--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1 @@\n+x\n")],
     ["apply", "--include=new.txt", "--summary", "p.diff"]),
    ("apply-newfile-effect",
     BASE + [("write", "p.diff",
              "diff --git a/new.txt b/new.txt\nnew file mode 100644\n"
              "--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1 @@\n+hello\n"),
             ["apply", "p.diff"]],
     ["status", "--short"]),
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
    # show with multiple revisions (blank-separated) and oneline.
    ("show-multi-rev", TAGGED, ["show", "-s", "HEAD", "HEAD~1"]),
    ("show-multi-rev-oneline", TAGGED, ["show", "-s", "--oneline", "HEAD", "HEAD~1"]),
    # show of a tree object lists bare names with a "tree <rev>" header.
    ("show-tree", TAGGED + [("write", "sub/c.txt", "s\n"), ["add", "-A"], ["commit", "-m", "c3"]],
     ["show", "HEAD^{tree}"]),
    # for-each-ref format atoms.
    ("fer-objectname-short", REFSET, ["for-each-ref", "--format=%(objectname:short)", "refs/heads"]),
    ("fer-head-marker", REFSET, ["for-each-ref", "--format=%(HEAD) %(refname:short)", "refs/heads"]),
    ("fer-subject", REFSET, ["for-each-ref", "--format=%(subject)", "refs/heads"]),
    ("fer-author", REFSET, ["for-each-ref", "--format=%(authorname) %(authoremail)", "refs/heads"]),
    ("fer-committerdate-short", REFSET, ["for-each-ref", "--format=%(committerdate:short)", "refs/heads"]),
    ("fer-objectsize", REFSET, ["for-each-ref", "--format=%(objecttype) %(objectsize)", "refs/tags"]),
    ("fer-multi-atom", REFSET, ["for-each-ref", "--format=%(objectname:short) %(refname:short) %(subject)", "refs/heads"]),
    # log --no-walk shows only the named revisions, no traversal.
    ("log-no-walk", MERGED, ["log", "--no-walk", "--oneline", "HEAD", "HEAD~1"]),
    ("log-no-walk-unsorted", MERGED, ["log", "--no-walk=unsorted", "--oneline", "HEAD~1", "HEAD"]),
    # grep flags and tree-grep.
    ("grep-basic", BASE, ["grep", "alpha"]),
    ("grep-n", BASE, ["grep", "-n", "alpha"]),
    ("grep-c", BASE, ["grep", "-c", "alpha"]),
    ("grep-w", BASE, ["grep", "-w", "alpha"]),
    ("grep-v", BASE, ["grep", "-v", "alpha", "a.txt"]),
    ("grep-F", BASE, ["grep", "-F", "alpha"]),
    ("grep-tree", TAGGED, ["grep", "more", "HEAD"]),
    ("grep-tree-n", TAGGED, ["grep", "-n", "more", "HEAD"]),
    ("grep-nomatch", BASE, ["grep", "zzznope"]),
    # blame -L / -l.
    ("blame-L", TAGGED, ["blame", "-L", "1,1", "a.txt"]),
    ("blame-l", TAGGED, ["blame", "-l", "a.txt"]),
    # archive --prefix (tar is byte-exact).
    ("archive-tar", TAGGED, ["archive", "--format=tar", "HEAD"]),
    ("archive-prefix", TAGGED, ["archive", "--format=tar", "--prefix=x/", "HEAD"]),
    ("archive-prefix-multi", TAGGED, ["archive", "--format=tar", "--prefix=a/b/", "HEAD"]),
    # -U/--unified context-line control on diff/log/show.
    ("diff-U1",
     BASE + [("write", "a.txt", "alpha\nx\ny\nz\n"), ["add", "-A"], ["commit", "-m", "c2"]],
     ["diff", "-U1", "HEAD~1", "HEAD"]),
    ("diff-U0",
     BASE + [("write", "a.txt", "alpha\nx\ny\nz\n"), ["add", "-A"], ["commit", "-m", "c2"]],
     ["diff", "--unified=0", "HEAD~1", "HEAD"]),
    ("log-p-U1", TAGGED, ["log", "-p", "-U1", "-1"]),
    ("show-U1", TAGGED, ["show", "-U1", "HEAD"]),
    # Short/human date atoms and empty encoding/notes atoms.
    ("log-fmt-as", DATED, ["log", "-1", "--format=%as"]),
    ("log-fmt-cs", DATED, ["log", "-1", "--format=%cs"]),
    ("log-fmt-ah", DATED, ["log", "-1", "--format=%ah"]),
    ("log-fmt-encoding", TAGGED, ["log", "-1", "--format=[%e]"]),
    ("log-fmt-notes", TAGGED, ["log", "-1", "--format=[%N]"]),
    # show --abbrev threads into %h.
    ("show-abbrev-len", TAGGED, ["show", "-s", "--abbrev=10", "--format=%h", "HEAD"]),
    # log --decorate=full keeps full ref names.
    ("log-decorate-full",
     BASE + [["tag", "-a", "-m", "r", "v1"], ["branch", "feat"]],
     ["log", "-1", "--oneline", "--decorate=full"]),
    # mv --dry-run, and format-patch numbering.
    ("mv-dry-run", BASE, ["mv", "-n", "a.txt", "c.txt"]),
    ("format-patch-numbered", TAGGED, ["format-patch", "-1", "--stdout", "--numbered"]),
    ("format-patch-no-numbered", TAGGED, ["format-patch", "-1", "--stdout", "--no-numbered"]),
    # log -g/--walk-reflogs: per-entry selector, identity, and message.
    ("log-g-oneline", MERGED, ["log", "-g", "--oneline"]),
    ("log-g-medium", MERGED, ["log", "-g"]),
    ("log-g-count", MERGED, ["log", "-g", "-n", "2"]),
    ("log-g-pretty-oneline", MERGED, ["log", "-g", "--pretty=oneline"]),
    ("log-g-reverse-err", MERGED, ["log", "-g", "--reverse"]),
    # describe --contains delegates to name-rev's tags-only naming.
    ("describe-contains", DESC, ["describe", "--contains", "HEAD~1"]),
    ("describe-contains-none", DESC, ["describe", "--contains", "HEAD"]),
    # name-rev appends ^0 for an annotated tag's exact (dereferenced) commit.
    ("name-rev-deref",
     BASE + [["tag", "-a", "-m", "r", "v1"], ("write", "z.txt", "z\n"), ["add", "-A"], ["commit", "-m", "c2"]],
     ["name-rev", "--tags", "--name-only", "HEAD~1"]),
    # notes show on an object with no note: stderr message + rc 1.
    ("notes-show-missing", BASE, ["notes", "show", "HEAD"]),
    # Reflog messages match git across history-mutating operations.
    ("reflog-reset",
     BASE + [("write", "a.txt", "x\n"), ["add", "-A"], ["commit", "-m", "c2"],
             ["reset", "--hard", "HEAD~1"]],
     ["reflog"]),
    ("reflog-amend", BASE + [["commit", "--amend", "-m", "amended"]], ["reflog"]),
    ("reflog-cherry-pick",
     BASE + [["checkout", "-b", "feat"], ("write", "g.txt", "g\n"), ["add", "-A"],
             ["commit", "-m", "gc"], ["checkout", "main"], ["cherry-pick", "feat"]],
     ["reflog"]),
    ("reflog-revert",
     BASE + [("write", "a.txt", "x\n"), ["add", "-A"], ["commit", "-m", "c2"],
             ["revert", "--no-edit", "HEAD"]],
     ["reflog"]),
    # branch -m of the current branch writes a HEAD delete/create pair (the
    # zero-oid delete half is hidden but still consumes an @{N} slot) plus a
    # no-op entry on the renamed branch's own log.
    ("reflog-branch-move", BASE + [["branch", "-m", "main", "trunk"]], ["reflog"]),
    ("reflog-branch-move-branch", BASE + [["branch", "-m", "main", "trunk"]],
     ["reflog", "show", "trunk"]),
    ("log-g-after-rename", BASE + [["branch", "-m", "main", "trunk"]],
     ["log", "-g", "--oneline"]),
    # reflog --date=<mode> formats the @{...} selector with the entry's time.
    ("reflog-date-iso", MERGED, ["reflog", "--date=iso"]),
    ("reflog-date-short", MERGED, ["reflog", "--date=short"]),
    ("reflog-date-relative", MERGED, ["reflog", "show", "--date=relative"]),
    # log -g reflog placeholders and date-form selectors.
    ("log-g-date-iso", MERGED, ["log", "-g", "--date=iso", "-1"]),
    ("log-g-fmt-gd", MERGED, ["log", "-g", "--format=%gd", "-2"]),
    ("log-g-fmt-gD", MERGED, ["log", "-g", "--format=%gD", "-2"]),
    ("log-g-fmt-gs", MERGED, ["log", "-g", "--format=%gs", "-2"]),
    ("log-g-fmt-gn-ge", MERGED, ["log", "-g", "--format=%gn <%ge>", "-1"]),
    ("log-g-fmt-gd-date", MERGED, ["log", "-g", "--format=%gd", "--date=iso", "-1"]),
    # %gd/%gD are empty outside a reflog walk.
    ("log-fmt-gd-empty", BASE, ["log", "-1", "--format=[%gd][%gD][%gs]"]),
    # stash: commit is byte-identical (so rev-parse matches), plus show/selectors.
    ("stash-list", STASH, ["stash", "list"]),
    ("stash-show", STASH, ["stash", "show"]),
    ("stash-show-p", STASH, ["stash", "show", "-p"]),
    ("stash-revparse", STASH, ["rev-parse", "stash@{0}"]),
    ("stash-revparse-index", STASH, ["rev-parse", "stash@{0}^2"]),
    ("stash-reflog", STASH, ["reflog", "stash"]),
    ("stash-log-g", STASH, ["log", "-g", "stash", "--oneline"]),
    ("stash-list-2", MULTISTASH, ["stash", "list"]),
    ("stash-show-1", MULTISTASH, ["stash", "show", "stash@{1}"]),
    # count-objects -v includes the size-pack/prune-packable/garbage lines.
    ("count-objects-v", TAGGED, ["count-objects", "-v"]),
    # cat-file --batch-all-objects enumerates every object, sorted by oid.
    ("cat-file-batch-all-check", SUBTREE, ["cat-file", "--batch-all-objects", "--batch-check"]),
    # branch --format reuses the ref-filter %(...) atom expansion.
    ("branch-format-refname", REFSET, ["branch", "--format=%(refname:short)"]),
    ("branch-format-multi", REFSET, ["branch", "--format=%(objectname) %(HEAD) %(refname)"]),
    # show-branch: legend + marker matrix + commit naming, ported from git.
    ("show-branch-1", TAGGED, ["show-branch"]),
    ("show-branch-fork", SB_FORK, ["show-branch"]),
    ("show-branch-fork-args", SB_FORK, ["show-branch", "main", "feat"]),
    ("show-branch-three", SB_THREE, ["show-branch"]),
    ("show-branch-three-rev", SB_THREE, ["show-branch", "third", "feat"]),
    ("show-branch-merge", SB_MERGE, ["show-branch", "main", "feat"]),
    ("show-branch-all", SB_THREE, ["show-branch", "--all"]),
    # show-branch plumbing modes: --merge-base, --independent, --reflog.
    ("show-branch-merge-base", SB_THREE, ["show-branch", "--merge-base", "feat", "main"]),
    ("show-branch-merge-base-3", SB_THREE, ["show-branch", "--merge-base", "feat", "main", "third"]),
    ("show-branch-independent", SB_THREE, ["show-branch", "--independent", "feat", "main", "third"]),
    ("show-branch-reflog", TAGGED, ["show-branch", "--reflog"]),
    ("show-branch-reflog-n", TAGGED, ["show-branch", "--reflog=2"]),
    ("show-branch-reflog-ref", TAGGED, ["show-branch", "--reflog", "main"]),
    # diff --word-diff: inline [-removed-]{+added+} word markers (plain mode).
    ("word-diff", WORDDIFF, ["diff", "--word-diff", "HEAD~1", "HEAD"]),
    ("word-diff-plain", WORDDIFF, ["diff", "--word-diff=plain", "HEAD~1", "HEAD"]),
    ("word-diff-reverse", WORDDIFF, ["diff", "--word-diff", "HEAD", "HEAD~1"]),
    ("word-diff-path", WORDDIFF, ["diff", "--word-diff", "HEAD~1", "HEAD", "--", "p.txt"]),
    ("word-diff-porcelain", WORDDIFF, ["diff", "--word-diff=porcelain", "HEAD~1", "HEAD"]),
    ("word-diff-porcelain-rev", WORDDIFF, ["diff", "--word-diff=porcelain", "HEAD", "HEAD~1"]),
    # whole-line deletion adjacent to a modification (the tricky newline case).
    ("word-diff-del-adjacent", WORDDIFF2, ["diff", "--word-diff", "HEAD~1", "HEAD"]),
    ("word-diff-del-adjacent-rev", WORDDIFF2, ["diff", "--word-diff", "HEAD", "HEAD~1"]),
    ("word-diff-del-adjacent-porc", WORDDIFF2, ["diff", "--word-diff=porcelain", "HEAD~1", "HEAD"]),
    # diff --check reports whitespace errors on added lines (rc 2), else nothing.
    ("diff-check", WSERR, ["diff", "--check", "HEAD~1", "HEAD"]),
    ("diff-check-clean", SUBTREE, ["diff", "--check", "HEAD~1", "HEAD"]),
    # log --follow tracks a file across a rename.
    ("log-follow-oneline", RENAME, ["log", "--follow", "--oneline", "renamed.txt"]),
    ("log-follow-format", RENAME, ["log", "--follow", "--format=%s", "renamed.txt"]),
    ("log-follow-medium", RENAME, ["log", "--follow", "renamed.txt"]),
    # format pretty styles also append --stat/--name-only/-p diff output.
    ("log-format-stat", SUBTREE, ["log", "--format=%s", "--stat"]),
    ("log-format-nameonly", SUBTREE, ["log", "--name-only", "--format=%s"]),
    # A pathspec restricts the per-commit diff to matching files.
    ("log-p-path", PATHHIST, ["log", "-p", "a.txt"]),
    ("log-stat-path", PATHHIST, ["log", "--stat", "a.txt"]),
    ("log-name-only-path", PATHHIST, ["log", "--name-only", "a.txt"]),
    ("log-raw-path", PATHHIST, ["log", "--raw", "c.txt"]),
    # log --oneline with diff output (stat/patch/shortstat/name-only).
    ("log-oneline-stat", SUBTREE, ["log", "--oneline", "--stat"]),
    ("log-oneline-patch", SUBTREE, ["log", "--oneline", "-p"]),
    ("log-oneline-shortstat", SUBTREE, ["log", "--oneline", "--shortstat"]),
    ("log-oneline-nameonly", SUBTREE, ["log", "--oneline", "--name-only"]),
    # diff-tree --no-commit-id suppresses the leading object id line.
    ("diff-tree-no-commit-id", SUBTREE, ["diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"]),
    ("diff-tree-no-commit-id-raw", SUBTREE, ["diff-tree", "--no-commit-id", "-r", "HEAD"]),
    # rev-list --children lists in-set children after each commit.
    ("rev-list-children", PATHHIST, ["rev-list", "--children", "HEAD"]),
    ("rev-list-children-parents-err", PATHHIST, ["rev-list", "--children", "--parents", "HEAD"]),
    # format-patch flags + range semantics.
    ("format-patch-no-stat", TAGGED, ["format-patch", "-1", "--stdout", "--no-stat"]),
    ("format-patch-signoff", TAGGED, ["format-patch", "-1", "--stdout", "--signoff"]),
    ("format-patch-reroll", TAGGED, ["format-patch", "-1", "--stdout", "-v2"]),
    ("format-patch-rev-range", PATHHIST, ["format-patch", "HEAD~1", "--stdout"]),
    ("format-patch-rev-range2", PATHHIST, ["format-patch", "HEAD~2", "--stdout"]),
    ("format-patch-multi", PATHHIST, ["format-patch", "-2", "--stdout"]),
    ("format-patch-bad-rev", BASE, ["format-patch", "nonexistent", "--stdout"]),
    # describe --all considers all refs with namespaced names.
    ("describe-all", DESC, ["describe", "--all"]),
    ("describe-all-parent", DESC, ["describe", "--all", "HEAD~1"]),
    # restore --source / --staged --source restore from an arbitrary tree;
    # probe the resulting status to confirm the effect matches git.
    ("restore-source",
     PATHHIST + [("write", "a.txt", "dirty\n"), ["restore", "--source=HEAD~1", "a.txt"]],
     ["status", "--short"]),
    ("restore-staged-source",
     PATHHIST + [["restore", "--staged", "--source=HEAD~1", "a.txt"]],
     ["status", "--short"]),
    # commit flag behaviors (output line + resulting commit verified via show).
    ("commit-author", COMMITSTAGE, ["commit", "-m", "msg", "--author=Bob <bob@x>"]),
    ("commit-author-show", COMMITSTAGE + [["commit", "-m", "msg", "--author=Bob <bob@x>"]],
     ["show", "-s", "--format=%an <%ae> / %cn <%ce>"]),
    ("commit-signoff", COMMITSTAGE + [["commit", "-s", "-m", "msg"]],
     ["show", "-s", "--format=%B"]),
    ("commit-trailer", COMMITSTAGE + [["commit", "-m", "msg", "--trailer", "Acked-by: A <a@b>"]],
     ["show", "-s", "--format=%B"]),
    # show --format terminator newline is unconditional (trailing blank for %B).
    ("show-format-B-newline", COMMITSTAGE + [["commit", "-s", "-m", "msg"]],
     ["show", "-s", "--format=%B"]),
    ("commit-only", COMMITSTAGE + [["commit", "-o", "a.txt", "-m", "only"]],
     ["status", "--short"]),
    ("commit-only-tree", COMMITSTAGE + [["commit", "-o", "a.txt", "-m", "only"]],
     ["ls-tree", "-r", "--name-only", "HEAD"]),
    ("commit-include", COMMITSTAGE + [["commit", "-i", "n.txt", "-m", "inc"]],
     ["status", "--short"]),
    ("commit-amend-reset-author",
     BASE + [["commit", "--amend", "--reset-author", "--no-edit"]], ["log", "-1"]),
    # commit --dry-run: long status of the worktree, never creates a commit.
    ("commit-dry-run-staged", CMSTAGE, ["commit", "--dry-run"]),
    ("commit-dry-run-clean", BASE, ["commit", "--dry-run"]),
    ("commit-dry-run-unstaged", BASE + [("write", "a.txt", "x\n")], ["commit", "--dry-run"]),
    ("commit-dry-run-all", BASE + [("write", "a.txt", "x\n")], ["commit", "--dry-run", "-a"]),
    ("commit-dry-run-untracked", BASE + [("write", "u.txt", "u\n")], ["commit", "--dry-run"]),
    ("commit-dry-run-pathspec",
     BASE + [("write", "a.txt", "x\n"), ("write", "b.txt", "y\n")], ["commit", "--dry-run", "a.txt"]),
    # commit --dry-run still doesn't commit when invoked next (proves no mutation).
    ("commit-dry-run-then-status", CMSTAGE + [["commit", "--dry-run"]], ["status", "--short"]),
    # commit dry-run output formats (each implies --dry-run, no commit created).
    ("commit-short", CMMIX, ["commit", "--short"]),
    ("commit-porcelain", CMMIX, ["commit", "--porcelain"]),
    ("commit-long", CMMIX, ["commit", "--long"]),
    ("commit-z", CMMIX, ["commit", "-z"]),
    ("commit-null", CMMIX, ["commit", "--null"]),
    ("commit-porcelain-branch", CMMIX, ["commit", "--porcelain", "--branch"]),
    ("commit-short-branch", CMMIX, ["commit", "--short", "--branch"]),
    ("commit-dry-run-short", CMMIX, ["commit", "--dry-run", "--short"]),
    ("commit-short-uno", CMMIX, ["commit", "--short", "-uno"]),
    ("commit-long-uno", CMMIX, ["commit", "--long", "-uno"]),
    ("commit-short-ignores-m", CMMIX, ["commit", "--short", "-m", "x"]),
    ("commit-porcelain-nothing-staged", BASE + [("write", "u.txt", "u\n")], ["commit", "--porcelain"]),
    ("commit-short-clean", BASE, ["commit", "--short"]),
    # message reuse: -C/-c reuse message (and author -> shows Date:); --reset-author resets.
    ("commit-reuse-C", CMSTAGE, ["commit", "-C", "HEAD"]),
    ("commit-reuse-C-reset-author", CMSTAGE, ["commit", "-C", "HEAD", "--reset-author"]),
    ("commit-reedit-c", CMSTAGE, ["commit", "-c", "HEAD"]),
    # autosquash headers.
    ("commit-squash", CMSTAGE, ["commit", "--squash", "HEAD"]),
    ("commit-fixup", CMSTAGE, ["commit", "--fixup", "HEAD"]),
    ("commit-fixup-amend", CMSTAGE, ["commit", "--fixup=amend:HEAD"]),
    ("commit-fixup-reword",
     BASE + [["commit", "--allow-empty", "-m", "x"]], ["commit", "--allow-empty", "--fixup=reword:HEAD~1"]),
    ("commit-squash-plus-m", CMSTAGE, ["commit", "--squash", "HEAD", "-m", "extra"]),
    ("commit-fixup-plus-m", CMSTAGE, ["commit", "--fixup", "HEAD", "-m", "extra"]),
    # --squash <X> -C <Y>: header from X, body+author from distinct Y.
    ("commit-squash-reuse-distinct", CMTWO, ["commit", "--squash", "HEAD~1", "-C", "HEAD"]),
    # same commit squashed and reused: the reused subject is dropped.
    ("commit-squash-reuse-same", CMML, ["commit", "--squash", "HEAD", "-C", "HEAD"]),
    # verify the constructed message bytes directly.
    ("commit-squash-reuse-same-show",
     CMML + [["commit", "--squash", "HEAD", "-C", "HEAD"]], ["show", "-s", "--format=%B"]),
    # message-source conflicts: name order in the fatal must match C Git exactly.
    ("commit-conflict-m-F", CMSTAGE + [("write", "m.txt", "x\n")], ["commit", "-m", "x", "-F", "m.txt"]),
    ("commit-conflict-C-m", CMSTAGE, ["commit", "-C", "HEAD", "-m", "x"]),
    ("commit-conflict-c-F", CMSTAGE + [("write", "m.txt", "x\n")], ["commit", "-c", "HEAD", "-F", "m.txt"]),
    ("commit-conflict-C-c", CMSTAGE, ["commit", "-C", "HEAD", "-c", "HEAD"]),
    ("commit-conflict-squash-fixup", CMSTAGE, ["commit", "--squash", "HEAD", "--fixup", "HEAD"]),
    ("commit-conflict-fixup-C", CMSTAGE, ["commit", "--fixup", "HEAD", "-C", "HEAD"]),
    ("commit-conflict-fixup-F", CMSTAGE + [("write", "m.txt", "x\n")], ["commit", "--fixup", "HEAD", "-F", "m.txt"]),
    # --reset-author requires -C/-c/--amend.
    ("commit-reset-author-alone", CMSTAGE, ["commit", "--reset-author", "-m", "x"]),
    # accept-only flags commit normally.
    ("commit-verify", CMSTAGE, ["commit", "--verify", "-m", "x"]),
    ("commit-status-flag", CMMIX, ["commit", "--status", "-m", "x"]),
    ("commit-ahead-behind", CMMIX, ["commit", "--ahead-behind", "-m", "x"]),
    ("commit-branch-commits", CMMIX, ["commit", "--branch", "-m", "x"]),
    # mailmap: %aN/%aE/%cN/%cE always map; %an/%ae stay raw; builtin formats map
    # by default (log.mailmap=true) with --no-use-mailmap opting out; raw doesn't.
    ("mailmap-aN-aE", MAILMAP_SETUP, ["log", "--format=%aN <%aE>"]),
    ("mailmap-an-ae-raw", MAILMAP_SETUP, ["log", "--format=%an <%ae>"]),
    ("mailmap-cN-cE", MAILMAP_SETUP, ["log", "-1", "--format=%cN <%cE>"]),
    ("mailmap-medium-default", MAILMAP_SETUP, ["log", "--format=medium"]),
    ("mailmap-no-use-mailmap", MAILMAP_SETUP, ["log", "--no-use-mailmap", "--format=medium"]),
    ("mailmap-use-mailmap-an-raw", MAILMAP_SETUP, ["log", "--use-mailmap", "--format=%an"]),
    ("mailmap-pretty-short", MAILMAP_SETUP, ["log", "--pretty=short"]),
    ("mailmap-pretty-full", MAILMAP_SETUP, ["log", "--pretty=full"]),
    ("mailmap-pretty-fuller", MAILMAP_SETUP, ["log", "-1", "--pretty=fuller"]),
    ("mailmap-pretty-raw", MAILMAP_SETUP, ["log", "-1", "--pretty=raw"]),
    ("mailmap-show", MAILMAP_SETUP, ["show", "--no-patch"]),
    ("mailmap-show-no-mailmap", MAILMAP_SETUP, ["show", "--no-patch", "--no-use-mailmap"]),
    ("mailmap-check", MAILMAP_SETUP,
     ["check-mailmap", "Joe D <joe@old.com>", "Jane <jane@work.com>",
      "Bob <bob@x>", "Typo <real@x>", "Unknown <u@x>"]),
    # log %N expands the commit note; checkout -q suppresses switch messages.
    ("log-fmt-note", BASE + [["notes", "add", "-m", "a note", "HEAD"]],
     ["log", "-1", "--format=[%N]"]),
    ("checkout-q-detach", TAGGED, ["checkout", "-q", "HEAD~1"]),
    ("checkout-q-branch", TAGGED + [["checkout", "-q", "HEAD~1"]], ["checkout", "-q", "main"]),
    # reflog expire/delete no-op; reflog exists.
    ("reflog-expire", BASE, ["reflog", "expire", "--all"]),
    ("reflog-exists", BASE, ["reflog", "exists", "HEAD"]),
    ("reflog-exists-no", BASE, ["reflog", "exists", "refs/heads/nope"]),
    # Mailmap name/email placeholders (%aN/%aE/%cN/%cE) and signature %GP.
    ("log-fmt-mailmap", BASE, ["log", "-1", "--format=%aN <%aE> / %cN <%cE>"]),
    ("log-fmt-gp", BASE, ["log", "-1", "--format=[%GP][%GF]"]),
    # rev-parse <rev>:<missing-path> gives git's specific "does not exist" error.
    ("revparse-missing-path", BASE, ["rev-parse", "HEAD:nonexistent"]),
    # check-ref-format --allow-onelevel accepts a single-component name.
    ("check-ref-onelevel", BASE, ["check-ref-format", "--allow-onelevel", "foo"]),
    ("check-ref-onelevel-bad", BASE, ["check-ref-format", "--allow-onelevel", "bad..name"]),
    # cat-file --allow-unknown-type and update-index --show-index-version.
    ("cat-file-allow-unknown", BASE, ["cat-file", "--allow-unknown-type", "-t", "HEAD"]),
    ("update-index-version", BASE, ["update-index", "--show-index-version"]),
    # rev-list --timestamp prefixes the committer epoch; merge-base --octopus
    # and tag-peeling; describe --candidates accepted.
    ("rev-list-timestamp", PATHHIST, ["rev-list", "--timestamp", "HEAD"]),
    ("merge-base-octopus", DESC, ["merge-base", "--octopus", "HEAD", "v1"]),
    ("merge-base-tag", DESC, ["merge-base", "HEAD", "v1"]),
    ("describe-candidates", DESC, ["describe", "--candidates=1"]),
    # tag --contains/--no-contains filters; tag --format ref-filter atoms.
    ("tag-contains", DESC, ["tag", "--contains", "HEAD~1"]),
    ("tag-format", REFSET, ["tag", "-l", "--format=%(refname:short) %(objecttype)"]),
    # log --no-abbrev / --no-abbrev-commit force full oids.
    ("log-no-abbrev", TAGGED, ["log", "-1", "--format=%h", "--no-abbrev"]),
    ("log-no-abbrev-commit", TAGGED, ["log", "--abbrev-commit", "--no-abbrev-commit", "-1"]),
    # shortlog lists each author's commits oldest-first; -e adds the email.
    ("shortlog-default", PATHHIST, ["shortlog", "HEAD"]),
    ("shortlog-email", PATHHIST, ["shortlog", "-e", "HEAD"]),
    ("shortlog-summary-email", PATHHIST, ["shortlog", "-se", "HEAD"]),
    # -c groups by committer (the pinned identity) rather than the per-commit author.
    ("shortlog-committer", SHORTLOG_MULTI, ["shortlog", "-c", "HEAD"]),
    ("shortlog-committer-email", SHORTLOG_MULTI, ["shortlog", "-c", "-e", "HEAD"]),
    ("shortlog-numbered", SHORTLOG_MULTI, ["shortlog", "-n", "HEAD"]),
    # --format/--pretty change the per-commit record; named styles fall back to %s.
    ("shortlog-format-h", SHORTLOG_MULTI, ["shortlog", "--format=%h", "HEAD"]),
    ("shortlog-format-h-s", SHORTLOG_MULTI, ["shortlog", "--format=%h %s", "HEAD"]),
    ("shortlog-format-prefix", SHORTLOG_MULTI, ["shortlog", "--format=format:%h", "HEAD"]),
    ("shortlog-pretty-oneline", SHORTLOG_MULTI, ["shortlog", "--pretty=oneline", "HEAD"]),
    ("shortlog-pretty-medium", SHORTLOG_MULTI, ["shortlog", "--pretty=medium", "HEAD"]),
    ("shortlog-pretty-format", SHORTLOG_MULTI, ["shortlog", "--pretty=format:%h", "HEAD"]),
    ("shortlog-pretty-reference", SHORTLOG_MULTI, ["shortlog", "--pretty=reference", "HEAD"]),
    ("shortlog-pretty-bare", SHORTLOG_MULTI, ["shortlog", "--pretty", "HEAD"]),
    ("shortlog-format-invalid", SHORTLOG_MULTI, ["shortlog", "--format=fixed", "HEAD"]),
    ("shortlog-pretty-invalid", SHORTLOG_MULTI, ["shortlog", "--pretty=fixed", "HEAD"]),
    # -w wraps each record line (width,indent1,indent2; 0 = no wrap, indent only).
    ("shortlog-wrap", SHORTLOG_MULTI, ["shortlog", "-w", "HEAD"]),
    ("shortlog-wrap-custom", SHORTLOG_MULTI, ["shortlog", "-w20,2,4", "HEAD"]),
    ("shortlog-wrap-zero", SHORTLOG_MULTI, ["shortlog", "-w0", "HEAD"]),
    ("shortlog-wrap-width-only", SHORTLOG_MULTI, ["shortlog", "-w40", "HEAD"]),
    # diff-tree -t emits changed tree (directory) nodes; without -r it lists
    # only the top-level changed entries.
    ("diff-tree-t", SUBTREE, ["diff-tree", "-t", "-r", "HEAD"]),
    ("diff-tree-t-nor", SUBTREE, ["diff-tree", "-t", "HEAD"]),
    ("diff-tree-nor", SUBTREE, ["diff-tree", "HEAD"]),
    # log --branches/--tags/--remotes walk selected ref namespaces.
    ("log-branches", MERGED, ["log", "--format=%H", "--branches"]),
    ("log-tags", DESC, ["log", "--format=%H", "--tags"]),
    ("log-branches-glob", REFSET, ["log", "--oneline", "--branches=a*"]),
    # archive --list and gc (silent on success).
    ("archive-list", BASE, ["archive", "--list"]),
    ("gc-silent", BASE, ["gc"]),
    # count-objects -H human-readable size; %m mark; %(decorate) atom.
    ("count-objects-H", TAGGED, ["count-objects", "-H"]),
    ("log-fmt-m", BASE, ["log", "-1", "--format=[%m]"]),
    ("log-fmt-decorate",
     BASE + [["tag", "-a", "-m", "r", "v1"]], ["log", "-1", "--format=%(decorate)"]),
    ("log-fmt-decorate-opts",
     BASE + [["tag", "-a", "-m", "r", "v1"]],
     ["log", "-1", "--format=%(decorate:prefix=[,suffix=],separator=; )"]),
    # log/show pretty column alignment %<()/%>()/%><() and truncation.
    ("log-col-left", BODY, ["log", "-1", "--pretty=format:[%<(12)%s]"]),
    ("log-col-right", BODY, ["log", "-1", "--pretty=format:[%>(12)%s]"]),
    ("log-col-center", BODY, ["log", "-1", "--pretty=format:[%><(12)%s]"]),
    ("log-col-trunc", BODY, ["log", "-1", "--pretty=format:[%<(6,trunc)%s]"]),
    ("log-col-mtrunc", BODY, ["log", "-1", "--pretty=format:[%<(6,mtrunc)%s]"]),
    ("log-col-literal", BODY, ["log", "-1", "--pretty=format:[%<(10)X%s]"]),
    ("log-col-multi", BODY, ["log", "-1", "--pretty=format:%<(20)%an %ae"]),
    # cat-file --textconv / --filters stream blob content (identity, no driver).
    ("cat-file-textconv", BASE, ["cat-file", "--textconv", "HEAD:a.txt"]),
    ("cat-file-filters", BASE, ["cat-file", "--filters", "HEAD:a.txt"]),
    # ls-tree --object-only prints only object ids.
    ("ls-tree-object-only", SUBTREE, ["ls-tree", "--object-only", "HEAD"]),
    ("ls-tree-object-only-r", SUBTREE, ["ls-tree", "--object-only", "-r", "HEAD"]),
    # rev-parse --symbolic prints ref names (full for --all, short otherwise).
    ("revparse-symbolic-all", REFSET, ["rev-parse", "--symbolic", "--all"]),
    ("revparse-symbolic-branches", REFSET, ["rev-parse", "--symbolic", "--branches"]),
    ("revparse-symbolic-tags", REFSET, ["rev-parse", "--symbolic", "--tags"]),
    # for-each-ref refname:lstrip/rstrip and %(if)/%(then)/%(else)/%(end).
    ("fer-lstrip", REFSET, ["for-each-ref", "--format=%(refname:lstrip=2)"]),
    ("fer-rstrip", REFSET, ["for-each-ref", "--format=%(refname:rstrip=1)"]),
    ("fer-if-head", REFSET,
     ["for-each-ref", "--format=%(if)%(HEAD)%(then)* %(else)  %(end)%(refname:short)"]),
    ("fer-if-equals", REFSET,
     ["for-each-ref", "--format=%(if:equals=main)%(refname:short)%(then)CUR%(else)-%(end)"]),
    # blame porcelain formats.
    ("blame-porcelain", PATHHIST, ["blame", "-p", "a.txt"]),
    ("blame-line-porcelain", PATHHIST, ["blame", "--line-porcelain", "a.txt"]),
    # reflog --no-abbrev shows full oids; --date renders entry times.
    ("reflog-no-abbrev", TAGGED, ["reflog", "--no-abbrev"]),
    # --date=unix renders the raw epoch (in show's format path too).
    ("show-date-unix", DATED, ["show", "-s", "--format=%ad", "--date=unix", "HEAD"]),
    ("log-date-unix", DATED, ["log", "-1", "--date=unix"]),
    # range-diff: commit matching (= identical, ! changed with inter-diff body,
    # < only-left, > only-right) and the padded "n:  <sha>" header format.
    ("range-diff-identical", RANGEDIFF, ["range-diff", "main..v1", "main..v1"]),
    ("range-diff-changed", RANGEDIFF, ["range-diff", "main..v1", "main..v2"]),
    ("range-diff-reverse", RANGEDIFF, ["range-diff", "main..v2", "main..v1"]),
    # commit-tree resolves the tree-ish argument (not used verbatim).
    ("commit-tree", SUBTREE, ["commit-tree", "-m", "msg", "HEAD^{tree}"]),
    # ls-remote against a local path: HEAD + sorted refs + peeled annotated tags.
    ("ls-remote-local", TAGGED, ["ls-remote", "."]),
    ("ls-remote-tags", TAGGED, ["ls-remote", "--tags", "."]),
    ("ls-remote-heads", REFSET, ["ls-remote", "--heads", "."]),
    ("ls-remote-no-remote", BASE, ["ls-remote"]),
    # config --show-origin prefixes each entry with its source file.
    ("config-show-origin", REMOTE, ["config", "--list", "--show-origin"]),
    # git merge -q suppresses the summary; ensure the flag is accepted and silent.
    ("merge-quiet",
     BASE + [["checkout", "-b", "feat"], ("write", "g.txt", "g\n"), ["add", "-A"],
             ["commit", "-m", "fc1"], ["checkout", "main"],
             ["merge", "-q", "--no-ff", "-m", "m", "feat"]],
     ["log", "--oneline", "-1"]),
    # rev-list traversal ordering: default is a committer-date max-heap; topo /
    # date order reorder the collected set (children always before parents).
    ("rev-list-topo-order", MERGED, ["rev-list", "--topo-order", "HEAD"]),
    ("rev-list-date-order", MERGED, ["rev-list", "--date-order", "HEAD"]),
    ("rev-list-topo-all", MERGED, ["rev-list", "--topo-order", "--all"]),
    ("rev-list-date-all", MERGED, ["rev-list", "--date-order", "--all"]),
    # rev-list ref-namespace tips: --branches/--tags/--remotes (and patterns).
    ("rev-list-branches", REFSET, ["rev-list", "--branches"]),
    ("rev-list-tags", REFSET, ["rev-list", "--tags"]),
    ("rev-list-remotes", REFSET, ["rev-list", "--remotes"]),
    ("rev-list-branches-glob", REFSET, ["rev-list", "--branches=a*"]),
    # rev-list --header: raw commit records separated by NUL.
    ("rev-list-header", TAGGED, ["rev-list", "--header", "HEAD"]),
    # rev-list oid abbreviation and the NUL record terminator (-z).
    ("rev-list-abbrev-commit", TAGGED, ["rev-list", "--abbrev-commit", "HEAD"]),
    ("rev-list-abbrev-n", TAGGED, ["rev-list", "--abbrev=4", "--abbrev-commit", "HEAD"]),
    ("rev-list-z", TAGGED, ["rev-list", "-z", "HEAD"]),
    ("rev-list-parents-abbrev", MERGED,
     ["rev-list", "--parents", "--abbrev-commit", "HEAD"]),
    # rev-list --disk-usage sums on-disk object bytes (loose objects exact).
    ("rev-list-disk-usage", TAGGED, ["rev-list", "--disk-usage", "HEAD"]),
    ("rev-list-disk-usage-human", TAGGED, ["rev-list", "--disk-usage=human", "HEAD"]),
    ("rev-list-disk-usage-objects", TAGGED, ["rev-list", "--disk-usage", "--objects", "HEAD"]),
    # rev-list --unpacked (all loose here) / no-op parent-limit resets.
    ("rev-list-unpacked", TAGGED, ["rev-list", "--unpacked", "HEAD"]),
    ("rev-list-no-min-parents", TAGGED, ["rev-list", "--no-min-parents", "HEAD"]),
    ("rev-list-no-max-parents", TAGGED, ["rev-list", "--no-max-parents", "HEAD"]),
    ("rev-list-remove-empty", TAGGED, ["rev-list", "--remove-empty", "HEAD"]),
    # rev-list --objects-edge prefixes boundary commits with '-' and omits the
    # objects already reachable from the uninteresting side.
    ("rev-list-objects-edge", TAGGED, ["rev-list", "--objects-edge", "v1..HEAD"]),
    ("rev-list-objects-exclude", MERGED, ["rev-list", "--objects", "HEAD~1..HEAD"]),
    # rev-list --bisect / --bisect-all / --bisect-vars: halving point, full
    # sorted set, and the shell-eval bisection variables.
    ("rev-list-bisect", TAGGED, ["rev-list", "--bisect", "HEAD"]),
    ("rev-list-bisect-all", MERGED, ["rev-list", "--bisect-all", "HEAD"]),
    ("rev-list-bisect-range", PATHHIST, ["rev-list", "--bisect", "HEAD", "^HEAD~2"]),
    ("rev-list-bisect-vars", MERGED, ["rev-list", "--bisect-vars", "HEAD"]),
    ("rev-list-bisect-vars-range", PATHHIST, ["rev-list", "--bisect-vars", "HEAD", "^HEAD~2"]),
    # rev-list --quiet suppresses output; --no-abbrev forces full oids;
    # --max-age/--min-age are raw-epoch date bounds.
    ("rev-list-quiet", TAGGED, ["rev-list", "--quiet", "HEAD"]),
    ("rev-list-no-abbrev", TAGGED, ["rev-list", "--abbrev-commit", "--no-abbrev", "HEAD"]),
    ("rev-list-max-age", TAGGED, ["rev-list", "--max-age=1700000000", "HEAD"]),
    ("rev-list-min-age", TAGGED, ["rev-list", "--min-age=1700000000", "HEAD"]),
    # for-each-ref quoting modes (--shell/--perl/--python/--tcl), ref-namespace
    # filters, root refs, exclusion, pagination and object-arg error formats.
    ("fer-shell", REFSET, ["for-each-ref", "--shell", "--format=%(refname) %(objecttype)"]),
    ("fer-perl", REFSET, ["for-each-ref", "--perl", "--format=%(refname)"]),
    ("fer-python", REFSET, ["for-each-ref", "--python", "--format=%(refname)"]),
    ("fer-tcl", REFSET, ["for-each-ref", "--tcl", "--format=%(refname)"]),
    ("fer-points-at", DESC, ["for-each-ref", "--points-at=HEAD", "--format=%(refname)"]),
    ("fer-merged", MERGED, ["for-each-ref", "--merged=HEAD", "--format=%(refname)"]),
    ("fer-no-merged", MERGED, ["for-each-ref", "--no-merged=HEAD", "--format=%(refname)"]),
    ("fer-contains", DESC, ["for-each-ref", "--contains=v1", "--format=%(refname)"]),
    ("fer-no-contains", DESC, ["for-each-ref", "--no-contains=v1", "--format=%(refname)"]),
    ("fer-merged-lastarg", MERGED, ["for-each-ref", "--format=%(refname)", "--merged"]),
    ("fer-exclude", REFSET, ["for-each-ref", "--exclude=refs/tags/*", "--format=%(refname)"]),
    ("fer-include-root", REFSET, ["for-each-ref", "--include-root-refs", "--format=%(refname)"]),
    ("fer-start-after", REFSET, ["for-each-ref", "--start-after=refs/heads/main", "--format=%(refname)"]),
    ("fer-points-at-bad", REFSET, ["for-each-ref", "--points-at=nope"]),
    ("fer-merged-bad", REFSET, ["for-each-ref", "--merged=nope"]),
    ("fer-contains-bad", REFSET, ["for-each-ref", "--contains=nope"]),
    # tag listing filters and columnar/case-insensitive output.
    ("tag-merged", TAGREPO, ["tag", "--merged=HEAD"]),
    ("tag-no-merged", TAGREPO, ["tag", "--no-merged=HEAD"]),
    ("tag-merged-lastarg", TAGREPO, ["tag", "-l", "--merged"]),
    ("tag-column", REFSET, ["tag", "--column"]),
    ("tag-column-plain", REFSET, ["tag", "--column=plain"]),
    ("tag-no-column", REFSET, ["tag", "--no-column"]),
    ("tag-ignore-case", REFSET, ["tag", "-i", "-l", "V*"]),
    # tag -v / --verify on a lightweight tag, an annotated (unsigned) tag, and a
    # missing tag — each fails with git's exact message and return code.
    ("tag-verify-lightweight", REFSET, ["tag", "-v", "v1"]),
    ("tag-verify-annotated", DESC, ["tag", "-v", "v1"]),
    ("tag-verify-missing", BASE, ["tag", "-v", "nope"]),
    # tag creation: -F <file>, --trailer, multi -m (probed via the tag object).
    ("tag-file", TAGMSGFILE + [["tag", "-a", "-F", "tmsg.txt", "ft"]],
     ["cat-file", "-p", "ft"]),
    ("tag-trailer", BASE + [["tag", "-a", "-m", "subj", "--trailer", "Acked-by: X", "tr"]],
     ["cat-file", "-p", "tr"]),
    ("tag-multi-m", BASE + [["tag", "-a", "-m", "line1", "-m", "line2", "mm"]],
     ["cat-file", "-p", "mm"]),
    # branch listing filters (--points-at/--no-contains/-i) and the creation /
    # tracking flags (-f, -q, -t/--track, -u/--set-upstream-to).
    ("branch-points-at", REFSET, ["branch", "--points-at=HEAD"]),
    ("branch-no-contains", SB_FORK, ["branch", "--no-contains=feat"]),
    ("branch-ignore-case", REFSET, ["branch", "-i", "-l", "A*"]),
    ("branch-exists-error", BASE + [["branch", "dup"]], ["branch", "dup"]),
    ("branch-force", BASE + [["branch", "dup"]], ["branch", "-f", "dup", "HEAD"]),
    ("branch-track", REFSET, ["branch", "-t", "trk", "main"]),
    ("branch-track-quiet", REFSET, ["branch", "-q", "-t", "trkq", "main"]),
    ("branch-track-config", REFSET + [["branch", "-t", "trk", "main"]],
     ["config", "--get-regexp", r"branch\.trk\."]),
    ("branch-set-upstream", REFSET + [["branch", "ups"]], ["branch", "-u", "main", "ups"]),
    ("branch-set-upstream-bad", BASE, ["branch", "-u", "origin/nope"]),
    # ls-files exclusion (-x/-X/-i/--exclude-standard), --format, -v/-f tags,
    # -u unmerged listing, combined selectors, and the --format conflict error.
    ("lsf-exclude", LSIGNORE, ["ls-files", "-o", "-x", "*.txt"]),
    ("lsf-exclude-multi", LSIGNORE, ["ls-files", "-o", "-x", "*.log", "-x", "*.txt"]),
    ("lsf-ignored-standard", LSIGNORE, ["ls-files", "-o", "-i", "--exclude-standard"]),
    ("lsf-ignored-x", LSIGNORE, ["ls-files", "-o", "-i", "-x", "*.log"]),
    ("lsf-exclude-standard", LSIGNORE, ["ls-files", "-o", "--exclude-standard"]),
    ("lsf-format-path", BASE, ["ls-files", "--format", "%(path)"]),
    ("lsf-format-atoms", BASE,
     ["ls-files", "--format", "%(objectmode) %(objectname) %(objecttype) %(path)"]),
    ("lsf-format-size", BASE, ["ls-files", "--format", "%(objectmode) %(objectsize) %(path)"]),
    ("lsf-v", BASE, ["ls-files", "-v"]),
    ("lsf-f", BASE, ["ls-files", "-f"]),
    ("lsf-combined", LSIGNORE, ["ls-files", "-cdmo"]),
    ("lsf-format-conflict", BASE, ["ls-files", "-s", "--format", "%(path)"]),
    ("lsf-unmerged", LSCONFLICT, ["ls-files", "-u"]),
    ("lsf-unmerged-stage", LSCONFLICT, ["ls-files", "-s"]),
    ("lsf-unmerged-tag", LSCONFLICT, ["ls-files", "-t"]),
    ("lsf-unmerged-default", LSCONFLICT, ["ls-files"]),
    # add staging modes: -u (tracked only), --no-all/--ignore-removal (skip
    # removals), --pathspec-from-file. Each is probed via the resulting status.
    ("add-update", ADDSETUP + [["add", "-u"]], ["status", "--short"]),
    ("add-update-path", ADDSETUP + [["add", "-u", "a.txt"]], ["status", "--short"]),
    ("add-no-all", ADDSETUP + [["add", "--no-all", "."]], ["status", "--short"]),
    ("add-ignore-removal", ADDSETUP + [["add", "--ignore-removal", "."]], ["status", "--short"]),
    ("add-no-all-paths", ADDSETUP + [["add", "--no-all", "a.txt", "b.txt"]], ["status", "--short"]),
    ("add-pathspec-from-file",
     ADDSETUP + [("write", "ps.txt", "a.txt\nc.txt\n"), ["add", "--pathspec-from-file", "ps.txt"]],
     ["status", "--short"]),
    # update-index --assume-unchanged: ls-files -v lowercases the tag and status
    # ignores the entry's worktree changes.
    ("ui-assume-lsv", BASE + [["update-index", "--assume-unchanged", "a.txt"]],
     ["ls-files", "-v"]),
    ("ui-assume-status",
     BASE + [["update-index", "--assume-unchanged", "a.txt"], ("write", "a.txt", "changed\n")],
     ["status", "--short"]),
    ("ui-no-assume",
     BASE + [["update-index", "--assume-unchanged", "a.txt"],
             ["update-index", "--no-assume-unchanged", "a.txt"]],
     ["ls-files", "-v"]),
    ("ui-assume-bad", BASE, ["update-index", "--assume-unchanged", "nope.txt"]),
    # index v3: intent-to-add (add -N) and skip-worktree, with their observable
    # status/ls-files/diff/version effects.
    ("add-N-status", BASE + [("write", "new.txt", "n\n"), ["add", "-N", "new.txt"]],
     ["status", "--short"]),
    ("add-N-lsfiles", BASE + [("write", "new.txt", "n\n"), ["add", "-N", "new.txt"]],
     ["ls-files", "-s"]),
    ("add-N-diff", BASE + [("write", "new.txt", "n\n"), ["add", "-N", "new.txt"]],
     ["diff"]),
    ("add-N-version", BASE + [("write", "new.txt", "n\n"), ["add", "-N", "new.txt"]],
     ["update-index", "--show-index-version"]),
    ("add-N-commit-nothing",
     BASE + [("write", "new.txt", "n\n"), ["add", "-N", "new.txt"], ["commit", "-m", "x"]],
     ["status", "--short"]),
    ("ui-skip-worktree",
     BASE + [["update-index", "--skip-worktree", "a.txt"], ("write", "a.txt", "changed\n")],
     ["status", "--short"]),
    ("ui-skip-lsfiles-t", BASE + [["update-index", "--skip-worktree", "a.txt"]],
     ["ls-files", "-t"]),
    ("ui-no-skip-worktree",
     BASE + [["update-index", "--skip-worktree", "a.txt"], ("write", "a.txt", "changed\n"),
             ["update-index", "--no-skip-worktree", "a.txt"]],
     ["status", "--short"]),
    # reset --keep / --merge two-way-merge semantics (state probed via status),
    # abort messages, the full mixed "Unstaged changes after reset:" report, the
    # with-paths guard, and --pathspec-from-file. (reset-in-setup → status probe
    # for success cases; reset-as-probe for the message/abort cases.)
    ("reset-keep-kept",
     RESETBASE + [("write", "b.txt", "b-local\n"), ["reset", "--keep", "HEAD~1"]],
     ["status", "--short"]),
    ("reset-keep-conflict", RESETBASE + [("write", "a.txt", "a-local\n")],
     ["reset", "--keep", "HEAD~1"]),
    ("reset-keep-staged-conflict",
     RESETBASE + [("write", "a.txt", "a3\n"), ["add", "a.txt"]],
     ["reset", "--keep", "HEAD~1"]),
    ("reset-merge-discard-staged",
     RESETBASE + [("write", "b.txt", "bs\n"), ["add", "b.txt"], ["reset", "--merge", "HEAD~1"]],
     ["status", "--short"]),
    ("reset-merge-conflict", RESETBASE + [("write", "a.txt", "a-local\n")],
     ["reset", "--merge", "HEAD~1"]),
    ("reset-mixed-unstaged", RESETBASE + [("write", "a.txt", "a-wt\n")],
     ["reset", "HEAD~1"]),
    ("reset-mixed-no-refresh", RESETBASE + [("write", "a.txt", "a-wt\n")],
     ["reset", "--no-refresh", "HEAD~1"]),
    ("reset-mixed-quiet", RESETBASE + [("write", "a.txt", "a-wt\n")],
     ["reset", "-q", "HEAD~1"]),
    ("reset-merge-paths-err", RESETBASE, ["reset", "--merge", "HEAD~1", "--", "a.txt"]),
    ("reset-soft-paths-err", RESETBASE, ["reset", "--soft", "HEAD~1", "--", "a.txt"]),
    ("reset-pathspec-from-file",
     RESETBASE + [("write", "a.txt", "a-wt\n"), ("write", "specs", "a.txt\n")],
     ["reset", "HEAD~1", "--pathspec-from-file", "specs"]),
    # reset -N: a file added in the last commit is re-marked intent-to-add.
    ("reset-intent-to-add",
     BASE + [("write", "new.txt", "n\n"), ["add", "-A"], ["commit", "-m", "c2"]],
     ["reset", "-N", "HEAD~1"]),
    # show-branch display flags: -a, topo/date order, --current, --list,
    # --no-name/--sha1-name naming, --more extension, --topics filter, and the
    # --color=always per-column marker palette.
    ("sb-all", SB_THREE, ["show-branch", "-a"]),
    ("sb-topo-order", SB_THREE, ["show-branch", "--topo-order"]),
    ("sb-date-order", SB_THREE, ["show-branch", "--date-order"]),
    ("sb-current", SB_THREE, ["show-branch", "--current"]),
    ("sb-list", SB_THREE, ["show-branch", "--list"]),
    ("sb-no-name", SB_FORK, ["show-branch", "--no-name", "main", "feat"]),
    ("sb-sha1-name", SB_FORK, ["show-branch", "--sha1-name", "main", "feat"]),
    ("sb-more", SB_FORK, ["show-branch", "--more=2", "main", "feat"]),
    ("sb-topics", SB_THREE, ["show-branch", "--topics", "main", "feat"]),
    ("sb-color", SB_THREE, ["show-branch", "--color=always"]),
    ("sb-color-explicit", SB_FORK, ["show-branch", "--color=always", "main", "feat"]),
    ("sb-no-color", SB_THREE, ["show-branch", "--no-color"]),
    # merge flags: -n (no diffstat), -s strategy (name echoed; ours; unknown
    # error), --no-verify, -X ours/theirs conflict resolution, the "Auto-merging"
    # notice, and -F file-read error.
    ("merge-n", MERGEDIV, ["merge", "-n", "--no-ff", "-m", "m", "feat"]),
    ("merge-stat", MERGEDIV, ["merge", "--stat", "--no-ff", "-m", "m", "feat"]),
    ("merge-s-recursive", MERGEDIV, ["merge", "-s", "recursive", "--no-ff", "-m", "m", "feat"]),
    ("merge-s-ours", MERGEDIV, ["merge", "-s", "ours", "-m", "m", "feat"]),
    ("merge-s-bogus", MERGEDIV, ["merge", "-s", "bogus", "-m", "m", "feat"]),
    ("merge-no-verify", MERGEDIV, ["merge", "--no-verify", "--no-ff", "-m", "m", "feat"]),
    ("merge-auto-merging", MERGEBOTH, ["merge", "--no-ff", "-m", "m", "feat"]),
    ("merge-X-ours", MERGECONF, ["merge", "-X", "ours", "--no-ff", "-m", "m", "feat"]),
    ("merge-X-theirs", MERGECONF, ["merge", "-X", "theirs", "--no-ff", "-m", "m", "feat"]),
    ("merge-conflict", MERGECONF, ["merge", "--no-ff", "-m", "m", "feat"]),
    ("merge-F-bad", MERGEDIV, ["merge", "-F", "nope.txt", "--no-ff", "feat"]),
    # ls-remote on a local path: --refs (drop HEAD + peeled), --symref, -b/
    # --branches namespace filter, --get-url, --sort, patterns, --exit-code, and
    # the boolean "takes no value" error.
    ("lsr-refs", DESC, ["ls-remote", "--refs", "."]),
    ("lsr-symref", DESC, ["ls-remote", "--symref", "."]),
    ("lsr-refs-symref", DESC, ["ls-remote", "--refs", "--symref", "."]),
    ("lsr-branches", REFSET, ["ls-remote", "--branches", "."]),
    ("lsr-b", REFSET, ["ls-remote", "-b", "."]),
    ("lsr-get-url", BASE, ["ls-remote", "--get-url", "."]),
    ("lsr-sort-rev", REFSET, ["ls-remote", "--sort=-refname", "."]),
    ("lsr-pattern", REFSET, ["ls-remote", ".", "main"]),
    ("lsr-exit-code-nomatch", BASE, ["ls-remote", "--exit-code", ".", "nomatchxyz"]),
    ("lsr-exit-code-ok", BASE, ["ls-remote", "--exit-code", "."]),
    ("lsr-branches-val-err", BASE, ["ls-remote", "--branches=val", "."]),
    # notes: append, --separator, copy, -f overwrite, list, remove, get-ref,
    # --ref, --allow-empty, and the no-force "existing notes" error.
    ("notes-append", BASE + [["notes", "add", "-m", "one"], ["notes", "append", "-m", "two"]],
     ["notes", "show"]),
    ("notes-separator",
     BASE + [["notes", "add", "-m", "l1", "--separator=---", "-m", "l2"]], ["notes", "show"]),
    ("notes-copy", TAGGED + [["notes", "add", "-m", "cp", "HEAD"], ["notes", "copy", "HEAD", "HEAD~1"]],
     ["notes", "show", "HEAD~1"]),
    ("notes-add-exists-err", BASE + [["notes", "add", "-m", "a"]], ["notes", "add", "-m", "b"]),
    ("notes-add-force", BASE + [["notes", "add", "-m", "a"]], ["notes", "add", "-f", "-m", "b"]),
    ("notes-list", TAGGED + [["notes", "add", "-m", "n1", "HEAD"], ["notes", "add", "-m", "n2", "HEAD~1"]],
     ["notes", "list"]),
    ("notes-list-obj", BASE + [["notes", "add", "-m", "n1"]], ["notes", "list", "HEAD"]),
    ("notes-remove", BASE + [["notes", "add", "-m", "n"]], ["notes", "remove", "HEAD"]),
    ("notes-get-ref", BASE, ["notes", "get-ref"]),
    ("notes-ref-custom", BASE + [["notes", "--ref=review", "add", "-m", "rn", "HEAD"]],
     ["notes", "--ref=review", "show", "HEAD"]),
    ("notes-allow-empty", BASE + [["notes", "add", "--allow-empty"]], ["notes", "show"]),
    # remote subcommands (config/ref-driven): add -t/--tags/--mirror/-m, rename,
    # set-url variants, get-url, set-branches --add, and the exact errors.
    ("remote-add-track",
     BASE + [["remote", "add", "-t", "main", "origin", "https://x/r.git"]],
     ["config", "--get-regexp", r"^remote\."]),
    ("remote-add-tags",
     BASE + [["remote", "add", "--tags", "o", "https://x/r.git"]],
     ["config", "--get-regexp", r"^remote\."]),
    ("remote-add-mirror-push",
     BASE + [["remote", "add", "--mirror=push", "o", "https://x/r.git"]],
     ["config", "--get-regexp", r"^remote\."]),
    ("remote-add-mirror-fetch",
     BASE + [["remote", "add", "--mirror=fetch", "o", "https://x/r.git"]],
     ["config", "--get-regexp", r"^remote\."]),
    ("remote-rename",
     BASE + [["remote", "add", "o5", "https://y/r.git"], ["remote", "rename", "o5", "o6"]],
     ["config", "--get-regexp", r"^remote\."]),
    ("remote-set-url",
     BASE + [["remote", "add", "o", "https://x/r.git"], ["remote", "set-url", "o", "https://y/r.git"]],
     ["config", "--get-regexp", r"^remote\."]),
    ("remote-set-url-push",
     BASE + [["remote", "add", "o", "https://x/r.git"], ["remote", "set-url", "--push", "o", "https://p/r.git"]],
     ["config", "--get-regexp", r"^remote\."]),
    ("remote-set-url-add",
     BASE + [["remote", "add", "o", "https://x/r.git"], ["remote", "set-url", "--add", "o", "https://x/e.git"]],
     ["config", "--get-regexp", r"^remote\."]),
    ("remote-set-branches-add",
     BASE + [["remote", "add", "o", "https://x/r.git"], ["remote", "set-branches", "--add", "o", "dev"]],
     ["config", "--get-regexp", r"^remote\."]),
    ("remote-get-url", BASE + [["remote", "add", "o", "https://y/r.git"]],
     ["remote", "get-url", "o"]),
    ("remote-get-url-push",
     BASE + [["remote", "add", "o", "https://y/r.git"], ["remote", "set-url", "--push", "o", "https://p/r.git"]],
     ["remote", "get-url", "--push", "o"]),
    ("remote-add-m-symref",
     BASE + [["remote", "add", "-m", "main", "origin", "https://x/r.git"]],
     ["symbolic-ref", "refs/remotes/origin/HEAD"]),
    ("remote-add-exists-err", BASE + [["remote", "add", "o", "https://x/r.git"]],
     ["remote", "add", "o", "https://x/d.git"]),
    ("remote-remove-nonexist", BASE, ["remote", "remove", "nope"]),
    ("remote-v", BASE + [["remote", "add", "o", "https://x/r.git"]], ["remote", "-v"]),
    # stash push -q/-m, save <msg>, and -k/--keep-index.
    ("stash-push-q", BASE + [("write", "a.txt", "mod\n")], ["stash", "push", "-q"]),
    ("stash-push-m",
     BASE + [("write", "a.txt", "mod\n"), ["stash", "push", "-m", "my msg"]], ["stash", "list"]),
    ("stash-save-msg",
     BASE + [("write", "a.txt", "mod\n"), ["stash", "save", "saved msg"]], ["stash", "list"]),
    ("stash-keep-index",
     BASE + [("write", "a.txt", "staged\n"), ["add", "a.txt"], ("write", "b.txt", "wt\n"),
             ["stash", "push", "-k"]],
     ["status", "--short"]),
    ("stash-push-nochanges", BASE, ["stash", "push"]),
    ("stash-bare-q", BASE + [("write", "a.txt", "mod\n")], ["stash", "-q"]),
    ("stash-bare-m",
     BASE + [("write", "a.txt", "mod\n"), ["stash", "-m", "bare msg"]], ["stash", "list"]),
    # --- workflow batch 1: update-server-info / prune-packed / prune ---
    ('update-server-info-force-basic', BASE, ["update-server-info", "-f"]),
    ('update-server-info-force-long', BASE, ["update-server-info", "--force"]),
    ('update-server-info-no-force', BASE, ["update-server-info", "--no-force"]),
    ('update-server-info-bare', BASE, ["update-server-info"]),
    ('update-server-info-tags-force', BASE + [["tag", "light"], ["tag", "-a", "-m", "ann", "annot"], ["branch", "feature"]], ["update-server-info", "-f"]),
    ('prune-packed-dry-run', BASE + [["repack", "-a"]], ["prune-packed", "-n"]),
    ('prune-packed-dry-run-long', BASE + [["repack", "-a"]], ["prune-packed", "--dry-run"]),
    ('prune-packed-quiet-dry-run', BASE + [["repack", "-a"]], ["prune-packed", "-q", "-n"]),
    ('prune-packed-actual', BASE + [["repack", "-a"]], ["prune-packed"]),
    ('prune-packed-quiet-actual', BASE + [["repack", "-a"]], ["prune-packed", "-q"]),
    ('prune-packed-no-duplicates', BASE, ["prune-packed", "-n"]),
    ('prune-packed-too-many-args', BASE, ["prune-packed", "foo"]),
    ('prune-packed-unknown-option', BASE, ["prune-packed", "--bogus"]),
    ('prune-packed-unknown-switch', BASE, ["prune-packed", "-x"]),
    ('prune-packed-help', BASE, ["prune-packed", "-h"]),
    ('prune-dry-run-lists-dangling', BASE + [("write","x.txt","x\n"),["hash-object","-w","x.txt"]], ["prune", "-n"]),
    ('prune-verbose-removes-and-lists', BASE + [("write","x.txt","x\n"),["hash-object","-w","x.txt"]], ["prune", "-v"]),
    ('prune-default-silent', BASE + [("write","x.txt","x\n"),["hash-object","-w","x.txt"]], ["prune"]),
    ('prune-expire-never-keeps', BASE + [("write","x.txt","x\n"),["hash-object","-w","x.txt"]], ["prune", "-n", "--expire=never"]),
    ('prune-expire-future-prunes', BASE + [("write","x.txt","x\n"),["hash-object","-w","x.txt"]], ["prune", "-n", "--expire=2099-01-01"]),
    ('prune-expire-malformed', BASE, ["prune", "-n", "--expire=bogus"]),
    ('prune-bad-head-arg', BASE, ["prune", "-n", "notarev"]),
    ('prune-no-progress', BASE + [("write","x.txt","x\n"),["hash-object","-w","x.txt"]], ["prune", "-n", "--no-progress"]),
]


CASES_WITH_STDIN: list[tuple] = [
    # commit --pathspec-from-file: read the partial-commit pathspec from a file
    # (here "-" reads the pathspec from stdin), message supplied via -F -.
    ("commit-pathspec-from-file",
     BASE + [("write", "a.txt", "x\n"), ("write", "b.txt", "y\n"), ("write", "specs", "a.txt\n")],
     ["commit", "-F", "-", "--pathspec-from-file", "specs"], "msg\n"),
    ("commit-pathspec-file-nul",
     BASE + [("write", "a.txt", "x\n"), ("write", "b.txt", "y\n"), ("write", "specs", "a.txt\0b.txt")],
     ["commit", "-F", "-", "--pathspec-from-file", "specs", "--pathspec-file-nul"], "msg\n"),
    # show-ref --exclude-existing filters stdin refnames (strip ^{}, warn on
    # ill-formed, drop existing, optional prefix pattern).
    ("showref-exclude-existing", SHOWREF, ["show-ref", "--exclude-existing"],
     "refs/heads/main\nrefs/heads/zzz\nfoo\n"),
    ("showref-exclude-caret", SHOWREF, ["show-ref", "--exclude-existing"],
     "refs/tags/zzz^{}\nrefs/tags/v1\n"),
    ("showref-exclude-pattern", SHOWREF, ["show-ref", "--exclude-existing=refs/tags/"],
     "refs/tags/v1\nrefs/tags/new\nrefs/heads/main\n"),
    # mktree builds a tree from ls-tree-format stdin (mode stored verbatim as %o,
    # type/availability checked, --missing/--batch/-z, empty input -> empty tree).
    ("mktree-std", MKTREE, ["mktree"], f"100644 blob {HELLO_BLOB}\ta\n"),
    ("mktree-noncanon-mode", MKTREE, ["mktree"], f"100664 blob {HELLO_BLOB}\ta\n"),
    ("mktree-exec", MKTREE, ["mktree"], f"100755 blob {HELLO_BLOB}\ta\n"),
    ("mktree-leading-zero", MKTREE, ["mktree"], f"0100644 blob {HELLO_BLOB}\ta\n"),
    ("mktree-empty", MKTREE, ["mktree"], ""),
    ("mktree-type-mismatch", MKTREE, ["mktree"], f"040000 tree {HELLO_BLOB}\tsub\n"),
    ("mktree-missing", MKTREE, ["mktree"], f"100644 blob {'0' * 40}\tx\n"),
    ("mktree-missing-ok", MKTREE, ["mktree", "--missing"], f"100644 blob {'0' * 40}\tx\n"),
    ("mktree-invalid-type", MKTREE, ["mktree"], f"100644 xyz {HELLO_BLOB}\ta\n"),
    ("mktree-bad-format", MKTREE, ["mktree"], "garbage line\n"),
    ("mktree-batch", MKTREE, ["mktree", "--batch"],
     f"100644 blob {HELLO_BLOB}\ta\n\n100644 blob {HELLO_BLOB}\tb\n"),
    ("mktree-batch-trailing-nl", MKTREE, ["mktree", "--batch"],
     f"100644 blob {HELLO_BLOB}\ta\n\n"),
    ("mktree-blank-nonbatch", MKTREE, ["mktree"], f"100644 blob {HELLO_BLOB}\ta\n\n"),
    ("mktree-z", MKTREE, ["mktree", "-z"], f"100644 blob {HELLO_BLOB}\ta\0"),
    ("cat-file-batch-check", BASE, ["cat-file", "--batch-check"], "HEAD\n"),
    ("cat-file-batch-check-fmt", BASE,
     ["cat-file", "--batch-check=%(objecttype) %(objectsize) %(objectname)"], "HEAD\nHEAD:a.txt\n"),
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
    # for-each-ref --stdin reads ref patterns from stdin.
    ("fer-stdin", REFSET, ["for-each-ref", "--stdin", "--format=%(refname)"],
     "refs/heads/*\n"),
    # name-rev --annotate-stdin / --stdin annotate oids on stdin in place.
    ("name-rev-annotate-stdin", BASE + [["tag", "-a", "-m", "r", "v1"]],
     ["name-rev", "--annotate-stdin"], "a158a009e0dde60c7a948f10fd523b2bf897d3ad\n"),
    ("name-rev-stdin-deprecated", BASE + [["tag", "-a", "-m", "r", "v1"]],
     ["name-rev", "--stdin"], "a158a009e0dde60c7a948f10fd523b2bf897d3ad\n"),
    # apply with no patch in the input errors (rc 128).
    ("apply-empty", BASE, ["apply", "--check"], ""),
    ("apply-empty-stat", BASE, ["apply", "--stat"], ""),
    # interpret-trailers --only-trailers; column --mode=plain passthrough.
    ("interpret-trailers-only", BASE, ["interpret-trailers", "--only-trailers"],
     "subject line\n\nSigned-off-by: A U Thor <a@u.thor>\nReviewed-by: R <r@e>\n"),
    ("column-plain", BASE, ["column", "--mode=plain"], "alpha\nbeta\ngamma\n"),
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


# pack-refs is transparent to every ref-reading command (loose and packed refs
# resolve identically), so its parity can only be observed by comparing the
# on-disk packed-refs file and the surviving loose refs directly.
PACK_REFS_SETUP = [
    ("write", "a.txt", "1\n"), ["add", "-A"], ["commit", "-m", "first"],
    ["branch", "dev"], ["tag", "v1"], ["tag", "-a", "-m", "anno", "v2"],
    ["update-ref", "refs/remotes/origin/main", "HEAD"],
]
PACK_REFS_CASES = [
    ("default", PACK_REFS_SETUP, ["pack-refs"]),
    ("all", PACK_REFS_SETUP, ["pack-refs", "--all"]),
    ("all-no-prune", PACK_REFS_SETUP, ["pack-refs", "--all", "--no-prune"]),
    ("prune-explicit", PACK_REFS_SETUP, ["pack-refs", "--prune"]),
    ("include-heads", PACK_REFS_SETUP, ["pack-refs", "--include", "refs/heads/*"]),
    ("all-exclude-tags", PACK_REFS_SETUP, ["pack-refs", "--all", "--exclude", "refs/tags/*"]),
    ("include-two", PACK_REFS_SETUP,
     ["pack-refs", "--include", "refs/heads/*", "--include", "refs/tags/*"]),
    ("exclude-specific", PACK_REFS_SETUP, ["pack-refs", "--all", "--exclude", "refs/tags/v1"]),
    ("auto-small-noop", PACK_REFS_SETUP, ["pack-refs", "--auto"]),
    ("no-tags-header-only",
     [("write", "a.txt", "1\n"), ["add", "-A"], ["commit", "-m", "f"], ["branch", "dev"]],
     ["pack-refs"]),
]


@pytest.mark.parametrize("case", PACK_REFS_CASES, ids=[c[0] for c in PACK_REFS_CASES])
def test_pack_refs_state_parity(case, tmp_path: Path, git_254_oracle: str):
    import subprocess
    from tests.git_parity.support import DETERMINISTIC_ENV, ROOT, pygit_cmd

    _id, setup, probe = case
    env = dict(__import__("os").environ)
    env.update(DETERMINISTIC_ENV)
    env["PYTHONPATH"] = str(ROOT)

    def snapshot(repo: Path):
        pf = repo / ".git" / "packed-refs"
        packed = pf.read_text() if pf.exists() else "<no-packed-refs>"
        refs_dir = repo / ".git" / "refs"
        loose = sorted(
            str(p.relative_to(repo / ".git")).replace("\\", "/")
            for p in refs_dir.rglob("*") if p.is_file()
        )
        return packed, loose

    results = {}
    for tool, base in (("oracle", [git_254_oracle]), ("pygit", pygit_cmd())):
        repo = tmp_path / tool
        repo.mkdir()
        subprocess.run([*base, "init", "-b", "main", "."], cwd=repo, env=env, capture_output=True)
        for step in setup:
            if isinstance(step, tuple) and step and step[0] == "write":
                (repo / step[1]).parent.mkdir(parents=True, exist_ok=True)
                (repo / step[1]).write_text(step[2])
            else:
                subprocess.run([*base, *step], cwd=repo, env=env, capture_output=True)
        proc = subprocess.run([*base, *probe], cwd=repo, env=env, capture_output=True, text=True)
        results[tool] = (proc.returncode, proc.stdout, proc.stderr, *snapshot(repo))

    assert results["pygit"] == results["oracle"]


# fmt-merge-msg reads FETCH_HEAD-format lines whose first field is a real commit
# id, so its inputs can't be a static literal — this resolves the placeholder to
# the actual (deterministic) sha before feeding stdin to both binaries.
FMT_MERGE_MSG_CASES = [
    ("fmt-merge-msg-branch-title",
     BASE + [["checkout", "-b", "feature"], ("write", "f.txt", "x\n"), ["add", "-A"],
             ["commit", "-m", "add f"], ["checkout", "main"]],
     {"SHA": "feature"}, "{SHA}\t\tbranch 'feature' of .\n", ["fmt-merge-msg"]),
    ("fmt-merge-msg-log",
     BASE + [["checkout", "-b", "feature"], ("write", "f1.txt", "1\n"), ["add", "-A"],
             ["commit", "-m", "c1"], ("write", "f2.txt", "2\n"), ["add", "-A"],
             ["commit", "-m", "c2"], ["checkout", "main"]],
     {"SHA": "feature"}, "{SHA}\t\tbranch 'feature' of .\n", ["fmt-merge-msg", "--log"]),
    ("fmt-merge-msg-no-log",
     BASE + [["checkout", "-b", "feature"], ("write", "f.txt", "x\n"), ["add", "-A"],
             ["commit", "-m", "c1"], ["checkout", "main"]],
     {"SHA": "feature"}, "{SHA}\t\tbranch 'feature' of .\n", ["fmt-merge-msg", "--no-log"]),
    ("fmt-merge-msg-m",
     BASE + [["checkout", "-b", "feature"], ("write", "f.txt", "x\n"), ["add", "-A"],
             ["commit", "-m", "c1"], ["checkout", "main"]],
     {"SHA": "feature"}, "{SHA}\t\tbranch 'feature' of .\n", ["fmt-merge-msg", "-m", "custom start"]),
    ("fmt-merge-msg-two-branches",
     BASE + [["checkout", "-b", "b1"], ("write", "x.txt", "1\n"), ["add", "-A"], ["commit", "-m", "x1"],
             ["checkout", "main"], ["checkout", "-b", "b2"], ("write", "y.txt", "2\n"), ["add", "-A"],
             ["commit", "-m", "x2"], ["checkout", "main"]],
     {"B1": "b1", "B2": "b2"}, "{B1}\t\tbranch 'b1' of .\n{B2}\t\tbranch 'b2' of .\n", ["fmt-merge-msg"]),
    ("fmt-merge-msg-not-for-merge",
     BASE + [["checkout", "-b", "feature"], ("write", "f.txt", "x\n"), ["add", "-A"],
             ["commit", "-m", "c1"], ["checkout", "main"]],
     {"SHA": "feature"}, "{SHA}\tnot-for-merge\tbranch 'feature' of .\n", ["fmt-merge-msg"]),
    ("fmt-merge-msg-error-bad-line", BASE, {}, "short\n", ["fmt-merge-msg"]),
    ("fmt-merge-msg-empty-input", BASE, {}, "", ["fmt-merge-msg"]),
    ("fmt-merge-msg-log-bad-int", BASE, {}, "", ["fmt-merge-msg", "--log=abc"]),
]


@pytest.mark.parametrize("case", FMT_MERGE_MSG_CASES, ids=[c[0] for c in FMT_MERGE_MSG_CASES])
def test_fmt_merge_msg_parity(case, tmp_path: Path, git_254_oracle: str):
    import subprocess
    from tests.git_parity.support import DETERMINISTIC_ENV, ROOT, pygit_cmd

    _id, setup, placeholders, stdin_tmpl, argv = case
    env = dict(__import__("os").environ)
    env.update(DETERMINISTIC_ENV)
    env["PYTHONPATH"] = str(ROOT)
    results = {}
    for tool, base in (("oracle", [git_254_oracle]), ("pygit", pygit_cmd())):
        repo = tmp_path / tool
        repo.mkdir()
        subprocess.run([*base, "init", "-b", "main", "."], cwd=repo, env=env, capture_output=True)
        for step in setup:
            if isinstance(step, tuple) and step and step[0] == "write":
                (repo / step[1]).parent.mkdir(parents=True, exist_ok=True)
                (repo / step[1]).write_text(step[2])
            else:
                subprocess.run([*base, *step], cwd=repo, env=env, capture_output=True)
        # Resolve placeholders to real shas with the oracle so both inputs match.
        subs = {}
        for key, rev in placeholders.items():
            sha = subprocess.run([git_254_oracle, "rev-parse", rev], cwd=repo, env=env,
                                 capture_output=True, text=True).stdout.strip()
            subs[key] = sha
        stdin = stdin_tmpl.format(**subs)
        proc = subprocess.run([*base, *argv], cwd=repo, env=env, input=stdin,
                              capture_output=True, text=True)
        results[tool] = (proc.returncode, proc.stdout, proc.stderr)
    assert results["pygit"] == results["oracle"]


# --- workflow batch 2: column / describe / rev-list / checkout-index ---
BATCH2_CASES = [
    ('describe-dirty-clean', TAGGED, ["describe", "--dirty"]),
    ('describe-dirty-modified', TAGGED + [("write", "a.txt", "alpha\nmore\nchanged\n")], ["describe", "--dirty"]),
    ('describe-dirty-custom-mark', TAGGED + [("write", "a.txt", "alpha\nmore\nchanged\n")], ["describe", "--dirty=-mod"]),
    ('describe-dirty-empty-mark', TAGGED + [("write", "a.txt", "alpha\nmore\nchanged\n")], ["describe", "--dirty="]),
    ('describe-dirty-staged', TAGGED + [("write", "a.txt", "alpha\nmore\nstaged\n"), ["add", "-A"]], ["describe", "--dirty"]),
    ('describe-dirty-untracked-only', TAGGED + [("write", "new.txt", "untracked\n")], ["describe", "--dirty"]),
    ('describe-dirty-with-rev-error', TAGGED, ["describe", "--dirty", "HEAD"]),
    ('describe-dirty-always-no-tags', BASE + [("write", "a.txt", "alpha-dirty\n")], ["describe", "--always", "--dirty"]),
    ('rev-list-no-args-usage', BASE, ["rev-list"]),
    ('rev-list-exclude-hidden-all', BASE + [("write", "a.txt", "alpha\nmore\n"), ["add", "-A"], ["commit", "-m", "second"], ["checkout", "-b", "secret"], ("write", "s.txt", "sec\n"), ["add", "-A"], ["commit", "-m", "third"], ["checkout", "main"], ["config", "transfer.hideRefs", "refs/heads/secret"]], ["rev-list", "--exclude-hidden=fetch", "--all"]),
    ('rev-list-exclude-hidden-negate', BASE + [("write", "a.txt", "alpha\nmore\n"), ["add", "-A"], ["commit", "-m", "second"], ["checkout", "-b", "secret"], ("write", "s.txt", "sec\n"), ["add", "-A"], ["commit", "-m", "third"], ["checkout", "main"], ["config", "transfer.hideRefs", "refs/heads"], ["config", "--add", "transfer.hideRefs", "!refs/heads/main"]], ["rev-list", "--exclude-hidden=fetch", "--all"]),
    ('rev-list-exclude-hidden-bad-section', BASE, ["rev-list", "--exclude-hidden=bogus", "--all"]),
    ('rev-list-exclude-hidden-twice', BASE, ["rev-list", "--exclude-hidden=fetch", "--exclude-hidden=receive", "--all"]),
    ('rev-list-exclude-hidden-conflict-branches', REFSET, ["rev-list", "--exclude-hidden=fetch", "--branches"]),
    ('rev-list-exclude-hidden-branches-first', REFSET, ["rev-list", "--branches", "--exclude-hidden=fetch"]),
    ('rev-list-exclude-hidden-no-value', BASE, ["rev-list", "--exclude-hidden"]),
    ('checkout-index-noop-explicit', BASE, ["checkout-index", "a.txt"]),
    ('checkout-index-missing-creates', BASE + [("rm", "a.txt")], ["checkout-index", "a.txt"]),
    ('checkout-index-no-create-skips-missing', BASE + [("rm", "a.txt")], ["checkout-index", "--no-create", "a.txt"]),
    ('checkout-index-n-short-skips-missing', BASE + [("rm", "a.txt")], ["checkout-index", "-n", "-f", "a.txt"]),
    ('checkout-index-create-overrides-no-create', BASE + [("rm", "a.txt")], ["checkout-index", "--no-create", "--create", "a.txt"]),
    ('checkout-index-exists-modified-no-force', BASE + [("write", "a.txt", "modified\n")], ["checkout-index", "a.txt"]),
    ('checkout-index-exists-modified-quiet', BASE + [("write", "a.txt", "modified\n")], ["checkout-index", "-q", "a.txt"]),
    ('checkout-index-exists-modified-force', BASE + [("write", "a.txt", "modified\n")], ["checkout-index", "-f", "a.txt"]),
    ('checkout-index-not-in-cache', BASE, ["checkout-index", "nope.txt"]),
    ('checkout-index-not-in-cache-quiet', BASE, ["checkout-index", "-q", "nope.txt"]),
    ('checkout-index-mix-all-explicit', BASE, ["checkout-index", "-a", "a.txt"]),
    ('checkout-index-stage-out-of-range', BASE, ["checkout-index", "--stage=9", "a.txt"]),
    ('checkout-index-stage-zero', BASE, ["checkout-index", "--stage=0", "a.txt"]),
    ('checkout-index-stage-all-no-temp', BASE, ["checkout-index", "--stage=all", "--no-temp", "a.txt"]),
    ('checkout-index-unknown-long', BASE, ["checkout-index", "--bogus", "a.txt"]),
    ('checkout-index-unknown-short', BASE, ["checkout-index", "-Z", "a.txt"]),
    ('checkout-index-stage-requires-value', BASE, ["checkout-index", "--stage"]),
    ('checkout-index-ambiguous-abbrev', BASE, ["checkout-index", "--st", "a.txt"]),
    ('checkout-index-help-short', BASE, ["checkout-index", "-h"]),
    ('checkout-index-conflict-default-unmerged', [("write", "f", "base\n"), ["add", "f"], ["commit", "-m", "base"], ["checkout", "-b", "feat"], ("write", "f", "theirs\n"), ["add", "f"], ["commit", "-m", "t"], ["checkout", "main"], ("write", "f", "ours\n"), ["add", "f"], ["commit", "-m", "o"], ["merge", "feat"], ("rm", "f")], ["checkout-index", "f"]),
    ('checkout-index-conflict-stage2', [("write", "f", "base\n"), ["add", "f"], ["commit", "-m", "base"], ["checkout", "-b", "feat"], ("write", "f", "theirs\n"), ["add", "f"], ["commit", "-m", "t"], ["checkout", "main"], ("write", "f", "ours\n"), ["add", "f"], ["commit", "-m", "o"], ["merge", "feat"], ("rm", "f")], ["checkout-index", "--stage=2", "f"]),
    ('checkout-index-conflict-stage3-all', [("write", "f", "base\n"), ["add", "f"], ["commit", "-m", "base"], ["checkout", "-b", "feat"], ("write", "f", "theirs\n"), ["add", "f"], ["commit", "-m", "t"], ["checkout", "main"], ("write", "f", "ours\n"), ["add", "f"], ["commit", "-m", "o"], ["merge", "feat"], ("rm", "f")], ["checkout-index", "--stage=3", "-a"]),
    ('checkout-index-conflict-stage-missing', [("write", "f", "base\n"), ["add", "f"], ["commit", "-m", "base"], ["checkout", "-b", "feat"], ("write", "f", "theirs\n"), ["add", "f"], ["commit", "-m", "t"], ["checkout", "main"], ("write", "f", "ours\n"), ["add", "f"], ["commit", "-m", "o"], ["merge", "feat"], ("rm", "f"), ("write", "g.txt", "x\n"), ["add", "g.txt"]], ["checkout-index", "--stage=2", "g.txt"]),
]

BATCH2_STDIN_CASES = [
    ('column-rawmode-disabled', [], ["column", "--raw-mode=0"], 'alpha\nbeta\ngamma\n'),
    ('column-rawmode-column', [], ["column", "--raw-mode=16", "--width=20"], 'alpha\nbeta\ngamma\ndelta\n'),
    ('column-rawmode-row', [], ["column", "--raw-mode=17", "--width=20"], 'alpha\nbeta\ngamma\ndelta\n'),
    ('column-rawmode-dense', [], ["column", "--raw-mode=144", "--width=20"], 'a\nbbbbbbbb\nc\nd\ne\nf\n'),
    ('column-rawmode-1k-suffix', [], ["column", "--raw-mode=1k"], 'alpha\nbeta\n'),
    ('column-rawmode-negative', [], ["column", "--raw-mode=-1"], 'alpha\n'),
    ('column-rawmode-extra-arg', [], ["column", "--raw-mode=16", "zzz"], 'alpha\n'),
    ('column-rawmode-indent-padding', [], ["column", "--raw-mode=16", "--indent=>>", "--padding=3"], 'a\nbb\nccc\n'),
    ('rev-list-stdin-rev', TAGGED, ["rev-list", "--stdin"], 'HEAD\n'),
    ('rev-list-stdin-exclude', MERGED, ["rev-list", "--stdin"], 'main\n^feat\n'),
    ('rev-list-stdin-cmdline-mix', MERGED, ["rev-list", "--stdin", "main"], 'feat\n'),
    ('rev-list-stdin-range', TAGGED, ["rev-list", "--stdin"], 'v1..HEAD\n'),
    ('rev-list-stdin-pathspec', PATHHIST, ["rev-list", "--stdin"], 'HEAD\n--\na.txt\n'),
    ('rev-list-stdin-not', MERGED, ["rev-list", "--stdin"], 'main\n--not\nfeat\n'),
    ('rev-list-stdin-all', REFSET, ["rev-list", "--stdin"], '--all\n'),
    ('rev-list-stdin-count', TAGGED, ["rev-list", "--stdin", "--count"], 'HEAD\n'),
    ('rev-list-stdin-bad-option', BASE, ["rev-list", "--stdin"], '--bogus\n'),
    ('rev-list-stdin-bad-rev', BASE, ["rev-list", "--stdin"], 'no-such-rev\n'),
    ('rev-list-stdin-twice', BASE, ["rev-list", "--stdin", "--stdin"], 'HEAD\n'),
    ('rev-list-stdin-self', BASE, ["rev-list", "--stdin"], '--stdin\n'),
    ('rev-list-stdin-empty', BASE, ["rev-list", "--stdin"], ''),
    ('rev-list-stdin-end-of-options', TAGGED, ["rev-list", "--stdin"], '--end-of-options\nHEAD\n'),
    ('rev-list-stdin-blank-terminates', MERGED, ["rev-list", "--stdin"], 'main\n\nfeat\n'),
    ('checkout-index-stdin-lf', BASE + [("rm", "a.txt")], ["checkout-index", "--stdin"], 'a.txt\n'),
    ('checkout-index-stdin-z', BASE + [("rm", "a.txt"), ("rm", "b.txt")], ["checkout-index", "--stdin", "-z"], 'a.txt\x00b.txt\x00'),
    ('checkout-index-stdin-quoted', [("write", "a b.txt", "hi\n"), ["add", "a b.txt"], ["commit", "-m", "x"], ("rm", "a b.txt")], ["checkout-index", "--stdin"], '"a b.txt"\n'),
    ('checkout-index-stdin-z-no-unquote', [("write", "a b.txt", "hi\n"), ["add", "a b.txt"], ["commit", "-m", "x"], ("rm", "a b.txt")], ["checkout-index", "--stdin", "-z"], '"a b.txt"\x00'),
    ('checkout-index-mix-stdin-explicit', BASE, ["checkout-index", "--stdin", "a.txt"], 'a.txt\n'),
]


@pytest.mark.parametrize("case", BATCH2_CASES, ids=[c[0] for c in BATCH2_CASES])
def test_batch2_parity(case, tmp_path: Path, git_254_oracle: str):
    _id, setup, probe = case
    assert_command_parity(git_254_oracle, tmp_path, setup, probe)


@pytest.mark.parametrize("case", BATCH2_STDIN_CASES, ids=[c[0] for c in BATCH2_STDIN_CASES])
def test_batch2_stdin_parity(case, tmp_path: Path, git_254_oracle: str):
    _id, setup, probe, stdin = case
    assert_command_parity(git_254_oracle, tmp_path, setup, probe, stdin=stdin)


# --- workflow batch 3: patch-id / cat-file / unpack-objects / merge-tree ---
BATCH3_CASES = [
    ('cat-file-p-use-mailmap-commit', [('write','.mailmap','Proper Name <proper@example.com> Parity <parity@example.com>\n')], ['cat-file', '-p', '--use-mailmap', 'HEAD']),
    ('cat-file-s-use-mailmap-commit', [('write','.mailmap','Proper Name <proper@example.com> Parity <parity@example.com>\n')], ['cat-file', '-s', '--use-mailmap', 'HEAD']),
    ('cat-file-batch-all-objects-unordered', [], ['cat-file', '--batch-check', '--batch-all-objects', '--unordered']),
    ('unpack-objects-h-sole', BASE, ["unpack-objects", "-h"]),
    ('unpack-objects-help-all-sole', BASE, ["unpack-objects", "--help-all"]),
    ('unpack-objects-h-not-sole', BASE, ["unpack-objects", "-h", "foo"]),
    ('unpack-objects-unknown-flag', BASE, ["unpack-objects", "--bogus"]),
    ('unpack-objects-unknown-short', BASE, ["unpack-objects", "-x"]),
    ('unpack-objects-positional', BASE, ["unpack-objects", "foo"]),
    ('unpack-objects-double-dash', BASE, ["unpack-objects", "--"]),
    ('merge-tree-conflict-default', BASE + [['checkout','-b','feat'], ('write','a.txt','alpha-feat\n'), ['add','-A'], ['commit','-m','feat-commit'], ['checkout','main'], ('write','a.txt','alpha-main\n'), ['add','-A'], ['commit','-m','main-commit']], ['merge-tree', 'feat', 'main']),
    ('merge-tree-conflict-z', BASE + [['checkout','-b','feat'], ('write','a.txt','alpha-feat\n'), ['add','-A'], ['commit','-m','feat-commit'], ['checkout','main'], ('write','a.txt','alpha-main\n'), ['add','-A'], ['commit','-m','main-commit']], ['merge-tree', '-z', 'feat', 'main']),
    ('merge-tree-conflict-name-only', BASE + [['checkout','-b','feat'], ('write','a.txt','alpha-feat\n'), ['add','-A'], ['commit','-m','feat-commit'], ['checkout','main'], ('write','a.txt','alpha-main\n'), ['add','-A'], ['commit','-m','main-commit']], ['merge-tree', '--name-only', 'feat', 'main']),
    ('merge-tree-conflict-quiet', BASE + [['checkout','-b','feat'], ('write','a.txt','alpha-feat\n'), ['add','-A'], ['commit','-m','feat-commit'], ['checkout','main'], ('write','a.txt','alpha-main\n'), ['add','-A'], ['commit','-m','main-commit']], ['merge-tree', '--quiet', 'feat', 'main']),
    ('merge-tree-conflict-messages', BASE + [['checkout','-b','feat'], ('write','a.txt','alpha-feat\n'), ['add','-A'], ['commit','-m','feat-commit'], ['checkout','main'], ('write','a.txt','alpha-main\n'), ['add','-A'], ['commit','-m','main-commit']], ['merge-tree', '--messages', 'feat', 'main']),
    ('merge-tree-X-ours', BASE + [['checkout','-b','feat'], ('write','a.txt','alpha-feat\n'), ['add','-A'], ['commit','-m','feat-commit'], ['checkout','main'], ('write','a.txt','alpha-main\n'), ['add','-A'], ['commit','-m','main-commit']], ['merge-tree', '-X', 'ours', 'feat', 'main']),
    ('merge-tree-X-theirs', BASE + [['checkout','-b','feat'], ('write','a.txt','alpha-feat\n'), ['add','-A'], ['commit','-m','feat-commit'], ['checkout','main'], ('write','a.txt','alpha-main\n'), ['add','-A'], ['commit','-m','main-commit']], ['merge-tree', '-X', 'theirs', 'feat', 'main']),
    ('merge-tree-clean', BASE + [['checkout','-b','feat'], ('write','g.txt','feature\n'), ['add','-A'], ['commit','-m','feat-commit'], ['checkout','main'], ('write','h.txt','mainline\n'), ['add','-A'], ['commit','-m','main-commit']], ['merge-tree', 'feat', 'main']),
    ('merge-tree-no-renames', BASE + [['checkout','-b','feat'], ['mv','a.txt','renamed.txt'], ('write','renamed.txt','alpha-feat\n'), ['add','-A'], ['commit','-m','feat-commit'], ['checkout','main'], ('write','a.txt','alpha-main\n'), ['add','-A'], ['commit','-m','main-commit']], ['merge-tree', '-X', 'no-renames', 'feat', 'main']),
    ('merge-tree-usage-error', BASE + [['checkout','-b','feat'], ('write','a.txt','alpha-feat\n'), ['add','-A'], ['commit','-m','feat-commit'], ['checkout','main']], ['merge-tree', 'feat']),
]

BATCH3_STDIN_CASES = [
    ('patch-id-stable', BASE, ["patch-id", "--stable"], 'diff --git a/a.txt b/a.txt\nindex 814f4a4..ddc897f 100644\n--- a/a.txt\n+++ b/a.txt\n@@ -1,2 +1,3 @@\n one\n-two\n+TWO\n+three\n'),
    ('patch-id-unstable', BASE, ["patch-id", "--unstable"], 'diff --git a/a.txt b/a.txt\nindex 814f4a4..ddc897f 100644\n--- a/a.txt\n+++ b/a.txt\n@@ -1,2 +1,3 @@\n one\n-two\n+TWO\n+three\n'),
    ('patch-id-verbatim', BASE, ["patch-id", "--verbatim"], 'diff --git a/a.txt b/a.txt\nindex 814f4a4..ddc897f 100644\n--- a/a.txt\n+++ b/a.txt\n@@ -1,2 +1,3 @@\n one\n-two\n+TWO\n+three\n'),
    ('patch-id-stable-unstable-conflict', BASE, ["patch-id", "--stable", "--unstable"], 'diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-x\n+y\n'),
    ('patch-id-h-usage', BASE, ["patch-id", "-h"], ''),
    ('cat-file-batch-check-use-mailmap', [('write','.mailmap','Proper Name <proper@example.com> Parity <parity@example.com>\n')], ['cat-file', '--batch-check', '--use-mailmap'], 'HEAD\n'),
    ('cat-file-batch-Z', [], ['cat-file', '--batch-check', '-Z'], 'HEAD\x00missingobj\x00'),
    ('cat-file-batch-command-flush-requires-buffer', [], ['cat-file', '--batch-command'], 'info HEAD\nflush\n'),
    ('merge-tree-stdin', BASE + [['checkout','-b','feat'], ('write','a.txt','alpha-feat\n'), ['add','-A'], ['commit','-m','feat-commit'], ['checkout','main'], ('write','a.txt','alpha-main\n'), ['add','-A'], ['commit','-m','main-commit']], ['merge-tree', '--stdin'], 'feat main\n'),
]


@pytest.mark.parametrize("case", BATCH3_CASES, ids=[c[0] for c in BATCH3_CASES])
def test_batch3_parity(case, tmp_path: Path, git_254_oracle: str):
    _id, setup, probe = case
    assert_command_parity(git_254_oracle, tmp_path, setup, probe)


@pytest.mark.parametrize("case", BATCH3_STDIN_CASES, ids=[c[0] for c in BATCH3_STDIN_CASES])
def test_batch3_stdin_parity(case, tmp_path: Path, git_254_oracle: str):
    _id, setup, probe, stdin = case
    assert_command_parity(git_254_oracle, tmp_path, setup, probe, stdin=stdin)


# --- workflow batch 4: clean/interpret-trailers/replace/log+show/archive/init/switch/pack-redundant/update-index ---
BATCH4_CASES = [
    ('clean-q-suppresses-output', [('write','a.txt','a\n'),('write','b.log','b\n')], ['clean','-n','-q']),
    ('clean-e-protects-pattern', [('write','a.txt','a\n'),('write','b.log','b\n')], ['clean','-n','-e','*.log']),
    ('clean-exclude-eq', [('write','a.txt','a\n'),('write','b.log','b\n')], ['clean','-n','--exclude=*.log']),
    ('clean-x-removes-ignored', [('write','.gitignore','*.o\n'),['add','.gitignore'],['commit','-m','gi'],('write','a.txt','a\n'),('write','ignored.o','i\n')], ['clean','-n','-x']),
    ('clean-x-with-e-still-protects', [('write','.gitignore','*.o\n'),['add','.gitignore'],['commit','-m','gi'],('write','a.txt','a\n'),('write','b.log','b\n')], ['clean','-n','-x','-e','*.log']),
    ('clean-X-only-ignored', [('write','.gitignore','*.o\n'),['add','.gitignore'],['commit','-m','gi'],('write','a.txt','a\n'),('write','ignored.o','i\n')], ['clean','-n','-X']),
    ('clean-X-e-adds-to-ignored', [('write','.gitignore','*.o\n'),['add','.gitignore'],['commit','-m','gi'],('write','ignored.o','i\n'),('write','x.tmp','x\n')], ['clean','-n','-X','-e','*.tmp']),
    ('clean-X-negation-unignores', [('write','.gitignore','*.o\n'),['add','.gitignore'],['commit','-m','gi'],('write','keep.o','i\n')], ['clean','-n','-X','-e','!keep.o']),
    ('clean-x-X-conflict', [('write','a.txt','a\n')], ['clean','-n','-x','-X']),
    ('clean-require-force', [('write','a.txt','a\n')], ['clean']),
    ('clean-unknown-option-usage', [('write','a.txt','a\n')], ['clean','-n','--bogus']),
    ('clean-e-missing-value', [('write','a.txt','a\n')], ['clean','-n','-e']),
    ('clean-dX-collapses-ignored-dir', [('write','.gitignore','*.o\n'),['add','.gitignore'],['commit','-m','gi'],('write','build/o1.o','o\n'),('write','build/x.txt','t\n')], ['clean','-n','-d','-X']),
    ('clean-d-mixed-dir-recurses', [('write','.gitignore','*.o\n'),['add','.gitignore'],['commit','-m','gi'],('write','sub/x.txt','x\n'),('write','sub/z.o','o\n')], ['clean','-n','-d']),
    ('replace-list-empty', [('write','a.txt','hello\n'), ['add','a.txt'], ['commit','-m','first']], ['replace', '-l']),
    ('replace-create-and-list-formats', [('write','a.txt','1\n'), ['add','a.txt'], ['commit','-m','c1'], ('write','a.txt','2\n'), ['add','a.txt'], ['commit','-m','c2'], ['replace','HEAD~1','HEAD']], ['replace', '--format=long', '-l']),
    ('replace-default-noargs-lists', [('write','a.txt','1\n'), ['add','a.txt'], ['commit','-m','c1'], ('write','a.txt','2\n'), ['add','a.txt'], ['commit','-m','c2'], ['replace','HEAD~1','HEAD']], ['replace']),
    ('replace-already-exists', [('write','a.txt','1\n'), ['add','a.txt'], ['commit','-m','c1'], ('write','a.txt','2\n'), ['add','a.txt'], ['commit','-m','c2'], ['replace','HEAD~1','HEAD']], ['replace', 'HEAD~1', 'HEAD']),
    ('replace-force-overwrite', [('write','a.txt','1\n'), ['add','a.txt'], ['commit','-m','c1'], ('write','a.txt','2\n'), ['add','a.txt'], ['commit','-m','c2'], ('write','a.txt','3\n'), ['add','a.txt'], ['commit','-m','c3'], ['replace','HEAD~2','HEAD']], ['replace', '-f', 'HEAD~2', 'HEAD~1']),
    ('replace-type-mismatch', [('write','a.txt','1\n'), ['add','a.txt'], ['commit','-m','c1']], ['replace', 'HEAD', 'HEAD^{tree}']),
    ('replace-delete', [('write','a.txt','1\n'), ['add','a.txt'], ['commit','-m','c1'], ('write','a.txt','2\n'), ['add','a.txt'], ['commit','-m','c2'], ['replace','HEAD~1','HEAD']], ['replace', '-d', 'HEAD~1']),
    ('replace-delete-not-found', [('write','a.txt','1\n'), ['add','a.txt'], ['commit','-m','c1']], ['replace', '-d', 'HEAD']),
    ('replace-delete-no-args', [('write','a.txt','1\n'), ['add','a.txt'], ['commit','-m','c1']], ['replace', '-d']),
    ('replace-graft-change-parent', [('write','a.txt','1\n'), ['add','a.txt'], ['commit','-m','c1'], ('write','a.txt','2\n'), ['add','a.txt'], ['commit','-m','c2'], ('write','a.txt','3\n'), ['add','a.txt'], ['commit','-m','c3']], ['replace', '--graft', 'HEAD~2', 'HEAD~1']),
    ('replace-graft-unnecessary', [('write','a.txt','1\n'), ['add','a.txt'], ['commit','-m','c1'], ('write','a.txt','2\n'), ['add','a.txt'], ['commit','-m','c2']], ['replace', '--graft', 'HEAD', 'HEAD~1']),
    ('replace-graft-bad-arg-count', [('write','a.txt','1\n'), ['add','a.txt'], ['commit','-m','c1']], ['replace', '--graft']),
    ('replace-format-not-listing', [('write','a.txt','1\n'), ['add','a.txt'], ['commit','-m','c1']], ['replace', '--format=short', '-d', 'HEAD']),
    ('replace-force-misuse', [('write','a.txt','1\n'), ['add','a.txt'], ['commit','-m','c1']], ['replace', '-f', '-l']),
    ('replace-cmdmode-conflict', [('write','a.txt','1\n'), ['add','a.txt'], ['commit','-m','c1']], ['replace', '-d', '-l']),
    ('replace-list-two-patterns', [('write','a.txt','1\n'), ['add','a.txt'], ['commit','-m','c1']], ['replace', '-l', 'a', 'b']),
    ('replace-convert-graft-no-file', [('write','a.txt','1\n'), ['add','a.txt'], ['commit','-m','c1']], ['replace', '--convert-graft-file']),
    ('replace-convert-graft-takes-no-arg', [('write','a.txt','1\n'), ['add','a.txt'], ['commit','-m','c1']], ['replace', '--convert-graft-file', 'foo']),
    ('log-quiet-with-patch-shows-diff', BASE + [('write','f.txt','a\nb\nc\n'), ['add','f.txt'], ['commit','-m','first'], ('write','f.txt','a\nB\nc\n'), ['add','f.txt'], ['commit','-m','second']], ['log','-q','-p','-n1']),
    ('log-quiet-patch-order-shows-diff', BASE + [('write','f.txt','a\nb\nc\n'), ['add','f.txt'], ['commit','-m','first'], ('write','f.txt','a\nB\nc\n'), ['add','f.txt'], ['commit','-m','second']], ['log','-p','-q','-n1']),
    ('log-quiet-alone-noop', BASE + [('write','f.txt','x\n'), ['add','f.txt'], ['commit','-m','only']], ['log','-q','-n1']),
    ('log-quiet-name-only-conflict', BASE + [('write','f.txt','x\n'), ['add','f.txt'], ['commit','-m','only']], ['log','-q','--name-only','-n1']),
    ('log-quiet-stat-overrides-no-conflict', BASE + [('write','f.txt','a\nb\nc\n'), ['add','f.txt'], ['commit','-m','first'], ('write','f.txt','a\nB\nc\n'), ['add','f.txt'], ['commit','-m','second']], ['log','-q','--raw','--name-status','-n1']),
    ('log-clear-decorations-noop', BASE + [('write','f.txt','x\n'), ['add','f.txt'], ['commit','-m','only'], ['tag','v1']], ['log','--clear-decorations','--decorate','--oneline','-n1']),
    ('show-quiet-suppresses-diff', BASE + [('write','f.txt','a\nb\nc\n'), ['add','f.txt'], ['commit','-m','first'], ('write','f.txt','a\nB\nc\n'), ['add','f.txt'], ['commit','-m','second']], ['show','-q','HEAD']),
    ('show-quiet-long-suppresses-diff', BASE + [('write','f.txt','a\nb\nc\n'), ['add','f.txt'], ['commit','-m','first'], ('write','f.txt','a\nB\nc\n'), ['add','f.txt'], ['commit','-m','second']], ['show','--quiet','HEAD']),
    ('show-quiet-name-only-conflict', BASE + [('write','f.txt','x\n'), ['add','f.txt'], ['commit','-m','only']], ['show','-q','--name-only','HEAD']),
    ('show-quiet-stat-overrides', BASE + [('write','f.txt','a\nb\nc\n'), ['add','f.txt'], ['commit','-m','first'], ('write','f.txt','a\nB\nc\n'), ['add','f.txt'], ['commit','-m','second']], ['show','-q','--stat','HEAD']),
    ('show-clear-decorations-noop', BASE + [('write','f.txt','a\nb\nc\n'), ['add','f.txt'], ['commit','-m','first'], ('write','f.txt','a\nB\nc\n'), ['add','f.txt'], ['commit','-m','second']], ['show','--clear-decorations','-s','HEAD']),
    ('archive-verbose-nested-stderr', [('write','a','x\n'),('write','d1/f1','x\n'),('write','d1/d2/f2','x\n'),('write','z/zz','x\n'),['add','-A'],['commit','-m','c']], ['archive','--format=tar','-v','HEAD']),
    ('archive-verbose-prefix-dir-collapse', [('write','a','x\n'),('write','d1/f1','x\n'),['add','-A'],['commit','-m','c']], ['archive','--format=tar','-v','--prefix=p//','HEAD']),
    ('archive-mtime-epoch', [('write','a','hello\n'),['add','-A'],['commit','-m','c']], ['archive','--format=tar','--mtime=@1234567890','HEAD']),
    ('archive-mtime-iso-tz', [('write','a','hello\n'),['add','-A'],['commit','-m','c']], ['archive','--format=tar','--mtime=2005-04-07T22:13:13 +0200','HEAD']),
    ('archive-symlink-mode-0777', [('write','a','hello\n'),['add','a'],['commit','-m','c'],['update-index','--add','--cacheinfo','120000,'+__import__('subprocess').run(['printf','a'],capture_output=True).stdout.decode(),'link']], ['archive','--format=tar','HEAD']),
    ('init-shared-group', [], ['init', '-q', '--shared=group', 'r']),
    ('init-shared-all', [], ['init', '-q', '--shared=all', 'r']),
    ('init-shared-bare', [], ['init', '--shared', 'r']),
    ('init-shared-octal', [], ['init', '-q', '--shared=0640', 'r']),
    ('init-shared-umask-noop', [], ['init', '-q', '--shared=umask', 'r']),
    ('init-shared-bad-filemode', [], ['init', '--shared=0400', 'r']),
    ('init-shared-bad-bool', [], ['init', '--shared=bogus', 'r']),
    ('init-separate-git-dir', [], ['init', '-q', '--separate-git-dir=realgit', 'wt']),
    ('init-separate-git-dir-bare-conflict', [], ['init', '--bare', '--separate-git-dir=g', 'wt']),
    ('init-template-missing', [], ['init', '-q', '--template=no_such_dir', 'r']),
    ('init-template-custom', [('write', 'tmpl/description', 'CUSTOM DESC\n'), ('write', 'tmpl/info/exclude', 'custom\n'), ('write', 'tmpl/topfile', 'top\n')], ['init', '-q', '--template=tmpl', 'r']),
    ('switch-detach-branch', BASE + [["branch", "other"]], ["switch", "-d", "other"]),
    ('switch-detach-no-arg', BASE, ["switch", "-d"]),
    ('switch-C-reset-existing', BASE + [["branch", "other"], ("write", "a.txt", "alpha\nmore\n"), ["add", "-A"], ["commit", "-m", "second"]], ["switch", "-C", "other", "master"]),
    ('switch-C-new', BASE, ["switch", "-C", "fresh"]),
    ('switch-quiet-branch', BASE + [["branch", "other"]], ["switch", "-q", "other"]),
    ('switch-two-refs-error', BASE + [["branch", "other"]], ["switch", "other", "master"]),
    ('switch-no-arg-error', BASE, ["switch"]),
    ('switch-invalid-ref-error', BASE, ["switch", "nope"]),
    ('switch-nonbranch-needs-detach', BASE + [["branch", "other"], ["tag", "v1", "other"]], ["switch", "v1"]),
    ('switch-detach-annotated-tag', BASE + [["branch", "other"], ["tag", "-a", "-m", "anno", "v1", "other"]], ["switch", "-d", "v1"]),
    ('switch-c-already-exists', BASE + [["branch", "other"]], ["switch", "-c", "other"]),
    ('switch-unknown-flag-usage', BASE, ["switch", "--bogus"]),
    ('switch-c-requires-value', BASE, ["switch", "-c"]),
    ('switch-detach-create-conflict', BASE, ["switch", "-c", "x", "-d"]),
    ('switch-leave-detached-prev-head', BASE + [["branch", "other"], ("write", "a.txt", "alpha\nmore\n"), ["add", "-A"], ["commit", "-m", "second"], ["switch", "-d", "other"]], ["switch", "master"]),
    ('pack-redundant-h-stdout', [], ['pack-redundant', '-h']),
    ('update-index-again-basic', [('write','a.txt','a\n'),('write','b.txt','b\n'),('write','c.txt','c\n'),['add','a.txt','b.txt','c.txt'],['commit','-qm','init'],['update-index','--cacheinfo','100644','0000000000000000000000000000000000000000','x'] and ['hash-object','-w','--stdin'],('write','a.txt','aworktree\n'),('write','b.txt','bmod\n')], ['update-index','--again']),
    ('update-index-again-missing-aborts', [('write','a.txt','a\n'),('write','c.txt','c\n'),['add','a.txt','c.txt'],['commit','-qm','init'],('write','a.txt','aw\n'),('rm','c.txt')], ['update-index','--again']),
    ('update-index-again-pathspec', [('write','a.txt','a\n'),('write','b.txt','b\n'),['add','a.txt','b.txt'],['commit','-qm','init'],('write','a.txt','aw\n'),('write','b.txt','bw\n')], ['update-index','--again','b.txt']),
    ('update-index-again-noop-clean', [('write','a.txt','a\n'),['add','a.txt'],['commit','-qm','init']], ['update-index','--again']),
    ('update-index-again-no-head', [('write','a.txt','a\n'),['add','a.txt'],('write','a.txt','aw\n')], ['update-index','--again']),
]

BATCH4_STDIN_CASES = [
    ('interpret-trailers-add-after-signoff', [], ['interpret-trailers', '--trailer', 'Reviewed-by: B <b@x>'], 'subject\n\nbody line\n\nSigned-off-by: A <a@x>\n'),
    ('interpret-trailers-parse-fold', [], ['interpret-trailers', '--parse'], 'subject\n\nReviewed-by: A\nFold: line1\n  cont\n'),
    ('interpret-trailers-only-trailers-25pct', [], ['interpret-trailers', '--only-trailers'], 'subject\n\nSigned-off-by: A\nplain text\n'),
    ('interpret-trailers-divider-default', [], ['interpret-trailers', '--trailer', 'Ack: me'], 'subject\n\nBody\n\nReviewed-by: A\n---\npatch\nFake: x\n'),
    ('interpret-trailers-no-divider', [], ['interpret-trailers', '--no-divider', '--trailer', 'Ack: me'], 'subject\n\nBody\n\nReviewed-by: A\n---\npatch\nFake: x\n'),
    ('interpret-trailers-if-exists-replace', [], ['interpret-trailers', '--if-exists', 'replace', '--trailer', 'Reviewed-by: B'], 'subject\n\nReviewed-by: A\n'),
    ('interpret-trailers-if-exists-addifdifferent-dup', [], ['interpret-trailers', '--if-exists', 'addIfDifferent', '--trailer', 'Reviewed-by: A'], 'subject\n\nReviewed-by: A\nReviewed-by: B\n'),
    ('interpret-trailers-where-start', [], ['interpret-trailers', '--where', 'start', '--trailer', 'Reviewed-by: C'], 'subject\n\nReviewed-by: A\nReviewed-by: B\n'),
    ('interpret-trailers-trim-empty', [], ['interpret-trailers', '--trim-empty', '--trailer', 'Reviewed-by'], 'subject\n\nReviewed-by: A\n'),
    ('interpret-trailers-only-input-trailer-error', [], ['interpret-trailers', '--only-input', '--trailer', 'C: d'], 'x\n\nA: b\n'),
    ('interpret-trailers-empty-token', [], ['interpret-trailers', '--trailer', '=val'], 'x\n'),
    ('interpret-trailers-in-place-no-file', [], ['interpret-trailers', '--in-place'], 'x\n'),
    ('interpret-trailers-unknown-option', [], ['interpret-trailers', '--bogus'], 'x\n'),
    ('interpret-trailers-no-trailing-newline', [], ['interpret-trailers', '--trailer', 'Ack: x'], 'subject\n\nReviewed-by: A'),
    ('interpret-trailers-config-where-and-alias', [['init', '-q'], ['config', 'trailer.where', 'start'], ['config', 'trailer.sign.key', 'Signed-off-by']], ['interpret-trailers', '--trailer', 'sign: me'], 'subj\n\nReviewed-by: A\n'),
    ('pack-redundant-gate-no-optin', [('write','a','a\n'), ['add','a'], ['commit','-m','a'], ['repack','-a','-d']], ['pack-redundant', '--all'], ''),
    ('pack-redundant-unknown-flag', [('write','a','a\n'), ['add','a'], ['commit','-m','a'], ['repack','-a','-d']], ['pack-redundant', '--bogus', '--i-still-use-this'], ''),
    ('pack-redundant-bad-filename-short', [('write','a','a\n'), ['add','a'], ['commit','-m','a'], ['repack','-a','-d']], ['pack-redundant', '--i-still-use-this', 'nope.pack'], ''),
    ('pack-redundant-bad-oid-stdin', [('write','a','a\n'), ['add','a'], ['commit','-m','a'], ['repack','-a','-d'], ('write','b','b\n'), ['add','b'], ['commit','-m','b'], ['repack','-a']], ['pack-redundant', '--all', '--i-still-use-this'], 'notahexid\n'),
]


@pytest.mark.parametrize("case", BATCH4_CASES, ids=[c[0] for c in BATCH4_CASES])
def test_batch4_parity(case, tmp_path: Path, git_254_oracle: str):
    _id, setup, probe = case
    assert_command_parity(git_254_oracle, tmp_path, setup, probe)


@pytest.mark.parametrize("case", BATCH4_STDIN_CASES, ids=[c[0] for c in BATCH4_STDIN_CASES])
def test_batch4_stdin_parity(case, tmp_path: Path, git_254_oracle: str):
    _id, setup, probe, stdin = case
    assert_command_parity(git_254_oracle, tmp_path, setup, probe, stdin=stdin)


BATCH5_CASES = [
    ('bugreport-suffix-missing-value', [], ['bugreport', '-s']),
    ('bugreport-long-suffix-missing-value', [], ['bugreport', '--suffix']),
    ('bugreport-output-dir-missing-value', [], ['bugreport', '-o']),
    ('bugreport-long-output-dir-missing-value', [], ['bugreport', '--output-directory']),
    ('bugreport-unknown-argument', [], ['bugreport', 'stray']),
    ('bugreport-unknown-option', [], ['bugreport', '--bogus']),
    ('bugreport-unknown-switch', [], ['bugreport', '-z']),
    ('bugreport-diagnose-invalid-value', [], ['bugreport', '--diagnose=bogus']),
    ('bugreport-diagnose-empty-value', [], ['bugreport', '--diagnose=']),
    ('bugreport-help', [], ['bugreport', '-h']),
    ('diagnose-invalid-mode-eq', [], ['diagnose', '--mode=bogus', '-s', 'foo']),
    ('diagnose-invalid-mode-empty', [], ['diagnose', '--mode=', '-s', 'foo']),
    ('diagnose-invalid-mode-separate', [], ['diagnose', '--mode', 'bogus', '-s', 'foo']),
    ('diagnose-invalid-mode-none', [], ['diagnose', '--mode=none', '-s', 'foo']),
    ('diagnose-mode-requires-value-last', [], ['diagnose', '--mode']),
    ('diagnose-mode-eats-next-option', [], ['diagnose', '--mode', '-s', 'foo']),
    ('diagnose-suffix-short-requires-value', [], ['diagnose', '-s']),
    ('diagnose-suffix-long-requires-value', [], ['diagnose', '--suffix']),
    ('diagnose-output-short-requires-value', [], ['diagnose', '-o']),
    ('diagnose-output-long-requires-value', [], ['diagnose', '--output-directory']),
    ('diagnose-no-mode-unknown', [], ['diagnose', '--no-mode', '-s', 'foo']),
    ('diagnose-unknown-long-option', [], ['diagnose', '--foo']),
    ('diagnose-unknown-short-switch', [], ['diagnose', '-z']),
    ('diagnose-help-short', [], ['diagnose', '-h']),
    ('init-ref-format-files', [], ['init', '--ref-format=files', 'myrepo']),
    ('init-ref-format-unknown-fatal', [], ['init', '--ref-format=bogus']),
    ('init-ref-format-unknown-after-path', [], ['init', 'mydir', '--ref-format=bogus']),
    ('init-ref-format-empty-value-fatal', [], ['init', '--ref-format=']),
    ('init-ref-format-missing-value-usage', [], ['init', '--ref-format']),
    ('init-db-ref-format-files', [], ['init-db', '--ref-format=files', 'dbrepo']),
    ('init-db-ref-format-unknown-fatal', [], ['init-db', '--ref-format=bogus']),
    ('init-db-ref-format-missing-value-usage', [], ['init-db', '--ref-format']),
    ('fast-export-no-data-linear', [('write', 'f.txt', 'one\n'), ['add', 'f.txt'], ['commit', '-m', 'one'], ('write', 'f.txt', 'two\n'), ['add', 'f.txt'], ['commit', '-m', 'two'], ['branch', '-M', 'main']], ['fast-export', '--no-data', 'HEAD']),
    ('fast-export-data-linear', [('write', 'f.txt', 'one\n'), ['add', 'f.txt'], ['commit', '-m', 'one'], ('write', 'f.txt', 'two\n'), ['add', 'f.txt'], ['commit', '-m', 'two'], ['branch', '-M', 'main']], ['fast-export', '--data', 'HEAD']),
    ('fast-export-no-data-add-modify-delete', [('write', 'a.txt', 'hello\n'), ('write', 'b.txt', 'world\n'), ['add', 'a.txt', 'b.txt'], ['commit', '-m', 'first commit'], ('write', 'a.txt', 'hello again\n'), ('write', 'c.txt', 'extra\n'), ['add', 'a.txt', 'c.txt'], ('rm', 'b.txt'), ['commit', '-m', 'second commit'], ['branch', '-M', 'main']], ['fast-export', '--no-data', 'HEAD']),
    ('fast-export-no-data-subdir', [('write', 'dir/a.txt', 'a\n'), ('write', 'top.txt', 'top\n'), ['add', '.'], ['commit', '-m', 'c1'], ('write', 'dir/b.txt', 'b\n'), ['add', '.'], ['commit', '-m', 'c2'], ['branch', '-M', 'main']], ['fast-export', '--no-data', 'HEAD']),
    ('fast-export-data-no-data-last-wins', [('write', 'f.txt', 'one\n'), ['add', 'f.txt'], ['commit', '-m', 'one'], ('write', 'f.txt', 'two\n'), ['add', 'f.txt'], ['commit', '-m', 'two'], ['branch', '-M', 'main']], ['fast-export', '--data', '--no-data', 'HEAD']),
    ('fast-export-no-data-data-last-wins', [('write', 'f.txt', 'one\n'), ['add', 'f.txt'], ['commit', '-m', 'one'], ('write', 'f.txt', 'two\n'), ['add', 'f.txt'], ['commit', '-m', 'two'], ['branch', '-M', 'main']], ['fast-export', '--no-data', '--data', 'HEAD']),
    ('update-index-unresolve-restores-stages', [('write', 'f.txt', 'a\nb\nc\n'), ['add', 'f.txt'], ['commit', '-m', 'base'], ['checkout', '-b', 'other'], ('write', 'f.txt', 'a\nX\nc\n'), ['commit', '-am', 'other'], ['checkout', 'main'], ('write', 'f.txt', 'a\nY\nc\n'), ['commit', '-am', 'main2'], ['merge', 'other'], ('write', 'f.txt', 'a\nZ\nc\n'), ['add', 'f.txt'], ['update-index', '--unresolve', 'f.txt']], ['ls-files', '-u']),
    ('update-index-unresolve-twice-second-is-noop', [('write', 'f.txt', 'a\nb\nc\n'), ['add', 'f.txt'], ['commit', '-m', 'base'], ['checkout', '-b', 'other'], ('write', 'f.txt', 'a\nX\nc\n'), ['commit', '-am', 'other'], ['checkout', 'main'], ('write', 'f.txt', 'a\nY\nc\n'), ['commit', '-am', 'main2'], ['merge', 'other'], ('write', 'f.txt', 'a\nZ\nc\n'), ['add', 'f.txt'], ['update-index', '--unresolve', 'f.txt'], ['update-index', '--unresolve', 'f.txt']], ['ls-files', '-u']),
    ('update-index-clear-resolve-undo-then-unresolve-noop', [('write', 'f.txt', 'a\nb\nc\n'), ['add', 'f.txt'], ['commit', '-m', 'base'], ['checkout', '-b', 'other'], ('write', 'f.txt', 'a\nX\nc\n'), ['commit', '-am', 'other'], ['checkout', 'main'], ('write', 'f.txt', 'a\nY\nc\n'), ['commit', '-am', 'main2'], ['merge', 'other'], ('write', 'f.txt', 'a\nZ\nc\n'), ['add', 'f.txt'], ['update-index', '--clear-resolve-undo'], ['update-index', '--unresolve', 'f.txt']], ['ls-files', '-u']),
    ('update-index-unresolve-no-record-is-noop', [('write', 'a.txt', 'x\n'), ['add', 'a.txt'], ['commit', '-m', 'base']], ['update-index', '--unresolve', 'a.txt']),
    ('update-index-unresolve-no-paths-noop', [], ['update-index', '--unresolve']),
    ('update-index-clear-resolve-undo-empty-noop', [], ['update-index', '--clear-resolve-undo']),
    ('update-index-fsmonitor-valid-existing-path', [('write', 'a.txt', 'x\n'), ['add', 'a.txt']], ['update-index', '--fsmonitor-valid', 'a.txt']),
    ('update-index-no-fsmonitor-valid-existing-path', [('write', 'a.txt', 'x\n'), ['add', 'a.txt']], ['update-index', '--no-fsmonitor-valid', 'a.txt']),
    ('update-index-fsmonitor-valid-missing-path-dies', [], ['update-index', '--fsmonitor-valid', 'missing.txt']),
    ('update-index-no-fsmonitor-valid-missing-path-dies', [], ['update-index', '--no-fsmonitor-valid', 'missing.txt']),
    ('update-index-fsmonitor-valid-no-path-noop', [], ['update-index', '--fsmonitor-valid']),
    ('backfill-bare-noop', [('write', 'f.txt', 'hi\n'), ['add', 'f.txt'], ['commit', '-m', 'init']], ['backfill']),
    ('backfill-min-batch-size-eq', [('write', 'f.txt', 'hi\n'), ['add', 'f.txt'], ['commit', '-m', 'init']], ['backfill', '--min-batch-size=100']),
    ('backfill-min-batch-size-separate', [('write', 'f.txt', 'hi\n'), ['add', 'f.txt'], ['commit', '-m', 'init']], ['backfill', '--min-batch-size', '100']),
    ('backfill-min-batch-size-zero', [('write', 'f.txt', 'hi\n'), ['add', 'f.txt'], ['commit', '-m', 'init']], ['backfill', '--min-batch-size=0']),
    ('backfill-min-batch-size-k-suffix', [('write', 'f.txt', 'hi\n'), ['add', 'f.txt'], ['commit', '-m', 'init']], ['backfill', '--min-batch-size=1k']),
    ('backfill-min-batch-size-m-suffix', [('write', 'f.txt', 'hi\n'), ['add', 'f.txt'], ['commit', '-m', 'init']], ['backfill', '--min-batch-size=2m']),
    ('backfill-min-batch-size-hex', [('write', 'f.txt', 'hi\n'), ['add', 'f.txt'], ['commit', '-m', 'init']], ['backfill', '--min-batch-size=0x10']),
    ('backfill-min-batch-size-octal', [('write', 'f.txt', 'hi\n'), ['add', 'f.txt'], ['commit', '-m', 'init']], ['backfill', '--min-batch-size=010']),
    ('backfill-min-batch-size-plus', [('write', 'f.txt', 'hi\n'), ['add', 'f.txt'], ['commit', '-m', 'init']], ['backfill', '--min-batch-size=+5']),
    ('backfill-min-batch-size-no-value', [('write', 'f.txt', 'hi\n'), ['add', 'f.txt'], ['commit', '-m', 'init']], ['backfill', '--min-batch-size']),
    ('backfill-min-batch-size-empty', [('write', 'f.txt', 'hi\n'), ['add', 'f.txt'], ['commit', '-m', 'init']], ['backfill', '--min-batch-size=']),
    ('backfill-min-batch-size-abc', [('write', 'f.txt', 'hi\n'), ['add', 'f.txt'], ['commit', '-m', 'init']], ['backfill', '--min-batch-size=abc']),
    ('backfill-min-batch-size-negative', [('write', 'f.txt', 'hi\n'), ['add', 'f.txt'], ['commit', '-m', 'init']], ['backfill', '--min-batch-size=-5']),
    ('backfill-min-batch-size-bad-suffix', [('write', 'f.txt', 'hi\n'), ['add', 'f.txt'], ['commit', '-m', 'init']], ['backfill', '--min-batch-size=5z']),
    ('backfill-min-batch-size-bare-0x', [('write', 'f.txt', 'hi\n'), ['add', 'f.txt'], ['commit', '-m', 'init']], ['backfill', '--min-batch-size=0x']),
    ('backfill-min-batch-size-dotted', [('write', 'f.txt', 'hi\n'), ['add', 'f.txt'], ['commit', '-m', 'init']], ['backfill', '--min-batch-size=1.5']),
    ('backfill-min-batch-size-overflow', [('write', 'f.txt', 'hi\n'), ['add', 'f.txt'], ['commit', '-m', 'init']], ['backfill', '--min-batch-size=999999999999999999999']),
    ('backfill-no-sparse', [('write', 'f.txt', 'hi\n'), ['add', 'f.txt'], ['commit', '-m', 'init']], ['backfill', '--no-sparse']),
    ('backfill-min-batch-size-then-no-sparse', [('write', 'f.txt', 'hi\n'), ['add', 'f.txt'], ['commit', '-m', 'init']], ['backfill', '--min-batch-size=10', '--no-sparse']),
    ('backfill-unknown-flag', [('write', 'f.txt', 'hi\n'), ['add', 'f.txt'], ['commit', '-m', 'init']], ['backfill', '--bogus']),
    ('backfill-help-h', [('write', 'f.txt', 'hi\n'), ['add', 'f.txt'], ['commit', '-m', 'init']], ['backfill', '-h']),
    ('archive-exec-eq-no-remote', [('write', 'f.txt', 'hello\n'), ['add', 'f.txt'], ['commit', '-m', 'init']], ['archive', '--exec=foo', '--format=tar', 'HEAD']),
    ('archive-exec-space-no-remote', [('write', 'f.txt', 'hello\n'), ['add', 'f.txt'], ['commit', '-m', 'init']], ['archive', '--exec', 'foo', '--format=tar', 'HEAD']),
    ('archive-exec-empty-value-no-remote', [('write', 'f.txt', 'hello\n'), ['add', 'f.txt'], ['commit', '-m', 'init']], ['archive', '--exec=', '--format=tar', 'HEAD']),
    ('archive-exec-with-verbose-no-remote', [('write', 'f.txt', 'hello\n'), ['add', 'f.txt'], ['commit', '-m', 'init']], ['archive', '--exec=foo', '-v', '--format=tar', 'HEAD']),
    ('archive-exec-default-name-no-remote', [('write', 'f.txt', 'hello\n'), ['add', 'f.txt'], ['commit', '-m', 'init']], ['archive', '--exec=git-upload-archive', '--format=tar', 'HEAD']),
]

BATCH5_STDIN_CASES = [
    ('follow-in-tree-link-batch', [('write', 'target.txt', 'hello\n'), ('write', '_lc_link', 'target.txt'), ['add', 'target.txt', '_lc_link'], ['update-index', '--add', '--cacheinfo', '120000', '4cbb553f3f4ac2ee7b01ff6c951d6bf583c39c15', 'link'], ['commit', '-m', 'c1']], ['cat-file', '--batch', '--follow-symlinks'], 'HEAD:link\n'),
    ('follow-in-tree-link-batch-check', [('write', 'target.txt', 'hello\n'), ('write', '_lc_link', 'target.txt'), ['add', 'target.txt', '_lc_link'], ['update-index', '--add', '--cacheinfo', '120000', '4cbb553f3f4ac2ee7b01ff6c951d6bf583c39c15', 'link'], ['commit', '-m', 'c1']], ['cat-file', '--batch-check', '--follow-symlinks'], 'HEAD:link\n'),
    ('follow-subdir-up-link', [('write', 'target.txt', 'hello\n'), ('write', 'sub/inner.txt', 'inside sub\n'), ('write', '_lc_up', '../target.txt'), ['add', 'target.txt', 'sub/inner.txt', '_lc_up'], ['update-index', '--add', '--cacheinfo', '120000', 'f9b1b32e6647369a82e9f90b38393d3cbe785a10', 'sub/up'], ['commit', '-m', 'c1']], ['cat-file', '--batch', '--follow-symlinks'], 'HEAD:sub/up\n'),
    ('follow-dir-link-with-path', [('write', 'sub/inner.txt', 'inside sub\n'), ('write', '_lc_dlink', 'sub'), ['add', 'sub/inner.txt', '_lc_dlink'], ['update-index', '--add', '--cacheinfo', '120000', '3de0f365ba57c94daac626bf53a7da269b65f57c', 'dlink'], ['commit', '-m', 'c1']], ['cat-file', '--batch', '--follow-symlinks'], 'HEAD:dlink/inner.txt\n'),
    ('follow-dir-link-alone-check', [('write', 'sub/inner.txt', 'inside sub\n'), ('write', '_lc_dlink', 'sub'), ['add', 'sub/inner.txt', '_lc_dlink'], ['update-index', '--add', '--cacheinfo', '120000', '3de0f365ba57c94daac626bf53a7da269b65f57c', 'dlink'], ['commit', '-m', 'c1']], ['cat-file', '--batch-check', '--follow-symlinks'], 'HEAD:dlink\n'),
    ('follow-dangling', [('write', '_lc_dang', 'nope.txt'), ['add', '_lc_dang'], ['update-index', '--add', '--cacheinfo', '120000', '993250523c8e84d9bc926cebba36cc938c1ab8bf', 'dangling'], ['commit', '-m', 'c1']], ['cat-file', '--batch', '--follow-symlinks'], 'HEAD:dangling\n'),
    ('follow-absolute', [('write', '_lc_abs', '/etc/hostname'), ['add', '_lc_abs'], ['update-index', '--add', '--cacheinfo', '120000', '48980ad58db1b502c17dd015c92dd262ee8092af', 'abs'], ['commit', '-m', 'c1']], ['cat-file', '--batch', '--follow-symlinks'], 'HEAD:abs\n'),
    ('follow-absolute-check', [('write', '_lc_abs', '/etc/hostname'), ['add', '_lc_abs'], ['update-index', '--add', '--cacheinfo', '120000', '48980ad58db1b502c17dd015c92dd262ee8092af', 'abs'], ['commit', '-m', 'c1']], ['cat-file', '--batch-check', '--follow-symlinks'], 'HEAD:abs\n'),
    ('follow-escape-root-dotdot', [('write', '_lc_esc', '../outside.txt'), ['add', '_lc_esc'], ['update-index', '--add', '--cacheinfo', '120000', 'bfca5af2d1b24387484c9d566024f01eb8de7a25', 'esc'], ['commit', '-m', 'c1']], ['cat-file', '--batch', '--follow-symlinks'], 'HEAD:esc\n'),
    ('follow-loop', [('write', '_lc_la', 'lb'), ('write', '_lc_lb', 'la'), ['add', '_lc_la', '_lc_lb'], ['update-index', '--add', '--cacheinfo', '120000', '8d8be316182c78d1fff99ecce277b4b61b1cde01', 'la'], ['update-index', '--add', '--cacheinfo', '120000', '3e6885e8ee1df290562f84604b0d259d89372cf7', 'lb'], ['commit', '-m', 'c1']], ['cat-file', '--batch', '--follow-symlinks'], 'HEAD:la\n'),
    ('follow-notdir-via-symlink', [('write', 'target.txt', 'hello\n'), ('write', '_lc_link', 'target.txt'), ['add', 'target.txt', '_lc_link'], ['update-index', '--add', '--cacheinfo', '120000', '4cbb553f3f4ac2ee7b01ff6c951d6bf583c39c15', 'link'], ['commit', '-m', 'c1']], ['cat-file', '--batch', '--follow-symlinks'], 'HEAD:link/x\n'),
    ('follow-notdir-plain-regular', [('write', 'target.txt', 'hello\n'), ['add', 'target.txt'], ['commit', '-m', 'c1']], ['cat-file', '--batch', '--follow-symlinks'], 'HEAD:target.txt/x\n'),
    ('follow-missing-path', [('write', 'target.txt', 'hello\n'), ['add', 'target.txt'], ['commit', '-m', 'c1']], ['cat-file', '--batch', '--follow-symlinks'], 'HEAD:nope\n'),
    ('follow-bad-rev', [('write', 'target.txt', 'hello\n'), ('write', '_lc_link', 'target.txt'), ['add', 'target.txt', '_lc_link'], ['update-index', '--add', '--cacheinfo', '120000', '4cbb553f3f4ac2ee7b01ff6c951d6bf583c39c15', 'link'], ['commit', '-m', 'c1']], ['cat-file', '--batch', '--follow-symlinks'], 'NOPE:link\n'),
    ('follow-index-form-no-follow', [('write', 'target.txt', 'hello\n'), ('write', '_lc_link', 'target.txt'), ['add', 'target.txt', '_lc_link'], ['update-index', '--add', '--cacheinfo', '120000', '4cbb553f3f4ac2ee7b01ff6c951d6bf583c39c15', 'link'], ['commit', '-m', 'c1']], ['cat-file', '--batch', '--follow-symlinks'], ':link\n'),
    ('follow-nul-delim-absolute', [('write', '_lc_abs', '/etc/hostname'), ['add', '_lc_abs'], ['update-index', '--add', '--cacheinfo', '120000', '48980ad58db1b502c17dd015c92dd262ee8092af', 'abs'], ['commit', '-m', 'c1']], ['cat-file', '--batch', '-Z', '--follow-symlinks'], 'HEAD:abs\x00'),
    ('follow-batch-command-info-loop', [('write', '_lc_la', 'lb'), ('write', '_lc_lb', 'la'), ['add', '_lc_la', '_lc_lb'], ['update-index', '--add', '--cacheinfo', '120000', '8d8be316182c78d1fff99ecce277b4b61b1cde01', 'la'], ['update-index', '--add', '--cacheinfo', '120000', '3e6885e8ee1df290562f84604b0d259d89372cf7', 'lb'], ['commit', '-m', 'c1']], ['cat-file', '--batch-command', '--follow-symlinks'], 'info HEAD:la\n'),
    ('follow-batch-command-contents-link', [('write', 'target.txt', 'hello\n'), ('write', '_lc_link', 'target.txt'), ['add', 'target.txt', '_lc_link'], ['update-index', '--add', '--cacheinfo', '120000', '4cbb553f3f4ac2ee7b01ff6c951d6bf583c39c15', 'link'], ['commit', '-m', 'c1']], ['cat-file', '--batch-command', '--follow-symlinks'], 'contents HEAD:link\n'),
]

@pytest.mark.parametrize("case", BATCH5_CASES, ids=[c[0] for c in BATCH5_CASES])
def test_batch5_parity(case, tmp_path: Path, git_254_oracle: str):
    _id, setup, probe = case
    assert_command_parity(git_254_oracle, tmp_path, setup, probe)


@pytest.mark.parametrize("case", BATCH5_STDIN_CASES, ids=[c[0] for c in BATCH5_STDIN_CASES])
def test_batch5_stdin_parity(case, tmp_path: Path, git_254_oracle: str):
    _id, setup, probe, stdin = case
    assert_command_parity(git_254_oracle, tmp_path, setup, probe, stdin=stdin)


# Branch rename (-m/-M) is mostly transparent to ref-reading commands, but its
# correctness — including the same-name no-op move that must NOT delete the ref —
# is only fully observable by diffing the on-disk refs AND reflogs. A same-name
# `branch -M main` is a common idiom (e.g. after `init`), and an earlier bug
# deleted refs/heads/main outright; these lock the byte-exact ref+reflog state.
_BR_BASE = [("write", "a.txt", "1\n"), ["add", "-A"], ["commit", "-m", "c1"]]
_BR_BASE2 = _BR_BASE + [("write", "a.txt", "2\n"), ["add", "-A"], ["commit", "-m", "c2"]]
BRANCH_RENAME_STATE_CASES = [
    ("force-move-same-name", _BR_BASE, ["branch", "-M", "main"]),
    ("move-same-name", _BR_BASE, ["branch", "-m", "main"]),
    ("force-move-current-newname", _BR_BASE, ["branch", "-M", "dev"]),
    ("move-current-to-other", _BR_BASE, ["branch", "-m", "main", "trunk"]),
    # Branch *creation* records "branch: Created from <start>" where <start> is
    # the explicit start-point, else the current branch name (literal HEAD when
    # detached) — not the generic "update:" reflog message.
    ("create-from-current-branch", _BR_BASE, ["branch", "foo"]),
    ("create-from-named-branch", _BR_BASE, ["branch", "foo", "main"]),
    ("create-from-rev", _BR_BASE2, ["branch", "foo", "HEAD~1"]),
    ("create-from-head-literal", _BR_BASE, ["branch", "foo", "HEAD"]),
    # Renaming a non-current branch: exercises both the create reflog (for the
    # source branch) and the rename reflog, end to end.
    ("rename-noncurrent-branch", _BR_BASE + [["branch", "other"]],
     ["branch", "-m", "other", "renamed"]),
]


@pytest.mark.parametrize("case", BRANCH_RENAME_STATE_CASES,
                         ids=[c[0] for c in BRANCH_RENAME_STATE_CASES])
def test_branch_rename_state_parity(case, tmp_path: Path, git_254_oracle: str):
    import subprocess
    from tests.git_parity.support import DETERMINISTIC_ENV, ROOT, pygit_cmd

    _id, setup, probe = case
    env = dict(__import__("os").environ)
    env.update(DETERMINISTIC_ENV)
    env["PYTHONPATH"] = str(ROOT)

    def snapshot(repo: Path):
        gd = repo / ".git"
        pf = gd / "packed-refs"
        packed = pf.read_text() if pf.exists() else "<no-packed-refs>"
        refs = {
            str(p.relative_to(gd)).replace("\\", "/"): p.read_text()
            for p in (gd / "refs").rglob("*") if p.is_file()
        }
        logs = {}
        logdir = gd / "logs"
        if logdir.exists():
            logs = {
                str(p.relative_to(gd)).replace("\\", "/"): p.read_text()
                for p in logdir.rglob("*") if p.is_file()
            }
        head = (gd / "HEAD").read_text() if (gd / "HEAD").exists() else "<no-HEAD>"
        return packed, sorted(refs.items()), sorted(logs.items()), head

    results = {}
    for tool, base in (("oracle", [git_254_oracle]), ("pygit", pygit_cmd())):
        repo = tmp_path / tool
        repo.mkdir()
        subprocess.run([*base, "init", "-b", "main", "."], cwd=repo, env=env, capture_output=True)
        for step in setup:
            if isinstance(step, tuple) and step and step[0] == "write":
                (repo / step[1]).write_text(step[2])
            else:
                subprocess.run([*base, *step], cwd=repo, env=env, capture_output=True)
        proc = subprocess.run([*base, *probe], cwd=repo, env=env, capture_output=True, text=True)
        results[tool] = (proc.returncode, proc.stdout, proc.stderr, *snapshot(repo))

    assert results["pygit"] == results["oracle"]


BATCH6_CASES = [
    ('push-u', [('write', 't.txt', 'a'), ['add', 't.txt'], ['commit', '-q', '-m', 'init'], ('write', 't.txt', 'amod'), ('write', 'u.txt', 'untr')], ['stash', 'push', '-u', '-m', 'wu']),
    ('text-attr-crlf-converts-via-path', [('write', '.gitattributes', '*.txt text\n'), ('write', 'crlf.txt', 'a\r\nb\r\n')], ['hash-object', '--path', 'crlf.txt', 'crlf.txt']),
    ('text-attr-plain-uses-own-path', [('write', '.gitattributes', '*.txt text\n'), ('write', 'crlf.txt', 'a\r\nb\r\n')], ['hash-object', 'crlf.txt']),
    ('no-filters-bypasses-conversion', [('write', '.gitattributes', '*.txt text\n'), ('write', 'crlf.txt', 'a\r\nb\r\n')], ['hash-object', '--no-filters', 'crlf.txt']),
    ('filters-flag-enables-default-conversion', [('write', '.gitattributes', '*.txt text\n'), ('write', 'crlf.txt', 'a\r\nb\r\n')], ['hash-object', '--filters', '--path', 'crlf.txt', 'crlf.txt']),
    ('text-auto-crlf-converts', [('write', '.gitattributes', '*.txt text=auto\n'), ('write', 'f.txt', 'a\r\nb\r\n')], ['hash-object', '--path', 'f.txt', 'f.txt']),
    ('text-auto-binary-no-conversion', [('write', '.gitattributes', '*.txt text=auto\n'), ('write', 'f.txt', 'a\r\nb\x00\r\n')], ['hash-object', '--path', 'f.txt', 'f.txt']),
    ('eol-crlf-lf-to-crlf', [('write', '.gitattributes', '*.txt text eol=crlf\n'), ('write', 'f.txt', 'a\nb\n')], ['hash-object', '--path', 'f.txt', 'f.txt']),
    ('binary-attr-no-conversion', [('write', '.gitattributes', '*.txt -text\n'), ('write', 'f.txt', 'a\r\nb\r\n')], ['hash-object', '--path', 'f.txt', 'f.txt']),
    ('text-mixed-lone-cr-kept', [('write', '.gitattributes', '*.txt text\n'), ('write', 'f.txt', 'a\r\nb\r\nc\rd')], ['hash-object', '--path', 'f.txt', 'f.txt']),
    ('write-w-emits-safecrlf-warning', [('write', '.gitattributes', '*.txt text\n'), ('write', 'f.txt', 'a\r\nb\r\n')], ['hash-object', '-w', '--path', 'f.txt', 'f.txt']),
    ('safecrlf-true-dies-with-w', [['config', 'core.safecrlf', 'true'], ('write', '.gitattributes', '*.txt text\n'), ('write', 'f.txt', 'a\r\nb\r\n')], ['hash-object', '-w', '--path', 'f.txt', 'f.txt']),
    ('safecrlf-true-no-check-without-w', [['config', 'core.safecrlf', 'true'], ('write', '.gitattributes', '*.txt text\n'), ('write', 'f.txt', 'a\r\nb\r\n')], ['hash-object', '--path', 'f.txt', 'f.txt']),
    ('autocrlf-true-converts-undefined', [['config', 'core.autocrlf', 'true'], ('write', 'f.txt', 'a\r\nb\r\n')], ['hash-object', '-w', '--path', 'f.txt', 'f.txt']),
    ('ident-strips-expanded-id', [('write', '.gitattributes', '*.txt ident\n'), ('write', 'f.txt', 'x $Id: abcdef0123 $ y\n')], ['hash-object', '--path', 'f.txt', 'f.txt']),
    ('ident-plain-id-unchanged', [('write', '.gitattributes', '*.txt ident\n'), ('write', 'f.txt', 'plain $Id$ here\n')], ['hash-object', '--path', 'f.txt', 'f.txt']),
    ('err-no-filters-with-path', [('write', 'crlf.txt', 'a\r\nb\r\n')], ['hash-object', '--no-filters', '--path', 'crlf.txt', 'crlf.txt']),
    ('err-stdin-paths-with-stdin', [], ['hash-object', '--stdin-paths', '--stdin']),
    ('err-stdin-paths-with-files', [], ['hash-object', '--stdin-paths', 'f']),
    ('err-multiple-stdin', [], ['hash-object', '--stdin', '--stdin', 'f']),
    ('err-missing-file', [], ['hash-object', 'nope']),
    ('info-attributes-precedence', [('write', '.gitattributes', '*.txt -text\n'), ('write', '.git/info/attributes', '*.txt text\n'), ('write', 'f.txt', 'a\r\nb\r\n')], ['hash-object', '--path', 'f.txt', 'f.txt']),
    ('last-rule-wins', [('write', '.gitattributes', '*.txt text\n*.txt -text\n'), ('write', 'f.txt', 'a\r\nb\r\n')], ['hash-object', '--path', 'f.txt', 'f.txt']),
    ('subdir-no-slash-pattern-matches-basename', [('write', '.gitattributes', '*.txt text\n'), ('write', 's/x.txt', 'a\r\nb\r\n')], ['hash-object', '--path', 's/x.txt', 's/x.txt']),
    ('anchored-pattern-not-in-subdir', [('write', '.gitattributes', '/x.txt text\n'), ('write', 's/x.txt', 'a\r\nb\r\n')], ['hash-object', '--path', 's/x.txt', 's/x.txt']),
    ('diff-files-find-renames-raw', [('write', 'orig.txt', 'l1\nl2\nl3\nl4\nl5\nl6\nl7\nl8\nl9\nl10\n'), ['add', '-A'], ['commit', '-q', '-m', 'init'], ('rm', 'orig.txt'), ('write', 'copy.txt', 'l1\nl2\nl3\nl4\nl5\nl6\nl7\nl8\nl9\nl10\n'), ['add', '-N', 'copy.txt']], ['diff-files', '-M']),
    ('diff-files-find-renames-inexact-patch', [('write', 'orig.txt', 'l1\nl2\nl3\nl4\nl5\nl6\nl7\nl8\nl9\nl10\n'), ['add', '-A'], ['commit', '-q', '-m', 'init'], ('rm', 'orig.txt'), ('write', 'copy.txt', 'l1\nl2\nCHG\nl4\nl5\nl6\nl7\nl8\nl9\nl10\n'), ['add', '-N', 'copy.txt']], ['diff-files', '-M', '-p']),
    ('diff-files-find-renames-name-status', [('write', 'orig.txt', 'l1\nl2\nl3\nl4\nl5\nl6\nl7\nl8\nl9\nl10\n'), ['add', '-A'], ['commit', '-q', '-m', 'init'], ('rm', 'orig.txt'), ('write', 'copy.txt', 'l1\nl2\nCHG\nl4\nl5\nl6\nl7\nl8\nl9\nl10\n'), ['add', '-N', 'copy.txt']], ['diff-files', '-M', '--name-status']),
    ('diff-files-find-renames-z', [('write', 'orig.txt', 'l1\nl2\nl3\nl4\nl5\nl6\nl7\nl8\nl9\nl10\n'), ['add', '-A'], ['commit', '-q', '-m', 'init'], ('rm', 'orig.txt'), ('write', 'copy.txt', 'l1\nl2\nl3\nl4\nl5\nl6\nl7\nl8\nl9\nl10\n'), ['add', '-N', 'copy.txt']], ['diff-files', '-M', '-z']),
    ('diff-files-find-renames-reverse', [('write', 'orig.txt', 'l1\nl2\nl3\nl4\nl5\nl6\nl7\nl8\nl9\nl10\n'), ['add', '-A'], ['commit', '-q', '-m', 'init'], ('rm', 'orig.txt'), ('write', 'copy.txt', 'l1\nl2\nCHG\nl4\nl5\nl6\nl7\nl8\nl9\nl10\n'), ['add', '-N', 'copy.txt']], ['diff-files', '-M', '-R']),
    ('diff-files-find-renames-threshold-reject', [('write', 'orig.txt', 'l1\nl2\nl3\nl4\nl5\nl6\nl7\nl8\nl9\nl10\n'), ['add', '-A'], ['commit', '-q', '-m', 'init'], ('rm', 'orig.txt'), ('write', 'copy.txt', 'l1\nl2\nCHG\nl4\nl5\nl6\nl7\nl8\nl9\nl10\n'), ['add', '-N', 'copy.txt']], ['diff-files', '-M85']),
    ('diff-files-rename-subdir-numstat-compact', [('write', 'dir/orig.txt', 'l1\nl2\nl3\nl4\nl5\nl6\nl7\nl8\nl9\nl10\n'), ['add', '-A'], ['commit', '-q', '-m', 'init'], ('rm', 'dir/orig.txt'), ('write', 'dir/moved.txt', 'l1\nl2\nl3\nl4\nl5\nl6\nl7\nl8\nl9\nl10\n'), ['add', '-N', 'dir/moved.txt']], ['diff-files', '-M', '--numstat']),
    ('diff-files-find-copies-raw', [('write', 'base.txt', 'l1\nl2\nl3\nl4\nl5\nl6\nl7\nl8\n'), ['add', '-A'], ['commit', '-q', '-m', 'init'], ('write', 'base.txt', 'l1\nl2\nMOD\nl4\nl5\nl6\nl7\nl8\n'), ('write', 'copy.txt', 'l1\nl2\nl3\nl4\nl5\nl6\nl7\nl8\n'), ['add', '-N', 'copy.txt']], ['diff-files', '-C']),
    ('diff-files-find-copies-patch', [('write', 'base.txt', 'l1\nl2\nl3\nl4\nl5\nl6\nl7\nl8\n'), ['add', '-A'], ['commit', '-q', '-m', 'init'], ('write', 'base.txt', 'l1\nl2\nMOD\nl4\nl5\nl6\nl7\nl8\n'), ('write', 'copy.txt', 'l1\nl2\nl3\nl4\nl5\nl6\nl7\nl8\n'), ['add', '-N', 'copy.txt']], ['diff-files', '-C', '-p']),
    ('diff-files-find-copies-harder', [('write', 'base.txt', 'l1\nl2\nl3\nl4\nl5\nl6\nl7\nl8\n'), ['add', '-A'], ['commit', '-q', '-m', 'init'], ('write', 'copy.txt', 'l1\nl2\nl3\nl4\nl5\nl6\nl7\nl8\n'), ['add', '-N', 'copy.txt']], ['diff-files', '-C', '--find-copies-harder']),
    ('diff-files-copy-one-src-two-dst', [('write', 'orig.txt', 'l1\nl2\nl3\nl4\nl5\nl6\nl7\nl8\n'), ['add', '-A'], ['commit', '-q', '-m', 'init'], ('rm', 'orig.txt'), ('write', 'c1.txt', 'l1\nl2\nl3\nl4\nl5\nl6\nl7\nl8\n'), ('write', 'c2.txt', 'l1\nl2\nl3\nl4\nl5\nl6\nl7\nl8\n'), ['add', '-N', 'c1.txt', 'c2.txt']], ['diff-files', '-C']),
    ('diff-files-pickaxe-S-raw', [('write', 'a.txt', 'apple\nbanana\ncherry\n'), ('write', 'b.txt', 'dog\ncat\n'), ['add', '-A'], ['commit', '-q', '-m', 'init'], ('write', 'a.txt', 'apple\nbanana\nneedle\ncherry\n'), ('write', 'b.txt', 'dog\ncat\nbird\n')], ['diff-files', '-S', 'needle']),
    ('diff-files-pickaxe-G-patch', [('write', 'a.txt', 'apple\nbanana\ncherry\n'), ('write', 'b.txt', 'dog\ncat\n'), ['add', '-A'], ['commit', '-q', '-m', 'init'], ('write', 'a.txt', 'apple\nbanana\nneedle\ncherry\n'), ('write', 'b.txt', 'dog\ncat\nbird\n')], ['diff-files', '-G', 'needle', '-p']),
    ('diff-files-pickaxe-G-deleted-line', [('write', 'a.txt', 'keep\nremoveme\nkeep2\n'), ['add', '-A'], ['commit', '-q', '-m', 'init'], ('write', 'a.txt', 'keep\nkeep2\n')], ['diff-files', '-G', 'removeme', '-p']),
    ('diff-files-pickaxe-all-patch', [('write', 'a.txt', 'apple\nbanana\ncherry\n'), ('write', 'b.txt', 'dog\ncat\n'), ['add', '-A'], ['commit', '-q', '-m', 'init'], ('write', 'a.txt', 'apple\nbanana\nneedle\ncherry\n'), ('write', 'b.txt', 'dog\ncat\nbird\n')], ['diff-files', '-S', 'needle', '--pickaxe-all', '-p']),
    ('diff-files-pickaxe-S-no-match', [('write', 'a.txt', 'apple\nbanana\ncherry\n'), ['add', '-A'], ['commit', '-q', '-m', 'init'], ('write', 'a.txt', 'apple\nbanana\nneedle\ncherry\n')], ['diff-files', '-S', 'zzz']),
    ('diff-files-orderfile', [('write', 'zebra.txt', 'z\n'), ('write', 'apple.txt', 'a\n'), ('write', 'middle.txt', 'm\n'), ['add', '-A'], ['commit', '-q', '-m', 'init'], ('write', 'zebra.txt', 'z2\n'), ('write', 'apple.txt', 'a2\n'), ('write', 'middle.txt', 'm2\n'), ('write', '.order', 'middle.txt\nzebra.txt\n')], ['diff-files', '-O.order']),
    ('diff-files-orderfile-missing-fatal', [('write', 'f.txt', 'a\n'), ['add', '-A'], ['commit', '-q', '-m', 'init'], ('write', 'f.txt', 'b\n')], ['diff-files', '-O.nope']),
    ('diff-files-break-rewrites-patch', [('write', 'f.txt', 'oldline000\noldline001\noldline002\noldline003\noldline004\noldline005\noldline006\noldline007\noldline008\noldline009\noldline010\noldline011\noldline012\noldline013\noldline014\noldline015\noldline016\noldline017\noldline018\noldline019\noldline020\noldline021\noldline022\noldline023\noldline024\noldline025\noldline026\noldline027\noldline028\noldline029\noldline030\noldline031\noldline032\noldline033\noldline034\noldline035\noldline036\noldline037\noldline038\noldline039\n'), ['add', '-A'], ['commit', '-q', '-m', 'init'], ('write', 'f.txt', 'newline000\nnewline001\nnewline002\nnewline003\nnewline004\nnewline005\nnewline006\nnewline007\nnewline008\nnewline009\nnewline010\nnewline011\nnewline012\nnewline013\nnewline014\nnewline015\nnewline016\nnewline017\nnewline018\nnewline019\nnewline020\nnewline021\nnewline022\nnewline023\nnewline024\nnewline025\nnewline026\nnewline027\nnewline028\nnewline029\nnewline030\nnewline031\nnewline032\nnewline033\nnewline034\nnewline035\nnewline036\nnewline037\nnewline038\nnewline039\n')], ['diff-files', '-B', '-p']),
    ('diff-files-break-rewrites-raw', [('write', 'f.txt', 'oldline000\noldline001\noldline002\noldline003\noldline004\noldline005\noldline006\noldline007\noldline008\noldline009\noldline010\noldline011\noldline012\noldline013\noldline014\noldline015\noldline016\noldline017\noldline018\noldline019\noldline020\noldline021\noldline022\noldline023\noldline024\noldline025\noldline026\noldline027\noldline028\noldline029\noldline030\noldline031\noldline032\noldline033\noldline034\noldline035\noldline036\noldline037\noldline038\noldline039\n'), ['add', '-A'], ['commit', '-q', '-m', 'init'], ('write', 'f.txt', 'newline000\nnewline001\nnewline002\nnewline003\nnewline004\nnewline005\nnewline006\nnewline007\nnewline008\nnewline009\nnewline010\nnewline011\nnewline012\nnewline013\nnewline014\nnewline015\nnewline016\nnewline017\nnewline018\nnewline019\nnewline020\nnewline021\nnewline022\nnewline023\nnewline024\nnewline025\nnewline026\nnewline027\nnewline028\nnewline029\nnewline030\nnewline031\nnewline032\nnewline033\nnewline034\nnewline035\nnewline036\nnewline037\nnewline038\nnewline039\n')], ['diff-files', '-B']),
    ('diff-files-break-rewrites-numstat', [('write', 'f.txt', 'oldline000\noldline001\noldline002\noldline003\noldline004\noldline005\noldline006\noldline007\noldline008\noldline009\noldline010\noldline011\noldline012\noldline013\noldline014\noldline015\noldline016\noldline017\noldline018\noldline019\noldline020\noldline021\noldline022\noldline023\noldline024\noldline025\noldline026\noldline027\noldline028\noldline029\noldline030\noldline031\noldline032\noldline033\noldline034\noldline035\noldline036\noldline037\noldline038\noldline039\n'), ['add', '-A'], ['commit', '-q', '-m', 'init'], ('write', 'f.txt', 'newline000\nnewline001\nnewline002\nnewline003\nnewline004\nnewline005\nnewline006\nnewline007\nnewline008\nnewline009\nnewline010\nnewline011\nnewline012\nnewline013\nnewline014\nnewline015\nnewline016\nnewline017\nnewline018\nnewline019\nnewline020\nnewline021\nnewline022\nnewline023\nnewline024\nnewline025\nnewline026\nnewline027\nnewline028\nnewline029\nnewline030\nnewline031\nnewline032\nnewline033\nnewline034\nnewline035\nnewline036\nnewline037\nnewline038\nnewline039\n')], ['diff-files', '-B', '--numstat']),
    ('diff-files-rename-limit-warning', [('write', 's1.txt', 'a\nb\nc\n'), ('write', 's2.txt', 'd\ne\nf\n'), ['add', '-A'], ['commit', '-q', '-m', 'init'], ('rm', 's1.txt'), ('rm', 's2.txt'), ('write', 'd1.txt', 'a\nb\nX\n'), ('write', 'd2.txt', 'd\ne\nY\n'), ['add', '-N', 'd1.txt', 'd2.txt']], ['diff-files', '-M', '-l1']),
    ('diff-files-M-invalid-arg', [('write', 'f.txt', 'a\n'), ['add', '-A'], ['commit', '-q', '-m', 'init'], ('write', 'f.txt', 'b\n')], ['diff-files', '-Mxyz']),
    ('diff-files-C-invalid-arg', [('write', 'f.txt', 'a\n'), ['add', '-A'], ['commit', '-q', '-m', 'init'], ('write', 'f.txt', 'b\n')], ['diff-files', '-Cabc']),
    ('diff-files-B-bad-form', [('write', 'f.txt', 'a\n'), ['add', '-A'], ['commit', '-q', '-m', 'init'], ('write', 'f.txt', 'b\n')], ['diff-files', '-B/0/0']),
    ('diff-files-S-missing-value', [('write', 'f.txt', 'a\n'), ['add', '-A'], ['commit', '-q', '-m', 'init'], ('write', 'f.txt', 'b\n')], ['diff-files', '-S']),
    ('diff-files-l-non-numeric', [('write', 'f.txt', 'a\n'), ['add', '-A'], ['commit', '-q', '-m', 'init'], ('write', 'f.txt', 'b\n')], ['diff-files', '-l', 'foo']),
    ('log-l-range-linear', [('write', 'f.txt', 'line1\nline2\nline3\nline4\nline5\n'), ['add', 'f.txt'], ['commit', '-m', 'c1'], ('write', 'f.txt', 'line1\nline2 modified\nline3\nline4\nline5\n'), ['commit', '-am', 'c2'], ('write', 'f.txt', 'line1\nline2 modified\nline3 changed\nline4\nline5\nline6 new\n'), ['commit', '-am', 'c3']], ['log', '-L2,3:f.txt']),
    ('log-l-oneline', [('write', 'f.txt', 'a\nb\nc\n'), ['add', 'f.txt'], ['commit', '-m', 'c1'], ('write', 'f.txt', 'a\nB\nc\n'), ['commit', '-am', 'c2']], ['log', '--oneline', '-L1,2:f.txt']),
    ('log-l-relative-end', [('write', 'f.txt', 'a\nb\nc\nd\ne\nf\ng\nh\ni\nj\n'), ['add', 'f.txt'], ['commit', '-m', 'c1'], ('write', 'f.txt', 'a\nB\nc\nd\ne\nf\ng\nh\nI\nj\n'), ['commit', '-am', 'c2']], ['log', '-L2,+3:f.txt']),
    ('log-l-funcname', [('write', 'f.c', 'int foo(void)\n{\n\treturn 1;\n}\n\nint bar(void)\n{\n\treturn 2;\n}\n'), ['add', 'f.c'], ['commit', '-m', 'c1'], ('write', 'f.c', 'int foo(void)\n{\n\treturn 11;\n}\n\nint bar(void)\n{\n\treturn 2;\n}\n'), ['commit', '-am', 'c2']], ['log', '-L:foo:f.c']),
    ('log-l-funcname-suffix-in-hunk', [('write', 'f.c', 'int foo(void)\n{\n\tint a = 1;\n\tint b = 2;\n\tint c = 3;\n\treturn a;\n}\n'), ['add', 'f.c'], ['commit', '-m', 'c1'], ('write', 'f.c', 'int foo(void)\n{\n\tint a = 1;\n\tint b = 2;\n\tint c = 30;\n\treturn a;\n}\n'), ['commit', '-am', 'c2']], ['log', '-L5,5:f.c']),
    ('log-l-regex-bounds', [('write', 'f.c', 'int foo(void)\n{\n\tint a = 1;\n\tint b = 2;\n\tint c = 3;\n\treturn a;\n}\n'), ['add', 'f.c'], ['commit', '-m', 'c1'], ('write', 'f.c', 'int foo(void)\n{\n\tint a = 1;\n\tint b = 2;\n\tint c = 30;\n\treturn a;\n}\n'), ['commit', '-am', 'c2']], ['log', '-L/int b/,/return a/:f.c']),
    ('log-l-tracks-through-insertion', [('write', 'f.txt', 'a\nb\nc\nd\ne\n'), ['add', 'f.txt'], ['commit', '-m', 'c1'], ('write', 'f.txt', 'HEADER\na\nb\nc\nd\ne\n'), ['commit', '-am', 'c2'], ('write', 'f.txt', 'HEADER\na\nb\nC\nd\ne\n'), ['commit', '-am', 'c3']], ['log', '-L4,4:f.txt']),
    ('log-l-deletion', [('write', 'f.txt', 'a\nb\nc\nd\ne\n'), ['add', 'f.txt'], ['commit', '-m', 'c1'], ('write', 'f.txt', 'a\nc\nd\ne\n'), ['commit', '-am', 'c2']], ['log', '-L1,3:f.txt']),
    ('log-l-multiple-files-path-sorted', [('write', 'a.txt', 'a1\na2\na3\n'), ('write', 'b.txt', 'b1\nb2\nb3\n'), ['add', 'a.txt'], ['add', 'b.txt'], ['commit', '-m', 'c1'], ('write', 'a.txt', 'a1\nA2\na3\n'), ['commit', '-am', 'c2'], ('write', 'b.txt', 'b1\nB2\nb3\n'), ['commit', '-am', 'c3']], ['log', '-L2,2:b.txt', '-L2,2:a.txt']),
    ('log-l-no-newline-eof', [('write', 'f.txt', 'a\nb\nc'), ['add', 'f.txt'], ['commit', '-m', 'c1'], ('write', 'f.txt', 'a\nB\nc'), ['commit', '-am', 'c2']], ['log', '-L1,3:f.txt']),
    ('log-l-raw-format', [('write', 'f.txt', 'a\nb\nc\nd\ne\nf\ng\nh\ni\nj\n'), ['add', 'f.txt'], ['commit', '-m', 'c1'], ('write', 'f.txt', 'a\nB\nc\nd\ne\nf\ng\nh\nI\nj\n'), ['commit', '-am', 'c2']], ['log', '-L2,2:f.txt', '--pretty=raw']),
    ('log-l-rename-follow', [('write', 'old.txt', 'a\nb\nc\nd\n'), ['add', 'old.txt'], ['commit', '-m', 'c1'], ['mv', 'old.txt', 'new.txt'], ['commit', '-am', 'rename'], ('write', 'new.txt', 'a\nB\nc\nd\n'), ['commit', '-am', 'modify']], ['log', '-L2,2:new.txt']),
    ('show-l-default-head', [('write', 'f.txt', 'line1\nline2\nline3\nline4\nline5\n'), ['add', 'f.txt'], ['commit', '-m', 'c1'], ('write', 'f.txt', 'line1\nline2 modified\nline3\nline4\nline5\n'), ['commit', '-am', 'c2']], ['show', '-L2,3:f.txt']),
    ('show-l-explicit-rev', [('write', 'f.txt', 'line1\nline2\nline3\nline4\nline5\n'), ['add', 'f.txt'], ['commit', '-m', 'c1'], ('write', 'f.txt', 'line1\nline2 modified\nline3\nline4\nline5\n'), ['commit', '-am', 'c2']], ['show', '-L2,3:f.txt', 'HEAD~1']),
    ('show-l-multi-range', [('write', 'f.txt', 'a\nb\nc\nd\ne\nf\ng\n'), ['add', 'f.txt'], ['commit', '-m', 'c1']], ['show', '-L2,3:f.txt', '-L6,6:f.txt']),
    ('show-l-no-patch', [('write', 'f.txt', 'a\nb\nc\n'), ['add', 'f.txt'], ['commit', '-m', 'c1'], ('write', 'f.txt', 'a\nB\nc\n'), ['commit', '-am', 'c2']], ['show', '-L1,2:f.txt', '-s']),
    ('show-l-two-commits-error', [('write', 'f.txt', 'a\nb\nc\n'), ['add', 'f.txt'], ['commit', '-m', 'c1'], ('write', 'f.txt', 'a\nB\nc\n'), ['commit', '-am', 'c2']], ['show', '-L1,2:f.txt', 'HEAD', 'HEAD~1']),
    ('log-l-error-no-file-part', [('write', 'f.txt', 'a\nb\nc\n'), ['add', 'f.txt'], ['commit', '-m', 'c1']], ['log', '-L2,3']),
    ('log-l-error-missing-colon', [('write', 'f.txt', 'a\nb\nc\n'), ['add', 'f.txt'], ['commit', '-m', 'c1']], ['log', '-Lfoo']),
    ('log-l-error-bad-range', [('write', 'f.txt', 'a\nb\nc\n'), ['add', 'f.txt'], ['commit', '-m', 'c1']], ['log', '-L2,bad:f.txt']),
    ('log-l-error-line-zero', [('write', 'f.txt', 'a\nb\nc\n'), ['add', 'f.txt'], ['commit', '-m', 'c1']], ['log', '-L0,2:f.txt']),
    ('log-l-error-too-many-lines', [('write', 'f.txt', 'a\nb\nc\n'), ['add', 'f.txt'], ['commit', '-m', 'c1']], ['log', '-L100,200:f.txt']),
    ('log-l-error-no-such-path', [('write', 'f.txt', 'a\nb\nc\n'), ['add', 'f.txt'], ['commit', '-m', 'c1']], ['log', '-L1,2:nope.txt']),
    ('log-l-error-empty-funcname', [('write', 'f.c', 'int foo(void)\n{\n\treturn 1;\n}\n'), ['add', 'f.c'], ['commit', '-m', 'c1']], ['log', '-L::f.c']),
    ('log-l-error-funcname-no-match', [('write', 'f.c', 'int foo(void)\n{\n\treturn 1;\n}\n'), ['add', 'f.c'], ['commit', '-m', 'c1']], ['log', '-L:nosuchfunc:f.c']),
    ('log-l-error-regex-no-match', [('write', 'f.c', 'int foo(void)\n{\n\treturn 1;\n}\n'), ['add', 'f.c'], ['commit', '-m', 'c1']], ['log', '-L/nomatch_regex_xyz/:f.c']),
    ('log-l-error-directory-path', [('write', 'sub/x.txt', 'a\nb\nc\n'), ['add', 'sub/x.txt'], ['commit', '-m', 'c1']], ['log', '-L1,2:sub']),
]

BATCH6_STDIN_CASES = [
    ('stdin-with-path-converts', [('write', '.gitattributes', '*.txt text\n')], ['hash-object', '--stdin', '--path', 'crlf.txt'], 'a\r\nb\r\n'),
    ('stdin-without-path-no-conversion', [('write', '.gitattributes', '*.txt text\n')], ['hash-object', '--stdin'], 'a\r\nb\r\n'),
    ('stdin-paths-converts', [('write', '.gitattributes', '*.txt text\n'), ('write', 'crlf.txt', 'a\r\nb\r\n')], ['hash-object', '--stdin-paths'], 'crlf.txt\n'),
    ('stdin-paths-no-filters', [('write', '.gitattributes', '*.txt text\n'), ('write', 'crlf.txt', 'a\r\nb\r\n')], ['hash-object', '--stdin-paths', '--no-filters'], 'crlf.txt\n'),
    ('no-args-succeeds-silently', [], ['hash-object'], ''),
]

@pytest.mark.parametrize("case", BATCH6_CASES, ids=[c[0] for c in BATCH6_CASES])
def test_batch6_parity(case, tmp_path: Path, git_254_oracle: str):
    _id, setup, probe = case
    assert_command_parity(git_254_oracle, tmp_path, setup, probe)


@pytest.mark.parametrize("case", BATCH6_STDIN_CASES, ids=[c[0] for c in BATCH6_STDIN_CASES])
def test_batch6_stdin_parity(case, tmp_path: Path, git_254_oracle: str):
    _id, setup, probe, stdin = case
    assert_command_parity(git_254_oracle, tmp_path, setup, probe, stdin=stdin)


# stash: the batch-6 agent verified ~28 cases but serialized only one, so these
# lock the implemented push/show/export flags (-u/-a/--staged, show -u/
# --only-untracked, export --print/--to-ref) byte-exact incl. the refs/stash
# commit graph. `stash push <pathspec>` is honestly rejected (not byte-exact),
# so it is intentionally absent.
_ST_BASE = [("write", "t.txt", "a\nb\n"), ["add", "t.txt"], ["commit", "-q", "-m", "init"]]
BATCH6_STASH_CASES = [
    ("stash-push-default-msg", _ST_BASE + [("write", "t.txt", "mod\n")], ["stash"]),
    ("stash-no-local-changes", _ST_BASE, ["stash"]),
    ("stash-list-after", _ST_BASE + [("write", "t.txt", "mod\n"), ["stash", "push", "-m", "one"]],
     ["stash", "list"]),
    ("stash-show-p-after", _ST_BASE + [("write", "t.txt", "mod\n"), ["stash", "push", "-m", "one"]],
     ["stash", "show", "-p", "stash@{0}"]),
    ("stash-show-u", _ST_BASE + [("write", "t.txt", "mod\n"), ("write", "u.txt", "untr\n"),
                                 ["stash", "push", "-u", "-m", "wu"]],
     ["stash", "show", "-u", "stash@{0}"]),
    ("stash-show-only-untracked",
     _ST_BASE + [("write", "t.txt", "mod\n"), ("write", "u.txt", "u\n"),
                 ["stash", "push", "-u", "-q", "-m", "wu"]],
     ["stash", "show", "--only-untracked", "stash@{0}"]),
    ("stash-staged-clean-worktree",
     _ST_BASE + [("write", "t.txt", "staged\n"), ["add", "t.txt"]],
     ["stash", "push", "--staged", "-m", "st"]),
    ("stash-all-ignored",
     _ST_BASE + [("write", ".gitignore", "*.ign\n"), ["add", ".gitignore"], ["commit", "-q", "-m", "gi"],
                 ("write", "t.txt", "mod\n"), ("write", "x.ign", "ign\n")],
     ["stash", "push", "-a", "-m", "all"]),
    ("stash-export-print",
     _ST_BASE + [("write", "t.txt", "mod\n"), ["stash", "push", "-q", "-m", "one"]],
     ["stash", "export", "--print", "stash@{0}"]),
    ("stash-export-to-ref",
     _ST_BASE + [("write", "t.txt", "mod\n"), ["stash", "push", "-q", "-m", "one"]],
     ["stash", "export", "--to-ref", "refs/x", "stash@{0}"]),
]


@pytest.mark.parametrize("case", BATCH6_STASH_CASES, ids=[c[0] for c in BATCH6_STASH_CASES])
def test_batch6_stash_parity(case, tmp_path: Path, git_254_oracle: str):
    _id, setup, probe = case
    assert_command_parity(git_254_oracle, tmp_path, setup, probe)


BATCH7_CASES = [
    ('apply-p2-strip', [('write', 'sub/g.txt', 'a\nb\nc\n'), ('write', 'g.txt', 'a\nb\nc\n'), ['add', '.'], ['commit', '-m', 'base'], ('write', 'P.diff', 'diff --git a/sub/g.txt b/sub/g.txt\nindex de98044..7be73ce 100644\n--- a/sub/g.txt\n+++ b/sub/g.txt\n@@ -1,3 +1,3 @@\n a\n-b\n+B\n c\n')], ['apply', '-p2', 'P.diff']),
    ('apply-no-add', [('write', 'f.txt', 'line1\nline2\nline3\nline4\nline5\n'), ['add', 'f.txt'], ['commit', '-m', 'base'], ('write', 'P.diff', 'diff --git a/f.txt b/f.txt\nindex b3c5a95..cf92929 100644\n--- a/f.txt\n+++ b/f.txt\n@@ -1,5 +1,5 @@\n line1\n line2\n-line3\n+CHANGED\n line4\n line5\n')], ['apply', '--no-add', 'P.diff']),
    ('apply-z-numstat', [('write', 'P.diff', 'diff --git a/f.txt b/f.txt\nindex b3c5a95..cf92929 100644\n--- a/f.txt\n+++ b/f.txt\n@@ -1,5 +1,5 @@\n line1\n line2\n-line3\n+CHANGED\n line4\n line5\n')], ['apply', '-z', '--numstat', 'P.diff']),
    ('apply-C1-context-fuzz', [('write', 'f.txt', 'Z1\nc2\nc3\nTARGET\nc4\nc5\nc6\n'), ['add', 'f.txt'], ['commit', '-m', 'base'], ('write', 'P.diff', 'diff --git a/f.txt b/f.txt\nindex f9fea4a..d8b4659 100644\n--- a/f.txt\n+++ b/f.txt\n@@ -1,7 +1,7 @@\n c1\n c2\n c3\n-TARGET\n+MODIFIED\n c4\n c5\n c6\n')], ['apply', '-C1', 'P.diff']),
    ('apply-C3-context-fuzz-fail', [('write', 'f.txt', 'Z1\nc2\nc3\nTARGET\nc4\nc5\nc6\n'), ['add', 'f.txt'], ['commit', '-m', 'base'], ('write', 'P.diff', 'diff --git a/f.txt b/f.txt\nindex f9fea4a..d8b4659 100644\n--- a/f.txt\n+++ b/f.txt\n@@ -1,7 +1,7 @@\n c1\n c2\n c3\n-TARGET\n+MODIFIED\n c4\n c5\n c6\n')], ['apply', '-C3', 'P.diff']),
    ('apply-N-intent-to-add', [('write', 'base.txt', 'base\n'), ['add', 'base.txt'], ['commit', '-m', 'base'], ('write', 'P.diff', 'diff --git a/new.txt b/new.txt\nnew file mode 100644\nindex 0000000..94954ab\n--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1,2 @@\n+hello\n+world\n')], ['apply', '-N', 'P.diff']),
    ('apply-3way-conflict-stages', [('write', 'f.txt', 'line1\nline2\nline3\nline4\nline5\n'), ['add', 'f.txt'], ['commit', '-m', 'base'], ('write', 'f.txt', 'line1\nline2\nLOCAL3\nline4\nline5\n'), ['add', 'f.txt'], ('write', 'P.diff', 'diff --git a/f.txt b/f.txt\nindex b3c5a95..cf92929 100644\n--- a/f.txt\n+++ b/f.txt\n@@ -1,5 +1,5 @@\n line1\n line2\n-line3\n+CHANGED\n line4\n line5\n')], ['apply', '--3way', 'P.diff']),
    ('apply-3way-ours', [('write', 'f.txt', 'line1\nline2\nline3\nline4\nline5\n'), ['add', 'f.txt'], ['commit', '-m', 'base'], ('write', 'f.txt', 'line1\nline2\nLOCAL3\nline4\nline5\n'), ['add', 'f.txt'], ('write', 'P.diff', 'diff --git a/f.txt b/f.txt\nindex b3c5a95..cf92929 100644\n--- a/f.txt\n+++ b/f.txt\n@@ -1,5 +1,5 @@\n line1\n line2\n-line3\n+CHANGED\n line4\n line5\n')], ['apply', '--3way', '--ours', 'P.diff']),
    ('apply-3way-union', [('write', 'f.txt', 'line1\nline2\nline3\nline4\nline5\n'), ['add', 'f.txt'], ['commit', '-m', 'base'], ('write', 'f.txt', 'line1\nline2\nLOCAL3\nline4\nline5\n'), ['add', 'f.txt'], ('write', 'P.diff', 'diff --git a/f.txt b/f.txt\nindex b3c5a95..cf92929 100644\n--- a/f.txt\n+++ b/f.txt\n@@ -1,5 +1,5 @@\n line1\n line2\n-line3\n+CHANGED\n line4\n line5\n')], ['apply', '--3way', '--union', 'P.diff']),
    ('apply-ours-without-3way-fatal', [('write', 'P.diff', 'diff --git a/f.txt b/f.txt\nindex b3c5a95..cf92929 100644\n--- a/f.txt\n+++ b/f.txt\n@@ -1,5 +1,5 @@\n line1\n line2\n-line3\n+CHANGED\n line4\n line5\n')], ['apply', '--ours', 'P.diff']),
    ('apply-reverse-newfile-becomes-delete', [('write', 'base.txt', 'base\n'), ['add', 'base.txt'], ['commit', '-m', 'base'], ('write', 'new.txt', 'hello\nworld\n'), ['add', 'new.txt'], ['commit', '-m', 'add new'], ('write', 'P.diff', 'diff --git a/new.txt b/new.txt\nnew file mode 100644\nindex 0000000..94954ab\n--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1,2 @@\n+hello\n+world\n')], ['apply', '-R', 'P.diff']),
    ('apply-stat-apply', [('write', 'f.txt', 'line1\nline2\nline3\nline4\nline5\n'), ['add', 'f.txt'], ['commit', '-m', 'base'], ('write', 'P.diff', 'diff --git a/f.txt b/f.txt\nindex b3c5a95..cf92929 100644\n--- a/f.txt\n+++ b/f.txt\n@@ -1,5 +1,5 @@\n line1\n line2\n-line3\n+CHANGED\n line4\n line5\n')], ['apply', '--stat', '--apply', 'P.diff']),
    ('apply-mismatch-fail', [('write', 'f.txt', 'DIFFERENT\nstuff\n'), ['add', 'f.txt'], ['commit', '-m', 'base'], ('write', 'P.diff', 'diff --git a/f.txt b/f.txt\nindex b3c5a95..cf92929 100644\n--- a/f.txt\n+++ b/f.txt\n@@ -1,5 +1,5 @@\n line1\n line2\n-line3\n+CHANGED\n line4\n line5\n')], ['apply', 'P.diff']),
    ('apply-lacks-filename-fatal', [('write', 't.txt', 'x\ny\nz\n'), ['add', 't.txt'], ['commit', '-m', 'base'], ('write', 'P.diff', 'diff --git t.txt t.txt\nindex 04ec35a..20a747d 100644\n--- t.txt\n+++ t.txt\n@@ -1,3 +1,3 @@\n x\n-y\n+Y\n z\n')], ['apply', 'P.diff']),
    ('stash push single pathspec resets only that path', [('write', 'a.txt', 'a1\n'), ('write', 'b.txt', 'b1\n'), ['add', 'a.txt', 'b.txt'], ['commit', '-m', 'init'], ('write', 'a.txt', 'a1\na2\n'), ('write', 'b.txt', 'b1\nb2\n')], ['stash', 'push', '--', 'a.txt']),
    ('stash push pathspec then status shows others still modified', [('write', 'a.txt', 'a1\n'), ('write', 'b.txt', 'b1\n'), ('write', 'c.txt', 'c1\n'), ['add', 'a.txt', 'b.txt', 'c.txt'], ['commit', '-m', 'init'], ('write', 'a.txt', 'a1\na2\n'), ('write', 'b.txt', 'b1\nb2\n'), ('write', 'c.txt', 'c1\nc2\n'), ['stash', 'push', '--', 'a.txt']], ['status', '--porcelain']),
    ('stash push pathspec staged+worktree resets both to HEAD', [('write', 'a.txt', 'a1\n'), ('write', 'b.txt', 'b1\n'), ['add', 'a.txt', 'b.txt'], ['commit', '-m', 'init'], ('write', 'a.txt', 'a1\nstaged\n'), ['add', 'a.txt'], ('write', 'a.txt', 'a1\nstaged\nwork\n'), ('write', 'b.txt', 'b1\nwork\n'), ['stash', 'push', '--', 'a.txt']], ['status', '--porcelain']),
    ('stash push --keep-index pathspec restores staged content', [('write', 'a.txt', 'a1\n'), ('write', 'b.txt', 'b1\n'), ['add', 'a.txt', 'b.txt'], ['commit', '-m', 'init'], ('write', 'a.txt', 'a1\nstaged\n'), ['add', 'a.txt'], ('write', 'a.txt', 'a1\nstaged\nwork\n'), ('write', 'b.txt', 'b1\nwork\n'), ['stash', 'push', '--keep-index', '--', 'a.txt']], ['status', '--porcelain']),
    ('stash push pathspec matching no tracked file errors', [('write', 'a.txt', 'a1\n'), ['add', 'a.txt'], ['commit', '-m', 'init'], ('write', 'a.txt', 'a1\na2\n')], ['stash', 'push', '--', 'nonexistent.txt']),
    ('stash push one matched one unmatched pathspec errors', [('write', 'a.txt', 'a1\n'), ('write', 'b.txt', 'b1\n'), ['add', 'a.txt', 'b.txt'], ['commit', '-m', 'init'], ('write', 'a.txt', 'a1\na2\n'), ('write', 'b.txt', 'b1\nb2\n')], ['stash', 'push', '--', 'a.txt', 'nope.txt']),
    ('stash push two unmatched pathspecs reports both in order', [('write', 'a.txt', 'a1\n'), ['add', 'a.txt'], ['commit', '-m', 'init'], ('write', 'a.txt', 'a1\na2\n')], ['stash', 'push', '--', 'zzz.txt', 'aaa.txt']),
    ('stash push pathspec matching tracked file with no change', [('write', 'a.txt', 'a1\n'), ('write', 'b.txt', 'b1\n'), ['add', 'a.txt', 'b.txt'], ['commit', '-m', 'init'], ('write', 'a.txt', 'a1\na2\n')], ['stash', 'push', '--', 'b.txt']),
    ('stash push directory pathspec stashes all under it', [('write', 'a.txt', 'a1\n'), ('write', 'd/x.txt', 'x1\n'), ('write', 'd/y.log', 'y1\n'), ['add', '-A'], ['commit', '-m', 'init'], ('write', 'a.txt', 'a1\na2\n'), ('write', 'd/x.txt', 'x1\nx2\n'), ('write', 'd/y.log', 'y1\ny2\n'), ['stash', 'push', '--', 'd']], ['status', '--porcelain']),
    ('stash push --pathspec-from-file newline', [('write', 'a.txt', 'a1\n'), ('write', 'b.txt', 'b1\n'), ('write', 'c.txt', 'c1\n'), ['add', 'a.txt', 'b.txt', 'c.txt'], ['commit', '-m', 'init'], ('write', 'a.txt', 'a1\na2\n'), ('write', 'b.txt', 'b1\nb2\n'), ('write', 'c.txt', 'c1\nc2\n'), ('write', 'specs.txt', 'a.txt\nb.txt\n'), ['stash', 'push', '--pathspec-from-file=specs.txt']], ['status', '--porcelain']),
    ('stash push --pathspec-from-file --pathspec-file-nul', [('write', 'a.txt', 'a1\n'), ('write', 'b.txt', 'b1\n'), ('write', 'c.txt', 'c1\n'), ['add', 'a.txt', 'b.txt', 'c.txt'], ['commit', '-m', 'init'], ('write', 'a.txt', 'a1\na2\n'), ('write', 'b.txt', 'b1\nb2\n'), ('write', 'c.txt', 'c1\nc2\n'), ('write', 'specs.txt', 'a.txt\x00b.txt\x00'), ['stash', 'push', '--pathspec-from-file=specs.txt', '--pathspec-file-nul']], ['status', '--porcelain']),
    ('error --pathspec-from-file with command-line pathspecs', [('write', 'a.txt', 'a1\n'), ['add', 'a.txt'], ['commit', '-m', 'init'], ('write', 'a.txt', 'a1\na2\n'), ('write', 'specs.txt', 'a.txt\n')], ['stash', 'push', '--pathspec-from-file=specs.txt', '--', 'a.txt']),
    ('error --pathspec-file-nul without --pathspec-from-file', [('write', 'a.txt', 'a1\n'), ['add', 'a.txt'], ['commit', '-m', 'init'], ('write', 'a.txt', 'a1\na2\n')], ['stash', 'push', '--pathspec-file-nul', '--', 'a.txt']),
    ('error --pathspec-from-file with --staged', [('write', 'a.txt', 'a1\n'), ['add', 'a.txt'], ['commit', '-m', 'init'], ('write', 'a.txt', 'a1\na2\n'), ('write', 'specs.txt', 'a.txt\n')], ['stash', 'push', '--pathspec-from-file=specs.txt', '--staged']),
    ('error missing pathspec file', [('write', 'a.txt', 'a1\n'), ['add', 'a.txt'], ['commit', '-m', 'init'], ('write', 'a.txt', 'a1\na2\n')], ['stash', 'push', '--pathspec-from-file=nofile.txt']),
    ('stash push pathspec with -m message', [('write', 'a.txt', 'a1\n'), ('write', 'b.txt', 'b1\n'), ['add', 'a.txt', 'b.txt'], ['commit', '-m', 'init'], ('write', 'a.txt', 'a1\na2\n'), ('write', 'b.txt', 'b1\nb2\n'), ['stash', 'push', '-m', 'my msg', '--', 'a.txt']], ['log', '--format=%s', '-1', 'refs/stash']),
    ('stash push -u pathspec deletes matched untracked', [('write', 'a.txt', 'a1\n'), ['add', 'a.txt'], ['commit', '-m', 'init'], ('write', 'a.txt', 'a1\na2\n'), ('write', 'untr.txt', 'u1\n'), ['stash', 'push', '-u', '--', 'untr.txt']], ['status', '--porcelain']),
    ('stash push pathspec deleted-in-worktree restores HEAD', [('write', 'a.txt', 'a1\n'), ('write', 'b.txt', 'b1\n'), ['add', 'a.txt', 'b.txt'], ['commit', '-m', 'init'], ('rm', 'a.txt'), ('write', 'b.txt', 'b1\nb2\n'), ['stash', 'push', '--', 'a.txt']], ['status', '--porcelain']),
    ('stash push pathspec newly-staged removed on reset', [('write', 'a.txt', 'a1\n'), ['add', 'a.txt'], ['commit', '-m', 'init'], ('write', 'new.txt', 'n1\n'), ['add', 'new.txt'], ('write', 'a.txt', 'a1\na2\n'), ['stash', 'push', '--', 'new.txt']], ['status', '--porcelain']),
    ('stash push exclude magic only-exclude matches all but excluded', [('write', 'a.txt', 'a1\n'), ('write', 'b.txt', 'b1\n'), ['add', 'a.txt', 'b.txt'], ['commit', '-m', 'init'], ('write', 'a.txt', 'a1\na2\n'), ('write', 'b.txt', 'b1\nb2\n'), ['stash', 'push', '--', ':!b.txt']], ['status', '--porcelain']),
    ('stash push :/ top magic matches single file from root', [('write', 'a.txt', 'a1\n'), ('write', 'b.txt', 'b1\n'), ['add', 'a.txt', 'b.txt'], ['commit', '-m', 'init'], ('write', 'a.txt', 'a1\na2\n'), ('write', 'b.txt', 'b1\nb2\n'), ['stash', 'push', '--', ':/a.txt']], ['status', '--porcelain']),
    ('error invalid pathspec magic', [('write', 'a.txt', 'a1\n'), ['add', 'a.txt'], ['commit', '-m', 'init'], ('write', 'a.txt', 'a1\na2\n')], ['stash', 'push', '--', ':(bogus)x']),
    ('no-HEAD pathspec nomatch reports path error', [('write', 'a.txt', 'a1\n')], ['stash', 'push', '--', 'a.txt']),
    ('no-HEAD pathspec match reports initial commit', [('write', 'a.txt', 'a1\n'), ['add', 'a.txt']], ['stash', 'push', '--', 'a.txt']),
    ('--staged + pathspec reverse-apply failure', [('write', 'a.txt', 'a1\n'), ['add', 'a.txt'], ['commit', '-m', 'init'], ('write', 'a.txt', 'a1\nstaged\n'), ['add', 'a.txt'], ('write', 'a.txt', 'a1\nstaged\nwork\n')], ['stash', 'push', '--staged', '--', 'a.txt']),
    ('stash push :(glob) does not cross slash', [('write', 'a.txt', 'a1\n'), ('write', 'd/x.txt', 'x1\n'), ['add', '-A'], ['commit', '-m', 'init'], ('write', 'a.txt', 'a1\na2\n'), ('write', 'd/x.txt', 'x1\nx2\n'), ['stash', 'push', '--', ':(glob)*.txt']], ['status', '--porcelain']),
]

BATCH7_STDIN_CASES = [
    ('diff-pairs default patch', [('write', 'a.txt', 'line1\nline2\nline3\n'), ('write', 'b.txt', 'hello\nworld\n'), ['add', '-A'], ['commit', '-m', 'c1'], ('write', 'a.txt', 'line1\nline2 changed\nline3\nline4\n'), ('rm', 'b.txt'), ('write', 'c.txt', 'new content\n'), ['add', '-A'], ['commit', '-m', 'c2']], ['diff-pairs', '-z'], ''),
    ('diff-pairs requires -z (rc 129)', [('write', 'a.txt', 'x\n'), ['add', '-A'], ['commit', '-m', 'c1'], ('write', 'a.txt', 'y\n'), ['add', '-A'], ['commit', '-m', 'c2']], ['diff-pairs'], ''),
    ('diff-pairs -h usage rc 129', [], ['diff-pairs', '-h'], ''),
    ('diff-pairs raw full oids', [('write', 'f.txt', 'a\nb\n'), ['add', '-A'], ['commit', '-m', 'c1'], ('write', 'f.txt', 'a\nB\nc\n'), ['add', '-A'], ['commit', '-m', 'c2']], ['diff-pairs', '-z', '--raw'], ''),
    ('diff-pairs name-status', [('write', 'f.txt', 'a\n'), ('write', 'g.txt', 'g\n'), ['add', '-A'], ['commit', '-m', 'c1'], ('rm', 'g.txt'), ('write', 'f.txt', 'a\nb\n'), ('write', 'h.txt', 'h\n'), ['add', '-A'], ['commit', '-m', 'c2']], ['diff-pairs', '-z', '--name-status'], ''),
    ('diff-pairs stat with binary', [('write', 't.txt', '1\n2\n3\n'), ('write', 'bin.dat', '\x00\x01\x02'), ['add', '-A'], ['commit', '-m', 'c1'], ('write', 't.txt', '1\n2\n3\n4\n'), ('write', 'bin.dat', '\x00\x01\x02\x03\x04'), ['add', '-A'], ['commit', '-m', 'c2']], ['diff-pairs', '-z', '--stat'], ''),
    ('diff-pairs numstat', [('write', 'f.txt', 'a\nb\nc\n'), ['add', '-A'], ['commit', '-m', 'c1'], ('write', 'f.txt', 'a\nX\nc\nd\n'), ['add', '-A'], ['commit', '-m', 'c2']], ['diff-pairs', '-z', '--numstat'], ''),
    ('diff-pairs -R reverse patch', [('write', 'f.txt', 'a\nb\n'), ('write', 'del.txt', 'd1\nd2\n'), ['add', '-A'], ['commit', '-m', 'c1'], ('rm', 'del.txt'), ('write', 'f.txt', 'a\nb\nc\n'), ('write', 'add.txt', 'new\n'), ['add', '-A'], ['commit', '-m', 'c2']], ['diff-pairs', '-z', '-R'], ''),
    ('diff-pairs -R raw keeps input status', [('write', 'add.txt', 'x\n'), ['add', '-A'], ['commit', '-m', 'base'], ('write', 'add2.txt', 'added\n'), ['add', '-A'], ['commit', '-m', 'c2']], ['diff-pairs', '-z', '-R', '--raw'], ''),
    ('diff-pairs whitespace -w', [('write', 'w.txt', 'foo  \nbar\nbaz\n'), ['add', '-A'], ['commit', '-m', 'c1'], ('write', 'w.txt', 'foo\nbar   \nbaz\nqux\n'), ['add', '-A'], ['commit', '-m', 'c2']], ['diff-pairs', '-z', '-w'], ''),
    ('diff-pairs ignore-cr-at-eol drops all-ignored', [('write', 'crlf.txt', 'a\r\nb\r\nc\r\n'), ('write', 'keep.txt', 'p\nq\n'), ['add', '-A'], ['commit', '-m', 'c1'], ('write', 'crlf.txt', 'a\nb\nc\n'), ('write', 'keep.txt', 'p\nq\nr\n'), ['add', '-A'], ['commit', '-m', 'c2']], ['diff-pairs', '-z', '--ignore-cr-at-eol', '--numstat'], ''),
    ('diff-pairs --binary', [('write', 'bin.dat', '\x00\x01\x02bin\x00\x03'), ['add', '-A'], ['commit', '-m', 'c1'], ('write', 'bin.dat', '\x00\x01\x02BIN\x00\x03changed'), ['add', '-A'], ['commit', '-m', 'c2']], ['diff-pairs', '-z', '--binary'], ''),
    ('diff-pairs no-prefix', [('write', 'f.txt', 'a\n'), ['add', '-A'], ['commit', '-m', 'c1'], ('write', 'f.txt', 'a\nb\n'), ['add', '-A'], ['commit', '-m', 'c2']], ['diff-pairs', '-z', '--no-prefix'], ''),
    ('diff-pairs src/dst prefix', [('write', 'f.txt', 'a\n'), ['add', '-A'], ['commit', '-m', 'c1'], ('write', 'f.txt', 'a\nb\n'), ['add', '-A'], ['commit', '-m', 'c2']], ['diff-pairs', '-z', '--src-prefix=S/', '--dst-prefix=D/'], ''),
    ('diff-pairs line-prefix', [('write', 'f.txt', 'a\n'), ['add', '-A'], ['commit', '-m', 'c1'], ('write', 'f.txt', 'a\nb\n'), ['add', '-A'], ['commit', '-m', 'c2']], ['diff-pairs', '-z', '--line-prefix=XX_'], ''),
    ('diff-pairs output indicators', [('write', 'f.txt', 'a\nb\nc\n'), ['add', '-A'], ['commit', '-m', 'c1'], ('write', 'f.txt', 'a\nB\nc\n'), ['add', '-A'], ['commit', '-m', 'c2']], ['diff-pairs', '-z', '--output-indicator-new=!', '--output-indicator-old=?', '--output-indicator-context=.'], ''),
    ('diff-pairs -M rename detect', [('write', 'orig.txt', 'one\ntwo\nthree\nfour\nfive\nsix\n'), ['add', '-A'], ['commit', '-m', 'c1'], ['mv', 'orig.txt', 'renamed.txt'], ['commit', '-am', 'c2']], ['diff-pairs', '-z', '-M', '--name-status'], ''),
    ('diff-pairs -D irreversible delete', [('write', 'del.txt', 'aaa\nbbb\nccc\n'), ('write', 'keep.txt', 'p\nq\n'), ['add', '-A'], ['commit', '-m', 'c1'], ('rm', 'del.txt'), ('write', 'keep.txt', 'p\nq\nr\n'), ['add', '-A'], ['commit', '-m', 'c2']], ['diff-pairs', '-z', '-D'], ''),
    ('diff-pairs diff-filter D', [('write', 'del.txt', 'x\ny\n'), ('write', 'mod.txt', 'a\n'), ['add', '-A'], ['commit', '-m', 'c1'], ('rm', 'del.txt'), ('write', 'mod.txt', 'a\nb\n'), ['add', '-A'], ['commit', '-m', 'c2']], ['diff-pairs', '-z', '--diff-filter=D'], ''),
    ('diff-pairs pickaxe -S', [('write', 'f.txt', 'needle here\nfoo\n'), ('write', 'g.txt', 'plain\n'), ['add', '-A'], ['commit', '-m', 'c1'], ('write', 'f.txt', 'no it\nfoo\nneedle twice needle\n'), ('write', 'g.txt', 'plain\nmore\n'), ['add', '-A'], ['commit', '-m', 'c2']], ['diff-pairs', '-z', '-Sneedle', '--name-only'], ''),
    ('diff-pairs find-object', [('write', 'a.txt', 'aa\n'), ('write', 'b.txt', 'bb\n'), ['add', '-A'], ['commit', '-m', 'c1'], ('write', 'a.txt', 'aa2\n'), ('write', 'b.txt', 'bb2\n'), ['add', '-A'], ['commit', '-m', 'c2']], ['diff-pairs', '-z', '--name-only'], ''),
    ('diff-pairs exclusivity name-only+name-status rc 128', [('write', 'f.txt', 'a\n'), ['add', '-A'], ['commit', '-m', 'c1'], ('write', 'f.txt', 'b\n'), ['add', '-A'], ['commit', '-m', 'c2']], ['diff-pairs', '-z', '--name-only', '--name-status'], ''),
    ('diff-pairs invalid raw input rc 128', [], ['diff-pairs', '-z'], 'garbage\x00'),
    ('diff-pairs unknown status rc 128', [], ['diff-pairs', '-z'], ':100644 100644 0000000000000000000000000000000000000000 0000000000000000000000000000000000000000 Z\x00p\x00'),
    ('diff-pairs --check rc 2', [('write', 'ws.txt', 'clean\n'), ['add', '-A'], ['commit', '-m', 'c1'], ('write', 'ws.txt', 'clean\ntrailing   \n\tspace before tab\n'), ['add', '-A'], ['commit', '-m', 'c2']], ['diff-pairs', '-z', '--check'], ''),
    ('diff-pairs word-diff plain', [('write', 'f.txt', 'the quick brown fox\n'), ['add', '-A'], ['commit', '-m', 'c1'], ('write', 'f.txt', 'the slow brown cat\n'), ['add', '-A'], ['commit', '-m', 'c2']], ['diff-pairs', '-z', '--word-diff'], ''),
    ('diff-pairs summary create+delete+mode', [('write', 'old.txt', 'x\n'), ['add', '-A'], ['commit', '-m', 'c1'], ('rm', 'old.txt'), ('write', 'new.txt', 'y\n'), ['add', '-A'], ['commit', '-m', 'c2']], ['diff-pairs', '-z', '--summary'], ''),
    ('diff-pairs multi-format numstat+stat order', [('write', 'f.txt', 'a\nb\nc\n'), ['add', '-A'], ['commit', '-m', 'c1'], ('write', 'f.txt', 'a\nX\nc\nd\n'), ['add', '-A'], ['commit', '-m', 'c2']], ['diff-pairs', '-z', '--numstat', '--stat'], ''),
    ('stash push --pathspec-from-file=- stdin', [('write', 'a.txt', 'a1\n'), ('write', 'b.txt', 'b1\n'), ['add', 'a.txt', 'b.txt'], ['commit', '-m', 'init'], ('write', 'a.txt', 'a1\na2\n'), ('write', 'b.txt', 'b1\nb2\n')], ['stash', 'push', '--pathspec-from-file=-'], 'a.txt\n'),
]

@pytest.mark.parametrize("case", BATCH7_CASES, ids=[c[0] for c in BATCH7_CASES])
def test_batch7_parity(case, tmp_path: Path, git_254_oracle: str):
    _id, setup, probe = case
    assert_command_parity(git_254_oracle, tmp_path, setup, probe)


@pytest.mark.parametrize("case", BATCH7_STDIN_CASES, ids=[c[0] for c in BATCH7_STDIN_CASES])
def test_batch7_stdin_parity(case, tmp_path: Path, git_254_oracle: str):
    _id, setup, probe, stdin = case
    assert_command_parity(git_254_oracle, tmp_path, setup, probe, stdin=stdin)


BATCH8_CASES = [
    ('init reftable byte-exact layout', [], ['init', '--ref-format=reftable', 'r']),
    ('init-db reftable', [], ['init-db', '--ref-format=reftable', 'r']),
    ('init reftable bare', [], ['init', '--bare', '--ref-format=reftable', 'r']),
    ('init reftable sha256', [], ['init', '--object-format=sha256', '--ref-format=reftable', 'r']),
    ('init unknown ref-format', [], ['init', '--ref-format=bogus', 'r']),
    ('commit then show-ref on reftable', [('write', 'a.txt', 'hello\n'), ['add', 'a.txt'], ['commit', '-m', 'first commit']], ['show-ref']),
    ('commit then for-each-ref on reftable', [('write', 'a.txt', 'hello\n'), ['add', 'a.txt'], ['commit', '-m', 'first commit']], ['for-each-ref']),
    ('branch list on reftable', [('write', 'a.txt', 'x\n'), ['add', 'a.txt'], ['commit', '-m', 'c1'], ['branch', 'feature']], ['branch']),
    ('branch -a on reftable', [('write', 'a.txt', 'x\n'), ['add', 'a.txt'], ['commit', '-m', 'c1'], ['branch', 'feature']], ['branch', '-a']),
    ('lightweight tag list', [('write', 'a.txt', 'x\n'), ['add', 'a.txt'], ['commit', '-m', 'c1'], ['tag', 'v1']], ['tag']),
    ('annotated tag for-each-ref objecttype', [('write', 'a.txt', 'x\n'), ['add', 'a.txt'], ['commit', '-m', 'c1'], ['tag', '-a', '-m', 'msg', 'v2']], ['for-each-ref', '--format=%(objecttype) %(refname)']),
    ('show-ref -d peels annotated tag', [('write', 'a.txt', 'x\n'), ['add', 'a.txt'], ['commit', '-m', 'c1'], ['tag', '-a', '-m', 'msg', 'v2']], ['show-ref', '-d']),
    ('rev-parse HEAD on reftable', [('write', 'a.txt', 'x\n'), ['add', 'a.txt'], ['commit', '-m', 'c1']], ['rev-parse', 'HEAD']),
    ('symbolic-ref HEAD on reftable', [('write', 'a.txt', 'x\n'), ['add', 'a.txt'], ['commit', '-m', 'c1']], ['symbolic-ref', 'HEAD']),
    ('symbolic-ref --short HEAD', [('write', 'a.txt', 'x\n'), ['add', 'a.txt'], ['commit', '-m', 'c1']], ['symbolic-ref', '--short', 'HEAD']),
    ('log oneline on reftable', [('write', 'a.txt', 'x\n'), ['add', 'a.txt'], ['commit', '-m', 'c1']], ['log', '--oneline']),
    ('checkout -b then symbolic-ref', [('write', 'a.txt', 'x\n'), ['add', 'a.txt'], ['commit', '-m', 'c1'], ['checkout', '-b', 'newbranch']], ['symbolic-ref', 'HEAD']),
    ('branch -D delete then show-ref --heads', [('write', 'a.txt', 'x\n'), ['add', 'a.txt'], ['commit', '-m', 'c1'], ['branch', 'b2'], ['branch', '-D', 'b2']], ['show-ref', '--heads']),
    ('tag -d delete then show-ref', [('write', 'a.txt', 'x\n'), ['add', 'a.txt'], ['commit', '-m', 'c1'], ['tag', 't1'], ['tag', '-d', 't1']], ['show-ref']),
    ('branch -m rename then show-ref --heads', [('write', 'a.txt', 'x\n'), ['add', 'a.txt'], ['commit', '-m', 'c1'], ['branch', 'old'], ['branch', '-m', 'old', 'newn']], ['show-ref', '--heads']),
    ('rename current branch then symbolic-ref', [('write', 'a.txt', 'x\n'), ['add', 'a.txt'], ['commit', '-m', 'c1'], ['branch', '-m', 'main', 'trunk']], ['symbolic-ref', 'HEAD']),
    ('update-ref create then show-ref --heads', [('write', 'a.txt', 'x\n'), ['add', 'a.txt'], ['commit', '-m', 'c1'], ['update-ref', 'refs/heads/manual', 'HEAD']], ['show-ref', '--heads']),
    ('update-ref -d delete', [('write', 'a.txt', 'x\n'), ['add', 'a.txt'], ['commit', '-m', 'c1'], ['branch', 'z'], ['update-ref', '-d', 'refs/heads/z']], ['show-ref', '--heads']),
    ('symbolic-ref set non-HEAD then rev-parse', [('write', 'a.txt', 'x\n'), ['add', 'a.txt'], ['commit', '-m', 'c1'], ['symbolic-ref', 'refs/heads/sym', 'refs/heads/main']], ['rev-parse', 'sym']),
    ('symbolic-ref -d delete', [('write', 'a.txt', 'x\n'), ['add', 'a.txt'], ['commit', '-m', 'c1'], ['symbolic-ref', 'refs/heads/sym', 'refs/heads/main'], ['symbolic-ref', '-d', 'refs/heads/sym']], ['show-ref', '--heads']),
    ('reflog after commit', [('write', 'a.txt', 'x\n'), ['add', 'a.txt'], ['commit', '-m', 'c1']], ['reflog']),
    ('reflog show main', [('write', 'a.txt', 'x\n'), ['add', 'a.txt'], ['commit', '-m', 'c1'], ('write', 'a.txt', 'y\n'), ['add', 'a.txt'], ['commit', '-m', 'c2']], ['reflog', 'show', 'main']),
    ('log -g HEAD format', [('write', 'a.txt', 'x\n'), ['add', 'a.txt'], ['commit', '-m', 'c1']], ['log', '-g', '--format=%gd %gs']),
    ('reflog after checkout -b', [('write', 'a.txt', 'x\n'), ['add', 'a.txt'], ['commit', '-m', 'c1'], ['checkout', '-b', 'dev']], ['reflog']),
    ('rename branch reflog carried over', [('write', 'a.txt', 'x\n'), ['add', 'a.txt'], ['commit', '-m', 'c1'], ['branch', 'old'], ['branch', '-m', 'old', 'new']], ['reflog', 'show', 'new']),
    ('rev-parse --all on reftable', [('write', 'a.txt', 'x\n'), ['add', 'a.txt'], ['commit', '-m', 'c1'], ['branch', 'b'], ['tag', 't']], ['rev-parse', '--all']),
    ('rev-parse --branches', [('write', 'a.txt', 'x\n'), ['add', 'a.txt'], ['commit', '-m', 'c1'], ['branch', 'feat']], ['rev-parse', '--branches']),
    ('describe annotated tag', [('write', 'a.txt', 'x\n'), ['add', 'a.txt'], ['commit', '-m', 'c1'], ['tag', '-a', '-m', 'm', 'v1.0']], ['describe']),
    ('name-rev HEAD on reftable', [('write', 'a.txt', 'x\n'), ['add', 'a.txt'], ['commit', '-m', 'c1'], ('write', 'a.txt', 'y\n'), ['add', 'a.txt'], ['commit', '-m', 'c2'], ['tag', 'v1']], ['name-rev', 'HEAD']),
    ('log --all on reftable', [('write', 'a.txt', 'x\n'), ['add', 'a.txt'], ['commit', '-m', 'c1'], ['branch', 'feat']], ['log', '--all', '--oneline']),
    ('for-each-ref sorted tags geometric', [('write', 'a.txt', 'x\n'), ['add', 'a.txt'], ['commit', '-m', 'c1'], ['tag', 'a'], ['tag', 'b'], ['tag', 'c']], ['for-each-ref', 'refs/tags/']),
    ('show-ref --verify missing ref', [('write', 'a.txt', 'x\n'), ['add', 'a.txt'], ['commit', '-m', 'c1']], ['show-ref', '--verify', 'refs/heads/nope']),
    ('show-ref --exists', [('write', 'a.txt', 'x\n'), ['add', 'a.txt'], ['commit', '-m', 'c1']], ['show-ref', '--exists', 'refs/heads/main']),
    ('clean filter tr upper', [('write', '.gitattributes', '*.txt filter=up\n'), ['config', 'filter.up.clean', 'tr a-z A-Z'], ('write', 'f.txt', 'hello world\n')], ['hash-object', '--filters', 'f.txt']),
    ('clean filter then crlf ordering', [('write', '.gitattributes', '*.txt filter=up text\n'), ['config', 'filter.up.clean', 'tr a-z A-Z'], ('write', 'f.txt', 'hello\r\nworld\r\n')], ['hash-object', '--filters', 'f.txt']),
    ('clean filter %f expansion', [('write', '.gitattributes', '*.txt filter=pf\n'), ['config', 'filter.pf.clean', 'cat; echo "PATH=%f"'], ('write', 'f.txt', 'hello\n')], ['hash-object', '--filters', 'f.txt']),
    ('clean %% and %z literal -w', [('write', '.gitattributes', '*.txt filter=pf\n'), ['config', 'filter.pf.clean', 'printf "[%f]-%%-%z"'], ('write', 'f.txt', 'X')], ['hash-object', '-w', '--filters', 'f.txt']),
    ('required clean fails die', [('write', '.gitattributes', '*.txt filter=pf\n'), ['config', 'filter.pf.clean', 'false'], ['config', 'filter.pf.required', 'true'], ('write', 'f.txt', 'hello\n')], ['hash-object', '--filters', 'f.txt']),
    ('non-required clean fails passthrough', [('write', '.gitattributes', '*.txt filter=pf\n'), ['config', 'filter.pf.clean', 'false'], ('write', 'f.txt', 'hello\n')], ['hash-object', '--filters', 'f.txt']),
    ('filter attr no driver configured', [('write', '.gitattributes', '*.txt filter=nf\n'), ('write', 'f.txt', 'hello\n')], ['hash-object', '--filters', 'f.txt']),
    ('filter attr no driver but required', [('write', '.gitattributes', '*.txt filter=nf\n'), ['config', 'filter.nf.required', 'true'], ('write', 'f.txt', 'hello\n')], ['hash-object', '--filters', 'f.txt']),
    ('process clean upper', [('write', '.gitattributes', '*.txt filter=pr\n'), ['config', 'filter.pr.process', '/homeTMC/m902216/src/pure-python-git/.venv/bin/python /tmp/procfilter.py'], ('write', 'f.txt', 'hello world\n')], ['hash-object', '--filters', 'f.txt']),
    ('process beats clean', [('write', '.gitattributes', '*.txt filter=pr\n'), ['config', 'filter.pr.process', '/homeTMC/m902216/src/pure-python-git/.venv/bin/python /tmp/procfilter.py'], ['config', 'filter.pr.clean', 'tr A-Z a-z'], ('write', 'f.txt', 'Hello\n')], ['hash-object', '--filters', 'f.txt']),
    ('process status=error non-required passthrough', [('write', '.gitattributes', '*.txt filter=pr\n'), ['config', 'filter.pr.process', '/homeTMC/m902216/src/pure-python-git/.venv/bin/python /tmp/procfilter_err.py'], ('write', 'f.txt', 'hello\n')], ['hash-object', '--filters', 'f.txt']),
    ('process status=error required die', [('write', '.gitattributes', '*.txt filter=pr\n'), ['config', 'filter.pr.process', '/homeTMC/m902216/src/pure-python-git/.venv/bin/python /tmp/procfilter_err.py'], ['config', 'filter.pr.required', 'true'], ('write', 'f.txt', 'hello\n')], ['hash-object', '--filters', 'f.txt']),
    ('wte UTF-16 with BOM -w', [('write', '.gitattributes', '*.txt working-tree-encoding=UTF-16\n'), ['init-shell', "iconv -f UTF-8 -t UTF-16 to produce f.txt from 'héllo\\n'"]], ['hash-object', '-w', '--filters', 'f.txt']),
    ('wte UTF-16LE prohibited BOM -w die', [('write', '.gitattributes', '*.txt working-tree-encoding=UTF-16LE\n'), ['init-shell', "write f.txt = iconv UTF-8->UTF-16 of 'héllo\\n' (has BOM)"]], ['hash-object', '-w', '--filters', 'f.txt']),
    ('wte UTF-16 missing BOM -w die', [('write', '.gitattributes', '*.txt working-tree-encoding=UTF-16\n'), ['init-shell', "write f.txt = iconv UTF-8->UTF-16BE of 'héllo\\n' (no BOM)"]], ['hash-object', '-w', '--filters', 'f.txt']),
    ('wte bogus encoding -w die', [('write', '.gitattributes', '*.txt working-tree-encoding=NOSUCHENC\n'), ('write', 'f.txt', 'hello\n')], ['hash-object', '-w', '--filters', 'f.txt']),
    ('wte invalid content for enc no-w passthrough', [('write', '.gitattributes', '*.txt working-tree-encoding=UTF-16LE\n'), ('write', 'f.txt', 'abc')], ['hash-object', '--filters', 'f.txt']),
    ('wte UTF-8 no-op', [('write', '.gitattributes', '*.txt working-tree-encoding=UTF-8\n'), ('write', 'f.txt', 'hello\n')], ['hash-object', '--filters', 'f.txt']),
    ('wte SHIFT-JIS roundtrip -w', [('write', '.gitattributes', '*.txt working-tree-encoding=SHIFT-JIS\n'), ['init-shell', 'write f.txt = bytes 82 a0 0a (SJIS hiragana + LF)']], ['hash-object', '-w', '--filters', 'f.txt']),
    ('wte true value die', [('write', '.gitattributes', '*.txt working-tree-encoding\n'), ('write', 'f.txt', 'hello\n')], ['hash-object', '--filters', 'f.txt']),
    ('combined filter+UTF-16+text', [('write', '.gitattributes', '*.txt filter=up working-tree-encoding=UTF-16 text\n'), ['config', 'filter.up.clean', 'tr a-z A-Z'], ['init-shell', "write f.txt = iconv UTF-8->UTF-16 of 'hi there\\n'"]], ['hash-object', '--filters', 'f.txt']),
    ('fast-export --anonymize on branches/paths/messages/tags', [('write', 'file.txt', 'hello\n'), ('write', 'dir/sub.txt', 'nested\n'), ['add', '-A'], ['commit', '-m', 'first commit'], ('write', 'file.txt', 'hello world\n'), ['add', '-A'], ['commit', '-m', 'second commit\n\nwith body'], ['branch', 'topic'], ['checkout', 'topic'], ('write', 'dir/sub.txt', 'topic change\n'), ['add', '-A'], ['commit', '-m', 'topic commit'], ['checkout', 'main'], ['tag', '-a', '-m', 'tag msg', 'v1.0']], ['fast-export', '--all', '--anonymize']),
    ('fast-export --anonymize-map repeatable (ref + path)', [('write', 'file.txt', 'hello\n'), ('write', 'dir/sub.txt', 'nested\n'), ['add', '-A'], ['commit', '-m', 'first commit'], ['branch', 'topic'], ['tag', '-a', '-m', 'tag msg', 'v1.0']], ['fast-export', '--all', '--anonymize', '--anonymize-map=main:trunk', '--anonymize-map=file.txt:secret.txt']),
    ('fast-export --anonymize-map orig-alone keeps token and counter', [('write', 'f', 'x\n'), ['add', '-A'], ['commit', '-m', 'c1'], ['branch', 'topic'], ['tag', '-a', '-m', 'tm', 'v1.0']], ['fast-export', '--all', '--anonymize', '--anonymize-map=main']),
    ('fast-export --anonymize-map without --anonymize errors rc128', [('write', 'f', 'x\n'), ['add', '-A'], ['commit', '-m', 'c1']], ['fast-export', '--all', '--anonymize-map=main:trunk']),
    ('fast-export --anonymize-map empty key errors rc129', [('write', 'f', 'x\n'), ['add', '-A'], ['commit', '-m', 'c1']], ['fast-export', '--all', '--anonymize', '--anonymize-map=:foo']),
    ('fast-export --anonymize-map empty value errors rc129', [('write', 'f', 'x\n'), ['add', '-A'], ['commit', '-m', 'c1']], ['fast-export', '--all', '--anonymize', '--anonymize-map=foo:']),
    ('fast-export --anonymize two distinct authors (committer-first ident counter)', [('write', 'a.txt', 'a\n'), ['add', '-A'], ['-c', 'user.name=Alice', '-c', 'user.email=alice@x.com', 'commit', '-m', 'c1'], ('write', 'a.txt', 'b\n'), ['add', '-A'], ['-c', 'user.name=Bob', '-c', 'user.email=bob@y.com', 'commit', '-m', 'c2']], ['fast-export', '--all', '--anonymize']),
    ('fast-export --anonymize with merge topology', [('write', 'top.txt', 'top\n'), ('write', 'deep/a/x.txt', '1\n'), ('write', 'deep/b/y.txt', '2\n'), ['add', '-A'], ['commit', '-m', 'c1'], ['branch', 'feature'], ('write', 'deep/a/x.txt', '1m\n'), ['add', '-A'], ['commit', '-m', 'c2-main'], ['checkout', 'feature'], ('rm', 'top.txt'), ('write', 'deep/b/y.txt', '2f\n'), ['add', '-A'], ['commit', '-m', 'c3-feature'], ['checkout', 'main'], ['merge', '--no-edit', 'feature']], ['fast-export', '--all', '--anonymize']),
    ('fast-export --anonymize --no-data (fake-oid M-lines)', [('write', 'top.txt', 'top\n'), ('write', 'deep/a/x.txt', '1\n'), ['add', '-A'], ['commit', '-m', 'c1'], ('write', 'deep/a/x.txt', '2\n'), ['add', '-A'], ['commit', '-m', 'c2']], ['fast-export', '--all', '--anonymize', '--no-data']),
    ('fast-export --anonymize nested annotated tags with --mark-tags', [('write', 'f', 'x\n'), ['add', '-A'], ['commit', '-m', 'c1'], ['tag', '-a', '-m', 'inner tag', 'inner'], ['tag', '-a', '-m', 'outer tag', 'outer', 'inner']], ['fast-export', '--all', '--mark-tags', '--anonymize']),
    ('fast-export --anonymize-map on tag/branch/path components together', [('write', 'shared/a/f1', '1\n'), ('write', 'other/f3', '3\n'), ['add', '-A'], ['commit', '-m', 'msg one'], ['branch', 'feature/awesome'], ['tag', '-a', '-m', 'ann', 'annotated/v1']], ['fast-export', '--all', '--anonymize', '--anonymize-map=shared:S', '--anonymize-map=feature:FE', '--anonymize-map=awesome:AW']),
    ('remote tar to stdout', [('write', 'f.txt', 'hello\n'), ('write', 'sub/g.txt', 'world\n'), ['add', '-A'], ['commit', '-m', 'initial']], ['archive', '--remote=.', '--format=tar', 'HEAD']),
    ('remote default format (tar) to stdout', [('write', 'f.txt', 'hello\n'), ('write', 'sub/g.txt', 'world\n'), ['add', '-A'], ['commit', '-m', 'initial']], ['archive', '--remote=.', 'HEAD']),
    ('remote tar with prefix', [('write', 'f.txt', 'hello\n'), ('write', 'sub/g.txt', 'world\n'), ['add', '-A'], ['commit', '-m', 'initial']], ['archive', '--remote=.', '--format=tar', '--prefix=p/', 'HEAD']),
    ('remote tar verbose (stderr relay)', [('write', 'f.txt', 'hello\n'), ('write', 'sub/g.txt', 'world\n'), ['add', '-A'], ['commit', '-m', 'initial']], ['archive', '--remote=.', '-v', '--format=tar', 'HEAD']),
    ('remote list formats', [('write', 'f.txt', 'hello\n'), ['add', '-A'], ['commit', '-m', 'initial']], ['archive', '--remote=.', '--list']),
    ('remote explicit default exec', [('write', 'f.txt', 'hello\n'), ('write', 'sub/g.txt', 'world\n'), ['add', '-A'], ['commit', '-m', 'initial']], ['archive', '--remote=.', '--exec=git-upload-archive', '--format=tar', 'HEAD']),
    ('remote complex tree (symlink/exec/binary/nested)', [('write', 'a.txt', 'alpha\n'), ('write', 'd1/d2/deep.txt', 'deep\n'), ('write', 'bin.dat', 'binary\x00\x01\x02data'), ['add', '-A'], ['commit', '-m', 'complex']], ['archive', '--remote=.', '--format=tar', 'HEAD']),
    ('upload-archive -h usage', [], ['upload-archive', '-h']),
    ('diagnose-stats-empty', [], ['diagnose', '-s', 'fixt']),
    ('diagnose-mode-all', [], ['diagnose', '--mode=all', '-s', 'fixt']),
    ('diagnose-invalid-mode', [], ['diagnose', '--mode=bogus', '-s', 'foo']),
    ('diagnose-mode-none', [], ['diagnose', '--mode=none', '-s', 'foo']),
    ('diagnose-mode-no-value', [], ['diagnose', '--mode']),
    ('diagnose-no-mode', [], ['diagnose', '--no-mode', '-s', 'foo']),
    ('diagnose-help-short', [], ['diagnose', '-h']),
    ('diagnose-unknown-long', [], ['diagnose', '--foo']),
    ('diagnose-output-dir', [], ['diagnose', '-o', 'out/sub', '-s', 'fixt']),
    ('diagnose-suffix-fixed', [], ['diagnose', '-s', 'FIXED']),
    ('bugreport-diagnose-stats', [], ['bugreport', '--diagnose', '-s', 'fixt']),
    ('bugreport-diagnose-all', [], ['bugreport', '--diagnose=all', '-s', 'fixt']),
    ('bugreport-diagnose-invalid', [], ['bugreport', '--diagnose=bogus']),
    ('bugreport-no-diagnose', [], ['bugreport', '-s', 'fixt']),
    ('bugreport-help-short', [], ['bugreport', '-h']),
]

BATCH8_STDIN_CASES = [
]

@pytest.mark.parametrize("case", BATCH8_CASES, ids=[c[0] for c in BATCH8_CASES])
def test_batch8_parity(case, tmp_path: Path, git_254_oracle: str):
    _id, setup, probe = case
    assert_command_parity(git_254_oracle, tmp_path, setup, probe)


@pytest.mark.parametrize("case", BATCH8_STDIN_CASES, ids=[c[0] for c in BATCH8_STDIN_CASES])
def test_batch8_stdin_parity(case, tmp_path: Path, git_254_oracle: str):
    _id, setup, probe, stdin = case
    assert_command_parity(git_254_oracle, tmp_path, setup, probe, stdin=stdin)


# Binary archive output (gzip: --format=tgz/tar.gz) cannot go through the text=True
# command-parity harness — gzip's 0x8b byte is invalid UTF-8. git writes its gzip
# header with a fixed mtime=0, so the stream is deterministic and byte-comparable;
# these compare raw stdout BYTES (rc + stderr + stdout) against the oracle.
BATCH8_ARCHIVE_BIN_CASES = [
    ("remote-tgz-bytes",
     [("write", "f.txt", "hello\n"), ("write", "sub/g.txt", "world\n"), ["add", "-A"], ["commit", "-m", "initial"]],
     ["archive", "--remote=.", "--format=tgz", "HEAD"]),
    ("remote-tar-gz-bytes",
     [("write", "f.txt", "hello\n"), ("write", "sub/g.txt", "world\n"), ["add", "-A"], ["commit", "-m", "initial"]],
     ["archive", "--remote=.", "--format=tar.gz", "HEAD"]),
    ("local-tgz-bytes",
     [("write", "f.txt", "hello\n"), ("write", "sub/g.txt", "world\n"), ["add", "-A"], ["commit", "-m", "initial"]],
     ["archive", "--format=tgz", "HEAD"]),
]


@pytest.mark.parametrize("case", BATCH8_ARCHIVE_BIN_CASES, ids=[c[0] for c in BATCH8_ARCHIVE_BIN_CASES])
def test_batch8_archive_binary_parity(case, tmp_path: Path, git_254_oracle: str):
    import subprocess
    from tests.git_parity.support import DETERMINISTIC_ENV, ROOT, pygit_cmd

    _id, setup, probe = case
    env = dict(__import__("os").environ)
    env.update(DETERMINISTIC_ENV)
    env["PYTHONPATH"] = str(ROOT)

    results = {}
    for tool, base in (("oracle", [git_254_oracle]), ("pygit", pygit_cmd())):
        repo = tmp_path / tool
        repo.mkdir()
        subprocess.run([*base, "init", "-b", "main", "."], cwd=repo, env=env, capture_output=True)
        for step in setup:
            if isinstance(step, tuple) and step and step[0] == "write":
                (repo / step[1]).parent.mkdir(parents=True, exist_ok=True)
                (repo / step[1]).write_text(step[2])
            else:
                subprocess.run([*base, *step], cwd=repo, env=env, capture_output=True)
        # capture_output WITHOUT text=True -> raw bytes (gzip-safe)
        proc = subprocess.run([*base, *probe], cwd=repo, env=env, capture_output=True)
        results[tool] = (proc.returncode, proc.stdout, proc.stderr)

    assert results["pygit"] == results["oracle"]


BATCH9_CASES = [
    ('diff-files -c combined diff of a content conflict', [('write', 'f.txt', 'l1\nl2\nl3\n'), ['add', 'f.txt'], ['commit', '-m', 'base'], ['checkout', '-b', 'branchA'], ('write', 'f.txt', 'l1\nA2\nl3\n'), ['commit', '-am', 'A'], ['checkout', 'main'], ['checkout', '-b', 'branchB'], ('write', 'f.txt', 'l1\nB2\nl3\n'), ['commit', '-am', 'B'], ['merge', 'branchA']], ['diff-files', '-c']),
    ('diff-files --cc dense combined diff', [('write', 'f.txt', 'l1\nl2\nl3\n'), ['add', 'f.txt'], ['commit', '-m', 'base'], ['checkout', '-b', 'branchA'], ('write', 'f.txt', 'l1\nA2\nl3\n'), ['commit', '-am', 'A'], ['checkout', 'main'], ['checkout', '-b', 'branchB'], ('write', 'f.txt', 'l1\nB2\nl3\n'), ['commit', '-am', 'B'], ['merge', 'branchA']], ['diff-files', '--cc']),
    ('diff-files -c --raw combined raw line', [('write', 'f.txt', 'l1\nl2\nl3\n'), ['add', 'f.txt'], ['commit', '-m', 'base'], ['checkout', '-b', 'branchA'], ('write', 'f.txt', 'l1\nA2\nl3\n'), ['commit', '-am', 'A'], ['checkout', 'main'], ['checkout', '-b', 'branchB'], ('write', 'f.txt', 'l1\nB2\nl3\n'), ['commit', '-am', 'B'], ['merge', 'branchA']], ['diff-files', '-c', '--raw']),
    ('diff-files -c --name-status combined', [('write', 'f.txt', 'l1\nl2\nl3\n'), ['add', 'f.txt'], ['commit', '-m', 'base'], ['checkout', '-b', 'branchA'], ('write', 'f.txt', 'l1\nA2\nl3\n'), ['commit', '-am', 'A'], ['checkout', 'main'], ['checkout', '-b', 'branchB'], ('write', 'f.txt', 'l1\nB2\nl3\n'), ['commit', '-am', 'B'], ['merge', 'branchA']], ['diff-files', '-c', '--name-status']),
    ('diff-files plain raw on unmerged (U line + comparison)', [('write', 'f.txt', 'l1\nl2\nl3\n'), ['add', 'f.txt'], ['commit', '-m', 'base'], ['checkout', '-b', 'branchA'], ('write', 'f.txt', 'l1\nA2\nl3\n'), ['commit', '-am', 'A'], ['checkout', 'main'], ['checkout', '-b', 'branchB'], ('write', 'f.txt', 'l1\nB2\nl3\n'), ['commit', '-am', 'B'], ['merge', 'branchA']], ['diff-files']),
    ('diff-files -2 selects ours stage', [('write', 'f.txt', 'l1\nl2\nl3\n'), ['add', 'f.txt'], ['commit', '-m', 'base'], ['checkout', '-b', 'branchA'], ('write', 'f.txt', 'l1\nA2\nl3\n'), ['commit', '-am', 'A'], ['checkout', 'main'], ['checkout', '-b', 'branchB'], ('write', 'f.txt', 'l1\nB2\nl3\n'), ['commit', '-am', 'B'], ['merge', 'branchA']], ['diff-files', '-2']),
    ('diff-files -c multi-hunk all shown', [('write', 'm.txt', '1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n'), ['add', 'm.txt'], ['commit', '-m', 'base'], ['checkout', '-b', 'branchA'], ('write', 'm.txt', '1\nA2\n3\n4\n5\n6\n7\n8\nA9\n10\n'), ['commit', '-am', 'A'], ['checkout', 'main'], ['checkout', '-b', 'branchB'], ('write', 'm.txt', '1\nB2\n3\n4\n5\n6\n7\n8\nB9\n10\n'), ['commit', '-am', 'B'], ['merge', 'branchA'], ('write', 'm.txt', '1\nA2\n3\n4\n5\n6\n7\n8\nZZ\n10\n')], ['diff-files', '-c']),
    ('diff-files --cc multi-hunk partial elision', [('write', 'm.txt', '1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n'), ['add', 'm.txt'], ['commit', '-m', 'base'], ['checkout', '-b', 'branchA'], ('write', 'm.txt', '1\nA2\n3\n4\n5\n6\n7\n8\nA9\n10\n'), ['commit', '-am', 'A'], ['checkout', 'main'], ['checkout', '-b', 'branchB'], ('write', 'm.txt', '1\nB2\n3\n4\n5\n6\n7\n8\nB9\n10\n'), ['commit', '-am', 'B'], ['merge', 'branchA'], ('write', 'm.txt', '1\nA2\n3\n4\n5\n6\n7\n8\nZZ\n10\n')], ['diff-files', '--cc']),
    ('diff-files -c -U0 multi-hunk zero context', [('write', 'm.txt', '1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n'), ['add', 'm.txt'], ['commit', '-m', 'base'], ['checkout', '-b', 'branchA'], ('write', 'm.txt', '1\nA2\n3\n4\n5\n6\n7\n8\nA9\n10\n'), ['commit', '-am', 'A'], ['checkout', 'main'], ['checkout', '-b', 'branchB'], ('write', 'm.txt', '1\nB2\n3\n4\n5\n6\n7\n8\nB9\n10\n'), ['commit', '-am', 'B'], ['merge', 'branchA'], ('write', 'm.txt', '1\nA2\n3\n4\n5\n6\n7\n8\nZZ\n10\n')], ['diff-files', '-c', '-U0']),
    ('diff-files -c --combined-all-paths header per-parent', [('write', 'm.txt', '1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n'), ['add', 'm.txt'], ['commit', '-m', 'base'], ['checkout', '-b', 'branchA'], ('write', 'm.txt', '1\nA2\n3\n4\n5\n6\n7\n8\nA9\n10\n'), ['commit', '-am', 'A'], ['checkout', 'main'], ['checkout', '-b', 'branchB'], ('write', 'm.txt', '1\nB2\n3\n4\n5\n6\n7\n8\nB9\n10\n'), ['commit', '-am', 'B'], ['merge', 'branchA'], ('write', 'm.txt', '1\nA2\n3\n4\n5\n6\n7\n8\nZZ\n10\n')], ['diff-files', '-c', '--combined-all-paths']),
    ('diff-files -c --name-only', [('write', 'm.txt', '1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n'), ['add', 'm.txt'], ['commit', '-m', 'base'], ['checkout', '-b', 'branchA'], ('write', 'm.txt', '1\nA2\n3\n4\n5\n6\n7\n8\nA9\n10\n'), ['commit', '-am', 'A'], ['checkout', 'main'], ['checkout', '-b', 'branchB'], ('write', 'm.txt', '1\nB2\n3\n4\n5\n6\n7\n8\nB9\n10\n'), ['commit', '-am', 'B'], ['merge', 'branchA'], ('write', 'm.txt', '1\nA2\n3\n4\n5\n6\n7\n8\nZZ\n10\n')], ['diff-files', '-c', '--name-only']),
    ('diff-files -c --shortstat (combined emits nothing)', [('write', 'm.txt', '1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n'), ['add', 'm.txt'], ['commit', '-m', 'base'], ['checkout', '-b', 'branchA'], ('write', 'm.txt', '1\nA2\n3\n4\n5\n6\n7\n8\nA9\n10\n'), ['commit', '-am', 'A'], ['checkout', 'main'], ['checkout', '-b', 'branchB'], ('write', 'm.txt', '1\nB2\n3\n4\n5\n6\n7\n8\nB9\n10\n'), ['commit', '-am', 'B'], ['merge', 'branchA'], ('write', 'm.txt', '1\nA2\n3\n4\n5\n6\n7\n8\nZZ\n10\n')], ['diff-files', '-c', '--shortstat']),
    ('diff-files -c on modify/delete conflict (* Unmerged path)', [('write', 'g.txt', 'x\ny\nz\n'), ['add', 'g.txt'], ['commit', '-m', 'base'], ['checkout', '-b', 'branchA'], ('write', 'g.txt', 'x\nA\nz\n'), ['commit', '-am', 'A'], ['checkout', 'main'], ['checkout', '-b', 'branchB'], ('rm', 'g.txt'), ['commit', '-m', 'del'], ['merge', 'branchA']], ['diff-files', '-c']),
    ('diff-files --cc on modify/delete conflict', [('write', 'g.txt', 'x\ny\nz\n'), ['add', 'g.txt'], ['commit', '-m', 'base'], ['checkout', '-b', 'branchA'], ('write', 'g.txt', 'x\nA\nz\n'), ['commit', '-am', 'A'], ['checkout', 'main'], ['checkout', '-b', 'branchB'], ('rm', 'g.txt'), ['commit', '-m', 'del'], ['merge', 'branchA']], ['diff-files', '--cc']),
    ('diff-files plain on modify/delete (U raw line)', [('write', 'g.txt', 'x\ny\nz\n'), ['add', 'g.txt'], ['commit', '-m', 'base'], ['checkout', '-b', 'branchA'], ('write', 'g.txt', 'x\nA\nz\n'), ['commit', '-am', 'A'], ['checkout', 'main'], ['checkout', '-b', 'branchB'], ('rm', 'g.txt'), ['commit', '-m', 'del'], ['merge', 'branchA']], ['diff-files']),
    ('diff-files --combined-all-paths without -c errors rc=128', [('write', 'f.txt', 'l1\nl2\nl3\n'), ['add', 'f.txt'], ['commit', '-m', 'base']], ['diff-files', '--combined-all-paths']),
    ('extcmd no-prompt', [('write', 'f.txt', 'a\nb\n'), ['add', 'f.txt'], ['commit', '-m', 'init'], ('write', 'f.txt', 'a\nC\n')], ['difftool', '-y', '-x', 'sh -c \'echo R=[$(cat "$2")]\' x']),
    ('multi counter', [('write', 'a.txt', 'a1\n'), ('write', 'b.txt', 'b1\n'), ['add', '-A'], ['commit', '-m', 'init'], ('write', 'a.txt', 'a2\n'), ('write', 'b.txt', 'b2\n')], ['difftool', '-y', '-x', "sh -c 'echo C=$GIT_DIFF_PATH_COUNTER T=$GIT_DIFF_PATH_TOTAL P=$BASE' x"]),
    ('no changes', [('write', 'f.txt', 'x\n'), ['add', '-A'], ['commit', '-m', 'init']], ['difftool', '-y', '-x', 'echo X']),
    ('add del mod', [('write', 'kept.txt', 'keep\n'), ('write', 'del.txt', 'del\n'), ['add', '-A'], ['commit', '-m', 'init'], ('rm', 'del.txt'), ('write', 'kept.txt', 'keep2\n')], ['difftool', '-y', '-x', 'sh -c \'echo M=$BASE L=[$(cat "$1" 2>/dev/null)] R=[$(cat "$2" 2>/dev/null)]\' x']),
    ('empty tool', [('write', 'f.txt', 'a\n'), ['add', 'f.txt'], ['commit', '-m', 'init'], ('write', 'f.txt', 'b\n')], ['difftool', '-y', '--tool=']),
    ('empty extcmd', [('write', 'f.txt', 'a\n'), ['add', 'f.txt'], ['commit', '-m', 'init'], ('write', 'f.txt', 'b\n')], ['difftool', '-y', '--extcmd=']),
    ('unknown tool', [('write', 'f.txt', 'a\n'), ['add', 'f.txt'], ['commit', '-m', 'init'], ('write', 'f.txt', 'b\n')], ['difftool', '-y', '--tool=nonexistenttool']),
    ('merge-only', [('write', 'f.txt', 'a\n'), ['add', 'f.txt'], ['commit', '-m', 'init'], ('write', 'f.txt', 'b\n')], ['difftool', '-y', '-t', 'tortoisemerge']),
    ('gui tool conflict', [('write', 'f.txt', 'a\n'), ['add', 'f.txt'], ['commit', '-m', 'init'], ('write', 'f.txt', 'b\n')], ['difftool', '-g', '-t', 'foo', '-y']),
    ('config tool', [('write', 'f.txt', 'a\n'), ['add', 'f.txt'], ['commit', '-m', 'init'], ('write', 'f.txt', 'b\n'), ['config', 'difftool.mt.cmd', 'echo R=$REMOTE B=$BASE']], ['difftool', '-y', '-t', 'mt']),
    ('diff.tool', [('write', 'f.txt', 'a\n'), ['add', 'f.txt'], ['commit', '-m', 'init'], ('write', 'f.txt', 'b\n'), ['config', 'difftool.mt.cmd', 'echo VIA $REMOTE'], ['config', 'diff.tool', 'mt']], ['difftool', '-y']),
    ('gui guitool', [('write', 'f.txt', 'a\n'), ['add', 'f.txt'], ['commit', '-m', 'init'], ('write', 'f.txt', 'b\n'), ['config', 'difftool.gt.cmd', 'echo G $REMOTE'], ['config', 'diff.guitool', 'gt']], ['difftool', '-g', '-y']),
    ('trust died', [('write', 'f.txt', 'a\n'), ['add', 'f.txt'], ['commit', '-m', 'init'], ('write', 'f.txt', 'b\n')], ['difftool', '-y', '--trust-exit-code', '-x', 'false']),
    ('no-trust swallow', [('write', 'f.txt', 'a\n'), ['add', 'f.txt'], ['commit', '-m', 'init'], ('write', 'f.txt', 'b\n')], ['difftool', '-y', '-x', 'false']),
    ('exit127', [('write', 'f.txt', 'a\n'), ['add', 'f.txt'], ['commit', '-m', 'init'], ('write', 'f.txt', 'b\n')], ['difftool', '-y', '-x', 'sh -c "exit 127"']),
    ('bad trust with -h', [('write', 'f.txt', 'a\n'), ['add', 'f.txt'], ['commit', '-m', 'init'], ['config', 'difftool.trustexitcode', 'maybe']], ['difftool', '-h']),
    ('cached', [('write', 'f.txt', 'base\n'), ['add', '-A'], ['commit', '-m', 'init'], ('write', 'f.txt', 'mod\n'), ['add', 'f.txt']], ['difftool', '--cached', '-y', '-x', 'sh -c \'echo L=[$(cat "$1")] R=[$(cat "$2")]\' x']),
    ('cached unborn', [('write', 'n.txt', 'new\n'), ['add', 'n.txt']], ['difftool', '--cached', '-y', '-x', 'sh -c \'echo R=[$(cat "$2")]\' x']),
    ('range', [('write', 'f.txt', 'v1\n'), ['add', '-A'], ['commit', '-m', 'c1'], ('write', 'f.txt', 'v2\n'), ['add', '-A'], ['commit', '-m', 'c2']], ['difftool', '-y', 'HEAD~1', 'HEAD', '-x', 'sh -c \'echo R=[$(cat "$2")]\' x']),
    ('dir-diff false', [('write', 'f.txt', 'a\n'), ['add', 'f.txt'], ['commit', '-m', 'init'], ('write', 'f.txt', 'b\n')], ['difftool', '-d', '-y', '-x', 'false']),
    ('dir-diff notfound', [('write', 'f.txt', 'a\n'), ['add', 'f.txt'], ['commit', '-m', 'init'], ('write', 'f.txt', 'b\n')], ['difftool', '-d', '-y', '-x', 'nonexistentprog123']),
    ('dir-diff slash exec', [('write', 'f.txt', 'a\n'), ['add', 'f.txt'], ['commit', '-m', 'init'], ('write', 'f.txt', 'b\n')], ['difftool', '-d', '-y', '-x', './nope/prog']),
    ('dir-diff unknown', [('write', 'f.txt', 'a\n'), ['add', 'f.txt'], ['commit', '-m', 'init'], ('write', 'f.txt', 'b\n')], ['difftool', '-d', '-y', '-t', 'bogus99']),
    ('dir no-index conflict', [('write', 'f.txt', 'a\n'), ['add', 'f.txt'], ['commit', '-m', 'init'], ('write', 'f.txt', 'b\n')], ['difftool', '-d', '--no-index', '-y', '-x', 'diff']),
    ('attached tt', [('write', 'f.txt', 'a\n'), ['add', 'f.txt'], ['commit', '-m', 'init'], ('write', 'f.txt', 'b\n'), ['config', 'difftool.t.cmd', 'echo D $REMOTE']], ['difftool', '-tt', '-y']),
    ('dash h', [], ['difftool', '-h']),
    ('sparse with pathspec shows commits dense prunes (shared subtree)', [('write', 'shared/s.txt', 'shared content\n'), ('write', 'sub1/a.txt', 'a1\n'), ('write', 'root.txt', 'root1\n'), ['add', '-A'], ['commit', '-m', 'c1'], ('write', 'sub2/b.txt', 'b2\n'), ('write', 'root.txt', 'root2\n'), ['add', '-A'], ['commit', '-m', 'c2'], ('write', 'sub1/a.txt', 'a3\n'), ['add', '-A'], ['commit', '-m', 'c3']], ['rev-list', '--sparse', 'HEAD', '--', 'shared']),
    ('dense with pathspec prunes treesame commits (baseline, unchanged)', [('write', 'shared/s.txt', 'shared content\n'), ('write', 'sub1/a.txt', 'a1\n'), ('write', 'root.txt', 'root1\n'), ['add', '-A'], ['commit', '-m', 'c1'], ('write', 'sub2/b.txt', 'b2\n'), ('write', 'root.txt', 'root2\n'), ['add', '-A'], ['commit', '-m', 'c2'], ('write', 'sub1/a.txt', 'a3\n'), ['add', '-A'], ['commit', '-m', 'c3']], ['rev-list', 'HEAD', '--', 'shared']),
    ('sparse with pathspec sub1 keeps the intervening untouched commit', [('write', 'shared/s.txt', 'shared content\n'), ('write', 'sub1/a.txt', 'a1\n'), ('write', 'root.txt', 'root1\n'), ['add', '-A'], ['commit', '-m', 'c1'], ('write', 'sub2/b.txt', 'b2\n'), ('write', 'root.txt', 'root2\n'), ['add', '-A'], ['commit', '-m', 'c2'], ('write', 'sub1/a.txt', 'a3\n'), ['add', '-A'], ['commit', '-m', 'c3']], ['rev-list', '--sparse', 'HEAD', '--', 'sub1']),
    ('dense with pathspec sub1 (baseline, unchanged)', [('write', 'shared/s.txt', 'shared content\n'), ('write', 'sub1/a.txt', 'a1\n'), ('write', 'root.txt', 'root1\n'), ['add', '-A'], ['commit', '-m', 'c1'], ('write', 'sub2/b.txt', 'b2\n'), ('write', 'root.txt', 'root2\n'), ['add', '-A'], ['commit', '-m', 'c2'], ('write', 'sub1/a.txt', 'a3\n'), ['add', '-A'], ['commit', '-m', 'c3']], ['rev-list', 'HEAD', '--', 'sub1']),
    ('sparse --count with pathspec counts the full walk', [('write', 'shared/s.txt', 'shared content\n'), ('write', 'sub1/a.txt', 'a1\n'), ('write', 'root.txt', 'root1\n'), ['add', '-A'], ['commit', '-m', 'c1'], ('write', 'sub2/b.txt', 'b2\n'), ('write', 'root.txt', 'root2\n'), ['add', '-A'], ['commit', '-m', 'c2'], ('write', 'sub1/a.txt', 'a3\n'), ['add', '-A'], ['commit', '-m', 'c3']], ['rev-list', '--sparse', '--count', 'HEAD', '--', 'shared']),
    ('dense --count with pathspec (baseline, unchanged)', [('write', 'shared/s.txt', 'shared content\n'), ('write', 'sub1/a.txt', 'a1\n'), ('write', 'root.txt', 'root1\n'), ['add', '-A'], ['commit', '-m', 'c1'], ('write', 'sub2/b.txt', 'b2\n'), ('write', 'root.txt', 'root2\n'), ['add', '-A'], ['commit', '-m', 'c2'], ('write', 'sub1/a.txt', 'a3\n'), ['add', '-A'], ['commit', '-m', 'c3']], ['rev-list', '--count', 'HEAD', '--', 'shared']),
    ('sparse without pathspec is a no-op (equals plain walk)', [('write', 'shared/s.txt', 'shared content\n'), ('write', 'sub1/a.txt', 'a1\n'), ('write', 'root.txt', 'root1\n'), ['add', '-A'], ['commit', '-m', 'c1'], ('write', 'sub2/b.txt', 'b2\n'), ('write', 'root.txt', 'root2\n'), ['add', '-A'], ['commit', '-m', 'c2'], ('write', 'sub1/a.txt', 'a3\n'), ['add', '-A'], ['commit', '-m', 'c3']], ['rev-list', '--sparse', 'HEAD']),
    ('sparse with --objects (no pathspec) is a no-op vs --objects', [('write', 'shared/s.txt', 'shared content\n'), ('write', 'sub1/a.txt', 'a1\n'), ('write', 'root.txt', 'root1\n'), ['add', '-A'], ['commit', '-m', 'c1'], ('write', 'sub2/b.txt', 'b2\n'), ('write', 'root.txt', 'root2\n'), ['add', '-A'], ['commit', '-m', 'c2'], ('write', 'sub1/a.txt', 'a3\n'), ['add', '-A'], ['commit', '-m', 'c3']], ['rev-list', '--objects', '--sparse', 'HEAD']),
    ('sparse with --topo-order and pathspec', [('write', 'shared/s.txt', 'shared content\n'), ('write', 'sub1/a.txt', 'a1\n'), ('write', 'root.txt', 'root1\n'), ['add', '-A'], ['commit', '-m', 'c1'], ('write', 'sub2/b.txt', 'b2\n'), ('write', 'root.txt', 'root2\n'), ['add', '-A'], ['commit', '-m', 'c2'], ('write', 'sub1/a.txt', 'a3\n'), ['add', '-A'], ['commit', '-m', 'c3']], ['rev-list', '--sparse', '--topo-order', 'HEAD', '--', 'sub2']),
    ('sparse with --reverse and pathspec', [('write', 'shared/s.txt', 'shared content\n'), ('write', 'sub1/a.txt', 'a1\n'), ('write', 'root.txt', 'root1\n'), ['add', '-A'], ['commit', '-m', 'c1'], ('write', 'sub2/b.txt', 'b2\n'), ('write', 'root.txt', 'root2\n'), ['add', '-A'], ['commit', '-m', 'c2'], ('write', 'sub1/a.txt', 'a3\n'), ['add', '-A'], ['commit', '-m', 'c3']], ['rev-list', '--sparse', '--reverse', 'HEAD', '--', 'shared']),
    ('--dense then --sparse: last flag wins (sparse)', [('write', 'shared/s.txt', 'shared content\n'), ('write', 'sub1/a.txt', 'a1\n'), ('write', 'root.txt', 'root1\n'), ['add', '-A'], ['commit', '-m', 'c1'], ('write', 'sub2/b.txt', 'b2\n'), ('write', 'root.txt', 'root2\n'), ['add', '-A'], ['commit', '-m', 'c2'], ('write', 'sub1/a.txt', 'a3\n'), ['add', '-A'], ['commit', '-m', 'c3']], ['rev-list', '--dense', '--sparse', 'HEAD', '--', 'shared']),
    ('--sparse then --dense: last flag wins (dense)', [('write', 'shared/s.txt', 'shared content\n'), ('write', 'sub1/a.txt', 'a1\n'), ('write', 'root.txt', 'root1\n'), ['add', '-A'], ['commit', '-m', 'c1'], ('write', 'sub2/b.txt', 'b2\n'), ('write', 'root.txt', 'root2\n'), ['add', '-A'], ['commit', '-m', 'c2'], ('write', 'sub1/a.txt', 'a3\n'), ['add', '-A'], ['commit', '-m', 'c3']], ['rev-list', '--sparse', '--dense', 'HEAD', '--', 'shared']),
    ('sparse with multiple pathspecs', [('write', 'shared/s.txt', 'shared content\n'), ('write', 'sub1/a.txt', 'a1\n'), ('write', 'root.txt', 'root1\n'), ['add', '-A'], ['commit', '-m', 'c1'], ('write', 'sub2/b.txt', 'b2\n'), ('write', 'root.txt', 'root2\n'), ['add', '-A'], ['commit', '-m', 'c2'], ('write', 'sub1/a.txt', 'a3\n'), ['add', '-A'], ['commit', '-m', 'c3']], ['rev-list', '--sparse', 'HEAD', '--', 'shared', 'sub2']),
    ('sparse with -n max-count and pathspec', [('write', 'shared/s.txt', 'shared content\n'), ('write', 'sub1/a.txt', 'a1\n'), ('write', 'root.txt', 'root1\n'), ['add', '-A'], ['commit', '-m', 'c1'], ('write', 'sub2/b.txt', 'b2\n'), ('write', 'root.txt', 'root2\n'), ['add', '-A'], ['commit', '-m', 'c2'], ('write', 'sub1/a.txt', 'a3\n'), ['add', '-A'], ['commit', '-m', 'c3']], ['rev-list', '--sparse', '-n', '2', 'HEAD', '--', 'sub1']),
    ('sparse with --parents and pathspec (non-merge, no rewrite needed)', [('write', 'shared/s.txt', 'shared content\n'), ('write', 'sub1/a.txt', 'a1\n'), ('write', 'root.txt', 'root1\n'), ['add', '-A'], ['commit', '-m', 'c1'], ('write', 'sub2/b.txt', 'b2\n'), ('write', 'root.txt', 'root2\n'), ['add', '-A'], ['commit', '-m', 'c2'], ('write', 'sub1/a.txt', 'a3\n'), ['add', '-A'], ['commit', '-m', 'c3']], ['rev-list', '--sparse', '--parents', 'HEAD', '--', 'sub1']),
    ('sparse range where dense yields empty output (root touch path)', [('write', 'other.txt', 'x\n'), ['add', '-A'], ['commit', '-m', 'c1'], ('write', 'sub/f.txt', 'y\n'), ['add', '-A'], ['commit', '-m', 'c2'], ('write', 'other.txt', 'z\n'), ['add', '-A'], ['commit', '-m', 'c3']], ['rev-list', '--sparse', 'HEAD', '--', 'sub']),
    ('dense counterpart of root-touch-path case (baseline, unchanged)', [('write', 'other.txt', 'x\n'), ['add', '-A'], ['commit', '-m', 'c1'], ('write', 'sub/f.txt', 'y\n'), ['add', '-A'], ['commit', '-m', 'c2'], ('write', 'other.txt', 'z\n'), ['add', '-A'], ['commit', '-m', 'c3']], ['rev-list', 'HEAD', '--', 'sub']),
]

BATCH9_STDIN_CASES = [
    ('add -p stage first hunk skip second', [('write', 'f.txt', '1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n11\n12\n13\n14\n15\n16\n17\n18\n19\n20\n'), ['add', 'f.txt'], ['commit', '-q', '-m', 'init'], ('write', 'f.txt', 'AAA\n2\n3\n4\n5\n6\n7\n8\n9\n10\n11\n12\n13\n14\n15\n16\n17\n18\n19\nZZZ\n')], ['add', '-p', 'f.txt'], 'y\nn\n'),
    ('add -p resulting staged state', [('write', 'f.txt', '1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n11\n12\n13\n14\n15\n16\n17\n18\n19\n20\n'), ['add', 'f.txt'], ['commit', '-q', '-m', 'init'], ('write', 'f.txt', 'AAA\n2\n3\n4\n5\n6\n7\n8\n9\n10\n11\n12\n13\n14\n15\n16\n17\n18\n19\nZZZ\n'), ['add', '-p', 'f.txt']], ['status', '--short'], 'y\nn\n'),
    ('add -p help then quit', [('write', 'f.txt', '1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n11\n12\n13\n14\n15\n16\n17\n18\n19\n20\n'), ['add', 'f.txt'], ['commit', '-q', '-m', 'init'], ('write', 'f.txt', 'AAA\n2\n3\n4\n5\n6\n7\n8\n9\n10\n11\n12\n13\n14\n15\n16\n17\n18\n19\nZZZ\n')], ['add', '-p', 'f.txt'], '?\nq\n'),
    ('add -p split a single splittable hunk', [('write', 'f.txt', '1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n11\n12\n13\n14\n15\n16\n17\n18\n19\n20\n'), ['add', 'f.txt'], ['commit', '-q', '-m', 'init'], ('write', 'f.txt', '1\n2\n3\n4\nFIVE\n6\n7\n8\nNINE\n10\n11\n12\n13\n14\n15\n16\n17\n18\n19\n20\n')], ['add', '-p', 'f.txt'], 's\ny\nn\n'),
    ('add -p split staged state', [('write', 'f.txt', '1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n11\n12\n13\n14\n15\n16\n17\n18\n19\n20\n'), ['add', 'f.txt'], ['commit', '-q', '-m', 'init'], ('write', 'f.txt', '1\n2\n3\n4\nFIVE\n6\n7\n8\nNINE\n10\n11\n12\n13\n14\n15\n16\n17\n18\n19\n20\n'), ['add', '-p', 'f.txt']], ['status', '--short'], 's\ny\nn\n'),
    ('add -p invalid and two-letter commands', [('write', 'f.txt', '1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n11\n12\n13\n14\n15\n16\n17\n18\n19\n20\n'), ['add', 'f.txt'], ['commit', '-q', '-m', 'init'], ('write', 'f.txt', 'AAA\n2\n3\n4\n5\n6\n7\n8\n9\n10\n11\n12\n13\n14\n15\n16\n17\n18\n19\nZZZ\n')], ['add', '-p', 'f.txt'], 'z\nyy\ny\nK\nq\n'),
    ('add -p goto and search on three hunks', [('write', 'f.txt', '1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n11\n12\n13\n14\n15\n16\n17\n18\n19\n20\n21\n22\n23\n24\n25\n26\n27\n28\n29\n30\n'), ['add', 'f.txt'], ['commit', '-q', '-m', 'init'], ('write', 'f.txt', 'A\n2\n3\n4\n5\n6\n7\n8\n9\n10\n11\n12\n13\n14\nB\n16\n17\n18\n19\n20\n21\n22\n23\n24\n25\n26\n27\n28\n29\nC\n')], ['add', '-p', 'f.txt'], 'g\n2\n/C\nq\n'),
    ('add -p -U1 reduced context', [('write', 'f.txt', '1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n11\n12\n13\n14\n15\n16\n17\n18\n19\n20\n'), ['add', 'f.txt'], ['commit', '-q', '-m', 'init'], ('write', 'f.txt', 'AAA\n2\n3\n4\n5\n6\n7\n8\n9\n10\n11\n12\n13\n14\n15\n16\n17\n18\n19\nZZZ\n')], ['add', '-p', '-U1', 'f.txt'], 'y\nn\n'),
    ('add -e edit with non-interactive editor (success)', [('write', 'f.txt', '1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n11\n12\n13\n14\n15\n16\n17\n18\n19\n20\n'), ['add', 'f.txt'], ['commit', '-q', '-m', 'init'], ('write', 'f.txt', 'AAA\n2\n3\n4\n5\n6\n7\n8\n9\n10\n11\n12\n13\n14\n15\n16\n17\n18\n19\n20\n'), ['config', 'core.editor', 'true'], ['add', '-e', 'f.txt']], ['status', '--short'], ''),
    ('add -p e edit hunk via editor then staged', [('write', 'f.txt', '1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n11\n12\n13\n14\n15\n16\n17\n18\n19\n20\n'), ['add', 'f.txt'], ['commit', '-q', '-m', 'init'], ('write', 'f.txt', 'AAA\n2\n3\n4\n5\n6\n7\n8\n9\n10\n11\n12\n13\n14\n15\n16\n17\n18\n19\n20\n'), ['config', 'core.editor', 'sed -i /^+AAA/d'], ['add', '-p', 'f.txt']], ['status', '--short'], 'e\n'),
    ('add -p -U without -p errors', [('write', 'f.txt', 'a\n'), ['add', 'f.txt'], ['commit', '-q', '-m', 'init'], ('write', 'f.txt', 'a\nb\n')], ['add', '-U2', 'f.txt'], ''),
    ('reset -p unstage on HEAD', [('write', 'f.txt', '1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n11\n12\n13\n14\n15\n16\n17\n18\n19\n20\n'), ['add', 'f.txt'], ['commit', '-q', '-m', 'c1'], ('write', 'f.txt', 'A\n2\n3\n4\n5\n6\n7\n8\n9\n10\n11\n12\n13\n14\n15\n16\n17\n18\n19\nZ\n'), ['add', 'f.txt']], ['reset', '-p', 'f.txt'], 'y\nn\n'),
    ('reset -p unstage staged state', [('write', 'f.txt', '1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n11\n12\n13\n14\n15\n16\n17\n18\n19\n20\n'), ['add', 'f.txt'], ['commit', '-q', '-m', 'c1'], ('write', 'f.txt', 'A\n2\n3\n4\n5\n6\n7\n8\n9\n10\n11\n12\n13\n14\n15\n16\n17\n18\n19\nZ\n'), ['add', 'f.txt'], ['reset', '-p', 'f.txt']], ['status', '--short'], 'y\nn\n'),
    ('reset --hard -p conflict error', [('write', 'f.txt', 'a\n'), ['add', 'f.txt'], ['commit', '-q', '-m', 'c1'], ('write', 'f.txt', 'b\n'), ['add', 'f.txt']], ['reset', '--hard', '-p', 'f.txt'], ''),
    ('stash push -p stash first hunk', [('write', 'f.txt', '1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n11\n12\n13\n14\n15\n16\n17\n18\n19\n20\n'), ['add', 'f.txt'], ['commit', '-q', '-m', 'init'], ('write', 'f.txt', 'AAA\n2\n3\n4\n5\n6\n7\n8\n9\n10\n11\n12\n13\n14\n15\n16\n17\n18\n19\nZZZ\n')], ['stash', 'push', '-p'], 'y\nn\n'),
    ('stash push -p worktree after selection', [('write', 'f.txt', '1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n11\n12\n13\n14\n15\n16\n17\n18\n19\n20\n'), ['add', 'f.txt'], ['commit', '-q', '-m', 'init'], ('write', 'f.txt', 'AAA\n2\n3\n4\n5\n6\n7\n8\n9\n10\n11\n12\n13\n14\n15\n16\n17\n18\n19\nZZZ\n'), ['stash', 'push', '-p']], ['diff'], 'y\nn\n'),
    ('stash push -p no hunk selected', [('write', 'f.txt', '1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n11\n12\n13\n14\n15\n16\n17\n18\n19\n20\n'), ['add', 'f.txt'], ['commit', '-q', '-m', 'init'], ('write', 'f.txt', 'AAA\n2\n3\n4\n5\n6\n7\n8\n9\n10\n11\n12\n13\n14\n15\n16\n17\n18\n19\nZZZ\n')], ['stash', 'push', '-p'], 'n\nn\n'),
    ('stash push -p with -u conflict', [('write', 'f.txt', 'a\n'), ['add', 'f.txt'], ['commit', '-q', '-m', 'i'], ('write', 'f.txt', 'b\n')], ['stash', 'push', '-p', '-u'], 'q\n'),
    ('commit -p commit selected hunk', [('write', 'f.txt', '1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n11\n12\n13\n14\n15\n16\n17\n18\n19\n20\n'), ['add', 'f.txt'], ['commit', '-q', '-m', 'init'], ('write', 'f.txt', 'AAA\n2\n3\n4\n5\n6\n7\n8\n9\n10\n11\n12\n13\n14\n15\n16\n17\n18\n19\nZZZ\n')], ['commit', '-p', '-m', 'partial'], 'y\nn\n'),
    ('commit -p committed tree contains only selected hunk', [('write', 'f.txt', '1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n11\n12\n13\n14\n15\n16\n17\n18\n19\n20\n'), ['add', 'f.txt'], ['commit', '-q', '-m', 'init'], ('write', 'f.txt', 'AAA\n2\n3\n4\n5\n6\n7\n8\n9\n10\n11\n12\n13\n14\n15\n16\n17\n18\n19\nZZZ\n'), ['commit', '-p', '-m', 'partial']], ['show', '--stat', 'HEAD'], 'y\nn\n'),
    ('commit -p select nothing reports unstaged', [('write', 'f.txt', '1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n11\n12\n13\n14\n15\n16\n17\n18\n19\n20\n'), ['add', 'f.txt'], ['commit', '-q', '-m', 'init'], ('write', 'f.txt', 'AAA\n2\n3\n4\n5\n6\n7\n8\n9\n10\n11\n12\n13\n14\n15\n16\n17\n18\n19\nZZZ\n')], ['commit', '-p', '-m', 'none'], 'n\nn\n'),
    ('add -i quit menu', [('write', 'f1.txt', 'a\nb\nc\n'), ('write', 'f2.txt', 'x\ny\nz\n'), ['add', 'f1.txt', 'f2.txt'], ['commit', '-q', '-m', 'init'], ('write', 'f1.txt', 'A\nb\nc\n'), ('write', 'f2.txt', 'x\ny\nZ\n')], ['add', '-i'], 'q\n'),
    ('add -i help command', [('write', 'f1.txt', 'a\nb\nc\n'), ('write', 'f2.txt', 'x\ny\nz\n'), ['add', 'f1.txt', 'f2.txt'], ['commit', '-q', '-m', 'init'], ('write', 'f1.txt', 'A\nb\nc\n'), ('write', 'f2.txt', 'x\ny\nZ\n')], ['add', '-i'], 'h\nq\n'),
    ('add -i update one file', [('write', 'f1.txt', 'a\nb\nc\n'), ('write', 'f2.txt', 'x\ny\nz\n'), ['add', 'f1.txt', 'f2.txt'], ['commit', '-q', '-m', 'init'], ('write', 'f1.txt', 'A\nb\nc\n'), ('write', 'f2.txt', 'x\ny\nZ\n')], ['add', '-i'], '2\n1\n\nq\n'),
    ('add -i update resulting staged state', [('write', 'f1.txt', 'a\nb\nc\n'), ('write', 'f2.txt', 'x\ny\nz\n'), ['add', 'f1.txt', 'f2.txt'], ['commit', '-q', '-m', 'init'], ('write', 'f1.txt', 'A\nb\nc\n'), ('write', 'f2.txt', 'x\ny\nZ\n'), ['add', '-i']], ['status', '--short'], '2\n1\n\nq\n'),
    ('add -i update prompt help and Huh error', [('write', 'f1.txt', 'a\nb\nc\n'), ('write', 'f2.txt', 'x\ny\nz\n'), ['add', 'f1.txt', 'f2.txt'], ['commit', '-q', '-m', 'init'], ('write', 'f1.txt', 'A\nb\nc\n'), ('write', 'f2.txt', 'x\ny\nZ\n')], ['add', '-i'], '2\n?\n99\n\nq\n'),
    ('add -i patch nested hunk selection', [('write', 'f1.txt', 'a\nb\nc\n'), ('write', 'f2.txt', 'x\ny\nz\n'), ['add', 'f1.txt', 'f2.txt'], ['commit', '-q', '-m', 'init'], ('write', 'f1.txt', 'A\nb\nc\n'), ('write', 'f2.txt', 'x\ny\nZ\n')], ['add', '-i'], '5\n1\n\ny\nq\n'),
    ('add -i revert staged file', [('write', 'f1.txt', 'a\nb\nc\n'), ('write', 'f2.txt', 'x\ny\nz\n'), ['add', 'f1.txt', 'f2.txt'], ['commit', '-q', '-m', 'init'], ('write', 'f1.txt', 'A\nb\nc\n'), ('write', 'f2.txt', 'x\ny\nZ\n'), ['add', 'f1.txt', 'f2.txt']], ['add', '-i'], '3\n1\n\nq\n'),
    ('add -i diff of staged file', [('write', 'f1.txt', 'a\nb\nc\n'), ('write', 'f2.txt', 'x\ny\nz\n'), ['add', 'f1.txt', 'f2.txt'], ['commit', '-q', '-m', 'init'], ('write', 'f1.txt', 'A\nb\nc\n'), ('write', 'f2.txt', 'x\ny\nZ\n'), ['add', 'f1.txt']], ['add', '-i'], '6\n1\nq\n'),
    ('add -p new file added', [('write', 'nf.txt', 'x\ny\n')], ['add', '-p', 'nf.txt'], 'y\n'),
    ('add -p deleted file', [('write', 'd.txt', 'a\n'), ['add', 'd.txt'], ['commit', '-q', '-m', 'i'], ('rm', 'd.txt')], ['add', '-p', 'd.txt'], 'y\n'),
    ('add -p mode change', [('write', 'm.txt', 'a\n'), ['add', 'm.txt'], ['commit', '-q', '-m', 'i']], ['add', '-p', 'm.txt'], 'y\n'),
    ('prompt n', [('write', 'f.txt', 'a\n'), ['add', 'f.txt'], ['commit', '-m', 'init'], ('write', 'f.txt', 'b\n')], ['difftool', '-x', 'echo DIFF'], 'n\n'),
    ('bad-bool prompt', [('write', 'f.txt', 'a\n'), ['add', 'f.txt'], ['commit', '-m', 'init'], ('write', 'f.txt', 'b\n'), ['config', 'difftool.prompt', 'maybe']], ['difftool', '-x', 'echo D'], 'n\n'),
    ('precedence y prompt', [('write', 'f.txt', 'a\n'), ['add', 'f.txt'], ['commit', '-m', 'init'], ('write', 'f.txt', 'b\n')], ['difftool', '-y', '--prompt', '-x', 'echo D'], 'n\n'),
]

@pytest.mark.parametrize("case", BATCH9_CASES, ids=[c[0] for c in BATCH9_CASES])
def test_batch9_parity(case, tmp_path: Path, git_254_oracle: str):
    _id, setup, probe = case
    assert_command_parity(git_254_oracle, tmp_path, setup, probe)


@pytest.mark.parametrize("case", BATCH9_STDIN_CASES, ids=[c[0] for c in BATCH9_STDIN_CASES])
def test_batch9_stdin_parity(case, tmp_path: Path, git_254_oracle: str):
    _id, setup, probe, stdin = case
    assert_command_parity(git_254_oracle, tmp_path, setup, probe, stdin=stdin)


# difftool copies the "left" version of each file pair into a random temp dir
# (/tmp/git-blob-XXXX/, and /tmp/git-difftool-XXXX/ for --dir-diff), whose name
# appears in -x/extcmd output. The behavior is byte-exact apart from that random
# component, so these normalize it away before comparing.
BATCH9_DIFFTOOL_TMP_CASES = [
    ("difftool-extcmd-eq", [("write", "f.txt", "a\n"), ["add", "f.txt"], ["commit", "-m", "init"],
                            ("write", "f.txt", "b\n")],
     ["difftool", "--no-prompt", "--extcmd=echo DIFF"], None),
    ("difftool-x-prompt-false",
     [("write", "f.txt", "a\n"), ["add", "f.txt"], ["commit", "-m", "init"], ("write", "f.txt", "b\n"),
      ["config", "difftool.prompt", "false"]],
     ["difftool", "-x", "echo D"], None),
    ("difftool-dir-diff",
     [("write", "f.txt", "l1\nl2\n"), ["add", "f.txt"], ["commit", "-m", "init"], ("write", "f.txt", "l1\nX\n")],
     ["difftool", "-d", "-y", "-x", "diff"], None),
    ("difftool-x-prompt-y",
     [("write", "f.txt", "a\n"), ["add", "f.txt"], ["commit", "-m", "init"], ("write", "f.txt", "b\n")],
     ["difftool", "-x", "echo DIFF"], "y\n"),
    ("difftool-x-prompt-n",
     [("write", "f.txt", "a\n"), ["add", "f.txt"], ["commit", "-m", "init"], ("write", "f.txt", "b\n")],
     ["difftool", "-x", "echo LAUNCHED"], "N\n"),
]


@pytest.mark.parametrize("case", BATCH9_DIFFTOOL_TMP_CASES, ids=[c[0] for c in BATCH9_DIFFTOOL_TMP_CASES])
def test_batch9_difftool_tmpnorm_parity(case, tmp_path: Path, git_254_oracle: str):
    import re
    import subprocess
    from tests.git_parity.support import DETERMINISTIC_ENV, ROOT, pygit_cmd

    _id, setup, probe, stdin = case
    env = dict(__import__("os").environ)
    env.update(DETERMINISTIC_ENV)
    env["PYTHONPATH"] = str(ROOT)

    def norm(s: str) -> str:
        # collapse the random temp-dir component git/pygit picks per run
        s = re.sub(r"git-blob-\w+", "git-blob-X", s)
        s = re.sub(r"git-difftool[.\-]\w+", "git-difftool-X", s)
        return s

    results = {}
    for tool, base in (("oracle", [git_254_oracle]), ("pygit", pygit_cmd())):
        repo = tmp_path / tool
        repo.mkdir()
        subprocess.run([*base, "init", "-b", "main", "."], cwd=repo, env=env, capture_output=True)
        for step in setup:
            if isinstance(step, tuple) and step and step[0] == "write":
                (repo / step[1]).write_text(step[2])
            else:
                subprocess.run([*base, *step], cwd=repo, env=env, capture_output=True)
        proc = subprocess.run([*base, *probe], cwd=repo, env=env, input=stdin,
                              text=True, capture_output=True)
        results[tool] = (proc.returncode, norm(proc.stdout), norm(proc.stderr))

    assert results["pygit"] == results["oracle"]


BATCH10_CASES = [
    ('reset -U2 without --patch -> requires --patch rc128', [], ['reset', '-U2']),
    ('reset --unified=2 without --patch -> requires --patch rc128', [], ['reset', '--unified=2']),
    ('reset --unified with no value -> requires a value rc129', [], ['reset', '--unified']),
    ('reset --unified= empty value -> expects a numerical value rc129', [], ['reset', '--unified=']),
    ('reset --inter-hunk-context=2 without --patch -> requires --patch rc128', [], ['reset', '--inter-hunk-context=2']),
    ('reset --inter-hunk-context no value -> requires a value rc129', [], ['reset', '--inter-hunk-context']),
    ('reset -U-2 negative -> cannot be negative rc128', [], ['reset', '-U-2']),
    ('reset --inter-hunk-context=-2 negative -> cannot be negative rc128', [], ['reset', '--inter-hunk-context=-2']),
    ('reset -Uabc non-integer -> switch U expects integer rc129', [], ['reset', '-Uabc']),
    ('reset -U with no value -> switch U requires a value rc129', [], ['reset', '-U']),
    ('reset -U2x trailing junk -> switch U expects integer rc129', [], ['reset', '-U2x']),
    ('reset --unified=abc -> option unified expects integer rc129', [], ['reset', '--unified=abc']),
    ('reset --unified=2k suffix without --patch -> requires --patch rc128', [], ['reset', '--unified=2k']),
    ('reset --unified=2147483648 out of range -> not in range rc129', [], ['reset', '--unified=2147483648']),
    ('reset -U2g (2*1024^3 overflow) -> not in range rc129', [], ['reset', '-U2g']),
    ('reset -U0x10 hex parsed then requires --patch rc128', [], ['reset', '-U0x10']),
    ('reset --unified=010 octal parsed then requires --patch rc128', [], ['reset', '--unified=010']),
    ('reset -U2 --inter-hunk-context=3 unified error reported first rc128', [], ['reset', '-U2', '--inter-hunk-context=3']),
    ('stash --only-untracked unknown option -> usage block rc129', [], ['stash', '--only-untracked']),
    ('stash --index unknown option -> usage block rc129', [], ['stash', '--index']),
    ('stash --print unknown option -> usage block rc129', [], ['stash', '--print']),
    ('stash --to-ref unknown option -> usage block rc129', [], ['stash', '--to-ref']),
    ('stash -Z unknown short option -> unknown switch + usage rc129', [], ['stash', '-Z']),
    ('fetch no remotes configured exits 0', [('write', 'f.txt', 'r1\n'), ['add', 'f.txt'], ['commit', '-m', 'r1']], ['fetch']),
]

BATCH10_STDIN_CASES = [
    ('apply --add on empty input -> No valid patches rc128', [], ['apply', '--add'], ''),
    ('apply --no-add on empty input -> identical to --add', [], ['apply', '--no-add'], ''),
    ('apply bare on empty input -> identical baseline', [], ['apply'], ''),
    ('apply --add --allow-empty on empty input -> rc0', [], ['apply', '--add', '--allow-empty'], ''),
    ('reset --patch -U1 shows 1 context line each side, quit', [('write', 'f.txt', '1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n'), ['add', 'f.txt'], ['commit', '-m', 'init'], ('write', 'f.txt', '1\n2\n3\n4\nFIVE\n6\n7\n8\n9\n10\n'), ['add', 'f.txt']], ['reset', '--patch', '-U1'], 'q\n'),
    ('reset --patch -U5 shows wide context, quit', [('write', 'f.txt', '1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n'), ['add', 'f.txt'], ['commit', '-m', 'init'], ('write', 'f.txt', '1\n2\n3\n4\nFIVE\n6\n7\n8\n9\n10\n'), ['add', 'f.txt']], ['reset', '--patch', '-U5'], 'q\n'),
    ('reset --patch --unified=2 long form, quit', [('write', 'f.txt', '1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n'), ['add', 'f.txt'], ['commit', '-m', 'init'], ('write', 'f.txt', '1\n2\n3\n4\nFIVE\n6\n7\n8\n9\n10\n'), ['add', 'f.txt']], ['reset', '--patch', '--unified=2'], 'q\n'),
    ('reset --patch -U2 accept hunk (y) unstages', [('write', 'f.txt', '1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n'), ['add', 'f.txt'], ['commit', '-m', 'init'], ('write', 'f.txt', '1\n2\n3\n4\nFIVE\n6\n7\n8\n9\n10\n'), ['add', 'f.txt']], ['reset', '--patch', '-U2'], 'y\n'),
    ('reset --patch -U2 skip hunk (n)', [('write', 'f.txt', '1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n'), ['add', 'f.txt'], ['commit', '-m', 'init'], ('write', 'f.txt', '1\n2\n3\n4\nFIVE\n6\n7\n8\n9\n10\n'), ['add', 'f.txt']], ['reset', '--patch', '-U2'], 'n\n'),
    ('reset -pU2 clustered short, quit', [('write', 'f.txt', '1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n'), ['add', 'f.txt'], ['commit', '-m', 'init'], ('write', 'f.txt', '1\n2\n3\n4\nFIVE\n6\n7\n8\n9\n10\n'), ['add', 'f.txt']], ['reset', '-pU2'], 'q\n'),
]

BATCH10_NETWORK_CASES = [
    ('pull real merge divergent', [['init', '--bare', '../remote.git'], ('write', 'f.txt', 'r1\n'), ['add', 'f.txt'], ['commit', '-m', 'r1'], ['remote', 'add', 'origin', '../remote.git'], ['push', 'origin', 'main'], ('write', 'f.txt', 'r1\nr2\n'), ['commit', '-am', 'r2'], ['push', 'origin', 'main'], ['reset', '--hard', 'HEAD~1'], ('write', 'g.txt', 'local\n'), ['add', 'g.txt'], ['commit', '-m', 'localwork']], ['pull', '--no-rebase', 'origin', 'main'], None),
    ('push new branch to local bare', [('write', 'a.txt', 'hello\n'), ['add', 'a.txt'], ['commit', '-m', 'first'], ['init', '--bare', '@BASE@/remote.git'], ['remote', 'add', 'origin', '@BASE@/remote.git']], ['push', 'origin', 'main'], None),
    ('push -v new branch (Pushing to + tracking ref)', [('write', 'a.txt', 'hello\n'), ['add', 'a.txt'], ['commit', '-m', 'first'], ['init', '--bare', '@BASE@/remote.git'], ['remote', 'add', 'origin', '@BASE@/remote.git']], ['push', '-v', 'origin', 'main'], None),
    ('push -u set-upstream', [('write', 'a.txt', 'hello\n'), ['add', 'a.txt'], ['commit', '-m', 'first'], ['init', '--bare', '@BASE@/remote.git'], ['remote', 'add', 'origin', '@BASE@/remote.git']], ['push', '-u', 'origin', 'main'], None),
    ('push HEAD displays HEAD -> main', [('write', 'a.txt', 'hello\n'), ['add', 'a.txt'], ['commit', '-m', 'first'], ['init', '--bare', '@BASE@/remote.git'], ['remote', 'add', 'origin', '@BASE@/remote.git']], ['push', 'origin', 'HEAD'], None),
    ('push up-to-date second push', [('write', 'a.txt', 'hello\n'), ['add', 'a.txt'], ['commit', '-m', 'first'], ['init', '--bare', '@BASE@/remote.git'], ['remote', 'add', 'origin', '@BASE@/remote.git'], ['push', 'origin', 'main']], ['push', 'origin', 'main'], None),
    ('push delete / colon-delete / delete-nonexistent', [('write', 'a.txt', 'hello\n'), ['add', 'a.txt'], ['commit', '-m', 'first'], ['init', '--bare', '@BASE@/remote.git'], ['remote', 'add', 'origin', '@BASE@/remote.git'], ['push', 'origin', 'main']], ['push', 'origin', '--delete', 'main'], None),
    ('push non-ff rejection with advice', [('write', 'a.txt', 'hello\n'), ['add', 'a.txt'], ['commit', '-m', 'first'], ['init', '--bare', '@BASE@/remote.git'], ['remote', 'add', 'origin', '@BASE@/remote.git'], ['push', 'origin', 'main'], ('write', 'a.txt', 'hello\ntwo\n'), ['commit', '-am', 'second'], ['push', 'origin', 'main'], ['reset', '--hard', 'HEAD~1'], ('write', 'c.txt', 'div\n'), ['add', 'c.txt'], ['commit', '-m', 'divergent']], ['push', 'origin', 'main'], None),
    ('push -f forced update', [('write', 'a.txt', 'hello\n'), ['add', 'a.txt'], ['commit', '-m', 'first'], ['init', '--bare', '@BASE@/remote.git'], ['remote', 'add', 'origin', '@BASE@/remote.git'], ['push', 'origin', 'main'], ('write', 'a.txt', 'hello\ntwo\n'), ['commit', '-am', 'second'], ['push', 'origin', 'main'], ['reset', '--hard', 'HEAD~1'], ('write', 'c.txt', 'div\n'), ['add', 'c.txt'], ['commit', '-m', 'divergent']], ['push', '-f', 'origin', 'main'], None),
    ('push --force-with-lease (tracking match -> forced)', [['init', '--bare', '@BASE@/remote.git'], ('write', 'f.txt', 'r1\n'), ['add', 'f.txt'], ['commit', '-m', 'r1'], ['remote', 'add', 'origin', '@BASE@/remote.git'], ['push', 'origin', 'main'], ['fetch', 'origin'], ('write', 'f.txt', 'r1\nr2\n'), ['commit', '-am', 'r2']], ['push', '--force-with-lease', 'origin', 'main'], None),
    ('push --mirror', [('write', 'a.txt', 'hello\n'), ['add', 'a.txt'], ['commit', '-m', 'first'], ['branch', 'dev'], ['tag', 'v1'], ['init', '--bare', '@BASE@/remote.git'], ['remote', 'add', 'origin', '@BASE@/remote.git']], ['push', '--mirror', 'origin'], None),
    ('push --all sorted branches', [('write', 'a.txt', 'hello\n'), ['add', 'a.txt'], ['commit', '-m', 'first'], ['branch', 'dev'], ['branch', 'feature'], ['init', '--bare', '@BASE@/remote.git'], ['remote', 'add', 'origin', '@BASE@/remote.git']], ['push', '--all', 'origin'], None),
    ('push --tags', [('write', 'a.txt', 'hello\n'), ['add', 'a.txt'], ['commit', '-m', 'first'], ['tag', 'v1'], ['tag', 'v2'], ['init', '--bare', '@BASE@/remote.git'], ['remote', 'add', 'origin', '@BASE@/remote.git']], ['push', 'origin', '--tags'], None),
    ('push no upstream fatal', [('write', 'a.txt', 'hello\n'), ['add', 'a.txt'], ['commit', '-m', 'first'], ['init', '--bare', '@BASE@/remote.git'], ['remote', 'add', 'origin', '@BASE@/remote.git']], ['push'], None),
    ('push -o without receive.advertisePushOptions', [('write', 'f.txt', 'r1\n'), ['add', 'f.txt'], ['commit', '-m', 'r1'], ['init', '--bare', '@BASE@/remote.git'], ['remote', 'add', 'origin', '@BASE@/remote.git']], ['push', '-o', 'x=y', 'origin', 'main'], None),
    ('push unknown option -> usage rc129', [('write', 'f.txt', 'r1\n'), ['add', 'f.txt'], ['commit', '-m', 'r1'], ['init', '--bare', '@BASE@/remote.git'], ['remote', 'add', 'origin', '@BASE@/remote.git']], ['push', '--bogus', 'origin', 'main'], None),
    ('push -d unmatched aborts all', [('write', 'f.txt', 'r1\n'), ['add', 'f.txt'], ['commit', '-m', 'r1'], ['branch', 'dev'], ['init', '--bare', '@BASE@/remote.git'], ['remote', 'add', 'origin', '@BASE@/remote.git'], ['push', 'origin', 'main', 'dev']], ['push', '-d', 'origin', 'dev', 'ghost'], None),
    ('fetch origin new branches+tag', [['init', '--bare', '@BASE@/remote.git'], ('write', 'f.txt', 'r1\n'), ['add', 'f.txt'], ['commit', '-m', 'r1'], ['branch', 'dev'], ['tag', 'v1'], ['remote', 'add', 'origin', '@BASE@/remote.git'], ['push', 'origin', 'main', 'dev'], ['push', 'origin', 'v1'], ['update-ref', '-d', 'refs/remotes/origin/main'], ['update-ref', '-d', 'refs/remotes/origin/dev']], ['fetch', 'origin'], None),
    ('fetch origin main refspec (FETCH_HEAD + opportunistic)', [['init', '--bare', '@BASE@/remote.git'], ('write', 'f.txt', 'r1\n'), ['add', 'f.txt'], ['commit', '-m', 'r1'], ['remote', 'add', 'origin', '@BASE@/remote.git'], ['push', 'origin', 'main'], ['update-ref', '-d', 'refs/remotes/origin/main']], ['fetch', 'origin', 'main'], None),
    ('fetch --no-tags', [['init', '--bare', '@BASE@/remote.git'], ('write', 'f.txt', 'r1\n'), ['add', 'f.txt'], ['commit', '-m', 'r1'], ['branch', 'dev'], ['tag', 'v1'], ['remote', 'add', 'origin', '@BASE@/remote.git'], ['push', 'origin', 'main', 'dev'], ['push', 'origin', 'v1'], ['update-ref', '-d', 'refs/remotes/origin/main'], ['update-ref', '-d', 'refs/remotes/origin/dev']], ['fetch', '--no-tags', 'origin'], None),
    ('fetch --prune deleted branch', [['init', '--bare', '@BASE@/remote.git'], ('write', 'f.txt', 'r1\n'), ['add', 'f.txt'], ['commit', '-m', 'r1'], ['branch', 'dev'], ['remote', 'add', 'origin', '@BASE@/remote.git'], ['push', 'origin', 'main', 'dev'], ['fetch', 'origin'], ['push', 'origin', '--delete', 'dev']], ['fetch', '--prune', 'origin'], None),
    ('fetch ff update', [['init', '--bare', '@BASE@/remote.git'], ('write', 'f.txt', 'r1\n'), ['add', 'f.txt'], ['commit', '-m', 'r1'], ['remote', 'add', 'origin', '@BASE@/remote.git'], ['push', 'origin', 'main'], ['fetch', 'origin'], ('write', 'f.txt', 'r1\nr2\n'), ['commit', '-am', 'r2'], ['push', 'origin', 'main'], ['reset', '--hard', 'HEAD~1']], ['fetch', 'origin'], None),
    ('fetch tag clobber reject (summary_width=0)', [['init', '--bare', '@BASE@/remote.git'], ('write', 'f.txt', 'r1\n'), ['add', 'f.txt'], ['commit', '-m', 'r1'], ['remote', 'add', 'origin', '@BASE@/remote.git'], ['push', 'origin', 'main'], ['tag', 'v1'], ['push', 'origin', 'v1'], ('write', 'x.txt', 'x\n'), ['add', 'x.txt'], ['commit', '-m', 'c2'], ['tag', '-f', 'v1'], ['push', 'origin', '-f', 'v1'], ['update-ref', 'refs/tags/v1', 'HEAD~1']], ['fetch', 'origin', 'refs/tags/v1:refs/tags/v1'], None),
    ('fetch tag <name> shorthand', [['init', '--bare', '@BASE@/remote.git'], ('write', 'f.txt', 'r1\n'), ['add', 'f.txt'], ['commit', '-m', 'r1'], ['tag', 'v1'], ['remote', 'add', 'origin', '@BASE@/remote.git'], ['push', 'origin', 'main'], ['push', 'origin', 'v1'], ['tag', '-d', 'v1']], ['fetch', 'origin', 'tag', 'v1'], None),
    ('fetch --all multiple remotes (Fetching <name>)', [['init', '--bare', '@BASE@/r1.git'], ['init', '--bare', '@BASE@/r2.git'], ('write', 'f.txt', 'r1\n'), ['add', 'f.txt'], ['commit', '-m', 'r1'], ['remote', 'add', 'one', '@BASE@/r1.git'], ['remote', 'add', 'two', '@BASE@/r2.git'], ['push', 'one', 'main'], ['push', 'two', 'main'], ['update-ref', '-d', 'refs/remotes/one/main'], ['update-ref', '-d', 'refs/remotes/two/main']], ['fetch', '--all'], None),
    ('fetch bare URL HEAD-only (FETCH_HEAD url-only note)', [['init', '--bare', '@BASE@/remote.git'], ('write', 'f.txt', 'r1\n'), ['add', 'f.txt'], ['commit', '-m', 'r1'], ['remote', 'add', 'origin', '@BASE@/remote.git'], ['push', 'origin', 'HEAD:refs/heads/master'], ['remote', 'remove', 'origin']], ['fetch', '@BASE@/remote.git'], None),
    ('fetch bare URL unresolvable HEAD fatal', [['init', '--bare', '@BASE@/remote.git'], ('write', 'f.txt', 'r1\n'), ['add', 'f.txt'], ['commit', '-m', 'r1'], ['remote', 'add', 'origin', '@BASE@/remote.git'], ['push', 'origin', 'main'], ['remote', 'remove', 'origin']], ['fetch', '@BASE@/remote.git', 'main'], None),
    ('fetch --dry-run (no FETCH_HEAD, no ref update)', [['init', '--bare', '@BASE@/remote.git'], ('write', 'f.txt', 'r1\n'), ['add', 'f.txt'], ['commit', '-m', 'r1'], ['remote', 'add', 'origin', '@BASE@/remote.git'], ['push', 'origin', 'main'], ['update-ref', '-d', 'refs/remotes/origin/main']], ['fetch', '--dry-run', 'origin'], None),
    ('fetch unknown option -> usage rc129', [('write', 'f.txt', 'r1\n'), ['add', 'f.txt'], ['commit', '-m', 'r1'], ['init', '--bare', '@BASE@/remote.git'], ['remote', 'add', 'origin', '@BASE@/remote.git']], ['fetch', '--bogus', 'origin'], None),
    ('fetch file:// URL', [['init', '--bare', '@BASE@/remote.git'], ('write', 'f.txt', 'r1\n'), ['add', 'f.txt'], ['commit', '-m', 'r1'], ['remote', 'add', 'origin', 'file://@BASE@/remote.git'], ['push', 'origin', 'main'], ['update-ref', '-d', 'refs/remotes/origin/main']], ['fetch', 'origin'], None),
    ('pull origin main fast-forward', [['init', '--bare', '@BASE@/remote.git'], ('write', 'f.txt', 'r1\n'), ['add', 'f.txt'], ['commit', '-m', 'r1'], ['remote', 'add', 'origin', '@BASE@/remote.git'], ['push', 'origin', 'main'], ('write', 'f.txt', 'r1\nr2\n'), ['commit', '-am', 'r2'], ['push', 'origin', 'main'], ['reset', '--hard', 'HEAD~1']], ['pull', 'origin', 'main'], None),
    ('pull origin main up to date', [['init', '--bare', '@BASE@/remote.git'], ('write', 'f.txt', 'r1\n'), ['add', 'f.txt'], ['commit', '-m', 'r1'], ['remote', 'add', 'origin', '@BASE@/remote.git'], ['push', 'origin', 'main']], ['pull', 'origin', 'main'], None),
    ('pull --ff-only divergent fail (diverging advice)', [['init', '--bare', '@BASE@/remote.git'], ('write', 'f.txt', 'r1\n'), ['add', 'f.txt'], ['commit', '-m', 'r1'], ['remote', 'add', 'origin', '@BASE@/remote.git'], ['push', 'origin', 'main'], ('write', 'f.txt', 'r1\nr2\n'), ['commit', '-am', 'r2'], ['push', 'origin', 'main'], ['reset', '--hard', 'HEAD~1'], ('write', 'g.txt', 'local\n'), ['add', 'g.txt'], ['commit', '-m', 'localwork']], ['pull', '--ff-only', 'origin', 'main'], None),
    ('pull --rebase divergent', [['init', '--bare', '@BASE@/remote.git'], ('write', 'f.txt', 'r1\n'), ['add', 'f.txt'], ['commit', '-m', 'r1'], ['remote', 'add', 'origin', '@BASE@/remote.git'], ['push', 'origin', 'main'], ('write', 'f.txt', 'r1\nr2\n'), ['commit', '-am', 'r2'], ['push', 'origin', 'main'], ['reset', '--hard', 'HEAD~1'], ('write', 'g.txt', 'local\n'), ['add', 'g.txt'], ['commit', '-m', 'localwork']], ['pull', '--rebase', 'origin', 'main'], None),
    ('pull no tracking information', [['init', '--bare', '@BASE@/remote.git'], ('write', 'f.txt', 'r1\n'), ['add', 'f.txt'], ['commit', '-m', 'r1'], ['remote', 'add', 'origin', '@BASE@/remote.git'], ['push', 'origin', 'main']], ['pull'], None),
    ('pull configured upstream ff (no args)', [['init', '--bare', '@BASE@/remote.git'], ('write', 'f.txt', 'r1\n'), ['add', 'f.txt'], ['commit', '-m', 'r1'], ['remote', 'add', 'origin', '@BASE@/remote.git'], ['push', '-u', 'origin', 'main'], ('write', 'f.txt', 'r1\nr2\n'), ['commit', '-am', 'r2'], ['push', 'origin', 'main'], ['reset', '--hard', 'HEAD~1']], ['pull'], None),
    ('pull non-default remote, no branch', [['init', '--bare', '@BASE@/remote.git'], ('write', 'f.txt', 'r1\n'), ['add', 'f.txt'], ['commit', '-m', 'r1'], ['remote', 'add', 'origin', '@BASE@/remote.git'], ['push', '-u', 'origin', 'main'], ['remote', 'add', 'other', '@BASE@/remote.git']], ['pull', 'other'], None),
]

@pytest.mark.parametrize("case", BATCH10_CASES, ids=[c[0] for c in BATCH10_CASES])
def test_batch10_parity(case, tmp_path: Path, git_254_oracle: str):
    _id, setup, probe = case
    assert_command_parity(git_254_oracle, tmp_path, setup, probe)


@pytest.mark.parametrize("case", BATCH10_STDIN_CASES, ids=[c[0] for c in BATCH10_STDIN_CASES])
def test_batch10_stdin_parity(case, tmp_path: Path, git_254_oracle: str):
    _id, setup, probe, stdin = case
    assert_command_parity(git_254_oracle, tmp_path, setup, probe, stdin=stdin)


# Network cases (fetch/pull/push) need a SEPARATE remote per tool: the standard
# harness runs oracle and pygit as siblings under one tmp_path, so a shared
# relative remote collides. Here each tool gets an isolated root; @BASE@ in the
# case is substituted with that root (so the remote lives at <root>/remote.git)
# and normalized back to @BASE@ in the captured output so the path strings match.
@pytest.mark.parametrize("case", BATCH10_NETWORK_CASES, ids=[c[0] for c in BATCH10_NETWORK_CASES])
def test_batch10_network_parity(case, tmp_path: Path, git_254_oracle: str):
    import subprocess
    from tests.git_parity.support import DETERMINISTIC_ENV, ROOT, pygit_cmd
    _id, setup, probe, stdin = case
    env = dict(__import__('os').environ); env.update(DETERMINISTIC_ENV); env['PYTHONPATH'] = str(ROOT)
    def snap(work):
        gd = work / '.git'
        pf = gd / 'packed-refs'
        packed = pf.read_text() if pf.exists() else '<none>'
        refs = sorted((str(p.relative_to(gd)).replace(chr(92), '/'), p.read_text())
                      for p in (gd / 'refs').rglob('*') if p.is_file())
        return packed, refs
    results = {}
    for tool, base in (('oracle', [git_254_oracle]), ('pygit', pygit_cmd())):
        root = tmp_path / tool
        work = root / 'work'
        work.mkdir(parents=True)
        BASE = str(root)
        def sub(x):
            return x.replace('@BASE@', BASE) if isinstance(x, str) else x
        def norm(s):
            return s.replace(BASE, '@BASE@')
        subprocess.run([*base, 'init', '-b', 'main', '.'], cwd=work, env=env, capture_output=True)
        for step in setup:
            if isinstance(step, tuple) and step and step[0] == 'write':
                (work / step[1]).parent.mkdir(parents=True, exist_ok=True)
                (work / step[1]).write_text(step[2])
            elif isinstance(step, tuple) and step and step[0] == 'rm':
                (work / step[1]).unlink()
            else:
                subprocess.run([*base, *[sub(a) for a in step]], cwd=work, env=env, capture_output=True)
        proc = subprocess.run([*base, *[sub(a) for a in probe]], cwd=work, env=env,
                              input=stdin, text=True, capture_output=True)
        packed, refs = snap(work)
        results[tool] = (proc.returncode, norm(proc.stdout), norm(proc.stderr),
                         norm(packed), [(k, norm(v)) for k, v in refs])
    assert results['pygit'] == results['oracle']


BATCH11_GPG_CASES = [
    ('commit -t missing template 128', [('write', 'f', 'hi\n'), ['add', 'f'], ['commit', '-m', 'init']], ['commit', '--allow-empty', '-t', '/tmp/pygit_no_such_template_xyz']),
    ('commit -t ignored when -m given', [('write', 'f', 'hi\n'), ['add', 'f'], ['commit', '-m', 'init'], ['commit', '--allow-empty', '-t', '/tmp/pygit_no_such_template_xyz', '-m', 'kept']], ['log', '-1', '--format=%B']),
    ('commit -S broken gpg 128', [('write', 'f', 'hi\n'), ['add', 'f'], ['commit', '-m', 'init'], ['config', 'gpg.program', '/bin/false']], ['commit', '--allow-empty', '-S', '-m', 'sign']),
    ('commit -S gpg no SIG_CREATED 128', [('write', 'f', 'hi\n'), ['add', 'f'], ['commit', '-m', 'init'], ['config', 'gpg.program', '/bin/true']], ['commit', '--allow-empty', '-S', '-m', 'sign']),
    ('commit.gpgsign true broken gpg 128', [('write', 'f', 'hi\n'), ['add', 'f'], ['commit', '-m', 'init'], ['config', 'commit.gpgsign', 'true'], ['config', 'gpg.program', '/bin/false']], ['commit', '--allow-empty', '-m', 'x']),
    ('no-gpg-sign overrides config 0', [('write', 'f', 'hi\n'), ['add', 'f'], ['commit', '-m', 'init'], ['config', 'commit.gpgsign', 'true'], ['config', 'gpg.program', '/bin/false']], ['commit', '--allow-empty', '--no-gpg-sign', '-m', 'unsigned']),
    ('commit -t requires value 129', [('write', 'f', 'hi\n'), ['add', 'f'], ['commit', '-m', 'init']], ['commit', '-t']),
    ('commit -F requires value 129', [('write', 'f', 'hi\n'), ['add', 'f'], ['commit', '-m', 'init']], ['commit', '-F']),
    ('tag -s broken gpg 128', [('write', 'f', 'hi\n'), ['add', 'f'], ['commit', '-m', 'init'], ['config', 'gpg.program', '/bin/false']], ['tag', '-s', '-m', 'tm', 'sigtag']),
    ('tag -u requires value 129', [('write', 'f', 'hi\n'), ['add', 'f'], ['commit', '-m', 'init']], ['tag', '-u']),
    ('tag --local-user requires value 129', [('write', 'f', 'hi\n'), ['add', 'f'], ['commit', '-m', 'init']], ['tag', '--local-user']),
    ('tag -F requires value 129', [('write', 'f', 'hi\n'), ['add', 'f'], ['commit', '-m', 'init']], ['tag', '-F']),
    ('verify-commit unsigned 1', [('write', 'f', 'hi\n'), ['add', 'f'], ['commit', '-m', 'init']], ['verify-commit', 'HEAD']),
    ('verify-commit -v unsigned 1', [('write', 'f', 'hi\n'), ['add', 'f'], ['commit', '-m', 'init']], ['verify-commit', '-v', 'HEAD']),
    ('verify-commit --raw unsigned 1', [('write', 'f', 'hi\n'), ['add', 'f'], ['commit', '-m', 'init']], ['verify-commit', '--raw', 'HEAD']),
    ('verify-commit no args 129', [('write', 'f', 'hi\n'), ['add', 'f'], ['commit', '-m', 'init']], ['verify-commit']),
    ('verify-commit unknown long option 129', [('write', 'f', 'hi\n'), ['add', 'f'], ['commit', '-m', 'init']], ['verify-commit', '--bogus']),
    ('verify-commit unknown short switch 129', [('write', 'f', 'hi\n'), ['add', 'f'], ['commit', '-m', 'init']], ['verify-commit', '-Z']),
    ('verify-commit nonexistent 1', [('write', 'f', 'hi\n'), ['add', 'f'], ['commit', '-m', 'init']], ['verify-commit', 'nope']),
    ('verify-commit on tag object 1', [('write', 'f', 'hi\n'), ['add', 'f'], ['commit', '-m', 'init'], ['tag', '-a', '-m', 't', 'v1']], ['verify-commit', 'v1']),
    ('verify-commit multi one bad 1', [('write', 'f', 'hi\n'), ['add', 'f'], ['commit', '-m', 'init']], ['verify-commit', 'HEAD', 'nope']),
    ('verify-tag no args 129', [('write', 'f', 'hi\n'), ['add', 'f'], ['commit', '-m', 'init']], ['verify-tag']),
    ('verify-tag missing 1', [('write', 'f', 'hi\n'), ['add', 'f'], ['commit', '-m', 'init']], ['verify-tag', 'notag']),
    ('verify-tag unsigned annotated 1', [('write', 'f', 'hi\n'), ['add', 'f'], ['commit', '-m', 'init'], ['tag', '-a', '-m', 't', 'v1']], ['verify-tag', 'v1']),
    ('verify-tag -v unsigned annotated 1', [('write', 'f', 'hi\n'), ['add', 'f'], ['commit', '-m', 'init'], ['tag', '-a', '-m', 't', 'v1']], ['verify-tag', '-v', 'v1']),
    ('verify-tag on commit object 1', [('write', 'f', 'hi\n'), ['add', 'f'], ['commit', '-m', 'init']], ['verify-tag', 'HEAD']),
    ('tag -v unsigned annotated 1', [('write', 'f', 'hi\n'), ['add', 'f'], ['commit', '-m', 'init'], ['tag', '-a', '-m', 't', 'v1']], ['tag', '-v', 'v1']),
]

BATCH11_GPG_STDIN_CASES = [
    ('commit -t seeds message via core.editor', [('write', 'f', 'hi\n'), ['add', 'f'], ['commit', '-m', 'init'], ('write', 'ed.sh', 'printf \'X\\n\' >> "$1"\n'), ['config', 'core.editor', 'sh ed.sh'], ('write', 't.txt', 'Subj\nBody\n'), ['commit', '--no-status', '--allow-empty', '-t', 't.txt']], ['log', '-1', '--format=%B'], ''),
    ('commit -t unchanged template aborts', [('write', 'f', 'hi\n'), ['add', 'f'], ['commit', '-m', 'init'], ['config', 'core.editor', 'true'], ('write', 't.txt', 'Untouched\n'), ['commit', '--no-status', '--allow-empty', '-t', 't.txt']], ['log', '--oneline'], ''),
]

@pytest.mark.parametrize("case", BATCH11_GPG_CASES, ids=[c[0] for c in BATCH11_GPG_CASES])
def test_batch11_gpg_parity(case, tmp_path: Path, git_254_oracle: str):
    _id, setup, probe = case
    assert_command_parity(git_254_oracle, tmp_path, setup, probe)


@pytest.mark.parametrize("case", BATCH11_GPG_STDIN_CASES, ids=[c[0] for c in BATCH11_GPG_STDIN_CASES])
def test_batch11_gpg_stdin_parity(case, tmp_path: Path, git_254_oracle: str):
    _id, setup, probe, stdin = case
    assert_command_parity(git_254_oracle, tmp_path, setup, probe, stdin=stdin)


BATCH11_ENG_CASES = [
    ('am-basic', [('write', 'f.txt', 'line1\n'), ['add', '-A'], ['commit', '-m', 'init'], ('write', 'p.mbox', 'From b9c5a12fc2eac585f2426f1397ad623c1701d2a8 Mon Sep 17 00:00:00 2001\nFrom: Parity <parity@example.com>\nDate: Tue, 14 Nov 2023 22:13:20 +0000\nSubject: [PATCH] second commit\n\nbody of second\n---\n f.txt | 1 +\n\ndiff --git a/f.txt b/f.txt\nindex a29bdeb..c0d0fb4 100644\n--- a/f.txt\n+++ b/f.txt\n@@ -1 +1,2 @@\n line1\n+line2\n-- \n2.54.0\n\n')], ['am', 'p.mbox']),
    ('am-basic-log', [('write', 'f.txt', 'line1\n'), ['add', '-A'], ['commit', '-m', 'init'], ('write', 'p.mbox', 'From b9c5a12fc2eac585f2426f1397ad623c1701d2a8 Mon Sep 17 00:00:00 2001\nFrom: Parity <parity@example.com>\nDate: Tue, 14 Nov 2023 22:13:20 +0000\nSubject: [PATCH] second commit\n\nbody of second\n---\n f.txt | 1 +\n\ndiff --git a/f.txt b/f.txt\nindex a29bdeb..c0d0fb4 100644\n--- a/f.txt\n+++ b/f.txt\n@@ -1 +1,2 @@\n line1\n+line2\n-- \n2.54.0\n\n'), ['am', 'p.mbox']], ['log', '--format=%H %an <%ae> %ad | %s | %b', '--date=raw']),
    ('am-conflict', [('write', 'f.txt', 'different\n'), ['add', '-A'], ['commit', '-m', 'init'], ('write', 'p.mbox', 'From b9c5a12fc2eac585f2426f1397ad623c1701d2a8 Mon Sep 17 00:00:00 2001\nFrom: Parity <parity@example.com>\nDate: Tue, 14 Nov 2023 22:13:20 +0000\nSubject: [PATCH] second commit\n\nbody of second\n---\n f.txt | 1 +\n\ndiff --git a/f.txt b/f.txt\nindex a29bdeb..c0d0fb4 100644\n--- a/f.txt\n+++ b/f.txt\n@@ -1 +1,2 @@\n line1\n+line2\n-- \n2.54.0\n\n')], ['am', 'p.mbox']),
    ('am-conflict-abort', [('write', 'f.txt', 'different\n'), ['add', '-A'], ['commit', '-m', 'init'], ('write', 'p.mbox', 'From b9c5a12fc2eac585f2426f1397ad623c1701d2a8 Mon Sep 17 00:00:00 2001\nFrom: Parity <parity@example.com>\nDate: Tue, 14 Nov 2023 22:13:20 +0000\nSubject: [PATCH] second commit\n\nbody\n---\n f.txt | 1 +\n\ndiff --git a/f.txt b/f.txt\nindex a29bdeb..c0d0fb4 100644\n--- a/f.txt\n+++ b/f.txt\n@@ -1 +1,2 @@\n line1\n+line2\n-- \n2.54.0\n\n'), ['am', 'p.mbox']], ['am', '--abort']),
    ('am-conflict-skip', [('write', 'f.txt', 'different\n'), ['add', '-A'], ['commit', '-m', 'init'], ('write', 'p.mbox', 'From b9c5a12fc2eac585f2426f1397ad623c1701d2a8 Mon Sep 17 00:00:00 2001\nFrom: Parity <parity@example.com>\nDate: Tue, 14 Nov 2023 22:13:20 +0000\nSubject: [PATCH] second commit\n\nbody\n---\n f.txt | 1 +\n\ndiff --git a/f.txt b/f.txt\nindex a29bdeb..c0d0fb4 100644\n--- a/f.txt\n+++ b/f.txt\n@@ -1 +1,2 @@\n line1\n+line2\n-- \n2.54.0\n\n'), ['am', 'p.mbox']], ['am', '--skip']),
    ('am-continue-resolve', [('write', 'f.txt', 'different\n'), ['add', '-A'], ['commit', '-m', 'init'], ('write', 'p.mbox', 'From b9c5a12fc2eac585f2426f1397ad623c1701d2a8 Mon Sep 17 00:00:00 2001\nFrom: Parity <parity@example.com>\nDate: Tue, 14 Nov 2023 22:13:20 +0000\nSubject: [PATCH] second commit\n\nbody of second\n---\n f.txt | 1 +\n\ndiff --git a/f.txt b/f.txt\nindex a29bdeb..c0d0fb4 100644\n--- a/f.txt\n+++ b/f.txt\n@@ -1 +1,2 @@\n line1\n+line2\n-- \n2.54.0\n\n'), ['am', 'p.mbox'], ('write', 'f.txt', 'line1\nline2\n'), ['add', '-A']], ['am', '--continue']),
    ('am-continue-no-changes', [('write', 'f.txt', 'different\n'), ['add', '-A'], ['commit', '-m', 'init'], ('write', 'p.mbox', 'From b9c5a12fc2eac585f2426f1397ad623c1701d2a8 Mon Sep 17 00:00:00 2001\nFrom: Parity <parity@example.com>\nDate: Tue, 14 Nov 2023 22:13:20 +0000\nSubject: [PATCH] second commit\n\nbody\n---\n f.txt | 1 +\n\ndiff --git a/f.txt b/f.txt\nindex a29bdeb..c0d0fb4 100644\n--- a/f.txt\n+++ b/f.txt\n@@ -1 +1,2 @@\n line1\n+line2\n-- \n2.54.0\n\n'), ['am', 'p.mbox']], ['am', '--continue']),
    ('am-show-raw', [('write', 'f.txt', 'different\n'), ['add', '-A'], ['commit', '-m', 'init'], ('write', 'p.mbox', 'From b9c5a12fc2eac585f2426f1397ad623c1701d2a8 Mon Sep 17 00:00:00 2001\nFrom: Parity <parity@example.com>\nDate: Tue, 14 Nov 2023 22:13:20 +0000\nSubject: [PATCH] second commit\n\nbody of second\n---\n f.txt | 1 +\n\ndiff --git a/f.txt b/f.txt\nindex a29bdeb..c0d0fb4 100644\n--- a/f.txt\n+++ b/f.txt\n@@ -1 +1,2 @@\n line1\n+line2\n-- \n2.54.0\n\n'), ['am', 'p.mbox']], ['am', '--show-current-patch=raw']),
    ('am-signoff', [('write', 'f.txt', 'line1\n'), ['add', '-A'], ['commit', '-m', 'init'], ('write', 'p.mbox', 'From b9c5a12fc2eac585f2426f1397ad623c1701d2a8 Mon Sep 17 00:00:00 2001\nFrom: Parity <parity@example.com>\nDate: Tue, 14 Nov 2023 22:13:20 +0000\nSubject: [PATCH] second commit\n\nbody of second\n---\n f.txt | 1 +\n\ndiff --git a/f.txt b/f.txt\nindex a29bdeb..c0d0fb4 100644\n--- a/f.txt\n+++ b/f.txt\n@@ -1 +1,2 @@\n line1\n+line2\n-- \n2.54.0\n\n'), ['am', '-s', 'p.mbox']], ['log', '-1', '--format=%B']),
    ('am-empty-stop', [('write', 'f.txt', 'x\n'), ['add', '-A'], ['commit', '-m', 'init'], ('write', 'e.mbox', 'From 35979d65c898595d969b01edfded6f6e9130c00d Mon Sep 17 00:00:00 2001\nFrom: Parity <parity@example.com>\nDate: Tue, 14 Nov 2023 22:13:20 +0000\nSubject: [PATCH] empty patch subject\n\nempty body\n-- \n2.54.0\n')], ['am', 'e.mbox']),
    ('am-empty-keep-log', [('write', 'f.txt', 'x\n'), ['add', '-A'], ['commit', '-m', 'init'], ('write', 'e.mbox', 'From 35979d65c898595d969b01edfded6f6e9130c00d Mon Sep 17 00:00:00 2001\nFrom: Parity <parity@example.com>\nDate: Tue, 14 Nov 2023 22:13:20 +0000\nSubject: [PATCH] empty patch subject\n\nempty body\n-- \n2.54.0\n'), ['am', '--empty=keep', 'e.mbox']], ['log', '--format=%s|%b']),
    ('am-3way-conflict', [('write', 'f.txt', 'a\nb\nc\nd\ne\nf\ng\nh\n'), ['add', '-A'], ['commit', '-m', 'base'], ('write', 'f.txt', 'a\nb\nC\nD\nE\nf\ng\nh\n'), ['add', '-A'], ['commit', '-m', 'up'], ('write', 'wa.mbox', 'From bad3116cc3e160a3b979456418fa1336c03e014d Mon Sep 17 00:00:00 2001\nFrom: Parity <parity@example.com>\nDate: Tue, 14 Nov 2023 22:13:20 +0000\nSubject: [PATCH] insert X\n\n---\n f.txt | 1 +\n\ndiff --git a/f.txt b/f.txt\nindex 71ac1b5..ca5eeb3 100644\n--- a/f.txt\n+++ b/f.txt\n@@ -2,6 +2,7 @@ a\n b\n c\n d\n+X\n e\n f\n g\n-- \n2.54.0\n')], ['am', '-3', 'wa.mbox']),
    ('am-3way-index', [('write', 'f.txt', 'a\nb\nc\nd\ne\nf\ng\nh\n'), ['add', '-A'], ['commit', '-m', 'base'], ('write', 'f.txt', 'a\nb\nC\nD\nE\nf\ng\nh\n'), ['add', '-A'], ['commit', '-m', 'up'], ('write', 'wa.mbox', 'From bad3116cc3e160a3b979456418fa1336c03e014d Mon Sep 17 00:00:00 2001\nFrom: Parity <parity@example.com>\nDate: Tue, 14 Nov 2023 22:13:20 +0000\nSubject: [PATCH] insert X\n\n---\n f.txt | 1 +\n\ndiff --git a/f.txt b/f.txt\nindex 71ac1b5..ca5eeb3 100644\n--- a/f.txt\n+++ b/f.txt\n@@ -2,6 +2,7 @@ a\n b\n c\n d\n+X\n e\n f\n g\n-- \n2.54.0\n'), ['am', '-3', 'wa.mbox']], ['ls-files', '-s']),
    ('am-binary-tree', [('write', 'del.txt', 'content\n'), ('write', 'ren.txt', 'old\n'), ('write', 'b.bin', ' bin\n'), ['add', '-A'], ['commit', '-m', 'base'], ('write', 'bin.mbox', 'From ff205d13cefb95c223414cf4396366eb87842558 Mon Sep 17 00:00:00 2001\nFrom: Parity <parity@example.com>\nDate: Tue, 14 Nov 2023 22:13:20 +0000\nSubject: [PATCH] change binary\n\n---\n b.bin | Bin\n\ndiff --git a/b.bin b/b.bin\nindex 20f982d561018f3bfdd8f3230d65ded24d81c79c..be50f221a926f4ca621910d18897933ce210d61e 100644\nGIT binary patch\nliteral 8\nPcmezW@2``mpAi=T7^Vae\n\nliteral 7\nOcmZQzWJ=1+;{pH!zyU`9\n\n-- \n2.54.0\n'), ['am', 'bin.mbox']], ['cat-file', '-p', 'HEAD^{tree}']),
    ('am-whitespace-fix', [('write', 'w.txt', 'hello\n'), ['add', '-A'], ['commit', '-m', 'base'], ('write', 'ws.mbox', 'From e79f53051276e28001fdef0e5c3f223f9fee9e58 Mon Sep 17 00:00:00 2001\nFrom: Parity <parity@example.com>\nDate: Tue, 14 Nov 2023 22:13:20 +0000\nSubject: [PATCH] add ws\n\n---\n w.txt | 1 +\n\ndiff --git a/w.txt b/w.txt\nindex ce01362..1e69bc7 100644\n--- a/w.txt\n+++ b/w.txt\n@@ -1 +1,2 @@\n hello\n+world  \n-- \n2.54.0\n')], ['am', '--whitespace=fix', 'ws.mbox']),
    ('am-whitespace-error', [('write', 'w.txt', 'hello\n'), ['add', '-A'], ['commit', '-m', 'base'], ('write', 'ws.mbox', 'From e79f53051276e28001fdef0e5c3f223f9fee9e58 Mon Sep 17 00:00:00 2001\nFrom: Parity <parity@example.com>\nDate: Tue, 14 Nov 2023 22:13:20 +0000\nSubject: [PATCH] add ws\n\n---\n w.txt | 1 +\n\ndiff --git a/w.txt b/w.txt\nindex ce01362..1e69bc7 100644\n--- a/w.txt\n+++ b/w.txt\n@@ -1 +1,2 @@\n hello\n+world  \n-- \n2.54.0\n')], ['am', '--whitespace=error', 'ws.mbox']),
    ('am-reject', [('write', 'f.txt', 'different\n'), ['add', '-A'], ['commit', '-m', 'init'], ('write', 'p.mbox', 'From b9c5a12fc2eac585f2426f1397ad623c1701d2a8 Mon Sep 17 00:00:00 2001\nFrom: Parity <parity@example.com>\nDate: Tue, 14 Nov 2023 22:13:20 +0000\nSubject: [PATCH] second commit\n\nbody\n---\n f.txt | 1 +\n\ndiff --git a/f.txt b/f.txt\nindex a29bdeb..c0d0fb4 100644\n--- a/f.txt\n+++ b/f.txt\n@@ -1 +1,2 @@\n line1\n+line2\n-- \n2.54.0\n\n')], ['am', '--reject', 'p.mbox']),
    ('am-cdiad-dates', [('write', 'a.txt', 'alpha\n'), ['add', '-A'], ['commit', '-m', 'init'], ('write', 'p.mbox', 'From abc1230000000000000000000000000000000000 Mon Sep 17 00:00:00 2001\nFrom: Alice <alice@example.com>\nDate: Sun, 13 Sep 2020 14:26:40 +0000\nSubject: [PATCH] feat thing\n\nbody here\n---\n a.txt | 2 +-\n\ndiff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-alpha\n+ALPHA\n'), ['am', '--committer-date-is-author-date', 'p.mbox']], ['log', '-1', '--format=%an|%ae|%ad|%cn|%cd', '--date=raw']),
    ('am-no-session-continue', [('write', 'f.txt', 'a\n'), ['add', '-A'], ['commit', '-m', 'i']], ['am', '--continue']),
    ('am-bad-empty', [('write', 'f.txt', 'a\n'), ['add', '-A'], ['commit', '-m', 'i']], ['am', '--empty=bogus', 'p.mbox']),
    ('am-bad-patch-format', [('write', 'f.txt', 'a\n'), ['add', '-A'], ['commit', '-m', 'i']], ['am', '--patch-format=bogus']),
    ('am-bad-quoted-cr', [('write', 'f.txt', 'a\n'), ['add', '-A'], ['commit', '-m', 'i']], ['am', '--quoted-cr=bogus', 'p.mbox']),
    ('am-two-cmdmodes', [('write', 'f.txt', 'a\n'), ['add', '-A'], ['commit', '-m', 'i']], ['am', '--skip', '--abort']),
    ('am-interactive-no-patches', [('write', 'f.txt', 'a\n'), ['add', '-A'], ['commit', '-m', 'i']], ['am', '-i']),
    ('am-dirty-index', [('write', 'f.txt', 'line1\n'), ['add', '-A'], ['commit', '-m', 'init'], ('write', 'other.txt', 'x\n'), ['add', 'other.txt'], ('write', 'p.mbox', 'From b9c5a12fc2eac585f2426f1397ad623c1701d2a8 Mon Sep 17 00:00:00 2001\nFrom: Parity <parity@example.com>\nDate: Tue, 14 Nov 2023 22:13:20 +0000\nSubject: [PATCH] second commit\n\nbody\n---\n f.txt | 1 +\n\ndiff --git a/f.txt b/f.txt\nindex a29bdeb..c0d0fb4 100644\n--- a/f.txt\n+++ b/f.txt\n@@ -1 +1,2 @@\n line1\n+line2\n-- \n2.54.0\n\n')], ['am', 'p.mbox']),
    ('mailsplit-multi', [('write', 'm.mbox', 'From 88cc9bfe7e17d8a3e461d18654b86332d751d767 Mon Sep 17 00:00:00 2001\nFrom: Parity <parity@example.com>\nSubject: [PATCH 1/2] one\n\nbody1\n\nFrom ef28e0a3abb4d3111ce6da1f6acdabc3680247e5 Mon Sep 17 00:00:00 2001\nFrom: Parity <parity@example.com>\nSubject: [PATCH 2/2] two\n\nbody2\n')], ['mailsplit', '-osplit', '-b', 'm.mbox']),
]

BATCH11_ENG_STDIN_CASES = [
    ('am-stdin', [('write', 'f.txt', 'line1\n'), ['add', '-A'], ['commit', '-m', 'init']], ['am'], 'From b9c5a12fc2eac585f2426f1397ad623c1701d2a8 Mon Sep 17 00:00:00 2001\nFrom: Parity <parity@example.com>\nDate: Tue, 14 Nov 2023 22:13:20 +0000\nSubject: [PATCH] second commit\n\nbody of second\n---\n f.txt | 1 +\n\ndiff --git a/f.txt b/f.txt\nindex a29bdeb..c0d0fb4 100644\n--- a/f.txt\n+++ b/f.txt\n@@ -1 +1,2 @@\n line1\n+line2\n-- \n2.54.0\n\n'),
    ('repack -a -d collapses loose into one pack, deletes loose', [('write', 'a.txt', 'hello\n'), ['add', 'a.txt'], ['commit', '-m', 'first'], ('write', 'b.txt', 'world\n'), ['add', 'b.txt'], ['commit', '-m', 'second'], ['repack', '-a', '-d']], ['count-objects', '-v'], ''),
    ('plain repack is incremental (keeps loose objects, prune-packable nonzero)', [('write', 'a.txt', 'hi\n'), ['add', 'a.txt'], ['commit', '-m', 'c1'], ['repack']], ['count-objects', '-v'], ''),
    ('repack -n is no-update-server-info, not dry-run', [('write', 'a.txt', 'hi\n'), ['add', 'a.txt'], ['commit', '-m', 'c1'], ['repack', '-n']], ['count-objects', '-v'], ''),
    ('repack -d on already-fully-packed repo prints Nothing new to pack', [('write', 'a.txt', 'hi\n'), ['add', 'a.txt'], ['commit', '-m', 'c1'], ['repack', '-a', '-d']], ['repack', '-d'], ''),
    ('repack -q -d on packed repo suppresses Nothing new to pack', [('write', 'a.txt', 'hi\n'), ['add', 'a.txt'], ['commit', '-m', 'c1'], ['repack', '-a', '-d']], ['repack', '-q', '-d'], ''),
    ('repack -a -d -k folds unreachable into single pack (no loose)', [('write', 'a.txt', 'hi\n'), ['add', 'a.txt'], ['commit', '-m', 'c1'], ('write', 'c.txt', 'extra\n'), ['add', 'c.txt'], ['commit', '-m', 'c2'], ['reset', '--hard', 'HEAD~1'], ['reflog', 'expire', '--expire=now', '--all'], ['repack', '-a', '-d', '-k']], ['count-objects', '-v'], ''),
    ('repack -A -d packs reachable, loosens unreachable', [('write', 'a.txt', 'hi\n'), ['add', 'a.txt'], ['commit', '-m', 'c1'], ('write', 'c.txt', 'extra\n'), ['add', 'c.txt'], ['commit', '-m', 'c2'], ['reset', '--hard', 'HEAD~1'], ['reflog', 'expire', '--expire=now', '--all'], ['repack', '-A', '-d']], ['count-objects', '-v'], ''),
    ('repack --cruft -d --cruft-expiration=now drops old unreachable from cruft', [('write', 'a.txt', 'hi\n'), ['add', 'a.txt'], ['commit', '-m', 'c1'], ('write', 'c.txt', 'extra\n'), ['add', 'c.txt'], ['commit', '-m', 'c2'], ['reset', '--hard', 'HEAD~1'], ['reflog', 'expire', '--expire=now', '--all'], ['repack', '--cruft', '-d', '--cruft-expiration=now']], ['count-objects', '-v'], ''),
    ('repack --geometric=2 -d combines packs, preserves object set', [('write', 'a.txt', 'hi\n'), ['add', 'a.txt'], ['commit', '-m', 'c1'], ['repack', '-d'], ('write', 'a.txt', 'hi\nworld\n'), ['add', 'a.txt'], ['commit', '-m', 'c2'], ['repack', '-d'], ['repack', '--geometric=2', '-d']], ['count-objects', '-v'], ''),
    ('repack -a -d --max-pack-size below 1MiB warns to stderr', [('write', 'a.txt', 'hi\n'), ['add', 'a.txt'], ['commit', '-m', 'c1']], ['repack', '-a', '-d', '--max-pack-size=100'], ''),
    ('repack -a -d -q --max-pack-size still warns (warning not gated by -q)', [('write', 'a.txt', 'hi\n'), ['add', 'a.txt'], ['commit', '-m', 'c1']], ['repack', '-a', '-d', '-q', '--max-pack-size=100'], ''),
    ('repack -a -d -m writes a valid multi-pack-index', [('write', 'a.txt', 'hi\n'), ['add', 'a.txt'], ['commit', '-m', 'c1'], ['repack', '-a', '-d', '-m']], ['multi-pack-index', 'verify'], ''),
    ('repack -a -d preserves full object set (cat-file batch-all-objects)', [('write', 'a.txt', 'hi\n'), ['add', 'a.txt'], ['commit', '-m', 'c1'], ('write', 'b.txt', 'yo\n'), ['add', 'b.txt'], ['commit', '-m', 'c2'], ['repack', '-a', '-d']], ['cat-file', '--batch-all-objects', '--batch-check'], ''),
    ('repack --geometric=2 -a is incompatible (fatal rc 128)', [('write', 'a.txt', 'hi\n'), ['add', 'a.txt'], ['commit', '-m', 'c1']], ['repack', '--geometric=2', '-a'], ''),
    ('repack -A -k incompatible (fatal rc 128)', [('write', 'a.txt', 'hi\n'), ['add', 'a.txt'], ['commit', '-m', 'c1']], ['repack', '-A', '-k'], ''),
    ('repack -k --cruft incompatible (fatal rc 128)', [('write', 'a.txt', 'hi\n'), ['add', 'a.txt'], ['commit', '-m', 'c1']], ['repack', '-k', '--cruft'], ''),
    ('repack --bogus unknown option prints error + usage (rc 129)', [('write', 'a.txt', 'hi\n'), ['add', 'a.txt'], ['commit', '-m', 'c1']], ['repack', '--bogus'], ''),
    ('repack --window without value prints error only (rc 129, no usage)', [('write', 'a.txt', 'hi\n'), ['add', 'a.txt'], ['commit', '-m', 'c1']], ['repack', '--window'], ''),
    ('repack --geometric=abc expects integer (rc 129, no usage)', [('write', 'a.txt', 'hi\n'), ['add', 'a.txt'], ['commit', '-m', 'c1']], ['repack', '--geometric=abc'], ''),
    ('repack --max-pack-size=abc non-negative integer error (rc 129)', [('write', 'a.txt', 'hi\n'), ['add', 'a.txt'], ['commit', '-m', 'c1']], ['repack', '--max-pack-size=abc'], ''),
    ('repack --missing rejected as unknown option (rc 129)', [('write', 'a.txt', 'hi\n'), ['add', 'a.txt'], ['commit', '-m', 'c1']], ['repack', '--missing=allow-any'], ''),
    ('repack --filter-to without --filter is fatal (rc 128)', [('write', 'a.txt', 'hi\n'), ['add', 'a.txt'], ['commit', '-m', 'c1']], ['repack', '--filter-to=x'], ''),
    ('repack -h prints usage to stdout (rc 129)', [], ['repack', '-h'], ''),
    ('repack -a -d --window/--depth/--threads accepted, same object result', [('write', 'a.txt', 'hi\n'), ['add', 'a.txt'], ['commit', '-m', 'c1'], ['repack', '-a', '-d', '--window=10', '--depth=5', '--threads=2']], ['rev-list', '--all', '--count'], ''),
    ('repack -a -d on empty repo writes nothing, no pack', [['repack', '-a', '-d']], ['count-objects', '-v'], ''),
    ('help_flag', [], ['fast-import', '-h'], ''),
    ('positional_usage', [], ['fast-import', 'extra'], ''),
]

@pytest.mark.parametrize("case", BATCH11_ENG_CASES, ids=[c[0] for c in BATCH11_ENG_CASES])
def test_batch11_eng_parity(case, tmp_path: Path, git_254_oracle: str):
    _id, setup, probe = case
    assert_command_parity(git_254_oracle, tmp_path, setup, probe)


@pytest.mark.parametrize("case", BATCH11_ENG_STDIN_CASES, ids=[c[0] for c in BATCH11_ENG_STDIN_CASES])
def test_batch11_eng_stdin_parity(case, tmp_path: Path, git_254_oracle: str):
    _id, setup, probe, stdin = case
    assert_command_parity(git_254_oracle, tmp_path, setup, probe, stdin=stdin)


# repack with --cruft / --filter produces multiple packs whose exact byte sizes
# differ between git and pygit (different delta/compression), so count-objects's
# `size`/`size-pack` KiB fields are not byte-lockable. These verify every OTHER
# count-objects field (count/in-pack/packs/prune-packable/garbage) — i.e. the
# object set and pack topology — with the two size lines normalized away.
BATCH11_REPACK_CO_CASES = [
    ("repack-cruft-d-two-packs",
     [("write", "a.txt", "hi\n"), ["add", "a.txt"], ["commit", "-m", "c1"],
      ("write", "c.txt", "extra\n"), ["add", "c.txt"], ["commit", "-m", "c2"],
      ["reset", "--hard", "HEAD~1"], ["reflog", "expire", "--expire=now", "--all"],
      ["repack", "--cruft", "-d"]],
     ["count-objects", "-v"]),
    ("repack-filter-blob-none-two-packs",
     [("write", "a.txt", "hi\n"), ["add", "a.txt"], ["commit", "-m", "c1"],
      ("write", "d.txt", "deep\n"), ["add", "d.txt"], ["commit", "-m", "c2"],
      ["repack", "-a", "-d"], ["repack", "--filter=blob:none", "-a", "-d"]],
     ["count-objects", "-v"]),
]


@pytest.mark.parametrize("case", BATCH11_REPACK_CO_CASES, ids=[c[0] for c in BATCH11_REPACK_CO_CASES])
def test_batch11_repack_countobjects_parity(case, tmp_path: Path, git_254_oracle: str):
    import re
    import subprocess
    from tests.git_parity.support import DETERMINISTIC_ENV, ROOT, pygit_cmd

    _id, setup, probe = case
    env = dict(__import__("os").environ)
    env.update(DETERMINISTIC_ENV)
    env["PYTHONPATH"] = str(ROOT)

    def norm(s: str) -> str:
        # pack/loose KiB sizes are not byte-reproducible across implementations
        s = re.sub(r"(?m)^(size|size-pack|size-garbage): \d+$", r"\1: X", s)
        return s

    results = {}
    for tool, base in (("oracle", [git_254_oracle]), ("pygit", pygit_cmd())):
        repo = tmp_path / tool
        repo.mkdir()
        subprocess.run([*base, "init", "-b", "main", "."], cwd=repo, env=env, capture_output=True)
        for step in setup:
            if isinstance(step, tuple) and step and step[0] == "write":
                (repo / step[1]).write_text(step[2])
            else:
                subprocess.run([*base, *step], cwd=repo, env=env, capture_output=True)
        proc = subprocess.run([*base, *probe], cwd=repo, env=env, text=True, capture_output=True)
        results[tool] = (proc.returncode, norm(proc.stdout), proc.stderr)

    assert results["pygit"] == results["oracle"]


# fast-import's default statistics block (builtin/fast-import.c dump_stats) is
# byte-exact for every structural field (Alloc'd/Total/per-type counts, branches,
# marks, atoms, pack_report) EXCEPT two genuinely non-reproducible elements: the
# Memory/pools/objects KiB lines (this Python process vs git's C heap) and the
# "N deltas of M attempts" columns (git deltifies during packing; we store full
# objects). These lock the whole block with only those two normalized, which also
# exercises the --date-format/--export-marks/--max-pack-size/--big-file-threshold/
# --depth/--active-branches flags through the deterministic path.
_FI_LINEAR = ("blob\nmark :1\ndata 2\na\n\ncommit refs/heads/main\nmark :2\n"
              "committer P <p@e> 1700000000 +0000\ndata 2\nx\nM 100644 :1 f\n\n")
_FI_SUBDIR = ("blob\nmark :1\ndata 2\na\n\ncommit refs/heads/main\nmark :2\n"
              "committer P <p@e> 1700000000 +0000\ndata 2\nx\nM 100644 :1 dir/a\n\n"
              "blob\nmark :3\ndata 2\nb\n\ncommit refs/heads/main\nmark :4\n"
              "committer P <p@e> 1700000000 +0000\ndata 2\ny\nM 100644 :3 dir/b\n\n")
BATCH12_FASTIMPORT_STATS_CASES = [
    ("fi-stats-empty", ["fast-import"], ""),
    ("fi-stats-linear", ["fast-import"], _FI_LINEAR),
    ("fi-stats-subdir", ["fast-import"], _FI_SUBDIR),
    ("fi-date-format-raw", ["fast-import", "--date-format=raw"], _FI_LINEAR),
    ("fi-max-pack-size", ["fast-import", "--max-pack-size=1m"], _FI_SUBDIR),
    ("fi-big-file-threshold", ["fast-import", "--big-file-threshold=1m"], _FI_SUBDIR),
    ("fi-depth", ["fast-import", "--depth=10"], _FI_SUBDIR),
    ("fi-active-branches", ["fast-import", "--active-branches=5"], _FI_SUBDIR),
    ("fi-export-marks", ["fast-import", "--export-marks=marks.txt"], _FI_LINEAR),
]


@pytest.mark.parametrize("case", BATCH12_FASTIMPORT_STATS_CASES,
                         ids=[c[0] for c in BATCH12_FASTIMPORT_STATS_CASES])
def test_batch12_fastimport_stats_parity(case, tmp_path: Path, git_254_oracle: str):
    import re
    import subprocess
    from tests.git_parity.support import DETERMINISTIC_ENV, ROOT, pygit_cmd

    _id, probe, stdin = case
    env = dict(__import__("os").environ)
    env.update(DETERMINISTIC_ENV)
    env["PYTHONPATH"] = str(ROOT)

    def norm(s: str) -> str:
        s = re.sub(r"(?m)^(Memory total|       pools|     objects):.*$", r"\1: X", s)
        s = re.sub(r"\d+ deltas of\s+\d+ attempts", "D deltas of A attempts", s)
        return s

    def snap(repo: Path):
        gd = repo / ".git"
        refs = sorted((str(p.relative_to(gd)).replace("\\", "/"), p.read_text())
                      for p in (gd / "refs").rglob("*") if p.is_file())
        return refs

    results = {}
    for tool, base in (("oracle", [git_254_oracle]), ("pygit", pygit_cmd())):
        repo = tmp_path / tool
        repo.mkdir()
        subprocess.run([*base, "init", "-b", "main", "."], cwd=repo, env=env, capture_output=True)
        proc = subprocess.run([*base, *probe], cwd=repo, env=env, input=stdin,
                              text=True, capture_output=True)
        results[tool] = (proc.returncode, norm(proc.stdout), norm(proc.stderr), snap(repo))

    assert results["pygit"] == results["oracle"]


BATCH13_CASES = [
    ('diff -b ignores trailing-space-only change', [('write', 'w.txt', 'alpha\nbeta\ngamma\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'w.txt', 'alpha\nbeta \ngamma\n')], ['diff', '-b']),
    ('diff -b --exit-code rc=0 for ws-only change', [('write', 'w.txt', 'alpha\nbeta\ngamma\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'w.txt', 'alpha\nbeta \ngamma\n')], ['diff', '-b', '--exit-code']),
    ('diff -w internal whitespace ignored', [('write', 'w.txt', 'a b\nc\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'w.txt', 'a  b\nc\n')], ['diff', '-w']),
    ('diff --no-prefix', [('write', 'a.txt', 'line1\nline2\nline3\nline4\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'x\n')], ['diff', '--no-prefix']),
    ('diff --src-prefix/--dst-prefix', [('write', 'a.txt', 'line1\nline2\nline3\nline4\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'x\n')], ['diff', '--src-prefix=A/', '--dst-prefix=B/']),
    ('diff --no-prefix then --src-prefix order wins', [('write', 'a.txt', 'line1\nline2\nline3\nline4\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'x\n')], ['diff', '--no-prefix', '--src-prefix=Z/']),
    ('diff --full-index', [('write', 'a.txt', 'line1\nline2\nline3\nline4\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'x\n')], ['diff', '--full-index']),
    ('diff --abbrev=8', [('write', 'a.txt', 'line1\nline2\nline3\nline4\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'x\n')], ['diff', '--abbrev=8']),
    ('diff --compact-summary', [('write', 'a.txt', 'line1\nline2\nline3\nline4\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'x\n')], ['diff', '--compact-summary']),
    ('diff --word-diff-regex', [('write', 'a.txt', 'line1\nline2\nline3\nline4\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'x\n')], ['diff', '--word-diff-regex=.']),
    ('log %(trailers)', [('write', 'a.txt', 'l1\n'), ['add', '-A'], ['commit', '-q', '-m', 'subject\n\nSigned-off-by: Parity <parity@example.com>\nReviewed-by: Foo <foo@example.com>']], ['log', '-1', '--format=%(trailers)']),
    ('log %(trailers:key=,valueonly)', [('write', 'a.txt', 'l1\n'), ['add', '-A'], ['commit', '-q', '-m', 'subject\n\nSigned-off-by: Parity <parity@example.com>\nReviewed-by: Foo <foo@example.com>']], ['log', '-1', '--format=%(trailers:key=Reviewed-by,valueonly)']),
    ('log %(trailers:keyonly)', [('write', 'a.txt', 'l1\n'), ['add', '-A'], ['commit', '-q', '-m', 'subject\n\nSigned-off-by: Parity <parity@example.com>\nReviewed-by: Foo <foo@example.com>']], ['log', '-1', '--format=%(trailers:keyonly)']),
    ('log --patch --no-patch --oneline override', [('write', 'a.txt', 'line1\nline2\nline3\n'), ['add', '-A'], ['commit', '-q', '-m', 'first commit'], ('write', 'a.txt', 'line1\nline2 changed\nline3\nline4\n'), ['add', '-A'], ['commit', '-q', '-m', 'second commit']], ['log', '--patch', '--no-patch', '--oneline']),
    ('log -p -q keeps patch (q hoisted)', [('write', 'f.txt', 'a\nb\nc\n'), ['add', 'f.txt'], ['commit', '-m', 'first'], ('write', 'f.txt', 'a\nB\nc\n'), ['add', 'f.txt'], ['commit', '-m', 'second']], ['log', '-p', '-q', '-n1']),
    ('log -p -s suppresses patch', [('write', 'f.txt', 'a\nb\nc\n'), ['add', 'f.txt'], ['commit', '-m', 'first'], ('write', 'f.txt', 'a\nB\nc\n'), ['add', 'f.txt'], ['commit', '-m', 'second']], ['log', '-p', '-s', '-n1']),
    ('log --left-right --oneline', [('write', 'a.txt', 'line1\n'), ['add', '-A'], ['commit', '-q', '-m', 'first commit'], ('write', 'a.txt', 'line2\n'), ['add', '-A'], ['commit', '-q', '-m', 'second commit']], ['log', '--left-right', '--oneline']),
    ('log --left-right verbose header', [('write', 'a.txt', 'line1\n'), ['add', '-A'], ['commit', '-q', '-m', 'first commit'], ('write', 'a.txt', 'line2\n'), ['add', '-A'], ['commit', '-q', '-m', 'second commit']], ['log', '--left-right', '-1']),
    ('log --format= -p -1 no spurious blanks', [('write', 'a.txt', 'l1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'l1\nl2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2']], ['log', '--format=', '-p', '-1']),
    ('log --format= --stat -1', [('write', 'a.txt', 'l1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'l1\nl2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2']], ['log', '--format=', '--stat', '-1']),
    ('log --raw --no-abbrev -1 --oneline', [('write', 'a.txt', 'l1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'l1\nl2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2']], ['log', '--raw', '--no-abbrev', '-1', '--oneline']),
    ('log --raw --abbrev=8 -1 --oneline', [('write', 'a.txt', 'l1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'l1\nl2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2']], ['log', '--raw', '--abbrev=8', '-1', '--oneline']),
    ('log -p --stat uses --- separator', [('write', 'a.txt', 'l1\nl2\nl3\n'), ['add', '-A'], ['commit', '-q', '-m', 'first'], ('write', 'a.txt', 'l1\nchg\nl3\nl4\n'), ['add', '-A'], ['commit', '-q', '-m', 'second']], ['log', '-p', '--stat', '-1']),
    ('log -q --raw --name-status name-status wins', [('write', 'f.txt', 'a\nb\nc\n'), ['add', 'f.txt'], ['commit', '-m', 'first'], ('write', 'f.txt', 'a\nB\nc\n'), ['add', 'f.txt'], ['commit', '-m', 'second']], ['log', '-q', '--raw', '--name-status', '-n1']),
    ('log --name-only --name-status conflict rc128', [('write', 'f.txt', 'a\nb\n'), ['add', 'f.txt'], ['commit', '-m', 'first'], ('write', 'f.txt', 'a\nB\n'), ['add', 'f.txt'], ['commit', '-m', 'second']], ['log', '--name-only', '--name-status', '-n1']),
    ('show --format=%H blank line before diff', [('write', 'a.txt', 'l1\nl2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'l1\nX\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2']], ['show', '--format=%H']),
    ('show --format=X%nY blank line before diff', [('write', 'a.txt', 'l1\nl2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'l1\nX\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2']], ['show', '--format=X%nY']),
    ('show --stat --no-patch suppresses stat', [('write', 'a.txt', 'line1\nline2\nline3\n'), ['add', '-A'], ['commit', '-q', '-m', 'first'], ('write', 'a.txt', 'line1\nchg\nline3\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2']], ['show', '--stat', '--no-patch']),
    ('show -p -q keeps patch (q hoisted)', [('write', 'f.txt', 'a\nb\nc\n'), ['add', 'f.txt'], ['commit', '-m', 'first'], ('write', 'f.txt', 'a\nB\nc\n'), ['add', 'f.txt'], ['commit', '-m', 'second']], ['show', '-p', '-q']),
    ('show -p -s suppresses patch', [('write', 'f.txt', 'a\nb\nc\n'), ['add', 'f.txt'], ['commit', '-m', 'first'], ('write', 'f.txt', 'a\nB\nc\n'), ['add', 'f.txt'], ['commit', '-m', 'second']], ['show', '-p', '-s']),
    ('status -z implies porcelain NUL', [('write', 'a.txt', 'a\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'b\n'), ('write', 'u.txt', 'new\n')], ['status', '-z']),
    ('status --null long form', [('write', 'a.txt', 'a\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'b\n'), ('write', 'u.txt', 'new\n')], ['status', '--null']),
    ('status --long -z conflict rc128', [('write', 'a.txt', 'a\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'b\n')], ['status', '--long', '-z']),
    ('whatchanged --no-abbrev raw blob shas', [('write', 'a.txt', 'line1\nline2\nline3\n'), ['add', '-A'], ['commit', '-q', '-m', 'first commit'], ('write', 'a.txt', 'line1\nline2 changed\nline3\nline4\n'), ['add', '-A'], ['commit', '-q', '-m', 'second commit']], ['whatchanged', '--i-still-use-this', '--no-abbrev']),
    ('whatchanged --abbrev=8', [('write', 'a.txt', 'line1\nline2\nline3\n'), ['add', '-A'], ['commit', '-q', '-m', 'first commit'], ('write', 'a.txt', 'line1\nline2 changed\nline3\nline4\n'), ['add', '-A'], ['commit', '-q', '-m', 'second commit']], ['whatchanged', '--i-still-use-this', '--abbrev=8']),
    ('grep -H forces filename', [('write', 'f1.txt', 'apple\nBanana\ncherry apple\n'), ('write', 'sub/f3.txt', 'apple sauce\nkiwi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['grep', '-H', 'apple']),
    ('grep -h suppresses filename', [('write', 'f1.txt', 'apple\nBanana\ncherry apple\n'), ('write', 'sub/f3.txt', 'apple sauce\nkiwi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['grep', '-h', 'apple']),
    ('grep -o only matching', [('write', 'f1.txt', 'apple apple\nBanana\ncherry apple\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['grep', '-o', 'apple']),
    ('grep -L files without match rc1', [('write', 'f1.txt', 'apple\nBanana\ncherry apple\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['grep', '-L', 'apple']),
    ('grep --name-only', [('write', 'f1.txt', 'apple\nBanana\ncherry apple\n'), ('write', 'sub/f3.txt', 'apple sauce\nkiwi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['grep', '--name-only', 'apple']),
    ('grep --column', [('write', 'f1.txt', 'apple\nBanana\ncherry apple\n'), ('write', 'sub/f3.txt', 'apple sauce\nkiwi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['grep', '--column', 'apple']),
    ('grep -e explicit pattern', [('write', 'f1.txt', 'apple\nBanana\ncherry apple\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['grep', '-e', 'apple']),
    ('grep -e multi-pattern', [('write', 'f1.txt', 'apple\nBanana\ncherry apple\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['grep', '-e', 'apple', '-e', 'Banana']),
    ('grep -q quiet match rc0', [('write', 'f1.txt', 'apple\nBanana\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['grep', '-q', 'apple']),
    ('grep -q quiet no-match rc1', [('write', 'f1.txt', 'apple\nBanana\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['grep', '-q', 'nonexistent']),
    ('grep -c count per file', [('write', 'f1.txt', 'apple\ncherry apple\n'), ('write', 'sub/f3.txt', 'apple sauce\nkiwi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['grep', '-c', 'apple']),
    ('grep -n line numbers', [('write', 'f1.txt', 'apple\nBanana\ncherry apple\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['grep', '-n', 'apple']),
    ('grep -A1 after-context', [('write', 'f1.txt', 'apple\nBanana\ncherry apple\ndate\nfig\n'), ('write', 'sub/f3.txt', 'apple sauce\nkiwi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['grep', '-A1', 'apple']),
    ('grep --heading', [('write', 'f1.txt', 'apple\nBanana\ncherry apple\n'), ('write', 'sub/f3.txt', 'apple sauce\nkiwi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['grep', '--heading', 'apple']),
    ('grep --break', [('write', 'f1.txt', 'apple\nBanana\ncherry apple\n'), ('write', 'sub/f3.txt', 'apple sauce\nkiwi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['grep', '--break', 'apple']),
    ('commit --cleanup=strip drops comment lines (message bytes)', [('write', 'a.txt', 'a\n'), ['add', 'a.txt'], ['commit', '-m', 'first'], ('write', 'b.txt', 'b\n'), ['add', 'b.txt'], ['commit', '-m', 'subject\n\n# comment line\nbody', '--cleanup=strip']], ['cat-file', 'commit', 'HEAD']),
    ('commit -m multi-line subject summary fold', [('write', 'a.txt', 'a\n'), ['add', 'a.txt'], ['commit', '-m', 'first'], ('write', 'b.txt', 'b\n'), ['add', 'b.txt']], ['commit', '-m', 'line one\nline two\nline three']),
    ('commit --fixup reword: leaves staged content (status)', [('write', 'a.txt', 'a\n'), ['add', 'a.txt'], ['commit', '-m', 'first'], ('write', 'b.txt', 'b\n'), ['add', 'b.txt'], ['commit', '--fixup', 'reword:HEAD']], ['status', '--short']),
    ('commit --fixup reword: makes empty amend! commit (message bytes)', [('write', 'a.txt', 'a\n'), ['add', 'a.txt'], ['commit', '-m', 'first'], ('write', 'b.txt', 'b\n'), ['add', 'b.txt'], ['commit', '--fixup', 'reword:HEAD']], ['cat-file', 'commit', 'HEAD']),
    ('checkout -B resets existing branch', [('write', 'a.txt', 'a\n'), ['add', 'a.txt'], ['commit', '-m', 'first'], ('write', 'sub/s.txt', 's\n'), ['add', 'sub/s.txt'], ['commit', '-m', 'second'], ('write', 'a.txt', 'a\nb\n'), ['add', 'a.txt'], ['commit', '-m', 'third'], ['tag', 'v1'], ['branch', 'feature']], ['checkout', '-B', 'feature', 'v1']),
    ('checkout --detach suppresses advice', [('write', 'a.txt', 'a\n'), ['add', 'a.txt'], ['commit', '-m', 'first'], ('write', 'sub/s.txt', 's\n'), ['add', 'sub/s.txt'], ['commit', '-m', 'second'], ('write', 'a.txt', 'a\nb\n'), ['add', 'a.txt'], ['commit', '-m', 'third']], ['checkout', '--detach', 'HEAD~1']),
    ('cherry-pick -n applies without committing (status)', [('write', 'a.txt', 'base\n'), ['add', 'a.txt'], ['commit', '-m', 'base'], ['checkout', '-b', 'feature'], ('write', 'f.txt', 'feat\n'), ['add', 'f.txt'], ['commit', '-m', 'feature commit'], ['checkout', 'main'], ['cherry-pick', '-n', 'feature']], ['status', '--short']),
    ('cherry-pick -n does not add a commit (log)', [('write', 'a.txt', 'base\n'), ['add', 'a.txt'], ['commit', '-m', 'base'], ['checkout', '-b', 'feature'], ('write', 'f.txt', 'feat\n'), ['add', 'f.txt'], ['commit', '-m', 'feature commit'], ['checkout', 'main'], ['cherry-pick', '-n', 'feature']], ['log', '--oneline']),
    ('revert -n stages without committing (status)', [('write', 'a.txt', 'base\n'), ['add', 'a.txt'], ['commit', '-m', 'base'], ('write', 'b.txt', 'more\n'), ['add', 'b.txt'], ['commit', '-m', 'add b'], ['revert', '-n', 'HEAD']], ['status', '--short']),
    ('revert -n does not add a commit (log)', [('write', 'a.txt', 'base\n'), ['add', 'a.txt'], ['commit', '-m', 'base'], ('write', 'b.txt', 'more\n'), ['add', 'b.txt'], ['commit', '-m', 'add b'], ['revert', '-n', 'HEAD']], ['log', '--oneline']),
    ('describe --candidates=0 fails on non-exact commit', [('write', 'a.txt', 'a\n'), ['add', 'a.txt'], ['commit', '-m', 'c1'], ['tag', '-a', 'v1.0', '-m', 'release 1.0'], ('write', 'a.txt', 'a\nb\n'), ['add', 'a.txt'], ['commit', '-m', 'c2'], ('write', 'a.txt', 'a\nb\nc\n'), ['add', 'a.txt'], ['commit', '-m', 'c3'], ['tag', 'light'], ('write', 'a.txt', 'a\nb\nc\nd\n'), ['add', 'a.txt'], ['commit', '-m', 'c4']], ['describe', '--candidates=0']),
    ('describe --candidates=0 succeeds on exact tag', [('write', 'a.txt', 'a\n'), ['add', 'a.txt'], ['commit', '-m', 'c1'], ['tag', '-a', 'v1.0', '-m', 'release 1.0']], ['describe', '--candidates=0', 'v1.0']),
    ('merge --ff-only diverging emits advice hint block', [('write', 'a.txt', 'base\n'), ['add', 'a.txt'], ['commit', '-m', 'base'], ['branch', 'other'], ('write', 'a.txt', 'base\nmain\n'), ['add', 'a.txt'], ['commit', '-m', 'main change'], ['checkout', 'other'], ('write', 'b.txt', 'other\n'), ['add', 'b.txt'], ['commit', '-m', 'other change'], ['checkout', 'master']], ['merge', '--ff-only', 'other']),
    ('branch --set-upstream-to a tag is not a branch', [('write', 'a.txt', 'a\n'), ['add', 'a.txt'], ['commit', '-m', 'first'], ('write', 'a.txt', 'a\nb\n'), ['add', 'a.txt'], ['commit', '-m', 'third'], ['tag', 'v1'], ['branch', 'feature']], ['branch', '--set-upstream-to=v1', 'feature']),
    ('branch --set-upstream-to a real branch still succeeds', [('write', 'a.txt', 'a\n'), ['add', 'a.txt'], ['commit', '-m', 'first'], ['branch', 'base'], ['branch', 'feature']], ['branch', '--set-upstream-to=base', 'feature']),
]

BATCH13_STDIN_CASES = [
    ("add -n labels a staged deletion 'remove'", [('write', 'f.txt', 'a'), ['add', 'f.txt'], ['commit', '-q', '-m', 'init'], ('rm', 'f.txt')], ['add', '-n', 'f.txt'], ''),
    ('add of an ignored file errors with advice (rc 1)', [('write', 'f.txt', 'a\nb\nc\n'), ['add', 'f.txt'], ['commit', '-q', '-m', 'init'], ('write', '.gitignore', '*.log\n'), ['add', '.gitignore'], ['commit', '-q', '-m', 'ignore'], ('write', 'skip.log', 'x')], ['add', 'skip.log'], ''),
    ('rm refuses a locally-modified file', [('write', 'f.txt', 'a\nb\nc\n'), ['add', 'f.txt'], ['commit', '-q', '-m', 'init'], ('write', 'f.txt', 'MOD')], ['rm', 'f.txt'], ''),
    ('rm refuses a file with staged content differing from both', [('write', 'f.txt', 'a\nb\nc\n'), ['add', 'f.txt'], ['commit', '-q', '-m', 'init'], ('write', 'f.txt', 'STG\n'), ['add', 'f.txt'], ('write', 'f.txt', 'WT\n')], ['rm', 'f.txt'], ''),
    ('mv onto an existing tracked destination is refused', [('write', 'f.txt', 'a\nb\nc\n'), ['add', 'f.txt'], ['commit', '-q', '-m', 'init'], ('write', 'other.txt', 'y'), ['add', 'other.txt']], ['mv', 'f.txt', 'other.txt'], ''),
    ('mv into an existing directory then ls-files', [('write', 'f.txt', 'a\nb\nc\n'), ('write', 'sub/g.txt', 'x'), ['add', 'f.txt', 'sub/g.txt'], ['commit', '-q', '-m', 'init'], ['mv', 'f.txt', 'sub']], ['ls-files'], ''),
    ('mv with a bad source', [('write', 'f.txt', 'a'), ['add', 'f.txt'], ['commit', '-q', '-m', 'init']], ['mv', 'nope.txt', 'dest.txt'], ''),
    ('stash apply stash@{0} prints status', [('write', 'f.txt', 'a\n'), ['add', 'f.txt'], ['commit', '-q', '-m', 'init'], ('write', 'f.txt', 'b\n'), ['stash']], ['stash', 'apply', 'stash@{0}'], ''),
    ('stash apply prints the post-restore status summary', [('write', 'f.txt', 'a\n'), ['add', 'f.txt'], ['commit', '-q', '-m', 'init'], ('write', 'f.txt', 'b\n'), ['stash']], ['stash', 'apply'], ''),
    ('stash pop prints status + Dropped and clears the stash', [('write', 'f.txt', 'a\n'), ['add', 'f.txt'], ['commit', '-q', '-m', 'init'], ('write', 'f.txt', 'b\n'), ['stash']], ['stash', 'pop'], ''),
    ('stash show on an empty stash', [('write', 'f.txt', 'a\nb\nc\n'), ['add', 'f.txt'], ['commit', '-q', '-m', 'init'], ('write', 'f.txt', 'b\n')], ['stash', 'show'], ''),
    ('stash clear on an empty stash is a no-op', [('write', 'f.txt', 'a'), ['add', 'f.txt'], ['commit', '-q', '-m', 'init']], ['stash', 'clear'], ''),
    ('stash drop on an empty stash', [('write', 'f.txt', 'a'), ['add', 'f.txt'], ['commit', '-q', '-m', 'init']], ['stash', 'drop'], ''),
    ('stash apply --index restores staged content (diff --cached)', [('write', 'f.txt', 'a\nb\nc\n'), ['add', 'f.txt'], ['commit', '-q', '-m', 'init'], ('write', 'f.txt', 'STG'), ['add', 'f.txt'], ('write', 'f.txt', 'WT'), ['stash'], ['stash', 'apply', '--index']], ['diff', '--cached'], ''),
    ('clean -fd keeps a nested git repo', [('write', 'tracked.txt', 'x'), ['add', 'tracked.txt'], ['commit', '-q', '-m', 'init'], ('write', 'untr.txt', 'a'), ['shell', 'mkdir nested && (cd nested && git init -q) && printf y>nested/in.txt']], ['clean', '-fd'], ''),
    ('update-index --remove updates a present file (ls-files -s)', [('write', 'f.txt', 'a\nb\nc\n'), ['add', 'f.txt'], ['commit', '-q', '-m', 'init'], ('write', 'f.txt', 'MOD'), ['update-index', '--remove', 'f.txt']], ['ls-files', '-s'], ''),
    ('update-index --force-remove drops a present file (ls-files)', [('write', 'f.txt', 'a\nb\nc\n'), ['add', 'f.txt'], ['commit', '-q', '-m', 'init'], ['update-index', '--force-remove', 'f.txt']], ['ls-files'], ''),
    ('write-tree --prefix prints the subtree oid', [('write', 'f.txt', 'a\n'), ('write', 'sub/g.txt', 'x'), ['add', 'f.txt', 'sub/g.txt'], ['commit', '-q', '-m', 'init']], ['write-tree', '--prefix=sub/'], ''),
    ('write-tree --prefix on a missing prefix', [('write', 'f.txt', 'a\n'), ['add', 'f.txt'], ['commit', '-q', '-m', 'init']], ['write-tree', '--prefix=nope/'], ''),
    ('sparse-checkout list after plain init (cone) prints empty', [('write', 'f.txt', 'a'), ['add', 'f.txt'], ['commit', '-q', '-m', 'init'], ['sparse-checkout', 'init']], ['sparse-checkout', 'list'], ''),
    ('sparse-checkout list on a non-sparse worktree fails', [('write', 'f.txt', 'a'), ['add', 'f.txt'], ['commit', '-q', '-m', 'init']], ['sparse-checkout', 'list'], ''),
    ('sparse-checkout set then list (cone) prints sorted dirs', [('write', 'f.txt', 'a'), ['add', 'f.txt'], ['commit', '-q', '-m', 'init'], ['sparse-checkout', 'set', 'foo', 'bar']], ['sparse-checkout', 'list'], ''),
    ('ls-remote on a non-repository argument', [], ['ls-remote', 'refs/nope/*'], ''),
    ('hash-object -t commit malformed refuses (fsck)', [], ['hash-object', '-t', 'commit', '--stdin'], 'garbage\n'),
    ('hash-object -t tree malformed: too-short tree object', [], ['hash-object', '-t', 'tree', '--stdin'], 'garbage\n'),
    ('hash-object -t tag malformed: missingObject', [], ['hash-object', '-t', 'tag', '--stdin'], 'garbage\n'),
    ('hash-object -t commit --literally bypasses fsck', [], ['hash-object', '-t', 'commit', '--stdin', '--literally'], 'garbage\n'),
    ('pack-objects --stdout BASE is a usage error', [('write', 'f', 'x'), ['add', 'f'], ['commit', '-qm', 'c']], ['pack-objects', '--stdout', 'BASE'], ''),
    ('pack-objects no args is a usage error', [], ['pack-objects'], ''),
    ('repack -a -d writes objects/info/packs', [('write', 'a.txt', 'one\n'), ['add', 'a.txt'], ['commit', '-qm', 'c1'], ('write', 'a.txt', 'one\ntwo\n'), ['add', 'a.txt'], ['commit', '-qm', 'c2'], ['repack', '-a', '-d']], ['update-server-info'], ''),
    ('commit-graph write is silent on success', [('write', 'a', 'c1\n'), ['add', 'a'], ['commit', '-qm', 'c1']], ['commit-graph', 'write', '--reachable'], ''),
    ('commit-graph verify is silent on success', [('write', 'a', 'c1\n'), ['add', 'a'], ['commit', '-qm', 'c1'], ['commit-graph', 'write', '--reachable']], ['commit-graph', 'verify'], ''),
    ('multi-pack-index write is silent on success', [('write', 'a.txt', 'one\n'), ['add', 'a.txt'], ['commit', '-qm', 'c1'], ['repack', '-d', '-q'], ('write', 'a.txt', 'one\ntwo\n'), ['add', 'a.txt'], ['commit', '-qm', 'c2'], ['repack', '-q']], ['multi-pack-index', 'write'], ''),
    ('fast-export --full-tree emits deleteall', [('write', 'a.txt', 'one\n'), ['add', 'a.txt'], ['commit', '-qm', 'c1'], ('write', 'a.txt', 'one\ntwo\n'), ('write', 'sub/b.txt', 'x\n'), ['add', '-A'], ['commit', '-qm', 'c2']], ['fast-export', '--full-tree', '--all'], ''),
    ('fast-export --show-original-ids emits original-oid lines', [('write', 'a.txt', 'one\n'), ['add', 'a.txt'], ['commit', '-qm', 'c1']], ['fast-export', '--show-original-ids', '--all'], ''),
    ('archive HEAD nonexistent pathspec fatal', [('write', 'a.txt', 'one\n'), ('write', 'sub/b.txt', 'x\n'), ['add', '-A'], ['commit', '-qm', 'c1']], ['archive', 'HEAD', 'nonexistent'], ''),
    ('bundle create --all succeeds and writes header', [('write', 'a.txt', 'one\n'), ['add', 'a.txt'], ['commit', '-qm', 'c1'], ['tag', 'v1.0'], ['branch', 'feature']], ['bundle', 'create', 'out.bundle', '--all'], ''),
    ('bundle create master is silent on success', [('write', 'a.txt', 'one\n'), ['add', 'a.txt'], ['commit', '-qm', 'c1']], ['bundle', 'create', 'out.bundle', 'master'], ''),
    ('bundle create unknown rev is fatal', [('write', 'a.txt', 'one\n'), ['add', 'a.txt'], ['commit', '-qm', 'c1']], ['bundle', 'create', 'out.bundle', 'main'], ''),
    ('bundle list-heads missing file error rc1', [], ['bundle', 'list-heads', 'nope.bundle'], ''),
    ('bundle verify missing file error rc1', [], ['bundle', 'verify', 'nope.bundle'], ''),
    ('bundle list-heads not-a-bundle error', [('write', 'x.bundle', 'garbage\n')], ['bundle', 'list-heads', 'x.bundle'], ''),
    ('bundle list-heads on real bundle prints ref lines', [('write', 'a.txt', 'one\n'), ['add', 'a.txt'], ['commit', '-qm', 'c1'], ['tag', 'v1.0'], ['branch', 'feature'], ['bundle', 'create', 'x.bundle', 'master', 'feature', 'v1.0']], ['bundle', 'list-heads', 'x.bundle'], ''),
    ('bundle verify on real bundle prints details + okay', [('write', 'a.txt', 'one\n'), ['add', 'a.txt'], ['commit', '-qm', 'c1'], ['tag', 'v1.0'], ['branch', 'feature'], ['bundle', 'create', 'x.bundle', 'master', 'feature', 'v1.0']], ['bundle', 'verify', 'x.bundle'], ''),
]

@pytest.mark.parametrize("case", BATCH13_CASES, ids=[c[0] for c in BATCH13_CASES])
def test_batch13_parity(case, tmp_path: Path, git_254_oracle: str):
    _id, setup, probe = case
    assert_command_parity(git_254_oracle, tmp_path, setup, probe)


@pytest.mark.parametrize("case", BATCH13_STDIN_CASES, ids=[c[0] for c in BATCH13_STDIN_CASES])
def test_batch13_stdin_parity(case, tmp_path: Path, git_254_oracle: str):
    _id, setup, probe, stdin = case
    assert_command_parity(git_254_oracle, tmp_path, setup, probe, stdin=stdin)


BATCH14_CASES = [
    ('cat-file -e on an unresolvable name dies rc 128', [('write', 'file.txt', 'x\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['cat-file', '-e', 'nonexistentref']),
    ('ls-tree trailing-slash pathspec lists contents', [('write', 'sub/a.txt', 'apple\n'), ('write', 'file.txt', 'x\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['ls-tree', 'HEAD', 'sub/']),
    ('ls-tree -t with trailing-slash shows the dir too', [('write', 'sub/a.txt', 'apple\n'), ('write', 'sub/deep/d.txt', 'deep\n'), ('write', 'file.txt', 'x\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['ls-tree', '-t', 'HEAD', 'sub/']),
    ('ls-files -i without -o/-c is rejected', [('write', '.gitignore', '*.log\n'), ('write', 'junk.log', 'x\n'), ('write', 'file.txt', 'x\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['ls-files', '-i', '--exclude-standard']),
    ('rev-parse --revs-only suppresses on-disk path', [('write', 'file.txt', 'x\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['rev-parse', '--revs-only', 'HEAD', 'file.txt']),
    ('rev-parse --no-revs echoes only the path', [('write', 'file.txt', 'x\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['rev-parse', '--no-revs', 'HEAD', 'file.txt']),
    ('rev-parse echoes the -- separator', [('write', 'file.txt', 'x\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['rev-parse', 'HEAD', '--', 'file.txt']),
    ('rev-parse leading -- echoes separator then path', [('write', 'file.txt', 'x\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['rev-parse', '--', 'file.txt']),
    ('for-each-ref %(align:20) left pad', [('write', 'file.txt', 'x\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['tag', 'v1.0'], ['branch', 'feature']], ['for-each-ref', '--format=%(align:20)%(refname)%(end)|']),
    ('for-each-ref %(align:20,middle)', [('write', 'file.txt', 'x\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['tag', 'v1.0'], ['branch', 'feature']], ['for-each-ref', '--format=%(align:20,middle)%(refname)%(end)|']),
    ('for-each-ref %(align:width=15,position=right)', [('write', 'file.txt', 'x\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['tag', 'v1.0'], ['branch', 'feature']], ['for-each-ref', '--format=%(align:width=15,position=right)%(refname)%(end)|']),
    ('merge-base --independent prints both diverged tips', [('write', 'f', 'a\n'), ['add', '-A'], ['commit', '-q', '-m', 'A'], ('write', 'f', 'a\nb\n'), ['add', '-A'], ['commit', '-q', '-m', 'B'], ['branch', 'topic'], ('write', 'f', 'a\nb\nc\n'), ['add', '-A'], ['commit', '-q', '-m', 'C'], ['checkout', '-q', 'topic'], ('write', 'g', 'd\n'), ['add', '-A'], ['commit', '-q', '-m', 'D'], ['checkout', '-q', 'main']], ['merge-base', '--independent', 'main', 'topic']),
    ('merge-base --independent dedups an ancestor', [('write', 'f', 'a\n'), ['add', '-A'], ['commit', '-q', '-m', 'A'], ('write', 'f', 'a\nb\n'), ['add', '-A'], ['commit', '-q', '-m', 'B']], ['merge-base', '--independent', 'main', 'main~1']),
    ('show-branch --independent dedups identical tips', [('write', 'file.txt', 'x\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['branch', 'feature']], ['show-branch', '--independent', 'main', 'feature']),
    ('show-branch --independent three identical tips', [('write', 'file.txt', 'x\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['branch', 'feature']], ['show-branch', '--independent', 'main', 'feature', 'main']),
    ('count-objects -v -H humanises sizes', [('write', 'file.txt', 'line1\nline2\nline3\n'), ('write', 'sub/a.txt', 'apple\n'), ['add', '-A'], ['commit', '-q', '-m', 'first commit'], ('write', 'file.txt', 'line1\nline2 changed\nline3\nline4\n'), ['add', '-A'], ['commit', '-q', '-m', 'second commit']], ['count-objects', '-v', '-H']),
    ('count-objects -v raw sizes unchanged', [('write', 'file.txt', 'line1\nline2\nline3\n'), ('write', 'sub/a.txt', 'apple\n'), ['add', '-A'], ['commit', '-q', '-m', 'first commit']], ['count-objects', '-v']),
    ('rev-list --count --left-right two counts', [('write', 'f', 'a\n'), ['add', '-A'], ['commit', '-q', '-m', 'A'], ('write', 'f', 'a\nb\n'), ['add', '-A'], ['commit', '-q', '-m', 'B'], ['branch', 'topic'], ('write', 'f', 'a\nb\nc\n'), ['add', '-A'], ['commit', '-q', '-m', 'C'], ['checkout', '-q', 'topic'], ('write', 'g', 'd\n'), ['add', '-A'], ['commit', '-q', '-m', 'D'], ['checkout', '-q', 'main']], ['rev-list', '--count', '--left-right', 'main...topic']),
    ('rev-list --pretty bare defaults to medium', [('write', 'f', 'a\n'), ['add', '-A'], ['commit', '-q', '-m', 'A'], ('write', 'f', 'a\nb\n'), ['add', '-A'], ['commit', '-q', '-m', 'B']], ['rev-list', '--pretty', 'HEAD']),
    ('rev-list --pretty=medium single header + trailing blank', [('write', 'f', 'a\n'), ['add', '-A'], ['commit', '-q', '-m', 'A'], ('write', 'f', 'a\nb\n'), ['add', '-A'], ['commit', '-q', '-m', 'B']], ['rev-list', '--pretty=medium', 'HEAD']),
    ('rev-list --pretty=full', [('write', 'f', 'a\n'), ['add', '-A'], ['commit', '-q', '-m', 'A'], ('write', 'f', 'a\nb\n'), ['add', '-A'], ['commit', '-q', '-m', 'B']], ['rev-list', '--pretty=full', 'HEAD']),
    ('rev-list --pretty=fuller', [('write', 'f', 'a\n'), ['add', '-A'], ['commit', '-q', '-m', 'A'], ('write', 'f', 'a\nb\n'), ['add', '-A'], ['commit', '-q', '-m', 'B']], ['rev-list', '--pretty=fuller', 'HEAD']),
    ('rev-list --pretty=short omits Date line', [('write', 'f', 'a\n'), ['add', '-A'], ['commit', '-q', '-m', 'A'], ('write', 'f', 'a\nb\n'), ['add', '-A'], ['commit', '-q', '-m', 'B']], ['rev-list', '--pretty=short', 'HEAD']),
    ('rev-list --pretty=raw raw headers', [('write', 'f', 'a\n'), ['add', '-A'], ['commit', '-q', '-m', 'A'], ('write', 'f', 'a\nb\n'), ['add', '-A'], ['commit', '-q', '-m', 'B']], ['rev-list', '--pretty=raw', 'HEAD']),
    ('rev-list --header -z rejected', [('write', 'f', 'a\n'), ['add', '-A'], ['commit', '-q', '-m', 'A'], ('write', 'f', 'a\nb\n'), ['add', '-A'], ['commit', '-q', '-m', 'B']], ['rev-list', '--header', '-z', 'HEAD']),
]

BATCH14_STDIN_CASES = [
    ('cat-file --batch-check %(rest) splits on whitespace', [('write', 'file.txt', 'x\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['cat-file', '--batch-check=%(objectname) %(objecttype) %(rest)'], 'HEAD foo bar\n'),
    ('cat-file --batch-check %(deltabase) for a loose object', [('write', 'file.txt', 'x\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['cat-file', '--batch-check=%(deltabase)'], 'HEAD\n'),
    ('cat-file --batch-check %(objectsize:disk) loose disk size', [('write', 'file.txt', 'line1\nline2\nline3\n'), ['add', '-A'], ['commit', '-q', '-m', 'first commit'], ('write', 'file.txt', 'line1\nline2 changed\nline3\nline4\n'), ['add', '-A'], ['commit', '-q', '-m', 'second commit']], ['cat-file', '--batch-check=%(objectsize) %(objectsize:disk)'], 'HEAD\n'),
    ('interpret-trailers runs trailer.<key>.command and substitutes output', [['config', 'trailer.sign.command', 'echo COMMITTER'], ('write', 'm.txt', 'subject\n\nbody\n')], ['interpret-trailers', '--trailer', 'sign:', 'm.txt'], ''),
    ('interpret-trailers runs trailer.<key>.cmd with value as argv', [['config', 'trailer.fix.cmd', 'echo "saw:$1"'], ('write', 'm.txt', 'subject\n\nbody\n')], ['interpret-trailers', '--trailer', 'fix: B12', 'm.txt'], ''),
    ('interpret-trailers auto-populates configured .command trailer with no --trailer', [['config', 'trailer.sign.command', 'echo COMMITTER'], ('write', 'm.txt', 'subject\n\nbody\n')], ['interpret-trailers', 'm.txt'], ''),
    ('interpret-trailers command with $ARG substitution', [['config', 'trailer.see.command', 'echo "ref: $ARG"'], ('write', 'm.txt', 'subj\n\nbody\n')], ['interpret-trailers', '--trailer', 'see: X', 'm.txt'], ''),
    ('mailinfo --quoted-cr=strip removes quoted CR from body', [], ['mailinfo', '--quoted-cr=strip', 'msg', 'patch'], 'From: A <a@b>\nSubject: s\nContent-Transfer-Encoding: quoted-printable\n\nline with cr=0D\nnext\n'),
    ('mailinfo default quoted-cr warns on quoted CRLF', [], ['mailinfo', 'msg', 'patch'], 'From: A <a@b>\nSubject: s\nContent-Transfer-Encoding: quoted-printable\n\nline with cr=0D\nnext\n'),
    ('mailinfo --quoted-cr=bogus rejected rc=129', [], ['mailinfo', '--quoted-cr=bogus', 'msg', 'patch'], 'From: A <a@b>\nSubject: s\nContent-Transfer-Encoding: quoted-printable\n\nline with cr=0D\nnext\n'),
    ('format-patch invalid range left endpoint rejected', [('write', 'f.txt', 'line1\n'), ['add', 'f.txt'], ['commit', '-qm', 'commit 1'], ('write', 'f.txt', 'line1\nline2\n'), ['commit', '-qam', 'commit 2'], ('write', 'f.txt', 'line1\nline2\nline3\n'), ['commit', '-qam', 'commit 3']], ['format-patch', '--stdout', 'HEAD~3..HEAD~1'], ''),
    ('apply --whitespace=bogus rejected rc=129', [('write', 'f.txt', 'a\nb\nc\n'), ['add', 'f.txt'], ['commit', '-qm', 'base'], ('write', 'ap.patch', 'diff --git a/f.txt b/f.txt\nindex 3d2c7e8..0a0a0a0 100644\n--- a/f.txt\n+++ b/f.txt\n@@ -1,3 +1,3 @@\n a\n-b\n+B\n c\n')], ['apply', '--whitespace=bogus', 'ap.patch'], ''),
    ('apply include then exclude same path applies it (first-match)', [('write', 'f1', 'a\n'), ('write', 'f2', 'x\n'), ['add', '.'], ['commit', '-qm', 'b'], ('write', 'mp.patch', 'diff --git a/f1 b/f1\n--- a/f1\n+++ b/f1\n@@ -1 +1 @@\n-a\n+A\n')], ['apply', '--include=f1', '--exclude=f1', 'mp.patch'], ''),
    ('apply exclude then include same path skips it, empty result rc=0', [('write', 'f1', 'a\n'), ('write', 'f2', 'x\n'), ['add', '.'], ['commit', '-qm', 'b'], ('write', 'mp.patch', 'diff --git a/f1 b/f1\n--- a/f1\n+++ b/f1\n@@ -1 +1 @@\n-a\n+A\n')], ['apply', '--exclude=f1', '--include=f1', 'mp.patch'], ''),
    ('apply nonexistent patch file error rc=128', [('write', 'f1', 'a\n'), ['add', '.'], ['commit', '-qm', 'b']], ['apply', 'nope.patch'], ''),
    ('revert -n stages without committing (log unchanged)', [('write', 'f.txt', 'a\n'), ['add', 'f.txt'], ['commit', '-qm', 'c1'], ('write', 'f.txt', 'a\nb\n'), ['commit', '-qam', 'c2']], ['log', '--oneline'], ''),
    ('rebase upstream already up to date prints message to stdout', [('write', 'f.txt', 'a\n'), ['add', 'f.txt'], ['commit', '-qm', 'base'], ['checkout', '-qb', 'topic'], ('write', 'f.txt', 'a\nb\n'), ['commit', '-qam', 't1']], ['rebase', 'main'], ''),
    ('revert conflict writes short-sha+subject marker into worktree', [('write', 'f.txt', 'a\nb\nc\n'), ['add', 'f.txt'], ['commit', '-qm', 'c1'], ('write', 'f.txt', 'a\nB\nc\n'), ['commit', '-qam', 'c2'], ('write', 'f.txt', 'a\nB\nC\n'), ['commit', '-qam', 'c3'], ['revert', '--no-edit', 'HEAD~1']], ['cat-file', 'blob', '@:f.txt'], ''),
]

@pytest.mark.parametrize("case", BATCH14_CASES, ids=[c[0] for c in BATCH14_CASES])
def test_batch14_parity(case, tmp_path: Path, git_254_oracle: str):
    _id, setup, probe = case
    assert_command_parity(git_254_oracle, tmp_path, setup, probe)


@pytest.mark.parametrize("case", BATCH14_STDIN_CASES, ids=[c[0] for c in BATCH14_STDIN_CASES])
def test_batch14_stdin_parity(case, tmp_path: Path, git_254_oracle: str):
    _id, setup, probe, stdin = case
    assert_command_parity(git_254_oracle, tmp_path, setup, probe, stdin=stdin)


BATCH15_CASES = [
    ('diff -S pickaxe restricts to matching file', [('write', 'src/code.txt', 'alpha\nbeta\ngamma\ndelta\n'), ('write', 'docs/readme.txt', 'old documentation\nsecond line\n'), ['add', '-A'], ['commit', '-q', '-m', 'first'], ('write', 'src/code.txt', 'alpha\nBETA\ngamma\n'), ('write', 'docs/readme.txt', 'new documentation\nsecond line\nthird line\n'), ['add', '-A'], ['commit', '-q', '-m', 'second']], ['diff', 'HEAD~1', 'HEAD', '-S', 'BETA']),
    ('diff -G pickaxe regex', [('write', 'src/code.txt', 'alpha\nbeta\ngamma\ndelta\n'), ('write', 'docs/readme.txt', 'old documentation\nsecond line\n'), ['add', '-A'], ['commit', '-q', '-m', 'first'], ('write', 'src/code.txt', 'alpha\nBETA\ngamma\n'), ('write', 'docs/readme.txt', 'new documentation\nsecond line\nthird line\n'), ['add', '-A'], ['commit', '-q', '-m', 'second']], ['diff', 'HEAD~1', 'HEAD', '-G', 'BETA']),
    ('diff --no-index two files differ', [('write', 'f1', '1\n2\n'), ('write', 'f2', '1\n3\n')], ['diff', '--no-index', 'f1', 'f2']),
    ('diff --no-index identical files rc 0', [('write', 'f1', 'same\n'), ('write', 'f2', 'same\n')], ['diff', '--no-index', 'f1', 'f2']),
    ('diff pathspec without dd', [('write', 'src/code.txt', 'alpha\nbeta\ngamma\ndelta\n'), ('write', 'docs/readme.txt', 'old documentation\nsecond line\n'), ['add', '-A'], ['commit', '-q', '-m', 'first'], ('write', 'src/code.txt', 'alpha\nBETA\ngamma\n'), ('write', 'docs/readme.txt', 'new documentation\nsecond line\nthird line\n'), ['add', '-A'], ['commit', '-q', '-m', 'second']], ['diff', 'HEAD~1', 'HEAD', 'src/code.txt']),
    ('diff -p with --stat emits both', [('write', 'a.txt', 'line1\nline2\nline3\n'), ['add', '-A'], ['commit', '-q', '-m', 'first'], ('write', 'feat.txt', 'feature content\n'), ['add', '-A'], ['commit', '-q', '-m', 'second']], ['diff', '-p', '--stat', 'HEAD~1', 'HEAD']),
    ('diff --patch-with-raw emits both', [('write', 'a.txt', 'line1\nline2\nline3\n'), ['add', '-A'], ['commit', '-q', '-m', 'first'], ('write', 'feat.txt', 'feature content\n'), ['add', '-A'], ['commit', '-q', '-m', 'second']], ['diff', '--patch-with-raw', 'HEAD~1', 'HEAD']),
    ('diff -z --name-status NUL', [('write', 'a.txt', 'line1\n'), ['add', '-A'], ['commit', '-q', '-m', 'first'], ('write', 'feat.txt', 'feature content\n'), ['add', '-A'], ['commit', '-q', '-m', 'second']], ['diff', 'HEAD~1', 'HEAD', '-z', '--name-status']),
    ('diff -z --raw NUL', [('write', 'a.txt', 'line1\n'), ['add', '-A'], ['commit', '-q', '-m', 'first'], ('write', 'feat.txt', 'feature content\n'), ['add', '-A'], ['commit', '-q', '-m', 'second']], ['diff', 'HEAD~1', 'HEAD', '-z', '--raw']),
    ('diff --abbrev=8 --raw width', [('write', 'a.txt', 'line1\n'), ['add', '-A'], ['commit', '-q', '-m', 'first'], ('write', 'feat.txt', 'feature content\n'), ['add', '-A'], ['commit', '-q', '-m', 'second']], ['diff', 'HEAD~1', 'HEAD', '--abbrev=8', '--raw']),
    ('diff --stat-width implies stat', [('write', 'src/code.txt', 'alpha\nbeta\ngamma\ndelta\n'), ('write', 'docs/readme.txt', 'old documentation\nsecond line\n'), ['add', '-A'], ['commit', '-q', '-m', 'first'], ('write', 'src/code.txt', 'alpha\nBETA\ngamma\n'), ('write', 'docs/readme.txt', 'new documentation\nsecond line\nthird line\n'), ['add', '-A'], ['commit', '-q', '-m', 'second']], ['diff', 'HEAD~1', 'HEAD', '--stat-width=40']),
    ('diff --dirstat percentages', [('write', 'src/code.txt', 'alpha\nbeta\ngamma\ndelta\n'), ('write', 'docs/readme.txt', 'old documentation\nsecond line\n'), ['add', '-A'], ['commit', '-q', '-m', 'first'], ('write', 'src/code.txt', 'alpha\nBETA\ngamma\n'), ('write', 'docs/readme.txt', 'new documentation\nsecond line\nthird line\n'), ['add', '-A'], ['commit', '-q', '-m', 'second']], ['diff', 'HEAD~1', 'HEAD', '--dirstat']),
    ('diff --output-indicator-new', [('write', 'a.txt', 'line1\n'), ['add', '-A'], ['commit', '-q', '-m', 'first'], ('write', 'feat.txt', 'feature content\n'), ['add', '-A'], ['commit', '-q', '-m', 'second']], ['diff', 'HEAD~1', 'HEAD', '--output-indicator-new=>']),
    ('diff --output-indicator-old and context', [('write', 'a.txt', 'one\ntwo\nthree\n'), ['add', '-A'], ['commit', '-q', '-m', 'first'], ('write', 'a.txt', 'one\nTWO\nthree\n'), ['add', '-A'], ['commit', '-q', '-m', 'second']], ['diff', 'HEAD~1', 'HEAD', '--output-indicator-old=:', '--output-indicator-context=.']),
    ('log --pretty=email', [('write', 'a.txt', 'x\n'), ['add', '-A'], ['commit', '-q', '-m', 'first'], ('write', 'a.txt', 'y\n'), ['add', '-A'], ['commit', '-q', '-m', 'second']], ['log', '--pretty=email']),
    ('show --pretty=email with diff', [('write', 'a.txt', 'x\n'), ['add', '-A'], ['commit', '-q', '-m', 'only']], ['show', '--pretty=email']),
    ('log color placeholders to empty', [('write', 'a.txt', 'x\n'), ['add', '-A'], ['commit', '-q', '-m', 'first']], ['log', '-1', '--format=%C(red)x%C(reset)']),
    ('log basic color words', [('write', 'a.txt', 'x\n'), ['add', '-A'], ['commit', '-q', '-m', 'first']], ['log', '-1', '--format=%Credx%Creset']),
    ('log %w wrap placeholder', [('write', 'a.txt', 'x\n'), ['add', '-A'], ['commit', '-q', '-m', 'first']], ['log', '-1', '--format=%w(20,2,4)wrapped_long_subject_here_to_test']),
    ('log %w wrap multi-word', [('write', 'a.txt', 'x\n'), ['add', '-A'], ['commit', '-q', '-m', 'first']], ['log', '-1', '--format=%w(20,2,4)alpha beta gamma delta epsilon zeta']),
    ('log %(describe)', [('write', 'a.txt', 'line1\nline2\nline3\n'), ('write', 'sub/b.txt', 'hello\nworld\n'), ['add', '-A'], ['commit', '-q', '-m', 'first commit'], ('write', 'a.txt', 'line1\nCHANGED\nline3\nline4\n'), ['add', 'a.txt'], ['commit', '-q', '-m', 'second commit'], ['tag', 'v1.0'], ['tag', '-a', 'v1.5', '-m', 'annotated tag'], ('write', 'a.txt', 'line1\nCHANGED\nline3\nline4\nmainline\n'), ['add', 'a.txt'], ['commit', '-q', '-m', 'main advance']], ['log', '-1', '--format=%(describe)']),
    ('show --abbrev=8 merge line', [('write', 'a.txt', 'line1\nline2\nline3\n'), ['add', '-A'], ['commit', '-q', '-m', 'first commit'], ['checkout', '-q', '-b', 'feature'], ('write', 'feat.txt', 'feature content\n'), ['add', 'feat.txt'], ['commit', '-q', '-m', 'feature commit'], ['checkout', '-q', 'main'], ('write', 'a.txt', 'line1\nCHANGED\n'), ['add', 'a.txt'], ['commit', '-q', '-m', 'main advance'], ['merge', '-q', '--no-ff', 'feature', '-m', 'merge feature']], ['show', '--abbrev=8', 'HEAD']),
    ('status --porcelain=v2 staged rename', [('write', 'orig.txt', 'one\ntwo\nthree\nfour\nfive\nsix\n'), ['add', '-A'], ['commit', '-q', '-m', 'base'], ['mv', 'orig.txt', 'renamed.txt']], ['status', '--porcelain=v2']),
    ('status --porcelain=v2 -z staged rename', [('write', 'orig.txt', 'one\ntwo\nthree\nfour\nfive\nsix\n'), ['add', '-A'], ['commit', '-q', '-m', 'base'], ['mv', 'orig.txt', 'renamed.txt']], ['status', '--porcelain=v2', '-z']),
    ('status --porcelain=v2 --no-renames', [('write', 'orig.txt', 'one\ntwo\nthree\nfour\nfive\nsix\n'), ['add', '-A'], ['commit', '-q', '-m', 'base'], ['mv', 'orig.txt', 'renamed.txt']], ['status', '--no-renames', '--porcelain=v2']),
    ('blame -L 1,3 column width', [('write', 'nums.txt', '1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n11\n12\n'), ['add', '-A'], ['commit', '-q', '-m', 'base'], ('write', 'nums.txt', '1\n2\n3\n4\nFIVE\n6\n7\n8\n9\n10\n11\n12\n'), ['add', '-A'], ['commit', '-q', '-m', 'edit5']], ['blame', '-L', '1,3', 'nums.txt']),
    ('blame -L 9,12 column width', [('write', 'nums.txt', '1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n11\n12\n'), ['add', '-A'], ['commit', '-q', '-m', 'base'], ('write', 'nums.txt', '1\n2\n3\n4\nFIVE\n6\n7\n8\n9\n10\n11\n12\n'), ['add', '-A'], ['commit', '-q', '-m', 'edit5']], ['blame', '-L', '9,12', 'nums.txt']),
    ('grep -r recursive default', [('write', 'src/code.txt', 'alpha\nbeta\n'), ('write', 'docs/readme.txt', 'new documentation\nsecond line\n'), ['add', '-A'], ['commit', '-q', '-m', 'first']], ['grep', '-r', 'documentation']),
    ('grep -rn recursive with line numbers', [('write', 'src/code.txt', 'alpha\nbeta\n'), ('write', 'docs/readme.txt', 'new documentation\nsecond line\n'), ['add', '-A'], ['commit', '-q', '-m', 'first']], ['grep', '-rn', 'documentation']),
    ('grep -p function context', [('write', 'code.c', 'int main(void)\n{\n\tint target = 42;\n\treturn target;\n}\n'), ['add', '-A'], ['commit', '-q', '-m', 'base']], ['grep', '-p', 'target']),
    ('grep -W whole function', [('write', 'code.c', 'int main(void)\n{\n\tint target = 42;\n\treturn target;\n}\n'), ['add', '-A'], ['commit', '-q', '-m', 'base']], ['grep', '-W', 'target']),
    ('grep -pn function with line numbers', [('write', 'code.c', 'int main(void)\n{\n\tint target = 42;\n\treturn target;\n}\n'), ['add', '-A'], ['commit', '-q', '-m', 'base']], ['grep', '-pn', 'target']),
    ('grep --no-index outside index', [('write', 'plain.txt', 'find me here\n')], ['grep', '--no-index', 'find me']),
    ('tag --points-at resolves to annotated tag object', [('write', 'a.txt', 'l\n'), ['add', '-A'], ['commit', '-q', '-m', 'first'], ('write', 'a.txt', 'l2\n'), ['commit', '-q', '-am', 'second'], ['tag', '-a', 'v1.0', '-m', 'version 1.0'], ['tag', 'light']], ['tag', '--points-at', 'v1.0']),
    ('name-rev prefers tag over closer branch', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', '1\n2\n'), ['commit', '-q', '-am', 'c2'], ['branch', 'feature'], ('write', 'a.txt', '1\n2\n3\n'), ['commit', '-q', '-am', 'c3'], ['tag', 'light']], ['name-rev', 'HEAD~1']),
    ('name-rev --all tag priority', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', '1\n2\n'), ['commit', '-q', '-am', 'c2'], ['branch', 'feature'], ('write', 'a.txt', '1\n2\n3\n'), ['commit', '-q', '-am', 'c3'], ['tag', 'light']], ['name-rev', '--all']),
    ('name-rev absent full sha', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['name-rev', 'deadbeefdeadbeefdeadbeefdeadbeefdeadbeef']),
    ('reset -q --hard suppresses summary', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', '1\n2\n'), ['commit', '-q', '-am', 'c2']], ['reset', '-q', '--hard', 'HEAD~1']),
    ('reset --mixed with paths warning', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['reset', '--mixed', 'HEAD', '--', 'a.txt']),
    ('checkout --orphan creates unborn branch', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['checkout', '--orphan', 'orphbr']),
    ('checkout aborts on overwrite', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['checkout', '-q', '-b', 'feature'], ('write', 'a.txt', '1\nfeat\n'), ['commit', '-q', '-am', 'cfeat'], ['checkout', '-q', 'main'], ('write', 'a.txt', '1\nlocalmod\n')], ['checkout', 'feature']),
    ('checkout -m three-way merges local change', [('write', 'a.txt', '1\n2\n3\n4\n5\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['branch', 'other'], ['checkout', '-q', 'other'], ('write', 'a.txt', '1\n2\n3\n4\nFIVE\n'), ['commit', '-q', '-am', 'c-other'], ['checkout', '-q', 'main'], ('write', 'a.txt', 'ONE\n2\n3\n4\n5\n')], ['checkout', '-m', 'other']),
    ('checkout --ours resolves unmerged path', [('write', 'a.txt', 'base\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['branch', 'feature'], ('write', 'a.txt', 'MAIN\n'), ['commit', '-q', '-am', 'main'], ['checkout', '-q', 'feature'], ('write', 'a.txt', 'FEATURE\n'), ['commit', '-q', '-am', 'feat'], ['checkout', '-q', 'main'], ['merge', 'feature']], ['checkout', '--ours', 'a.txt']),
    ('checkout --theirs resolves unmerged path', [('write', 'a.txt', 'base\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['branch', 'feature'], ('write', 'a.txt', 'MAIN\n'), ['commit', '-q', '-am', 'main'], ['checkout', '-q', 'feature'], ('write', 'a.txt', 'FEATURE\n'), ['commit', '-q', '-am', 'feat'], ['checkout', '-q', 'main'], ['merge', 'feature']], ['checkout', '--theirs', 'a.txt']),
    ('plain checkout on unmerged path errors', [('write', 'a.txt', 'base\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['branch', 'feature'], ('write', 'a.txt', 'MAIN\n'), ['commit', '-q', '-am', 'main'], ['checkout', '-q', 'feature'], ('write', 'a.txt', 'FEATURE\n'), ['commit', '-q', '-am', 'feat'], ['checkout', '-q', 'main'], ['merge', 'feature']], ['checkout', 'a.txt']),
    ('merge -q keeps Auto-merging', [('write', 'a.txt', '1\n2\n3\n4\n5\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['branch', 'feature'], ('write', 'a.txt', 'TOP\n1\n2\n3\n4\n5\n'), ['commit', '-q', '-am', 'main change'], ['checkout', '-q', 'feature'], ('write', 'a.txt', '1\n2\n3\n4\n5\nBOTTOM\n'), ['commit', '-q', '-am', 'feature change'], ['checkout', '-q', 'main']], ['merge', '-q', 'feature']),
    ('merge --continue with no merge', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['merge', '--continue']),
    ('commit --trailer colon normalization', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', '1\n2\n'), ['add', 'a.txt'], ['commit', '-m', 'subj', '--trailer', 'Acked-by:Bob']], ['log', '-1', '--format=%B']),
    ('commit --trailer equals normalization', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', '1\n2\n'), ['add', 'a.txt'], ['commit', '-m', 'subj', '--trailer', 'Acked-by=Bob']], ['log', '-1', '--format=%B']),
    ('tag --trailer colon normalization', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['tag', '-a', '-m', 'body', '--trailer', 'Acked-by:Bob', 't1']], ['cat-file', '-p', 't1']),
    ('commit --author no match fatal', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', '1\n2\n'), ['add', 'a.txt']], ['commit', '-m', 'subj', '--author=Joe']),
    ('commit --author matches existing', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', '1\n2\n'), ['add', 'a.txt'], ['commit', '-m', 'subj', '--author=Parity']], ['log', '-1', '--format=%an <%ae>']),
    ('commit --date rfc2822', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', '1\n2\n'), ['add', 'a.txt'], ['commit', '-m', 'subj', '--date=Mon, 3 Jul 2006 17:18:43 +0200']], ['log', '-1', '--format=%at %ai']),
    ('commit --date abbrev month no weekday', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', '1\n2\n'), ['add', 'a.txt'], ['commit', '-m', 'subj', '--date=Jul 3 17:18:43 2006 +0200']], ['log', '-1', '--format=%at %ai']),
    ('branch -d unmerged refuses', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['checkout', '-q', '-b', 'unmerged'], ('write', 'a.txt', '1\n2\n'), ['commit', '-q', '-am', 'c2'], ['checkout', '-q', 'main']], ['branch', '-d', 'unmerged']),
    ('branch -d two names', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['branch', 'b1'], ['branch', 'b2']], ['branch', '-d', 'b1', 'b2']),
    ('branch -m onto existing refuses', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', '1\n2\n'), ['commit', '-q', '-am', 'c2'], ['branch', 'existing', 'HEAD~1'], ['branch', 'topic']], ['branch', '-m', 'topic', 'existing']),
    ('branch -c onto existing refuses', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', '1\n2\n'), ['commit', '-q', '-am', 'c2'], ['branch', 'existing', 'HEAD~1'], ['branch', 'topic']], ['branch', '-c', 'topic', 'existing']),
    ('tag -d two names', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['tag', 't1'], ['tag', 't2']], ['tag', '-d', 't1', 't2']),
    ('tag -d with missing middle name', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['tag', 't1'], ['tag', 't2']], ['tag', '-d', 't1', 'x', 't2']),
    ('tag -n2 prints two lines', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['tag', '-a', '-m', 'line one\nline two\nline three', 'annot']], ['tag', '-n2']),
    ('tag -n0 prints names only', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['tag', '-a', '-m', 'line one\nline two\nline three', 'annot']], ['tag', '-n0']),
    ('commit --amend --short uses HEAD parent', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'b.txt', 'new\n'), ['add', 'b.txt'], ['commit', '-q', '-m', 'c2'], ('write', 'a.txt', '1\n2\n'), ['add', 'a.txt']], ['commit', '--amend', '--short']),
    ('commit --amend --dry-run root commit', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'b.txt', 'new\n'), ['add', 'b.txt']], ['commit', '--amend', '--dry-run']),
    ('commit --amend --dry-run with parent unstage hint', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'b.txt', 'new\n'), ['add', 'b.txt'], ['commit', '-q', '-m', 'c2'], ('write', 'a.txt', '1\n2\n'), ['add', 'a.txt']], ['commit', '--amend', '--dry-run']),
    ('switch overwrite-safety abort', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['checkout', '-q', '-b', 'feature'], ('write', 'a.txt', '1\nfeat\n'), ['commit', '-q', '-am', 'cfeat'], ['checkout', '-q', 'main'], ('write', 'a.txt', '1\nlocalmod\n')], ['switch', 'feature']),
    ('rev-parse --symbolic prints the as-is rev name', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'first commit']], ['rev-parse', '--symbolic', 'HEAD']),
    ('rev-parse --no-flags suppresses flag echo', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'first commit']], ['rev-parse', '--no-flags', '--foo', 'HEAD']),
    ('rev-parse --default fallback rev', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'first commit']], ['rev-parse', '--default', 'HEAD']),
    ('rev-parse --sq shell-quotes output', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'first commit']], ['rev-parse', '--sq', 'HEAD']),
    ('rev-parse --sq-quote standalone mode', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'first commit']], ['rev-parse', '--sq-quote', 'a', 'b c', 'd']),
    ('rev-parse --sq-quote quotes ! and apostrophe', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'first commit']], ['rev-parse', '--sq-quote', "it's", 'a!b']),
    ('rev-parse --show-object-format=storage', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'first commit']], ['rev-parse', '--show-object-format=storage']),
    ('rev-parse --show-object-format=compat empty', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'first commit']], ['rev-parse', '--show-object-format=compat']),
    ('rev-parse --show-object-format unknown mode dies', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'first commit']], ['rev-parse', '--show-object-format=zzz']),
    ('rev-parse --local-env-vars lists GIT_* names', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'first commit']], ['rev-parse', '--local-env-vars']),
    ('rev-parse --resolve-git-dir .git', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'first commit']], ['rev-parse', '--resolve-git-dir', '.git']),
    ('rev-parse --resolve-git-dir not-a-gitdir', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'first commit']], ['rev-parse', '--resolve-git-dir', 'nope']),
    ('rev-parse --resolve-git-dir requires argument', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'first commit']], ['rev-parse', '--resolve-git-dir']),
    ('rev-parse --since full datetime to --max-age', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'first commit']], ['rev-parse', '--since=2020-01-01 00:00:00']),
    ('rev-parse --until full datetime to --min-age', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'first commit']], ['rev-parse', '--until=2020-06-15 12:30:00']),
    ('rev-list --objects emits annotated tag object', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'first commit'], ['tag', '-a', 'v1.0', '-m', 'release one']], ['rev-list', '--objects', 'v1.0']),
    ('rev-list --objects --all includes tag', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'first commit'], ['tag', '-a', 'v1.0', '-m', 'release one']], ['rev-list', '--objects', '--all']),
    ('rev-list --objects --tags includes tag', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'first commit'], ['tag', '-a', 'v1.0', '-m', 'release one']], ['rev-list', '--objects', '--tags']),
    ('rev-list plain annotated tag peels to commit', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'first commit'], ['tag', '-a', 'v1.0', '-m', 'release one']], ['rev-list', 'v1.0']),
    ('rev-list -z --objects tag uses path= form', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'first commit'], ['tag', '-a', 'v1.0', '-m', 'release one']], ['rev-list', '-z', '--objects', 'v1.0']),
    ('for-each-ref empty committerdate sorts first ascending', [('write', 'f', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'a'], ['branch', 'b1'], ('write', 'f', '2\n'), ['add', '-A'], ['commit', '-q', '-m', 'b'], ['branch', 'b2'], ['tag', '-a', 'atag', '-m', 'tagmsg']], ['for-each-ref', '--sort=committerdate', '--format=%(refname)']),
    ('for-each-ref empty key mirrored descending', [('write', 'f', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'a'], ['branch', 'b1'], ('write', 'f', '2\n'), ['add', '-A'], ['commit', '-q', '-m', 'b'], ['branch', 'b2'], ['tag', '-a', 'atag', '-m', 'tagmsg']], ['for-each-ref', '--sort=-committerdate', '--format=%(refname)']),
    ('for-each-ref unknown atom rejected', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'first commit']], ['for-each-ref', '--format=%(bogus)']),
    ('for-each-ref if without then rejected', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'first commit']], ['for-each-ref', '--format=%(if)%(end)']),
    ('for-each-ref refname bad modifier rejected', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'first commit']], ['for-each-ref', '--format=%(refname:bogusmod)']),
    ('for-each-ref align without args rejected', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'first commit']], ['for-each-ref', '--format=%(align)']),
    ('for-each-ref color bad value rejected', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'first commit']], ['for-each-ref', '--format=%(color:notacolor)']),
    ('for-each-ref valid if-then-else accepted', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'msg'], ['branch', 'feature']], ['for-each-ref', '--format=%(if)%(HEAD)%(then)cur%(else)other%(end) %(refname)']),
    ('for-each-ref valid align accepted', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'msg']], ['for-each-ref', '--format=%(align:20)%(refname)%(end)|']),
    ('cat-file blob on nonexistent full-hex SHA -> bad file', [('write', 'a.txt', 'hello\nsecond\n'), ['add', '-A'], ['commit', '-q', '-m', 'commit']], ['cat-file', 'blob', '27f295c7e31cdfe2e9f8fc5fca4a5b97514e3fcd']),
    ('cat-file blob on a commit ref -> bad file', [('write', 'a.txt', 'hello\nsecond\n'), ['add', '-A'], ['commit', '-q', '-m', 'commit']], ['cat-file', 'blob', 'main']),
    ('cat-file blob on unresolvable name -> Not a valid object name', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c']], ['cat-file', 'blob', 'nonexistentref']),
    ('cat-file commit on missing full-hex -> bad file', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c']], ['cat-file', 'commit', 'ffffffffffffffffffffffffffffffffffffffff']),
    ('verify-pack missing .idx error text', [('write', 'f1', 'f1 content line\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['verify-pack', 'nope.idx']),
    ('verify-pack missing .pack reports .idx path', [('write', 'f1', 'f1 content line\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['verify-pack', 'nope.pack']),
    ('verify-pack missing bare name reports .idx path', [('write', 'f1', 'f1 content line\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['verify-pack', 'nope']),
    ('verify-pack -v missing idx prints pack bad', [('write', 'f1', 'f1 content line\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['verify-pack', '-v', 'nope.idx']),
    ('verify-pack -s missing idx prints pack bad', [('write', 'f1', 'f1 content line\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['verify-pack', '-s', 'nope.idx']),
    ('update-index --refresh reports needs update rc1', [('write', 'a.txt', 'hello\n'), ['add', 'a.txt'], ['commit', '-q', '-m', 'init'], ('write', 'a.txt', 'changed\n')], ['update-index', '--refresh']),
    ('update-index -q --refresh quiet-before silent rc0', [('write', 'a.txt', 'hello\n'), ['add', 'a.txt'], ['commit', '-q', '-m', 'init'], ('write', 'a.txt', 'changed\n')], ['update-index', '-q', '--refresh']),
    ('update-index --refresh -q quiet-after still reports rc1', [('write', 'a.txt', 'hello\n'), ['add', 'a.txt'], ['commit', '-q', '-m', 'init'], ('write', 'a.txt', 'changed\n')], ['update-index', '--refresh', '-q']),
    ('update-index cacheinfo null sha rejected', [], ['update-index', '--add', '--cacheinfo', '100644', '0000000000000000000000000000000000000000', 'foo']),
    ('update-index cacheinfo comma-form null sha rejected', [], ['update-index', '--add', '--cacheinfo', '100644,0000000000000000000000000000000000000000,foo']),
    ('update-index cacheinfo no --add for new path rejected', [], ['update-index', '--cacheinfo', '100644', '1234567890123456789012345678901234567890', 'foo']),
    ('update-index cacheinfo --add valid then ls-files', [['update-index', '--add', '--cacheinfo', '100644', '1234567890123456789012345678901234567890', 'foo']], ['ls-files', '-s']),
    ('update-index --chmod on non-indexed file routes through path processor', [('write', 'a.txt', 'a\n')], ['update-index', '--chmod=+x', 'a.txt']),
    ('update-index --add --chmod --verbose new file reports add+chmod', [('write', 'a.txt', 'a\n')], ['update-index', '--add', '--chmod=+x', '--verbose', 'a.txt']),
    ('mv -k bad source skipped rc0', [('write', 'a.txt', 'a\n'), ['add', 'a.txt'], ['commit', '-q', '-m', 'c']], ['mv', '-k', 'nope.txt', 'b.txt']),
    ('mv bad source dies rc128 without -k', [('write', 'a.txt', 'a\n'), ['add', 'a.txt'], ['commit', '-q', '-m', 'c']], ['mv', 'nope.txt', 'b.txt']),
    ('clean -n pathspec u.txt only', [('write', 'a.txt', 'a\n'), ['add', 'a.txt'], ['commit', '-q', '-m', 'c'], ('write', 'u.txt', 'u\n'), ('write', 'v.txt', 'v\n')], ['clean', '-n', 'u.txt']),
    ('clean -f pathspec removes only u.txt', [('write', 'a.txt', 'a\n'), ['add', 'a.txt'], ['commit', '-q', '-m', 'c'], ('write', 'u.txt', 'u\n'), ('write', 'v.txt', 'v\n')], ['clean', '-f', 'u.txt']),
    ('clean -nd subdir collapses dir', [('write', 'a.txt', 'a\n'), ['add', 'a.txt'], ['commit', '-q', '-m', 'c'], ('write', 'sub/x.txt', 'x\n'), ('write', 'sub/y.txt', 'y\n')], ['clean', '-nd', 'sub']),
    ('clean -nd file inside dir keeps siblings', [('write', 'a.txt', 'a\n'), ['add', 'a.txt'], ['commit', '-q', '-m', 'c'], ('write', 'sub/x.txt', 'x\n'), ('write', 'sub/y.txt', 'y\n')], ['clean', '-nd', 'sub/x.txt']),
    ('clean -n exact-dir-name removes without -d', [('write', 'a.txt', 'a\n'), ['add', 'a.txt'], ['commit', '-q', '-m', 'c'], ('write', 'sub/x.txt', 'x\n')], ['clean', '-n', 'sub']),
    ('stash list --oneline shows abbrev sha + full selector', [('write', 'a.txt', 'a\n'), ['add', 'a.txt'], ['commit', '-q', '-m', 'c'], ('write', 'a.txt', 'a\nm\n'), ['stash']], ['stash', 'list', '--oneline']),
    ('stash list -p appends each stash diff', [('write', 'a.txt', 'a\n'), ['add', 'a.txt'], ['commit', '-q', '-m', 'c'], ('write', 'a.txt', 'a\nm\n'), ['stash']], ['stash', 'list', '-p']),
    ('stash list --format=%gd %gs', [('write', 'a.txt', 'a\n'), ['add', 'a.txt'], ['commit', '-q', '-m', 'c'], ('write', 'a.txt', 'a\nm\n'), ['stash']], ['stash', 'list', '--format=%gd %gs']),
    ('sparse-checkout set then ls-files -t shows skip bits', [('write', 'a/x', 'x\n'), ('write', 'b/y', 'y\n'), ['add', '.'], ['commit', '-q', '-m', 'c'], ['sparse-checkout', 'set', 'a']], ['ls-files', '-t']),
    ('stash store bad object rc1', [('write', 'a.txt', 'a\n'), ['add', 'a.txt'], ['commit', '-q', '-m', 'c']], ['stash', 'store', 'deadbeef']),
    ('stash store no arg rc1', [('write', 'a.txt', 'a\n'), ['add', 'a.txt'], ['commit', '-q', '-m', 'c']], ['stash', 'store']),
    ('stash create no tracked changes rc0 no output', [('write', 'a.txt', 'a\n'), ['add', 'a.txt'], ['commit', '-q', '-m', 'c'], ('write', 'u.txt', 'u\n')], ['stash', 'create']),
    ('format-patch -o sanitizes subject filename (collapse runs, drop trailing dot)', [('write', 'f', 'a\n'), ['add', 'f'], ['commit', '-q', '-m', 'Fix: the bug (now)']], ['format-patch', '-1', '-o', 'OUTDIR']),
    ('format-patch -o keeps single dot and underscore, collapses double dots', [('write', 'f', 'a\n'), ['add', 'f'], ['commit', '-q', '-m', 'two..dots a.b.c _u_ keep']], ['format-patch', '-1', '-o', 'OD']),
    ('format-patch -o reroll prefix counts toward filename length', [('write', 'f', 'a\n'), ['add', 'f'], ['commit', '-q', '-m', 'Hello World']], ['format-patch', '-1', '-v2', '-o', 'OD']),
    ('log %f uses format_sanitized_subject (dot/underscore title chars)', [('write', 'f', 'a\n'), ['add', 'f'], ['commit', '-q', '-m', 'Two..dots: and_under']], ['log', '-1', '--format=%f']),
    ('apply emits an error pair per failing file before bailing', [('write', 'a.txt', 'a1\naD\na3\n'), ('write', 'b.txt', 'b1\nbD\nb3\n'), ['add', '-A'], ['commit', '-q', '-m', 's'], ('write', 'a.txt', 'a1\na2\na3\n'), ('write', 'b.txt', 'b1\nb2\nb3\n'), ['commit', '-q', '-am', 'mk'], ['format-patch', '-1', '-o', 'po'], ['reset', '-q', '--hard', 'HEAD~1']], ['apply', 'po/0001-mk.patch']),
    ('archive tar -0 rejected', [('write', 'a.txt', 'hello world\n'), ['add', '-A'], ['commit', '-q', '-m', 'first commit']], ['archive', '--format=tar', '-0', 'HEAD']),
    ('archive tgz -99 deflateInit2 die', [('write', 'a.txt', 'hello world\n'), ['add', '-A'], ['commit', '-q', '-m', 'first commit']], ['archive', '--format=tgz', '-99', 'HEAD']),
    ('archive add-virtual-file tar', [('write', 'a.txt', 'hello world\n'), ['add', '-A'], ['commit', '-q', '-m', 'first commit']], ['archive', '--format=tar', '--add-virtual-file=hello.txt:vcontent', 'HEAD']),
    ('archive add-virtual-file missing colon die', [('write', 'a.txt', 'hello world\n'), ['add', '-A'], ['commit', '-q', '-m', 'first commit']], ['archive', '--format=tar', '--add-virtual-file=nocolon', 'HEAD']),
    ('archive add-virtual-file empty name die', [('write', 'a.txt', 'hello world\n'), ['add', '-A'], ['commit', '-q', '-m', 'first commit']], ['archive', '--format=tar', '--add-virtual-file=:content', 'HEAD']),
    ('archive export-ignore tar', [('write', 'sec.txt', 'secret\n'), ('write', 'norm.txt', 'normal\n'), ('write', '.gitattributes', 'sec.txt export-ignore\n'), ['add', '-A'], ['commit', '-q', '-m', 'm']], ['archive', '--format=tar', 'HEAD']),
    ('archive export-subst tar', [('write', 'ver.txt', 'commit: $Format:%H$\n'), ('write', '.gitattributes', 'ver.txt export-subst\n'), ['add', '-A'], ['commit', '-q', '-m', 'm']], ['archive', '--format=tar', 'HEAD']),
    ('bundle create caret-exclusion rc and stderr', [('write', 'a.txt', 'hello world\n'), ['add', '-A'], ['commit', '-q', '-m', 'first commit'], ('write', 'a.txt', 'hello world\nmore\n'), ['add', '-A'], ['commit', '-q', '-m', 'second commit']], ['bundle', 'create', '/tmp/pygit_parity_x.bundle', 'HEAD', '^HEAD~1']),
    ('bundle create range A..B rc and stderr', [('write', 'a.txt', 'hello world\n'), ['add', '-A'], ['commit', '-q', '-m', 'first commit'], ('write', 'a.txt', 'hello world\nmore\n'), ['add', '-A'], ['commit', '-q', '-m', 'second commit']], ['bundle', 'create', '/tmp/pygit_parity_r.bundle', 'HEAD~1..HEAD']),
    ('fast-export use-done-feature', [('write', 'f1.txt', 'content1\n'), ['add', '-A'], ['commit', '-q', '-m', 'msg1'], ('write', 'f2.txt', 'content2\n'), ['add', '-A'], ['commit', '-q', '-m', 'msg2']], ['fast-export', '--use-done-feature', '--all']),
    ('fast-export progress=1', [('write', 'f1.txt', 'content1\n'), ['add', '-A'], ['commit', '-q', '-m', 'msg1'], ('write', 'f2.txt', 'content2\n'), ['add', '-A'], ['commit', '-q', '-m', 'msg2']], ['fast-export', '--progress=1', '--all']),
    ('fast-export refspec rename main', [('write', 'f1.txt', 'content1\n'), ['add', '-A'], ['commit', '-q', '-m', 'msg1'], ('write', 'f2.txt', 'content2\n'), ['add', '-A'], ['commit', '-q', '-m', 'msg2']], ['fast-export', '--refspec', 'refs/heads/main:refs/heads/renamed', '--all']),
    ('fast-export refspec no-match', [('write', 'f1.txt', 'content1\n'), ['add', '-A'], ['commit', '-q', '-m', 'msg1'], ('write', 'f2.txt', 'content2\n'), ['add', '-A'], ['commit', '-q', '-m', 'msg2']], ['fast-export', '--refspec', 'refs/heads/master:refs/heads/renamed', '--all']),
    ('fast-export import-marks conflict die', [('write', 'f1.txt', 'content1\n'), ['add', '-A'], ['commit', '-q', '-m', 'msg1']], ['fast-export', '--import-marks=/tmp/pygit_parity_a', '--import-marks-if-exists=/tmp/pygit_parity_b', '--all']),
    ('commit-graph write default loose-only no file', [('write', 'f1.txt', 'content1\n'), ['add', '-A'], ['commit', '-q', '-m', 'msg1'], ('write', 'f2.txt', 'content2\n'), ['add', '-A'], ['commit', '-q', '-m', 'msg2']], ['commit-graph', 'write']),
    ('commit-graph write reachable rc', [('write', 'f1.txt', 'content1\n'), ['add', '-A'], ['commit', '-q', '-m', 'msg1'], ('write', 'f2.txt', 'content2\n'), ['add', '-A'], ['commit', '-q', '-m', 'msg2']], ['commit-graph', 'write', '--reachable']),
    ('multi-pack-index expire rc (overlapping packs via repack -a)', [('write', 'f1.txt', 'c1\n'), ['add', 'f1.txt'], ['commit', '-q', '-m', 'm1'], ['repack'], ('write', 'f2.txt', 'c2\n'), ['add', 'f2.txt'], ['commit', '-q', '-m', 'm2'], ['repack', '-a'], ['multi-pack-index', 'write']], ['multi-pack-index', 'expire']),
    ('multi-pack-index verify after expire', [('write', 'f1.txt', 'c1\n'), ['add', 'f1.txt'], ['commit', '-q', '-m', 'm1'], ['repack'], ('write', 'f2.txt', 'c2\n'), ['add', 'f2.txt'], ['commit', '-q', '-m', 'm2'], ['repack', '-a'], ['multi-pack-index', 'write'], ['multi-pack-index', 'expire']], ['multi-pack-index', 'verify']),
]

BATCH15_STDIN_CASES = [
    ('rev-parse --parseopt emits cat<<EOF usage block', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'first commit']], ['rev-parse', '--parseopt', '--', '--help'], 'h,help\nv,verbose\n--\n'),
    ('rev-parse --parseopt set-- snippet for parsed args', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'first commit']], ['rev-parse', '--parseopt', '--', '-v', 'foo', 'bar'], 'usage line\n--\nh,help show help\nv,verbose be verbose\n'),
    ('am -q shows git-apply hard errors on a failing patch', [('write', 'f.txt', 'orig\ncontent\nhere\n'), ['add', '-A'], ['commit', '-q', '-m', 'base'], ('write', 'f.txt', 'CHANGED\ncontent\nhere\n'), ['add', '-A'], ['commit', '-q', '-m', 'first patch'], ['format-patch', '-1', '-o', 'series'], ['reset', '-q', '--hard', 'HEAD~1'], ('write', 'f.txt', 'DIFFERENT\ncontent\nhere\n'), ['add', '-A'], ['commit', '-q', '-m', 'base commit']], ['am', '-q', 'series/0001-first-patch.patch'], ''),
    ('am -3 add/add: no spurious A lines, labels conflict add/add', [('write', 'f.txt', 'base\n'), ['add', '-A'], ['commit', '-q', '-m', 'base'], ('write', 'newf.txt', 'added content\n'), ['add', '-A'], ['commit', '-q', '-m', 'add newf'], ['format-patch', '-1', '-o', 'sa'], ['reset', '-q', '--hard', 'HEAD~1'], ('write', 'newf.txt', 'target-version different\n'), ['add', '-A'], ['commit', '-q', '-m', 'preexisting newf']], ['am', '-3', 'sa/0001-add-newf.patch'], ''),
    ('am -3 content-3way base reconstruction prints M name-status', [('write', 'f.txt', 'A\nB\nC\nD\nE\n'), ['add', '-A'], ['commit', '-q', '-m', 'base'], ('write', 'f.txt', 'A\nB\nCHANGED\nD\nE\n'), ['add', '-A'], ['commit', '-q', '-m', 'patchcommit'], ['format-patch', '-1', '-o', 'sm'], ['reset', '-q', '--hard', 'HEAD~1'], ('write', 'f.txt', 'TOTALLY\nDIFFERENT\nFILE\nCONTENT\nHERE\n'), ['add', '-A'], ['commit', '-q', '-m', 'b2']], ['am', '-3', 'sm/0001-patchcommit.patch'], ''),
    ('am nonexistent mbox dies with could-not-open message', [('write', 'f', 'x\n'), ['add', '-A'], ['commit', '-q', '-m', 'c']], ['am', '/tmp/nonexistent-xyz.patch'], ''),
    ('pack-objects non-empty stdout empty stdin', [('write', 'f1.txt', 'content1\n'), ['add', '-A'], ['commit', '-q', '-m', 'msg1']], ['pack-objects', '--non-empty', '--stdout'], ''),
]

@pytest.mark.parametrize("case", BATCH15_CASES, ids=[c[0] for c in BATCH15_CASES])
def test_batch15_parity(case, tmp_path: Path, git_254_oracle: str):
    _id, setup, probe = case
    assert_command_parity(git_254_oracle, tmp_path, setup, probe)


@pytest.mark.parametrize("case", BATCH15_STDIN_CASES, ids=[c[0] for c in BATCH15_STDIN_CASES])
def test_batch15_stdin_parity(case, tmp_path: Path, git_254_oracle: str):
    _id, setup, probe, stdin = case
    assert_command_parity(git_254_oracle, tmp_path, setup, probe, stdin=stdin)


# BATCH16: GPG-signing for merge/pull, and pull's parse-options error wording.
# A divergent (non-ff) two-branch history reused by the merge-signing cases.
_DIV_DISTINCT = [
    ('write', 'f', 'base\n'), ['add', 'f'], ['commit', '-qm', 'base'],
    ['checkout', '-b', 'side'], ('write', 's', 'side\n'), ['add', 's'], ['commit', '-qm', 'side'],
    ['checkout', 'main'], ('write', 'm', 'main\n'), ['add', 'm'], ['commit', '-qm', 'main'],
]
_DIV_SAMEFILE = [
    ('write', 'f', 'a\nb\nc\nd\ne\n'), ['add', 'f'], ['commit', '-qm', 'base'],
    ['checkout', '-b', 'side'], ('write', 'f', 'A\nb\nc\nd\ne\n'), ['add', 'f'], ['commit', '-qm', 'side'],
    ['checkout', 'main'], ('write', 'f', 'a\nb\nc\nd\nE\n'), ['add', 'f'], ['commit', '-qm', 'main'],
]

BATCH16_CASES = [
    # merge --no-gpg-sign is accepted (was a parser gap); up to date -> rc 0.
    ('merge --no-gpg-sign up-to-date', [('write', 'f', 'x\n'), ['add', 'f'], ['commit', '-qm', 'c1']], ['merge', '--no-gpg-sign', 'HEAD']),
    # merge -S actually signs: broken gpg (/bin/false) -> rc 128, gpg error block,
    # HEAD unchanged (no merge commit), proving signing is attempted.
    ('merge -S broken-gpg distinct files', _DIV_DISTINCT + [['config', 'gpg.program', '/bin/false']], ['merge', '-S', '-m', 'msg', 'side']),
    # Same with an auto-merged file: the "Auto-merging" notice prints before the error.
    ('merge -S broken-gpg auto-merge same file', _DIV_SAMEFILE + [['config', 'gpg.program', '/bin/false']], ['merge', '-S', '-m', 'msg', 'side']),
    # --no-gpg-sign overrides commit.gpgsign=true even with a broken gpg -> merge succeeds unsigned.
    ('merge --no-gpg-sign overrides commit.gpgsign broken-gpg', _DIV_DISTINCT + [['config', 'commit.gpgsign', 'true'], ['config', 'gpg.program', '/bin/false']], ['merge', '--no-gpg-sign', '-m', 'msg', 'side']),
    # commit.gpgsign=true makes a plain merge attempt signing too -> broken gpg rc 128.
    ('merge config gpgsign broken-gpg rc128', _DIV_DISTINCT + [['config', 'commit.gpgsign', 'true'], ['config', 'gpg.program', '/bin/false']], ['merge', '-m', 'msg', 'side']),
    # pull now accepts -S / --gpg-sign[=keyid] / --no-gpg-sign (forwarded to merge);
    # with no upstream configured it reaches the no-tracking-information error (rc 1).
    ('pull -S no-tracking', [('write', 'f', 'x\n'), ['add', 'f'], ['commit', '-qm', 'c1']], ['pull', '-S']),
    ('pull --gpg-sign=keyid no-tracking', [('write', 'f', 'x\n'), ['add', 'f'], ['commit', '-qm', 'c1']], ['pull', '--gpg-sign=DEADBEEF']),
    ('pull --no-gpg-sign no-tracking', [('write', 'f', 'x\n'), ['add', 'f'], ['commit', '-qm', 'c1']], ['pull', '--no-gpg-sign']),
    # pull's parse-options error wording: short -> "unknown switch", long ->
    # "unknown option", each followed by the full usage block.
    ('pull -m unknown switch + usage', [('write', 'f', 'x\n'), ['add', 'f'], ['commit', '-qm', 'c1']], ['pull', '-m', 'x']),
    ('pull --bogus unknown option + usage', [('write', 'f', 'x\n'), ['add', 'f'], ['commit', '-qm', 'c1']], ['pull', '--bogus']),
    ('pull -Z unknown switch + usage', [('write', 'f', 'x\n'), ['add', 'f'], ['commit', '-qm', 'c1']], ['pull', '-Z']),
]


@pytest.mark.parametrize("case", BATCH16_CASES, ids=[c[0] for c in BATCH16_CASES])
def test_batch16_parity(case, tmp_path: Path, git_254_oracle: str):
    _id, setup, probe = case
    assert_command_parity(git_254_oracle, tmp_path, setup, probe)


# BATCH17: round-3 audit fix-wave Batch 1 (inspect, cat-tree, bundle-io, network).
BATCH17_CASES = [
    ('inspect:check-ref-format--print', [], ['check-ref-format', '--print', 'heads//main']),
    ('inspect:check-ref-format--refspec-pattern', [], ['check-ref-format', '--refspec-pattern', 'refs/heads/*']),
    ('inspect:check-ref-format-midpath-lock', [], ['check-ref-format', 'refs/foo.lock/heads/x']),
    ('inspect:check-ref-format-trailing-dot', [], ['check-ref-format', 'refs/heads/foo.']),
    ('inspect:check-ref-format-leading-slash', [], ['check-ref-format', '/refs/heads/main']),
    ('inspect:check-ref-format-double-slash', [], ['check-ref-format', 'refs//heads/main']),
    ('inspect:check-ref-format-branch-prev', [('write', 'a.txt', 'x\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['check-ref-format', '--branch', '@{-1}']),
    ('inspect:check-ref-format-branch-hier', [], ['check-ref-format', '--branch', 'foo/bar']),
    ('inspect:check-ref-format-plain-quiet', [], ['check-ref-format', 'foo']),
    ('inspect:check-ref-format-unknown-opt', [], ['check-ref-format', '--bogus', 'refs/heads/main']),
    ('inspect:check-ref-format-opt-after-refname', [], ['check-ref-format', 'refs/heads/main', '--normalize']),
    ('inspect:check-ref-format-no-args', [], ['check-ref-format']),
    ('inspect:check-ref-format-extra-refname', [], ['check-ref-format', 'refs/heads/a', 'refs/heads/b']),
    ('inspect:check-mailmap-unsplittable', [('write', '.mailmap', 'Proper Name <proper@example.com> <commit@example.com>\n')], ['check-mailmap', 'justaname']),
    ('inspect:check-mailmap-email-only', [('write', '.mailmap', 'Proper Name <proper@example.com> <commit@example.com>\n')], ['check-mailmap', '<commit@example.com>']),
    ('inspect:check-mailmap-file', [('write', 'extra.map', 'File Name <file@example.com> <commit@example.com>\n')], ['check-mailmap', '--mailmap-file', 'extra.map', 'X <commit@example.com>']),
    ('inspect:check-mailmap-no-contacts', [('write', '.mailmap', 'Proper Name <proper@example.com> <commit@example.com>\n')], ['check-mailmap']),
    ('inspect:check-attr-single-attr-no-dd', [('write', '.gitattributes', '*.txt text\nfoo.txt myattr=hello\n'), ('write', 'foo.txt', 'x\n'), ('write', 'a.txt', 'z\n')], ['check-attr', 'text', 'myattr', 'foo.txt']),
    ('inspect:check-attr-all-ordering', [('write', '.gitattributes', '*.txt text\n*.bin -text\nfoo.txt diff=cpp myattr=hello\n'), ('write', 'foo.txt', 'x\n')], ['check-attr', '-a', 'foo.txt']),
    ('inspect:check-attr-all-binary-macro', [('write', '.gitattributes', '*.bin binary\n'), ('write', 'foo.bin', 'x\n')], ['check-attr', '-a', 'foo.bin']),
    ('inspect:check-attr-source-no-commit', [('write', '.gitattributes', '*.txt text\n'), ('write', 'foo.txt', 'x\n')], ['check-attr', '--source', 'HEAD', 'text', 'foo.txt']),
    ('inspect:check-attr-invalid-name', [('write', '.gitattributes', '*.txt text\n'), ('write', 'foo.txt', 'x\n')], ['check-attr', 'bad name', 'foo.txt']),
    ('inspect:check-attr-no-file', [('write', '.gitattributes', '*.txt text\n'), ('write', 'foo.txt', 'x\n')], ['check-attr', 'text']),
    ('inspect:check-attr-dd-first', [('write', '.gitattributes', '*.txt text\n'), ('write', 'foo.txt', 'x\n')], ['check-attr', '--', 'foo.txt']),
    ('inspect:check-ignore-negated-verbose', [('write', '.gitignore', '*.log\n!important.log\nbuild/\n'), ('write', 'a.log', 'x\n')], ['check-ignore', '-v', 'important.log']),
    ('inspect:check-ignore-tracked-index', [('write', '.gitignore', 'tracked.txt\n'), ('write', 'tracked.txt', 'x\n'), ['add', '-f', 'tracked.txt']], ['check-ignore', 'tracked.txt']),
    ('inspect:check-ignore-no-index', [('write', '.gitignore', 'tracked.txt\n'), ('write', 'tracked.txt', 'x\n'), ['add', '-f', 'tracked.txt']], ['check-ignore', '--no-index', 'tracked.txt']),
    ('inspect:check-ignore-quiet', [('write', '.gitignore', '*.log\n'), ('write', 'a.log', 'x\n')], ['check-ignore', '--quiet', 'a.log']),
    ('inspect:check-ignore-no-path', [], ['check-ignore']),
    ('inspect:check-ignore-quiet-two', [('write', '.gitignore', '*.log\n'), ('write', 'a.log', 'x\n')], ['check-ignore', '--quiet', 'a.log', 'b.log']),
    ('inspect:check-ignore-quiet-verbose', [('write', '.gitignore', '*.log\n'), ('write', 'a.log', 'x\n')], ['check-ignore', '--quiet', '--verbose', 'a.log']),
    ('inspect:check-ignore-z-no-stdin', [('write', '.gitignore', '*.log\n'), ('write', 'a.log', 'x\n')], ['check-ignore', '-z', 'a.log']),
    ('inspect:check-ignore-stdin-path', [('write', '.gitignore', '*.log\n'), ('write', 'a.log', 'x\n')], ['check-ignore', '--stdin', 'a.log']),
    ('inspect:check-ignore-n-no-v', [('write', '.gitignore', '*.log\n'), ('write', 'a.log', 'x\n')], ['check-ignore', '-n', 'a.log']),
    ('inspect:ls-tree-format', [('write', 'a.txt', 'aaa\n'), ['mkdir', 'sub'], ('write', 'sub/b.txt', 'bbb\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['ls-tree', '--format=%(objectname) %(path)', 'HEAD']),
    ('inspect:ls-tree-cmdmode-conflict', [('write', 'a.txt', 'aaa\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['ls-tree', '--name-only', '--long', 'HEAD']),
    ('inspect:ls-tree-format-with-long', [('write', 'a.txt', 'aaa\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['ls-tree', '--format=%(path)', '--long', 'HEAD']),
    ('inspect:ls-tree-no-args', [], ['ls-tree']),
    ('inspect:ls-files-eol', [('write', 'a.txt', 'aaa\n'), ['mkdir', 'sub'], ('write', 'sub/b.txt', 'bbb\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['ls-files', '--eol']),
    ('inspect:ls-files-directory', [('write', 'a.txt', 'aaa\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'c.txt', 'ccc\n'), ['mkdir', 'd'], ('write', 'd/x.txt', 'x\n')], ['ls-files', '-o', '--directory']),
    ('inspect:ls-files-with-tree', [('write', 'a.txt', 'aaa\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['ls-files', '--with-tree=HEAD', '-d']),
    ('inspect:ls-files-deduplicate', [('write', 'a.txt', 'aaa\n'), ['mkdir', 'sub'], ('write', 'sub/b.txt', 'bbb\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['ls-files', '--deduplicate']),
    ('inspect:ls-files-resolve-undo', [('write', 'a.txt', 'aaa\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['ls-files', '--resolve-undo']),
    ('inspect:ls-files-sparse', [('write', 'a.txt', 'aaa\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['ls-files', '--sparse']),
    ('inspect:ls-files-recurse-submodules', [('write', 'a.txt', 'aaa\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['ls-files', '--recurse-submodules']),
    ('inspect:ls-files-exclude-per-directory', [('write', 'a.txt', 'aaa\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['ls-files', '--exclude-per-directory=.gitignore', '-o']),
    ('inspect:status-show-stash', [('write', 'a.txt', 'aaa\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'wip\n'), ['stash']], ['status', '--show-stash']),
    ('inspect:status-ahead-behind', [('write', 'a.txt', 'aaa\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['status', '--ahead-behind']),
    ('inspect:status-no-ahead-behind', [('write', 'a.txt', 'aaa\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['status', '--no-ahead-behind']),
    ('inspect:status-ignored', [('write', '.gitignore', '*.log\n'), ('write', 'x.log', 'data\n'), ('write', 'a.txt', 'aaa\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['status', '--ignored']),
    ('inspect:status-ignored-short', [('write', '.gitignore', '*.log\n'), ('write', 'x.log', 'data\n'), ('write', 'a.txt', 'aaa\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['status', '-s', '--ignored']),
    ('inspect:status-ignore-submodules', [('write', 'a.txt', 'aaa\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['status', '--ignore-submodules']),
    ('inspect:status-column', [('write', 'a.txt', 'aaa\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['status', '--column']),
    ('inspect:grep-bre-literal-plus', [('write', 'f.txt', 'alpha beta\ngamma+delta\nfoo(bar)\nNUMBER 123\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['grep', 'gamma+delta']),
    ('inspect:grep-bre-literal-parens', [('write', 'f.txt', 'alpha beta\ngamma+delta\nfoo(bar)\nNUMBER 123\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['grep', 'foo(bar)']),
    ('inspect:grep-bre-alternation', [('write', 'f.txt', 'alpha beta\ngamma+delta\nfoo(bar)\nNUMBER 123\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['grep', 'NUMBER\\|alpha']),
    ('inspect:grep-G', [('write', 'f.txt', 'alpha beta\ngamma\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['grep', '-G', 'alpha']),
    ('inspect:grep-P-no-pcre', [('write', 'f.txt', 'abc123\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['grep', '-P', '\\d']),
    ('inspect:grep-a-text', [('write', 'f.txt', 'alpha beta\ngamma\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['grep', '-a', 'alpha']),
    ('inspect:grep-exclude-standard-tracked', [('write', 'f.txt', 'alpha beta\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['grep', '--exclude-standard', 'alpha']),
    ('inspect:grep-and-no-preceding', [('write', 'f.txt', 'alpha beta\ngamma\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['grep', '--and', '-e', 'alpha', '-e', 'beta']),
    ('inspect:grep-bool-and-valid', [('write', 'f.txt', 'alpha beta\ngamma\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['grep', '-e', 'alpha', '--and', '-e', 'beta']),
    ('inspect:grep-bool-or-valid', [('write', 'f.txt', 'alpha beta\ngamma\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['grep', '-e', 'alpha', '--or', '-e', 'gamma']),
    ('inspect:blame-rev-positional', [('write', 'f.txt', 'line1\nline2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'f.txt', 'line1\nline2\nline3\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2']], ['blame', 'HEAD~1', 'f.txt']),
    ('inspect:blame-rev-dashdash', [('write', 'f.txt', 'line1\nline2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'f.txt', 'line1\nline2\nline3\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2']], ['blame', 'HEAD', '--', 'f.txt']),
    ('inspect:blame-s', [('write', 'f.txt', 'line1\nline2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'f.txt', 'line1\nline2\nline3\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2']], ['blame', '-s', 'f.txt']),
    ('inspect:blame-incremental', [('write', 'f.txt', 'line1\nline2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'f.txt', 'line1\nline2\nline3\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2']], ['blame', '--incremental', 'f.txt']),
    ('inspect:blame-abbrev', [('write', 'f.txt', 'line1\nline2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['blame', '--abbrev=12', 'f.txt']),
    ('inspect:blame-error-quoting', [('write', 'a.txt', 'x\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['blame', 'nonexist.txt']),
    ('inspect:blame-no-args', [], ['blame']),
    ('inspect:blame-e', [('write', 'f.txt', 'line1\nline2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['blame', '-e', 'f.txt']),
    ('inspect:blame-t', [('write', 'f.txt', 'line1\nline2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['blame', '-t', 'f.txt']),
    ('inspect:blame-n', [('write', 'f.txt', 'line1\nline2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['blame', '-n', 'f.txt']),
    ('inspect:blame-c-annotate-compat', [('write', 'f.txt', 'line1\nline2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['blame', '-c', 'f.txt']),
    ('inspect:annotate-default', [('write', 'f.txt', 'line1\nline2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['annotate', 'f.txt']),
    ('cat-tree:cat-file/head-only', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['cat-file', 'HEAD']),
    ('cat-tree:cat-file/t-too-many', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['cat-file', '-t', 'HEAD', 'extra']),
    ('cat-tree:cat-file/type-object-3pos', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['cat-file', 'commit', 'tree', 'blob']),
    ('cat-tree:cat-file/path-without-cw', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['cat-file', '--path=a.txt', 'HEAD']),
    ('cat-tree:cat-file/filter-outside-batch', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['cat-file', '--filter=blob:none', 'HEAD']),
    ('cat-tree:hash-object/t-missing-value', [], ['hash-object', '-t']),
    ('cat-tree:write-tree/extra-positional', [('write', 'a.txt', 'hi\n'), ['add', '-A']], ['write-tree', 'extra']),
    ('cat-tree:read-tree/-m', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'b.txt', 'two\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2']], ['read-tree', '-m', 'HEAD']),
    ('cat-tree:read-tree/--reset', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'b.txt', 'two\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2']], ['read-tree', '--reset', 'HEAD']),
    ('cat-tree:read-tree/-u-alone', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'b.txt', 'two\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2']], ['read-tree', '-u', 'HEAD']),
    ('cat-tree:read-tree/--empty', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'b.txt', 'two\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2']], ['read-tree', '--empty']),
    ('cat-tree:read-tree/--prefix', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'b.txt', 'two\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2']], ['read-tree', '--prefix=sub/', 'HEAD^{tree}']),
    ('cat-tree:read-tree/-n-m', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'b.txt', 'two\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2']], ['read-tree', '-n', '-m', 'HEAD']),
    ('cat-tree:read-tree/-v', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'b.txt', 'two\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2']], ['read-tree', '-v', 'HEAD']),
    ('cat-tree:read-tree/-i-m', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'b.txt', 'two\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2']], ['read-tree', '-i', '-m', 'HEAD']),
    ('cat-tree:read-tree/--trivial', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'b.txt', 'two\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2']], ['read-tree', '-m', '--trivial', 'HEAD^', 'HEAD']),
    ('cat-tree:read-tree/--aggressive', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'b.txt', 'two\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2']], ['read-tree', '-m', '--aggressive', 'HEAD^', 'HEAD']),
    ('cat-tree:read-tree/--index-output', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'b.txt', 'two\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2']], ['read-tree', '--index-output=.git/altindex', 'HEAD']),
    ('cat-tree:read-tree/-q', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'b.txt', 'two\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2']], ['read-tree', '-q', 'HEAD']),
    ('cat-tree:read-tree/multiple-trees', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'b.txt', 'two\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2']], ['read-tree', '-m', 'HEAD^', 'HEAD']),
    ('cat-tree:read-tree/no-args', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'b.txt', 'two\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2']], ['read-tree']),
    ('cat-tree:read-tree/invalid-name', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'b.txt', 'two\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2']], ['read-tree', 'nonexistent']),
    ('cat-tree:read-tree/which-one', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['read-tree', '-m', '--reset', 'HEAD']),
    ('cat-tree:read-tree/-u-i', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['read-tree', '-u', '-i', '-m', 'HEAD']),
    ('cat-tree:read-tree/empty-with-trees', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['read-tree', '--empty', 'HEAD']),
    ('cat-tree:read-tree/-m-notree', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['read-tree', '-m']),
    ('cat-tree:read-tree/bad-prefix', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['read-tree', '--prefix=/sub', 'HEAD']),
    ('cat-tree:read-tree/-i-nomerge', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['read-tree', '-i', 'HEAD']),
    ('cat-tree:var/shell-path', [], ['var', 'GIT_SHELL_PATH']),
    ('cat-tree:var/config-system', [], ['var', 'GIT_CONFIG_SYSTEM']),
    ('cat-tree:var/config-global', [], ['var', 'GIT_CONFIG_GLOBAL']),
    ('cat-tree:var/attr-global', [], ['var', 'GIT_ATTR_GLOBAL']),
    ('bundle-io:bundle-no-subcommand', [], ['bundle']),
    ('bundle-io:bundle-unknown-subcommand', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['bundle', 'bogus']),
    ('bundle-io:bundle-subcommand-abbrev', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['bundle', 'cre', 'out.bundle', '--all']),
    ('bundle-io:bundle-create-no-file', [], ['bundle', 'create']),
    ('bundle-io:bundle-verify-no-file', [], ['bundle', 'verify']),
    ('bundle-io:bundle-list-heads-no-file', [], ['bundle', 'list-heads']),
    ('bundle-io:bundle-unbundle-no-file', [], ['bundle', 'unbundle']),
    ('bundle-io:bundle-top-h', [], ['bundle', '-h']),
    ('bundle-io:bundle-create-h', [], ['bundle', 'create', '-h']),
    ('bundle-io:bundle-verify-h', [], ['bundle', 'verify', '-h']),
    ('bundle-io:bundle-list-heads-h', [], ['bundle', 'list-heads', '-h']),
    ('bundle-io:bundle-unbundle-h', [], ['bundle', 'unbundle', '-h']),
    ('bundle-io:bundle-create-bogus-flag', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['bundle', 'create', '--bogus', 'out.bundle', '--all']),
    ('bundle-io:bundle-verify-bogus-flag', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['bundle', 'create', 'my.bundle', '--all']], ['bundle', 'verify', '--bogus', 'my.bundle']),
    ('bundle-io:bundle-list-heads-bogus-flag', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['bundle', 'create', 'my.bundle', '--all']], ['bundle', 'list-heads', '--bogus', 'my.bundle']),
    ('bundle-io:bundle-unbundle-bogus-flag', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['bundle', 'create', 'my.bundle', '--all']], ['bundle', 'unbundle', '--bogus', 'my.bundle']),
    ('bundle-io:bundle-version-bad', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['bundle', 'create', '--version=9', 'out.bundle', '--all']),
    ('bundle-io:bundle-version1-before', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['bundle', 'create', '--version=1', 'out.bundle', '--all']),
    ('bundle-io:bundle-q-after-file', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['bundle', 'create', 'out.bundle', '-q', '--all']),
    ('bundle-io:bundle-progress-after-file', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['bundle', 'create', 'out.bundle', '--progress', '--all']),
    ('bundle-io:bundle-version-after-file', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['bundle', 'create', 'out.bundle', '--version=1', '--all']),
    ('bundle-io:bundle-all-progress-after-file', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['bundle', 'create', 'out.bundle', '--all-progress', '--all']),
    ('bundle-io:bundle-verify-q-after-file', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['bundle', 'create', 'my.bundle', '--all']], ['bundle', 'verify', 'my.bundle', '-q']),
    ('bundle-io:bundle-create-empty-incremental', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['branch', 'feature']], ['bundle', 'create', 'x.bundle', 'main', '^feature']),
    ('bundle-io:archive-format-bogus', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['archive', '--format=bogus', 'HEAD']),
    ('bundle-io:archive-format-bogus-no-tree', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['archive', '--format=bogus']),
    ('bundle-io:archive-no-tree-ish', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['archive']),
    ('bundle-io:archive-h', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['archive', '-h']),
    ('bundle-io:archive-bogus-flag', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['archive', '--bogus', 'HEAD']),
    ('bundle-io:archive-mtime-now', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['archive', '--mtime=now', '--format=tar', '-o', 'x.tar', 'HEAD']),
    ('bundle-io:fast-export-signed-tags-bad', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['tag', '-a', 'v1', '-m', 't']], ['fast-export', '--signed-tags=bogus', '--all']),
    ('bundle-io:fast-export-signed-commits-bad', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['fast-export', '--signed-commits=bogus', '--all']),
    ('bundle-io:fast-export-tag-of-filtered-bad', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['fast-export', '--tag-of-filtered-object=bogus', '--all']),
    ('bundle-io:fast-export-reencode-bad', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['fast-export', '--reencode=bogus', '--all']),
    ('bundle-io:fast-export-bogus-opt', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['fast-export', '--bogus-opt', '--all']),
    ('bundle-io:fast-export-progress-bad', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['fast-export', '--progress=abc', '--all']),
    ('bundle-io:fast-export-exclude-rev', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'b.txt', 'world\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['branch', 'feature', 'HEAD~1']], ['fast-export', 'main', '^feature']),
    ('bundle-io:fast-export-range', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'b.txt', 'world\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['branch', 'feature', 'HEAD~1']], ['fast-export', 'feature..main']),
    ('bundle-io:fast-export-not', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'b.txt', 'world\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['branch', 'feature', 'HEAD~1']], ['fast-export', '--not', 'feature', 'main']),
    ('bundle-io:fast-export-reference-excluded-parents', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'b.txt', 'world\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['branch', 'feature']], ['fast-export', '--reference-excluded-parents', 'main', '^feature']),
    ('bundle-io:remote-archive-h', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['archive', '--remote=.', '-h']),
    ('network:ls-remote-get-url-insteadof', [['remote', 'add', 'origin', 'https://example.com/foo.git'], ['config', 'url.git@host:.insteadOf', 'https://example.com/']], ['ls-remote', '--get-url', 'origin']),
    ('network:ls-remote-exec-local', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['ls-remote', '--exec=/bin/true', '.']),
    ('network:ls-remote-h-synonym', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['ls-remote', '-h', '.']),
    ('network:ls-remote-ambiguous-ex', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['ls-remote', '--ex', '.']),
    ('network:fetch-pack-diag-url-ssh', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['fetch-pack', '--diag-url', 'ssh://user@host:22/path/to/repo.git']),
    ('network:fetch-pack-diag-url-scp', [], ['fetch-pack', '--diag-url', 'user@host:path/to/repo.git']),
    ('network:fetch-pack-diag-url-file', [], ['fetch-pack', '--diag-url', '/path/to/repo.git']),
    ('network:fetch-pack-diag-url-git', [], ['fetch-pack', '--diag-url', 'git://host/repo.git']),
    ('network:fetch-pack-diag-url-https-unsupported', [], ['fetch-pack', '--diag-url', 'https://host/repo.git']),
    ('network:send-pack-local', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['send-pack', '../dst.git', 'main']),
    ('network:send-pack-dry-run', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['send-pack', '--dry-run', '../dst.git', 'main']),
    ('network:send-pack-all', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['send-pack', '--all', '../dst.git']),
    ('network:request-pull-p', [('write', 'a.txt', 'a\n'), ['add', '-A'], ['commit', '-q', '-m', 'initial commit'], ['tag', '-a', 'v1', '-m', 'version 1'], ('write', 'b.txt', 'b\n'), ['add', '-A'], ['commit', '-q', '-m', 'add feature b']], ['request-pull', '-p', 'v1', '../pub.git']),
    ('network:request-pull-no-p', [('write', 'a.txt', 'a\n'), ['add', '-A'], ['commit', '-q', '-m', 'initial commit'], ['tag', '-a', 'v1', '-m', 'version 1'], ('write', 'b.txt', 'b\n'), ['add', '-A'], ['commit', '-q', '-m', 'add feature b']], ['request-pull', 'v1', '../pub.git']),
    ('network:remote-show-n', [['remote', 'add', 'origin', 'https://example.com/foo.git']], ['remote', 'show', '-n', 'origin']),
    ('network:remote-show-unconfigured', [], ['remote', 'show', 'origin']),
    ('network:remote-unknown-subcommand', [], ['remote', 'frobnicate']),
    ('network:remote-prune', [], ['remote', 'prune', 'foo']),
    ('network:remote-set-head-auto', [['remote', 'add', 'origin', 'https://example.com/foo.git']], ['remote', 'set-head', '-a', 'foo']),
    ('network:remote-set-head-branch', [['remote', 'add', 'origin', 'https://example.com/foo.git'], ('write', '.git/refs/remotes/origin/main', '1111111111111111111111111111111111111111\n')], ['remote', 'set-head', 'origin', 'main']),
    ('network:remote-rename-branch-rewrite', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['remote', 'add', 'origin', 'https://example.com/foo.git'], ['config', 'branch.main.remote', 'origin'], ['remote', 'rename', 'origin', 'upstream']], ['config', 'branch.main.remote']),
    ('network:remote-rename-existing', [['remote', 'add', 'origin', 'https://example.com/a.git'], ['remote', 'add', 'other', 'https://example.com/b.git']], ['remote', 'rename', 'origin', 'other']),
    ('network:remote-add-mirror-bogus', [], ['remote', 'add', '--mirror=bogus', 'm2', 'http://x']),
    ('network:fetch-unshallow-complete', [['remote', 'add', 'origin', './rem']], ['fetch', '--unshallow', 'origin']),
    ('network:fetch-negotiate-only', [['remote', 'add', 'origin', './rem']], ['fetch', '--negotiate-only', 'origin']),
    ('network:fetch-dry-abbrev', [['remote', 'add', 'origin', './rem']], ['fetch', '--dry', 'origin']),
    ('network:push-porcelain-new', [('write', 'a.txt', 'a\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['remote', 'add', 'origin', '../dst.git']], ['push', '--porcelain', 'origin', 'main']),
    ('network:push-force-with-lease-new', [('write', 'a.txt', 'a\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['remote', 'add', 'origin', '../dst.git']], ['push', '--force-with-lease', 'origin', 'main']),
    ('network:push-repo-positional-override', [('write', 'a.txt', 'a\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['remote', 'add', 'origin', '../dst.git']], ['push', '--repo', 'origin', 'main']),
    ('network:push-follow-tags', [('write', 'a.txt', 'a\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['remote', 'add', 'origin', '../dst.git']], ['push', '--follow-tags', 'origin', 'main']),
    ('network:push-prune', [('write', 'a.txt', 'a\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['remote', 'add', 'origin', '../dst.git']], ['push', '--prune', 'origin', 'main']),
    ('network:push-signed-no', [('write', 'a.txt', 'a\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['remote', 'add', 'origin', '../dst.git']], ['push', '--signed=no', 'origin', 'main']),
    ('network:push-recurse-submodules-no', [('write', 'a.txt', 'a\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['remote', 'add', 'origin', '../dst.git']], ['push', '--recurse-submodules=no', 'origin', 'main']),
    ('network:push-porcelain-uptodate', [('write', 'a.txt', 'a\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['remote', 'add', 'origin', '../dst.git'], ['push', '-q', 'origin', 'main']], ['push', '--porcelain', 'origin', 'main']),
    ('cat-tree:read-tree --exclude-per-directory bare requires value', [('write', 'a', 'a\n'), ['add', 'a'], ['commit', '-q', '-m', 'c1']], ['read-tree', '--exclude-per-directory']),
    ('cat-tree:read-tree --exclude-per-directory valued without -u', [('write', 'a', 'a\n'), ['add', 'a'], ['commit', '-q', '-m', 'c1']], ['read-tree', '--exclude-per-directory=.gitignore', 'HEAD']),
    ('cat-tree:read-tree --exclude-per-directory before -u dies (order-dependent cb)', [('write', 'a', 'a\n'), ['add', 'a'], ['commit', '-q', '-m', 'c1']], ['read-tree', '--exclude-per-directory=.gitignore', '-u', '-m', 'HEAD']),
    ('cat-tree:read-tree -u -m --exclude-per-directory ok', [('write', 'a', 'a\n'), ['add', 'a'], ['commit', '-q', '-m', 'c1']], ['read-tree', '-u', '-m', '--exclude-per-directory=.gitignore', 'HEAD']),
]

BATCH17_STDIN_CASES = [
    ('inspect:check-attr-stdin', [('write', '.gitattributes', '*.txt text\n'), ('write', 'foo.txt', 'x\n')], ['check-attr', '--stdin', 'text'], 'foo.txt\n'),
    ('inspect:check-ignore-stdin', [('write', '.gitignore', '*.log\n'), ('write', 'a.log', 'x\n')], ['check-ignore', '--stdin'], 'a.log\nb.txt\n'),
    ('cat-tree:cat-file/batch-with-positional', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['cat-file', '--batch', 'HEAD'], ''),
    ('cat-tree:cat-file/attr-with-batch', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['cat-file', '-t', '--batch'], ''),
    ('cat-tree:hash-object/invalid-type', [], ['hash-object', '-t', 'frobnicate', '--stdin'], 'x\n'),
    ('cat-tree:hash-object/invalid-type-literally', [], ['hash-object', '-t', 'frobnicate', '--literally', '--stdin'], 'x\n'),
    ('cat-tree:hash-object/uppercase-type', [], ['hash-object', '-t', 'Blob', '--stdin'], 'x\n'),
    ('cat-tree:hash-object/empty-type', [], ['hash-object', '-t', '', '--stdin'], 'x\n'),
    ('cat-tree:mktag/missing-tagged-object', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['mktag'], 'object 0000000000000000000000000000000000000000\ntype commit\ntag v1\ntagger Parity <parity@example.com> 1700000000 +0000\n\nmsg\n'),
    ('cat-tree:mktag/type-mismatch', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['mktag'], 'object ce013625030ba8dba906f756967f9e9ca394464a\ntype commit\ntag v1\ntagger Parity <parity@example.com> 1700000000 +0000\n\nmsg\n'),
    ('cat-tree:mktag/missing-tagger', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['mktag'], 'object fafef46ef578e96260e395f90e712ae5dc954743\ntype commit\ntag v1\n\nmsg\n'),
    ('cat-tree:mktag/wrong-header-order', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['mktag'], 'type commit\nobject fafef46ef578e96260e395f90e712ae5dc954743\ntag v1\ntagger Parity <parity@example.com> 1700000000 +0000\n\nmsg\n'),
    ('cat-tree:mktag/extra-header', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['mktag'], 'object fafef46ef578e96260e395f90e712ae5dc954743\ntype commit\ntag v1\ntagger Parity <parity@example.com> 1700000000 +0000\nextra header\n\nmsg\n'),
    ('cat-tree:mktag/bad-object-sha', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['mktag'], 'object zzzz\ntype commit\ntag v1\ntagger Parity <parity@example.com> 1700000000 +0000\n\nmsg\n'),
    ('cat-tree:mktag/bad-type', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['mktag'], 'object fafef46ef578e96260e395f90e712ae5dc954743\ntype frob\ntag v1\ntagger Parity <parity@example.com> 1700000000 +0000\n\nmsg\n'),
    ('cat-tree:mktag/empty-tag-name', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['mktag'], 'object fafef46ef578e96260e395f90e712ae5dc954743\ntype commit\ntag \ntagger Parity <parity@example.com> 1700000000 +0000\n\nmsg\n'),
    ('cat-tree:mktag/bad-tagger-email', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['mktag'], 'object fafef46ef578e96260e395f90e712ae5dc954743\ntype commit\ntag v1\ntagger Parity 1700000000 +0000\n\nmsg\n'),
    ('cat-tree:mktag/missing-tag-line', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['mktag'], 'object fafef46ef578e96260e395f90e712ae5dc954743\ntype commit\ntagger Parity <parity@example.com> 1700000000 +0000\n\nmsg\n'),
    ('cat-tree:mktag/empty-input', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['mktag'], ''),
    ('cat-tree:mktag/strict-valid', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['mktag', '--strict'], 'object fafef46ef578e96260e395f90e712ae5dc954743\ntype commit\ntag v1\ntagger Parity <parity@example.com> 1700000000 +0000\n\nmsg\n'),
    ('cat-tree:mktag/no-strict-tagger-warns', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['mktag', '--no-strict'], 'object fafef46ef578e96260e395f90e712ae5dc954743\ntype commit\ntag v1\n\nmsg\n'),
    ('cat-tree:commit-tree/dup-parent', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['commit-tree', '-p', 'fafef46ef578e96260e395f90e712ae5dc954743', '-p', 'fafef46ef578e96260e395f90e712ae5dc954743', '-m', 'x', 'HEAD^{tree}'], ''),
    ('cat-tree:commit-tree/noncommit-parent', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['commit-tree', '-p', 'ce013625030ba8dba906f756967f9e9ca394464a', '-m', 'x', 'HEAD^{tree}'], ''),
    ('cat-tree:commit-tree/F-stdin', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['commit-tree', '-F', '-', 'HEAD^{tree}'], 'hi from stdin\n'),
    ('cat-tree:commit-tree/no-tree', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['commit-tree', '-m', 'x'], ''),
    ('cat-tree:commit-tree/extra-tree', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['commit-tree', '-m', 'x', 'HEAD^{tree}', 'HEAD^{tree}'], ''),
    ('cat-tree:commit-tree/message-long', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['commit-tree', '--message=hi', 'HEAD^{tree}'], ''),
    ('cat-tree:commit-tree/parent-long', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['commit-tree', '--parent', 'X', 'HEAD^{tree}'], ''),
    ('cat-tree:commit-tree/file-long', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['commit-tree', '--file', 'X', 'HEAD^{tree}'], ''),
    ('cat-tree:commit-tree/m-trailing-nl', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['commit-tree', '-m', 'line1\n', 'HEAD^{tree}'], ''),
    ('cat-tree:commit-tree/empty-msg', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['commit-tree', 'HEAD^{tree}'], ''),
    ('cat-tree:commit-tree/bad-tree', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['commit-tree', '-m', 'x', 'nonexistent'], ''),
    ('cat-tree:commit-tree/bad-parent-name', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['commit-tree', '-p', 'doesnotexist', '-m', 'x', 'HEAD^{tree}'], ''),
    ('cat-tree:commit-tree/absent-parent', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['commit-tree', '-p', '1111111111111111111111111111111111111111', '-m', 'x', 'HEAD^{tree}'], ''),
    ('cat-tree:stripspace/s-c-exclusive', [], ['stripspace', '-s', '-c'], 'x\n'),
    ('cat-tree:stripspace/c-s-exclusive', [], ['stripspace', '-c', '-s'], 'x\n'),
    ('cat-tree:stripspace/s-indented-comment', [], ['stripspace', '-s'], '   # indented comment\nkeep\n'),
    ('cat-tree:stripspace/c-no-trailing-nl', [], ['stripspace', '-c'], 'no newline'),
    ('cat-tree:stripspace/s-commentchar', [['config', 'core.commentChar', ';']], ['stripspace', '-s'], '; comment\nkeep\n# notcomment\n'),
    ('cat-tree:stripspace/c-commentchar', [['config', 'core.commentChar', ';']], ['stripspace', '-c'], 'hi\n'),
    ('cat-tree:interpret-trailers/trailer-noval', [], ['interpret-trailers', '--trailer'], ''),
    ('cat-tree:interpret-trailers/where-noval', [], ['interpret-trailers', '--where'], ''),
    ('cat-tree:interpret-trailers/ifexists-noval', [], ['interpret-trailers', '--if-exists'], ''),
    ('cat-tree:interpret-trailers/ifmissing-noval', [], ['interpret-trailers', '--if-missing'], ''),
    ('cat-tree:get-tar-commit-id/empty-input', [], ['get-tar-commit-id'], ''),
    ('cat-tree:get-tar-commit-id/short-comment', [], ['get-tar-commit-id'], 'comment=deadbeef\n'),
    ('cat-tree:get-tar-commit-id/extra-positional', [], ['get-tar-commit-id', 'extra'], ''),
    ('bundle-io:fast-import-signed-commits-bad', [], ['fast-import', '--signed-commits=bogus'], ''),
    ('bundle-io:fast-import-signed-tags-bad', [], ['fast-import', '--signed-tags=bogus'], ''),
    ('bundle-io:fast-import-max-pack-size-small', [], ['fast-import', '--max-pack-size=5'], ''),
    ('bundle-io:fast-import-stats-empty', [], ['fast-import'], ''),
    ('bundle-io:upload-archive-no-args', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['upload-archive'], ''),
    ('bundle-io:upload-archive-two-args', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['upload-archive', '.', 'extra'], ''),
    ('bundle-io:upload-archive-writer-no-args', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['upload-archive--writer'], ''),
]

@pytest.mark.parametrize("case", BATCH17_CASES, ids=[c[0] for c in BATCH17_CASES])
def test_batch17_parity(case, tmp_path: Path, git_254_oracle: str):
    _id, setup, probe = case
    assert_command_parity(git_254_oracle, tmp_path, setup, probe)


@pytest.mark.parametrize("case", BATCH17_STDIN_CASES, ids=[c[0] for c in BATCH17_STDIN_CASES])
def test_batch17_stdin_parity(case, tmp_path: Path, git_254_oracle: str):
    _id, setup, probe, stdin = case
    assert_command_parity(git_254_oracle, tmp_path, setup, probe, stdin=stdin)


# BATCH18: round-3 audit fix-wave Batch 2 (commit-merge, branch-ref, objpack, apply-index).
BATCH18_CASES = [
    ('commit-merge:commit-allow-empty-message', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'c.txt', 'x\n'), ['add', '-A']], ['commit', '--allow-empty-message', '-m', '']),
    ('commit-merge:commit-date-invalid', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'c.txt', 'x\n'), ['add', '-A']], ['commit', '-m', 'x', '--date', 'not-a-date']),
    ('commit-merge:commit-cleanup-invalid', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'c.txt', 'x\n'), ['add', '-A']], ['commit', '-m', 'x', '--cleanup=bogus']),
    ('commit-merge:commit-untracked-invalid', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'c.txt', 'x\n'), ['add', '-A']], ['commit', '--dry-run', '-ubogus']),
    ('commit-merge:commit-dryrun-verbose', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'c.txt', 'x\n'), ['add', '-A']], ['commit', '--dry-run', '-v']),
    ('commit-merge:commit-long-z', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'c.txt', 'x\n'), ['add', '-A']], ['commit', '--dry-run', '--long', '-z']),
    ('commit-merge:commit-only-no-path', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'ch\n')], ['commit', '-o', '-m', 'x']),
    ('commit-merge:merge-no-commit', [('write', 'base.txt', 'base\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['checkout', '-q', '-b', 'feature'], ('write', 'feat.txt', 'feat\n'), ['add', '-A'], ['commit', '-q', '-m', 'feat'], ['checkout', '-q', 'main'], ('write', 'm2.txt', 'm\n'), ['add', '-A'], ['commit', '-q', '-m', 'm2']], ['merge', '--no-commit', '-m', 'x', 'feature']),
    ('commit-merge:merge-squash', [('write', 'base.txt', 'base\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['checkout', '-q', '-b', 'feature'], ('write', 'feat.txt', 'feat\n'), ['add', '-A'], ['commit', '-q', '-m', 'feat'], ['checkout', '-q', 'main'], ('write', 'm2.txt', 'm\n'), ['add', '-A'], ['commit', '-q', '-m', 'm2']], ['merge', '--squash', 'feature']),
    ('commit-merge:merge-log', [('write', 'base.txt', 'base\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['checkout', '-q', '-b', 'feature'], ('write', 'feat.txt', 'feat\n'), ['add', '-A'], ['commit', '-q', '-m', 'feat'], ['checkout', '-q', 'main'], ('write', 'm2.txt', 'm\n'), ['add', '-A'], ['commit', '-q', '-m', 'm2']], ['merge', '--log', '-m', 'x', 'feature']),
    ('commit-merge:merge-summary', [('write', 'base.txt', 'base\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['checkout', '-q', '-b', 'feature'], ('write', 'feat.txt', 'feat\n'), ['add', '-A'], ['commit', '-q', '-m', 'feat'], ['checkout', '-q', 'main'], ('write', 'm2.txt', 'm\n'), ['add', '-A'], ['commit', '-q', '-m', 'm2']], ['merge', '--summary', '-m', 'x', 'feature']),
    ('commit-merge:merge-compact-summary', [('write', 'base.txt', 'base\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['checkout', '-q', '-b', 'feature'], ('write', 'feat.txt', 'feat\n'), ['add', '-A'], ['commit', '-q', '-m', 'feat'], ['checkout', '-q', 'main'], ('write', 'm2.txt', 'm\n'), ['add', '-A'], ['commit', '-q', '-m', 'm2']], ['merge', '--compact-summary', '-m', 'x', 'feature']),
    ('commit-merge:merge-signoff', [('write', 'base.txt', 'base\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['checkout', '-q', '-b', 'feature'], ('write', 'feat.txt', 'feat\n'), ['add', '-A'], ['commit', '-q', '-m', 'feat'], ['checkout', '-q', 'main'], ('write', 'm2.txt', 'm\n'), ['add', '-A'], ['commit', '-q', '-m', 'm2']], ['merge', '--signoff', '-m', 'x', 'feature']),
    ('commit-merge:merge-into-name', [('write', 'base.txt', 'base\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['checkout', '-q', '-b', 'feature'], ('write', 'feat.txt', 'feat\n'), ['add', '-A'], ['commit', '-q', '-m', 'feat'], ['checkout', '-q', 'main'], ('write', 'm2.txt', 'm\n'), ['add', '-A'], ['commit', '-q', '-m', 'm2']], ['merge', '--into-name', 'custom', '-m', 'x', 'feature']),
    ('commit-merge:merge-quit', [('write', 'base.txt', 'base\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['checkout', '-q', '-b', 'feature'], ('write', 'feat.txt', 'feat\n'), ['add', '-A'], ['commit', '-q', '-m', 'feat'], ['checkout', '-q', 'main'], ('write', 'm2.txt', 'm\n'), ['add', '-A'], ['commit', '-q', '-m', 'm2']], ['merge', '--quit']),
    ('commit-merge:merge-autostash', [('write', 'base.txt', 'base\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['checkout', '-q', '-b', 'feature'], ('write', 'feat.txt', 'feat\n'), ['add', '-A'], ['commit', '-q', '-m', 'feat'], ['checkout', '-q', 'main'], ('write', 'm2.txt', 'm\n'), ['add', '-A'], ['commit', '-q', '-m', 'm2'], ('write', 'base.txt', 'dirty\n')], ['merge', '--autostash', '-m', 'x', 'feature']),
    ('commit-merge:merge-rerere-autoupdate', [('write', 'base.txt', 'base\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['checkout', '-q', '-b', 'feature'], ('write', 'feat.txt', 'feat\n'), ['add', '-A'], ['commit', '-q', '-m', 'feat'], ['checkout', '-q', 'main'], ('write', 'm2.txt', 'm\n'), ['add', '-A'], ['commit', '-q', '-m', 'm2']], ['merge', '--rerere-autoupdate', '-m', 'x', 'feature']),
    ('commit-merge:merge-cleanup-invalid', [('write', 'base.txt', 'base\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['checkout', '-q', '-b', 'feature'], ('write', 'feat.txt', 'feat\n'), ['add', '-A'], ['commit', '-q', '-m', 'feat'], ['checkout', '-q', 'main'], ('write', 'm2.txt', 'm\n'), ['add', '-A'], ['commit', '-q', '-m', 'm2']], ['merge', '--cleanup=bogus', '-m', 'x', 'feature']),
    ('commit-merge:merge-no-args-defaulttoupstream', [('write', 'base.txt', 'base\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['checkout', '-q', '-b', 'feature'], ('write', 'feat.txt', 'feat\n'), ['add', '-A'], ['commit', '-q', '-m', 'feat'], ['checkout', '-q', 'main'], ('write', 'm2.txt', 'm\n'), ['add', '-A'], ['commit', '-q', '-m', 'm2']], ['merge']),
    ('commit-merge:merge-ff-message-ignored', [('write', 'base.txt', 'base\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['checkout', '-q', '-b', 'feature'], ('write', 'feat.txt', 'feat\n'), ['add', '-A'], ['commit', '-q', '-m', 'feat'], ['checkout', '-q', 'main']], ['merge', '-m', 'ignored', 'feature']),
    ('commit-merge:mergetree-trivial-flag', [('write', 'base.txt', 'base\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['checkout', '-q', '-b', 'feature'], ('write', 'feat.txt', 'feat\n'), ['add', '-A'], ['commit', '-q', '-m', 'feat'], ['checkout', '-q', 'main'], ('write', 'main2.txt', 'main2\n'), ['add', '-A'], ['commit', '-q', '-m', 'main2']], ['merge-tree', '--trivial-merge', 'main^', 'main', 'feature']),
    ('commit-merge:mergetree-trivial-legacy', [('write', 'base.txt', 'base\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['checkout', '-q', '-b', 'feature'], ('write', 'feat.txt', 'feat\n'), ['add', '-A'], ['commit', '-q', '-m', 'feat'], ['checkout', '-q', 'main'], ('write', 'main2.txt', 'main2\n'), ['add', '-A'], ['commit', '-q', '-m', 'main2']], ['merge-tree', 'main^', 'main', 'feature']),
    ('commit-merge:mergetree-trivial-conflict', [('write', 'f.txt', 'l1\nl2\nl3\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['checkout', '-q', '-b', 'feature'], ('write', 'f.txt', 'l1\nFEAT\nl3\n'), ['add', '-A'], ['commit', '-q', '-m', 'feat'], ['checkout', '-q', 'main'], ('write', 'f.txt', 'l1\nMAIN\nl3\n'), ['add', '-A'], ['commit', '-q', '-m', 'main2']], ['merge-tree', 'main^', 'main', 'feature']),
    ('commit-merge:cherrypick-range', [('write', 'base.txt', 'base\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['checkout', '-q', '-b', 'feature'], ('write', 'f1.txt', 'one\n'), ['add', '-A'], ['commit', '-q', '-m', 'pick1'], ('write', 'f2.txt', 'two\n'), ['add', '-A'], ['commit', '-q', '-m', 'pick2'], ['checkout', '-q', 'main']], ['cherry-pick', 'feature~1..feature']),
    ('commit-merge:cherrypick-conflict', [('write', 'f.txt', 'base\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['checkout', '-q', '-b', 'feature'], ('write', 'f.txt', 'feature\n'), ['add', '-A'], ['commit', '-q', '-m', 'feat'], ['checkout', '-q', 'main'], ('write', 'f.txt', 'mainside\n'), ['add', '-A'], ['commit', '-q', '-m', 'main2']], ['cherry-pick', 'feature']),
    ('commit-merge:cherrypick-x', [('write', 'base.txt', 'base\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['checkout', '-q', '-b', 'feature'], ('write', 'f1.txt', 'one\n'), ['add', '-A'], ['commit', '-q', '-m', 'pick1'], ['checkout', '-q', 'main']], ['cherry-pick', '-x', 'feature']),
    ('commit-merge:cherrypick-abort-nosession', [('write', 'base.txt', 'base\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['cherry-pick', '--abort']),
    ('commit-merge:cherrypick-noargs-usage', [('write', 'base.txt', 'base\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['cherry-pick']),
    ('commit-merge:revert-range', [('write', 'base.txt', 'base\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'f1.txt', 'one\n'), ['add', '-A'], ['commit', '-q', '-m', 'r1'], ('write', 'f2.txt', 'two\n'), ['add', '-A'], ['commit', '-q', '-m', 'r2']], ['revert', '--no-edit', 'HEAD~2..HEAD']),
    ('commit-merge:revert-signoff', [('write', 'base.txt', 'base\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'f1.txt', 'one\n'), ['add', '-A'], ['commit', '-q', '-m', 'r1']], ['revert', '--no-edit', '-s', 'HEAD']),
    ('commit-merge:revert-abort-nosession', [('write', 'base.txt', 'base\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'f1.txt', 'one\n'), ['add', '-A'], ['commit', '-q', '-m', 'r1']], ['revert', '--abort']),
    ('commit-merge:revert-noargs-usage', [('write', 'base.txt', 'base\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'f1.txt', 'one\n'), ['add', '-A'], ['commit', '-q', '-m', 'r1']], ['revert']),
    ('commit-merge:rebase-quiet', [('write', 'base.txt', 'base\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['checkout', '-q', '-b', 'feature'], ('write', 'f1.txt', 'one\n'), ['add', '-A'], ['commit', '-q', '-m', 'f1'], ['checkout', '-q', 'main'], ('write', 'm2.txt', 'm2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['checkout', '-q', 'feature']], ['rebase', '-q', 'main']),
    ('commit-merge:rebase-continue-nosession', [('write', 'base.txt', 'base\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['checkout', '-q', '-b', 'feature'], ('write', 'f1.txt', 'one\n'), ['add', '-A'], ['commit', '-q', '-m', 'f1'], ['checkout', '-q', 'main'], ('write', 'm2.txt', 'm2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['checkout', '-q', 'feature']], ['rebase', '--continue']),
    ('commit-merge:rebase-no-upstream', [('write', 'base.txt', 'base\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['checkout', '-q', '-b', 'feature'], ('write', 'f1.txt', 'one\n'), ['add', '-A'], ['commit', '-q', '-m', 'f1'], ['checkout', '-q', 'main'], ('write', 'm2.txt', 'm2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['checkout', '-q', 'feature']], ['rebase']),
    ('commit-merge:reset-mode-combo', [('write', 'a.txt', 'a1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'a2\n'), ('write', 'b.txt', 'b\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2']], ['reset', '--soft', '--hard', 'HEAD~1']),
    ('commit-merge:reset-nonexistent', [('write', 'a.txt', 'a1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'a2\n'), ('write', 'b.txt', 'b\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2']], ['reset', 'nonexistent']),
    ('commit-merge:reset-hard-nonexistent', [('write', 'a.txt', 'a1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'a2\n'), ('write', 'b.txt', 'b\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2']], ['reset', '--hard', 'nonexistent']),
    ('commit-merge:reset-recurse-submodules', [('write', 'a.txt', 'a1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'a2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2']], ['reset', '--recurse-submodules', '--hard', 'HEAD~1']),
    ('commit-merge:reset-N-missing-path', [('write', 'a.txt', 'a1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'b.txt', 'b\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ('rm', 'b.txt')], ['reset', '-N', 'b.txt']),
    ('commit-merge:restore-nonexistent-path', [('write', 'a.txt', 'a1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'a2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2']], ['restore', 'nope.txt']),
    ('commit-merge:restore-no-path', [('write', 'a.txt', 'a1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'dirty\n')], ['restore']),
    ('commit-merge:restore-S-staged', [('write', 'a.txt', 'a1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'staged\n'), ['add', 'a.txt']], ['restore', '-S', 'a.txt']),
    ('commit-merge:restore-ours', [('write', 'a.txt', 'a1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'dirty\n')], ['restore', '--ours', 'a.txt']),
    ('commit-merge:am-directory', [('write', 'a.txt', 'base\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'p1.patch', 'FROM_FORMAT_PATCH_0001')], ['am', '--directory=sub', 'p1.patch']),
    ('commit-merge:am-3way-apply-error-order', [('write', 'a.txt', 'base\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'bad.patch', 'FROM_FORMAT_PATCH_0001_CONTEXT_MANGLED')], ['am', '-3', 'bad.patch']),
    ('commit-merge:am-whitespace-invalid-order', [('write', 'a.txt', 'base\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'p1.patch', 'FROM_FORMAT_PATCH_0001')], ['am', '--whitespace=bogus', 'p1.patch']),
    ('branch-ref:branch_--set-upstream', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'b.txt', 'yo\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['branch', 'feature'], ['update-ref', 'refs/remotes/origin/main', 'HEAD'], ['tag', 'v1']], ['branch', '--set-upstream', 'feature']),
    ('branch-ref:branch_--track_value', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'b.txt', 'yo\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['branch', 'feature'], ['update-ref', 'refs/remotes/origin/main', 'HEAD'], ['tag', 'v1']], ['branch', '--track=bogus', 'feat4', 'main']),
    ('branch-ref:branch_--track_inherit', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'b.txt', 'yo\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['branch', 'feature'], ['update-ref', 'refs/remotes/origin/main', 'HEAD'], ['tag', 'v1']], ['branch', '--track=inherit', 'feat3', 'main']),
    ('branch-ref:branch_--unset-upstream', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'b.txt', 'yo\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['branch', 'feature'], ['update-ref', 'refs/remotes/origin/main', 'HEAD'], ['tag', 'v1']], ['branch', '--unset-upstream']),
    ('branch-ref:branch_--omit-empty', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'b.txt', 'yo\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['branch', 'feature'], ['update-ref', 'refs/remotes/origin/main', 'HEAD'], ['tag', 'v1']], ['branch', '--omit-empty']),
    ('branch-ref:branch_--create-reflog', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'b.txt', 'yo\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['branch', 'feature'], ['update-ref', 'refs/remotes/origin/main', 'HEAD'], ['tag', 'v1']], ['branch', '--create-reflog', 'newbr1']),
    ('branch-ref:branch_--edit-description', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'b.txt', 'yo\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['branch', 'feature'], ['update-ref', 'refs/remotes/origin/main', 'HEAD'], ['tag', 'v1']], ['branch', '--edit-description']),
    ('branch-ref:branch_--column', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'b.txt', 'yo\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['branch', 'feature'], ['update-ref', 'refs/remotes/origin/main', 'HEAD'], ['tag', 'v1']], ['branch', '--column']),
    ('branch-ref:branch_--no-column', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'b.txt', 'yo\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['branch', 'feature'], ['update-ref', 'refs/remotes/origin/main', 'HEAD'], ['tag', 'v1']], ['branch', '--no-column']),
    ('branch-ref:branch_--abbrev', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'b.txt', 'yo\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['branch', 'feature'], ['update-ref', 'refs/remotes/origin/main', 'HEAD'], ['tag', 'v1']], ['branch', '--abbrev=8', '-v']),
    ('branch-ref:branch_--no-abbrev', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'b.txt', 'yo\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['branch', 'feature'], ['update-ref', 'refs/remotes/origin/main', 'HEAD'], ['tag', 'v1']], ['branch', '--no-abbrev', '-v']),
    ('branch-ref:branch_--color', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'b.txt', 'yo\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['branch', 'feature'], ['update-ref', 'refs/remotes/origin/main', 'HEAD'], ['tag', 'v1']], ['branch', '--color']),
    ('branch-ref:branch_--no-color', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'b.txt', 'yo\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['branch', 'feature'], ['update-ref', 'refs/remotes/origin/main', 'HEAD'], ['tag', 'v1']], ['branch', '--no-color']),
    ('branch-ref:branch_--with', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'b.txt', 'yo\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['branch', 'feature'], ['update-ref', 'refs/remotes/origin/main', 'HEAD'], ['tag', 'v1']], ['branch', '--with', 'HEAD']),
    ('branch-ref:branch_--without', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'b.txt', 'yo\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['branch', 'feature'], ['update-ref', 'refs/remotes/origin/main', 'HEAD'], ['tag', 'v1']], ['branch', '--without', 'HEAD']),
    ('branch-ref:branch_--no-track', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'b.txt', 'yo\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['branch', 'feature'], ['update-ref', 'refs/remotes/origin/main', 'HEAD'], ['tag', 'v1']], ['branch', '--no-track', 'x1', 'HEAD']),
    ('branch-ref:branch_-a_-r', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'b.txt', 'yo\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['branch', 'feature'], ['update-ref', 'refs/remotes/origin/main', 'HEAD'], ['tag', 'v1']], ['branch', '-a', '-r']),
    ('branch-ref:branch_--sort', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'b.txt', 'yo\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['branch', 'feature'], ['update-ref', 'refs/remotes/origin/main', 'HEAD'], ['tag', 'v1']], ['branch', '--sort=bogus']),
    ('branch-ref:branch_-M___-C___-m_-f_worktree_clobber', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['branch', 'topic'], ('write', 'b.txt', 'yo\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2']], ['branch', '-M', 'topic', 'main']),
    ('branch-ref:branch_name_bad-start', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['branch', 'topic']], ['branch', 'newb', 'nonexistentref']),
    ('branch-ref:tag_--omit-empty', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['tag', 'v1'], ['tag', '-a', '-m', 'annot tag', 'v2'], ('write', 'b.txt', '2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['tag', 'v3']], ['tag', '--omit-empty', '--format=%(refname)']),
    ('branch-ref:tag_--color', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['tag', 'v1'], ['tag', '-a', '-m', 'annot tag', 'v2'], ('write', 'b.txt', '2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['tag', 'v3']], ['tag', '--color']),
    ('branch-ref:tag_--sort', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['tag', 'v1'], ['tag', '-a', '-m', 'annot tag', 'v2'], ('write', 'b.txt', '2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['tag', 'v3']], ['tag', '--sort=bogusfield']),
    ('branch-ref:tag_--cleanup', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['tag', '--cleanup=bogus', '-m', 'x', 'vbogus']),
    ('branch-ref:tag_-a___-l_conflict', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['tag', 'v1'], ['tag', '-a', '-m', 'annot tag', 'v2'], ('write', 'b.txt', '2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['tag', 'v3']], ['tag', '-a', '-l']),
    ('branch-ref:tag_-m___-F_conflict', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['tag', 'v1'], ['tag', '-a', '-m', 'annot tag', 'v2'], ('write', 'b.txt', '2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['tag', 'v3']], ['tag', '-m', 'x', '-F', 'somefile']),
    ('branch-ref:tag_-a_no_message', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['tag', '-a', 'v2']),
    ('branch-ref:tag_--points-at_bare', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['tag', 'v1'], ['tag', '-a', '-m', 'annot tag', 'v2'], ('write', 'b.txt', '2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['tag', 'v3']], ['tag', '--points-at']),
    ('branch-ref:describe_--no-abbrev', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['tag', '-a', '-m', 'release one', 'v1.0'], ('write', 'b.txt', '2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['tag', 'light'], ('write', 'c.txt', '3\n'), ['add', '-A'], ['commit', '-q', '-m', 'c3'], ['tag', '-a', '-m', 'release two', 'v2.0'], ('write', 'd.txt', '4\n'), ['add', '-A'], ['commit', '-q', '-m', 'c4']], ['describe', '--no-abbrev']),
    ('branch-ref:describe_--exact-match', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['tag', '-a', '-m', 'release one', 'v1.0'], ('write', 'b.txt', '2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['tag', 'light'], ('write', 'c.txt', '3\n'), ['add', '-A'], ['commit', '-q', '-m', 'c3'], ['tag', '-a', '-m', 'release two', 'v2.0'], ('write', 'd.txt', '4\n'), ['add', '-A'], ['commit', '-q', '-m', 'c4']], ['describe', '--exact-match']),
    ('branch-ref:describe_--first-parent', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['tag', '-a', '-m', 'release one', 'v1.0'], ('write', 'b.txt', '2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['tag', 'light'], ('write', 'c.txt', '3\n'), ['add', '-A'], ['commit', '-q', '-m', 'c3'], ['tag', '-a', '-m', 'release two', 'v2.0'], ('write', 'd.txt', '4\n'), ['add', '-A'], ['commit', '-q', '-m', 'c4']], ['describe', '--first-parent']),
    ('branch-ref:describe_--match', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['tag', '-a', '-m', 'release one', 'v1.0'], ('write', 'b.txt', '2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['tag', 'light'], ('write', 'c.txt', '3\n'), ['add', '-A'], ['commit', '-q', '-m', 'c3'], ['tag', '-a', '-m', 'release two', 'v2.0'], ('write', 'd.txt', '4\n'), ['add', '-A'], ['commit', '-q', '-m', 'c4']], ['describe', '--match', 'v2*']),
    ('branch-ref:describe_--exclude', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['tag', '-a', '-m', 'release one', 'v1.0'], ('write', 'b.txt', '2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['tag', 'light'], ('write', 'c.txt', '3\n'), ['add', '-A'], ['commit', '-q', '-m', 'c3'], ['tag', '-a', '-m', 'release two', 'v2.0'], ('write', 'd.txt', '4\n'), ['add', '-A'], ['commit', '-q', '-m', 'c4']], ['describe', '--exclude', 'v2*']),
    ('branch-ref:describe_--debug', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['tag', '-a', '-m', 'release one', 'v1.0'], ('write', 'b.txt', '2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['tag', 'light'], ('write', 'c.txt', '3\n'), ['add', '-A'], ['commit', '-q', '-m', 'c3'], ['tag', '-a', '-m', 'release two', 'v2.0'], ('write', 'd.txt', '4\n'), ['add', '-A'], ['commit', '-q', '-m', 'c4']], ['describe', '--debug']),
    ('branch-ref:describe_--broken', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['tag', '-a', '-m', 'release one', 'v1.0'], ('write', 'b.txt', '2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['tag', 'light'], ('write', 'c.txt', '3\n'), ['add', '-A'], ['commit', '-q', '-m', 'c3'], ['tag', '-a', '-m', 'release two', 'v2.0'], ('write', 'd.txt', '4\n'), ['add', '-A'], ['commit', '-q', '-m', 'c4']], ['describe', '--broken']),
    ('branch-ref:describe_multiple_commit-ishes', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['tag', '-a', '-m', 'release one', 'v1.0'], ('write', 'b.txt', '2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['tag', 'light'], ('write', 'c.txt', '3\n'), ['add', '-A'], ['commit', '-q', '-m', 'c3'], ['tag', '-a', '-m', 'release two', 'v2.0'], ('write', 'd.txt', '4\n'), ['add', '-A'], ['commit', '-q', '-m', 'c4']], ['describe', 'HEAD', 'HEAD~1']),
    ('branch-ref:name-rev_--refs', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['tag', 'v1'], ('write', 'b.txt', '2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['tag', '-a', '-m', 'second', 'v2'], ['branch', 'topic'], ('write', 'c.txt', '3\n'), ['add', '-A'], ['commit', '-q', '-m', 'c3']], ['name-rev', '--refs=*v1*', 'HEAD']),
    ('branch-ref:name-rev_--exclude', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['tag', 'v1'], ('write', 'b.txt', '2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['tag', '-a', '-m', 'second', 'v2'], ['branch', 'topic'], ('write', 'c.txt', '3\n'), ['add', '-A'], ['commit', '-q', '-m', 'c3']], ['name-rev', '--exclude', '*v2*', 'HEAD']),
    ('branch-ref:name-rev_--undefined', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['tag', 'v1'], ('write', 'b.txt', '2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['tag', '-a', '-m', 'second', 'v2'], ['branch', 'topic'], ('write', 'c.txt', '3\n'), ['add', '-A'], ['commit', '-q', '-m', 'c3']], ['name-rev', '--undefined', 'HEAD']),
    ('branch-ref:name-rev_--no-undefined', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['tag', 'v1'], ('write', 'b.txt', '2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['tag', '-a', '-m', 'second', 'v2'], ['branch', 'topic'], ('write', 'c.txt', '3\n'), ['add', '-A'], ['commit', '-q', '-m', 'c3']], ['name-rev', '--no-undefined', 'HEAD']),
    ('branch-ref:name-rev_--always', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['tag', 'v1'], ('write', 'b.txt', '2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['tag', '-a', '-m', 'second', 'v2'], ['branch', 'topic'], ('write', 'c.txt', '3\n'), ['add', '-A'], ['commit', '-q', '-m', 'c3']], ['name-rev', '--always', 'HEAD']),
    ('branch-ref:name-rev_--peel-tag', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['tag', 'v1'], ('write', 'b.txt', '2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['tag', '-a', '-m', 'second', 'v2'], ['branch', 'topic'], ('write', 'c.txt', '3\n'), ['add', '-A'], ['commit', '-q', '-m', 'c3']], ['name-rev', '--peel-tag', 'HEAD']),
    ('branch-ref:notes_add_--stripspace', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['notes', 'add', '-m', 'n', '--stripspace', 'HEAD']),
    ('branch-ref:notes_remove_no_note', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['notes', 'remove', 'HEAD']),
    ('branch-ref:notes_copy_--stdin', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['notes', 'copy', '--stdin']),
    ('branch-ref:notes_get-ref_extra_option', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['notes', 'get-ref', '--bad']),
    ('branch-ref:notes_edit_-m_deprecation', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['notes', 'edit', '-m', 'x', 'HEAD']),
    ('branch-ref:notes_--ref_missing_value', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['notes', '--ref']),
    ('branch-ref:notes_prune_extra_arg', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['notes', 'prune', 'extra']),
    ('branch-ref:notes_unknown_subcommand', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['notes', 'bogussubcmd']),
    ('branch-ref:replace_--bogus_usage_spacing', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'b.txt', '2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ('write', 'c.txt', '3\n'), ['add', '-A'], ['commit', '-q', '-m', 'c3']], ['replace', '--bogus']),
    ('branch-ref:for-each-ref_--count_0', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['tag', 'v1'], ['tag', '-a', '-m', 'ann', 'v2'], ['branch', 'topic'], ['update-ref', 'refs/remotes/origin/main', 'HEAD']], ['for-each-ref', '--count=0']),
    ('branch-ref:for-each-ref_--count_-1', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['tag', 'v1'], ['tag', '-a', '-m', 'ann', 'v2'], ['branch', 'topic'], ['update-ref', 'refs/remotes/origin/main', 'HEAD']], ['for-each-ref', '--count=-1']),
    ('branch-ref:for-each-ref_--shell_--perl_two_quoting', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['tag', 'v1'], ['tag', '-a', '-m', 'ann', 'v2'], ['branch', 'topic'], ['update-ref', 'refs/remotes/origin/main', 'HEAD']], ['for-each-ref', '--shell', '--perl']),
    ('branch-ref:for-each-ref_--sort_invalid', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['tag', 'v1'], ['tag', '-a', '-m', 'ann', 'v2'], ['branch', 'topic'], ['update-ref', 'refs/remotes/origin/main', 'HEAD']], ['for-each-ref', '--sort=bogusfield']),
    ('branch-ref:for-each-ref_--ignore-case_pattern', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['tag', 'v1'], ['tag', '-a', '-m', 'ann', 'v2'], ['branch', 'topic'], ['update-ref', 'refs/remotes/origin/main', 'HEAD']], ['for-each-ref', '--ignore-case', 'refs/HEADS/*']),
    ('branch-ref:for-each-ref_--no-sort', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['tag', 'v1'], ['tag', '-a', '-m', 'ann', 'v2'], ['branch', 'topic'], ['update-ref', 'refs/remotes/origin/main', 'HEAD']], ['for-each-ref', '--no-sort']),
    ('branch-ref:show-branch_-a___--all_ordering_&_naming', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['branch', 'topic'], ('write', 'b.txt', '2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['checkout', 'topic'], ('write', 'c.txt', '3\n'), ['add', '-A'], ['commit', '-q', '-m', 't1'], ['checkout', 'main'], ['tag', 'v1'], ['update-ref', 'refs/remotes/origin/main', 'HEAD']], ['show-branch', '-a']),
    ('branch-ref:show-branch_--color_bare_defval', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['branch', 'topic'], ('write', 'b.txt', '2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['checkout', 'topic'], ('write', 'c.txt', '3\n'), ['add', '-A'], ['commit', '-q', '-m', 't1'], ['checkout', 'main'], ['tag', 'v1'], ['update-ref', 'refs/remotes/origin/main', 'HEAD']], ['show-branch', '--color']),
    ('branch-ref:symbolic-ref_--recurse', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['branch', 'topic'], ['symbolic-ref', 'refs/heads/sym', 'refs/heads/main']], ['symbolic-ref', '--recurse', 'HEAD']),
    ('branch-ref:symbolic-ref_--no-recurse', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['branch', 'topic'], ['symbolic-ref', 'refs/heads/sym', 'refs/heads/main']], ['symbolic-ref', '--no-recurse', 'HEAD']),
    ('branch-ref:symbolic-ref_name_bad_target', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['branch', 'topic'], ['symbolic-ref', 'refs/heads/sym', 'refs/heads/main']], ['symbolic-ref', 'HEAD', 'not-a-ref-format']),
    ('branch-ref:symbolic-ref_-m_missing_value', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['branch', 'topic'], ['symbolic-ref', 'refs/heads/sym', 'refs/heads/main']], ['symbolic-ref', '-m']),
    ('branch-ref:update-ref_--create-reflog', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'b.txt', '2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['branch', 'topic'], ['tag', '-a', '-m', 'ann', 'v2']], ['update-ref', '--create-reflog', 'refs/heads/y', 'HEAD']),
    ('branch-ref:update-ref_--stdin', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'b.txt', '2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['branch', 'topic'], ['tag', '-a', '-m', 'ann', 'v2']], ['update-ref', '--stdin']),
    ('branch-ref:update-ref_-z', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'b.txt', '2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['branch', 'topic'], ['tag', '-a', '-m', 'ann', 'v2']], ['update-ref', '-z', '--stdin']),
    ('branch-ref:update-ref_--batch-updates', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'b.txt', '2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['branch', 'topic'], ['tag', '-a', '-m', 'ann', 'v2']], ['update-ref', '--batch-updates', '--stdin']),
    ('branch-ref:update-ref_3-arg_ref_new_old', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'b.txt', '2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['branch', 'topic'], ['tag', '-a', '-m', 'ann', 'v2']], ['update-ref', 'refs/heads/z', 'HEAD', 'WRONGOLD']),
    ('branch-ref:update-ref_-d_ref_old_oldval_ignored', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'b.txt', '2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['branch', 'topic'], ['tag', '-a', '-m', 'ann', 'v2']], ['update-ref', '-d', 'refs/tags/v2', 'WRONGOLD']),
    ('branch-ref:update-ref_-m_missing_value', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'b.txt', '2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['branch', 'topic'], ['tag', '-a', '-m', 'ann', 'v2']], ['update-ref', '-m']),
    ('branch-ref:update-ref_ref_bad-object_crash', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'b.txt', '2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['branch', 'topic'], ['tag', '-a', '-m', 'ann', 'v2']], ['update-ref', 'refs/heads/q', 'nonexistentref']),
    ('branch-ref:pack-refs_positional_rejected', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['tag', 'v1'], ['tag', '-a', '-m', 'ann', 'v2'], ['branch', 'topic'], ['update-ref', 'refs/remotes/origin/main', 'HEAD']], ['pack-refs', 'extra-positional']),
    ('branch-ref:pack-refs_--bogus_usage', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['tag', 'v1'], ['tag', '-a', '-m', 'ann', 'v2'], ['branch', 'topic'], ['update-ref', 'refs/remotes/origin/main', 'HEAD']], ['pack-refs', '--bogus']),
    ('branch-ref:tag_--col_abbrev_ambiguity', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['tag', 'v1']], ['tag', '--col']),
    ('branch-ref:notes_copy_dest_exists', [('write', 'a', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'b', '2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['notes', 'add', '-m', 'seed', 'HEAD']], ['notes', 'copy', 'HEAD~1', 'HEAD']),
    ('branch-ref:symbolic_ref_delete_HEAD', [('write', 'a', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['symbolic-ref', '-d', 'HEAD']),
    ('branch-ref:update_ref_cas_mismatch', [('write', 'a', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'b', '2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['branch', 'feature', 'HEAD~1']], ['update-ref', 'refs/heads/feature', 'HEAD', '']),
    ('objpack:gc_unknown_option', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['gc', '--no-such-flag']),
    ('objpack:gc_h', [], ['gc', '-h']),
    ('objpack:gc_prune_bad_date', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['gc', '--prune=bogusdate']),
    ('objpack:gc_max_cruft_bad', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['gc', '--max-cruft-size=abc']),
    ('objpack:gc_extra_positional', [], ['gc', 'extra-pos']),
    ('objpack:fsck_dangling_default', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'dangle.txt', 'i am dangling\n'), ['hash-object', '-w', 'dangle.txt']], ['fsck']),
    ('objpack:fsck_unreachable', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'dangle.txt', 'i am dangling\n'), ['hash-object', '-w', 'dangle.txt']], ['fsck', '--unreachable']),
    ('objpack:fsck_no_dangling', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'dangle.txt', 'i am dangling\n'), ['hash-object', '-w', 'dangle.txt']], ['fsck', '--no-dangling']),
    ('objpack:fsck_root', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'b.txt', 'world\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2']], ['fsck', '--root']),
    ('objpack:fsck_bogus_flag', [], ['fsck', '--bogus-flag']),
    ('objpack:fsck_h', [], ['fsck', '-h']),
    ('objpack:count_objects_h', [], ['count-objects', '-h']),
    ('objpack:count_objects_bogus', [], ['count-objects', '--bogus']),
    ('objpack:count_objects_extra', [], ['count-objects', 'extra-arg']),
    ('objpack:verify_pack_object_format_sha1_missing', [], ['verify-pack', '--object-format=sha1', 'nonexistent.idx']),
    ('objpack:verify_pack_object_format_bogus', [], ['verify-pack', '--object-format=bogus', 'nonexistent.idx']),
    ('objpack:verify_pack_no_args', [], ['verify-pack']),
    ('objpack:verify_pack_h', [], ['verify-pack', '-h']),
    ('objpack:verify_pack_bogus', [], ['verify-pack', '--bogus']),
    ('objpack:show_index_h', [], ['show-index', '-h']),
    ('objpack:repack_window_bad', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['repack', '--window=abc']),
    ('objpack:repack_depth_bad', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['repack', '--depth=abc']),
    ('objpack:repack_window_memory_bad', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['repack', '--window-memory=abc']),
    ('objpack:repack_extra_positional', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['repack', 'extra-positional']),
    ('objpack:repack_extra_then_d', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['repack', 'extra-positional', '-d']),
    ('objpack:pack_objects_h', [], ['pack-objects', '-h']),
    ('objpack:commit_graph_verify_no_graph', [('write', 'a', 'x\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['commit-graph', 'verify']),
    ('objpack:commit_graph_write_append', [('write', 'a', 'x\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['commit-graph', 'write', '--append']),
    ('objpack:commit_graph_write_max_commits', [('write', 'a', 'x\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['commit-graph', 'write', '--max-commits=1']),
    ('objpack:commit_graph_no_subcommand', [], ['commit-graph']),
    ('objpack:commit_graph_bad_subcommand', [], ['commit-graph', 'bogus-subcmd']),
    ('objpack:commit_graph_write_h', [], ['commit-graph', 'write', '-h']),
    ('objpack:commit_graph_write_bogus', [], ['commit-graph', 'write', '--bogus']),
    ('objpack:commit_graph_verify_progress', [('write', 'f1', 'v1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'f2', 'v2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['commit-graph', 'write', '--reachable']], ['commit-graph', 'verify', '--progress']),
    ('objpack:midx_verify_no_midx', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['repack', '-ad']], ['multi-pack-index', 'verify']),
    ('objpack:midx_compact_bare', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['repack', '-ad']], ['multi-pack-index', 'compact']),
    ('objpack:midx_write_progress', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['repack', '-ad']], ['multi-pack-index', 'write', '--progress']),
    ('objpack:midx_write_preferred_pack_unknown', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['repack', '-ad']], ['multi-pack-index', 'write', '--preferred-pack=x']),
    ('objpack:midx_verify_progress', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['repack', '-ad'], ['multi-pack-index', 'write']], ['multi-pack-index', 'verify', '--progress']),
    ('objpack:midx_no_subcommand', [], ['multi-pack-index']),
    ('objpack:midx_bad_subcommand', [], ['multi-pack-index', 'bogus-subcmd']),
    ('objpack:midx_write_bogus', [], ['multi-pack-index', 'write', '--bogus']),
    ('objpack:prune_h', [], ['prune', '-h']),
    ('objpack:index_pack_h', [], ['index-pack', '-h']),
    ('objpack:index_pack_v_h', [], ['index-pack', '-v', '-h']),
    ('objpack:index_pack_verify_missing', [], ['index-pack', '--verify', 'nonexist.pack']),
    ('apply-index:rp-show-ref-format', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['rev-parse', '--show-ref-format']),
    ('apply-index:rp-git-path-eq', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['rev-parse', '--git-path=hooks']),
    ('apply-index:rp-end-of-options', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['rev-parse', 'HEAD', '--end-of-options', '--foo']),
    ('apply-index:rp-end-of-options-all', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['rev-parse', '--end-of-options', '--all']),
    ('apply-index:rp-exclude-branches', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['tag', 'v1'], ['branch', 'feature'], ('write', 'b.txt', 'yo\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2']], ['rev-parse', '--exclude=feature', '--branches']),
    ('apply-index:rp-exclude-hidden-all', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['tag', 'v1'], ['branch', 'feature']], ['rev-parse', '--exclude-hidden=fetch', '--all']),
    ('apply-index:rp-exclude-hidden-branches-err', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['branch', 'feature']], ['rev-parse', '--exclude-hidden=fetch', '--branches']),
    ('apply-index:rp-exclude-hidden-bad-section', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['rev-parse', '--exclude-hidden=bogus', '--all']),
    ('apply-index:rp-glob', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['branch', 'feature']], ['rev-parse', '--glob=refs/heads/*']),
    ('apply-index:rp-bisect', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['rev-parse', '--bisect']),
    ('apply-index:rp-n-space', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['rev-parse', '-n', '2', 'HEAD']),
    ('apply-index:rp-n-bare', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['rev-parse', '-n']),
    ('apply-index:rp-output-object-format', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['rev-parse', '--output-object-format=sha1', 'HEAD']),
    ('apply-index:rp-output-object-format-bad', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['rev-parse', '--output-object-format=bogus', 'HEAD']),
    ('apply-index:rp-branches-glob', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['branch', 'feature'], ['branch', 'topic']], ['rev-parse', '--symbolic', '--branches=top*']),
    ('apply-index:ui-unmerged', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['update-index', '--unmerged']),
    ('apply-index:ui-test-untracked-cache', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['update-index', '--test-untracked-cache']),
    ('apply-index:ui-index-version-4', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['update-index', '--index-version', '4']),
    ('apply-index:ui-index-version-bad', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['update-index', '--index-version', '99']),
    ('apply-index:ui-index-version-verbose', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['update-index', '--verbose', '--index-version', '4']),
    ('apply-index:ui-untracked-cache-verbose', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['update-index', '--verbose', '--untracked-cache']),
    ('apply-index:ui-fsmonitor', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['update-index', '--fsmonitor']),
    ('apply-index:ui-split-index', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['update-index', '--split-index']),
    ('apply-index:mi-h-stdout', [], ['mailinfo', '-h']),
    ('regr:pack-objects --keep-pack requires value', [('write', 'a', 'a\n'), ['add', 'a'], ['commit', '-q', '-m', 'c1']], ['pack-objects', '--keep-pack']),
    ('regr:pack-objects --filter requires value', [('write', 'a', 'a\n'), ['add', 'a'], ['commit', '-q', '-m', 'c1']], ['pack-objects', '--filter']),
    ('regr:pack-objects --missing requires value', [('write', 'a', 'a\n'), ['add', 'a'], ['commit', '-q', '-m', 'c1']], ['pack-objects', '--missing']),
    ('regr:pack-objects --cruft-expiration bare usage', [('write', 'a', 'a\n'), ['add', 'a'], ['commit', '-q', '-m', 'c1']], ['pack-objects', '--cruft-expiration']),
    ('regr:pack-objects --path-walk bare usage', [('write', 'a', 'a\n'), ['add', 'a'], ['commit', '-q', '-m', 'c1']], ['pack-objects', '--path-walk']),
    ('regr:pack-objects --window requires value (no usage)', [('write', 'a', 'a\n'), ['add', 'a'], ['commit', '-q', '-m', 'c1']], ['pack-objects', '--window']),
    ('regr:restore --unified requires value', [('write', 'a', 'a\n'), ['add', 'a'], ['commit', '-q', '-m', 'c1']], ['restore', '--unified']),
    ('regr:restore --unified without --patch dies', [('write', 'a', 'a\n'), ['add', 'a'], ['commit', '-q', '-m', 'c1']], ['restore', '--unified=3', 'a']),
    ('regr:restore -U without --patch dies', [('write', 'a', 'a\n'), ['add', 'a'], ['commit', '-q', '-m', 'c1']], ['restore', '-U3', 'a']),
    ('regr:restore --inter-hunk-context without --patch dies', [('write', 'a', 'a\n'), ['add', 'a'], ['commit', '-q', '-m', 'c1']], ['restore', '--inter-hunk-context=2', 'a']),
    ('regr:update-ref --deref usage', [('write', 'a', 'a\n'), ['add', 'a'], ['commit', '-q', '-m', 'c1']], ['update-ref', '--deref']),
    ('regr:update-ref --no-deref usage', [('write', 'a', 'a\n'), ['add', 'a'], ['commit', '-q', '-m', 'c1']], ['update-ref', '--no-deref']),
    ('regr:update-ref -z usage', [('write', 'a', 'a\n'), ['add', 'a'], ['commit', '-q', '-m', 'c1']], ['update-ref', '-z']),
]

BATCH18_STDIN_CASES = [
    ('commit-merge:commit-empty-message-text', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'c.txt', 'x\n'), ['add', '-A']], ['commit'], ''),
    ('branch-ref:update_ref_stdin_batch', [('write', 'a', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['branch', 'feature']], ['update-ref', '--stdin'], 'start\nupdate refs/heads/feature HEAD\ncommit\n'),
    ('objpack:show_index_object_format_bad', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['repack', '-ad']], ['show-index', '--object-format=bogus'], ''),
    ('objpack:pack_objects_unknown_option', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['pack-objects', '--bogus', 'pk'], ''),
    ('objpack:pack_objects_window_bad', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['pack-objects', '--window=abc', 'pk'], ''),
    ('objpack:pack_objects_max_pack_size_bad', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['pack-objects', '--max-pack-size=abc', 'pk'], ''),
    ('objpack:pack_objects_index_version_bad', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['pack-objects', '--index-version=abc', 'pk'], ''),
    ('objpack:pack_objects_base_name_cwd', [('write', 'a', 'a\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['pack-objects', 'mypack'], ''),
    ('objpack:pack_objects_dir_prefix_missing', [('write', 'a', 'a\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['pack-objects', 'sub/mypack'], ''),
    ('objpack:midx_write_stdin_packs_empty', [('write', 'a.txt', 'hello\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['repack', '-ad']], ['multi-pack-index', 'write', '--stdin-packs'], ''),
    ('apply-index:apply-ignore-whitespace', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['apply', '--ignore-whitespace', '--check'], 'diff --git a/a.txt b/a.txt\nindex 45b983b..37d4e6c 100644\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-hi\n+hi there\n'),
    ('apply-index:apply-ignore-space-change', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['apply', '--ignore-space-change', '--check'], 'diff --git a/a.txt b/a.txt\nindex 45b983b..37d4e6c 100644\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-hi\n+hi there\n'),
    ('apply-index:apply-inaccurate-eof', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['apply', '--inaccurate-eof', '--check'], 'diff --git a/a.txt b/a.txt\nindex 45b983b..37d4e6c 100644\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-hi\n+hi there\n'),
    ('apply-index:apply-binary-noop', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['apply', '--binary', '--check'], 'diff --git a/a.txt b/a.txt\nindex 45b983b..37d4e6c 100644\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-hi\n+hi there\n'),
    ('apply-index:apply-allow-binary-replacement', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['apply', '--allow-binary-replacement', '--check'], 'diff --git a/a.txt b/a.txt\nindex 45b983b..37d4e6c 100644\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-hi\n+hi there\n'),
    ('apply-index:apply-unsafe-paths', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['apply', '--unsafe-paths', '--check'], 'diff --git a/a.txt b/a.txt\nindex 45b983b..37d4e6c 100644\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-hi\n+hi there\n'),
    ('apply-index:apply-directory', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['apply', '--check', '--directory=sub'], 'diff --git a/a.txt b/a.txt\nindex 45b983b..37d4e6c 100644\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-hi\n+hi there\n'),
    ('apply-index:apply-no-reject', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['apply', '--no-reject', '--check'], 'diff --git a/a.txt b/a.txt\nindex 45b983b..37d4e6c 100644\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-hi\n+hi there\n'),
    ('apply-index:ms-corrupt-stdin', [['mkdir', 'out']], ['mailsplit', '-oout'], 'From: A <a@x>\nSubject: bare\n\nbody\n'),
    ('apply-index:mi-unknown-long', [], ['mailinfo', '--bogus', 'msg', 'patch'], 'From: John Doe <john@example.com>\nSubject: [PATCH] add feature\nDate: Mon, 1 Jan 2020 00:00:00 +0000\nMessage-ID: <abc123@example.com>\n\nThis is the commit body.\n\n---\n diff --git a/a.txt b/a.txt\n'),
    ('apply-index:mi-unknown-short', [], ['mailinfo', '-X', 'msg', 'patch'], 'From: John Doe <john@example.com>\nSubject: [PATCH] add feature\nDate: Mon, 1 Jan 2020 00:00:00 +0000\n\nbody\n'),
    ('apply-index:mi-cluster-kn', [], ['mailinfo', '-kn', 'msg', 'patch'], 'From: John Doe <john@example.com>\nSubject: [PATCH] add feature\nDate: Mon, 1 Jan 2020 00:00:00 +0000\n\nbody\n'),
    ('apply-index:mi-usage-one-positional', [], ['mailinfo', 'msg'], 'From: A <a@x>\nSubject: x\n\nbody\n'),
    ('apply-index:mi-no-quoted-cr-neg', [], ['mailinfo', '--no-quoted-cr=x', 'msg', 'patch'], 'From: A <a@x>\nSubject: x\n\nbody\n'),
    ('apply-index:fmm-log-space', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['checkout', '-q', '-b', 'feature'], ('write', 'b.txt', 'yo\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['checkout', '-q', 'main']], ['fmt-merge-msg', '--log', '5'], "06745ec41343cd591f099c5c80daba1b8bf432b1\t\tbranch 'feature' of ../repo\n"),
    ('apply-index:fmm-summary-space', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['checkout', '-q', '-b', 'feature'], ('write', 'b.txt', 'yo\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['checkout', '-q', 'main']], ['fmt-merge-msg', '--summary', '5'], "06745ec41343cd591f099c5c80daba1b8bf432b1\t\tbranch 'feature' of ../repo\n"),
    ('apply-index:fmm-log-eq', [('write', 'a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['checkout', '-q', '-b', 'feature'], ('write', 'b.txt', 'yo\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['checkout', '-q', 'main']], ['fmt-merge-msg', '--log=5'], "06745ec41343cd591f099c5c80daba1b8bf432b1\t\tbranch 'feature' of ../repo\n"),
]

@pytest.mark.parametrize("case", BATCH18_CASES, ids=[c[0] for c in BATCH18_CASES])
def test_batch18_parity(case, tmp_path: Path, git_254_oracle: str):
    _id, setup, probe = case
    assert_command_parity(git_254_oracle, tmp_path, setup, probe)


@pytest.mark.parametrize("case", BATCH18_STDIN_CASES, ids=[c[0] for c in BATCH18_STDIN_CASES])
def test_batch18_stdin_parity(case, tmp_path: Path, git_254_oracle: str):
    _id, setup, probe, stdin = case
    assert_command_parity(git_254_oracle, tmp_path, setup, probe, stdin=stdin)


# BATCH19: round-3 audit fix-wave Batch 3 (workdir, log-history, diff-family, setup-misc).
BATCH19_CASES = [
    ('workdir:add:--refresh', [('write', 'a.txt', 'hi\n'), ['add', 'a.txt']], ['add', '--refresh', 'a.txt']),
    ('workdir:add:--ignore-errors', [('write', 'a.txt', 'hi\n')], ['add', '--ignore-errors', 'a.txt']),
    ('workdir:add:--ignore-missing', [('write', '.gitignore', '*.log\n')], ['add', '-n', '--ignore-missing', 'x.log']),
    ('workdir:add:--chmod', [('write', 'a.txt', 'hi\n')], ['add', '--chmod=+x', 'a.txt']),
    ('workdir:add:--chmod-bad', [('write', 'a.txt', 'hi\n')], ['add', '--chmod=foo', 'a.txt']),
    ('workdir:add:--renormalize', [('write', 'a.txt', 'hi\n'), ['add', 'a.txt'], ['commit', '-q', '-m', 'c1']], ['add', '--renormalize', 'a.txt']),
    ('workdir:add:--sparse', [('write', 'a.txt', 'hi\n')], ['add', '--sparse', 'a.txt']),
    ('workdir:add:--warn-embedded-repo', [('write', 'a.txt', 'hi\n')], ['add', '--warn-embedded-repo', 'a.txt']),
    ('workdir:add:(no pathspec)', [], ['add']),
    ('workdir:add:embedded-repo', [['mkdir', 'sub'], ['shell', '/tmp/git-2.54.0/bin/git init -q sub'], ('write', 'sub/f.txt', 'x\n')], ['add', 'sub']),
    ('workdir:add:-A/-u exclusive', [('write', 'a.txt', 'hi\n')], ['add', '-A', '-u']),
    ('workdir:add:--int (abbrev)', [('write', 'a.txt', 'hi\n')], ['add', '--int', 'a.txt']),
    ('workdir:stage:--chmod', [('write', 'a.txt', 'hi\n')], ['stage', '--chmod=+x', 'a.txt']),
    ('workdir:stage:--refresh', [('write', 'a.txt', 'hi\n'), ['add', 'a.txt']], ['stage', '--refresh', 'a.txt']),
    ('workdir:stage:(no pathspec)', [], ['stage']),
    ('workdir:rm:--quiet/-q', [('write', 'a.txt', 'one\n'), ['add', 'a.txt'], ['commit', '-q', '-m', 'c1']], ['rm', '--quiet', 'a.txt']),
    ('workdir:rm:--ignore-unmatch', [('write', 'a.txt', 'one\n'), ['add', 'a.txt'], ['commit', '-q', '-m', 'c1']], ['rm', '--ignore-unmatch', 'nope.txt']),
    ('workdir:rm:--sparse', [('write', 'a.txt', 'one\n'), ['add', 'a.txt'], ['commit', '-q', '-m', 'c1']], ['rm', '--sparse', 'a.txt']),
    ('workdir:rm:--pathspec-from-file', [('write', 'a.txt', 'one\n'), ['add', 'a.txt'], ['commit', '-q', '-m', 'c1'], ('write', 'list.txt', 'a.txt\n')], ['rm', '--pathspec-from-file=list.txt']),
    ('workdir:rm:(no pathspec)', [('write', 'a.txt', 'one\n'), ['add', 'a.txt'], ['commit', '-q', '-m', 'c1']], ['rm']),
    ('workdir:rm:<dir> no -r', [('write', 'd/a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['rm', 'd']),
    ('workdir:rm:--cached <dir> no -r', [('write', 'd/a.txt', 'hi\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['rm', '--cached', 'd']),
    ('workdir:mv:--sparse', [('write', 'a.txt', 'one\n'), ['add', 'a.txt'], ['commit', '-q', '-m', 'c1']], ['mv', '--sparse', 'a.txt', 'b.txt']),
    ('workdir:mv:trailing-slash', [('write', 'a.txt', 'one\n'), ['add', 'a.txt'], ['commit', '-q', '-m', 'c1']], ['mv', 'a.txt', 'nodir/']),
    ('workdir:mv:(no args)', [], ['mv']),
    ('workdir:mv:(single arg)', [('write', 'a.txt', 'one\n'), ['add', 'a.txt'], ['commit', '-q', '-m', 'c1']], ['mv', 'a.txt']),
    ('workdir:checkout:--conflict', [('write', 'a.txt', 'one\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['branch', 'feature'], ('write', 'a.txt', 'two\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['tag', 'v1']], ['checkout', '--conflict=bogus', 'feature']),
    ('workdir:checkout:--ours clean', [('write', 'a.txt', 'one\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['checkout', '--ours', 'a.txt']),
    ('workdir:checkout:(no args)', [('write', 'a.txt', 'one\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['branch', 'feature'], ('write', 'a.txt', 'two\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['tag', 'v1']], ['checkout']),
    ('workdir:checkout:-', [('write', 'a.txt', 'one\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['branch', 'feature'], ('write', 'a.txt', 'two\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['tag', 'v1'], ['checkout', 'feature'], ['checkout', 'main']], ['checkout', '-']),
    ('workdir:checkout:-- nonmatch', [('write', 'a.txt', 'one\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['branch', 'feature'], ('write', 'a.txt', 'two\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['tag', 'v1']], ['checkout', '--', 'nope.txt']),
    ('workdir:checkout:<tree> -- nonmatch', [('write', 'a.txt', 'one\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['branch', 'feature'], ('write', 'a.txt', 'two\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['tag', 'v1']], ['checkout', 'feature', '--', 'nope.txt']),
    ('workdir:checkout:<branch> <extra>', [('write', 'a.txt', 'one\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['branch', 'feature'], ('write', 'a.txt', 'two\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['tag', 'v1']], ['checkout', 'feature', 'main']),
    ('workdir:checkout:-b invalid', [('write', 'a.txt', 'one\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['branch', 'feature'], ('write', 'a.txt', 'two\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['tag', 'v1']], ['checkout', '-b', 'bad..name']),
    ('workdir:checkout:--det (abbrev)', [('write', 'a.txt', 'one\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['branch', 'feature']], ['checkout', '--det', 'feature']),
    ('workdir:switch:--guess/--no-guess', [('write', 'a.txt', 'one\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['branch', 'feature'], ('write', 'a.txt', 'two\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['tag', 'v1']], ['switch', '--no-guess', 'feature']),
    ('workdir:switch:--orphan', [('write', 'a.txt', 'one\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['branch', 'feature'], ('write', 'a.txt', 'two\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['tag', 'v1']], ['switch', '--orphan', 'orph']),
    ('workdir:switch:--discard-changes', [('write', 'a.txt', 'one\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['branch', 'feature'], ('write', 'a.txt', 'two\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['tag', 'v1'], ('write', 'a.txt', 'dirty\n')], ['switch', '--discard-changes', 'feature']),
    ('workdir:switch:-m/--merge', [('write', 'a.txt', 'one\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['branch', 'feature'], ('write', 'a.txt', 'two\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['tag', 'v1']], ['switch', '-m', 'feature']),
    ('workdir:switch:--conflict', [('write', 'a.txt', 'one\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['branch', 'feature'], ('write', 'a.txt', 'two\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['tag', 'v1']], ['switch', '--conflict=diff3', 'feature']),
    ('workdir:switch:--conflict-bad', [('write', 'a.txt', 'one\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['branch', 'feature'], ('write', 'a.txt', 'two\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['tag', 'v1']], ['switch', '--conflict=bogus', 'feature']),
    ('workdir:switch:--progress', [('write', 'a.txt', 'one\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['branch', 'feature'], ('write', 'a.txt', 'two\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['tag', 'v1']], ['switch', '--progress', 'feature']),
    ('workdir:switch:--recurse-submodules', [('write', 'a.txt', 'one\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['branch', 'feature'], ('write', 'a.txt', 'two\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['tag', 'v1']], ['switch', '--recurse-submodules', 'feature']),
    ('workdir:switch:-f/--force', [('write', 'a.txt', 'one\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['branch', 'feature'], ('write', 'a.txt', 'two\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['tag', 'v1'], ('write', 'a.txt', 'dirty\n')], ['switch', '-f', 'feature']),
    ('workdir:switch:-c invalid', [('write', 'a.txt', 'one\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['branch', 'feature'], ('write', 'a.txt', 'two\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['tag', 'v1']], ['switch', '-c', 'bad..name']),
    ('workdir:switch:--cr (abbrev)', [('write', 'a.txt', 'one\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['branch', 'feature']], ['switch', '--cr', 'newb']),
    ('workdir:switch:--det (abbrev)', [('write', 'a.txt', 'one\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['branch', 'feature']], ['switch', '--det', 'feature']),
    ('workdir:worktree:add implicit branch', [('write', 'a.txt', 'one\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['worktree', 'add', '../wt1']),
    ('workdir:worktree:add flags', [('write', 'a.txt', 'one\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['worktree', 'add', '-b', 'nb', '../wt1']),
    ('workdir:worktree:subcommands', [('write', 'a.txt', 'one\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['worktree', 'add', '../wt1']], ['worktree', 'prune']),
    ('workdir:worktree:remove -f', [('write', 'a.txt', 'one\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['worktree', 'add', '../wt1']], ['worktree', 'remove', '-f', '../wt1']),
    ('workdir:worktree:remove nonexistent', [('write', 'a.txt', 'one\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['worktree', 'remove', '../nope']),
    ('workdir:worktree:add dup', [('write', 'a.txt', 'one\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['worktree', 'add', '../wt1']], ['worktree', 'add', '../wt1']),
    ('workdir:worktree:no subcommand', [('write', 'a.txt', 'one\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['worktree']),
    ('workdir:sparse-checkout:init flags', [('write', 'a.txt', 'one\n'), ('write', 'dir/b.txt', 'two\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['sparse-checkout', 'init', '--cone']),
    ('workdir:sparse-checkout:set no patterns', [('write', 'a.txt', 'one\n'), ('write', 'dir/b.txt', 'two\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['sparse-checkout', 'init', '--cone']], ['sparse-checkout', 'set']),
    ('workdir:sparse-checkout:subcommands', [('write', 'a.txt', 'one\n'), ('write', 'dir/b.txt', 'two\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['sparse-checkout', 'init', '--cone'], ['sparse-checkout', 'set', 'dir']], ['sparse-checkout', 'add', 'other']),
    ('workdir:sparse-checkout:no subcommand', [('write', 'a.txt', 'one\n'), ('write', 'dir/b.txt', 'two\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['sparse-checkout']),
    ('workdir:sparse-checkout:bad subcommand', [('write', 'a.txt', 'one\n'), ('write', 'dir/b.txt', 'two\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['sparse-checkout', 'bogus']),
    ('workdir:stash:store non-stash-like', [('write', 'a.txt', 'one\n'), ['add', 'a.txt'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'two\n')], ['stash', 'store', '-m', 'x', 'HEAD']),
    ('workdir:stash:apply out-of-range', [('write', 'a.txt', 'one\n'), ['add', 'a.txt'], ['commit', '-q', '-m', 'c1']], ['stash', 'apply', 'stash@{5}']),
    ('workdir:stash:unexpected token', [('write', 'a.txt', 'one\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['stash', 'frobnicate']),
    ('workdir:stash:--inc (abbrev)', [('write', 'a.txt', 'one\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'two\n'), ('write', 'u.txt', 'u\n')], ['stash', '--inc']),
    ('workdir:stash:--mess (abbrev)', [('write', 'a.txt', 'one\n'), ['add', 'a.txt'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'two\n')], ['stash', 'push', '--mess', 'm']),
    ('log-history:log-0', [('write', 'a.txt', 'l1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['tag', 'v1']], ['log', '--decorate=auto', '--oneline']),
    ('log-history:log-1', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['checkout', '-q', '-b', 'feat'], ('write', 'b.txt', 'b\n'), ['add', '-A'], ['commit', '-q', '-m', 'feat-commit'], ['checkout', '-q', 'main'], ('write', 'a.txt', '2\n'), ['add', '-A'], ['commit', '-q', '-m', 'main-commit']], ['log', '--oneline', 'main', 'feat']),
    ('log-history:log-2', [('write', 'a.txt', 'l1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'l2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['tag', 'v1']], ['log', '--oneline', '--no-walk', 'HEAD', 'HEAD~1']),
    ('log-history:log-3', [('write', 'a.txt', 'l1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['checkout', '-q', '-b', 'feature'], ('write', 'c.txt', 'f\n'), ['add', '-A'], ['commit', '-q', '-m', 'feat'], ['checkout', '-q', 'main'], ('write', 'a.txt', 'l2\n'), ['add', '-A'], ['commit', '-q', '-m', 'main2']], ['log', '--left-right', '--oneline', 'feature...main']),
    ('log-history:log-4', [('write', 'a.txt', 'l1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['checkout', '-q', '-b', 'feature'], ('write', 'c.txt', 'f\n'), ['add', '-A'], ['commit', '-q', '-m', 'feat'], ['checkout', '-q', 'main'], ('write', 'a.txt', 'l2\n'), ['add', '-A'], ['commit', '-q', '-m', 'main2']], ['log', '--oneline', 'main', '^feature']),
    ('log-history:log-5', [('write', 'a.txt', 'l1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['checkout', '-q', '-b', 'feature'], ('write', 'c.txt', 'f\n'), ['add', '-A'], ['commit', '-q', '-m', 'feat'], ['checkout', '-q', 'main'], ('write', 'a.txt', 'l2\n'), ['add', '-A'], ['commit', '-q', '-m', 'main2']], ['log', '--oneline', 'main', '--not', 'feature']),
    ('log-history:log-6', [('write', 'a.txt', 'l1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['log', '--merge', '--oneline']),
    ('log-history:log-7', [('write', 'a.txt', 'l1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['log', '--author-date-order', '--oneline']),
    ('log-history:log-8', [('write', 'a.txt', 'l1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'l2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2']], ['log', '--children', '--oneline']),
    ('log-history:log-9', [('write', 'a.txt', 'l1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['log', '--source', '--oneline']),
    ('log-history:log-10', [('write', 'a.txt', 'l1\n'), ['add', '-A'], ['commit', '-q', '-m', 'alpha'], ('write', 'a.txt', 'l2\n'), ['add', '-A'], ['commit', '-q', '-m', 'beta']], ['log', '--grep=beta', '--invert-grep', '--oneline']),
    ('log-history:log-11', [('write', 'a.txt', 'l1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'l2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ('write', 'a.txt', 'l3\n'), ['add', '-A'], ['commit', '-q', '-m', 'c3']], ['log', '--skip=2', '--oneline']),
    ('log-history:log-12', [('write', 'a.txt', 'l1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'l2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2']], ['log', '-z', '--format=%H']),
    ('log-history:log-13', [('write', 'a.txt', 'l1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['log', '--relative-date', '-n', '1']),
    ('log-history:log-14', [('write', 'a.txt', 'l1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['log', '--min-age=1', '--oneline']),
    ('log-history:log-15', [('write', 'a.txt', 'l1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'l2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2']], ['log', '--diff-merges=on', '--oneline', '-n', '1']),
    ('log-history:log-16', [('write', 'a.txt', 'l1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'l2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2']], ['log', '-M', '--oneline', '-n', '1']),
    ('log-history:log-17', [('write', 'a.txt', 'l1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'l1X\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2']], ['log', '-S', 'X', '--pickaxe-regex', '--oneline']),
    ('log-history:log-18', [('write', 'a.txt', 'l1\nl2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'l1\nX\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2']], ['log', '--patience', '-p', '-n', '1']),
    ('log-history:log-19', [('write', 'a.txt', 'l1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'l2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2']], ['log', '--full-history', '--oneline']),
    ('log-history:show-20', [('write', 'a.txt', 'l1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'l2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ('write', 'a.txt', 'l3\n'), ['add', '-A'], ['commit', '-q', '-m', 'c3']], ['show', '--oneline', '-s', 'HEAD~2..HEAD']),
    ('log-history:show-21', [('write', 'a.txt', 'l1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['show', '--abbrev-commit', '-s', '--oneline']),
    ('log-history:show-22', [('write', 'a.txt', 'l1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['show', '--no-textconv']),
    ('log-history:show-23', [('write', 'a.txt', 'l1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['show', '--expand-tabs']),
    ('log-history:show-24', [('write', 'a.txt', 'l1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['show', '--no-notes']),
    ('log-history:show-25', [('write', 'a.txt', 'l1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['show', '--encoding=UTF-8', '-s']),
    ('log-history:show-26', [('write', 'a.txt', 'l1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['show', '-m', 'HEAD']),
    ('log-history:rev-list-27', [('write', 'a.txt', 'l1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'l2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['tag', 'v1']], ['rev-list', '--count', '--objects', 'HEAD']),
    ('log-history:rev-list-28', [('write', 'a.txt', 'l1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'l2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ('write', 'a.txt', 'l3\n'), ['add', '-A'], ['commit', '-q', '-m', 'c3']], ['rev-list', '--skip=1', 'HEAD']),
    ('log-history:rev-list-29', [('write', 'a.txt', 'l1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['checkout', '-q', '-b', 'feature'], ('write', 'c.txt', 'f\n'), ['add', '-A'], ['commit', '-q', '-m', 'feat'], ['checkout', '-q', 'main'], ('write', 'a.txt', 'l2\n'), ['add', '-A'], ['commit', '-q', '-m', 'main2']], ['rev-list', '--cherry-mark', 'feature...main']),
    ('log-history:rev-list-30', [('write', 'a.txt', 'l1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['rev-list', '--missing=allow-any', 'HEAD']),
    ('log-history:rev-list-31', [('write', 'a.txt', 'l1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'l2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2']], ['rev-list', '--objects', '--filter=blob:none', 'HEAD']),
    ('log-history:rev-list-32', [('write', 'a.txt', 'l1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'l2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2']], ['rev-list', 'HEAD', '--not', 'HEAD~1']),
    ('log-history:shortlog-33', [('write', 'a.txt', 'l1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['shortlog']),
    ('log-history:shortlog-34', [('write', 'a.txt', 'l1\n'), ['add', '-A'], ['commit', '-q', '-m', 'First commit']], ['shortlog', '--group=author', 'HEAD']),
    ('log-history:shortlog-35', [('write', 'a.txt', 'l1\n'), ['add', '-A'], ['commit', '-q', '-m', 'First commit']], ['shortlog', '--no-merges', 'HEAD']),
    ('log-history:reflog-36', [('write', 'a.txt', 'l1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'l2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2']], ['reflog', '--format=%gd']),
    ('log-history:reflog-37', [('write', 'a.txt', 'l1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'l2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2']], ['reflog', 'show', '--all']),
    ('log-history:reflog-38', [('write', 'a.txt', 'l1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['checkout', '-q', '-b', 'feature'], ('write', 'a.txt', 'l2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['checkout', '-q', 'main']], ['reflog', '--relative-date']),
    ('log-history:reflog-39', [('write', 'a.txt', 'l1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['checkout', '-q', '-b', 'x'], ['checkout', '-q', 'main']], ['reflog', '--grep-reflog=checkout']),
    ('log-history:cherry-40', [('write', 'a.txt', 'base\n'), ['add', '-A'], ['commit', '-q', '-m', 'base'], ['checkout', '-q', '-b', 'topic'], ('write', 'b.txt', 'B\n'), ['add', '-A'], ['commit', '-q', '-m', 'commit B'], ('write', 'c.txt', 'C\n'), ['add', '-A'], ['commit', '-q', '-m', 'commit C'], ('write', 'd.txt', 'D\n'), ['add', '-A'], ['commit', '-q', '-m', 'commit D'], ['checkout', '-q', 'main']], ['cherry', '-v', 'main', 'topic']),
    ('log-history:range-diff-41', [('write', 'a.txt', 'l1\nl2\nl3\n'), ['add', '-A'], ['commit', '-q', '-m', 'base'], ['checkout', '-q', '-b', 'v1'], ('write', 'a.txt', 'l1\nX\nl3\n'), ['add', '-A'], ['commit', '-q', '-m', 'change line2'], ('write', 'b.txt', 'new\n'), ['add', '-A'], ['commit', '-q', '-m', 'add b'], ['checkout', '-q', 'main'], ['checkout', '-q', '-b', 'v2'], ('write', 'a.txt', 'l1\nY\nl3\n'), ['add', '-A'], ['commit', '-q', '-m', 'change line2'], ('write', 'b.txt', 'new2\n'), ['add', '-A'], ['commit', '-q', '-m', 'add b']], ['range-diff', 'main..v1', 'main..v2']),
    ('log-history:range-diff-42', [('write', 'a.txt', 'l1\n'), ['add', '-A'], ['commit', '-q', '-m', 'base'], ['checkout', '-q', '-b', 'v1'], ('write', 'a.txt', 'X\n'), ['add', '-A'], ['commit', '-q', '-m', 'c'], ['checkout', '-q', 'main'], ['checkout', '-q', '-b', 'v2'], ('write', 'a.txt', 'Y\n'), ['add', '-A'], ['commit', '-q', '-m', 'c']], ['range-diff', 'main', 'v1', 'v2']),
    ('log-history:range-diff-43', [('write', 'a.txt', 'l1\n'), ['add', '-A'], ['commit', '-q', '-m', 'base'], ['checkout', '-q', '-b', 'v1'], ('write', 'a.txt', 'X\n'), ['add', '-A'], ['commit', '-q', '-m', 'c'], ['checkout', '-q', 'main'], ['checkout', '-q', '-b', 'v2'], ('write', 'a.txt', 'Y\n'), ['add', '-A'], ['commit', '-q', '-m', 'c']], ['range-diff', '--left-only', 'main..v1', 'main..v2']),
    ('log-history:bisect-44', [('write', 'a.txt', 'line 1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'line 2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ('write', 'a.txt', 'line 3\n'), ['add', '-A'], ['commit', '-q', '-m', 'c3'], ('write', 'a.txt', 'line 4\n'), ['add', '-A'], ['commit', '-q', '-m', 'c4'], ('write', 'a.txt', 'line 5\n'), ['add', '-A'], ['commit', '-q', '-m', 'c5'], ('write', 'a.txt', 'line 6\n'), ['add', '-A'], ['commit', '-q', '-m', 'c6'], ('write', 'a.txt', 'line 7\n'), ['add', '-A'], ['commit', '-q', '-m', 'c7'], ['bisect', 'start'], ['bisect', 'bad']], ['bisect', 'good', 'HEAD~6']),
    ('log-history:bisect-45', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', '2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2']], ['bisect', 'start']),
    ('log-history:bisect-46', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', '2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ('write', 'a.txt', '3\n'), ['add', '-A'], ['commit', '-q', '-m', 'c3'], ['bisect', 'start', 'HEAD', 'HEAD~2']], ['bisect', 'log']),
    ('log-history:bisect-47', [('write', 'a.txt', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', '2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ('write', 'a.txt', '3\n'), ['add', '-A'], ['commit', '-q', '-m', 'c3'], ['bisect', 'start', 'HEAD', 'HEAD~2']], ['bisect', 'reset']),
    ('diff-family:diff_s_no_patch', [('write', 'a.txt', 'x\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'y\n')], ['diff', '-s']),
    ('diff-family:diff_no_patch_long', [('write', 'a.txt', 'x\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'y\n')], ['diff', '--no-patch']),
    ('diff-family:diff_line_prefix', [('write', 'a.txt', 'x\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'y\n')], ['diff', '--line-prefix=PFX']),
    ('diff-family:diff_stat_count', [('write', 'a.txt', 'x\n'), ('write', 'b.txt', 'z\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'y\n'), ('write', 'b.txt', 'w\n')], ['diff', '--stat-count=1']),
    ('diff-family:diff_ignore_matching_lines', [('write', 'a.txt', 'line1\nline2\n'), ('write', 'b.txt', 'z\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'line1 X\nline2\n'), ('write', 'b.txt', 'w\n')], ['diff', '-I', 'line1']),
    ('diff-family:diff_dirstat_short_X', [['mkdir', 's'], ('write', 's/a.txt', 'x\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 's/a.txt', 'x\ny\n')], ['diff', '-Xfiles']),
    ('diff-family:diff_X_space_ambiguous', [['mkdir', 's'], ('write', 's/a.txt', 'x\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 's/a.txt', 'x\ny\n')], ['diff', '-X', 'files']),
    ('diff-family:diff_rotate_to', [('write', 'a.txt', 'x\n'), ('write', 'b.txt', 'z\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'y\n'), ('write', 'b.txt', 'w\n')], ['diff', '--rotate-to=b.txt']),
    ('diff-family:diff_skip_to', [('write', 'a.txt', 'x\n'), ('write', 'b.txt', 'z\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'y\n'), ('write', 'b.txt', 'w\n')], ['diff', '--skip-to=b.txt']),
    ('diff-family:diff_find_object', [('write', 'a.txt', 'x\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'y\n')], ['diff', '--find-object=HEAD']),
    ('diff-family:diff_orderfile_missing', [('write', 'a.txt', 'x\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'y\n')], ['diff', '-Onosuchfile']),
    ('diff-family:diff_max_depth', [('write', 'a.txt', 'x\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'y\n')], ['diff', '--max-depth=1']),
    ('diff-family:diff_ambiguous_arg', [('write', 'a.txt', 'x\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'y\n')], ['diff', 'nosuchpath']),
    ('diff-family:diff_no_such_path', [('write', 'a.txt', 'x\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'y\n')], ['diff', 'a.txt', 'b.txt']),
    ('diff-family:diff_color_always', [('write', 'a.txt', 'foo\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'bar\n')], ['diff', '--color=always']),
    ('diff-family:diff_color_config', [('write', 'a.txt', 'foo\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'bar\n')], ['-c', 'color.diff=always', 'diff']),
    ('diff-family:diff_color_funcname', [('write', 'f.py', 'def foo():\n    a = 1\n    b = 2\n    c = 3\n    return a\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'f.py', 'def foo():\n    a = 1\n    b = 99\n    c = 3\n    return a\n')], ['diff', '--color=always', 'f.py']),
    ('diff-family:diff_ws_error_highlight_all', [('write', 'a.txt', 'ok\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'ok\nbad  \n')], ['diff', '--color=always', '--ws-error-highlight=all']),
    ('diff-family:diff_color_words', [('write', 'a.txt', 'foo bar\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'foo baz\n')], ['diff', '--color-words', 'a.txt']),
    ('diff-family:diff_word_diff_color', [('write', 'a.txt', 'foo bar\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'foo baz\n')], ['diff', '--word-diff=color', 'a.txt']),
    ('diff-family:diff_color_moved', [('write', 'a.txt', 'aaa\nbbb\nccc\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'ccc\naaa\nbbb\n')], ['diff', '--color=always', '--color-moved']),
    ('diff-family:diff_files_summary', [('write', 'a.txt', 'x\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['chmod', 'a.txt', '755']], ['diff-files', '--summary']),
    ('diff-family:diff_tree_p_implicit_recursion', [['mkdir', 'd'], ('write', 'd/x.txt', 'X\n'), ('write', 'top.txt', 't\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'd/x.txt', 'X\nY\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2']], ['diff-tree', '-p', 'HEAD^', 'HEAD']),
    ('diff-family:diff_tree_stat', [['mkdir', 'd'], ('write', 'd/x.txt', 'X\n'), ('write', 'top.txt', 't\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'd/x.txt', 'X\nY\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2']], ['diff-tree', '--stat', 'HEAD^', 'HEAD']),
    ('diff-family:diff_tree_raw_no_recurse', [['mkdir', 'd'], ('write', 'd/x.txt', 'X\n'), ('write', 'top.txt', 't\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'd/x.txt', 'X\nY\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2']], ['diff-tree', '--raw', 'HEAD^', 'HEAD']),
    ('diff-family:diff_tree_s_silent', [('write', 'a.txt', 'one\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'one\ntwo\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2']], ['diff-tree', '-s', 'HEAD^', 'HEAD']),
    ('diff-family:diff_tree_pretty_oneline', [('write', 'a.txt', 'one\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'new.txt', 'new\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2']], ['diff-tree', '--pretty=oneline', 'HEAD']),
    ('diff-family:diff_tree_abbrev', [('write', 'a.txt', 'one\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'one\ntwo\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2']], ['diff-tree', '--abbrev', 'HEAD^', 'HEAD']),
    ('diff-family:diff_tree_merge_base', [('write', 'a.txt', 'base\n'), ['add', '-A'], ['commit', '-q', '-m', 'base'], ['checkout', '-q', '-b', 'side'], ('write', 'a.txt', 'side\n'), ['add', '-A'], ['commit', '-q', '-m', 's'], ['checkout', '-q', 'main'], ('write', 'a.txt', 'main\n'), ['add', '-A'], ['commit', '-q', '-m', 'm']], ['diff-tree', '--merge-base', '-r', 'main', 'side']),
    ('diff-family:diff_tree_combined_c', [('write', 'a.txt', 'base\n'), ['add', '-A'], ['commit', '-q', '-m', 'base'], ['checkout', '-q', '-b', 'side'], ('write', 'a.txt', 'base\nside\n'), ['add', '-A'], ['commit', '-q', '-m', 's'], ['checkout', '-q', 'main'], ('write', 'a.txt', 'main\nbase\n'), ['add', '-A'], ['commit', '-q', '-m', 'm'], ['merge', '-q', '--no-edit', '-m', 'merge', 'side']], ['diff-tree', '-c', 'HEAD']),
    ('diff-family:diff_tree_combined_cc', [('write', 'a.txt', 'base\n'), ['add', '-A'], ['commit', '-q', '-m', 'base'], ['checkout', '-q', '-b', 'side'], ('write', 'a.txt', 'base\nside\n'), ['add', '-A'], ['commit', '-q', '-m', 's'], ['checkout', '-q', 'main'], ('write', 'a.txt', 'main\nbase\n'), ['add', '-A'], ['commit', '-q', '-m', 'm'], ['merge', '-q', '--no-edit', '-m', 'merge', 'side']], ['diff-tree', '--cc', 'HEAD']),
    ('diff-family:diff_tree_combined_all_paths_no_c', [('write', 'a.txt', 'base\n'), ['add', '-A'], ['commit', '-q', '-m', 'base'], ['checkout', '-q', '-b', 'side'], ('write', 'a.txt', 'base\nside\n'), ['add', '-A'], ['commit', '-q', '-m', 's'], ['checkout', '-q', 'main'], ('write', 'a.txt', 'main\nbase\n'), ['add', '-A'], ['commit', '-q', '-m', 'm'], ['merge', '-q', '--no-edit', '-m', 'merge', 'side']], ['diff-tree', '--combined-all-paths', 'HEAD']),
    ('diff-family:diff_tree_root_suppressed', [('write', 'a.txt', 'x\n'), ['add', '-A'], ['commit', '-q', '-m', 'first']], ['diff-tree', 'HEAD']),
    ('diff-family:diff_tree_no_arg_usage', [('write', 'a.txt', 'x\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['diff-tree']),
    ('diff-family:diff_index_p', [('write', 'a.txt', 'one\n'), ('write', 'b.txt', 'B\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'one\ntwo\n'), ('write', 'b.txt', 'B\nC\n'), ['add', '-A']], ['diff-index', '-p', 'HEAD']),
    ('diff-family:diff_index_stat', [('write', 'a.txt', 'one\n'), ('write', 'b.txt', 'B\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'one\ntwo\n'), ('write', 'b.txt', 'B\nC\n'), ['add', '-A']], ['diff-index', '--stat', 'HEAD']),
    ('diff-family:diff_index_name_status', [('write', 'a.txt', 'one\n'), ('write', 'b.txt', 'B\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'one\ntwo\n'), ('write', 'b.txt', 'B\nC\n'), ['add', '-A']], ['diff-index', '--name-status', 'HEAD']),
    ('diff-family:diff_index_z', [('write', 'a.txt', 'one\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'one\ntwo\n'), ['add', '-A']], ['diff-index', '-z', 'HEAD']),
    ('diff-family:diff_index_worktree_p', [('write', 'a.txt', 'one\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ('write', 'a.txt', 'WT\n')], ['diff-index', '-p', 'HEAD']),
    ('diff-family:diff_index_merge_base', [('write', 'a.txt', 'base\n'), ['add', '-A'], ['commit', '-q', '-m', 'base'], ['checkout', '-q', '-b', 'side'], ('write', 'a.txt', 'side\n'), ['add', '-A'], ['commit', '-q', '-m', 's'], ['checkout', '-q', 'main'], ('write', 'a.txt', 'main\n'), ['add', '-A'], ['commit', '-q', '-m', 'm']], ['diff-index', '--merge-base', 'side']),
    ('diff-family:diff_index_no_arg_usage', [('write', 'a.txt', 'x\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['diff-index']),
    ('diff-family:difftool_no_prompt_empty_cached', [('write', 'a.txt', 'one\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['difftool', '--no-prompt', '--cached']),
    ('diff-family:format_patch_subject_prefix', [('write', 'a.txt', 'one\n'), ['add', '-A'], ['commit', '-q', '-m', 'First commit subject\n\nBody line one.\nBody line two.'], ('write', 'a.txt', 'one\ntwo\n'), ['add', '-A'], ['commit', '-q', '-m', 'Second commit'], ('write', 'b.txt', 'new\n'), ['add', '-A'], ['commit', '-q', '-m', 'Third commit adds b']], ['format-patch', '--stdout', '--subject-prefix=P8TCH', '-3']),
    ('diff-family:format_patch_rfc', [('write', 'a.txt', 'one\n'), ['add', '-A'], ['commit', '-q', '-m', 'First'], ('write', 'b.txt', 'new\n'), ['add', '-A'], ['commit', '-q', '-m', 'Third commit adds b']], ['format-patch', '--stdout', '--rfc', '-1']),
    ('diff-family:format_patch_keep_subject', [('write', 'a.txt', 'one\n'), ['add', '-A'], ['commit', '-q', '-m', 'First'], ('write', 'b.txt', 'new\n'), ['add', '-A'], ['commit', '-q', '-m', 'Third commit adds b']], ['format-patch', '--stdout', '-k', '-1']),
    ('diff-family:format_patch_reroll', [('write', 'a.txt', 'one\n'), ['add', '-A'], ['commit', '-q', '-m', 'First'], ('write', 'b.txt', 'new\n'), ['add', '-A'], ['commit', '-q', '-m', 'Third commit adds b']], ['format-patch', '--stdout', '-v2', '-1']),
    ('diff-family:format_patch_zero_commit', [('write', 'a.txt', 'one\n'), ['add', '-A'], ['commit', '-q', '-m', 'First'], ('write', 'b.txt', 'new\n'), ['add', '-A'], ['commit', '-q', '-m', 'Third commit adds b']], ['format-patch', '--stdout', '--zero-commit', '-1']),
    ('diff-family:format_patch_headers', [('write', 'a.txt', 'one\n'), ['add', '-A'], ['commit', '-q', '-m', 'First'], ('write', 'b.txt', 'new\n'), ['add', '-A'], ['commit', '-q', '-m', 'Third commit adds b']], ['format-patch', '--stdout', '--to=a@b.com', '--cc=c@d.com', '--add-header=X-Foo: bar', '-1']),
    ('diff-family:format_patch_from', [('write', 'a.txt', 'one\n'), ['add', '-A'], ['commit', '-q', '-m', 'First'], ('write', 'b.txt', 'new\n'), ['add', '-A'], ['commit', '-q', '-m', 'Third commit adds b']], ['format-patch', '--stdout', '--from=Me <me@x.com>', '-1']),
    ('diff-family:format_patch_in_reply_to', [('write', 'a.txt', 'one\n'), ['add', '-A'], ['commit', '-q', '-m', 'First'], ('write', 'b.txt', 'new\n'), ['add', '-A'], ['commit', '-q', '-m', 'Third commit adds b']], ['format-patch', '--stdout', '--in-reply-to=<msg@id>', '-1']),
    ('diff-family:format_patch_no_signature', [('write', 'a.txt', 'one\n'), ['add', '-A'], ['commit', '-q', '-m', 'First'], ('write', 'b.txt', 'new\n'), ['add', '-A'], ['commit', '-q', '-m', 'Third commit adds b']], ['format-patch', '--stdout', '--no-signature', '-1']),
    ('diff-family:format_patch_signature', [('write', 'a.txt', 'one\n'), ['add', '-A'], ['commit', '-q', '-m', 'First'], ('write', 'b.txt', 'new\n'), ['add', '-A'], ['commit', '-q', '-m', 'Third commit adds b']], ['format-patch', '--stdout', '--signature=SIG', '-1']),
    ('diff-family:format_patch_stat', [('write', 'a.txt', 'one\n'), ['add', '-A'], ['commit', '-q', '-m', 'First'], ('write', 'b.txt', 'new\n'), ['add', '-A'], ['commit', '-q', '-m', 'Third commit adds b']], ['format-patch', '--stdout', '--stat', '-1']),
    ('diff-family:format_patch_p_only', [('write', 'a.txt', 'one\n'), ['add', '-A'], ['commit', '-q', '-m', 'First'], ('write', 'b.txt', 'new\n'), ['add', '-A'], ['commit', '-q', '-m', 'Third commit adds b']], ['format-patch', '--stdout', '-p', '-1']),
    ('diff-family:format_patch_start_number', [('write', 'a.txt', 'one\n'), ['add', '-A'], ['commit', '-q', '-m', 'First'], ('write', 'a.txt', 'one\ntwo\n'), ['add', '-A'], ['commit', '-q', '-m', 'Second']], ['format-patch', '--stdout', '--start-number=5', '-2']),
    ('diff-family:format_patch_ignore_if_in_upstream_no_range', [('write', 'a.txt', 'one\n'), ['add', '-A'], ['commit', '-q', '-m', 'First'], ('write', 'b.txt', 'new\n'), ['add', '-A'], ['commit', '-q', '-m', 'Third commit adds b']], ['format-patch', '--stdout', '--ignore-if-in-upstream', '-1']),
    ('diff-family:format_patch_filename_max_length', [('write', 'a.txt', 'x\n'), ['add', '-A'], ['commit', '-q', '-m', 'A very long commit subject that will be truncated by max length']], ['format-patch', '--filename-max-length=30', '-1']),
    ('setup-misc:config-get-subcmd', [['config', 'user.name', 'Alice']], ['config', 'get', 'user.name']),
    ('setup-misc:config-set-subcmd', [['config', 'user.name', 'Alice']], ['config', 'set', 'user.name', 'Bob']),
    ('setup-misc:config-list-subcmd', [['config', 'user.name', 'Alice']], ['config', 'list']),
    ('setup-misc:config-unset-subcmd', [['config', 'user.name', 'Alice']], ['config', 'unset', 'user.name']),
    ('setup-misc:config-get-all-subcmd', [['config', '--add', 'a.b', '1'], ['config', '--add', 'a.b', '2']], ['config', 'get', '--all', 'a.b']),
    ('setup-misc:config-get-default-subcmd', [], ['config', 'get', '--default', 'DV', 'no.such']),
    ('setup-misc:config-list-null-subcmd', [['config', 'user.name', 'Alice']], ['config', 'list', '--null']),
    ('setup-misc:config-show-scope', [['config', 'user.name', 'Alice']], ['config', '--list', '--show-scope']),
    ('setup-misc:config-null', [['config', 'user.name', 'Alice']], ['config', '--list', '-z']),
    ('setup-misc:config-bool', [['config', 'core.bare', 'yes']], ['config', '--bool', 'core.bare']),
    ('setup-misc:config-int', [['config', 'core.x', '1k']], ['config', '--int', 'core.x']),
    ('setup-misc:config-type-int-validate', [], ['config', '--type=int', 'core.x', 'notanint']),
    ('setup-misc:config-type-bool-validate', [], ['config', '--type=bool', 'core.bare', 'notabool']),
    ('setup-misc:config-expiry-date', [['config', 'gc.x', 'now']], ['config', '--type=expiry-date', 'gc.x']),
    ('setup-misc:config-color', [['config', 'x.y', 'red']], ['config', '--type=color', 'x.y']),
    ('setup-misc:config-path', [['config', 'core.x', '~/foo']], ['config', '--path', 'core.x']),
    ('setup-misc:config-get-color', [['config', 'color.diff.meta', 'blue']], ['config', '--get-color', 'color.diff.meta']),
    ('setup-misc:config-get-colorbool', [], ['config', '--get-colorbool', 'color.diff', 'false']),
    ('setup-misc:config-get-urlmatch', [['config', 'http.sslVerify', 'true'], ['config', 'http.https://example.com.sslVerify', 'false']], ['config', '--get-urlmatch', 'http', 'https://example.com']),
    ('setup-misc:config-rename-section', [['config', 'old.x', '1']], ['config', '--rename-section', 'old', 'new']),
    ('setup-misc:config-remove-section', [['config', 'sec.x', '1']], ['config', '--remove-section', 'sec']),
    ('setup-misc:config-comment', [], ['config', '--comment', 'hi', 'a.b', 'c']),
    ('setup-misc:config-unknown-opt', [], ['config', '--bogusflag', 'user.name']),
    ('setup-misc:config-abbrev-ge', [['config', 'user.name', 'Alice']], ['config', '--ge', 'user.name']),
    ('setup-misc:config-no-args', [], ['config']),
    ('setup-misc:config-edit', [], ['config', '--edit']),
    ('setup-misc:config-unset-multi-warn', [['config', '--add', 'user.email', 'a@b'], ['config', '--add', 'user.email', 'c@d']], ['config', '--unset', 'user.email']),
    ('setup-misc:config-get-regexp-z', [['config', 'user.name', 'Alice']], ['config', '--get-regexp', '-z', 'user.name']),
    ('setup-misc:merge-file-default', [('write', 'o', 'base\nx\n'), ('write', 'a', 'base\nA\n'), ('write', 'b', 'base\nB\n')], ['merge-file', '-p', 'a', 'o', 'b']),
    ('setup-misc:merge-file-diff3', [('write', 'o', 'base\nx\n'), ('write', 'a', 'base\nA\n'), ('write', 'b', 'base\nB\n')], ['merge-file', '-p', '--diff3', 'a', 'o', 'b']),
    ('setup-misc:merge-file-zdiff3', [('write', 'o', 'base\nx\n'), ('write', 'a', 'base\nA\n'), ('write', 'b', 'base\nB\n')], ['merge-file', '-p', '--zdiff3', 'a', 'o', 'b']),
    ('setup-misc:merge-file-ours', [('write', 'o', 'base\nx\n'), ('write', 'a', 'base\nA\n'), ('write', 'b', 'base\nB\n')], ['merge-file', '-p', '--ours', 'a', 'o', 'b']),
    ('setup-misc:merge-file-theirs', [('write', 'o', 'base\nx\n'), ('write', 'a', 'base\nA\n'), ('write', 'b', 'base\nB\n')], ['merge-file', '-p', '--theirs', 'a', 'o', 'b']),
    ('setup-misc:merge-file-union', [('write', 'o', 'base\nx\n'), ('write', 'a', 'base\nA\n'), ('write', 'b', 'base\nB\n')], ['merge-file', '-p', '--union', 'a', 'o', 'b']),
    ('setup-misc:merge-file-labels', [('write', 'o', 'base\nx\n'), ('write', 'a', 'base\nA\n'), ('write', 'b', 'base\nB\n')], ['merge-file', '-p', '-L', 'mine', '-L', 'orig', '-L', 'yours', 'a', 'o', 'b']),
    ('setup-misc:merge-file-marker-size', [('write', 'o', 'base\nx\n'), ('write', 'a', 'base\nA\n'), ('write', 'b', 'base\nB\n')], ['merge-file', '-p', '--marker-size=10', 'a', 'o', 'b']),
    ('setup-misc:merge-file-quiet', [('write', 'o', 'base\nx\n'), ('write', 'a', 'base\nA\n'), ('write', 'b', 'base\nB\n')], ['merge-file', '-q', '-p', 'a', 'o', 'b']),
    ('setup-misc:merge-file-diff-algo', [('write', 'o', 'base\nx\n'), ('write', 'a', 'base\nA\n'), ('write', 'b', 'base\nB\n')], ['merge-file', '-p', '--diff-algorithm=histogram', 'a', 'o', 'b']),
    ('setup-misc:merge-file-object-id', [('write', 'o', 'base\nx\n'), ('write', 'a', 'base\nA\n'), ('write', 'b', 'base\nB\n')], ['merge-file', '--object-id', '-p', 'a', 'o', 'b']),
    ('setup-misc:merge-file-missing', [('write', 'a', 'x\n')], ['merge-file', '-p', 'a', 'nope', 'b']),
    ('setup-misc:merge-file-diff3-labels', [('write', 'o', 'base\nx\n'), ('write', 'a', 'base\nA\n'), ('write', 'b', 'base\nB\n')], ['merge-file', '-p', '--diff3', '-L', 'mine', '-L', 'orig', '-L', 'yours', 'a', 'o', 'b']),
    ('setup-misc:merge-file-h', [], ['merge-file', '-h']),
    ('setup-misc:merge-index-quiet-false', [('write', 'f.txt', 'base\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['checkout', '-q', '-b', 'feat'], ('write', 'f.txt', 'theirs\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['checkout', '-q', 'main'], ('write', 'f.txt', 'ours\n'), ['add', '-A'], ['commit', '-q', '-m', 'c3'], ['merge', 'feat']], ['merge-index', '-q', '/bin/false', '-a']),
    ('setup-misc:merge-index-unknown-opt', [('write', 'f.txt', 'base\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['checkout', '-q', '-b', 'feat'], ('write', 'f.txt', 'theirs\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['checkout', '-q', 'main'], ('write', 'f.txt', 'ours\n'), ['add', '-A'], ['commit', '-q', '-m', 'c3'], ['merge', 'feat']], ['merge-index', '/bin/true', '-z', 'f.txt']),
    ('setup-misc:merge-index-prog-failed', [('write', 'f.txt', 'base\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['checkout', '-q', '-b', 'feat'], ('write', 'f.txt', 'theirs\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['checkout', '-q', 'main'], ('write', 'f.txt', 'ours\n'), ['add', '-A'], ['commit', '-q', '-m', 'c3'], ['merge', 'feat']], ['merge-index', '/bin/false', '-a']),
    ('setup-misc:merge-index-too-few', [('write', 'f.txt', 'base\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['merge-index', '/bin/true']),
    ('setup-misc:merge-index-not-in-cache', [('write', 'f.txt', 'base\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['checkout', '-q', '-b', 'feat'], ('write', 'f.txt', 'theirs\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['checkout', '-q', 'main'], ('write', 'f.txt', 'ours\n'), ['add', '-A'], ['commit', '-q', '-m', 'c3'], ['merge', 'feat']], ['merge-index', '/bin/true', 'nosuch.txt']),
    ('setup-misc:merge-index-clean-noop', [('write', 'a', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['merge-index', '/bin/true', '-a']),
    ('setup-misc:merge-base-a-short', [('write', 'a', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['checkout', '-q', '-b', 'feat'], ('write', 'b', '2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['checkout', '-q', 'main'], ('write', 'c', '3\n'), ['add', '-A'], ['commit', '-q', '-m', 'c3']], ['merge-base', '-a', 'main', 'feat']),
    ('setup-misc:merge-base-fork-point', [('write', 'a', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['checkout', '-q', '-b', 'feat'], ('write', 'b', '2\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2']], ['merge-base', '--fork-point', 'main', 'feat']),
    ('setup-misc:merge-base-single-arg', [('write', 'a', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['merge-base', 'HEAD']),
    ('setup-misc:merge-base-is-anc-all', [('write', 'a', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['merge-base', '--is-ancestor', '--all', 'HEAD', 'HEAD']),
    ('setup-misc:merge-base-bad-rev', [('write', 'a', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['merge-base', 'HEAD', 'nosuchref']),
    ('setup-misc:merge-base-is-anc-1arg', [('write', 'a', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['merge-base', '--is-ancestor', 'HEAD']),
    ('setup-misc:merge-base-h', [('write', 'a', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['merge-base', '-h']),
    ('setup-misc:maintenance-run-quiet', [('write', 'a', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['maintenance', 'run', '--quiet']),
    ('setup-misc:maintenance-run-task-bad', [], ['maintenance', 'run', '--task=bogus']),
    ('setup-misc:maintenance-run-task-gc', [], ['maintenance', 'run', '--task=gc']),
    ('setup-misc:maintenance-run-schedule-bad', [], ['maintenance', 'run', '--schedule=bogus']),
    ('setup-misc:maintenance-is-needed', [], ['maintenance', 'is-needed']),
    ('setup-misc:maintenance-unregister-force', [], ['maintenance', 'unregister', '--force']),
    ('setup-misc:maintenance-unregister', [], ['maintenance', 'unregister']),
    ('setup-misc:maintenance-register', [], ['maintenance', 'register']),
    ('setup-misc:maintenance-no-sub', [], ['maintenance']),
    ('setup-misc:maintenance-unknown-sub', [], ['maintenance', 'bogus']),
    ('setup-misc:maintenance-h', [], ['maintenance', '-h']),
    ('setup-misc:backfill-sparse', [('write', 'a', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['backfill', '--sparse']),
    ('setup-misc:backfill-rev', [('write', 'a', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['backfill', 'HEAD']),
    ('setup-misc:backfill-bad-rev', [('write', 'a', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['backfill', 'nonsense']),
    ('setup-misc:init-object-format-bad', [], ['init', '--object-format=bogus', 'sub']),
    ('setup-misc:init-b-no-value', [], ['init', '-b']),
    ('setup-misc:init-h', [], ['init', '-h']),
    ('setup-misc:init-unknown', [], ['init', '--bogus']),
    ('setup-misc:init-extra-arg', [], ['init', 'sub', 'extra']),
    ('setup-misc:init-db-h', [], ['init-db', '-h']),
    ('setup-misc:init-db-unknown', [], ['init-db', '--bogus']),
    ('setup-misc:verify-commit-h', [], ['verify-commit', '-h']),
    ('setup-misc:verify-tag-h', [], ['verify-tag', '-h']),
    ('setup-misc:mergetool-no-files', [('write', 'a', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['mergetool']),
    ('setup-misc:mergetool-gui', [('write', 'a', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['mergetool', '-g']),
    ('setup-misc:mergetool-no-prompt', [('write', 'a', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['mergetool', '--no-prompt']),
    ('setup-misc:mergetool-y', [('write', 'a', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['mergetool', '-y']),
    ('setup-misc:mergetool-prompt', [('write', 'a', '1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['mergetool', '--prompt']),
]

BATCH19_STDIN_CASES = [
    ('workdir:rm:--pathspec-file-nul', [('write', 'a.txt', 'one\n'), ['add', 'a.txt'], ['commit', '-q', '-m', 'c1']], ['rm', '--pathspec-from-file=-', '--pathspec-file-nul'], 'a.txt'),
    ('workdir:checkout:--pathspec-from-file empty', [('write', 'a.txt', 'one\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['branch', 'feature'], ('write', 'a.txt', 'two\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ['tag', 'v1']], ['checkout', '--pathspec-from-file=-'], ''),
    ('workdir:checkout:-p', [('write', 'a.txt', 'one\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['branch', 'feature'], ('write', 'a.txt', 'two\n'), ['add', '-A'], ['commit', '-q', '-m', 'c2'], ('write', 'a.txt', 'three\n')], ['checkout', '-p', 'a.txt'], 'q\n'),
    ('workdir:sparse-checkout:set flags', [('write', 'a.txt', 'one\n'), ('write', 'dir/b.txt', 'two\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1'], ['sparse-checkout', 'init', '--cone']], ['sparse-checkout', 'set', '--stdin'], 'dir\n'),
    ('log-history:shortlog-stdin-author', [('write', 'a.txt', 'l1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['shortlog'], 'Author: Foo Bar <f@b.com>\n\n    Some commit\n'),
    ('log-history:shortlog-stdin-committer', [('write', 'a.txt', 'l1\n'), ['add', '-A'], ['commit', '-q', '-m', 'c1']], ['shortlog', '-c'], 'Commit: Foo Bar <f@b.com>\n\n    Some commit\n'),
]

@pytest.mark.parametrize("case", BATCH19_CASES, ids=[c[0] for c in BATCH19_CASES])
def test_batch19_parity(case, tmp_path: Path, git_254_oracle: str):
    _id, setup, probe = case
    assert_command_parity(git_254_oracle, tmp_path, setup, probe)


@pytest.mark.parametrize("case", BATCH19_STDIN_CASES, ids=[c[0] for c in BATCH19_STDIN_CASES])
def test_batch19_stdin_parity(case, tmp_path: Path, git_254_oracle: str):
    _id, setup, probe, stdin = case
    assert_command_parity(git_254_oracle, tmp_path, setup, probe, stdin=stdin)
