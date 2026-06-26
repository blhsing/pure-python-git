"""GPG signing and verification helpers.

A faithful port of the OpenPGP code paths in Git 2.54's gpg-interface.c,
commit.c (add_header_signature / parse_buffer_signed_by_header) and the
sign/verify entry points used by `git commit -S`, `git tag -s`,
`git verify-commit` and `git verify-tag`.

Only the OpenPGP ("gpg") format is wired into the CLI -- x509 (gpgsm) and ssh
(ssh-keygen) share the same gpg-style sign/verify code in C but are selected by
gpg.format; we resolve the program/args for openpgp and shell out exactly as C
does.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from typing import Optional


# gpg_sig_headers[] in commit.c -- index 1 is sha1, 2 is sha256.
GPG_SIG_HEADER = "gpgsig"


def _config(repo, name: str) -> Optional[str]:
    from . import gitconfig
    try:
        return gitconfig.get(repo, name)
    except Exception:
        return None


def _gpg_program(repo) -> str:
    """git_gpg_config(): gpg.program / gpg.openpgp.program override the default
    'gpg' for the openpgp format."""
    return (_config(repo, "gpg.program")
            or _config(repo, "gpg.openpgp.program")
            or "gpg")


def get_signing_key(repo, committer_info: str) -> str:
    """get_signing_key() for the openpgp format: user.signingkey if set,
    otherwise the committer identity without the trailing date
    (git_committer_info(IDENT_STRICT | IDENT_NO_DATE) => 'Name <email>')."""
    key = _config(repo, "user.signingkey")
    if key:
        return key
    return committer_info


def sign_buffer(repo, payload: bytes, signing_key: str) -> tuple[Optional[bytes], str]:
    """Port of sign_buffer_gpg() (gpg-interface.c).

    Runs `gpg --status-fd=2 -bsau <signing_key>` with *payload* on stdin.  On
    success returns (signature_bytes, "").  On failure returns (None, errmsg)
    where errmsg is the exact text printed by `error("gpg failed to sign the
    data:\\n%s")` -- the caller prints 'error: ' + errmsg.
    """
    program = _gpg_program(repo)
    args = [program, "--status-fd=2", "-bsau", signing_key]
    try:
        proc = subprocess.run(
            args,
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, FileNotFoundError):
        # pipe_command can't even start the program; git reports the same
        # "gpg failed to sign the data" path with no status output.
        return None, "gpg failed to sign the data:\n(no gpg output)"

    signature = proc.stdout
    gpg_status = proc.stderr.decode("utf-8", "replace")

    # Look for a "[GNUPG:] SIG_CREATED " line at the beginning of a line.
    found = False
    idx = 0
    while True:
        pos = gpg_status.find("[GNUPG:] SIG_CREATED ", idx)
        if pos < 0:
            break
        if pos == 0 or gpg_status[pos - 1] == "\n":
            found = True
            break
        idx = pos + 1

    ret = proc.returncode
    if ret or not found:
        detail = gpg_status if gpg_status else "(no gpg output)"
        return None, "gpg failed to sign the data:\n" + detail

    # remove_cr_after(): strip CR characters from the signature bytes.
    signature = signature.replace(b"\r", b"")
    return signature, ""


def add_header_signature(buf: bytes, sig: bytes, header: str = GPG_SIG_HEADER) -> bytes:
    """Port of add_header_signature() (commit.c).

    Insert *sig* as a folded ``<header>`` block just before the blank line that
    ends the commit headers.  Every signature line is prefixed with a single
    space; the first one is prefixed with the header name.
    """
    # find the end of the header: first "\n\n"
    eoh = buf.find(b"\n\n")
    if eoh < 0:
        inspos = len(buf)
    else:
        inspos = eoh + 1

    out = bytearray(buf[:inspos])
    tail = buf[inspos:]

    copypos = 0
    hdr = header.encode("ascii")
    n = len(sig)
    first = True
    while copypos < n:
        nl = sig.find(b"\n", copypos)
        if nl < 0:
            eol = n
            has_nl = False
        else:
            eol = nl
            has_nl = True
        length = (eol - copypos) + (1 if has_nl else 0)
        line = sig[copypos:copypos + length]
        if first:
            out += hdr
            first = False
        out += b" "
        out += line
        copypos += length
    out += tail
    return bytes(out)


def parse_signature(buf: bytes) -> tuple[bytes, bytes]:
    """Port of parse_signature()/parse_signed_buffer() for the *tag* case where
    the signature is appended verbatim at the end of the object.

    Returns (payload, signature).  signature is empty if none present.
    """
    sig_starts = (b"-----BEGIN PGP SIGNATURE-----",
                  b"-----BEGIN PGP MESSAGE-----",
                  b"-----BEGIN SIGNED MESSAGE-----",
                  b"-----BEGIN SSH SIGNATURE-----")
    size = len(buf)
    match = size
    pos = 0
    while pos < size:
        # does a known signature format begin at pos?
        for s in sig_starts:
            if buf.startswith(s, pos):
                match = pos
                break
        nl = buf.find(b"\n", pos)
        if nl < 0:
            pos = size
        else:
            pos = nl + 1
    if match == size:
        return buf, b""
    return buf[:match], buf[match:]


def parse_commit_signature(buf: bytes, header: str = GPG_SIG_HEADER) -> tuple[bytes, bytes]:
    """Port of parse_buffer_signed_by_header() (commit.c) for commits.

    Splits a commit object buffer into (payload, signature), where the
    signature lives in a folded ``gpgsig`` header.  Continuation lines (leading
    space) belong to the signature; the header itself is stripped from the
    payload, and a competing ``gpgsig-sha256`` header is dropped too
    (other_signature handling).
    """
    payload = bytearray()
    signature = bytearray()
    in_signature = False
    other_signature = False
    hdr = header.encode("ascii")
    size = len(buf)
    line = 0
    while line < size:
        nl = buf.find(b"\n", line)
        nxt = (nl + 1) if nl >= 0 else size
        sig = None
        if in_signature and line < size and buf[line:line + 1] == b" ":
            sig = line + 1
        elif buf.startswith(hdr, line) and buf[line + len(hdr):line + len(hdr) + 1] == b" ":
            sig = line + len(hdr) + 1
            other_signature = False
        elif buf.startswith(b"gpgsig", line):
            other_signature = True
        elif other_signature and buf[line:line + 1] != b" ":
            other_signature = False
        if sig is not None:
            signature += buf[sig:nxt]
            in_signature = True
        else:
            if line < size and buf[line:line + 1] == b"\n":
                nxt = size
            if not other_signature:
                payload += buf[line:nxt]
            in_signature = False
        line = nxt
    return bytes(payload), bytes(signature)


def verify_signed(repo, payload: bytes, signature: bytes
                  ) -> tuple[int, str, str]:
    """Port of verify_gpg_signed_buffer() + check_signature() (gpg-interface.c)
    for the OpenPGP format.

    Returns (status, gpg_stderr, gpg_status_stdout):
      * status == 0  => the signature verified ('G' GOODSIG or 'Y' EXPKEYSIG)
      * status != 0  => verification failed
    The caller relays gpg_stderr (or gpg_status_stdout for --raw) and uses
    `status` as the exit code contribution.
    """
    program = _gpg_program(repo)
    # openpgp verify_args: --keyid-format=long
    with tempfile.NamedTemporaryFile(prefix=".git_vtag_tmp", delete=False) as tf:
        tf.write(signature)
        sigfile = tf.name
    try:
        args = [program, "--keyid-format=long", "--status-fd=1",
                "--verify", sigfile, "-"]
        try:
            proc = subprocess.run(
                args, input=payload,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
        except (OSError, FileNotFoundError):
            return 1, "", ""
        gpg_stdout = proc.stdout.decode("utf-8", "replace")
        gpg_stderr = proc.stderr.decode("utf-8", "replace")
        ret = proc.returncode
    finally:
        try:
            os.unlink(sigfile)
        except OSError:
            pass

    # ret |= !GOODSIG && !EXPKEYSIG  (note the leading "\n" in C's strstr)
    if ("\n[GNUPG:] GOODSIG " not in gpg_stdout and
            "\n[GNUPG:] EXPKEYSIG " not in gpg_stdout):
        ret = ret | 1

    result = _parse_gpg_result(gpg_stdout)

    # check_signature(): status |= result != 'G' && result != 'Y'
    status = ret
    if result != "G" and result != "Y":
        status = status | 1
    return (1 if status else 0), gpg_stderr, gpg_stdout


def _parse_gpg_result(gpg_status: str) -> str:
    """Subset of parse_gpg_output(): determine sigc->result from the
    [GNUPG:] status lines.  Only the result letter is needed by callers."""
    return parse_gpg_output(gpg_status)["result"]


# Keep in sync with enum signature_trust_level / sigcheck_gpg_trust_level[].
_TRUST_LEVELS = {
    "UNDEFINED": (0, "undefined"),
    "NEVER": (1, "never"),
    "MARGINAL": (2, "marginal"),
    "FULLY": (3, "fully"),
    "ULTIMATE": (4, "ultimate"),
}


def parse_gpg_output(gpg_status: str) -> dict:
    """Port of parse_gpg_output() (gpg-interface.c): parse the GNUPG status-fd
    lines into the signature_check fields used by the %G* pretty placeholders.

    Returns a dict with keys: result, signer, key, fingerprint,
    primary_key_fingerprint, trust_level (int), trust_name (str)."""
    out = {
        "result": "N",
        "signer": "",
        "key": "",
        "fingerprint": "",
        "primary_key_fingerprint": "",
        "trust_level": 0,
        "trust_name": "undefined",
    }
    # (result_letter or None, check_prefix, KEYID, UID, FINGERPRINT, TRUST, EXCL)
    KEYID, UID, FINGERPRINT, TRUST, EXCL = 1, 2, 4, 8, 16
    STD = EXCL | KEYID | UID
    table = [
        ("G", "GOODSIG ", STD),
        ("B", "BADSIG ", STD),
        ("E", "ERRSIG ", EXCL | KEYID),
        ("X", "EXPSIG ", STD),
        ("Y", "EXPKEYSIG ", STD),
        ("R", "REVKEYSIG ", STD),
        (None, "VALIDSIG ", FINGERPRINT),
        (None, "TRUST_", TRUST),
    ]
    seen_exclusive = 0
    for raw in gpg_status.split("\n"):
        if not raw.startswith("[GNUPG:] "):
            continue
        line = raw[len("[GNUPG:] "):]
        for letter, check, flags in table:
            if not line.startswith(check):
                continue
            rest = line[len(check):]
            if flags & EXCL:
                seen_exclusive += 1
                if seen_exclusive > 1:
                    return _gpg_error_result()
            if letter:
                out["result"] = letter
            if flags & KEYID:
                sp = rest.find(" ")
                out["key"] = rest if sp < 0 else rest[:sp]
                if sp >= 0 and (flags & UID):
                    out["signer"] = rest[sp + 1:]
            if flags & TRUST:
                tname = rest.split(" ", 1)[0]
                if tname in _TRUST_LEVELS:
                    out["trust_level"], out["trust_name"] = _TRUST_LEVELS[tname]
                else:
                    return _gpg_error_result()
            if flags & FINGERPRINT:
                fields = rest.split(" ")
                out["fingerprint"] = fields[0] if fields else ""
                # primary key fingerprint is the 10th field on the VALIDSIG line.
                if len(fields) >= 10:
                    out["primary_key_fingerprint"] = fields[9]
                else:
                    out["primary_key_fingerprint"] = ""
            break
    return out


def _gpg_error_result() -> dict:
    return {
        "result": "E", "signer": "", "key": "", "fingerprint": "",
        "primary_key_fingerprint": "", "trust_level": 0,
        "trust_name": "undefined",
    }


def check_commit_signature_full(repo, commit_buf: bytes) -> dict:
    """Port of check_commit_signature(): verify a commit object buffer and
    return the full signature_check field dict (plus 'output' = gpg stderr).
    result stays 'N' when there is no signature."""
    payload, signature = parse_commit_signature(commit_buf)
    fields = {
        "result": "N", "signer": "", "key": "", "fingerprint": "",
        "primary_key_fingerprint": "", "trust_level": 0,
        "trust_name": "undefined", "output": "", "payload": payload,
    }
    if not signature:
        return fields
    _status, gpg_stderr, gpg_status = verify_signed(repo, payload, signature)
    parsed = parse_gpg_output(gpg_status)
    fields.update(parsed)
    fields["output"] = gpg_stderr
    fields["payload"] = payload
    return fields


def remove_signature_from_commit(buf: bytes) -> bytes:
    """Return the commit buffer with its gpgsig header(s) stripped -- i.e. the
    payload as `git log --format=%B`/verify see it.  Thin wrapper over
    parse_commit_signature for callers that only want the payload."""
    payload, _sig = parse_commit_signature(buf)
    return payload
