"""Repository discovery, layout, and config."""
from __future__ import annotations

import configparser
import os
from pathlib import Path
from typing import Optional


class RepositoryError(Exception):
    pass


class Repository:
    """A git repository on disk.

    `path` points at the worktree root. `gitdir` is the `.git` directory (or
    the repo itself for bare repositories).
    """

    def __init__(self, path: os.PathLike | str, *, gitdir: Optional[Path] = None, bare: bool = False):
        self.path = Path(path).resolve()
        if gitdir is not None:
            self.gitdir = Path(gitdir).resolve()
        elif bare:
            self.gitdir = self.path
        else:
            self.gitdir = self.path / ".git"
        self.bare = bare

    # ---- discovery -----------------------------------------------------

    @classmethod
    def discover(cls, start: os.PathLike | str = ".") -> "Repository":
        cur = Path(start).resolve()
        for candidate in [cur, *cur.parents]:
            git = candidate / ".git"
            if git.is_dir():
                return cls(candidate, gitdir=git)
            if (candidate / "HEAD").exists() and (candidate / "objects").is_dir():
                return cls(candidate, gitdir=candidate, bare=True)
        raise RepositoryError(f"not a git repository: {start}")

    # ---- init ----------------------------------------------------------

    @classmethod
    def init(cls, path: os.PathLike | str, *, bare: bool = False) -> "Repository":
        path = Path(path).resolve()
        path.mkdir(parents=True, exist_ok=True)
        gitdir = path if bare else (path / ".git")
        gitdir.mkdir(exist_ok=True)
        for sub in ("objects", "objects/info", "objects/pack", "refs", "refs/heads", "refs/tags"):
            (gitdir / sub).mkdir(parents=True, exist_ok=True)
        head = gitdir / "HEAD"
        if not head.exists():
            head.write_text("ref: refs/heads/main\n", encoding="utf-8")
        cfg = gitdir / "config"
        if not cfg.exists():
            cfg.write_text(
                "[core]\n"
                "\trepositoryformatversion = 0\n"
                "\tfilemode = false\n"
                f"\tbare = {'true' if bare else 'false'}\n",
                encoding="utf-8",
            )
        (gitdir / "description").write_text(
            "Unnamed repository; edit this file 'description' to name the repository.\n",
            encoding="utf-8",
        )
        return cls(path, gitdir=gitdir, bare=bare)

    # ---- config --------------------------------------------------------

    def config(self) -> configparser.ConfigParser:
        cp = configparser.ConfigParser()
        cfg = self.gitdir / "config"
        if cfg.exists():
            cp.read(cfg, encoding="utf-8")
        return cp

    def user(self) -> tuple[str, str]:
        cp = self.config()
        name = email = None
        if cp.has_section("user"):
            name = cp.get("user", "name", fallback=None)
            email = cp.get("user", "email", fallback=None)
        # Fall back to environment, then a generic default.
        name = os.environ.get("GIT_AUTHOR_NAME") or name or "pythongit"
        email = os.environ.get("GIT_AUTHOR_EMAIL") or email or "pythongit@example.invalid"
        return name, email
