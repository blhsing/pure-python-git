"""Smart HTTPS git protocol v1 — clone (fetch) only.

Sequence:
  GET  $URL/info/refs?service=git-upload-pack
       -> pkt-line list of refs and capabilities
  POST $URL/git-upload-pack
       -> body: pkt-line wants + flush + done
       <- side-band-64k stream: NAK then packfile bytes on channel 1.
"""
from __future__ import annotations

import http.client
import os
from pathlib import Path
import tempfile
import urllib.parse
import urllib.request
from typing import Iterator

from .repo import Repository


def _pkt_line(payload: bytes) -> bytes:
    if payload is None:
        return b"0000"
    n = len(payload) + 4
    return f"{n:04x}".encode() + payload


def _flush() -> bytes:
    return b"0000"


def _iter_pkt(buf: bytes) -> Iterator[bytes]:
    pos = 0
    while pos < len(buf):
        hdr = buf[pos : pos + 4]
        if len(hdr) < 4:
            return
        n = int(hdr.decode(), 16)
        if n == 0:
            pos += 4
            yield b""
            continue
        yield buf[pos + 4 : pos + n]
        pos += n


def _iter_pkt_stream(source) -> Iterator[bytes]:
    while True:
        hdr = source.read(4)
        if not hdr:
            return
        if len(hdr) < 4:
            raise RuntimeError("truncated pkt-line header")
        n = int(hdr.decode(), 16)
        if n == 0:
            yield b""
            continue
        payload = source.read(n - 4)
        if len(payload) != n - 4:
            raise RuntimeError("truncated pkt-line payload")
        yield payload


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "pythongit/0.1"})
    with urllib.request.urlopen(req) as r:
        return r.read()


def _post(url: str, body: bytes, content_type: str, accept: str) -> bytes:
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "User-Agent": "pythongit/0.1",
            "Content-Type": content_type,
            "Accept": accept,
        },
    )
    with urllib.request.urlopen(req) as r:
        return r.read()


def _post_stream(url: str, body: bytes, content_type: str, accept: str):
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "User-Agent": "pythongit/0.1",
            "Content-Type": content_type,
            "Accept": accept,
        },
    )
    return urllib.request.urlopen(req)


def _post_with_pack_file(
    url: str,
    prefix: bytes,
    pack_path: Path,
    content_type: str,
    accept: str,
) -> bytes:
    parsed = urllib.parse.urlsplit(url)
    conn_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    host = parsed.hostname or ""
    if parsed.port:
        host_header = f"{host}:{parsed.port}"
    else:
        host_header = host
    target = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    pack_size = pack_path.stat().st_size
    conn = conn_cls(host, parsed.port)
    try:
        conn.putrequest("POST", target)
        conn.putheader("Host", host_header)
        conn.putheader("User-Agent", "pythongit/0.1")
        conn.putheader("Content-Type", content_type)
        conn.putheader("Accept", accept)
        conn.putheader("Content-Length", str(len(prefix) + pack_size))
        conn.endheaders()
        if prefix:
            conn.send(prefix)
        with pack_path.open("rb") as fh:
            while True:
                chunk = fh.read(256 * 1024)
                if not chunk:
                    break
                conn.send(chunk)
        resp = conn.getresponse()
        data = resp.read()
        if resp.status >= 400:
            raise RuntimeError(f"HTTP {resp.status}: {data.decode(errors='replace')}")
        return data
    finally:
        conn.close()


def discover_refs(base_url: str) -> dict[str, str]:
    url = base_url.rstrip("/") + "/info/refs?service=git-upload-pack"
    body = _get(url)
    refs: dict[str, str] = {}
    head_target: str = ""
    first = True
    for pkt in _iter_pkt(body):
        if not pkt:
            continue
        line = pkt.rstrip(b"\n")
        if line.startswith(b"#"):
            continue  # service announcement
        if first:
            first = False
            # first ref line carries capabilities after a NUL
            nul = line.find(b"\0")
            if nul != -1:
                payload = line[:nul]
            else:
                payload = line
            sha, _, name = payload.partition(b" ")
            refs[name.decode()] = sha.decode()
        else:
            sha, _, name = line.partition(b" ")
            if sha and name:
                refs[name.decode()] = sha.decode()
    return refs


def _fetch_pack_request_body(wants: list[str], haves: list[str] | None = None) -> bytes:
    caps = "multi_ack_detailed no-done side-band-64k thin-pack ofs-delta agent=pythongit/0.1"
    if any(len(w) == 64 for w in wants):
        caps += " object-format=sha256"
    body = bytearray()
    for i, w in enumerate(wants):
        line = f"want {w}"
        if i == 0:
            line += " " + caps
        body += _pkt_line((line + "\n").encode())
    body += _flush()
    for h in (haves or []):
        body += _pkt_line(f"have {h}\n".encode())
    body += _pkt_line(b"done\n")
    return bytes(body)


def fetch_pack_to_file(base_url: str, wants: list[str], pack_path: Path, haves: list[str] | None = None) -> int:
    """Negotiate a fetch and stream side-band channel 1 into ``pack_path``."""
    body = _fetch_pack_request_body(wants, haves)
    url = base_url.rstrip("/") + "/git-upload-pack"
    written = 0
    with _post_stream(
        url,
        body,
        "application/x-git-upload-pack-request",
        "application/x-git-upload-pack-result",
    ) as resp:
        with Path(pack_path).open("wb") as out:
            for pkt in _iter_pkt_stream(resp):
                if not pkt:
                    continue
                if pkt.startswith(b"NAK") or pkt.startswith(b"ACK"):
                    continue
                ch = pkt[0]
                data = pkt[1:]
                if ch == 1:
                    out.write(data)
                    written += len(data)
                elif ch == 2:
                    pass
                elif ch == 3:
                    raise RuntimeError("remote error: " + data.decode(errors="replace"))
    return written


def fetch_pack(base_url: str, wants: list[str], haves: list[str] | None = None) -> bytes:
    """Negotiate a fetch, return raw pack bytes. Single round, no multi_ack."""
    with tempfile.NamedTemporaryFile(prefix="pygit-fetch-", suffix=".pack", delete=False) as tmp:
        tmp_name = tmp.name
    try:
        fetch_pack_to_file(base_url, wants, Path(tmp_name), haves=haves)
        return Path(tmp_name).read_bytes()
    finally:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass


def clone(url: str, target_dir: str, object_format: str | None = None) -> Repository:
    import shutil
    import tempfile
    from . import pack as pack_mod
    from . import refs as refs_mod

    remote_refs = discover_refs(url)
    if not remote_refs:
        raise RuntimeError("no refs from remote")
    source_format = "sha256" if any(len(s) == 64 for s in remote_refs.values()) else "sha1"
    target_format = object_format or source_format
    repo = Repository.init(target_dir, object_format=target_format)
    wants = sorted(set(remote_refs.values()))
    unpack_repo = repo
    tmp_dir = None
    tmp_pack = None
    if target_format != source_format:
        tmp_dir = tempfile.mkdtemp(prefix="pygit-translate-")
        unpack_repo = Repository.init(tmp_dir, object_format=source_format)
    try:
        with tempfile.NamedTemporaryFile(prefix="pygit-fetch-", suffix=".pack", delete=False) as tmp:
            tmp_pack = tmp.name
        fetch_pack_to_file(url, wants, Path(tmp_pack))
        pack_mod.install_pack_file(unpack_repo, Path(tmp_pack))
        tmp_pack = None
    finally:
        if tmp_pack:
            try:
                os.unlink(tmp_pack)
            except OSError:
                pass
            try:
                os.unlink(str(Path(tmp_pack).with_suffix(".idx")))
            except OSError:
                pass

    # set up refs and HEAD
    for name, sha in remote_refs.items():
        if name == "HEAD":
            continue
        if name.startswith("refs/heads/"):
            # mirror as remote tracking refs/remotes/origin/<branch>
            branch = name[len("refs/heads/") :]
            refs_mod.update_ref(unpack_repo, f"refs/remotes/origin/{branch}", sha)
            refs_mod.update_ref(unpack_repo, name, sha)
        elif name.startswith("refs/tags/"):
            refs_mod.update_ref(unpack_repo, name, sha)

    # pick a default branch
    chosen = None
    for cand in ("refs/heads/main", "refs/heads/master"):
        if cand in remote_refs:
            chosen = cand
            break
    if chosen is None:
        for n in remote_refs:
            if n.startswith("refs/heads/"):
                chosen = n
                break
    if chosen:
        refs_mod.set_head(unpack_repo, chosen)

    if unpack_repo is not repo:
        try:
            from . import translate
            translate.translate_repository(unpack_repo, repo, checkout=True)
        finally:
            if tmp_dir:
                shutil.rmtree(tmp_dir, ignore_errors=True)
    elif chosen:
        # checkout
        from . import workdir
        sha = refs_mod.read_ref(repo, chosen)
        if sha:
            from . import objects as objs
            t, data = objs.read_object(repo, sha)
            if t == "commit":
                commit = objs.parse_commit(data)
                workdir.checkout_tree(repo, commit.tree)

    # save remote
    cfg_path = repo.gitdir / "config"
    cfg = cfg_path.read_text(encoding="utf-8")
    cfg += f'\n[remote "origin"]\n\turl = {url}\n\tfetch = +refs/heads/*:refs/remotes/origin/*\n'
    cfg_path.write_text(cfg, encoding="utf-8")
    return repo


# ---------------------------------------------------------------------------
# fetch — like clone, but updates remote-tracking refs only.


def fetch(repo: Repository, remote: str = "origin") -> dict[str, str]:
    from . import pack as pack_mod
    from . import refs as refs_mod
    cp = repo.config()
    sect = f'remote "{remote}"'
    if not cp.has_section(sect):
        raise RuntimeError(f"remote {remote!r} not configured")
    url = cp.get(sect, "url")
    remote_refs = discover_refs(url)
    # known objects to use as haves
    haves: list[str] = []
    for b in refs_mod.list_branches(repo):
        s = refs_mod.read_ref(repo, f"refs/heads/{b}")
        if s:
            haves.append(s)
    wants = sorted(set(remote_refs.values()) - set(haves))
    updated: dict[str, str] = {}
    if wants:
        tmp_pack = None
        try:
            with tempfile.NamedTemporaryFile(prefix="pygit-fetch-", suffix=".pack", delete=False) as tmp:
                tmp_pack = tmp.name
            fetch_pack_to_file(url, wants, Path(tmp_pack), haves=haves)
            pack_mod.install_pack_file(repo, Path(tmp_pack))
            tmp_pack = None
        finally:
            if tmp_pack:
                try:
                    os.unlink(tmp_pack)
                except OSError:
                    pass
                try:
                    os.unlink(str(Path(tmp_pack).with_suffix(".idx")))
                except OSError:
                    pass
    for name, sha in remote_refs.items():
        if not name.startswith("refs/heads/"):
            continue
        branch = name[len("refs/heads/"):]
        ref = f"refs/remotes/{remote}/{branch}"
        cur = refs_mod.read_ref(repo, ref)
        if cur != sha:
            refs_mod.update_ref(repo, ref, sha, message=f"fetch {remote}")
            updated[ref] = sha
    return updated


# ---------------------------------------------------------------------------
# push — receive-pack over HTTPS.


def _build_pack(repo: Repository, shas: list[str]) -> bytes:
    """Build a pack (with deltas) — thin wrapper over pack.build_pack."""
    from . import pack as _p
    raw, _ = _p.build_pack(repo, shas)
    return raw


def _collect_objects(repo: Repository, tip: str, stop_at: set[str]) -> list[str]:
    """Return all objects reachable from tip but not from stop_at."""
    from . import objects as objs
    if not stop_at:
        try:
            from . import pack as pack_mod

            bitmapped = pack_mod.reachable_from_bitmaps(repo, [tip])
            if bitmapped is not None:
                return list(bitmapped)
        except Exception:
            pass
    try:
        from . import commitgraph

        graph = commitgraph.read_commit_graph(repo)
    except Exception:
        graph = None
    seen: set[str] = set()
    out: list[str] = []
    stack = [tip]
    while stack:
        sha = stack.pop()
        if sha in seen or sha in stop_at:
            continue
        seen.add(sha)
        if graph is not None:
            entry = graph.get(sha)
            if entry is not None:
                out.append(sha)
                stack.append(entry.tree)
                stack.extend(entry.parents)
                continue
        try:
            t, data = objs.read_object(repo, sha)
        except KeyError:
            continue
        out.append(sha)
        if t == "commit":
            c = objs.parse_commit(data)
            stack.append(c.tree)
            stack.extend(c.parents)
        elif t == "tree":
            for e in objs.parse_tree(data, repo.hash_len):
                stack.append(e.sha)
        elif t == "tag":
            # parse target line
            for line in data.decode("utf-8", errors="replace").splitlines():
                if line.startswith("object "):
                    stack.append(line[len("object "):].strip())
                    break
    return out


def push(repo: Repository, remote: str = "origin", refspecs: list[str] | None = None) -> dict[str, str]:
    from . import refs as refs_mod
    cp = repo.config()
    sect = f'remote "{remote}"'
    if not cp.has_section(sect):
        raise RuntimeError(f"remote {remote!r} not configured")
    url = cp.get(sect, "url")
    # discover refs via receive-pack
    info_url = url.rstrip("/") + "/info/refs?service=git-receive-pack"
    raw = _get(info_url)
    remote_refs: dict[str, str] = {}
    first = True
    for pkt in _iter_pkt(raw):
        if not pkt or pkt.startswith(b"#"):
            continue
        line = pkt.rstrip(b"\n")
        if first:
            first = False
            nul = line.find(b"\0")
            line = line[:nul] if nul != -1 else line
        sha, _, name = line.partition(b" ")
        if sha and name:
            remote_refs[name.decode()] = sha.decode()

    head_sym, head_sha = refs_mod.read_head(repo)
    if refspecs is None:
        if head_sym:
            refspecs = [head_sym]
        else:
            return {}

    caps = "report-status side-band-64k agent=pythongit/0.1"
    if repo.object_format() == "sha256":
        caps += " object-format=sha256"
    commands = bytearray()
    new_shas: list[str] = []
    stop = set(remote_refs.values())
    for i, rs in enumerate(refspecs):
        local = rs
        remote_ref = rs
        if ":" in rs:
            local, remote_ref = rs.split(":", 1)
        sha = refs_mod.rev_parse(repo, local) or refs_mod.read_ref(repo, local)
        if not sha:
            continue
        old = remote_refs.get(remote_ref, repo.null_oid())
        if old == sha:
            continue
        line = f"{old} {sha} {remote_ref}"
        if i == 0:
            line += "\0" + caps
        commands += _pkt_line((line + "\n").encode())
        new_shas.append(sha)
    if not commands:
        return {}
    commands += _flush()
    objects = []
    seen = set()
    for s in new_shas:
        for o in _collect_objects(repo, s, stop):
            if o not in seen:
                seen.add(o)
                objects.append(o)
    url_post = url.rstrip("/") + "/git-receive-pack"
    from . import pack as pack_mod
    tmp_name = None
    try:
        with tempfile.NamedTemporaryFile(prefix="pygit-push-", suffix=".pack", delete=False) as tmp:
            tmp_name = tmp.name
        pack_mod.write_pack_stream(repo, objects, Path(tmp_name))
        resp = _post_with_pack_file(
            url_post,
            bytes(commands),
            Path(tmp_name),
            "application/x-git-receive-pack-request",
            "application/x-git-receive-pack-result",
        )
    finally:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
    results: dict[str, str] = {}
    for pkt in _iter_pkt(resp):
        if not pkt:
            continue
        # side-band channels in response; ignore channel byte
        line = pkt
        if line[:1] in (b"\x01", b"\x02", b"\x03"):
            line = line[1:]
        text = line.decode(errors="replace").rstrip("\n")
        if text.startswith("ok ") or text.startswith("ng "):
            kind, _, name = text.partition(" ")
            results[name] = kind
    return results
