# pygit flag-parity gap manifest (vs git 2.54.0)

Probed 147 commands; 1479 documented flags scanned; 417 flags across 48 commands are currently *rejected* by pygit.

NOTE: a flag absent here only means pygit's parser accepts it — not that its behaviour is byte-identical. This is a lower bound on remaining parity work.

## add (7)
  -i -p -U --unified --inter-hunk-context -e -N

## am (26)
  --continue --skip --abort -i -n --no-verify --verify -q -s -u -k -b -m --keep-cr -c --quoted-cr -C -p -r --resolved --quit --show-current-patch --retry --allow-empty -S --empty

## apply (10)
  -p --no-add --add -N --intent-to-add --ours --theirs --union -z -C

## archive (4)
  --remote --exec -v --mtime

## backfill (1)
  --min-batch-size

## bugreport (4)
  -s --suffix --no-suffix --diagnose

## cat-file (5)
  --buffer --follow-symlinks --unordered -Z --use-mailmap

## checkout-index (7)
  -q -n --no-create --create -u -z --stage

## clean (5)
  -i -q -e -X --exclude

## column (1)
  --raw-mode

## commit (19)
  --interactive --patch -u --dry-run -c -C --squash --fixup --pathspec-from-file --pathspec-file-nul -S -t -p -U --unified --inter-hunk-context --verify -z --post-rewrite

## describe (1)
  --dirty

## diagnose (3)
  -s --suffix --mode

## diff-files (22)
  -q -c --cc -z -p -u --patch-with-raw --patch-with-stat --name-status --full-index --abbrev -R -B -M -C --find-copies-harder -l -O -S --pickaxe-all -a --text

## diff-pairs (83)
  -z -p --patch -s --no-patch -u -U --unified -W --raw --patch-with-raw --patch-with-stat --stat --numstat --shortstat -X --dirstat --cumulative --dirstat-by-file --check --summary --name-only --name-status --stat-width --stat-name-width --stat-graph-width --stat-count --binary --ws-error-highlight --src-prefix --dst-prefix --line-prefix --no-prefix --default-prefix --inter-hunk-context --output-indicator-new --output-indicator-old --output-indicator-context -B --break-rewrites -M --find-renames -D --irreversible-delete -C --find-copies --no-renames -l --minimal -w --ignore-all-space -b --ignore-space-change --ignore-space-at-eol --ignore-cr-at-eol --ignore-blank-lines -I --patience --histogram --diff-algorithm --anchored --word-diff --word-diff-regex --color-words --color-moved -a -R --ignore-submodules --submodule --ita-invisible-in-index -N --ita-visible-in-index -S -G --pickaxe-all --pickaxe-regex -O --rotate-to --skip-to --find-object --diff-filter --max-depth --output

## difftool (7)
  -g -d -y --no-prompt -x --no-index --index

## fast-export (3)
  --no-data --data --anonymize-map

## fast-import (6)
  --date-format --max-pack-size --big-file-threshold --depth --active-branches --export-marks

## fetch (22)
  --multiple --all -v -q -a -f -m -t -n --no-tags -j -p -P -k -u --unshallow --refetch --refmap -o --ipv4 --ipv6 --auto

## fmt-merge-msg (4)
  -m --log --no-log -F

## hash-object (1)
  --filters

## init (4)
  --template --separate-git-dir --ref-format --shared

## init-db (4)
  --template --separate-git-dir --ref-format --shared

## interpret-trailers (6)
  --in-place --trim-empty --parse --unfold --no-divider --divider

## log (4)
  -q --use-mailmap --clear-decorations -L

## merge-tree (6)
  --quiet -z --name-only --allow-unrelated-histories --stdin -X

## mktree (3)
  -z --missing --batch

## pack-redundant (2)
  --verbose --alt-odb

## pack-refs (4)
  --no-prune --auto --include --exclude

## patch-id (2)
  --unstable --verbatim

## prune (3)
  -v --progress --expire

## prune-packed (2)
  -q --quiet

## pull (17)
  -v -q -r -n --ff-only -s -X -S -a -f -t -p -j -k --unshallow --refmap -o

## push (14)
  -v -q --all -d --branches --mirror -n -f -u --no-verify --verify -o --ipv4 --ipv6

## repack (25)
  -A -f -F -l -n -q -m --window --depth --threads --keep-pack --write-midx --name-hash-version --path-walk --cruft --combine-cruft-below-size --max-cruft-size --no-reuse-delta --no-reuse-object --local -i --delta-islands -k --max-pack-size -g

## replace (7)
  -f --edit --graft --convert-graft-file --format -e -g

## reset (6)
  --patch -p -U --unified --inter-hunk-context -N

## rev-list (3)
  --sparse --stdin --exclude-hidden

## shortlog (3)
  --pretty -c -w

## show (4)
  -q --use-mailmap --clear-decorations -L

## show-ref (6)
  --abbrev --branches -q --quiet --exclude-existing --exists

## stage (7)
  -i -p -U --unified --inter-hunk-context -e -N

## stash (19)
  -u --include-untracked --only-untracked -q --quiet --index -p --patch -S --staged -k -a --all -m --message --pathspec-from-file --pathspec-file-nul --print --to-ref

## status (5)
  -v --no-renames --renames -M --find-renames

## switch (6)
  -C -q -m -d -t -f

## unpack-objects (4)
  -n -q -r --strict

## update-index (8)
  --skip-worktree --no-skip-worktree --unresolve -g --again --clear-resolve-undo --fsmonitor-valid --no-fsmonitor-valid

## update-server-info (2)
  -f --force
