"""Translate object graphs between SHA-1 and SHA-256 repositories."""
from __future__ import annotations

import os
from pathlib import Path

from . import objects as objs
from . import refs as refs_mod
from . import workdir
from .repo import Repository


class ObjectTranslator:
    def __init__(self, src: Repository, dst: Repository):
        self.src = src
        self.dst = dst
        self.memo: dict[str, str] = {}
        self._active: set[str] = set()

    def translate_oid(self, sha: str) -> str:
        if sha in self.memo:
            return self.memo[sha]
        if sha in self._active:
            raise ValueError(f"cyclic object reference involving {sha}")
        self._active.add(sha)
        try:
            obj_type, data = objs.read_object(self.src, sha)
            if obj_type == "blob":
                new_sha = objs.write_object(self.dst, "blob", data)
            elif obj_type == "tree":
                new_sha = objs.write_object(self.dst, "tree", self._translate_tree(data))
            elif obj_type == "commit":
                new_sha = objs.write_object(self.dst, "commit", self._translate_commit(data))
            elif obj_type == "tag":
                new_sha = objs.write_object(self.dst, "tag", self._translate_tag(data))
            else:
                raise ValueError(f"unsupported object type {obj_type!r}")
            self.memo[sha] = new_sha
            return new_sha
        finally:
            self._active.discard(sha)

    def _translate_tree(self, data: bytes) -> bytes:
        entries: list[objs.TreeEntry] = []
        for entry in objs.parse_tree(data, self.src.hash_len):
            try:
                new_sha = self.translate_oid(entry.sha)
            except KeyError:
                if entry.is_gitlink() and len(entry.sha) == self.dst.hex_len:
                    new_sha = entry.sha
                else:
                    raise ValueError(
                        f"cannot translate tree entry {entry.name!r}: missing object {entry.sha}"
                    )
            entries.append(objs.TreeEntry(entry.mode, entry.name, new_sha))
        return objs.encode_tree(entries)

    def _translate_commit(self, data: bytes) -> bytes:
        head, sep, msg = data.partition(b"\n\n")
        if not sep:
            raise ValueError("malformed commit object")
        out: list[bytes] = []
        for line in head.split(b"\n"):
            if line.startswith(b"tree "):
                out.append(b"tree " + self.translate_oid(line[5:].decode("ascii")).encode())
            elif line.startswith(b"parent "):
                out.append(b"parent " + self.translate_oid(line[7:].decode("ascii")).encode())
            else:
                out.append(line)
        return b"\n".join(out) + b"\n\n" + msg

    def _translate_tag(self, data: bytes) -> bytes:
        head, sep, msg = data.partition(b"\n\n")
        out: list[bytes] = []
        translated = False
        for line in head.split(b"\n"):
            if line.startswith(b"object ") and not translated:
                out.append(b"object " + self.translate_oid(line[7:].decode("ascii")).encode())
                translated = True
            else:
                out.append(line)
        return b"\n".join(out) + (sep + msg if sep else b"")


def iter_refs(repo: Repository) -> list[str]:
    found: set[str] = set(refs_mod.read_packed_refs(repo))
    root = repo.gitdir / "refs"
    if root.exists():
        for path in root.rglob("*"):
            if path.is_file():
                found.add(str(path.relative_to(repo.gitdir)).replace(os.sep, "/"))
    return sorted(found)


def translate_repository(src: Repository, dst: Repository, *, checkout: bool = True) -> dict[str, str]:
    translator = ObjectTranslator(src, dst)
    ref_map: dict[str, str] = {}
    for ref in iter_refs(src):
        sha = refs_mod.read_ref(src, ref)
        if not sha:
            continue
        new_sha = translator.translate_oid(sha)
        refs_mod.update_ref(dst, ref, new_sha, message=f"translate from {src.object_format()}")
        ref_map[ref] = new_sha

    head_sym, head_sha = refs_mod.read_head(src)
    if head_sym:
        refs_mod.set_head(dst, head_sym)
    elif head_sha:
        refs_mod.set_head(dst, translator.translate_oid(head_sha))

    if checkout and not dst.bare:
        _sym, dst_head = refs_mod.read_head(dst)
        if dst_head:
            obj_type, data = objs.read_object(dst, dst_head)
            if obj_type == "commit":
                workdir.checkout_tree(dst, objs.parse_commit(data).tree)
    return ref_map


def convert_repository(src_path: str | Path, dst_path: str | Path, object_format: str) -> Repository:
    src = Repository.discover(src_path)
    dst = Repository.init(dst_path, object_format=object_format)
    translate_repository(src, dst, checkout=True)
    return dst
