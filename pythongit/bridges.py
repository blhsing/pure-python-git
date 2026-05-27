"""Implementations for the external-system bridges:
  send-email (smtplib), difftool / mergetool (shell out to configured tool),
  daemon (TCP git:// server), http-backend (CGI for smart HTTP),
  http-fetch (dumb HTTP fetch), instaweb (basic browser),
  credential-store, maintenance (dispatcher), shell (restricted dispatcher),
  remote-helper / remote-ext / remote-fd (plug-in protocols),
  fsmonitor (polling daemon), cvsexportcommit / cvsimport / cvsserver / svn
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
import threading
import time
import urllib.request
import urllib.parse
from pathlib import Path
from typing import Optional

from . import objects as objs
from . import pack as pack_mod
from . import refs as refs_mod
from .repo import Repository


# ---------------------------------------------------------------------------
# send-email — read an mbox of patches and SMTP them out.


def send_email(mbox: str, *, to: list[str], from_addr: Optional[str] = None,
               smtp_host: str = "localhost", smtp_port: int = 25,
               smtp_user: Optional[str] = None, smtp_pass: Optional[str] = None) -> int:
    import smtplib
    from email.message import EmailMessage
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
    with smtplib.SMTP(smtp_host, smtp_port) as s:
        if smtp_user:
            s.starttls()
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
        sha, _ = objs.hash_bytes("blob", data)
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
                from .protocol import _collect_objects
                for w in wants:
                    for o in _collect_objects(repo, w, set()):
                        if o not in shas:
                            shas.append(o)
                pack_bytes, _ = pack_mod.build_pack(repo, shas)
                # chunk side-band channel 1
                i = 0
                while i < len(pack_bytes):
                    chunk = pack_bytes[i : i + 65500]
                    self.request.sendall(_pkt(b"\x01" + chunk))
                    i += 65500
                self.request.sendall(b"0000")

        def _receive_pack(self, repo: Repository):
            # advertise refs
            caps = b"report-status agent=pythongit/0.1"
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
            pack_buf = b""
            while True:
                chunk = self.request.recv(65536)
                if not chunk:
                    break
                pack_buf += chunk
            if pack_buf:
                try:
                    pack_mod.unpack_pack(repo, pack_buf)
                except Exception:
                    pass
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
            out += _pkt(b"0000000000000000000000000000000000000000 capabilities^{}\0" + caps + b"\n")
        out += b"0000"
        return 200, {"Content-Type": f"application/x-{service}-advertisement"}, bytes(out)
    if path.endswith("/git-upload-pack"):
        # parse wants + done
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
        from .protocol import _collect_objects
        shas: list[str] = []
        for w in wants:
            for o in _collect_objects(repo, w, set()):
                if o not in shas:
                    shas.append(o)
        pack_bytes, _ = pack_mod.build_pack(repo, shas)
        out = bytearray()
        out += _pkt(b"NAK\n")
        k = 0
        while k < len(pack_bytes):
            chunk = pack_bytes[k : k + 65500]
            out += _pkt(b"\x01" + chunk)
            k += 65500
        out += b"0000"
        return 200, {"Content-Type": "application/x-git-upload-pack-result"}, bytes(out)
    if path.endswith("/git-receive-pack"):
        # commands then pack
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
        pack_buf = body[i:]
        if pack_buf:
            try:
                pack_mod.unpack_pack(repo, pack_buf)
            except Exception:
                pass
        out = bytearray()
        out += _pkt(b"unpack ok\n")
        for old, new, ref in updates:
            try:
                refs_mod.update_ref(repo, ref, new)
                out += _pkt(f"ok {ref}\n".encode())
            except Exception as e:
                out += _pkt(f"ng {ref} {e}\n".encode())
        out += b"0000"
        return 200, {"Content-Type": "application/x-git-receive-pack-result"}, bytes(out)
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
            body = self.rfile.read(length) if length else b""
            status, hdrs, out = http_backend("POST", self.path, body, base)
            self._send(status, hdrs, out)

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
        print("tkinter not available", file=sys.stderr)
        return 1
    root = tk.Tk()
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


# ---------------------------------------------------------------------------
# CVS / SVN bridges — shell out to the cvs/svn binaries (what git's C does too).


def shell_out(args: list[str], *, env: Optional[dict] = None, cwd: Optional[str] = None) -> int:
    try:
        return subprocess.call(args, env=env, cwd=cwd)
    except FileNotFoundError:
        print(f"required binary not installed: {args[0]}", file=sys.stderr)
        return 127


# ---------------------------------------------------------------------------
# fsmonitor — polling daemon writing a list of recently-changed paths.


def fsmonitor_run(repo: Repository, interval: float = 1.0, iterations: int = 0) -> int:
    """Poll the worktree; print changed paths to stdout. Stops after `iterations`
    polls (0 = run forever).
    """
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
