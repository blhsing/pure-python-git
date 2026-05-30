"""Implementations for the external-system bridges:
  send-email (smtplib), difftool / mergetool (shell out to configured tool),
  daemon (TCP git:// server), http-backend (CGI for smart HTTP),
  http-fetch (dumb HTTP fetch), instaweb (basic browser),
  credential-store, maintenance (dispatcher), shell (restricted dispatcher),
  remote-helper / remote-ext / remote-fd (plug-in protocols),
  fsmonitor (native or polling watcher), cvsexportcommit / cvsimport / cvsserver / svn
  (shell out to cvs/svn binaries), gitk / gui / gitweb (tk + http.server).

These mirror what the C implementations do — most just orchestrate other
binaries or talk a documented wire protocol.
"""
from __future__ import annotations

import io
import os
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.parse
from pathlib import Path
from typing import Iterable, Optional

from . import objects as objs
from . import pack as pack_mod
from . import refs as refs_mod
from .repo import Repository


# ---------------------------------------------------------------------------
# send-email — read an mbox of patches and SMTP them out.


def credential_fill(fields: dict[str, str], *, use_external: bool = True) -> dict[str, str]:
    """Resolve credentials from env, ~/.git-credentials, then configured helpers."""
    out = dict(fields)
    env_user = os.environ.get("GIT_USERNAME") or ""
    env_pass = os.environ.get("GIT_PASSWORD") or os.environ.get("GIT_SMTP_PASSWORD") or ""
    if env_user and not out.get("username"):
        out["username"] = env_user
    if env_pass and not out.get("password"):
        out["password"] = env_pass
    stored = credential_store("get", out)
    for key in ("username", "password"):
        if stored.get(key) and not out.get(key):
            out[key] = stored[key]
    if use_external and (not out.get("username") or not out.get("password")):
        payload = "".join(f"{k}={v}\n" for k, v in out.items() if v) + "\n"
        try:
            proc = subprocess.run(
                ["git", "credential", "fill"],
                input=payload,
                text=True,
                capture_output=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            proc = None
        if proc is not None and proc.returncode == 0:
            for line in proc.stdout.splitlines():
                if "=" in line:
                    key, _, val = line.partition("=")
                    if val and not out.get(key):
                        out[key] = val
    return out


def send_email(mbox: str, *, to: list[str], from_addr: Optional[str] = None,
               smtp_host: str = "localhost", smtp_port: int = 25,
               smtp_user: Optional[str] = None, smtp_pass: Optional[str] = None,
               smtp_encryption: Optional[str] = None,
               smtp_ssl_cert_path: Optional[str] = None,
               smtp_auth: str = "plain",
               smtp_oauth2_token: Optional[str] = None,
               use_credential_helpers: bool = True) -> int:
    import base64
    import smtplib
    import ssl
    from email.message import EmailMessage
    encryption = (smtp_encryption or "auto").lower()
    auth = (smtp_auth or "plain").lower()
    if encryption == "tls":
        encryption = "starttls"
    context = None
    if encryption in ("ssl", "starttls"):
        context = ssl.create_default_context()
        if smtp_ssl_cert_path:
            path = Path(smtp_ssl_cert_path)
            if path.is_dir():
                context.load_verify_locations(capath=str(path))
            else:
                context.load_verify_locations(cafile=str(path))
    text = Path(mbox).read_text(encoding="utf-8", errors="replace")
    pieces: list[list[str]] = []
    cur: list[str] = []
    for line in text.splitlines():
        if line.startswith("From ") and cur:
            pieces.append(cur)
            cur = [line]
        else:
            cur.append(line)
    if cur:
        pieces.append(cur)
    smtp_cls = smtplib.SMTP_SSL if encryption == "ssl" else smtplib.SMTP
    kwargs = {"context": context} if encryption == "ssl" and context is not None else {}
    with smtp_cls(smtp_host, smtp_port, **kwargs) as s:
        if encryption == "starttls":
            s.starttls(context=context)
        elif encryption == "auto" and smtp_user:
            s.starttls()
        should_fill = use_credential_helpers and (smtp_user or smtp_pass or auth in ("xoauth2", "oauth2") or smtp_host != "localhost")
        if should_fill and (not smtp_user or not smtp_pass):
            creds = credential_fill(
                {"protocol": "smtp", "host": smtp_host, "username": smtp_user or ""},
                use_external=use_credential_helpers,
            )
            smtp_user = smtp_user or creds.get("username")
            smtp_pass = smtp_pass or creds.get("password")
        if auth in ("xoauth2", "oauth2"):
            token = smtp_oauth2_token or smtp_pass or ""
            if smtp_user and token:
                raw = f"user={smtp_user}\x01auth=Bearer {token}\x01\x01".encode()
                code, resp = s.docmd("AUTH", "XOAUTH2 " + base64.b64encode(raw).decode("ascii"))
                if code >= 400:
                    raise RuntimeError(f"SMTP XOAUTH2 failed: {code} {resp!r}")
        elif smtp_user:
            s.login(smtp_user, smtp_pass or "")
        for piece in pieces:
            try:
                blank = piece.index("")
            except ValueError:
                continue
            headers_raw = piece[:blank]
            body = "\n".join(piece[blank + 1 :])
            msg = EmailMessage()
            subject = ""
            author = from_addr or "unknown@example.invalid"
            for h in headers_raw:
                if h.startswith("Subject: "):
                    subject = h[len("Subject: "):]
                elif h.startswith("From: ") and not from_addr:
                    author = h[len("From: "):]
            msg["Subject"] = subject
            msg["From"] = author
            msg["To"] = ", ".join(to)
            msg.set_content(body)
            s.send_message(msg)
            print(f"Sent: {subject}")
    return 0


# ---------------------------------------------------------------------------
# difftool / mergetool — invoke a configured external tool.


def run_difftool(repo: Repository, tool: Optional[str]) -> int:
    tool = tool or repo.config().get("diff", "tool", fallback="vimdiff")
    # iterate diff between worktree and index, calling tool for each pair
    from .index import read_index
    idx = read_index(repo)
    rc = 0
    for e in idx.entries:
        full = repo.path / e.path
        if not full.exists():
            continue
        data = full.read_bytes()
        sha, _ = objs.hash_bytes("blob", data, repo)
        if sha == e.sha:
            continue
        # write a temp file for the staged version
        import tempfile
        _, idx_blob = objs.read_object(repo, e.sha)
        with tempfile.NamedTemporaryFile(delete=False, suffix="." + Path(e.path).suffix) as tf:
            tf.write(idx_blob)
            staged_path = tf.name
        try:
            rc = subprocess.call([tool, staged_path, str(full)])
        except FileNotFoundError:
            print(f"difftool '{tool}' not installed; skipping {e.path}", file=sys.stderr)
            rc = 1
        finally:
            os.unlink(staged_path)
    return rc


def run_mergetool(repo: Repository, tool: Optional[str], paths: list[str]) -> int:
    tool = tool or repo.config().get("merge", "tool", fallback="vimdiff")
    for p in paths:
        full = repo.path / p
        if not full.exists():
            continue
        # If the file contains conflict markers, invoke the tool on it.
        text = full.read_text(encoding="utf-8", errors="replace")
        if "<<<<<<<" not in text:
            continue
        try:
            subprocess.call([tool, str(full)])
        except FileNotFoundError:
            print(f"mergetool '{tool}' not installed", file=sys.stderr)
            return 1
    return 0


# ---------------------------------------------------------------------------
# credential-store — plain text ~/.git-credentials


def credential_store(op: str, fields: dict[str, str]) -> dict[str, str]:
    path = Path.home() / ".git-credentials"
    entries: list[dict[str, str]] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            # entries are URLs like https://user:pass@host/path
            parsed = urllib.parse.urlparse(line.strip())
            if parsed.username:
                entries.append({
                    "protocol": parsed.scheme,
                    "host": parsed.hostname or "",
                    "username": parsed.username,
                    "password": parsed.password or "",
                })
    if op == "get":
        for e in entries:
            if all(e.get(k) == fields.get(k) for k in ("protocol", "host") if k in fields):
                return e
        return {}
    if op == "store":
        # add or update
        for e in entries:
            if e["protocol"] == fields.get("protocol") and e["host"] == fields.get("host"):
                e["username"] = fields.get("username", e["username"])
                e["password"] = fields.get("password", e["password"])
                break
        else:
            entries.append(fields)
    elif op == "erase":
        entries = [e for e in entries if not (e["protocol"] == fields.get("protocol") and e["host"] == fields.get("host"))]
    out = []
    for e in entries:
        url = f"{e['protocol']}://{urllib.parse.quote(e['username'])}:{urllib.parse.quote(e['password'])}@{e['host']}"
        out.append(url)
    path.write_text("\n".join(out) + ("\n" if out else ""), encoding="utf-8")
    return {}


# ---------------------------------------------------------------------------
# daemon — git:// protocol server.


def _pkt(b: bytes) -> bytes:
    return f"{len(b) + 4:04x}".encode() + b


def _pkt_chunks(channel: bytes, data: bytes, limit: int = 65500):
    max_payload = limit - len(channel)
    for i in range(0, len(data), max_payload):
        yield _pkt(channel + data[i : i + max_payload])


def _read_pkt(sock: socket.socket) -> Optional[bytes]:
    hdr = b""
    while len(hdr) < 4:
        chunk = sock.recv(4 - len(hdr))
        if not chunk:
            return None
        hdr += chunk
    n = int(hdr.decode(), 16)
    if n == 0:
        return b""
    body = b""
    while len(body) < n - 4:
        chunk = sock.recv(n - 4 - len(body))
        if not chunk:
            break
        body += chunk
    return body


def daemon_serve(base_path: str, host: str = "127.0.0.1", port: int = 9418) -> int:
    base = Path(base_path).resolve()

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            req = _read_pkt(self.request)
            if not req:
                return
            # request looks like: "git-upload-pack /path\0host=...\0"
            text = req.rstrip(b"\0").decode("utf-8", errors="replace")
            cmd, _, rest = text.partition(" ")
            path = rest.split("\0", 1)[0]
            repo_path = base / path.lstrip("/")
            try:
                repo = Repository.discover(str(repo_path))
            except Exception:
                self.request.sendall(_pkt(b"ERR no such repository\n"))
                return
            if cmd == "git-upload-pack":
                self._upload_pack(repo)
            elif cmd == "git-receive-pack":
                self._receive_pack(repo)
            else:
                self.request.sendall(_pkt(f"ERR unknown command {cmd}\n".encode()))

        def _upload_pack(self, repo: Repository):
            # list refs
            caps = b"side-band-64k ofs-delta agent=pythongit/0.1"
            if repo.object_format() == "sha256":
                caps += b" object-format=sha256"
            first = True
            for kind in ("refs/heads", "refs/tags"):
                root = repo.gitdir / kind
                if root.exists():
                    for f in sorted(root.rglob("*")):
                        if f.is_file():
                            rel = str(f.relative_to(repo.gitdir)).replace(os.sep, "/")
                            s = refs_mod.read_ref(repo, rel)
                            if s:
                                line = f"{s} {rel}".encode()
                                if first:
                                    first = False
                                    line += b"\0" + caps
                                self.request.sendall(_pkt(line + b"\n"))
            self.request.sendall(b"0000")
            # read wants
            wants = []
            while True:
                pkt = _read_pkt(self.request)
                if pkt is None or pkt == b"":
                    break
                if pkt.startswith(b"want "):
                    wants.append(pkt[5:].decode().split()[0])
            if wants:
                # send NAK + pack on side-band
                self.request.sendall(_pkt(b"NAK\n"))
                shas: list[str] = []
                seen_shas: set[str] = set()
                from .protocol import _collect_objects
                for w in wants:
                    for o in _collect_objects(repo, w, set()):
                        if o not in seen_shas:
                            shas.append(o)
                            seen_shas.add(o)
                def send_pack_data(data: bytes) -> None:
                    for pkt in _pkt_chunks(b"\x01", data):
                        self.request.sendall(pkt)

                pack_mod.write_pack_stream_to(repo, shas, send_pack_data, collect_entries=False)
                self.request.sendall(b"0000")

        def _receive_pack(self, repo: Repository):
            # advertise refs
            caps = b"report-status agent=pythongit/0.1"
            if repo.object_format() == "sha256":
                caps += b" object-format=sha256"
            first = True
            for kind in ("refs/heads", "refs/tags"):
                root = repo.gitdir / kind
                if root.exists():
                    for f in sorted(root.rglob("*")):
                        if f.is_file():
                            rel = str(f.relative_to(repo.gitdir)).replace(os.sep, "/")
                            s = refs_mod.read_ref(repo, rel)
                            if s:
                                line = f"{s} {rel}".encode()
                                if first:
                                    first = False
                                    line += b"\0" + caps
                                self.request.sendall(_pkt(line + b"\n"))
            self.request.sendall(b"0000")
            # read commands
            updates: list[tuple[str, str, str]] = []
            while True:
                pkt = _read_pkt(self.request)
                if pkt is None or pkt == b"":
                    break
                line = pkt.rstrip(b"\n").split(b"\0", 1)[0].decode()
                parts = line.split(" ")
                if len(parts) >= 3:
                    updates.append((parts[0], parts[1], parts[2]))
            # read pack from remaining stream
            tmp_pack = None
            pack_size = 0
            with tempfile.NamedTemporaryFile(prefix="pygit-receive-", suffix=".pack", delete=False) as tmp:
                tmp_pack = tmp.name
                while True:
                    chunk = self.request.recv(65536)
                    if not chunk:
                        break
                    tmp.write(chunk)
                    pack_size += len(chunk)
            if pack_size:
                try:
                    pack_mod.install_pack_file(repo, Path(tmp_pack))
                    tmp_pack = None
                except Exception:
                    pass
            if tmp_pack:
                Path(tmp_pack).unlink(missing_ok=True)
                Path(tmp_pack).with_suffix(".idx").unlink(missing_ok=True)
            self.request.sendall(_pkt(b"unpack ok\n"))
            for old, new, ref in updates:
                try:
                    refs_mod.update_ref(repo, ref, new)
                    self.request.sendall(_pkt(f"ok {ref}\n".encode()))
                except Exception as e:
                    self.request.sendall(_pkt(f"ng {ref} {e}\n".encode()))
            self.request.sendall(b"0000")

    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer((host, port), Handler) as srv:
        print(f"daemon listening on {host}:{port}, root={base}")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


# ---------------------------------------------------------------------------
# http-fetch — dumb HTTP fetch one object at a time.


def http_fetch(url: str, sha: str, repo: Repository) -> int:
    obj_url = url.rstrip("/") + f"/objects/{sha[:2]}/{sha[2:]}"
    req = urllib.request.Request(obj_url, headers={"User-Agent": "pythongit/0.1"})
    with urllib.request.urlopen(req) as r:
        raw = r.read()
    out = repo.gitdir / "objects" / sha[:2] / sha[2:]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(raw)
    return 0


# ---------------------------------------------------------------------------
# http-backend — CGI-style handler producing smart HTTP responses.
# We expose a thin in-process function used by instaweb.


def _repo_from_smart_http_path(path: str, base: Path):
    for ep in ("/info/refs", "/git-upload-pack", "/git-receive-pack"):
        if ep in path:
            repo_path = path.split(ep)[0].lstrip("/")
            break
    else:
        return None
    try:
        return Repository.discover(str(base / repo_path))
    except Exception:
        return None


def _parse_upload_pack_wants(body: bytes) -> list[str]:
    wants = []
    i = 0
    while i < len(body):
        n = int(body[i : i + 4].decode(), 16)
        i += 4
        if n == 0:
            continue
        line = body[i : i + n - 4].decode("utf-8", errors="replace").rstrip("\n")
        i += n - 4
        if line.startswith("want "):
            wants.append(line[5:].split()[0])
    return wants


def _objects_for_wants(repo: Repository, wants: list[str]) -> list[str]:
    from .protocol import _collect_objects

    shas: list[str] = []
    seen_shas: set[str] = set()
    for w in wants:
        for obj in _collect_objects(repo, w, set()):
            if obj not in seen_shas:
                shas.append(obj)
                seen_shas.add(obj)
    return shas


def _upload_pack_response_iter(repo: Repository, shas: list[str]) -> Iterable[bytes]:
    import tempfile

    tmp_name = None
    try:
        with tempfile.NamedTemporaryFile(prefix="pygit-upload-", suffix=".pack", delete=False) as tmp:
            tmp_name = tmp.name
        pack_mod.write_pack_stream(repo, shas, Path(tmp_name))
        yield _pkt(b"NAK\n")
        with open(tmp_name, "rb") as fh:
            while True:
                chunk = fh.read(65500 - 1)
                if not chunk:
                    break
                yield _pkt(b"\x01" + chunk)
        yield b"0000"
    finally:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def _receive_pack_report(repo: Repository, updates: list[tuple[str, str, str]], pack_path: Optional[Path]) -> bytes:
    if pack_path is not None:
        try:
            pack_mod.install_pack_file(repo, pack_path)
            pack_path = None
        except Exception:
            pass
    if pack_path is not None:
        pack_path.unlink(missing_ok=True)
        pack_path.with_suffix(".idx").unlink(missing_ok=True)
    out = bytearray()
    out += _pkt(b"unpack ok\n")
    for old, new, ref in updates:
        try:
            refs_mod.update_ref(repo, ref, new)
            out += _pkt(f"ok {ref}\n".encode())
        except Exception as e:
            out += _pkt(f"ng {ref} {e}\n".encode())
    out += b"0000"
    return bytes(out)


def _receive_pack_body(repo: Repository, body: bytes) -> bytes:
    i = 0
    updates = []
    while i < len(body):
        n = int(body[i : i + 4].decode(), 16)
        i += 4
        if n == 0:
            break
        line = body[i : i + n - 4].decode("utf-8", errors="replace").rstrip("\n")
        i += n - 4
        line = line.split("\0", 1)[0]
        parts = line.split(" ")
        if len(parts) >= 3:
            updates.append((parts[0], parts[1], parts[2]))
    pack_path = None
    if i < len(body):
        with tempfile.NamedTemporaryFile(prefix="pygit-receive-", suffix=".pack", delete=False) as tmp:
            tmp.write(body[i:])
            pack_path = Path(tmp.name)
    return _receive_pack_report(repo, updates, pack_path)


def http_backend_receive_pack_stream(path: str, source, length: int, base: Path) -> tuple[int, dict[str, str], bytes]:
    clean_path = path.split("?", 1)[0]
    repo = _repo_from_smart_http_path(clean_path, base)
    if repo is None:
        return 404, {"Content-Type": "text/plain"}, b"no such repo\n"
    updates: list[tuple[str, str, str]] = []
    remaining = length
    while remaining > 0:
        hdr = source.read(4)
        remaining -= len(hdr)
        if len(hdr) < 4:
            break
        n = int(hdr.decode(), 16)
        if n == 0:
            break
        payload = source.read(n - 4)
        remaining -= len(payload)
        if len(payload) != n - 4:
            break
        line = payload.decode("utf-8", errors="replace").rstrip("\n").split("\0", 1)[0]
        parts = line.split(" ")
        if len(parts) >= 3:
            updates.append((parts[0], parts[1], parts[2]))
    pack_path = None
    if remaining > 0:
        with tempfile.NamedTemporaryFile(prefix="pygit-receive-", suffix=".pack", delete=False) as tmp:
            pack_path = Path(tmp.name)
            while remaining > 0:
                chunk = source.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                tmp.write(chunk)
                remaining -= len(chunk)
    return (
        200,
        {"Content-Type": "application/x-git-receive-pack-result"},
        _receive_pack_report(repo, updates, pack_path),
    )


def http_backend_stream(method: str, path: str, body: bytes, base: Path) -> tuple[int, dict[str, str], Iterable[bytes]]:
    """Streaming variant of ``http_backend`` for large upload-pack responses."""
    clean_path = path.split("?", 1)[0]
    if clean_path.endswith("/git-upload-pack"):
        repo = _repo_from_smart_http_path(clean_path, base)
        if repo is None:
            return 404, {"Content-Type": "text/plain"}, [b"no such repo\n"]
        shas = _objects_for_wants(repo, _parse_upload_pack_wants(body))
        return (
            200,
            {"Content-Type": "application/x-git-upload-pack-result"},
            _upload_pack_response_iter(repo, shas),
        )
    if clean_path.endswith("/git-receive-pack"):
        repo = _repo_from_smart_http_path(clean_path, base)
        if repo is None:
            return 404, {"Content-Type": "text/plain"}, [b"no such repo\n"]
        return (
            200,
            {"Content-Type": "application/x-git-receive-pack-result"},
            [_receive_pack_body(repo, body)],
        )
    status, headers, out = http_backend(method, path, body, base)
    return status, headers, [out]


def http_backend(method: str, path: str, body: bytes, base: Path) -> tuple[int, dict[str, str], bytes]:
    """Return (status, headers, body) for a given smart-HTTP request."""
    # split off query string
    query = ""
    if "?" in path:
        path, query = path.split("?", 1)
    # repo path is everything up to /info/refs or /git-{receive,upload}-pack
    for ep in ("/info/refs", "/git-upload-pack", "/git-receive-pack"):
        if ep in path:
            repo_path = path.split(ep)[0].lstrip("/")
            break
    else:
        return 404, {"Content-Type": "text/plain"}, b"not found\n"
    repo_dir = base / repo_path
    try:
        repo = Repository.discover(str(repo_dir))
    except Exception:
        return 404, {"Content-Type": "text/plain"}, b"no such repo\n"
    if path.endswith("/info/refs"):
        service = ""
        if "service=git-upload-pack" in query:
            service = "git-upload-pack"
        elif "service=git-receive-pack" in query:
            service = "git-receive-pack"
        if not service:
            return 400, {"Content-Type": "text/plain"}, b"need service\n"
        out = bytearray()
        adv = f"# service={service}\n".encode()
        out += _pkt(adv) + b"0000"
        caps = b"side-band-64k ofs-delta agent=pythongit/0.1" if service == "git-upload-pack" else b"report-status agent=pythongit/0.1"
        if repo.object_format() == "sha256":
            caps += b" object-format=sha256"
        first = True
        any_refs = False
        for kind in ("refs/heads", "refs/tags"):
            root = repo.gitdir / kind
            if root.exists():
                for f in sorted(root.rglob("*")):
                    if f.is_file():
                        rel = str(f.relative_to(repo.gitdir)).replace(os.sep, "/")
                        s = refs_mod.read_ref(repo, rel)
                        if s:
                            any_refs = True
                            line = f"{s} {rel}".encode()
                            if first:
                                first = False
                                line += b"\0" + caps
                            out += _pkt(line + b"\n")
        if not any_refs:
            # advertise HEAD shim if there's nothing else
            out += _pkt(repo.null_oid().encode() + b" capabilities^{}\0" + caps + b"\n")
        out += b"0000"
        return 200, {"Content-Type": f"application/x-{service}-advertisement"}, bytes(out)
    if path.endswith("/git-upload-pack"):
        # parse wants + done
        shas = _objects_for_wants(repo, _parse_upload_pack_wants(body))
        out = b"".join(_upload_pack_response_iter(repo, shas))
        return 200, {"Content-Type": "application/x-git-upload-pack-result"}, bytes(out)
    if path.endswith("/git-receive-pack"):
        return 200, {"Content-Type": "application/x-git-receive-pack-result"}, _receive_pack_body(repo, body)
    return 404, {"Content-Type": "text/plain"}, b"not found\n"


# ---------------------------------------------------------------------------
# instaweb / gitweb — minimal HTTP browser.


def instaweb(repo: Repository, port: int = 1234) -> int:
    from http.server import HTTPServer, BaseHTTPRequestHandler

    base = repo.path.parent  # serve all repos in the parent directory

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _send(self, status, headers, body):
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_stream(self, status, headers, chunks: Iterable[bytes]):
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.end_headers()
            for chunk in chunks:
                if chunk:
                    self.wfile.write(chunk)

        def do_GET(self):
            if "/info/refs" in self.path or self.path.endswith("/git-upload-pack"):
                status, hdrs, body = http_backend("GET", self.path, b"", base)
                self._send(status, hdrs, body)
                return
            # gitweb: list refs and log
            html = ["<html><body><h1>pygit instaweb</h1>"]
            html.append(f"<p>repo: {repo.path}</p>")
            html.append("<h2>branches</h2><ul>")
            for b in refs_mod.list_branches(repo):
                s = refs_mod.read_ref(repo, f"refs/heads/{b}") or ""
                html.append(f"<li>{b}: <code>{s[:8]}</code></li>")
            html.append("</ul><h2>recent commits</h2><ul>")
            head = refs_mod.rev_parse(repo, "HEAD")
            cur = head
            n = 0
            while cur and n < 50:
                try:
                    c = objs.parse_commit(objs.read_object(repo, cur)[1])
                except KeyError:
                    break
                subject = c.message.splitlines()[0] if c.message.strip() else ""
                html.append(f"<li><code>{cur[:8]}</code> {subject}</li>")
                cur = c.parents[0] if c.parents else None
                n += 1
            html.append("</ul></body></html>")
            body = "\n".join(html).encode()
            self._send(200, {"Content-Type": "text/html; charset=utf-8"}, body)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            if self.path.split("?", 1)[0].endswith("/git-receive-pack"):
                status, hdrs, body = http_backend_receive_pack_stream(self.path, self.rfile, length, base)
                self._send(status, hdrs, body)
                return
            body = self.rfile.read(length) if length else b""
            status, hdrs, chunks = http_backend_stream("POST", self.path, body, base)
            self._send_stream(status, hdrs, chunks)

    print(f"instaweb listening on http://127.0.0.1:{port}/")
    with HTTPServer(("127.0.0.1", port), Handler) as srv:
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


# ---------------------------------------------------------------------------
# gitk / gui — Tk GUI showing log + refs.


def launch_tk(repo: Repository) -> int:
    try:
        import tkinter as tk
        from tkinter import ttk, scrolledtext
    except ImportError:
        return launch_text_log(repo)
    try:
        root = tk.Tk()
    except Exception:
        return launch_text_log(repo)
    root.title(f"pygit gui — {repo.path}")

    frame = ttk.Frame(root, padding=8)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text=f"Repository: {repo.path}").pack(anchor="w")

    listbox = tk.Listbox(frame, width=100, height=20)
    listbox.pack(fill="both", expand=True, padx=4, pady=4)

    msg_box = scrolledtext.ScrolledText(frame, height=10)
    msg_box.pack(fill="both", expand=True, padx=4, pady=4)

    commits: list[str] = []
    head = refs_mod.rev_parse(repo, "HEAD")
    cur = head
    while cur and len(commits) < 200:
        try:
            c = objs.parse_commit(objs.read_object(repo, cur)[1])
        except KeyError:
            break
        subject = c.message.splitlines()[0] if c.message.strip() else ""
        commits.append(cur)
        listbox.insert("end", f"{cur[:8]}  {subject}")
        cur = c.parents[0] if c.parents else None

    def on_select(evt):
        sel = listbox.curselection()
        if not sel:
            return
        sha = commits[sel[0]]
        c = objs.parse_commit(objs.read_object(repo, sha)[1])
        msg_box.delete("1.0", "end")
        msg_box.insert("end", f"commit {sha}\n")
        msg_box.insert("end", f"Author: {c.author}\n")
        msg_box.insert("end", f"Date:   {c.committer}\n\n")
        msg_box.insert("end", c.message)

    listbox.bind("<<ListboxSelect>>", on_select)
    root.mainloop()
    return 0


def launch_text_log(repo: Repository, limit: int = 200) -> int:
    head = refs_mod.rev_parse(repo, "HEAD")
    cur = head
    n = 0
    while cur and n < limit:
        try:
            c = objs.parse_commit(objs.read_object(repo, cur)[1])
        except KeyError:
            break
        subject = c.message.splitlines()[0] if c.message.strip() else ""
        print(f"{cur[:8]} {subject}")
        cur = c.parents[0] if c.parents else None
        n += 1
    return 0


# ---------------------------------------------------------------------------
# CVS / SVN bridges — shell out to the cvs/svn binaries (what git's C does too).


def shell_out(args: list[str], *, env: Optional[dict] = None, cwd: Optional[str] = None) -> int:
    try:
        return subprocess.call(args, env=env, cwd=cwd)
    except FileNotFoundError:
        print(f"required binary not installed: {args[0]}", file=sys.stderr)
        return 127


# ---------------------------------------------------------------------------
# fsmonitor — native daemon where possible, polling fallback otherwise.


def _emit_fsmonitor_path(repo: Repository, path: Path | str) -> None:
    try:
        rel = Path(path).resolve().relative_to(repo.path)
        text = str(rel)
    except Exception:
        text = str(path)
    text = text.replace(os.sep, "/")
    if text and ".git" not in text.split("/"):
        print(text)
        sys.stdout.flush()


def _fsmonitor_run_polling(repo: Repository, interval: float = 1.0, iterations: int = 0) -> int:
    snapshot: dict[str, float] = {}

    def scan():
        for root, dirs, files in os.walk(repo.path):
            dirs[:] = [d for d in dirs if d != ".git"]
            for f in files:
                full = Path(root) / f
                try:
                    snapshot[str(full.relative_to(repo.path))] = full.stat().st_mtime
                except OSError:
                    pass

    scan()
    n = 0
    while True:
        time.sleep(interval)
        changed: list[str] = []
        for root, dirs, files in os.walk(repo.path):
            dirs[:] = [d for d in dirs if d != ".git"]
            for f in files:
                full = Path(root) / f
                rel = str(full.relative_to(repo.path))
                try:
                    mt = full.stat().st_mtime
                except OSError:
                    continue
                if snapshot.get(rel) != mt:
                    changed.append(rel)
                    snapshot[rel] = mt
        for c in changed:
            print(c)
            sys.stdout.flush()
        n += 1
        if iterations and n >= iterations:
            break
    return 0


def _fsmonitor_run_windows(repo: Repository, iterations: int = 0) -> int:
    import ctypes
    from ctypes import wintypes

    FILE_LIST_DIRECTORY = 0x0001
    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    FILE_SHARE_DELETE = 0x00000004
    OPEN_EXISTING = 3
    FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    FILE_NOTIFY_CHANGE_FILE_NAME = 0x00000001
    FILE_NOTIFY_CHANGE_DIR_NAME = 0x00000002
    FILE_NOTIFY_CHANGE_LAST_WRITE = 0x00000010
    FILE_NOTIFY_CHANGE_SIZE = 0x00000008
    INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.CreateFileW(
        str(repo.path),
        FILE_LIST_DIRECTORY,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        None,
        OPEN_EXISTING,
        FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    if handle == INVALID_HANDLE_VALUE:
        raise OSError(ctypes.get_last_error(), "CreateFileW failed")
    try:
        count = 0
        while True:
            buf = ctypes.create_string_buffer(64 * 1024)
            returned = wintypes.DWORD()
            ok = kernel32.ReadDirectoryChangesW(
                handle,
                ctypes.byref(buf),
                len(buf),
                True,
                FILE_NOTIFY_CHANGE_FILE_NAME | FILE_NOTIFY_CHANGE_DIR_NAME |
                FILE_NOTIFY_CHANGE_LAST_WRITE | FILE_NOTIFY_CHANGE_SIZE,
                ctypes.byref(returned),
                None,
                None,
            )
            if not ok:
                raise OSError(ctypes.get_last_error(), "ReadDirectoryChangesW failed")
            pos = 0
            while pos < returned.value:
                next_off = int.from_bytes(buf.raw[pos : pos + 4], "little")
                name_len = int.from_bytes(buf.raw[pos + 8 : pos + 12], "little")
                name = buf.raw[pos + 12 : pos + 12 + name_len].decode("utf-16le", errors="replace")
                _emit_fsmonitor_path(repo, repo.path / name)
                count += 1
                if iterations and count >= iterations:
                    return 0
                if not next_off:
                    break
                pos += next_off
    finally:
        kernel32.CloseHandle(handle)


def _fsmonitor_run_inotify(repo: Repository, iterations: int = 0) -> int:
    import ctypes
    import errno
    import select
    import struct

    libc = ctypes.CDLL(None, use_errno=True)
    fd = libc.inotify_init1(getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0))
    if fd < 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    watches: dict[int, Path] = {}
    mask = 0x00000100 | 0x00000080 | 0x00000002 | 0x00000004 | 0x00000200 | 0x40000000

    def add_dir(path: Path) -> None:
        wd = libc.inotify_add_watch(fd, os.fsencode(path), mask)
        if wd >= 0:
            watches[wd] = path

    try:
        for root, dirs, _files in os.walk(repo.path):
            dirs[:] = [d for d in dirs if d != ".git"]
            add_dir(Path(root))
        count = 0
        while True:
            select.select([fd], [], [])
            try:
                data = os.read(fd, 64 * 1024)
            except BlockingIOError:
                continue
            pos = 0
            while pos + 16 <= len(data):
                wd, event_mask, _cookie, name_len = struct.unpack_from("iIII", data, pos)
                pos += 16
                raw_name = data[pos : pos + name_len].split(b"\0", 1)[0]
                pos += name_len
                parent = watches.get(wd)
                if parent is None or not raw_name:
                    continue
                path = parent / os.fsdecode(raw_name)
                if event_mask & 0x40000000 and path.is_dir():
                    add_dir(path)
                _emit_fsmonitor_path(repo, path)
                count += 1
                if iterations and count >= iterations:
                    return 0
    finally:
        try:
            os.close(fd)
        except OSError as exc:
            if exc.errno != errno.EBADF:
                raise


def fsmonitor_backend() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform.startswith("linux"):
        return "inotify"
    return "polling"


def fsmonitor_run(repo: Repository, interval: float = 1.0, iterations: int = 0, backend: str = "auto") -> int:
    """Watch the worktree and print changed paths."""
    if backend == "polling":
        return _fsmonitor_run_polling(repo, interval=interval, iterations=iterations)
    if backend == "auto" and iterations:
        return _fsmonitor_run_polling(repo, interval=interval, iterations=iterations)
    selected = fsmonitor_backend() if backend in ("auto", "native") else backend
    if selected == "windows":
        if sys.platform != "win32":
            print("windows fsmonitor backend is not available on this platform", file=sys.stderr)
            return 1
        try:
            return _fsmonitor_run_windows(repo, iterations)
        except (AttributeError, OSError) as exc:
            if backend != "auto":
                print(f"native fsmonitor failed: {exc}", file=sys.stderr)
                return 1
    if selected == "inotify":
        if not sys.platform.startswith("linux"):
            print("inotify fsmonitor backend is not available on this platform", file=sys.stderr)
            return 1
        try:
            return _fsmonitor_run_inotify(repo, iterations)
        except (AttributeError, OSError) as exc:
            if backend != "auto":
                print(f"native fsmonitor failed: {exc}", file=sys.stderr)
                return 1
    if backend == "native":
        print("native fsmonitor backend is not available on this platform", file=sys.stderr)
        return 1
    return _fsmonitor_run_polling(repo, interval=interval, iterations=iterations)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def fsmonitor_daemon(repo: Repository, op: str) -> int:
    pid_file = repo.gitdir / "pygit-fsmonitor.pid"
    log_file = repo.gitdir / "pygit-fsmonitor.log"
    if op == "status":
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text(encoding="utf-8").strip())
            except ValueError:
                pid = 0
            if pid and _pid_alive(pid):
                print(f"fsmonitor-daemon: running pid={pid} backend={fsmonitor_backend()}")
                return 0
        print(f"fsmonitor-daemon: stopped backend={fsmonitor_backend()}")
        return 0
    if op == "stop":
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text(encoding="utf-8").strip())
                os.kill(pid, 15)
            except OSError:
                pass
            pid_file.unlink(missing_ok=True)
        print("fsmonitor-daemon: stopped")
        return 0
    if op == "start":
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text(encoding="utf-8").strip())
                if _pid_alive(pid):
                    print(f"fsmonitor-daemon: already running pid={pid}")
                    return 0
            except ValueError:
                pass
        with log_file.open("ab") as log:
            kwargs = {}
            if sys.platform == "win32":
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            proc = subprocess.Popen(
                [sys.executable, "-m", "pythongit", "fsmonitor-daemon", "run"],
                cwd=str(repo.path),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                **kwargs,
            )
        pid_file.write_text(str(proc.pid) + "\n", encoding="utf-8")
        print(f"fsmonitor-daemon: started pid={proc.pid} backend={fsmonitor_backend()}")
        return 0
    if op == "run":
        return fsmonitor_run(repo, iterations=0, backend="auto")
    return 1
