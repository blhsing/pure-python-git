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


# ---------------------------------------------------------------------------
# reftable backend dispatch
#
# When the repository stores refs in the reftable format (.git/reftable/), all
# ref reads and writes go through pythongit.reftable.RefStore instead of loose
# files + packed-refs. The functions below detect this once per call and route
# accordingly so every command (commit, branch, tag, show-ref, ...) works on a
# reftable repo transparently.
# ---------------------------------------------------------------------------
def _reftable_store(repo: Repository):
    from . import reftable as _reftable
    if _reftable.is_reftable(repo.gitdir):
        return _reftable.RefStore(repo.gitdir, hash_size=repo.hash_len)
    return None


def uses_reftable(repo: Repository) -> bool:
    from . import reftable as _reftable
    return _reftable.is_reftable(repo.gitdir)


def _reftable_now() -> "tuple[int, int]":
    """Return (time_seconds, tz_offset) for a reflog entry.

    ``tz_offset`` matches C Git's reftable backend (fill_reftable_log_record):
    it is ``sign * atoi(HHMM)`` of the committer-ident timezone string, i.e. the
    raw 4-digit value (e.g. "+0530" -> 530, "-0800" -> -800), NOT total minutes.
    Honours GIT_COMMITTER_DATE ("<seconds> <+/-HHMM>") for reproducibility, then
    falls back to wall clock with the local UTC offset.
    """
    import time as _time
    env = os.environ.get("GIT_COMMITTER_DATE")
    if env:
        env = env.strip()
        parts = env.split()
        try:
            secs = int(parts[0])
        except (ValueError, IndexError):
            secs = int(_time.time())
        tz = 0
        if len(parts) > 1 and len(parts[1]) == 5 and parts[1][0] in "+-":
            sign = -1 if parts[1][0] == "-" else 1
            tz = sign * int(parts[1][1:5])  # raw HHMM, like atoi("0530")==530
        return secs, tz
    secs = int(_time.time())
    off = -_time.timezone if (_time.localtime().tm_isdst == 0) else -_time.altzone
    # Convert seconds-of-offset to the raw HHMM integer git would store.
    sign = -1 if off < 0 else 1
    off = abs(off) // 60
    return secs, sign * (off // 60 * 100 + off % 60)


def _reftable_committer() -> "tuple[str, str]":
    name = os.environ.get("GIT_COMMITTER_NAME") or "pythongit"
    email = os.environ.get("GIT_COMMITTER_EMAIL") or "pythongit@example.invalid"
    return name, email


def _reftable_peeler(repo: Repository):
    """Return a function hex_sha -> peeled hex_sha (None if not an annotated tag).

    Mirrors peel_object(PEEL_OBJECT_VERIFY_TAGGED_OBJECT_TYPE): an object peels
    only when it is a tag chain that ultimately resolves to a real object.
    """
    from . import objects as objs

    def peel(hexsha: str) -> Optional[str]:
        try:
            obj_type, data = objs.read_object(repo, hexsha)
        except (KeyError, ValueError):
            return None
        if obj_type != "tag":
            return None
        cur = hexsha
        for _ in range(32):
            try:
                obj_type, data = objs.read_object(repo, cur)
            except (KeyError, ValueError):
                return None
            if obj_type != "tag":
                return cur
            tgt = _tag_target(data)
            if tgt is None:
                return None
            cur = tgt
        return None

    return peel


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
    store = _reftable_store(repo)
    if store is not None:
        # HEAD is the only ref kept on disk for reftable repos; everything
        # else (including HEAD's symref target) lives in the table stack.
        if name == "HEAD":
            sym, sha = read_head(repo)
            return sha
        return store.read_ref(name)
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
    store = _reftable_store(repo)
    if store is not None:
        old = read_ref(repo, name) or repo.null_oid()
        # git's reftable backend logs the message verbatim (an empty message,
        # e.g. from `update-ref` without -m, is stored as just "\n"); it does
        # not synthesise an "update:" line the way our files path historically
        # did. Whether the ref is logged at all follows core.logAllRefUpdates.
        msg = message
        updates = [{"name": name, "type": "val1", "new": sha, "old": old,
                    "message": msg if _should_log(repo, name) else None}]
        # also log HEAD if it points at this ref (git logs both)
        head_sym = store.read_symbolic("HEAD")
        if head_sym == name:
            updates.append({"name": "HEAD", "type": "val1", "new": sha,
                            "old": old, "message": msg})
            # HEAD's symref target is updated implicitly; do not change it.
            updates[-1]["type"] = "headlog"
        store.transaction(_reftable_updates(repo, store, updates),
                          committer=_reftable_committer(), now=_reftable_now(),
                          peel=_reftable_peeler(repo))
        return
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


def rename_ref(repo: Repository, oldname: str, newname: str,
               logmsg: str) -> bool:
    """Rename ref ``oldname`` to ``newname`` through the reftable backend.

    Returns True on success. Only valid for reftable repos; callers gate on
    uses_reftable(). Mirrors reftable-backend.c write_copy_table (two update
    indices: delete old + reflog, create new + reflog, copy old reflog)."""
    store = _reftable_store(repo)
    if store is None:
        return False
    ok = store.rename_ref(oldname, newname, logmsg,
                          committer=_reftable_committer(), now=_reftable_now())
    return ok


def update_ref_symbolic(repo: Repository, name: str, target: str) -> None:
    """Write a symbolic ref ``name`` -> ``target`` (e.g. for symbolic-ref)."""
    store = _reftable_store(repo)
    if store is not None:
        store.transaction(
            [{"name": name, "type": "symref", "new": target, "old": None,
              "message": None}],
            committer=_reftable_committer(), now=_reftable_now())
        return
    p = repo.gitdir / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"ref: {target}\n", encoding="utf-8")


def _should_log(repo: Repository, name: str) -> bool:
    return (name.startswith("refs/heads/") or name == "HEAD"
            or name.startswith("refs/remotes/") or name == "refs/stash")


def _reftable_updates(repo: Repository, store, updates):
    """Normalise update dicts for RefStore.transaction.

    A 'headlog' pseudo-type logs HEAD (resolved sha) without rewriting HEAD's
    symref record; it is turned into a 'logonly' update here so HEAD's symref
    record keeps its original update_index (matching C Git's reftable backend,
    which only writes a HEAD reflog entry when the pointed-to branch moves).
    """
    out = []
    for up in updates:
        if up.get("type") == "headlog":
            out.append({"name": "HEAD", "type": "logonly", "new": None,
                        "old": up.get("old"), "message": up.get("message")})
        else:
            out.append(up)
    return out


def delete_ref(repo: Repository, name: str) -> None:
    store = _reftable_store(repo)
    if store is not None:
        old = read_ref(repo, name) or repo.null_oid()
        store.transaction(
            [{"name": name, "type": "delete", "new": None, "old": old,
              "message": None}],
            committer=_reftable_committer(), now=_reftable_now())
        return
    p = repo.gitdir / name
    if p.exists():
        p.unlink()


def read_head(repo: Repository) -> tuple[Optional[str], Optional[str]]:
    """Return (symbolic_ref_or_None, sha_or_None)."""
    store = _reftable_store(repo)
    if store is not None:
        rec = store.read_raw("HEAD")
        if rec is None:
            return None, None
        from . import reftable as _reftable
        if rec.value_type == _reftable.REF_SYMREF:
            return rec.target, store.read_ref(rec.target)
        if rec.value_type == _reftable.REF_VAL2:
            return None, rec.value[0].hex()
        if rec.value_type == _reftable.REF_VAL1:
            return None, rec.value.hex()
        return None, None
    p = repo.gitdir / "HEAD"
    if not p.exists():
        return None, None
    txt = p.read_text(encoding="utf-8").strip()
    if txt.startswith("ref: "):
        ref = txt[5:].strip()
        return ref, read_ref(repo, ref)
    return None, txt if _is_sha(txt, repo.hex_len) else None


def set_head(repo: Repository, target: str, *, message: Optional[str] = None,
             old: Optional[str] = None) -> None:
    """Point HEAD at ``target`` (a full ref, a branch shorthand, or a sha).

    For reftable repos, when ``message`` is given the symref switch and the
    HEAD reflog entry are written in one transaction (matching git, where e.g.
    "checkout: moving from X to Y" shares the update_index of the symref move).
    """
    store = _reftable_store(repo)
    if store is not None:
        if target.startswith("refs/"):
            typ, val = "symref", target
        elif _is_sha(target, repo.hex_len):
            typ, val = "val1", target
        else:
            typ, val = "symref", f"refs/heads/{target}"
        store.transaction(
            [{"name": "HEAD", "type": typ, "new": val, "old": old,
              "message": message}],
            committer=_reftable_committer(), now=_reftable_now())
        return
    p = repo.gitdir / "HEAD"
    if target.startswith("refs/"):
        p.write_text(f"ref: {target}\n", encoding="utf-8")
    elif _is_sha(target, repo.hex_len):
        p.write_text(target + "\n", encoding="utf-8")
    else:
        # branch shorthand
        p.write_text(f"ref: refs/heads/{target}\n", encoding="utf-8")


def iter_all_refs(repo: Repository) -> "dict[str, str]":
    """All refs as full-name -> resolved hex SHA (heads/tags/remotes/etc).

    For reftable repos this reads the table stack; for files repos it walks
    loose refs under refs/ plus packed-refs. Symbolic refs are followed; HEAD
    is not included (callers add it explicitly when wanted).
    """
    store = _reftable_store(repo)
    if store is not None:
        out = {}
        for name, sha in store.iter_refs().items():
            if name == "HEAD":
                continue
            out[name] = sha
        return out
    out: dict[str, str] = {}
    root = repo.gitdir / "refs"
    if root.exists():
        for f in root.rglob("*"):
            if f.is_file():
                rel = str(f.relative_to(repo.gitdir)).replace(os.sep, "/")
                s = read_ref(repo, rel)
                if s:
                    out[rel] = s
    for ref, s in read_packed_refs(repo).items():
        out.setdefault(ref, s)
    return out


def read_symbolic(repo: Repository, name: str) -> Optional[str]:
    """Return the target of symbolic ref ``name`` (e.g. "refs/heads/main"), or
    None if ``name`` is not a symbolic ref / does not exist."""
    store = _reftable_store(repo)
    if store is not None:
        return store.read_symbolic(name)
    p = repo.gitdir / name
    if not p.exists():
        return None
    txt = p.read_text(encoding="utf-8").strip()
    if txt.startswith("ref: "):
        return txt[5:].strip()
    return None


def ref_record_exists(repo: Repository, name: str) -> bool:
    store = _reftable_store(repo)
    if store is not None:
        return store.read_raw(name) is not None
    return (repo.gitdir / name).exists()


def read_raw_ref_exists(repo: Repository, name: str) -> bool:
    """True if ``name`` exists as a direct (non-dwim) ref record."""
    store = _reftable_store(repo)
    if store is not None:
        return store.read_raw(name) is not None
    return ((repo.gitdir / name).is_file() or name in read_packed_refs(repo))


def peel_ref(repo: Repository, name: str) -> Optional[str]:
    """Return the peeled (VAL2) target for a tag ref if stored, else None."""
    store = _reftable_store(repo)
    if store is not None:
        return store.peeled(name)
    return None


def list_branches(repo: Repository) -> list[str]:
    store = _reftable_store(repo)
    if store is not None:
        return sorted(name[len("refs/heads/"):]
                      for name in store.iter_refs()
                      if name.startswith("refs/heads/"))
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
    store = _reftable_store(repo)
    if store is not None:
        return sorted(name[len("refs/tags/"):]
                      for name in store.iter_refs()
                      if name.startswith("refs/tags/"))
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
