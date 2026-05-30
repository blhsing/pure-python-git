# pure-python-git

[![CI](https://github.com/blhsing/pure-python-git/actions/workflows/ci.yml/badge.svg)](https://github.com/blhsing/pure-python-git/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/pure-python-git.svg)](https://pypi.org/project/pure-python-git/)

A pure-Python reimplementation of `git`. No external runtime dependencies — just
the Python standard library. All 141 of git's built-in subcommands are
implemented, plus aliases and pythongit-specific helpers. The on-disk format is
byte-for-byte compatible with real `git`, and the package optionally installs a
drop-in `git` console script.

```text
pure-python-git/             (repo root)
├── pyproject.toml
├── README.md                this file
├── pythongit/               importable package — at repo root
│   ├── __init__.py
│   ├── __main__.py          `python -m pythongit ...`
│   ├── cli.py               command dispatch (161 commands)
│   ├── repo.py              Repository discovery + config
│   ├── objects.py           blob / tree / commit / tag encode/decode
│   ├── refs.py              ref resolution, update, reflog hook
│   ├── reflog.py            append-only ref log
│   ├── index.py             DIRC v2 with conflict stages
│   ├── workdir.py           add/rm/status/checkout, tree↔workdir
│   ├── diff.py              Myers diff + unified-diff renderer
│   ├── merge.py             merge-base + three-way blob merge
│   ├── xdiff.py             xdiff port: histogram/Myers diff + xdl_merge
│   ├── diffcore.py          diffcore-rename: spanhash similarity + matching
│   ├── mergeort.py          merge-ort tree engine (collect/rename/process)
│   ├── ort.py               ort adapter → OrtResult(tree, conflicts, index)
│   ├── sequencer.py         cherry-pick / revert / rebase
│   ├── porcelain_merge.py   ff + 3-way merge entry point
│   ├── patch.py             unified-diff parser + applier
│   ├── pack.py              pack v2 + idx v2, bitmaps, MIDX, streaming writer
│   ├── commitgraph.py       cached commit-graph reader
│   ├── bloom.py             changed-path Bloom filters
│   ├── protocol.py          smart HTTPS clone / fetch / push
│   ├── stash.py             refs/stash + reflog-backed stash
│   ├── ignore.py            .gitignore engine
│   ├── rerere.py            reuse recorded resolution
│   └── bridges.py           daemon / http-backend / SMTP / Tk / shell-out
├── pure-python-git-shim/    companion distribution (opt-in `git` shim)
└── tests/                   pytest + script-style integration tests
```

## Why does this exist?

Sometimes you need `git` on a machine where you can't install a real `git`
binary — locked-down CI workers, restricted containers, environments where the
only thing you can `pip install` is wheels. `pythongit` ships as a single
pure-Python wheel and exposes a `git` command. Most everyday workflows just
work.

This is also a reasonable reference implementation if you want to understand
git's on-disk formats and protocols. The code in this repo cross-references
git's own `Documentation/gitformat-*.adoc` specs for the wire formats it
implements.

## Install

```bash
pip install pure-python-git
```

By default this installs **one console script**: `pygit`. The system `git`
binary on your PATH is **not** shadowed unless you explicitly opt in.

### Opt-in `git` drop-in

The `git` command name is **not** installed by default. You can opt in two ways:

**1. The standard extras syntax — recommended:**

```bash
pip install "pure-python-git[git]"
```

This pulls in the tiny companion package `pure-python-git-shim`, which exists
only to register a `git` console-script. Uninstall it with
`pip uninstall pure-python-git-shim` to remove the `git` command without
touching the rest of pythongit.

**2. After-the-fact, without reinstalling:**

```bash
pygit install-git-shim
```

This copies `pygit` to a sibling `git` (or `git.exe` on Windows) in the same
scripts directory. Reverse with `pygit uninstall-git-shim`. Useful when you
already have pythongit installed and don't want to touch the pip metadata.

Whichever way you choose, whether `git` resolves to pythongit depends on PATH
order — both commands warn if a different `git` is earlier on PATH.

You can also run pythongit from a checkout without installing:

```bash
python -m pythongit <command> [args...]
```

### Why is the `git` name opt-in?

Silently shadowing `git` on every install is a footgun: scripts that shell
out to `git` start invoking pythongit instead the next time you
`pip install pure-python-git` into a venv, without warning. Making it opt-in
turns it into a deliberate choice you make per-environment.

## Tutorial

```bash
mkdir demo && cd demo
pygit init .
pygit config user.name "You"
pygit config user.email "you@example.com"

echo "hello" > a.txt
pygit add a.txt
pygit commit -m "first commit"

echo "world" >> a.txt
pygit diff
pygit add a.txt
pygit commit -m "append world"

pygit log --oneline
pygit tag v1
pygit branch feature
pygit checkout feature
echo "feature work" > f.txt
pygit add f.txt
pygit commit -m "feature commit"

pygit checkout main
pygit merge feature
```

Cloning over HTTPS:

```bash
pygit clone https://github.com/some/repo.git
```

Cloning or converting across object formats:

```bash
pygit clone --object-format=sha256 ./sha1-repo ./sha256-copy
pygit convert-object-format --object-format=sha1 ./sha256-copy ./sha1-copy
```

## Supported commands

All 141 git built-in subcommands plus aliases and pythongit-specific helpers
(161 entries in total). Selected highlights:

**Plumbing.** `hash-object`, `cat-file`, `ls-tree`, `write-tree`, `read-tree`,
`commit-tree`, `mktree`, `mktag`, `update-ref`, `symbolic-ref`, `rev-parse`,
`rev-list`, `ls-files`, `diff-tree`, `diff-index`, `diff-files`, `diff-pairs`,
`pack-objects`, `unpack-objects`, `index-pack`, `verify-pack`, `show-index`,
`unpack-file`, `merge-index`, `merge-file`, `update-index`, `update-server-info`,
`check-ref-format`, `check-attr`, `check-mailmap`, `check-ignore`, `for-each-ref`,
`show-ref`, `pack-refs`, `prune-packed`, `pack-redundant`, `multi-pack-index`,
`fetch-pack`, `send-pack`, `upload-pack`, `receive-pack`, `upload-archive`,
`http-fetch`, `http-backend`, `fmt-merge-msg`, `mailinfo`, `mailsplit`,
`patch-id`, `commit-graph`, `var`, `stripspace`.

**Porcelain.** `init`, `clone`, `add`, `rm`, `mv`, `status`, `commit`, `log`,
`show`, `diff`, `branch`, `tag`, `checkout`, `switch`, `restore`, `reset`,
`merge`, `merge-tree`, `cherry-pick`, `revert`, `rebase`, `replay`, `cherry`,
`range-diff`, `stash`, `reflog`, `notes`, `bisect`, `blame`, `annotate`,
`describe`, `name-rev`, `shortlog`, `whatchanged`, `clean`, `archive`,
`bundle`, `format-patch`, `am`, `apply`, `grep`, `show-branch`, `worktree`,
`submodule`, `sparse-checkout`, `request-pull`, `interpret-trailers`,
`verify-commit`, `verify-tag`, `rerere`, `replace`, `gc`, `repack`, `prune`,
`count-objects`, `fsck`, `pull`, `fetch`, `push`, `remote`, `ls-remote`,
`config`, `refs`, `convert-object-format`, `repo`, `diagnose`, `bugreport`,
`last-modified`, `history`, `url-parse`, `maintenance`.

**Bridges (orchestrate other binaries / protocols).** `send-email` (via
`smtplib`), `daemon` (TCP git:// server), `instaweb`/`gitweb` (`http.server`-based
browser), `gitk`/`gui` (Tk log viewer), `cvsimport`/`cvsexportcommit`/`cvsserver`
(shell out to `cvs`), `svn` (shell out to `svn`), `difftool`/`mergetool`
(invoke configured external tool), `credential`/`credential-store`/
`credential-cache`/`credential-cache-daemon`, `remote-helper`/`remote-ext`/
`remote-fd`, `fsmonitor`/`fsmonitor-daemon`, `shell` (restricted ssh
dispatcher), `init-db`, `submodule-helper`, `checkout-worker`, `backfill`.

To see the full list:

```bash
pygit help
```

## Interop with real git

The on-disk format is byte-for-byte compatible with the git C implementation.
The test suite verifies this against the real `git` binary:

| pythongit writes... | ...real `git` validates |
|---|---|
| loose objects | `git fsck` |
| tree / commit objects | `git cat-file -p` |
| index v2 with stages | `git ls-files --stage` |
| pack v2 + idx v2 (with deltas) | `git verify-pack -v` |
| pack and MIDX bitmap indexes | `git rev-list --test-bitmap` |
| binary commit-graph file with changed-path Bloom filters | `git commit-graph verify` |
| SHA-1/SHA-256 object-format repos | `git fsck`, `git rev-parse --show-object-format` |
| refs / packed-refs / reflog | `git log --all` |
| smart HTTPS push payload | `git receive-pack` |

The reverse also holds: pythongit reads packs and indexes produced by real
`git` clones.

## Architecture

### Object storage

Loose objects under `.git/objects/<oid[:2]>/<oid[2:]>`, zlib-compressed. SHA-1
and SHA-256 repositories are selected by `extensions.objectformat`. Loose-object
enumeration uses a persistent `.git/objects/info/pygit-loose-cache-v1` cache
validated by fanout directory mtimes/sizes, so repeated `count-objects`,
abbreviated-OID resolution, and pruning commands do not rewalk every loose
object directory when nothing changed.

Pack objects live in `.git/objects/pack/pack-*.{pack,idx}`. The pack reader
mmaps pack files, binary-searches `.idx` tables, and handles both `REF_DELTA`
(delta against a hex object-id base) and `OFS_DELTA` (delta against an earlier
offset in the same pack). Pack creation has two paths: `pack.build_pack` is the
small in-memory builder used by tests and helper code, while CLI repacks,
bundles, `pack-objects --stdout`, push requests, and upload-pack responses use
a bounded-memory streaming writer that still emits OFS deltas against recent
same-type bases.

`pack-objects --all` and `repack` write pack `.bitmap` indexes for full
reachable packs. `multi-pack-index write --bitmap` writes `RIDX`/`BTMP` chunks
plus the companion `multi-pack-index-<hash>.bitmap` file. Reachability queries,
`rev-list --count`, pruning, and maintenance paths use pack/MIDX bitmaps when
available. The bitmaps use Git's v1 `BITM` format and EWAH containers; the
first implementation emits literal EWAH words rather than XOR-compressed
chains, prioritizing compatibility and simple verification over minimum file
size.

`translate.ObjectTranslator` converts complete reachable object graphs between
SHA-1 and SHA-256 by rehashing blobs and rewriting embedded object IDs in
trees, commits, and annotated tags. `clone --object-format=...` uses this when
the requested target format differs from the source format.

### Index

DIRC v2 with full stage support (bits 14-13 of the flags field). When a merge
or cherry-pick conflicts, stages 1 (base), 2 (ours), 3 (theirs) are written to
the index while the merged-with-markers blob is left in the worktree.
`pygit commit` refuses to commit while any stage > 0 exists; `pygit add`
clears the conflict stages on resolution. `pygit merge-index -o <tool>` walks
conflicted entries and invokes the driver with `(path, base-tmp, ours-tmp,
theirs-tmp)`.

### Refs & reflog

`refs.update_ref` is the single chokepoint for all ref updates; it
automatically appends to `.git/logs/<ref>` and (when the updated ref is what
HEAD points at symbolically) to `.git/logs/HEAD`. This means `reflog`, `stash`
(via `refs/stash`), and `notes` (via `refs/notes/commits`) all share one
mechanism.

### Merge

`merge.merge_bases` mirrors `commit-reach.c`'s `paint_down_to_common`: BFS
from both tips with PARENT1/PARENT2 flags, marking double-flagged commits as
results and pushing STALE to their ancestors.

High-level three-way merges run a pure-Python port of Git's own `ort` engine —
no `git` binary and no fallback engine. The port lives in three modules and
reproduces `git merge-tree --write-tree` byte-for-byte (result tree oid,
conflicted blobs with markers, and conflicted index stages):

* `xdiff.py` — Git's xdiff library: record classification, the **histogram**
  diff that `ort` hardcodes for content merges (with the classic Myers
  algorithm as its documented fallback), change compaction, and the zealous
  three-way `xdl_merge` that emits `<<<<<<<` / `=======` / `>>>>>>>` markers.
* `diffcore.py` — rename detection: the `diffcore-delta` spanhash similarity
  estimator plus exact, basename-driven, and inexact NxM matrix matching from
  `diffcore-rename.c`.
* `mergeort.py` — the `merge-ort.c` tree engine: the recursive three-way tree
  walk (`collect_merge_info`), file and **directory** rename detection and
  resolution (`process_renames`), per-path resolution (`process_entry`), and
  streamed result-tree assembly with conflicted index stages.

`ort.py` is a thin adapter exposing `merge_tree(repo, merge_base, ours,
theirs)`; the `merge_base`/`ours`/`theirs` arguments double as the
conflict-marker labels, exactly as the corresponding `git merge-tree
--merge-base` arguments do.

### Rerere

When a conflict is produced, the file (with markers) is hashed after
normalization (branch labels stripped) and stored under
`.git/rr-cache/<hash>/preimage` plus a line in `_pending.txt`. When the user
resolves the conflict and runs `commit`, the post-image is recorded next to
it. The next time the *same* logical conflict appears, the merge replays the
post-image automatically.

### Bisect

`bisect_step` follows git's `best_bisection`: for each candidate commit,
compute `min(reachable_from_it, n - reachable_from_it)` and pick the maximum;
that is, the commit that splits the candidate DAG as evenly as possible. Parent
lookups use the commit-graph when present. The scorer mirrors Git's `bisect.c`
shape: single-parent chains inherit parent weights, while merge commits get an
exact distance walk so shared ancestors are counted once.

### Pack writer (delta compression)

`pack._compute_delta` builds a hash table of every 16-byte block in the base,
then sweeps the target looking for matches >= 4 bytes long. Matches become
`COPY` ops; misses are accumulated into `INSERT` ops capped at 127 bytes each.
The encoder is conservative: it accepts a delta only when it's at most 50% of
raw size, keeping the chain length sensible. The streaming writer processes
bounded batches sorted by type/size and keeps only a small recent-base window,
so large pack creation no longer requires all object contents or the final pack
bytes in memory. Incoming fetch/receive packs are streamed to a temporary file,
mmap-indexed from disk, and installed as pack/idx pairs; thin packs are fixed by
appending missing bases before the final index is written.

### Binary commit-graph

Implements the format from `gitformat-commit-graph.adoc`:

```text
HEADER  (8 bytes)   CGPH + ver(1) + hashver(1) + chunk_count + base_count
TOC     ((C+1)*12)  per-chunk (id, offset_uint64) + terminator
OIDF    (256*4)     fanout: cumulative counts indexed by first byte of OID
OIDL    (N*H)       sorted object IDs
CDAT    (N*(H+16))  tree(H) + parent1_pos(4) + parent2_pos(4) + gen+time(8)
EDGE    (optional)  octopus extra parents
BIDX    (N*4)       cumulative byte offsets for changed-path Bloom filters
BDAT    (optional)  Bloom settings + concatenated changed-path filters
TRAILER (H)         repository hash of all preceding bytes
```

Generation numbers count topological level (1 for roots). The on-disk file is
verifiable by real `git commit-graph verify`. `pygit` also reads and caches the
commit-graph for parent/tree lookups during history walks. Changed-path Bloom
filters use Git's default settings: hash version 1, seven hashes, and ten bits
per changed path; parent directories are included so path-limited history can
test both `dir` and `dir/file`. `blame` uses those filters to avoid tree/blob
work for commits that definitely did not touch the requested path.

### Smart HTTPS

`protocol.discover_refs` calls `GET /info/refs?service=git-upload-pack`,
strips the pkt-line framing, and returns the ref map. Fetch/clone stream the
side-band-encoded pack response directly into the pack indexer instead of
building one large response buffer. `protocol.push` does the receive-pack flow
including streaming a non-thin pack of only-new objects from a temporary pack
file and parsing `ok/ng` lines.

The `daemon` command serves the same flow over a raw TCP socket (git:// at
port 9418), implemented with `socketserver.ThreadingTCPServer`. Upload-pack
responses stream side-band pack chunks instead of assembling the full response
body. `http-backend` is an in-process variant used by `instaweb`; the web server
uses the streaming backend for upload-pack responses and receive-pack request
bodies.

## Testing

```bash
pip install pythongit[test]
pytest
```

The suite passes:

| File                    | Coverage |
|-------------------------|----------|
| `unit_objects.py`       | hash, encode/decode, signatures, gitlinks |
| `unit_refs.py`          | symbolic refs, reflog, packed-refs, abbrev SHA |
| `unit_index.py`         | DIRC v2 roundtrip, conflict stages, long paths |
| `unit_pack.py`          | delta apply, idx v2, build_pack, inbound pack indexing, pack/MIDX bitmaps, binary MIDX, SHA-256 interop |
| `unit_modules.py`       | diff/merge/patch/ignore/rerere/SMTP/XOAUTH2/fsmonitor/bisect unit-level |
| `unit_integration.py`   | end-to-end CLI flows incl. ort-backed conflicts, rename-aware merge, rerere replay, SHA-256 translation, loose cache, streaming upload-pack, recursive tree diff |
| `test_ort_parity.py`    | byte-for-byte `ort` parity vs `git merge-tree --write-tree` across every conflict type (content, modify/delete, add/add, rename/rename, rename/delete, directory rename, distinct-types, exec-bit) |
| `unit_phase_scripts.py` | wraps the script-style phase tests |

Tests that require the real `git` binary are silently skipped when it's not on
PATH, so the suite runs cleanly in containers without one.

The pure-Python `ort` engine is additionally cross-checked against C Git with
the differential fuzzers in `tests/diff_xdiff_harness.py` (blob-level 3-way
merges vs `git merge-file`) and `tests/diff_ort_harness.py` (whole-tree merges
vs `git merge-tree`); both compare results byte-for-byte over thousands of
randomized cases.

## What's intentionally NOT implemented

* `git filter-repo` (it's a separate Python tool anyway, not a git built-in).

## Limitations to know about

* Big repos: packed repositories now use mmap-backed pack reads, binary MIDX
  lookup, pack/MIDX bitmaps, commit-graph parent/tree lookup, changed-path
  Bloom filters, cached loose-object enumeration, and bounded-memory streaming
  pack generation/indexing. Tree-diff commands skip identical subtrees. The
  remaining scale-sensitive cases are commands whose output inherently requires
  inspecting every path or blob.
* The `ort` merge engine is a pure-Python reimplementation (no `git` binary,
  no fallback) and is validated for byte-for-byte parity against
  `git merge-tree --write-tree` across content merges, rename detection
  (file and directory), and conflict presentation. It targets a single merge
  base (as `git merge-tree --merge-base` provides); recursive merge of multiple
  merge bases (a virtual ancestor) and full submodule fast-forward resolution
  are not modelled, and `merge.conflictStyle`/whitespace merge drivers default
  to Git's standard behavior.
* `fsmonitor-daemon run` uses native filesystem notifications on Windows and
  Linux (`ReadDirectoryChangesW` / inotify). One-shot `fsmonitor` calls and
  unsupported platforms fall back to configurable polling.
* `send-email` uses `smtplib` with plain SMTP, STARTTLS/TLS, SMTP-over-SSL,
  XOAUTH2 bearer tokens, `~/.git-credentials`, and configured `git credential`
  helpers. Browser-based provider OAuth consent flows are still external.
* `gitk` / `gui` use Tk when available and fall back to a text log in headless
  Python installs.

## Contributing

The project tries to follow git's published wire and on-disk format specs
(`Documentation/gitformat-*.adoc`, `Documentation/technical/*.adoc`). When
adding a feature:

1. Find the matching `builtin/<name>.c` and read its argument parser to figure
   out the flag set people actually use.
2. Implement the behavior, but only the common flags first. Less-common flags
   should `argparse.error` rather than silently misbehave.
3. Add a unit test in `tests/unit_*.py`. If real `git` can verify the output,
   also add an interop check.
4. Run `pytest` — must remain green.

## License

MIT.
