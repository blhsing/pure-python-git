# pygit flag-parity gap manifest (vs git 2.54.0)

Probed 147 commands; 1479 documented flags scanned; 15 flags across 6 commands are currently *rejected* by pygit.

NOTE: a flag absent here only means pygit's parser accepts it — not that its behaviour is byte-identical. This is a lower bound on remaining parity work.

## fast-import (6)
  --date-format --max-pack-size --big-file-threshold --depth --active-branches --export-marks

## fetch (1)
  --auto

## mailsplit (1)
  -o

## pull (1)
  -S

## repack (2)
  --no-reuse-delta --no-reuse-object

## stash (4)
  --only-untracked --index --print --to-ref
