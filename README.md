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
│   ├── sequencer.py         cherry-pick / revert / rebase
│   ├── porcelain_merge.py   ff + 3-way merge entry point
│   ├── patch.py             unified-diff parser + applier
│   ├── pack.py              pack v2 + idx v2, REF_DELTA + OFS_DELTA, encoder
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
| binary commit-graph file | `git commit-graph verify` |
| SHA-1/SHA-256 object-format repos | `git fsck`, `git rev-parse --show-object-format` |
| refs / packed-refs / reflog | `git log --all` |
| smart HTTPS push payload | `git receive-pack` |

The reverse also holds: pythongit reads packs and indexes produced by real
`git` clones.

## Architecture

### Object storage

Loose objects under `.git/objects/<oid[:2]>/<oid[2:]>`, zlib-compressed. SHA-1
and SHA-256 repositories are selected by `extensions.objectformat`. Pack objects
live in `.git/objects/pack/pack-*.{pack,idx}`. The pack reader handles both
`REF_DELTA` (delta against a hex object-id base) and `OFS_DELTA` (delta against
an earlier offset in the same pack). `pack.build_pack` also writes deltas:
candidate bases come from a windowed search over recent same-type objects,
accepted when the delta is at most half the raw size.

`translate.ObjectTranslator` converts complete reachable object graphs between
SHA-1 and SHA-256 by rehashing blobs and rewriting embedded object IDs in
trees, commits, and annotated tags. `clone --object-format=...` uses this when
the requested target format differs from the source format.

### Index

DIRC v2 with full stage support (bits 14-13 of the flags field). When a merge
or cherry-pick conflicts, stages 1 (base), 2 (ours), 3 (theirs) are written to
the index alongside a stage-0 entry pointing at the merged-with-markers blob.
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
results and pushing STALE to their ancestors. `merge.merge_blob` is a
line-based three-way merge that consults the rerere cache before falling back
to emitting conflict markers.

### Rerere

When a conflict is produced, the file (with markers) is hashed after
normalization (branch labels stripped) and stored under
`.git/rr-cache/<hash>/preimage` plus a line in `_pending.txt`. When the user
resolves the conflict and runs `commit`, the post-image is recorded next to
it. The next time the *same* logical conflict appears, the merge replays the
post-image automatically.

### Bisect

`bisect_step` follows git's `best_bisection`: for each candidate commit,
compute `min(reachable_from_it, n - reachable_from_it)` and pick the maximum
— i.e. the commit that splits the candidate DAG as evenly as possible.

### Pack writer (delta compression)

`pack._compute_delta` builds a hash table of every 16-byte block in the base,
then sweeps the target looking for matches >= 4 bytes long. Matches become
`COPY` ops; misses are accumulated into `INSERT` ops capped at 127 bytes each.
The encoder is conservative: it accepts a delta only when it's at most 50% of
raw size, keeping the chain length sensible.

### Binary commit-graph

Implements the format from `gitformat-commit-graph.adoc`:

```text
HEADER  (8 bytes)   CGPH + ver(1) + hashver(1) + chunk_count + base_count
TOC     ((C+1)*12)  per-chunk (id, offset_uint64) + terminator
OIDF    (256*4)     fanout: cumulative counts indexed by first byte of OID
OIDL    (N*H)       sorted object IDs
CDAT    (N*(H+16))  tree(H) + parent1_pos(4) + parent2_pos(4) + gen+time(8)
EDGE    (optional)  octopus extra parents
TRAILER (H)         repository hash of all preceding bytes
```

Generation numbers count topological level (1 for roots). The on-disk file is
verifiable by real `git commit-graph verify`.

### Smart HTTPS

`protocol.discover_refs` calls `GET /info/refs?service=git-upload-pack`,
strips the pkt-line framing, and returns the ref map. `protocol.fetch_pack`
posts `want <sha>` lines + capability list and parses the side-band-encoded
pack response. `protocol.push` does the receive-pack flow including building
a non-thin pack of only-new objects and parsing `ok/ng` lines.

The `daemon` command serves the same flow over a raw TCP socket (git:// at
port 9418), implemented with `socketserver.ThreadingTCPServer`. `http-backend`
is an in-process variant used by `instaweb`.

## Testing

```bash
pip install pythongit[test]
pytest
```

88 tests pass:

| File                    | Coverage |
|-------------------------|----------|
| `unit_objects.py`       | hash, encode/decode, signatures, gitlinks |
| `unit_refs.py`          | symbolic refs, reflog, packed-refs, abbrev SHA |
| `unit_index.py`         | DIRC v2 roundtrip, conflict stages, long paths |
| `unit_pack.py`          | delta apply, idx v2, build_pack, binary MIDX, SHA-256 interop |
| `unit_modules.py`       | diff/merge/patch/ignore/rerere/SMTP unit-level |
| `unit_integration.py`   | end-to-end CLI flows incl. conflicts, rerere replay, SHA-256 translation |
| `unit_phase_scripts.py` | wraps the script-style phase tests |

Tests that require the real `git` binary are silently skipped when it's not on
PATH, so the suite runs cleanly in containers without one.

## What's intentionally NOT implemented

* Bitmap indexes and bloom filters on the commit-graph. Binary
  multi-pack-index files are implemented for pack lookup, but MIDX bitmaps are
  not.
* `git filter-repo` (it's a separate Python tool anyway, not a git built-in).
* The fancier merge strategies (`recursive`'s rename detection, `ort`'s
  three-way for trees). `pygit merge-recursive` aliases to the default
  three-way merge.

## Limitations to know about

* Big repos: loose-object discovery and some reachability walks are still
  straightforward scans. Pack object lookup can use `.idx` files and the
  binary multi-pack-index, but this is still not designed for the
  linux-kernel-or-larger end of the spectrum.
* The `bisect` heuristic computes exact candidate weights with iterative
  bitsets. It avoids Python recursion, but very large candidate DAGs can still
  spend noticeable CPU and memory on transitive reachability.
* `fsmonitor` uses polling, not OS-level inotify/fsevent. Configurable
  interval; not free.
* `send-email` uses `smtplib` with plain SMTP, STARTTLS/TLS, and SMTP-over-SSL.
  It does not include provider-specific OAuth/keychain helpers.
* `gitk` / `gui` need a working Tk install (`tkinter`).

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
