"""Pure-Python reftable backend (Git 2.54 on-disk format).

This is a byte-faithful port of git's ``reftable/`` library: the block,
record, varint and footer encodings (reftable/writer.c, block.c, record.c,
basics.c and Documentation/technical/reftable.adoc), plus enough of the
stack/backend logic (reftable/stack.c, refs/reftable-backend.c) to read and
write references the way C Git does.

A reftable repository stores its refs in ``.git/reftable/`` rather than in
loose files + ``packed-refs``:

  * ``.git/refs`` is a directory containing a single stub file ``heads`` with
    the text "this repository uses the reftable format\n".
  * ``.git/reftable/tables.list`` lists the active table file names, newest
    last, one per line (newline-terminated).
  * each table is ``0x%012x-0x%012x-%08x.ref`` (min/max update_index + a
    random suffix) holding a header, ref/obj/log blocks and a CRC32 footer.

The writer is byte-exact against the oracle for the cases git itself produces
(small repos: a single ref block and optional single log block, never an
index/obj block until there are more than three ref blocks); the index and
object-block machinery is ported faithfully so larger tables encode the same
way even though git only triggers them past that threshold.
"""
from __future__ import annotations

import os
import struct
import zlib
from pathlib import Path
from typing import Optional

# Block type bytes.
BLOCK_TYPE_REF = ord("r")
BLOCK_TYPE_OBJ = ord("o")
BLOCK_TYPE_LOG = ord("g")
BLOCK_TYPE_INDEX = ord("i")

# Ref record value types (low 3 bits of the key's extra field).
REF_DELETION = 0x0
REF_VAL1 = 0x1
REF_VAL2 = 0x2
REF_SYMREF = 0x3

# Log record value types.
LOG_DELETION = 0x0
LOG_UPDATE = 0x1

DEFAULT_BLOCK_SIZE = 4096
DEFAULT_RESTART_INTERVAL = 16
MAX_RESTARTS = (1 << 16) - 1

HEADER_SIZE_V1 = 24
HEADER_SIZE_V2 = 28
FOOTER_SIZE_V1 = 68
FOOTER_SIZE_V2 = 72

STUB_TEXT = "this repository uses the reftable format\n"
HEAD_STUB_TEXT = "ref: refs/heads/.invalid\n"


# --------------------------------------------------------------------------
# varint (identical to pack ofs-delta encoding; reftable/record.c)
# --------------------------------------------------------------------------
def put_var_int(value: int) -> bytes:
    """Encode ``value`` as a reftable varint (big-endian, continuation bit)."""
    out = bytearray()
    out.append(value & 0x7F)
    value >>= 7
    while value:
        value -= 1
        out.append(0x80 | (value & 0x7F))
        value >>= 7
    out.reverse()
    return bytes(out)


def get_var_int(data: bytes, pos: int) -> "tuple[int, int]":
    """Decode a varint at ``pos``; return ``(value, new_pos)``."""
    c = data[pos]
    pos += 1
    val = c & 0x7F
    while c & 0x80:
        val += 1
        c = data[pos]
        pos += 1
        val = (val << 7) + (c & 0x7F)
    return val, pos


def _common_prefix(a: bytes, b: bytes) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def _be24(value: int) -> bytes:
    return bytes(((value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF))


def encode_key(prev_key: bytes, key: bytes, extra: int) -> "tuple[bytes, bool]":
    """Prefix-compress ``key`` against ``prev_key``; return (bytes, is_restart).

    Mirrors reftable_encode_key: a record is a restart point iff prefix_len==0.
    """
    prefix_len = _common_prefix(prev_key, key)
    suffix = key[prefix_len:]
    out = bytearray()
    out += put_var_int(prefix_len)
    out += put_var_int((len(suffix) << 3) | (extra & 0x7))
    out += suffix
    return bytes(out), (prefix_len == 0)


def encode_string(s: bytes) -> bytes:
    return put_var_int(len(s)) + s


def decode_string(data: bytes, pos: int) -> "tuple[bytes, int]":
    n, pos = get_var_int(data, pos)
    return data[pos:pos + n], pos + n


# --------------------------------------------------------------------------
# records
# --------------------------------------------------------------------------
class RefRecord:
    """A single reference (reftable_ref_record)."""

    __slots__ = ("refname", "update_index", "value_type", "value", "target")

    def __init__(self, refname: str, update_index: int, value_type: int,
                 value=None, target: Optional[str] = None):
        self.refname = refname
        self.update_index = update_index
        self.value_type = value_type
        # value: for VAL1 a 1-element bytes hash; for VAL2 (value, peeled).
        self.value = value
        self.target = target  # for SYMREF (str)

    def key(self) -> bytes:
        return self.refname.encode("utf-8")

    def val_type(self) -> int:
        return self.value_type

    def encode_value(self, min_update_index: int, hash_size: int) -> bytes:
        out = bytearray()
        out += put_var_int(self.update_index - min_update_index)
        if self.value_type == REF_SYMREF:
            out += encode_string(self.target.encode("utf-8"))
        elif self.value_type == REF_VAL2:
            out += self.value[0]
            out += self.value[1]
        elif self.value_type == REF_VAL1:
            out += self.value
        # REF_DELETION: nothing.
        return bytes(out)


class LogRecord:
    """A single reflog line (reftable_log_record)."""

    __slots__ = ("refname", "update_index", "value_type", "old_hash",
                 "new_hash", "name", "email", "time", "tz_offset", "message")

    def __init__(self, refname, update_index, value_type=LOG_UPDATE,
                 old_hash=b"", new_hash=b"", name="", email="", time=0,
                 tz_offset=0, message=""):
        self.refname = refname
        self.update_index = update_index
        self.value_type = value_type
        self.old_hash = old_hash
        self.new_hash = new_hash
        self.name = name
        self.email = email
        self.time = time
        self.tz_offset = tz_offset
        self.message = message

    def key(self) -> bytes:
        # refname '\0' reverse_int64(update_index)
        ts = (0xFFFFFFFFFFFFFFFF - self.update_index) & 0xFFFFFFFFFFFFFFFF
        return self.refname.encode("utf-8") + b"\0" + struct.pack(">Q", ts)

    def val_type(self) -> int:
        return self.value_type

    def encode_value(self, hash_size: int) -> bytes:
        if self.value_type == LOG_DELETION:
            return b""
        out = bytearray()
        out += self.old_hash
        out += self.new_hash
        out += encode_string((self.name or "").encode("utf-8"))
        out += encode_string((self.email or "").encode("utf-8"))
        out += put_var_int(self.time)
        out += struct.pack(">h", self.tz_offset)
        out += encode_string((self.message or "").encode("utf-8"))
        return bytes(out)


class ObjRecord:
    """An object -> ref-block-position mapping (reftable_obj_record)."""

    __slots__ = ("hash_prefix", "offsets")

    def __init__(self, hash_prefix: bytes, offsets: "list[int]"):
        self.hash_prefix = hash_prefix
        self.offsets = offsets

    def key(self) -> bytes:
        return self.hash_prefix

    def val_type(self) -> int:
        n = len(self.offsets)
        return n if 0 < n < 8 else 0

    def encode_value(self, hash_size: int) -> bytes:
        out = bytearray()
        n = len(self.offsets)
        if n == 0 or n >= 8:
            out += put_var_int(n)
        if n == 0:
            return bytes(out)
        out += put_var_int(self.offsets[0])
        last = self.offsets[0]
        for off in self.offsets[1:]:
            out += put_var_int(off - last)
            last = off
        return bytes(out)


class IndexRecord:
    __slots__ = ("last_key", "offset")

    def __init__(self, last_key: bytes, offset: int):
        self.last_key = last_key
        self.offset = offset

    def key(self) -> bytes:
        return self.last_key

    def val_type(self) -> int:
        return 0

    def encode_value(self, hash_size: int) -> bytes:
        return put_var_int(self.offset)


# --------------------------------------------------------------------------
# block writer (reftable/block.c)
# --------------------------------------------------------------------------
class _BlockWriter:
    def __init__(self, typ: int, block_size: int, header_off: int,
                 hash_size: int, restart_interval: int):
        self.typ = typ
        self.block_size = block_size
        self.header_off = header_off
        self.hash_size = hash_size
        self.restart_interval = restart_interval
        # buffer holding [header_off..] of the block; index 0 == start of file.
        self.buf = bytearray()
        self.next = header_off + 4  # type byte + 3-byte block_len
        self.entries = 0
        self.restarts: list[int] = []
        self.last_key = b""

    def _encode_record(self, rec, min_update_index: int) -> "tuple[bytes, bool]":
        last = b"" if (self.entries % self.restart_interval == 0) else self.last_key
        key = rec.key()
        keypart, is_restart = encode_key(last, key, rec.val_type())
        if self.typ == BLOCK_TYPE_LOG:
            valpart = rec.encode_value(self.hash_size)
        elif self.typ == BLOCK_TYPE_REF:
            valpart = rec.encode_value(min_update_index, self.hash_size)
        else:
            valpart = rec.encode_value(self.hash_size)
        return keypart + valpart, is_restart, key

    def add(self, rec, min_update_index: int) -> bool:
        """Append ``rec``; return False if it would overflow the block."""
        encoded, is_restart, key = self._encode_record(rec, min_update_index)
        n = len(encoded)
        rlen = len(self.restarts)
        if rlen >= MAX_RESTARTS:
            is_restart = False
        if is_restart:
            rlen += 1
        # block_writer_register_restart's bound check.
        if 2 + 3 * rlen + n > self.block_size - self.next:
            return False
        if is_restart:
            self.restarts.append(self.next)
        self.buf += encoded
        self.next += n
        self.last_key = key
        self.entries += 1
        return True

    def finish(self) -> bytes:
        """Serialise the full block including any leading file-header slot.

        The returned bytes start at the block's position in the file, so the
        first ``header_off`` bytes are a placeholder for the file header (the
        caller overwrites them on the first block). ``block_len`` is measured
        from the start of the file, hence includes ``header_off``.
        """
        body = bytearray()
        body += self.buf
        for r in self.restarts:
            body += _be24(r)
        body += struct.pack(">H", len(self.restarts))
        block_len = self.header_off + 4 + len(self.buf) + 3 * len(self.restarts) + 2
        raw = bytearray(self.header_off)  # placeholder for the file header
        raw.append(self.typ)
        raw += _be24(block_len)
        raw += body
        if self.typ == BLOCK_TYPE_LOG:
            # Log blocks are never the first block (header_off==0). zlib-deflate
            # everything after the 4-byte block header (level 9).
            comp = _deflate9(bytes(raw[4:]))
            return bytes(raw[:4]) + comp
        return bytes(raw)

    def block_len(self) -> int:
        return self.header_off + 4 + len(self.buf) + 3 * len(self.restarts) + 2

    def raw_uncompressed_len(self) -> int:
        """Inflated size including the 4-byte header (the log block_len)."""
        return self.header_off + 4 + len(self.buf) + 3 * len(self.restarts) + 2


def _deflate9(data: bytes) -> bytes:
    c = zlib.compressobj(9)
    return c.compress(data) + c.flush()


# --------------------------------------------------------------------------
# table writer (reftable/writer.c)
# --------------------------------------------------------------------------
class Writer:
    def __init__(self, min_update_index: int, max_update_index: int, *,
                 hash_size: int = 20, block_size: int = DEFAULT_BLOCK_SIZE,
                 restart_interval: int = DEFAULT_RESTART_INTERVAL):
        self.min_update_index = min_update_index
        self.max_update_index = max_update_index
        self.hash_size = hash_size
        self.block_size = block_size
        self.restart_interval = restart_interval
        self.version = 1 if hash_size == 20 else 2
        self.out = bytearray()
        self.next = 0
        self.pending_padding = 0
        # per-section stats (offset, index_offset, blocks)
        self.ref_offset = 0
        self.ref_index_offset = 0
        self.ref_blocks = 0
        self.ref_index_blocks = 0
        self.obj_offset = 0
        self.obj_index_offset = 0
        self.obj_blocks = 0
        self.log_offset = 0
        self.log_index_offset = 0
        self.object_id_len = 0
        self._index: list[IndexRecord] = []
        self.block_writer: Optional[_BlockWriter] = None
        self._cur_type = 0
        # obj index: hash(full bytes) -> list of ref block offsets
        self._obj_index: dict[bytes, list[int]] = {}
        self._pending_ref_block_off = 0

    # -- header / footer ----------------------------------------------------
    def _header_size(self) -> int:
        return HEADER_SIZE_V1 if self.version == 1 else HEADER_SIZE_V2

    def _footer_size(self) -> int:
        return FOOTER_SIZE_V1 if self.version == 1 else FOOTER_SIZE_V2

    def _write_header_bytes(self) -> bytes:
        h = bytearray()
        h += b"REFT"
        h.append(self.version)
        h += _be24(self.block_size)
        h += struct.pack(">Q", self.min_update_index)
        h += struct.pack(">Q", self.max_update_index)
        if self.version == 2:
            hash_id = b"sha1" if self.hash_size == 20 else b"s256"
            h += hash_id
        return bytes(h)

    # -- low-level padded writer (reftable/writer.c padded_write) -----------
    def _padded_write(self, data: bytes, padding: int) -> None:
        if self.pending_padding > 0:
            self.out += b"\0" * self.pending_padding
            self.pending_padding = 0
        self.pending_padding = padding
        self.out += data

    # -- block management ---------------------------------------------------
    def _reinit_block_writer(self, typ: int) -> None:
        header_off = self._header_size() if self.next == 0 else 0
        self.block_writer = _BlockWriter(typ, self.block_size, header_off,
                                         self.hash_size, self.restart_interval)
        self._cur_type = typ

    def _flush_nonempty_block(self) -> None:
        bw = self.block_writer
        typ = bw.typ
        serialized = bw.finish()
        raw_bytes = len(serialized)
        # By default, all blocks except log are padded to block_size.
        padding = 0
        if typ != BLOCK_TYPE_LOG:
            padding = self.block_size - raw_bytes
        # section offset = position of first block of this type
        if self._section_blocks(typ) == 0:
            off = self.next
            if off > 0:
                self._set_section_offset(typ, off)
        self._inc_section_blocks(typ)
        # If this is the very first block, prepend the file header.
        if self.next == 0:
            # The block_writer already reserved header_off bytes at the front;
            # overwrite them with the real header.
            header = self._write_header_bytes()
            serialized = header + serialized[len(header):]
        self._padded_write(serialized, padding)
        # record an index entry for this block
        self._index.append(IndexRecord(bw.last_key, self.next))
        self.next += padding + raw_bytes
        self.block_writer = None

    def _flush_block(self) -> None:
        if self.block_writer is None:
            return
        if self.block_writer.entries == 0:
            self.block_writer = None
            return
        self._flush_nonempty_block()

    def _section_blocks(self, typ: int) -> int:
        return {BLOCK_TYPE_REF: self.ref_blocks, BLOCK_TYPE_OBJ: self.obj_blocks,
                BLOCK_TYPE_INDEX: 0, BLOCK_TYPE_LOG: 0}.get(typ, 0)

    def _inc_section_blocks(self, typ: int) -> None:
        if typ == BLOCK_TYPE_REF:
            self.ref_blocks += 1
        elif typ == BLOCK_TYPE_OBJ:
            self.obj_blocks += 1

    def _set_section_offset(self, typ: int, off: int) -> None:
        if typ == BLOCK_TYPE_REF:
            self.ref_offset = off
        elif typ == BLOCK_TYPE_OBJ:
            self.obj_offset = off
        elif typ == BLOCK_TYPE_LOG:
            self.log_offset = off

    def _finish_section(self, typ: int) -> int:
        """Flush current block, then write any needed index. Returns index_off."""
        self._flush_block()
        threshold = 3
        index_start = 0
        while len(self._index) > threshold:
            index_start = self.next
            self._reinit_block_writer(BLOCK_TYPE_INDEX)
            idx = self._index
            self._index = []
            for rec in idx:
                if not self.block_writer.add(rec, self.min_update_index):
                    self._flush_block()
                    self._reinit_block_writer(BLOCK_TYPE_INDEX)
                    self.block_writer.add(rec, self.min_update_index)
            self._flush_block()
        index_blocks = 0 if index_start == 0 else 1
        self._index = []
        return index_start, index_blocks

    # -- public add ---------------------------------------------------------
    def add_ref(self, rec: RefRecord) -> None:
        if self.block_writer is None or self._cur_type != BLOCK_TYPE_REF:
            self._reinit_block_writer(BLOCK_TYPE_REF)
        block_off = self.next  # offset the current ref block will live at
        if not self.block_writer.add(rec, self.min_update_index):
            self._flush_block()
            self._reinit_block_writer(BLOCK_TYPE_REF)
            block_off = self.next
            self.block_writer.add(rec, self.min_update_index)
        # Track object -> ref-block offset for the obj index (only materialised
        # into obj blocks when a ref index is written, i.e. >3 ref blocks).
        if rec.value_type in (REF_VAL1, REF_VAL2):
            h = rec.value if rec.value_type == REF_VAL1 else rec.value[0]
            lst = self._obj_index.setdefault(h, [])
            if not lst or lst[-1] != block_off:
                lst.append(block_off)
            if rec.value_type == REF_VAL2:
                lst2 = self._obj_index.setdefault(rec.value[1], [])
                if not lst2 or lst2[-1] != block_off:
                    lst2.append(block_off)

    def add_log(self, rec: LogRecord) -> None:
        if self.block_writer is not None and self._cur_type == BLOCK_TYPE_REF:
            self._finish_public_ref_section()
        if self.block_writer is None or self._cur_type != BLOCK_TYPE_LOG:
            # Log blocks are unaligned: drop the ref section's pending padding
            # (reftable/writer.c reftable_writer_add_log_verbatim).
            self.next -= self.pending_padding
            self.pending_padding = 0
            self._reinit_block_writer(BLOCK_TYPE_LOG)
        if not self.block_writer.add(rec, self.min_update_index):
            self._flush_block()
            self._reinit_block_writer(BLOCK_TYPE_LOG)
            self.block_writer.add(rec, self.min_update_index)

    def _finish_public_ref_section(self) -> None:
        if self.block_writer is None:
            return
        typ = self._cur_type
        self.ref_index_offset, self.ref_index_blocks = self._finish_section(typ)
        if typ == BLOCK_TYPE_REF and self.ref_index_blocks > 0:
            self._dump_object_index()
        self._obj_index = {}
        self.block_writer = None

    def _dump_object_index(self) -> None:
        # Determine the shortest unique abbreviation length (>= 2 bytes).
        hashes = sorted(self._obj_index.keys())
        obj_id_len = 2
        # widen until all distinct at this prefix length
        while True:
            seen = set()
            collision = False
            for h in hashes:
                p = h[:obj_id_len]
                if p in seen:
                    collision = True
                    break
                seen.add(p)
            if not collision or obj_id_len >= self.hash_size:
                break
            obj_id_len += 1
        self.object_id_len = obj_id_len
        self._reinit_block_writer(BLOCK_TYPE_OBJ)
        for h in hashes:
            rec = ObjRecord(h[:obj_id_len], sorted(set(self._obj_index[h])))
            if not self.block_writer.add(rec, self.min_update_index):
                self._flush_block()
                self._reinit_block_writer(BLOCK_TYPE_OBJ)
                self.block_writer.add(rec, self.min_update_index)
        self.obj_index_offset, _ = self._finish_section(BLOCK_TYPE_OBJ)

    def _finish_log_section(self) -> None:
        if self.block_writer is not None and self._cur_type == BLOCK_TYPE_LOG:
            self.log_index_offset, _ = self._finish_section(BLOCK_TYPE_LOG)
            self.block_writer = None

    # -- close --------------------------------------------------------------
    def finish(self) -> bytes:
        # finish any open public section
        if self.block_writer is not None:
            if self._cur_type == BLOCK_TYPE_LOG:
                self._finish_log_section()
            else:
                self._finish_public_ref_section()
        empty_table = (self.next == 0)
        # Footer-pending padding is discarded (writer sets pending_padding=0).
        self.pending_padding = 0
        if empty_table:
            header = self._write_header_bytes()
            self._padded_write(header, 0)
        footer = bytearray()
        footer += self._write_header_bytes()
        footer += struct.pack(">Q", self.ref_index_offset)
        footer += struct.pack(">Q", (self.obj_offset << 5) | self.object_id_len)
        footer += struct.pack(">Q", self.obj_index_offset)
        footer += struct.pack(">Q", self.log_offset)
        footer += struct.pack(">Q", self.log_index_offset)
        crc = zlib.crc32(bytes(footer)) & 0xFFFFFFFF
        footer += struct.pack(">I", crc)
        self._padded_write(bytes(footer), 0)
        return bytes(self.out)


# --------------------------------------------------------------------------
# table reader (reftable/block.c + table.c, minimal: refs + logs)
# --------------------------------------------------------------------------
class TableReader:
    def __init__(self, data: bytes):
        self.data = data
        if data[:4] != b"REFT":
            raise ValueError("not a reftable")
        self.version = data[4]
        self.block_size = int.from_bytes(data[5:8], "big")
        self.min_update_index = struct.unpack(">Q", data[8:16])[0]
        self.max_update_index = struct.unpack(">Q", data[16:24])[0]
        self.hash_size = 20
        if self.version == 2:
            self.hash_size = 20 if data[24:28] == b"sha1" else 32
        self.header_size = HEADER_SIZE_V1 if self.version == 1 else HEADER_SIZE_V2
        self.footer_size = FOOTER_SIZE_V1 if self.version == 1 else FOOTER_SIZE_V2
        foot = data[len(data) - self.footer_size:]
        self.log_offset = struct.unpack(">Q", foot[48:56])[0]

    # iterate refs in key order
    def refs(self) -> "list[RefRecord]":
        out: list[RefRecord] = []
        pos = 0
        end = self.log_offset if self.log_offset else len(self.data) - self.footer_size
        first = True
        while pos < end:
            off = self.header_size if first else pos
            if first:
                btype = self.data[off]
            else:
                btype = self.data[pos]
            if btype != BLOCK_TYPE_REF:
                break
            block_start = self.header_size if first else pos
            recs, block_len = self._read_ref_block(block_start, first)
            out.extend(recs)
            # advance to next block_size boundary (blocks are padded/aligned)
            consumed = block_len
            if first:
                pos = self.block_size if self.block_size else block_len
                first = False
            else:
                # aligned to block_size
                if self.block_size:
                    pos = ((pos // self.block_size) + 1) * self.block_size
                else:
                    pos = pos + block_len
            if self.block_size == 0:
                pos = block_len
        return out

    def _read_ref_block(self, start: int, first: bool):
        data = self.data
        btype = data[start]
        block_len = int.from_bytes(data[start + 1:start + 4], "big")
        # block_len for first block is measured from file start (incl header).
        if first:
            abs_end = block_len
        else:
            abs_end = start + block_len
        restart_count = struct.unpack(">H", data[abs_end - 2:abs_end])[0]
        records_end = abs_end - 2 - 3 * restart_count
        pos = start + 4
        prev = b""
        recs = []
        while pos < records_end:
            prefix_len, pos = get_var_int(data, pos)
            v, pos = get_var_int(data, pos)
            suffix_len = v >> 3
            extra = v & 0x7
            suffix = data[pos:pos + suffix_len]
            pos += suffix_len
            key = prev[:prefix_len] + suffix
            prev = key
            ui_delta, pos = get_var_int(data, pos)
            update_index = self.min_update_index + ui_delta
            refname = key.decode("utf-8")
            if extra == REF_SYMREF:
                tgt, pos = decode_string(data, pos)
                recs.append(RefRecord(refname, update_index, REF_SYMREF,
                                      target=tgt.decode("utf-8")))
            elif extra == REF_VAL1:
                val = data[pos:pos + self.hash_size]
                pos += self.hash_size
                recs.append(RefRecord(refname, update_index, REF_VAL1, value=val))
            elif extra == REF_VAL2:
                val = data[pos:pos + self.hash_size]
                pos += self.hash_size
                peeled = data[pos:pos + self.hash_size]
                pos += self.hash_size
                recs.append(RefRecord(refname, update_index, REF_VAL2,
                                      value=(val, peeled)))
            else:  # deletion
                recs.append(RefRecord(refname, update_index, REF_DELETION))
        return recs, block_len

    def logs(self) -> "list[LogRecord]":
        if not self.log_offset:
            return []
        out: list[LogRecord] = []
        data = self.data
        pos = self.log_offset
        end = len(data) - self.footer_size
        while pos < end and data[pos] == BLOCK_TYPE_LOG:
            inflated_len = int.from_bytes(data[pos + 1:pos + 4], "big")
            d = zlib.decompressobj()
            raw = d.decompress(data[pos + 4:end])
            comp_consumed = len(data[pos + 4:end]) - len(d.unused_data)
            full = data[pos:pos + 4] + raw
            out.extend(self._parse_log_block(full))
            pos = pos + 4 + comp_consumed
        return out

    def _parse_log_block(self, full: bytes):
        block_len = int.from_bytes(full[1:4], "big")
        restart_count = struct.unpack(">H", full[block_len - 2:block_len])[0]
        records_end = block_len - 2 - 3 * restart_count
        pos = 4
        prev = b""
        recs = []
        while pos < records_end:
            prefix_len, pos = get_var_int(full, pos)
            v, pos = get_var_int(full, pos)
            suffix_len = v >> 3
            extra = v & 0x7
            suffix = full[pos:pos + suffix_len]
            pos += suffix_len
            key = prev[:prefix_len] + suffix
            prev = key
            nul = key.index(0)
            refname = key[:nul].decode("utf-8")
            ts = struct.unpack(">Q", key[nul + 1:nul + 9])[0]
            update_index = (0xFFFFFFFFFFFFFFFF - ts) & 0xFFFFFFFFFFFFFFFF
            if extra == LOG_DELETION:
                recs.append(LogRecord(refname, update_index, LOG_DELETION))
                continue
            old = full[pos:pos + self.hash_size]; pos += self.hash_size
            new = full[pos:pos + self.hash_size]; pos += self.hash_size
            name, pos = decode_string(full, pos)
            email, pos = decode_string(full, pos)
            time, pos = get_var_int(full, pos)
            tz = struct.unpack(">h", full[pos:pos + 2])[0]; pos += 2
            msg, pos = decode_string(full, pos)
            recs.append(LogRecord(refname, update_index, LOG_UPDATE,
                                  old_hash=old, new_hash=new,
                                  name=name.decode("utf-8", "replace"),
                                  email=email.decode("utf-8", "replace"),
                                  time=time, tz_offset=tz,
                                  message=msg.decode("utf-8", "replace")))
        return recs


# --------------------------------------------------------------------------
# stack / high-level ref store (reftable/stack.c + reftable-backend.c)
# --------------------------------------------------------------------------
def is_reftable(gitdir) -> bool:
    """True if ``gitdir`` is a reftable repository."""
    gitdir = Path(gitdir)
    return (gitdir / "reftable" / "tables.list").exists()


def _rand_suffix() -> int:
    return int.from_bytes(os.urandom(4), "big")


def _format_name(min_ui: int, max_ui: int) -> str:
    return "0x%012x-0x%012x-%08x" % (min_ui, max_ui, _rand_suffix())


def _suggest_compaction_segment(sizes: "list[int]", factor: int = 2):
    """Port of reftable/stack.c suggest_compaction_segment.

    Returns (start, end) with end exclusive; start==end means no compaction.
    ``sizes`` are per-table byte counts (file size minus header overhead),
    ordered oldest..newest.
    """
    n = len(sizes)
    if not factor:
        factor = 2
    seg_start = seg_end = 0
    if n <= 1:
        return seg_start, seg_end
    # Find the segment end (exclusive): walk back from newest until the
    # preceding table is smaller than current*factor.
    bytes_ = 0
    i = n - 1
    found = False
    while i > 0:
        if sizes[i - 1] < sizes[i] * factor:
            seg_end = i + 1
            bytes_ = sizes[i]
            found = True
            break
        i -= 1
    if not found:
        return 0, 0
    # Find the segment start by accumulating sizes backwards.
    while i > 0:
        curr = bytes_
        bytes_ += sizes[i - 1]
        if sizes[i - 1] < curr * factor:
            seg_start = i - 1
        i -= 1
    return seg_start, seg_end


class RefStore:
    """High-level reftable-backed reference store for one gitdir."""

    def __init__(self, gitdir, hash_size: int = 20):
        self.gitdir = Path(gitdir)
        self.reftable_dir = self.gitdir / "reftable"
        self.hash_size = hash_size

    # -- table list ---------------------------------------------------------
    def _table_names(self) -> "list[str]":
        lst = self.reftable_dir / "tables.list"
        if not lst.exists():
            return []
        return [ln for ln in lst.read_text(encoding="utf-8").splitlines() if ln]

    def _load_tables(self) -> "list[TableReader]":
        readers = []
        for name in self._table_names():
            p = self.reftable_dir / name
            if p.exists():
                tr = TableReader(p.read_bytes())
                tr.name = name
                tr.size_on_disk = p.stat().st_size
                readers.append(tr)
        return readers

    def _next_update_index(self) -> int:
        mx = 0
        for r in self._load_tables():
            if r.max_update_index > mx:
                mx = r.max_update_index
        return mx + 1

    # -- merged view --------------------------------------------------------
    def _merged_refs(self, tables=None, keep_deletions=False) -> "dict[str, RefRecord]":
        """Newest record per refname across ``tables`` (newest table wins).

        With ``keep_deletions`` False, refs whose newest record is a deletion
        tombstone are dropped (the normal external view). With it True the
        tombstone is retained (used during compaction of a non-base range).
        """
        if tables is None:
            tables = self._load_tables()
        merged: dict[str, RefRecord] = {}
        for reader in tables:  # oldest..newest
            for rec in reader.refs():
                merged[rec.refname] = rec
        if not keep_deletions:
            merged = {n: r for n, r in merged.items()
                      if r.value_type != REF_DELETION}
        return merged

    def read_symbolic(self, name: str) -> Optional[str]:
        rec = self._merged_refs().get(name)
        if rec is None or rec.value_type != REF_SYMREF:
            return None
        return rec.target

    def read_ref(self, name: str) -> Optional[str]:
        """Resolve ``name`` to a hex SHA, following symrefs."""
        merged = self._merged_refs()
        seen = set()
        cur = name
        while True:
            if cur in seen:
                return None
            seen.add(cur)
            rec = merged.get(cur)
            if rec is None:
                return None
            if rec.value_type == REF_SYMREF:
                cur = rec.target
                continue
            if rec.value_type == REF_DELETION:
                return None
            if rec.value_type == REF_VAL2:
                return rec.value[0].hex()
            return rec.value.hex()

    def read_raw(self, name: str) -> Optional[RefRecord]:
        return self._merged_refs().get(name)

    def iter_refs(self) -> "dict[str, str]":
        """All non-deleted, non-symbolic refs as name -> hex SHA (resolved)."""
        out = {}
        for name, rec in self._merged_refs().items():
            if rec.value_type == REF_DELETION:
                continue
            if rec.value_type == REF_SYMREF:
                continue
            if rec.value_type == REF_VAL2:
                out[name] = rec.value[0].hex()
            else:
                out[name] = rec.value.hex()
        return out

    def peeled(self, name: str) -> Optional[str]:
        rec = self._merged_refs().get(name)
        if rec is not None and rec.value_type == REF_VAL2:
            return rec.value[1].hex()
        return None

    # -- write --------------------------------------------------------------
    def _serialize_table(self, ref_records: "list[RefRecord]",
                         log_records: "list[LogRecord]",
                         min_ui: int, max_ui: int) -> bytes:
        w = Writer(min_ui, max_ui, hash_size=self.hash_size)
        for rec in sorted(ref_records, key=lambda r: r.refname.encode("utf-8")):
            w.add_ref(rec)
        for rec in sorted(log_records, key=lambda r: r.key()):
            w.add_log(rec)
        return w.finish()

    def write_table(self, ref_records: "list[RefRecord]",
                    log_records: "list[LogRecord]",
                    min_ui: int, max_ui: int) -> None:
        """Write a single reftable that becomes the entire stack (one table).

        Used for the initial table at init time.
        """
        self.reftable_dir.mkdir(parents=True, exist_ok=True)
        data = self._serialize_table(ref_records, log_records, min_ui, max_ui)
        name = _format_name(min_ui, max_ui) + ".ref"
        (self.reftable_dir / name).write_bytes(data)
        (self.reftable_dir / "tables.list").write_text(name + "\n",
                                                       encoding="utf-8")
        for f in self.reftable_dir.glob("*.ref"):
            if f.name != name:
                try:
                    f.unlink()
                except OSError:
                    pass

    # -- stack maintenance --------------------------------------------------
    def _write_tables_list(self, names: "list[str]") -> None:
        self.reftable_dir.mkdir(parents=True, exist_ok=True)
        (self.reftable_dir / "tables.list").write_text(
            "".join(n + "\n" for n in names), encoding="utf-8")

    def _auto_compact(self) -> None:
        """Geometric auto-compaction (reftable/stack.c reftable_stack_auto_compact).

        Computes the compaction segment from per-table sizes (file size minus
        header overhead) and, if non-empty, merges that contiguous range into a
        single table, deleting the now-orphaned files.
        """
        tables = self._load_tables()
        n = len(tables)
        if n < 2:
            return
        # reftable/table.c sets table->size = file_size - footer_size, and
        # stack_segments_for_compaction subtracts (header_size - 1) on top, so
        # the compaction size of a table is file_size - footer_size - header + 1.
        if self.hash_size == 20:
            header_sz, footer_sz = HEADER_SIZE_V1, FOOTER_SIZE_V1
        else:
            header_sz, footer_sz = HEADER_SIZE_V2, FOOTER_SIZE_V2
        overhead = footer_sz + header_sz - 1
        sizes = [t.size_on_disk - overhead for t in tables]
        start, end = _suggest_compaction_segment(sizes, 2)
        if end - start <= 0:
            return
        # Compact tables[start .. end-1] (end exclusive) -> [start, last].
        last = end - 1
        self._compact_range(tables, start, last)

    def _compact_range(self, tables, first: int, last: int) -> None:
        sub = tables[first:last + 1]
        keep_deletions = (first != 0)
        merged = self._merged_refs(tables=sub, keep_deletions=keep_deletions)
        ref_records = list(merged.values())
        # Logs across the sub-range, newest-per-key wins; drop deletions only
        # when the range starts at the base table.
        logs = self._merged_logs(sub, drop_deletions=(first == 0))
        min_ui = sub[0].min_update_index
        max_ui = sub[-1].max_update_index
        data = self._serialize_table(ref_records, logs, min_ui, max_ui)
        name = _format_name(min_ui, max_ui) + ".ref"
        (self.reftable_dir / name).write_bytes(data)
        new_names = ([t.name for t in tables[:first]] + [name]
                     + [t.name for t in tables[last + 1:]])
        self._write_tables_list(new_names)
        keep = set(new_names)
        for f in self.reftable_dir.glob("*.ref"):
            if f.name not in keep:
                try:
                    f.unlink()
                except OSError:
                    pass

    def _merged_logs(self, tables, drop_deletions: bool) -> "list[LogRecord]":
        """Newest log per (refname, update_index) across ``tables``."""
        out: "dict[tuple, LogRecord]" = {}
        for reader in tables:  # oldest..newest
            for rec in reader.logs():
                out[(rec.refname, rec.update_index)] = rec
        result = list(out.values())
        if drop_deletions:
            result = [r for r in result if r.value_type != LOG_DELETION]
        return result

    def _all_logs(self) -> "list[LogRecord]":
        """Every reflog record across the stack, newest-per-key."""
        return self._merged_logs(self._load_tables(), drop_deletions=False)

    def transaction(self, updates: "list[dict]", *,
                    committer=("", ""), now=(0, 0), peel=None) -> None:
        """Apply a batch of ref updates, appending one delta table then auto-compacting.

        Each update dict has:
          name      - full ref name
          type      - 'val1' | 'symref' | 'delete' | 'logonly'
          new       - hex sha (val1) or target (symref); None for delete/logonly
          old       - hex sha before the update (for reflog), or null oid
          peeled    - optional hex sha for a peeled (VAL2) tag value
          message   - reflog message (without trailing newline), or None to
                      skip logging this ref

        This mirrors C Git's reftable backend: the transaction writes a single
        table containing only the changed records at update_index =
        next_update_index, appends it to the stack, then runs geometric
        auto-compaction (reftable_stack_auto_compact).
        """
        merged = self._merged_refs(keep_deletions=True)  # for symref resolution
        update_index = self._next_update_index()
        cname, cemail = committer
        secs, tz = now
        null_hex = "0" * (self.hash_size * 2)

        # The delta table contains ONLY the records changed by this transaction.
        delta_refs: list[RefRecord] = []
        # Pass 1: apply ref mutations (to `merged` for symref resolution and to
        # `delta_refs` for the new table).
        for up in updates:
            name = up["name"]
            typ = up["type"]
            if typ == "logonly":
                continue
            if typ == "delete":
                rec = RefRecord(name, update_index, REF_DELETION)
            elif typ == "symref":
                rec = RefRecord(name, update_index, REF_SYMREF, target=up["new"])
            else:  # val1 / val2
                peeled_hex = up.get("peeled")
                if peeled_hex is None and peel is not None:
                    peeled_hex = peel(up["new"])
                if peeled_hex:
                    rec = RefRecord(name, update_index, REF_VAL2,
                                    value=(bytes.fromhex(up["new"]),
                                           bytes.fromhex(peeled_hex)))
                else:
                    rec = RefRecord(name, update_index, REF_VAL1,
                                    value=bytes.fromhex(up["new"]))
            merged[name] = rec
            delta_refs.append(rec)

        def resolve(refname: str) -> str:
            seen = set()
            cur = refname
            while cur not in seen:
                seen.add(cur)
                rec = merged.get(cur)
                if rec is None or rec.value_type == REF_DELETION:
                    return null_hex
                if rec.value_type == REF_SYMREF:
                    cur = rec.target
                    continue
                if rec.value_type == REF_VAL2:
                    return rec.value[0].hex()
                return rec.value.hex()
            return null_hex

        # Pre-index existing reflog entries by refname for delete tombstones.
        existing_logs = self._all_logs()
        logs_by_ref: "dict[str, list[int]]" = {}
        for lg in existing_logs:
            if lg.value_type == LOG_UPDATE:
                logs_by_ref.setdefault(lg.refname, []).append(lg.update_index)

        # Pass 2: reflog records, resolving symref targets via `merged`.
        delta_logs: list[LogRecord] = []
        for up in updates:
            name = up["name"]
            typ = up["type"]
            # Deleting a ref also deletes its whole reflog: emit a LOG_DELETION
            # tombstone for each existing reflog entry of that ref, at the same
            # update_index (reftable-backend.c reftable_be_transaction_prepare).
            if typ == "delete":
                for ui in logs_by_ref.get(name, []):
                    delta_logs.append(LogRecord(name, ui, LOG_DELETION))
            msg = up.get("message")
            if msg is None:
                continue
            old = up.get("old") or null_hex
            if typ == "delete":
                new_hex = null_hex
            elif typ == "logonly":
                new_hex = resolve(name)
            elif typ == "symref":
                new_hex = resolve(up["new"])
            else:
                new_hex = up["new"]
            delta_logs.append(LogRecord(
                name, update_index, LOG_UPDATE,
                old_hash=bytes.fromhex(old),
                new_hash=bytes.fromhex(new_hex),
                name=cname, email=cemail, time=secs, tz_offset=tz,
                message=(msg + "\n") if not msg.endswith("\n") else msg))

        self._append_and_compact(delta_refs, delta_logs,
                                 update_index, update_index)

    def _append_and_compact(self, delta_refs, delta_logs, min_ui, max_ui):
        self.reftable_dir.mkdir(parents=True, exist_ok=True)
        data = self._serialize_table(delta_refs, delta_logs, min_ui, max_ui)
        name = _format_name(min_ui, max_ui) + ".ref"
        (self.reftable_dir / name).write_bytes(data)
        self._write_tables_list(self._table_names() + [name])
        self._auto_compact()

    def rename_ref(self, oldname: str, newname: str, logmsg: str, *,
                   committer=("", ""), now=(0, 0)) -> bool:
        """Rename ``oldname`` to ``newname`` (reftable-backend write_copy_table).

        Uses two update indices: ``deletion_ts`` deletes the old ref and reflog,
        ``creation_ts = deletion_ts + 1`` creates the new ref and reflog. The new
        branch's reflog encodes both the deletion (old->0) and recreation
        (0->old) halves and copies the old reflog under the new name.
        Returns False if ``oldname`` does not exist or is symbolic.
        """
        merged = self._merged_refs(keep_deletions=True)
        old_ref = merged.get(oldname)
        if old_ref is None or old_ref.value_type == REF_DELETION:
            return False
        if old_ref.value_type == REF_SYMREF:
            return False
        if oldname == newname:
            return True
        cname, cemail = committer
        secs, tz = now
        old_val = (old_ref.value if old_ref.value_type == REF_VAL1
                   else old_ref.value[0])
        old_hex = old_val.hex()
        null_hex = "0" * (self.hash_size * 2)
        deletion_ts = self._next_update_index()
        creation_ts = deletion_ts + 1

        # Refs: new ref at creation_ts (preserving val type), delete old at del_ts.
        new_ref = RefRecord(newname, creation_ts, old_ref.value_type,
                            value=old_ref.value)
        del_ref = RefRecord(oldname, deletion_ts, REF_DELETION)
        delta_refs = [new_ref, del_ref]

        # Logs.
        delta_logs: list[LogRecord] = []
        msg = logmsg if logmsg.endswith("\n") else (logmsg + "\n")
        # deletion-half log on newname (old->0) at deletion_ts.
        delta_logs.append(LogRecord(
            newname, deletion_ts, LOG_UPDATE,
            old_hash=old_val, new_hash=bytes.fromhex(null_hex),
            name=cname, email=cemail, time=secs, tz_offset=tz, message=msg))
        # HEAD reflog if HEAD points at oldname.
        head = merged.get("HEAD")
        if (head is not None and head.value_type == REF_SYMREF
                and head.target == oldname):
            delta_logs.append(LogRecord(
                "HEAD", deletion_ts, LOG_UPDATE,
                old_hash=old_val, new_hash=bytes.fromhex(null_hex),
                name=cname, email=cemail, time=secs, tz_offset=tz, message=msg))
        # creation-half log on newname (0->old) at creation_ts.
        delta_logs.append(LogRecord(
            newname, creation_ts, LOG_UPDATE,
            old_hash=bytes.fromhex(null_hex), new_hash=old_val,
            name=cname, email=cemail, time=secs, tz_offset=tz, message=msg))
        # Copy old reflog entries under the new name + tombstone the old ones.
        for lg in self._all_logs():
            if lg.refname != oldname or lg.value_type != LOG_UPDATE:
                continue
            copied = LogRecord(newname, lg.update_index, LOG_UPDATE,
                               old_hash=lg.old_hash, new_hash=lg.new_hash,
                               name=lg.name, email=lg.email, time=lg.time,
                               tz_offset=lg.tz_offset, message=lg.message)
            delta_logs.append(copied)
            delta_logs.append(LogRecord(oldname, lg.update_index, LOG_DELETION))

        self._append_and_compact(delta_refs, delta_logs, deletion_ts, creation_ts)
        # If HEAD pointed at oldname, repoint it at newname (separate symref
        # update). Done after the rename so update indices match git's, which
        # repoints HEAD via a follow-up symref write in refs.c rename machinery.
        return True
