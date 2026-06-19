#!/usr/bin/env python3
"""Enumerate, per git 2.54.0 subcommand, the flags that pygit does NOT yet
accept (a definite, finite subset of "needs parity").

For each command in the parity manifest we read git's own `-h` usage to list
its documented options, then probe pygit with each flag in a throwaway repo and
record those it rejects ("unrecognized arguments" / "invalid choice" / "no such
option" / "unknown option"). A flag that pygit *accepts* is NOT proof of
behavioural parity — only that the option parses — so this report is a lower
bound on remaining work, not an upper bound.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile

ORACLE = os.environ.get("PYGIT_PARITY_GIT", "/tmp/git-2.54.0/bin/git")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

ENV = {
    **os.environ,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "LC_ALL": "C",
    "TZ": "UTC",
    "PYTHONPATH": ROOT + os.pathsep + os.environ.get("PYTHONPATH", ""),
    "GIT_AUTHOR_NAME": "Parity", "GIT_AUTHOR_EMAIL": "parity@example.com",
    "GIT_COMMITTER_NAME": "Parity", "GIT_COMMITTER_EMAIL": "parity@example.com",
    "GIT_AUTHOR_DATE": "1700000000 +0000", "GIT_COMMITTER_DATE": "1700000000 +0000",
}

REJECT = ("unrecognized arguments", "invalid choice", "no such option",
          "unknown option", "unrecognized option")

FLAG_RE = re.compile(r"(?<![\w-])(--[A-Za-z][A-Za-z0-9-]+|-[A-Za-z])\b")


def git_flags(cmd: str) -> list[str]:
    """Parse `git <cmd> -h` stderr for the option tokens it documents."""
    try:
        p = subprocess.run([ORACLE, cmd, "-h"], capture_output=True, text=True,
                           env=ENV, timeout=10)
    except Exception:
        return []
    text = (p.stderr or "") + (p.stdout or "")
    flags: list[str] = []
    seen = set()
    for m in FLAG_RE.finditer(text):
        f = m.group(1)
        # Skip placeholders/usage noise.
        if f in ("--", "-h"):
            continue
        if f not in seen:
            seen.add(f)
            flags.append(f)
    return flags


def make_repo() -> str:
    d = tempfile.mkdtemp()
    subprocess.run([sys.executable, "-m", "pythongit", "init", "-q", "-b", "main", "."],
                   cwd=d, env=ENV, capture_output=True)
    with open(os.path.join(d, "f.txt"), "w") as fh:
        fh.write("a\nb\n")
    subprocess.run([sys.executable, "-m", "pythongit", "add", "-A"], cwd=d, env=ENV, capture_output=True)
    subprocess.run([sys.executable, "-m", "pythongit", "commit", "-m", "c1"], cwd=d, env=ENV, capture_output=True)
    return d


def pygit_rejects(cmd: str, flag: str, repo: str) -> bool:
    try:
        p = subprocess.run([sys.executable, "-m", "pythongit", cmd, flag],
                           cwd=repo, env=ENV, capture_output=True, text=True, timeout=15)
    except Exception:
        return False
    err = (p.stderr or "").lower()
    return any(marker in err for marker in REJECT)


def main() -> int:
    import json
    manifest = json.load(open(os.path.join(ROOT, "tests/git_parity/manifest/git-2.54.0.json")))
    cmds = manifest["commands"]
    names = sorted(x["name"] if isinstance(x, dict) else x for x in cmds)

    # Only probe commands pygit registers as real (not stubs).
    import pythongit.cli as c
    registered = set(c._COMMANDS.keys())

    report: dict[str, list[str]] = {}
    total_flags = total_gap = 0
    for name in names:
        if name not in registered:
            continue
        flags = git_flags(name)
        if not flags:
            continue
        repo = make_repo()
        missing = [f for f in flags if pygit_rejects(name, f, repo)]
        total_flags += len(flags)
        if missing:
            report[name] = missing
            total_gap += len(missing)

    out = [f"# pygit flag-parity gap manifest (vs git {manifest['git_version']})",
           "",
           f"Probed {len(names)} commands; {total_flags} documented flags scanned; "
           f"{total_gap} flags across {len(report)} commands are currently *rejected* by pygit.",
           "",
           "NOTE: a flag absent here only means pygit's parser accepts it — not that its "
           "behaviour is byte-identical. This is a lower bound on remaining parity work.",
           ""]
    for name in sorted(report):
        out.append(f"## {name} ({len(report[name])})")
        out.append("  " + " ".join(report[name]))
        out.append("")
    open(os.path.join(ROOT, "docs/parity-flag-gaps.md"), "w").write("\n".join(out))
    print("\n".join(out[:6]))
    print(f"... wrote docs/parity-flag-gaps.md ({total_gap} flags / {len(report)} commands)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
