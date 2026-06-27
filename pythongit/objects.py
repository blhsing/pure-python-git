"""Git object storage: blob, tree, commit, tag.

Loose objects are stored zlib-compressed under .git/objects/xx/yyyy...
Pack objects are read transparently by `read_object` via the pack module.
"""
from __future__ import annotations

import hashlib
import os
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from .repo import Repository


# ---------------------------------------------------------------------------
# low-level loose object I/O


def _loose_path(repo: Repository, sha: str) -> Path:
    return repo.gitdir / "objects" / sha[:2] / sha[2:]


def hash_bytes(obj_type: str, data: bytes, repo: Optional[Repository] = None) -> tuple[str, bytes]:
    """Return (object id, serialized) for a raw object payload."""
    header = f"{obj_type} {len(data)}".encode() + b"\0"
    full = header + data
    if repo is not None:
        return repo.hash_hex(full), full
    return hashlib.sha1(full).hexdigest(), full


def write_object(repo: Repository, obj_type: str, data: bytes) -> str:
    sha, full = hash_bytes(obj_type, data, repo)
    p = _loose_path(repo, sha)
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        # git compresses loose objects at zlib level 1 (core.loosecompression's
        # default), which yields byte-identical files to the reference tool.
        tmp.write_bytes(zlib.compress(full, 1))
        os.replace(tmp, p)
        try:
            from . import loose as _loose

            _loose.clear_cache(repo)
        except Exception:
            pass
    return sha


def read_object(repo: Repository, sha: str) -> tuple[str, bytes]:
    """Return (type, payload). Looks in loose objects then packs."""
    p = _loose_path(repo, sha)
    if p.exists():
        raw = zlib.decompress(p.read_bytes())
        nul = raw.index(b"\0")
        header = raw[:nul].decode()
        obj_type, _, size = header.partition(" ")
        data = raw[nul + 1 :]
        if int(size) != len(data):
            raise ValueError(f"object {sha} size mismatch")
        return obj_type, data
    # try packs
    from . import pack as _pack
    res = _pack.find_in_packs(repo, sha)
    if res is None:
        raise KeyError(sha)
    return res


def object_exists(repo: Repository, sha: str) -> bool:
    if _loose_path(repo, sha).exists():
        return True
    from . import pack as _pack
    return _pack.find_in_packs(repo, sha) is not None


# ---------------------------------------------------------------------------
# tree


@dataclass
class TreeEntry:
    mode: str  # e.g. "100644", "100755", "120000", "40000"
    name: str
    sha: str

    def is_dir(self) -> bool:
        return self.mode in ("40000", "040000")

    def is_gitlink(self) -> bool:
        return self.mode == "160000"


def parse_tree(data: bytes, hash_len: int = 20) -> list[TreeEntry]:
    entries: list[TreeEntry] = []
    i = 0
    while i < len(data):
        sp = data.index(b" ", i)
        mode = data[i:sp].decode()
        nul = data.index(b"\0", sp)
        name = data[sp + 1 : nul].decode("utf-8", errors="replace")
        sha = data[nul + 1 : nul + 1 + hash_len].hex()
        entries.append(TreeEntry(mode, name, sha))
        i = nul + 1 + hash_len
    return entries


def encode_tree(entries: Iterable[TreeEntry]) -> bytes:
    # Git sorts tree entries by name with directories effectively having a
    # trailing slash for comparison purposes.
    def key(e: TreeEntry) -> bytes:
        suffix = b"/" if e.is_dir() else b""
        return e.name.encode("utf-8") + suffix

    out = bytearray()
    for e in sorted(entries, key=key):
        out += e.mode.lstrip("0").encode() or b"0"
        out += b" " + e.name.encode("utf-8") + b"\0" + bytes.fromhex(e.sha)
    return bytes(out)


# ---------------------------------------------------------------------------
# commit / tag


@dataclass
class Commit:
    tree: str
    parents: list[str] = field(default_factory=list)
    author: str = ""
    committer: str = ""
    message: str = ""

    def encode(self) -> bytes:
        lines = [f"tree {self.tree}"]
        for p in self.parents:
            lines.append(f"parent {p}")
        lines.append(f"author {self.author}")
        lines.append(f"committer {self.committer}")
        header = "\n".join(lines) + "\n\n"
        return header.encode("utf-8") + self.message.encode("utf-8")


def parse_commit(data: bytes) -> Commit:
    text = data.decode("utf-8", errors="replace")
    head, _, msg = text.partition("\n\n")
    c = Commit(tree="", message=msg)
    for line in head.splitlines():
        if not line:
            continue
        key, _, val = line.partition(" ")
        if key == "tree":
            c.tree = val
        elif key == "parent":
            c.parents.append(val)
        elif key == "author":
            c.author = val
        elif key == "committer":
            c.committer = val
    return c


def format_signature(name: str, email: str, *, when: Optional[int] = None, tz_minutes: int = 0) -> str:
    import time
    if when is None:
        when = int(time.time())
    sign = "+" if tz_minutes >= 0 else "-"
    tz_abs = abs(tz_minutes)
    return f"{name} <{email}> {when} {sign}{tz_abs // 60:02d}{tz_abs % 60:02d}"


def _parse_date_env(value: str) -> Optional[tuple[int, int]]:
    """Parse a ``GIT_*_DATE`` value into ``(epoch_seconds, tz_minutes)``.

    Handles the raw ``<seconds> <±HHMM>`` and ``@<seconds>`` forms that Git
    emits and round-trips, plus common ISO-8601 spellings; returns None when
    the value cannot be parsed so the caller can fall back to the current time.
    """
    import re
    import datetime

    value = value.strip()
    if not value:
        return None
    m = re.match(r"^@?(-?\d+)\s+([+-])(\d{2})(\d{2})$", value)
    if m:
        secs = int(m.group(1))
        tzmin = (1 if m.group(2) == "+" else -1) * (int(m.group(3)) * 60 + int(m.group(4)))
        return secs, tzmin
    m = re.match(r"^@(-?\d+)$", value)
    if m:
        return int(m.group(1)), 0
    iso = value.replace("Z", "+00:00")
    for fmt in (
        None,  # datetime.fromisoformat
        "%Y-%m-%d %H:%M:%S %z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            dt = datetime.datetime.fromisoformat(iso) if fmt is None else datetime.datetime.strptime(value, fmt)
        except ValueError:
            continue
        off = dt.utcoffset()
        tzmin = int(off.total_seconds()) // 60 if off is not None else 0
        return int(dt.timestamp()), tzmin

    # RFC2822 / abbreviated-month forms (git's date.c parse_date accepts these,
    # e.g. "Mon, 3 Jul 2006 17:18:43 +0200" and the weekday/comma-less variants
    # "Jul 3 17:18:43 2006 +0200", "3 Jul 2006 17:18:43 +0200"). Pull off an
    # explicit ±HHMM offset and parse the remainder against month-name formats,
    # treating the parsed wall-clock as being in that offset to recover the
    # UTC epoch (matching parse_date_basic's tm_to_time_t over the named tz).
    mtz = re.search(r"\s([+-])(\d{2})(\d{2})\s*$", value)
    explicit_off = None
    body = value
    if mtz:
        explicit_off = (1 if mtz.group(1) == "+" else -1) * (
            int(mtz.group(2)) * 60 + int(mtz.group(3)))
        body = value[:mtz.start()].strip()
    body = body.rstrip(",").strip()
    # Drop a leading weekday token ("Mon" / "Mon,") — git ignores it. Only strip
    # actual weekday names so a leading month ("Jul 3 ...") is left intact.
    body = re.sub(r"^(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),?\s+", "", body,
                  flags=re.IGNORECASE)
    naive_fmts = (
        "%d %b %Y %H:%M:%S",   # 3 Jul 2006 17:18:43
        "%b %d %H:%M:%S %Y",   # Jul 3 17:18:43 2006
        "%b %d %Y %H:%M:%S",   # Jul 3 2006 17:18:43
        "%Y-%m-%d %H:%M:%S",
    )
    for fmt in naive_fmts:
        try:
            dt = datetime.datetime.strptime(body, fmt)
        except ValueError:
            continue
        if explicit_off is not None:
            # Interpret the wall-clock as offset-local; epoch = UTC seconds.
            dt = dt.replace(tzinfo=datetime.timezone(
                datetime.timedelta(minutes=explicit_off)))
            return int(dt.timestamp()), explicit_off
        # No explicit tz: treat as local time (git uses the local zone).
        secs = int(dt.replace(
            tzinfo=datetime.datetime.now().astimezone().tzinfo).timestamp())
        return secs, _local_tz_minutes(secs)
    return None


def _local_tz_minutes(when: int) -> int:
    import datetime
    try:
        local = datetime.datetime.fromtimestamp(when).astimezone()
        off = local.utcoffset()
        return int(off.total_seconds()) // 60 if off is not None else 0
    except (OverflowError, OSError, ValueError):
        return 0


def _system_ident() -> tuple[str, str]:
    """Fallback identity matching C Git when nothing is configured: the GECOS
    full name and ``<user>@<fqdn>`` (ident.c). Falls back to a generic value
    only if the host lookup fails entirely."""
    name = "pythongit"
    email = "pythongit@example.invalid"
    try:
        import pwd
        pw = pwd.getpwuid(os.getuid())
        gecos = (pw.pw_gecos or "").split(",", 1)[0].strip()
        name = gecos or pw.pw_name or name
        import socket
        host = socket.getfqdn()
        email = f"{pw.pw_name}@{host}" if host else f"{pw.pw_name}@{socket.gethostname()}"
    except Exception:
        pass
    return name, email


def build_signature(repo: Repository, role: str, date_override: Optional[str] = None) -> str:
    """Build an ``author`` or ``committer`` signature line.

    Identity precedence matches C Git: the role-specific ``GIT_*`` env vars,
    then ``user.name`` / ``user.email`` (``EMAIL`` for the address), then a
    generic fallback. The date honors an explicit ``date_override`` (as from
    ``commit --date``), then ``GIT_AUTHOR_DATE`` / ``GIT_COMMITTER_DATE``, and
    otherwise uses the current local time with the local UTC offset.
    """
    import time
    from . import gitconfig

    cfg_name = gitconfig.get(repo, "user.name")
    cfg_email = gitconfig.get(repo, "user.email")
    if role == "author":
        name = os.environ.get("GIT_AUTHOR_NAME") or cfg_name
        email = os.environ.get("GIT_AUTHOR_EMAIL") or os.environ.get("EMAIL") or cfg_email
        date = os.environ.get("GIT_AUTHOR_DATE")
    else:
        name = os.environ.get("GIT_COMMITTER_NAME") or cfg_name
        email = os.environ.get("GIT_COMMITTER_EMAIL") or os.environ.get("EMAIL") or cfg_email
        date = os.environ.get("GIT_COMMITTER_DATE")
    if date_override is not None:
        date = date_override
    sys_name, sys_email = _system_ident()
    name = name or sys_name
    email = email or sys_email
    parsed = _parse_date_env(date) if date else None
    if parsed is not None:
        secs, tzmin = parsed
    else:
        secs = int(time.time())
        tzmin = _local_tz_minutes(secs)
    return format_signature(name, email, when=secs, tz_minutes=tzmin)
