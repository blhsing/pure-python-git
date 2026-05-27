"""rerere — reuse recorded resolution.

We store per-conflict-instance directories under .git/rr-cache/<hash>/:
  preimage   — file content with conflict markers (normalized)
  postimage  — the resolved file content (recorded by `rerere`)

The hash is sha1 of the normalized preimage. Normalization replaces the
"ours" branch label, hunk content, and "theirs" branch label with
positional placeholders so the same logical conflict at different commits
hashes the same.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .repo import Repository


_CONFLICT_RE = re.compile(
    r"^<<<<<<<.*?\n(.*?)^=======\n(.*?)^>>>>>>>.*?\n",
    re.MULTILINE | re.DOTALL,
)


def _normalize(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Return (normalized_text, list_of_(ours, theirs)_chunks).

    Normalization replaces conflict markers with bare <<<<<<< / ======= /
    >>>>>>> so identical conflicts at different commits collide.
    """
    chunks: list[tuple[str, str]] = []

    def _sub(m: re.Match) -> str:
        chunks.append((m.group(1), m.group(2)))
        return f"<<<<<<<\n{m.group(1)}=======\n{m.group(2)}>>>>>>>\n"

    return _CONFLICT_RE.sub(_sub, text), chunks


def _digest(text: str) -> str:
    norm, _ = _normalize(text)
    return hashlib.sha1(norm.encode("utf-8", errors="replace")).hexdigest()


def record_resolution(repo: Repository, path: str, preimage: str, postimage: str) -> str:
    """Record that the resolution of `preimage` for `path` was `postimage`."""
    h = _digest(preimage)
    d = repo.gitdir / "rr-cache" / h
    d.mkdir(parents=True, exist_ok=True)
    (d / "preimage").write_text(preimage, encoding="utf-8")
    (d / "postimage").write_text(postimage, encoding="utf-8")
    return h


def has_resolution(repo: Repository, preimage: str) -> bool:
    h = _digest(preimage)
    return (repo.gitdir / "rr-cache" / h / "postimage").exists()


def replay(repo: Repository, preimage: str) -> str | None:
    """If we have a recorded resolution for this preimage, return it."""
    h = _digest(preimage)
    p = repo.gitdir / "rr-cache" / h / "postimage"
    if p.exists():
        return p.read_text(encoding="utf-8")
    return None


def scan_and_record(repo: Repository) -> list[str]:
    """For each file that previously had conflict markers (preimage stored)
    and now has no markers, record the resolved post-image.

    Returns the list of paths recorded.
    """
    recorded: list[str] = []
    rr = repo.gitdir / "rr-cache"
    if not rr.exists():
        return recorded
    # Walk pending preimages: ones without a postimage and where the named
    # file in the worktree is now conflict-free.
    pending_meta = rr / "_pending.txt"
    if not pending_meta.exists():
        return recorded
    lines = pending_meta.read_text(encoding="utf-8").splitlines()
    new_lines: list[str] = []
    for line in lines:
        try:
            h, path = line.split("\t", 1)
        except ValueError:
            continue
        full = repo.path / path
        if not full.exists():
            new_lines.append(line)
            continue
        text = full.read_text(encoding="utf-8", errors="replace")
        if "<<<<<<<" in text:
            new_lines.append(line)
            continue
        # resolved — store postimage
        d = rr / h
        if (d / "preimage").exists():
            (d / "postimage").write_text(text, encoding="utf-8")
            recorded.append(path)
        else:
            new_lines.append(line)
    if new_lines:
        pending_meta.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    else:
        pending_meta.unlink(missing_ok=True)
    return recorded


def note_conflict(repo: Repository, path: str, preimage: str) -> str:
    """Note that `path` has a fresh conflict. If a previously-stored
    resolution exists for this preimage, return its postimage. Otherwise
    store the preimage under rr-cache and add to the pending list.
    """
    h = _digest(preimage)
    d = repo.gitdir / "rr-cache" / h
    d.mkdir(parents=True, exist_ok=True)
    if not (d / "preimage").exists():
        (d / "preimage").write_text(preimage, encoding="utf-8")
    post = d / "postimage"
    if post.exists():
        return post.read_text(encoding="utf-8")
    # add to pending list
    meta = repo.gitdir / "rr-cache" / "_pending.txt"
    line = f"{h}\t{path}\n"
    existing = meta.read_text(encoding="utf-8") if meta.exists() else ""
    if line not in existing:
        with meta.open("a", encoding="utf-8") as f:
            f.write(line)
    return ""
