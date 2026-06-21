# pygit flag-parity gap manifest (vs git 2.54.0)

Probed 147 commands; 1479 documented flags scanned; 264 flags across 20 commands are currently *rejected* by pygit.

NOTE: a flag absent here only means pygit's parser accepts it — not that its behaviour is byte-identical. This is a lower bound on remaining parity work.

## add (6)
  -i -p -U --unified --inter-hunk-context -e

## am (26)
  --continue --skip --abort -i -n --no-verify --verify -q -s -u -k -b -m --keep-cr -c --quoted-cr -C -p -r --resolved --quit --show-current-patch --retry --allow-empty -S --empty

## apply (10)
  -p --no-add --add -N --intent-to-add --ours --theirs --union -z -C

## commit (8)
  --interactive --patch -S -t -p -U --unified --inter-hunk-context

## diff-files (10)
  -c --cc -B -M -C --find-copies-harder -l -O -S --pickaxe-all

## diff-pairs (83)
  -z -p --patch -s --no-patch -u -U --unified -W --raw --patch-with-raw --patch-with-stat --stat --numstat --shortstat -X --dirstat --cumulative --dirstat-by-file --check --summary --name-only --name-status --stat-width --stat-name-width --stat-graph-width --stat-count --binary --ws-error-highlight --src-prefix --dst-prefix --line-prefix --no-prefix --default-prefix --inter-hunk-context --output-indicator-new --output-indicator-old --output-indicator-context -B --break-rewrites -M --find-renames -D --irreversible-delete -C --find-copies --no-renames -l --minimal -w --ignore-all-space -b --ignore-space-change --ignore-space-at-eol --ignore-cr-at-eol --ignore-blank-lines -I --patience --histogram --diff-algorithm --anchored --word-diff --word-diff-regex --color-words --color-moved -a -R --ignore-submodules --submodule --ita-invisible-in-index -N --ita-visible-in-index -S -G --pickaxe-all --pickaxe-regex -O --rotate-to --skip-to --find-object --diff-filter --max-depth --output

## difftool (7)
  -g -d -y --no-prompt -x --no-index --index

## fast-export (1)
  --anonymize-map

## fast-import (6)
  --date-format --max-pack-size --big-file-threshold --depth --active-branches --export-marks

## fetch (22)
  --multiple --all -v -q -a -f -m -t -n --no-tags -j -p -P -k -u --unshallow --refetch --refmap -o --ipv4 --ipv6 --auto

## hash-object (1)
  --filters

## log (1)
  -L

## pull (17)
  -v -q -r -n --ff-only -s -X -S -a -f -t -p -j -k --unshallow --refmap -o

## push (14)
  -v -q --all -d --branches --mirror -n -f -u --no-verify --verify -o --ipv4 --ipv6

## repack (25)
  -A -f -F -l -n -q -m --window --depth --threads --keep-pack --write-midx --name-hash-version --path-walk --cruft --combine-cruft-below-size --max-cruft-size --no-reuse-delta --no-reuse-object --local -i --delta-islands -k --max-pack-size -g

## reset (5)
  --patch -p -U --unified --inter-hunk-context

## rev-list (1)
  --sparse

## show (1)
  -L

## stage (6)
  -i -p -U --unified --inter-hunk-context -e

## stash (14)
  -u --include-untracked --only-untracked --index -p --patch -S --staged -a --all --pathspec-from-file --pathspec-file-nul --print --to-ref
