# pygit flag-parity gap manifest (vs git 2.54.0)

Probed 147 commands; 1479 documented flags scanned; 63 flags across 6 commands are currently *rejected* by pygit.

NOTE: a flag absent here only means pygit's parser accepts it — not that its behaviour is byte-identical. This is a lower bound on remaining parity work.

## am (26)
  --continue --skip --abort -i -n --no-verify --verify -q -s -u -k -b -m --keep-cr -c --quoted-cr -C -p -r --resolved --quit --show-current-patch --retry --allow-empty -S --empty

## fast-import (6)
  --date-format --max-pack-size --big-file-threshold --depth --active-branches --export-marks

## fetch (1)
  --auto

## pull (1)
  -S

## repack (25)
  -A -f -F -l -n -q -m --window --depth --threads --keep-pack --write-midx --name-hash-version --path-walk --cruft --combine-cruft-below-size --max-cruft-size --no-reuse-delta --no-reuse-object --local -i --delta-islands -k --max-pack-size -g

## stash (4)
  --only-untracked --index --print --to-ref
