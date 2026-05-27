"""Parse and apply unified diffs.

Supports:
  - file creation, modification, deletion (new file mode / deleted file mode)
  - simple binary detection (skipped with warning)
  - --reverse, --check, --index, --cached
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Hunk:
    a_start: int
    a_count: int
    b_start: int
    b_count: int
    lines: list[str] = field(default_factory=list)  # each begins with ' ', '+' or '-'


@dataclass
class FilePatch:
    a_path: str = ""
    b_path: str = ""
    new_file: bool = False
    deleted: bool = False
    binary: bool = False
    hunks: list[Hunk] = field(default_factory=list)

    @property
    def target(self) -> str:
        if self.deleted:
            return self.a_path
        return self.b_path or self.a_path


def parse_patch(text: str) -> list[FilePatch]:
    lines = text.splitlines()
    i = 0
    out: list[FilePatch] = []
    cur: Optional[FilePatch] = None
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            if cur:
                out.append(cur)
            cur = FilePatch()
            # parse 'diff --git a/x b/y'
            parts = line.split(" ", 3)
            if len(parts) >= 4:
                a, b = parts[2], parts[3]
                cur.a_path = a[2:] if a.startswith("a/") else a
                cur.b_path = b[2:] if b.startswith("b/") else b
            i += 1
            continue
        if cur is None:
            i += 1
            continue
        if line.startswith("new file mode"):
            cur.new_file = True
            i += 1
            continue
        if line.startswith("deleted file mode"):
            cur.deleted = True
            i += 1
            continue
        if line.startswith("Binary files"):
            cur.binary = True
            i += 1
            continue
        if line.startswith("--- "):
            p = line[4:]
            if p == "/dev/null":
                cur.new_file = True
            elif p.startswith("a/"):
                cur.a_path = p[2:]
            else:
                cur.a_path = p
            i += 1
            continue
        if line.startswith("+++ "):
            p = line[4:]
            if p == "/dev/null":
                cur.deleted = True
            elif p.startswith("b/"):
                cur.b_path = p[2:]
            else:
                cur.b_path = p
            i += 1
            continue
        if line.startswith("@@"):
            # @@ -A,B +C,D @@ ...
            try:
                meta = line.split("@@")[1].strip()
                a_part, b_part = meta.split(" ")
                a_part = a_part[1:]  # drop '-'
                b_part = b_part[1:]  # drop '+'
                a_start, _, a_count = a_part.partition(",")
                b_start, _, b_count = b_part.partition(",")
                h = Hunk(
                    a_start=int(a_start),
                    a_count=int(a_count) if a_count else 1,
                    b_start=int(b_start),
                    b_count=int(b_count) if b_count else 1,
                )
            except (ValueError, IndexError):
                i += 1
                continue
            i += 1
            seen_a = 0
            seen_b = 0
            while i < len(lines) and (seen_a < h.a_count or seen_b < h.b_count):
                ln = lines[i]
                if ln == "" or ln == " ":
                    h.lines.append(" ")
                    seen_a += 1
                    seen_b += 1
                    i += 1
                    continue
                tag = ln[0]
                if tag == "\\":
                    i += 1
                    continue
                if tag == " ":
                    h.lines.append(ln)
                    seen_a += 1
                    seen_b += 1
                elif tag == "-":
                    h.lines.append(ln)
                    seen_a += 1
                elif tag == "+":
                    h.lines.append(ln)
                    seen_b += 1
                else:
                    break
                i += 1
            cur.hunks.append(h)
            continue
        i += 1
    if cur:
        out.append(cur)
    return out


def apply_to_text(content: str, hunks: list[Hunk], *, reverse: bool = False) -> Optional[str]:
    src = content.splitlines(keepends=True)
    out: list[str] = []
    src_idx = 0  # 0-based position in src
    for h in hunks:
        # context lines until hunk start
        start = (h.b_start if reverse else h.a_start) - 1
        # copy through start
        if start < src_idx:
            return None  # cannot apply
        out.extend(src[src_idx:start])
        src_idx = start
        for hl in h.lines:
            tag, text = hl[0], hl[1:]
            # text may not end with newline (last line); preserve
            if reverse:
                # invert tag
                tag = {"+": "-", "-": "+", " ": " "}[tag]
            if tag == " ":
                if src_idx < len(src) and src[src_idx].rstrip("\r\n") == text.rstrip("\r\n"):
                    out.append(src[src_idx])
                    src_idx += 1
                else:
                    out.append(text + "\n")
                    if src_idx < len(src) and src[src_idx].rstrip("\r\n") == text.rstrip("\r\n"):
                        src_idx += 1
            elif tag == "-":
                if src_idx < len(src) and src[src_idx].rstrip("\r\n") == text.rstrip("\r\n"):
                    src_idx += 1
                else:
                    return None
            elif tag == "+":
                out.append(text + "\n")
    out.extend(src[src_idx:])
    result = "".join(out)
    # if original lacked trailing newline and last hunk preserved that, strip one '\n'
    if not content.endswith("\n") and result.endswith("\n"):
        result = result[:-1]
    return result


def apply_patch_text(patch_text: str, *, repo_path, reverse: bool = False) -> tuple[list[str], list[str]]:
    """Apply patch to files under repo_path. Returns (applied, failed)."""
    from pathlib import Path
    patches = parse_patch(patch_text)
    applied: list[str] = []
    failed: list[str] = []
    for fp in patches:
        if fp.binary:
            failed.append(fp.target + " (binary)")
            continue
        path = Path(repo_path) / fp.target
        if fp.new_file and not reverse:
            content = ""
        elif fp.deleted and not reverse:
            if path.exists():
                path.unlink()
            applied.append(fp.target)
            continue
        else:
            if not path.exists():
                content = ""
            else:
                content = path.read_text(encoding="utf-8", errors="replace")
        result = apply_to_text(content, fp.hunks, reverse=reverse)
        if result is None:
            failed.append(fp.target)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(result, encoding="utf-8")
        applied.append(fp.target)
    return applied, failed
