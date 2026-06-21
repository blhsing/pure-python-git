# pygit flag-parity gap manifest (vs git 2.54.0)

Probed 147 commands; 1479 documented flags scanned; 150 flags across 15 commands are currently *rejected* by pygit.

NOTE: a flag absent here only means pygit's parser accepts it — not that its behaviour is byte-identical. This is a lower bound on remaining parity work.

## add (6)
  -i -p -U --unified --inter-hunk-context -e

## am (26)
  --continue --skip --abort -i -n --no-verify --verify -q -s -u -k -b -m --keep-cr -c --quoted-cr -C -p -r --resolved --quit --show-current-patch --retry --allow-empty -S --empty

## apply (1)
  --add

## commit (8)
  --interactive --patch -S -t -p -U --unified --inter-hunk-context

## diff-files (2)
  -c --cc

## difftool (7)
  -g -d -y --no-prompt -x --no-index --index

## fast-import (6)
  --date-format --max-pack-size --big-file-threshold --depth --active-branches --export-marks

## fetch (22)
  --multiple --all -v -q -a -f -m -t -n --no-tags -j -p -P -k -u --unshallow --refetch --refmap -o --ipv4 --ipv6 --auto

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

## stage (6)
  -i -p -U --unified --inter-hunk-context -e

## stash (4)
  --only-untracked --index --print --to-ref
