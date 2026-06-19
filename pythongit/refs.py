"""Reference resolution and update.

Refs may live as loose files (.git/refs/...) or in packed-refs.
HEAD may be a symref ("ref: refs/heads/main") or a detached sha.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from .repo import Repository


SHA_LEN = 40


def _is_sha(s: str, hex_len: int = SHA_LEN) -> bool:
    return len(s) == hex_len and all(c in "0123456789abcdef" for c in s.lower())


def read_packed_refs(repo: Repository) -> dict[str, str]:
    f = repo.gitdir / "packed-refs"
    out: dict[str, str] = {}
    if not f.exists():
        return out
    for line in f.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or line.startswith("^"):
            continue
        sha, _, name = line.partition(" ")
        out[name] = sha
    return out


def read_ref(repo: Repository, name: str) -> Optional[str]:
    """Return the SHA the ref ultimately points at, following symrefs."""
    seen: set[str] = set()
    cur = name
    while True:
        if cur in seen:
            return None
        seen.add(cur)
        p = repo.gitdir / cur
        if p.exists():
            txt = p.read_text(encoding="utf-8").strip()
        else:
            packed = read_packed_refs(repo)
            if cur in packed:
                return packed[cur]
            return None
        if txt.startswith("ref: "):
            cur = txt[5:].strip()
            continue
        return txt if _is_sha(txt, repo.hex_len) else None


def update_ref(repo: Repository, name: str, sha: str, *, message: str = "") -> None:
    if not _is_sha(sha, repo.hex_len):
        raise ValueError(f"not a sha: {sha}")
    old = read_ref(repo, name) or repo.null_oid()
    p = repo.gitdir / name
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(sha + "\n", encoding="utf-8")
    os.replace(tmp, p)
    # reflog
    if name.startswith("refs/heads/") or name == "HEAD" or name.startswith("refs/remotes/") or name == "refs/stash":
        from . import reflog as _reflog
        _reflog.append(repo, name, old, sha, message or f"update: {name}")
        # also log HEAD if it points at this ref
        try:
            sym = (repo.gitdir / "HEAD").read_text(encoding="utf-8").strip()
            if sym == f"ref: {name}":
                _reflog.append(repo, "HEAD", old, sha, message or f"update: {name}")
        except FileNotFoundError:
            pass


def delete_ref(repo: Repository, name: str) -> None:
    p = repo.gitdir / name
    if p.exists():
        p.unlink()


def read_head(repo: Repository) -> tuple[Optional[str], Optional[str]]:
    """Return (symbolic_ref_or_None, sha_or_None)."""
    p = repo.gitdir / "HEAD"
    if not p.exists():
        return None, None
    txt = p.read_text(encoding="utf-8").strip()
    if txt.startswith("ref: "):
        ref = txt[5:].strip()
        return ref, read_ref(repo, ref)
    return None, txt if _is_sha(txt, repo.hex_len) else None


def set_head(repo: Repository, target: str) -> None:
    p = repo.gitdir / "HEAD"
    if target.startswith("refs/"):
        p.write_text(f"ref: {target}\n", encoding="utf-8")
    elif _is_sha(target, repo.hex_len):
        p.write_text(target + "\n", encoding="utf-8")
    else:
        # branch shorthand
        p.write_text(f"ref: refs/heads/{target}\n", encoding="utf-8")


def list_branches(repo: Repository) -> list[str]:
    root = repo.gitdir / "refs" / "heads"
    found: set[str] = set()
    if root.exists():
        for f in root.rglob("*"):
            if f.is_file():
                found.add(str(f.relative_to(root)).replace(os.sep, "/"))
    for ref in read_packed_refs(repo):
        if ref.startswith("refs/heads/"):
            found.add(ref[len("refs/heads/") :])
    return sorted(found)


def list_tags(repo: Repository) -> list[str]:
    root = repo.gitdir / "refs" / "tags"
    found: set[str] = set()
    if root.exists():
        for f in root.rglob("*"):
            if f.is_file():
                found.add(str(f.relative_to(root)).replace(os.sep, "/"))
    for ref in read_packed_refs(repo):
        if ref.startswith("refs/tags/"):
            found.add(ref[len("refs/tags/") :])
    return sorted(found)


def _resolve_base(repo: Repository, name: str) -> Optional[str]:
    """Resolve a ref-ish with no ^/~ suffix operators to a full SHA.

    DWIM precedence follows C Git's ``ref_rev_parse_rules``: the literal name
    under $GIT_DIR, then refs/, refs/tags/, refs/heads/, refs/remotes/, and
    refs/remotes/<name>/HEAD. Note tags resolve *before* heads.
    """
    name = name.strip()
    if name == "@":
        name = "HEAD"
    for candidate in (
        name,
        f"refs/{name}",
        f"refs/tags/{name}",
        f"refs/heads/{name}",
        f"refs/remotes/{name}",
        f"refs/remotes/{name}/HEAD",
    ):
        sha = read_ref(repo, candidate)
        if sha:
            return sha
    # raw / abbreviated sha
    low = name.lower()
    if _is_sha(low, repo.hex_len):
        return low
    if 4 <= len(low) <= repo.hex_len and all(c in "0123456789abcdef" for c in low):
        # search loose objects through the persistent loose-object cache
        from . import loose as _loose
        loose_match = _loose.resolve_short(repo, low)
        if loose_match is None:
            return None
        if loose_match:
            return loose_match
        # search packs
        from . import pack as _pack
        m = _pack.resolve_short(repo, low)
        if m:
            return m
    return None


def _tag_target(data: bytes) -> Optional[str]:
    head = data.decode("utf-8", "replace").split("\n\n", 1)[0]
    for line in head.splitlines():
        key, _, val = line.partition(" ")
        if key == "object":
            return val.strip()
    return None


def _commit_parents(repo: Repository, sha: str) -> list[str]:
    from . import objects as objs
    try:
        obj_type, data = objs.read_object(repo, sha)
    except KeyError:
        return []
    if obj_type != "commit":
        return []
    return list(objs.parse_commit(data).parents)


def _peel_to_commit(repo: Repository, sha: str) -> Optional[str]:
    from . import objects as objs
    cur: Optional[str] = sha
    for _ in range(32):
        try:
            obj_type, data = objs.read_object(repo, cur)
        except KeyError:
            return None
        if obj_type == "commit":
            return cur
        if obj_type == "tag":
            cur = _tag_target(data)
            if cur is None:
                return None
            continue
        return None
    return None


def _peel_to_type(repo: Repository, sha: str, spec: str) -> Optional[str]:
    from . import objects as objs
    if spec == "object":
        return sha
    if spec.startswith("/"):
        # commit-message text search is not supported
        return None
    cur: Optional[str] = sha
    for _ in range(32):
        try:
            obj_type, data = objs.read_object(repo, cur)
        except KeyError:
            return None
        if spec == "" and obj_type != "tag":
            return cur
        if spec and obj_type == spec:
            return cur
        if obj_type == "tag":
            cur = _tag_target(data)
            if cur is None:
                return None
            continue
        if obj_type == "commit" and spec == "tree":
            return objs.parse_commit(data).tree
        return None
    return None


def _split_revision(name: str) -> tuple[str, list[str]]:
    """Split a revision into its base name and ordered suffix operators."""
    i = 0
    while i < len(name) and name[i] not in "^~":
        i += 1
    base, rest = name[:i], name[i:]
    ops: list[str] = []
    while rest:
        ch = rest[0]
        if ch == "~":
            j = 1
            while j < len(rest) and rest[j].isdigit():
                j += 1
            ops.append(rest[:j])
            rest = rest[j:]
        elif ch == "^":
            if len(rest) > 1 and rest[1] == "{":
                end = rest.find("}")
                if end == -1:
                    ops.append(rest)
                    rest = ""
                else:
                    ops.append(rest[: end + 1])
                    rest = rest[end + 1 :]
            else:
                j = 1
                while j < len(rest) and rest[j].isdigit():
                    j += 1
                ops.append(rest[:j])
                rest = rest[j:]
        else:  # pragma: no cover - defensive
            break
    return base, ops


def _apply_op(repo: Repository, sha: str, op: str) -> Optional[str]:
    if op[0] == "~":
        count = int(op[1:]) if len(op) > 1 else 1
        cur: Optional[str] = _peel_to_commit(repo, sha)
        for _ in range(count):
            if cur is None:
                return None
            parents = _commit_parents(repo, cur)
            cur = parents[0] if parents else None
        return cur
    # op[0] == "^"
    arg = op[1:]
    if arg.startswith("{"):
        return _peel_to_type(repo, sha, arg[1:-1])
    count = int(arg) if arg else 1
    if count == 0:
        return _peel_to_commit(repo, sha)
    commit = _peel_to_commit(repo, sha)
    if commit is None:
        return None
    parents = _commit_parents(repo, commit)
    if count > len(parents):
        return None
    return parents[count - 1]


def dwim_full_name(repo: Repository, name: str) -> Optional[str]:
    """Return the full ref name ``name`` resolves to, or None.

    Mirrors C Git's ``dwim_ref``: HEAD yields the branch it points at (or the
    literal "HEAD" when detached), and other names follow the rev-parse rules.
    """
    name = name.strip()
    if name == "@":
        name = "HEAD"
    if name == "HEAD":
        sym, _ = read_head(repo)
        return sym if sym else "HEAD"
    for candidate in (
        f"refs/{name}",
        f"refs/tags/{name}",
        f"refs/heads/{name}",
        f"refs/remotes/{name}",
        f"refs/remotes/{name}/HEAD",
    ):
        if read_ref(repo, candidate) is not None:
            return candidate
    if read_ref(repo, name) is not None and name.startswith("refs/"):
        return name
    return None


def shorten_ref(full: str) -> str:
    """Shorten a full ref name the way ``--abbrev-ref`` does for common cases."""
    for prefix in ("refs/heads/", "refs/tags/", "refs/remotes/"):
        if full.startswith(prefix):
            return full[len(prefix):]
    if full.startswith("refs/"):
        return full[len("refs/"):]
    return full


def _object_at_path(repo: Repository, start_sha: str, path: str) -> Optional[str]:
    from . import objects as objs
    cur = start_sha
    try:
        obj_type, data = objs.read_object(repo, cur)
    except KeyError:
        return None
    if obj_type == "commit":
        cur = objs.parse_commit(data).tree
    elif obj_type == "tag":
        cur = _peel_to_type(repo, cur, "tree")
        if cur is None:
            return None
    if path == "":
        return cur
    for part in path.split("/"):
        try:
            obj_type, data = objs.read_object(repo, cur)
        except KeyError:
            return None
        if obj_type != "tree":
            return None
        match = next((e for e in objs.parse_tree(data, repo.hash_len) if e.name == part), None)
        if match is None:
            return None
        cur = match.sha
    return cur


def _resolve_revision(repo: Repository, name: str) -> Optional[str]:
    base, ops = _split_revision(name)
    sha = _resolve_base(repo, base)
    if sha is None:
        return None
    for op in ops:
        sha = _apply_op(repo, sha, op)
        if sha is None:
            return None
    return sha


def rev_parse(repo: Repository, name: str) -> Optional[str]:
    """Resolve a rev-ish to a full SHA.

    Accepts: full sha, abbreviated sha (>=4), HEAD, ``@``, branch, tag,
    refs/heads/x, refs/tags/x, refs/remotes/x, the suffix operators
    ``^``, ``^<n>``, ``~<n>``, ``^{}``, ``^{<type>}``, and the
    ``<tree-ish>:<path>`` / ``:<path>`` path-in-tree syntax.
    """
    import re as _re

    name = name.strip()
    if not name:
        return None
    # <ref>@{<n>}: the n-th prior value of <ref> from its reflog (0 = current).
    # A bare @{<n>} refers to HEAD. The index counts reflog positions directly,
    # so it stays consistent with `reflog`/`log -g` selector numbering. Any
    # trailing ^/~ suffix operators are applied to the resolved commit.
    at_match = _re.match(r"^(.*?)@\{(\d+)\}(.*)$", name)
    if at_match and not name.startswith("^{"):
        refname = at_match.group(1) or "HEAD"
        n = int(at_match.group(2))
        rest = at_match.group(3)
        from . import reflog as _reflog
        full = refname if refname == "HEAD" else (dwim_full_name(repo, refname) or refname)
        entries = _reflog.read(repo, full)
        if n >= len(entries):
            return None
        base_sha = entries[-(n + 1)][1]
        if rest:
            return _resolve_revision(repo, base_sha + rest)
        return base_sha
    if ":" in name and not name.startswith("^{"):
        left, _, path = name.partition(":")
        if path.startswith("/"):
            return None  # ``:/text`` commit-message search is unsupported
        if left == "":
            stage_match = _re.match(r"^(\d+):(.*)$", path)
            if stage_match:
                path = stage_match.group(2)
            from .index import read_index
            entry = read_index(repo).by_path().get(path)
            return entry.sha if entry else None
        base_sha = _resolve_revision(repo, left)
        if base_sha is None:
            return None
        return _object_at_path(repo, base_sha, path)
    return _resolve_revision(repo, name)
