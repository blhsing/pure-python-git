"""Repository discovery, layout, and config."""
from __future__ import annotations

import configparser
import hashlib
import os
from pathlib import Path
from typing import Optional


class RepositoryError(Exception):
    pass


def _probe_filemode(gitdir: Path) -> bool:
    """Whether the filesystem honors the executable bit, like C Git's probe.

    Git creates a file, flips the user-execute bit, and checks whether the
    change survives a stat; filesystems such as those on Windows do not.
    """
    probe = gitdir / "config.filemode.probe"
    try:
        probe.write_text("", encoding="utf-8")
        probe.chmod(0o644)
        if probe.stat().st_mode & 0o100:
            return False
        probe.chmod(0o744)
        result = bool(probe.stat().st_mode & 0o100)
        return result
    except (OSError, NotImplementedError):
        return False
    finally:
        try:
            probe.unlink()
        except OSError:
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
        env_git_dir = os.environ.get("GIT_DIR")
        env_work_tree = os.environ.get("GIT_WORK_TREE")
        if env_git_dir:
            gitdir = Path(env_git_dir).resolve()
            if env_work_tree:
                return cls(Path(env_work_tree).resolve(), gitdir=gitdir)
            if gitdir.name == ".git":
                return cls(gitdir.parent, gitdir=gitdir)
            return cls(gitdir, gitdir=gitdir, bare=True)
        cur = Path(env_work_tree or start).resolve()
        for candidate in [cur, *cur.parents]:
            git = candidate / ".git"
            if git.is_dir():
                return cls(candidate, gitdir=git)
            if (candidate / "HEAD").exists() and (candidate / "objects").is_dir():
                return cls(candidate, gitdir=candidate, bare=True)
        raise RepositoryError(f"not a git repository: {start}")

    # ---- init ----------------------------------------------------------

    @classmethod
    def init(
        cls,
        path: os.PathLike | str,
        *,
        bare: bool = False,
        object_format: str = "sha1",
        gitdir_override: "os.PathLike | str | None" = None,
        write_default_extras: bool = True,
    ) -> "Repository":
        if object_format not in ("sha1", "sha256"):
            raise ValueError(f"unsupported object format {object_format}")
        path = Path(path).resolve()
        path.mkdir(parents=True, exist_ok=True)
        if gitdir_override is not None:
            gitdir = Path(gitdir_override)
        else:
            gitdir = path if bare else (path / ".git")
        gitdir.mkdir(parents=True, exist_ok=True)
        for sub in ("objects", "objects/info", "objects/pack", "refs", "refs/heads", "refs/tags"):
            (gitdir / sub).mkdir(parents=True, exist_ok=True)
        head = gitdir / "HEAD"
        if not head.exists():
            head.write_text("ref: refs/heads/main\n", encoding="utf-8")
        cfg = gitdir / "config"
        if not cfg.exists():
            config = (
                "[core]\n"
                f"\trepositoryformatversion = {1 if object_format == 'sha256' else 0}\n"
                f"\tfilemode = {'true' if _probe_filemode(gitdir) else 'false'}\n"
                f"\tbare = {'true' if bare else 'false'}\n"
            )
            if not bare:
                config += "\tlogallrefupdates = true\n"
            if object_format != "sha1":
                config += "[extensions]\n\tobjectformat = sha256\n"
            cfg.write_text(config, encoding="utf-8")
        if write_default_extras:
            # C Git 2.54 produces description/hooks/info/exclude only from the
            # template directory; pygit ships no compiled template, so it writes
            # these stand-ins on a plain init. When a template is supplied the
            # caller passes write_default_extras=False so the template's own
            # files (or their absence) win, matching the oracle byte-for-byte.
            if not (gitdir / "description").exists():
                (gitdir / "description").write_text(
                    "Unnamed repository; edit this file 'description' to name the repository.\n",
                    encoding="utf-8",
                )
            (gitdir / "hooks").mkdir(exist_ok=True)
            info = gitdir / "info"
            info.mkdir(exist_ok=True)
            exclude = info / "exclude"
            if not exclude.exists():
                exclude.write_text(
                    "# git ls-files --others --exclude-from=.git/info/exclude\n"
                    "# Lines that start with '#' are comments.\n"
                    "# For a project mostly in C, the following would be a good set of\n"
                    "# exclude patterns (uncomment them if you want to use them):\n"
                    "# *.[oa]\n"
                    "# *~\n",
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

    def object_format(self) -> str:
        from . import gitconfig
        fmt = (gitconfig.get(self, "extensions.objectformat") or "sha1").lower()
        if fmt not in ("sha1", "sha256"):
            raise RepositoryError(f"unsupported object format: {fmt}")
        return fmt

    @property
    def hash_len(self) -> int:
        return 32 if self.object_format() == "sha256" else 20

    @property
    def hex_len(self) -> int:
        return self.hash_len * 2

    def null_oid(self) -> str:
        return "0" * self.hex_len

    def new_hash(self):
        return hashlib.sha256() if self.object_format() == "sha256" else hashlib.sha1()

    def hash_bytes(self, data: bytes) -> bytes:
        h = self.new_hash()
        h.update(data)
        return h.digest()

    def hash_hex(self, data: bytes) -> str:
        h = self.new_hash()
        h.update(data)
        return h.hexdigest()

    def user(self) -> tuple[str, str]:
        from . import gitconfig
        name = gitconfig.get(self, "user.name")
        email = gitconfig.get(self, "user.email")
        # Fall back to environment, then a generic default.
        name = os.environ.get("GIT_AUTHOR_NAME") or name or "pythongit"
        email = os.environ.get("GIT_AUTHOR_EMAIL") or email or os.environ.get("EMAIL") or "pythongit@example.invalid"
        return name, email
