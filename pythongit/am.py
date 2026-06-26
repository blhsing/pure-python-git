"""Pure-Python port of ``git am`` (builtin/am.c) plus the ``mailsplit`` and
``mailinfo`` helpers it drives (builtin/mailsplit.c, mailinfo.c).

The code in this module is a faithful, line-for-line port of the Git 2.54.0 C
sources so that ``pygit am`` is byte-exact with the oracle: the same mailbox
splitting, the same RFC2822/RFC2047 header decoding, the same subject/From
munging, the same ``.git/rebase-apply`` state machine, the same 3-way fallback
and the same diagnostics, ordering and exit codes.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

# ===========================================================================
# mailsplit (builtin/mailsplit.c)
# ===========================================================================


def _is_from_line(line: bytes) -> bool:
    """Port of is_from_line(): does ``line`` look like a Unix mbox "From " line
    (with a date that ends in HH:MM:SS and a >90 year)?"""
    n = len(line)
    if n < 20 or line[:5] != b"From ":
        return 0
    # colon = line + len - 2; scan backwards for ':'
    colon = n - 2
    i = 5
    while True:
        if colon < i:
            return 0
        colon -= 1
        if line[colon : colon + 1] == b":":
            break

    def _isdigit(idx: int) -> bool:
        return 0 <= idx < n and 48 <= line[idx] <= 57

    if not (_isdigit(colon - 4) and _isdigit(colon - 2) and _isdigit(colon - 1)
            and _isdigit(colon + 1) and _isdigit(colon + 2)):
        return 0
    # year: strtol(colon+3, NULL, 10) <= 90 -> not a from line
    m = re.match(rb"\s*([+-]?\d+)", line[colon + 3:])
    year = int(m.group(1)) if m else 0
    if year <= 90:
        return 0
    return 1


def _is_gtfrom(buf: bytes) -> bool:
    """Port of is_gtfrom(): a ``>+From `` line for mboxrd unescaping."""
    if len(buf) < len(b">From "):
        return False
    ngt = 0
    while ngt < len(buf) and buf[ngt : ngt + 1] == b">":
        ngt += 1
    return ngt > 0 and buf[ngt:].startswith(b"From ")


class _MboxReader:
    """Cursor over an mbox byte buffer reproducing C's strbuf_getwholeline."""

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def getwholeline(self) -> Optional[bytes]:
        if self.pos >= len(self.data):
            return None
        nl = self.data.find(b"\n", self.pos)
        if nl < 0:
            line = self.data[self.pos:]
            self.pos = len(self.data)
            return line
        line = self.data[self.pos : nl + 1]
        self.pos = nl + 1
        return line


def _split_one_msg(rdr: _MboxReader, first: bytes, out_path: Path,
                   allow_bare: bool, keep_cr: bool, mboxrd: bool) -> tuple[bool, bool]:
    """Write one message starting with ``first`` already read. Returns
    (status_done, corrupt)."""
    buf = first
    is_bare = not _is_from_line(buf)
    if is_bare and not allow_bare:
        return True, True  # caller prints "corrupt mailbox"
    chunks: list[bytes] = []
    status = False
    while True:
        if not keep_cr and len(buf) > 1 and buf[-1:] == b"\n" and buf[-2:-1] == b"\r":
            buf = buf[:-2] + b"\n"
        if mboxrd and _is_gtfrom(buf):
            buf = buf[1:]
        chunks.append(buf)
        nxt = rdr.getwholeline()
        if nxt is None:
            status = True
            break
        buf = nxt
        if not is_bare and _is_from_line(buf):
            # rewind: this 'From' line belongs to the next message
            rdr.pos -= len(buf)
            break
    out_path.write_bytes(b"".join(chunks))
    return status, False


def split_mbox(data: bytes, out_dir: Path, allow_bare: bool, nr_prec: int,
               skip: int, keep_cr: bool, mboxrd: bool,
               is_stdin: bool) -> tuple[int, Optional[str]]:
    """Port of split_mbox(). Returns (new_skip, error_or_None)."""
    # Skip leading whitespace; detect empty input.
    i = 0
    n = len(data)
    while i < n and data[i] in (0x20, 0x09, 0x0a, 0x0b, 0x0c, 0x0d):
        i += 1
    if i >= n:
        # EOF after whitespace: empty stdin is OK; empty file is an error.
        if is_stdin:
            return skip, None
        return skip, "empty"
    rdr = _MboxReader(data)
    rdr.pos = i
    first = rdr.getwholeline()
    if first is None:
        if is_stdin:
            return skip, None
        return skip, "read"
    file_done = False
    while not file_done:
        skip += 1
        name = out_dir / ("%0*d" % (nr_prec, skip))
        status, corrupt = _split_one_msg(rdr, first, name, allow_bare,
                                         keep_cr, mboxrd)
        if corrupt:
            return -1, "corrupt"
        file_done = status
        if not file_done:
            # next message's first line was put back by _split_one_msg
            first = rdr.getwholeline()
            if first is None:
                file_done = True
    return skip, None


def _maildir_filename_cmp_key(name: str):
    """Build a sort key mirroring maildir_filename_cmp (numeric runs compared
    as integers, other chars by byte value)."""
    key: list = []
    for m in re.finditer(r"\d+|\D+", name):
        s = m.group(0)
        if s[0].isdigit():
            key.append((1, int(s)))
        else:
            key.append((0, tuple(ord(c) for c in s)))
    return key


def split_maildir(maildir: Path, out_dir: Path, nr_prec: int, skip: int,
                  keep_cr: bool, mboxrd: bool) -> tuple[int, Optional[str]]:
    """Port of split_maildir(): read cur/ and new/ subdirs, sorted."""
    names: list[str] = []
    for sub in ("cur", "new"):
        d = maildir / sub
        if not d.is_dir():
            continue
        for entry in os.listdir(d):
            if entry.startswith("."):
                continue
            names.append(f"{sub}/{entry}")
    names = sorted(set(names), key=_maildir_filename_cmp_key)
    for rel in names:
        f = maildir / rel
        data = f.read_bytes()
        rdr = _MboxReader(data)
        first = rdr.getwholeline()
        if first is None:
            return -1, "read"
        skip += 1
        name = out_dir / ("%0*d" % (nr_prec, skip))
        status, corrupt = _split_one_msg(rdr, first, name, True, keep_cr, mboxrd)
    return skip, None


# ===========================================================================
# mailinfo (mailinfo.c)
# ===========================================================================

# quoted_cr_action enum
QCR_UNSET = -1
QCR_NOWARN = 0
QCR_WARN = 1
QCR_STRIP = 2

# transfer_encoding enum
TE_DONTCARE = 0
TE_QP = 1
TE_BASE64 = 2

_HEADERS = ("From", "Subject", "Date")


def _isspace(b: int) -> bool:
    return b in (0x20, 0x09, 0x0a, 0x0b, 0x0c, 0x0d)


def _hex2chr(s: bytes) -> int:
    """Decode two hex digits; return -1 on garbage (mirrors hex2chr)."""
    if len(s) < 2:
        return -1
    try:
        hi = int(chr(s[0]), 16)
        lo = int(chr(s[1]), 16)
    except ValueError:
        return -1
    return hi * 16 + lo


class Mailinfo:
    def __init__(self):
        self.name = b""
        self.email = b""
        self.keep_subject = 0
        self.keep_non_patch_brackets_in_subject = 0
        self.quoted_cr = QCR_WARN
        self.add_message_id = 0
        self.use_scissors = 0
        self.use_inbody_headers = 1
        self.metainfo_charset: Optional[str] = "UTF-8"
        self.content: list[Optional[bytes]] = [None] * 5
        self.content_top = 0
        self.charset = b""
        self.format_flowed = 0
        self.delsp = 0
        self.have_quoted_cr = 0
        self.message_id: Optional[bytes] = None
        self.transfer_encoding = TE_DONTCARE
        self.patch_lines = 0
        self.filter_stage = 0
        self.header_stage = 1
        self.inbody_header_accum = b""
        self.p_hdr_data: list[Optional[bytes]] = [None, None, None]
        self.s_hdr_data: list[Optional[bytes]] = [None, None, None]
        self.log_message = b""
        self.input_error = 0
        # output (the "info" file lines)
        self.info_lines: list[bytes] = []
        self.patch_chunks: list[bytes] = []
        # input cursor
        self._data = b""
        self._pos = 0

    # -- low-level input helpers (mirror FILE* with strbuf_getline_lf etc.) ----
    def _getline_lf(self) -> Optional[bytes]:
        """Return next line WITHOUT trailing \n (mirror strbuf_getline_lf);
        None at EOF."""
        if self._pos >= len(self._data):
            return None
        nl = self._data.find(b"\n", self._pos)
        if nl < 0:
            line = self._data[self._pos:]
            self._pos = len(self._data)
            return line
        line = self._data[self._pos : nl]
        self._pos = nl + 1
        return line

    def _getwholeline(self, into: bytes) -> Optional[bytes]:
        if self._pos >= len(self._data):
            return None
        nl = self._data.find(b"\n", self._pos)
        if nl < 0:
            line = self._data[self._pos:]
            self._pos = len(self._data)
            return line
        line = self._data[self._pos : nl + 1]
        self._pos = nl + 1
        return line

    def _peek(self) -> int:
        return self._data[self._pos] if self._pos < len(self._data) else -1


def _cleanup_space(sb: bytes) -> bytes:
    """Port of cleanup_space(): collapse whitespace runs to single spaces."""
    out = bytearray()
    i = 0
    n = len(sb)
    while i < n:
        c = sb[i]
        if _isspace(c):
            out.append(0x20)
            i += 1
            while i < n and _isspace(sb[i]):
                i += 1
        else:
            out.append(c)
            i += 1
    return bytes(out)


def _strpbrk(s: bytes, chars: bytes) -> bool:
    return any(c in s for c in chars)


def _get_sane_name(name: bytes, email: bytes) -> bytes:
    """Port of get_sane_name(): use email when name is empty/too long/has @<>."""
    src = name
    if not name or len(name) > 60 or _strpbrk(name, b"@<>"):
        src = email
    return src


def _parse_bogus_from(mi: Mailinfo, line: bytes) -> None:
    if mi.email:
        return
    bra = line.find(b"<")
    if bra < 0:
        return
    ket = line.find(b">", bra)
    if ket < 0:
        return
    mi.email = line[bra + 1 : ket]
    nm = line[:bra].strip()
    mi.name = _get_sane_name(nm, mi.email)


def _unquote_comment(out: bytearray, s: bytes, i: int) -> int:
    out.append(0x20)
    depth = 1
    n = len(s)
    while i < n:
        c = s[i]
        i += 1
        if c == ord("\\") and i < n:
            out.append(s[i])
            i += 1
            continue
        if c == ord("("):
            depth += 1
        elif c == ord(")"):
            depth -= 1
            if depth == 0:
                break
            out.append(c)
            continue
        out.append(c)
    out.append(0x20)
    return i


def _unquote_quoted_string(out: bytearray, s: bytes, i: int) -> int:
    n = len(s)
    while i < n:
        c = s[i]
        i += 1
        if c == ord("\\") and i < n:
            out.append(s[i])
            i += 1
            continue
        if c == ord('"'):
            break
        if c == ord("("):
            i = _unquote_comment(out, s, i)
            continue
        out.append(c)
    return i


def _unquote_quoted_pair(line: bytes) -> bytes:
    out = bytearray()
    i = 0
    n = len(line)
    while i < n:
        c = line[i]
        i += 1
        if c == ord('"'):
            i = _unquote_quoted_string(out, line, i)
            continue
        if c == ord("("):
            i = _unquote_comment(out, line, i)
            continue
        out.append(c)
    return bytes(out)


def _handle_from(mi: Mailinfo, frm: bytes) -> None:
    f = _unquote_quoted_pair(frm)
    at = f.find(b"@")
    if at < 0:
        _parse_bogus_from(mi, frm)
        return
    if mi.email and f.find(b"@", at + 1) >= 0:
        return
    fa = bytearray(f)
    while at > 0:
        c = fa[at - 1]
        if _isspace(c):
            break
        if c == ord("<"):
            fa[at - 1] = 0x20
            break
        at -= 1
    # el = strcspn(at, " \n\t\r\v\f>")
    el = 0
    delim = b" \n\t\r\x0b\x0c>"
    while at + el < len(fa) and fa[at + el] not in delim:
        el += 1
    mi.email = bytes(fa[at : at + el])
    had_delim = (at + el < len(fa))
    # strbuf_remove(&f, at, el + (at[el] ? 1 : 0))
    remove_len = el + (1 if had_delim else 0)
    del fa[at : at + remove_len]
    f = _cleanup_space(bytes(fa))
    f = f.strip()
    if f[:1] == b"(" and f[-1:] == b")":
        f = f[1:-1]
    mi.name = _get_sane_name(f, mi.email)


def _handle_header(line: bytes) -> bytes:
    return line


def _strcasestr(hay: bytes, needle: bytes) -> int:
    return hay.lower().find(needle.lower())


def _slurp_attr(line: bytes, name: bytes) -> Optional[bytes]:
    idx = _strcasestr(line, name)
    if idx < 0:
        return b""  # found nothing -> empty (but C returns 0/no-set)
    ap = idx + len(name)
    if ap < len(line) and line[ap : ap + 1] == b'"':
        ap += 1
        end_chars = b'"'
    else:
        end_chars = b"; \t"
    sz = 0
    while ap + sz < len(line) and line[ap + sz] not in end_chars:
        sz += 1
    return line[ap : ap + sz]


def _slurp_attr_found(line: bytes, name: bytes) -> tuple[bool, bytes]:
    idx = _strcasestr(line, name)
    if idx < 0:
        return False, b""
    ap = idx + len(name)
    if ap < len(line) and line[ap : ap + 1] == b'"':
        ap += 1
        end_chars = b'"'
    else:
        end_chars = b"; \t"
    sz = 0
    while ap + sz < len(line) and line[ap + sz] not in end_chars:
        sz += 1
    return True, line[ap : ap + sz]


def _has_attr_value(line: bytes, name: bytes, value: bytes) -> bool:
    found, sb = _slurp_attr_found(line, name)
    return found and sb.lower() == value.lower()


def _handle_content_type(mi: Mailinfo, line: bytes) -> None:
    mi.format_flowed = 1 if _has_attr_value(line, b"format=", b"flowed") else 0
    mi.delsp = 1 if _has_attr_value(line, b"delsp=", b"yes") else 0
    found, boundary = _slurp_attr_found(line, b"boundary=")
    if found:
        boundary = b"--" + boundary
        mi.content_top += 1
        if mi.content_top >= 5:
            mi.input_error = -1
            mi.content_top = 4
            return
        mi.content[mi.content_top] = boundary
    found_cs, cs = _slurp_attr_found(line, b"charset=")
    if found_cs:
        mi.charset = cs


def _handle_content_transfer_encoding(mi: Mailinfo, line: bytes) -> None:
    if _strcasestr(line, b"base64") >= 0:
        mi.transfer_encoding = TE_BASE64
    elif _strcasestr(line, b"quoted-printable") >= 0:
        mi.transfer_encoding = TE_QP
    else:
        mi.transfer_encoding = TE_DONTCARE


def _is_multipart_boundary(mi: Mailinfo, line: bytes) -> bool:
    top = mi.content[mi.content_top]
    return top is not None and len(top) <= len(line) and line[: len(top)] == top


def _cleanup_subject(mi: Mailinfo, subject: bytes) -> bytes:
    s = bytearray(subject)
    at = 0
    while at < len(s):
        c = s[at]
        if c in (ord("r"), ord("R")):
            if len(s) <= at + 3:
                break
            if s[at + 1] in (ord("e"), ord("E")) and s[at + 2] == ord(":"):
                del s[at : at + 3]
                continue
            at += 1
            continue
        if c in (ord(" "), ord("\t"), ord(":")):
            del s[at : at + 1]
            continue
        if c == ord("["):
            pos = s.find(b"]", at)
            if pos < 0:
                break
            remove = pos - at + 1
            seg = bytes(s[at : at + remove])
            if (not mi.keep_non_patch_brackets_in_subject
                    or (7 <= remove and b"PATCH" in seg)):
                del s[at : at + remove]
            else:
                at += remove
                if at < len(s) and _isspace(s[at]):
                    at += 1
            continue
        break
    return bytes(s).strip()


def _skip_header(line: bytes, hdr: bytes) -> Optional[bytes]:
    """Port of skip_header(): case-insensitive hdr prefix then ':' then skip
    leading whitespace.  Returns the value bytes or None."""
    if line[: len(hdr)].lower() != hdr.lower():
        return None
    rest = line[len(hdr):]
    if not rest or rest[:1] != b":":
        return None
    rest = rest[1:]
    i = 0
    while i < len(rest) and _isspace(rest[i]):
        i += 1
    return rest[i:]


def _is_format_patch_separator(line: bytes) -> bool:
    SAMPLE = b"From e6807f3efca28b30decfecb1732a56c7db1137ee Mon Sep 17 00:00:00 2001\n"
    if len(line) != len(SAMPLE):
        return False
    if not line.startswith(b"From "):
        return False
    cp = line[5:]
    hexpart = cp[:40]
    if len(hexpart) != 40 or any(ch not in b"0123456789abcdef" for ch in hexpart):
        return False
    return line[45:] == SAMPLE[45:]


def _decode_q_segment(seg: bytes, rfc2047: bool) -> bytes:
    out = bytearray()
    i = 0
    n = len(seg)
    while i < n:
        c = seg[i]
        i += 1
        if c == ord("="):
            d = seg[i] if i < n else 0
            if d == ord("\n") or not d:
                break
            ch = _hex2chr(seg[i : i + 2])
            if ch >= 0:
                out.append(ch)
                i += 2
                continue
            # garbage -- fall through
        if rfc2047 and c == ord("_"):
            c = 0x20
        out.append(c)
    return bytes(out)


def _decode_b_segment(seg: bytes) -> bytes:
    out = bytearray()
    pos = 0
    acc = 0
    for c in seg:
        if c == ord("+"):
            c = 62
        elif c == ord("/"):
            c = 63
        elif ord("A") <= c <= ord("Z"):
            c -= ord("A")
        elif ord("a") <= c <= ord("z"):
            c -= ord("a") - 26
        elif ord("0") <= c <= ord("9"):
            c -= ord("0") - 52
        else:
            continue
        if pos == 0:
            acc = c << 2
            pos = 1
        elif pos == 1:
            out.append((acc | (c >> 4)) & 0xFF)
            acc = (c & 15) << 4
            pos = 2
        elif pos == 2:
            out.append((acc | (c >> 2)) & 0xFF)
            acc = (c & 3) << 6
            pos = 3
        elif pos == 3:
            out.append((acc | c) & 0xFF)
            acc = 0
            pos = 0
    return bytes(out)


def _same_encoding(a: str, b: str) -> bool:
    def norm(s: str) -> str:
        return s.replace("-", "").replace("_", "").lower()
    if norm(a) == norm(b):
        return True
    utf8 = {"utf8", "utf"}
    return norm(a) in utf8 and norm(b) in utf8


def _convert_to_utf8(mi: Mailinfo, line: bytes, charset: bytes) -> bytes:
    if not mi.metainfo_charset or not charset:
        return line
    cs = charset.decode("ascii", "replace")
    if _same_encoding(mi.metainfo_charset, cs):
        return line
    try:
        decoded = line.decode(cs)
    except (LookupError, UnicodeDecodeError):
        mi.input_error = -1
        return line
    try:
        return decoded.encode(mi.metainfo_charset)
    except (LookupError, UnicodeEncodeError):
        mi.input_error = -1
        return line


def _decode_header(mi: Mailinfo, it: bytes) -> bytes:
    out = bytearray()
    in_buf = it
    pos = 0
    n = len(it)
    while pos <= n:
        ep = in_buf.find(b"=?", pos)
        if ep < 0:
            break
        if pos != ep:
            # something before the encoded word
            scan = pos
            while scan < ep and _isspace(in_buf[scan]):
                scan += 1
            if scan != ep or pos == 0:
                out += in_buf[pos:ep]
        ep += 2
        if ep >= n:
            mi.input_error = -1
            return it
        q = in_buf.find(b"?", ep)
        if q < 0:
            mi.input_error = -1
            return it
        if q + 3 > n:
            mi.input_error = -1
            return it
        charset_q = in_buf[ep:q]
        encoding = in_buf[q + 1] if q + 1 < n else 0
        if not encoding or in_buf[q + 2 : q + 3] != b"?":
            mi.input_error = -1
            return it
        ep2 = in_buf.find(b"?=", q + 3)
        if ep2 < 0:
            mi.input_error = -1
            return it
        piece = in_buf[q + 3 : ep2]
        enc = chr(encoding).lower()
        if enc == "b":
            dec = _decode_b_segment(piece)
        elif enc == "q":
            dec = _decode_q_segment(piece, True)
        else:
            mi.input_error = -1
            return it
        dec = _convert_to_utf8(mi, dec, charset_q)
        out += dec
        pos = ep2 + 2
    out += in_buf[pos:]
    return bytes(out)


def _parse_header(line: bytes, hdr: bytes, mi: Mailinfo) -> Optional[bytes]:
    val = _skip_header(line, hdr)
    if val is None:
        return None
    return _decode_header(mi, val)


def _check_header(mi: Mailinfo, line: bytes, hdr_data: list, overwrite: bool) -> bool:
    for i, h in enumerate(_HEADERS):
        if (hdr_data[i] is None or overwrite):
            v = _parse_header(line, h.encode(), mi)
            if v is not None:
                hdr_data[i] = v
                return True
    v = _parse_header(line, b"Content-Type", mi)
    if v is not None:
        _handle_content_type(mi, v)
        return True
    v = _parse_header(line, b"Content-Transfer-Encoding", mi)
    if v is not None:
        _handle_content_transfer_encoding(mi, v)
        return True
    v = _parse_header(line, b"Message-ID", mi)
    if v is not None:
        if mi.add_message_id:
            mi.message_id = v
        return True
    return False


def _is_inbody_header(mi: Mailinfo, line: bytes) -> bool:
    for i, h in enumerate(_HEADERS):
        if mi.s_hdr_data[i] is None and _skip_header(line, h.encode()) is not None:
            return True
    return False


def _decode_transfer_encoding(mi: Mailinfo, line: bytes) -> bytes:
    if mi.transfer_encoding == TE_QP:
        return _decode_q_segment(line, False)
    if mi.transfer_encoding == TE_BASE64:
        return _decode_b_segment(line)
    return line


def _patchbreak(line: bytes) -> bool:
    if line.startswith(b"diff -"):
        return True
    if line.startswith(b"Index: "):
        return True
    if len(line) < 4:
        return False
    if line.startswith(b"---"):
        if line[3:4] == b" " and not _isspace(line[4]):
            return True
        i = 3
        while i < len(line):
            c = line[i]
            if c == ord("\n"):
                return True
            if not _isspace(c):
                break
            i += 1
        return False
    return False


def _is_scissors_line(line: bytes) -> bool:
    scissors = 0
    gap = 0
    first_nonblank = -1
    last_nonblank = -1
    perforation = 0
    in_perforation = 0
    i = 0
    n = len(line)
    while i < n:
        c = line[i]
        if _isspace(c):
            if in_perforation:
                perforation += 1
                gap += 1
            i += 1
            continue
        last_nonblank = i
        if first_nonblank < 0:
            first_nonblank = i
        if c == ord("-"):
            in_perforation = 1
            perforation += 1
            i += 1
            continue
        two = line[i : i + 2]
        if two in (b">8", b"8<", b">%", b"%<"):
            in_perforation = 1
            perforation += 2
            scissors += 2
            i += 2
            continue
        in_perforation = 0
        i += 1
    if first_nonblank >= 0 and last_nonblank >= 0:
        visible = last_nonblank - first_nonblank + 1
    else:
        visible = 0
    return bool(scissors and 8 <= visible and visible < perforation * 3
                and gap * 2 < perforation)


def _flush_inbody_header_accum(mi: Mailinfo) -> None:
    if not mi.inbody_header_accum:
        return
    _check_header(mi, mi.inbody_header_accum, mi.s_hdr_data, False)
    mi.inbody_header_accum = b""


def _check_inbody_header(mi: Mailinfo, line: bytes) -> bool:
    if mi.inbody_header_accum and line[:1] in (b" ", b"\t"):
        if mi.use_scissors and _is_scissors_line(line):
            _flush_inbody_header_accum(mi)
            return False
        if mi.inbody_header_accum.endswith(b"\n"):
            mi.inbody_header_accum = mi.inbody_header_accum[:-1]
        mi.inbody_header_accum += line
        return True
    _flush_inbody_header_accum(mi)
    if line.startswith(b">From") and len(line) > 5 and _isspace(line[5]):
        return _is_format_patch_separator(line[1:])
    if line.startswith(b"[PATCH]") and len(line) > 7 and _isspace(line[7]):
        for i, h in enumerate(_HEADERS):
            if h == "Subject":
                mi.s_hdr_data[i] = line
                return True
        return False
    if _is_inbody_header(mi, line):
        mi.inbody_header_accum += line
        return True
    return False


def _handle_commit_msg(mi: Mailinfo, line: bytes) -> bool:
    """Returns True when the patch body has started (line should go to patch)."""
    if mi.header_stage:
        if not line or (len(line) == 1 and line[0] == ord("\n")):
            if mi.inbody_header_accum:
                _flush_inbody_header_accum(mi)
                mi.header_stage = 0
            return False
    if mi.use_inbody_headers and mi.header_stage:
        mi.header_stage = 1 if _check_inbody_header(mi, line) else 0
        if mi.header_stage:
            return False
    else:
        mi.header_stage = 0
    line = _convert_to_utf8(mi, line, mi.charset)
    if mi.input_error:
        return False
    if mi.use_scissors and _is_scissors_line(line):
        mi.log_message = b""
        mi.header_stage = 1
        for i in range(len(_HEADERS)):
            mi.s_hdr_data[i] = None
        return False
    if _patchbreak(line):
        if mi.message_id:
            mi.log_message += b"Message-ID: " + mi.message_id + b"\n"
        return True
    mi.log_message += line
    return False


def _handle_patch(mi: Mailinfo, line: bytes) -> None:
    mi.patch_chunks.append(line)
    mi.patch_lines += 1


def _handle_filter(mi: Mailinfo, line: bytes) -> None:
    if mi.filter_stage == 0:
        if not _handle_commit_msg(mi, line):
            return
        mi.filter_stage += 1
    _handle_patch(mi, line)


def _is_rfc2822_header(line: bytes) -> bool:
    if line.startswith(b"From ") or line.startswith(b">From "):
        return True
    for ch in line:
        if ch == ord(":"):
            return True
        if 33 <= ch <= 57 or 59 <= ch <= 126:
            continue
        break
    return False


def _read_one_header_line(mi: Mailinfo) -> Optional[bytes]:
    """Port of read_one_header_line(). Returns the (folded) header line WITHOUT
    trailing whitespace, or None when the next line is not a header (in which
    case the line is left buffered for handle_body via mi._pushback)."""
    line = mi._getline_lf()
    if line is None:
        mi._pushback = None
        return None
    # strbuf_rtrim
    line = line.rstrip(b" \t\r\n\x0b\x0c")
    if not line or not _is_rfc2822_header(line):
        mi._pushback = line + b"\n"
        return None
    # eat continuation lines
    while True:
        peek = mi._peek()
        if peek == -1:
            break
        if peek != ord(" ") and peek != ord("\t"):
            break
        cont = mi._getline_lf()
        if cont is None:
            break
        cont = bytearray(cont)
        cont[0] = 0x20
        cont = bytes(cont).rstrip(b" \t\r\n\x0b\x0c")
        line += cont
    mi._pushback = None
    return line


def _find_boundary(mi: Mailinfo) -> Optional[bytes]:
    while True:
        line = mi._getline_lf()
        if line is None:
            return None
        line = line + b"\n" if mi._had_nl else line
        # getline_lf strips \n; we need the raw for boundary check, but
        # is_multipart_boundary compares prefix only, so add nothing.
        if mi.content[mi.content_top] is not None and _is_multipart_boundary(mi, line):
            return line


def _handle_filter_flowed(mi: Mailinfo, line: bytes, prev: bytearray) -> None:
    n = len(line)
    if not mi.format_flowed:
        if n >= 2 and line[n - 2] == ord("\r") and line[n - 1] == ord("\n"):
            mi.have_quoted_cr = 1
            if mi.quoted_cr == QCR_STRIP:
                line = line[: n - 2] + b"\n"
        _handle_filter(mi, line)
        return
    ln = n
    if line[ln - 1 : ln] == b"\n":
        ln -= 1
        if ln and line[ln - 1 : ln] == b"\r":
            ln -= 1
    # signature separator "-- "
    if line.startswith(b"-- ") and ln == 3:
        if prev:
            _handle_filter(mi, bytes(prev))
            prev.clear()
        _handle_filter(mi, line)
        return
    if ln and line[:1] == b" ":
        line = line[1:]
        ln -= 1
    if ln and line[ln - 1 : ln] == b" ":
        prev += line[: ln - (1 if mi.delsp else 0)]
        return
    line = bytes(prev) + line
    prev.clear()
    _handle_filter(mi, line)


def _split_keep_nl(buf: bytes) -> list[bytes]:
    out = []
    i = 0
    n = len(buf)
    while i < n:
        nl = buf.find(b"\n", i)
        if nl < 0:
            out.append(buf[i:])
            break
        out.append(buf[i : nl + 1])
        i = nl + 1
    return out


def _handle_boundary(mi: Mailinfo) -> bool:
    """Process a boundary line currently in mi._cur_line. Returns False to end
    body, True to continue (mi._cur_line replenished)."""
    newline = b"\n"
    while True:
        line = mi._cur_line
        top = mi.content[mi.content_top]
        if (len(line) >= len(top) + 2
                and line[len(top) : len(top) + 2] == b"--"):
            # end boundary: pop
            mi.content[mi.content_top] = None
            mi.content_top -= 1
            if mi.content_top < 0:
                mi.input_error = -1
                mi.content_top = 0
                return False
            _handle_filter(mi, newline)
            if mi.input_error:
                return False
            nb = _find_boundary(mi)
            if nb is None:
                return False
            mi._cur_line = nb
            continue
        # new section
        mi.transfer_encoding = TE_DONTCARE
        mi.charset = b""
        # slurp section headers
        while True:
            hline = _read_one_header_line(mi)
            if hline is None:
                break
            _check_header(mi, hline, mi.p_hdr_data, False)
        # the non-header line left over is in mi._pushback
        nxt = mi._pushback
        mi._pushback = None
        if nxt is None:
            # replenish line via getwholeline
            wl = mi._getwholeline(b"")
            if wl is None:
                return False
            mi._cur_line = wl
            return True
        # _read_one_header_line stored a line+\n pushback; use as the body line
        mi._cur_line = nxt
        return True


def _summarize_quoted_cr(mi: Mailinfo) -> None:
    # warnings go to stderr; recorded via input_error? No -- warning only.
    if mi.have_quoted_cr and mi.quoted_cr == QCR_WARN:
        mi._warn_quoted_cr = True


def _handle_body(mi: Mailinfo) -> None:
    prev = bytearray()
    # Skip up to first boundary
    if mi.content[mi.content_top] is not None:
        nb = _find_boundary(mi)
        if nb is None:
            return
        mi._cur_line = nb
    else:
        # the leftover line from header parsing is in mi._pushback
        first = mi._pushback
        mi._pushback = None
        if first is None:
            first = mi._getwholeline(b"")
        if first is None:
            return
        mi._cur_line = first

    while True:
        line = mi._cur_line
        if mi.content[mi.content_top] is not None and _is_multipart_boundary(mi, line):
            if prev:
                _handle_filter(mi, bytes(prev))
                prev.clear()
            _summarize_quoted_cr(mi)
            mi.have_quoted_cr = 0
            if not _handle_boundary(mi):
                break
            continue
        decoded = _decode_transfer_encoding(mi, line)
        if mi.transfer_encoding in (TE_BASE64, TE_QP):
            decoded = bytes(prev) + decoded
            prev.clear()
            lines = _split_keep_nl(decoded)
            for idx, sb in enumerate(lines):
                is_last = (idx == len(lines) - 1)
                if is_last and (not sb or sb[-1:] != b"\n"):
                    prev += sb
                    break
                _handle_filter_flowed(mi, sb, prev)
        else:
            _handle_filter_flowed(mi, decoded, prev)
        if mi.input_error:
            break
        nxt = mi._getwholeline(b"")
        if nxt is None:
            break
        mi._cur_line = nxt

    if prev:
        _handle_filter(mi, bytes(prev))
    _summarize_quoted_cr(mi)
    _flush_inbody_header_accum(mi)


def _output_header_lines(out: list, hdr: bytes, data: bytes) -> None:
    sp = 0
    while True:
        ep = data.find(b"\n", sp)
        if ep < 0:
            seg = data[sp:]
        else:
            seg = data[sp:ep]
        out.append(hdr + b": " + seg)
        if ep < 0:
            break
        sp = ep + 1


def _handle_info(mi: Mailinfo) -> None:
    for i, h in enumerate(_HEADERS):
        hb = h.encode()
        if mi.patch_lines and mi.s_hdr_data[i] is not None:
            hdr = mi.s_hdr_data[i]
        elif mi.p_hdr_data[i] is not None:
            hdr = mi.p_hdr_data[i]
        else:
            continue
        if b"\0" in hdr:
            mi.input_error = -1
        if h == "Subject":
            if not mi.keep_subject:
                hdr = _cleanup_subject(mi, hdr)
                hdr = _cleanup_space(hdr)
            _output_header_lines(mi.info_lines, b"Subject", hdr)
        elif h == "From":
            hdr = _cleanup_space(hdr)
            _handle_from(mi, hdr)
            mi.info_lines.append(b"Author: " + mi.name)
            mi.info_lines.append(b"Email: " + mi.email)
        else:
            hdr = _cleanup_space(hdr)
            mi.info_lines.append(hb + b": " + hdr)
    mi.info_lines.append(b"")  # trailing blank line (the fprintf "\n")


def run_mailinfo(mi: Mailinfo, data: bytes) -> tuple[Optional[bytes], Optional[bytes], Optional[bytes]]:
    """Run mailinfo over ``data``. Returns (msg_bytes, patch_bytes, info_bytes)
    or (None, None, None) with mi.input_error set on an empty patch."""
    mi._data = data
    mi._pos = 0
    mi._pushback = None
    mi._had_nl = True
    mi._cur_line = b""
    mi._warn_quoted_cr = False

    # skip leading whitespace; EOF -> empty patch error
    while mi._pos < len(data) and _isspace(data[mi._pos]):
        mi._pos += 1
    if mi._pos >= len(data):
        return None, None, None  # empty patch

    # email headers
    while True:
        line = _read_one_header_line(mi)
        if line is None:
            break
        _check_header(mi, line, mi.p_hdr_data, True)

    _handle_body(mi)

    msg = mi.log_message
    patch = b"".join(mi.patch_chunks)
    _handle_info(mi)
    info = b"\n".join(mi.info_lines) + b"\n" if mi.info_lines else b"\n"
    # info_lines ends with an empty entry, producing trailing "\n\n"; emulate
    # the C output which is each "X: v\n" then a final "\n".
    info = b"".join(l + b"\n" for l in mi.info_lines)
    return msg, patch, info


# ===========================================================================
# shell-quote helpers (quote.c) for the author-script / apply-opt state files
# ===========================================================================


def sq_quote(src: str) -> str:
    """Port of sq_quote_buf(): single-quote ``src`` for safe shell eval."""
    out = ["'"]
    for ch in src:
        if ch == "'" or ch == "!":
            out.append("'\\" + ch + "'")
        else:
            out.append(ch)
    out.append("'")
    return "".join(out)


def sq_dequote(s: str) -> Optional[str]:
    """Port of sq_dequote(): undo sq_quote.  Returns None on malformed input."""
    s = s.strip()
    if not s or s[0] != "'":
        return None
    out = []
    i = 1
    n = len(s)
    while i < n:
        c = s[i]
        if c == "'":
            # end, unless followed by an escaped quote: '\'' or '\!'
            if i + 1 >= n:
                return "".join(out)
            if s[i + 1] != "\\":
                # trailing garbage after closing quote -> malformed
                return None
            # '\X' sequence: next is backslash, then the literal, then '
            if i + 3 >= n + 1 or i + 2 >= n:
                return None
            ch = s[i + 2]
            if i + 3 >= n or s[i + 3] != "'":
                return None
            out.append(ch)
            i += 4
            continue
        out.append(c)
        i += 1
    # no closing quote
    return None


# ===========================================================================
# patch format detection (builtin/am.c detect_patch_format / is_mail)
# ===========================================================================

PATCH_FORMAT_UNKNOWN = 0
PATCH_FORMAT_MBOX = 1
PATCH_FORMAT_STGIT = 2
PATCH_FORMAT_STGIT_SERIES = 3
PATCH_FORMAT_HG = 4
PATCH_FORMAT_MBOXRD = 5

_HEADER_RE = re.compile(rb"^[!-9;-~]+:")


def _is_mail(lines: list[bytes]) -> bool:
    """Port of is_mail(): every non-folded line up to the first blank must look
    like an RFC2822 header."""
    for line in lines:
        if not line:
            break
        if line[:1] in (b"\t", b" "):
            continue
        if not _HEADER_RE.match(line):
            return False
    return True


def detect_patch_format(data: bytes, is_stdin_or_dir: bool) -> int:
    """Port of detect_patch_format(). ``data`` is the first file's bytes."""
    if is_stdin_or_dir:
        return PATCH_FORMAT_MBOX
    # split into logical lines (strip trailing \r\n)
    raw = data.split(b"\n")
    # find first non-blank line (strbuf_getline strips trailing \r too)
    def _strip(b: bytes) -> bytes:
        return b.rstrip(b"\r")
    idx = 0
    l1 = b""
    while idx < len(raw):
        cand = _strip(raw[idx])
        idx += 1
        if cand:
            l1 = cand
            break
    if l1.startswith(b"From ") or l1.startswith(b"From: "):
        return PATCH_FORMAT_MBOX
    if l1.startswith(b"# This series applies on GIT commit"):
        return PATCH_FORMAT_STGIT_SERIES
    if l1 == b"# HG changeset patch":
        return PATCH_FORMAT_HG
    l2 = _strip(raw[idx]) if idx < len(raw) else b""
    l3 = _strip(raw[idx + 1]) if idx + 1 < len(raw) else b""
    if l1 and not l2 and (l3.startswith(b"From:") or l3.startswith(b"Author:")
                          or l3.startswith(b"Date:")):
        return PATCH_FORMAT_STGIT
    if l1:
        # is_mail() scans from the current position (after l1) in C, since
        # detect already consumed l1/l2/l3.  Reproduce: feed the remaining
        # lines (from where the file cursor is, i.e. after l3) — but C's is_mail
        # fseeks to 0 and rescans the whole file.
        rest = [_strip(x) for x in data.split(b"\n")]
        if _is_mail(rest):
            return PATCH_FORMAT_MBOX
    return PATCH_FORMAT_UNKNOWN
