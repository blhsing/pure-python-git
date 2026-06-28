"""git difftool — byte-exact port of builtin/difftool.c + git-difftool--helper.sh.

``git difftool`` is, in C, a thin wrapper that runs ``git diff`` with
``GIT_EXTERNAL_DIFF=git-difftool--helper``.  The helper resolves the configured
diff tool (or runs ``--extcmd``), optionally prompts before launching it once
per changed path, and feeds it the two versions of each file.  With ``-d`` git
copies both trees into temporary directories and runs the tool a single time on
the two directories.

This module ports that whole pipeline so the observable behaviour (prompts,
tool resolution, exit codes, env/argument contract) is identical without
shelling out to a real ``git``.
"""

from __future__ import annotations

import os
import shutil
import stat as _stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Optional

from . import gitconfig


# ---------------------------------------------------------------------------
# Small output helpers (difftool prompts go to stdout; diagnostics to stderr).


def _out(s: str = "") -> None:
    sys.stdout.write(s)
    sys.stdout.flush()


def _eprint(s: str) -> None:
    sys.stderr.write(s if s.endswith("\n") else s + "\n")


class _Fatal(Exception):
    """Raised to emit ``fatal: <msg>`` and exit 128, like git's die()."""


# ---------------------------------------------------------------------------
# config bool, matching git_config_bool semantics.

_TRUE = {"true", "yes", "on", "1"}
_FALSE = {"false", "no", "off", "0", ""}


def _parse_bool(value: str):
    """git_config_bool_or_int core: returns True/False, or None if not a valid
    boolean/integer."""
    v = value.strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    try:
        return int(v, 10) != 0
    except ValueError:
        return None


def _config_bool(value: Optional[str], default: bool) -> bool:
    if value is None:
        return default
    parsed = _parse_bool(value)
    return default if parsed is None else parsed


class _BadBool(Exception):
    def __init__(self, value, key):
        self.value = value
        self.key = key


def _config_bool_strict(value: Optional[str], default: bool, key: str) -> bool:
    """git_config_bool: dies on a value that is neither bool nor integer."""
    if value is None:
        return default
    parsed = _parse_bool(value)
    if parsed is None:
        raise _BadBool(value, key)
    return parsed


def _validate_eager_config(repo):
    """Port of difftool_config(): git_config_bool over difftool.trustexitcode
    and core.symlinks runs at startup (before option parsing), dying on a bad
    boolean.  difftool_config is invoked per config entry in file/-c order, so
    the first offending value (by config order) is the one that is reported."""
    checked = {"difftool.trustexitcode", "core.symlinks"}
    for full, value in gitconfig.list_all(repo):
        if full in checked:
            parsed = _parse_bool(value)
            if parsed is None:
                raise _BadBool(value, full)


# ---------------------------------------------------------------------------
# Diff tool resolution — ports git-mergetool--lib.sh.
#
# A "tool" is resolved to a command template (a shell snippet) using
# difftool.<tool>.cmd / mergetool.<tool>.cmd for user tools, or the built-in
# metadata below for built-in tools.  The command is run via the shell with
# $LOCAL/$REMOTE/$MERGED/$BASE in the environment, exactly as the helper does.


class ToolMeta:
    __slots__ = ("name", "diff_cmd", "help", "can_diff", "available_check")

    def __init__(self, name, diff_cmd, help, can_diff=True, available_check=None):
        self.name = name
        self.diff_cmd = diff_cmd
        self.help = help
        self.can_diff = can_diff
        # binary that must be on PATH for the tool to be "available"
        self.available_check = available_check


# Built-in tool diff commands, ported from git's mergetools/* scripts (the
# diff_cmd () definitions) for the diff (TOOL_MODE=diff) case.  Each value is a
# shell snippet evaluated with $LOCAL and $REMOTE set.  The help string matches
# diff_cmd_help.  available_check is the program looked up with `type` by
# is_available (translate_merge_tool_path identity for all of these).
_BUILTIN_TOOLS = {
    "araxis": ToolMeta(
        "araxis", '''compare -wait -2 "$LOCAL" "$REMOTE"''',
        "Use Araxis Merge", available_check="compare"),
    "bc": ToolMeta(
        "bc", '''"$merge_tool_path" "$LOCAL" "$REMOTE"''',
        "Use Beyond Compare", available_check="bcompare"),
    "bc3": ToolMeta(
        "bc3", '''"$merge_tool_path" "$LOCAL" "$REMOTE"''',
        "Use Beyond Compare", available_check="bcompare"),
    "bc4": ToolMeta(
        "bc4", '''"$merge_tool_path" "$LOCAL" "$REMOTE"''',
        "Use Beyond Compare", available_check="bcompare"),
    "codecompare": ToolMeta(
        "codecompare", '''"$merge_tool_path" "$LOCAL" "$REMOTE"''',
        "Use Code Compare", available_check="CodeCompare"),
    "deltawalker": ToolMeta(
        "deltawalker", '''"$merge_tool_path" "$LOCAL" "$REMOTE" -nosplash''',
        "Use DeltaWalker", available_check="DeltaWalker"),
    "diffmerge": ToolMeta(
        "diffmerge", '''"$merge_tool_path" "$LOCAL" "$REMOTE"''',
        "Use DiffMerge", available_check="diffmerge"),
    "diffuse": ToolMeta(
        "diffuse", '''"$merge_tool_path" "$LOCAL" "$REMOTE" | cat''',
        "Use Diffuse", available_check="diffuse"),
    "ecmerge": ToolMeta(
        "ecmerge", '''"$merge_tool_path" "$LOCAL" "$REMOTE" --default --mode=diff2''',
        "Use ECMerge", available_check="ecmerge"),
    "emerge": ToolMeta(
        "emerge", '''"$merge_tool_path" -f emerge-files-command "$LOCAL" "$REMOTE"''',
        "Use Emacs' Emerge", available_check="emacs"),
    "examdiff": ToolMeta(
        "examdiff", '''"$merge_tool_path" "$LOCAL" "$REMOTE" -nh''',
        "Use ExamDiff Pro", available_check="ExamDiff"),
    "guiffy": ToolMeta(
        "guiffy", '''"$merge_tool_path" "$LOCAL" "$REMOTE"''',
        "Use Guiffy's Diff Tool", available_check="guiffy"),
    "gvimdiff": ToolMeta(
        "gvimdiff", '''"$merge_tool_path" -R -f -d \
			-c 'wincmd l' -c 'cd $GIT_PREFIX' "$LOCAL" "$REMOTE"''',
        "Use gVim", available_check="gvim"),
    "kdiff3": ToolMeta(
        "kdiff3", '''"$merge_tool_path" --L1 "$MERGED (A)" --L2 "$MERGED (B)" \
			"$LOCAL" "$REMOTE" >/dev/null 2>&1''',
        "Use KDiff3", available_check="kdiff3"),
    "kompare": ToolMeta(
        "kompare", '''"$merge_tool_path" "$LOCAL" "$REMOTE"''',
        "Use Kompare", available_check="kompare"),
    "meld": ToolMeta(
        "meld", '''"$merge_tool_path" "$LOCAL" "$REMOTE"''',
        "Use Meld", available_check="meld"),
    "nvimdiff": ToolMeta(
        "nvimdiff", '''"$merge_tool_path" -R -f -d \
			-c 'wincmd l' -c 'cd $GIT_PREFIX' "$LOCAL" "$REMOTE"''',
        "Use Neovim", available_check="nvim"),
    "opendiff": ToolMeta(
        "opendiff", '''"$merge_tool_path" "$LOCAL" "$REMOTE" | cat''',
        "Use FileMerge", available_check="opendiff"),
    "p4merge": ToolMeta(
        "p4merge", '''"$merge_tool_path" "$LOCAL" "$REMOTE"''',
        "Use HelixCore P4Merge", available_check="p4merge"),
    "smerge": ToolMeta(
        "smerge", '''"$merge_tool_path" diff "$LOCAL" "$REMOTE"''',
        "Use Sublime Merge", available_check="smerge"),
    "tkdiff": ToolMeta(
        "tkdiff", '''"$merge_tool_path" "$LOCAL" "$REMOTE"''',
        "Use TkDiff", available_check="tkdiff"),
    "vimdiff": ToolMeta(
        "vimdiff", '''"$merge_tool_path" -R -f -d \
			-c 'wincmd l' -c 'cd $GIT_PREFIX' "$LOCAL" "$REMOTE"''',
        "Use Vim", available_check="vim"),
    "vscode": ToolMeta(
        "vscode", '''"$merge_tool_path" --wait --diff "$LOCAL" "$REMOTE"''',
        "Use Visual Studio Code", available_check="code"),
    "winmerge": ToolMeta(
        "winmerge", '''"$merge_tool_path" -u -e "$LOCAL" "$REMOTE"''',
        "Use WinMerge", available_check="WinMergeU"),
    "xxdiff": ToolMeta(
        "xxdiff", '''"$merge_tool_path" \
			-R 'Accel.Search: "Ctrl+F"' \
			-R 'Accel.SearchForward: "Ctrl-G"' \
			"$LOCAL" "$REMOTE"''',
        "Use xxdiff", available_check="xxdiff"),
    # Merge-only tool: present so we can emit the correct "can only be used to
    # resolve merges" error when selected in diff mode.
    "tortoisemerge": ToolMeta(
        "tortoisemerge", None,
        "Use TortoiseMerge", can_diff=False, available_check="tortoisegitmerge"),
}

# Base scripts that accept a stripped trailing-digit variant, and the exact set
# of variant names valid in DIFF mode (mergetools/* list_tool_variants).
_TOOL_VARIANTS = {
    "bc": {"bc", "bc3", "bc4"},
    "vimdiff": {"vimdiff"},
    "gvimdiff": {"gvimdiff"},
    "nvimdiff": {"nvimdiff"},
}

# Tools that only function in a windowed environment.  show_tool_help appends
# "(requires a graphical session)" to a tool's help line when can_diff but the
# tool needs a GUI.  In git this comes from each script's can_diff/notes; the
# terminal-only tools are emerge, vimdiff and nvimdiff.
_TERMINAL_TOOLS = {"emerge", "vimdiff", "nvimdiff"}


def _get_merge_tool_cmd(repo, tool):
    """diff_mode get_merge_tool_cmd: difftool.<t>.cmd then mergetool.<t>.cmd."""
    cmd = gitconfig.get(repo, f"difftool.{tool}.cmd")
    if cmd is not None:
        return cmd
    return gitconfig.get(repo, f"mergetool.{tool}.cmd")


def _base_script(tool):
    """Resolve the mergetools script backing `tool`, mirroring setup_tool's
    "$tool" / "${tool%[0-9]}" lookup.  Returns the base name or None."""
    if tool in _BUILTIN_TOOLS or tool in _TOOL_VARIANTS:
        return tool
    if tool and tool[-1].isdigit():
        stripped = tool[:-1]
        if stripped in _BUILTIN_TOOLS or stripped in _TOOL_VARIANTS:
            return stripped
    return None


def _setup_tool(repo, tool):
    """Port of mergetool--lib setup_tool for diff mode.

    Returns (ok, error_message_or_None).  ok=False with a message means
    initialize_merge_tool failed and the helper should exit nonzero (the
    file-diff path turns that into 'external diff died')."""
    base = _base_script(tool)
    if base is None:
        # setup_user_tool: requires difftool/mergetool.<tool>.cmd
        if _get_merge_tool_cmd(repo, tool):
            return True, None
        return False, "error: difftool.%s.cmd not set for tool '%s'" % (tool, tool)
    # A built-in script backs this tool; check the variant is valid in diff mode.
    variants = _TOOL_VARIANTS.get(base, {base})
    if tool not in variants and tool != base:
        return False, "error: unknown tool variant '%s'" % tool
    meta = _BUILTIN_TOOLS.get(base)
    if meta is not None and not meta.can_diff:
        return False, "error: '%s' can only be used to resolve merges" % tool
    return True, None


def _valid_tool(repo, tool):
    """valid_tool: setup_tool succeeds, or a user cmd is configured."""
    ok, _ = _setup_tool(repo, tool)
    if ok:
        return True
    return bool(_get_merge_tool_cmd(repo, tool))


def _is_available(tool):
    """is_available after setup_tool: type "$(translate_merge_tool_path tool)".

    For built-in tools the per-tool translate_merge_tool_path maps to the real
    program name (e.g. bc -> bcompare); this is the form used by --tool-help,
    which calls setup_tool first."""
    meta = _BUILTIN_TOOLS.get(tool)
    if meta is None:
        return False
    return shutil.which(meta.available_check) is not None


def _is_available_guess(tool):
    """is_available in the guess loop: setup_tool has NOT run, so the GLOBAL
    translate_merge_tool_path identity applies and `type <tool>` is checked
    against the tool name itself (e.g. `bc` finds /usr/bin/bc)."""
    return shutil.which(tool) is not None


def _gui_mode(repo, gui_flag):
    """gui_mode: GIT_MERGETOOL_GUI from -g, else difftool.guiDefault."""
    if gui_flag is True:
        return True
    if gui_flag is False:
        return False
    # difftool.guiDefault: false (default), true, or auto (DISPLAY-driven).
    val = (gitconfig.get(repo, "difftool.guiDefault") or "false").lower()
    if val == "auto":
        return bool(os.environ.get("DISPLAY"))
    return _config_bool(val, False)


def _get_configured_merge_tool(repo, gui_flag):
    """get_configured_merge_tool (diff mode): walk the key precedence list."""
    if _gui_mode(repo, gui_flag):
        keys = ["diff.guitool", "merge.guitool", "diff.tool", "merge.tool"]
    else:
        keys = ["diff.tool", "merge.tool"]
    selected = None
    for key in keys:
        v = gitconfig.get(repo, key)
        if v:
            selected = v
            break
    if selected and not _valid_tool(repo, selected):
        _eprint(f"git config option diff.tool set to unknown tool: {selected}")
        _eprint("Resetting to default...")
        return None, 1
    return selected, 0


def _list_merge_tool_candidates():
    """list_merge_tool_candidates for diff mode (ported)."""
    tools = ["kompare"]
    if os.environ.get("DISPLAY"):
        if os.environ.get("GNOME_DESKTOP_SESSION_ID"):
            tools = ["meld", "opendiff", "kdiff3", "tkdiff", "xxdiff"] + tools
        else:
            tools = ["opendiff", "kdiff3", "tkdiff", "xxdiff", "meld"] + tools
        tools += ["gvimdiff", "diffuse", "diffmerge", "ecmerge",
                  "p4merge", "araxis", "bc", "codecompare", "smerge"]
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or ""
    if "nvim" in editor:
        tools += ["nvimdiff", "vimdiff", "emerge"]
    elif "vim" in editor:
        tools += ["vimdiff", "nvimdiff", "emerge"]
    else:
        tools += ["emerge", "vimdiff", "nvimdiff"]
    return tools


def _guess_merge_tool(repo):
    """guess_merge_tool: print the candidate banner, pick first available."""
    tools = _list_merge_tool_candidates()
    _eprint("")
    _eprint("This message is displayed because 'diff.tool' is not configured.")
    _eprint("See 'git difftool --tool-help' or 'git help config' for more details.")
    _eprint("'git difftool' will now attempt to use one of the following tools:")
    _eprint(" ".join(tools))
    for tool in tools:
        if _is_available_guess(tool):
            return tool
    _eprint("No known diff tool is available.")
    return None


def _get_merge_tool(repo, gui_flag):
    """get_merge_tool: configured tool, else guess.

    Returns (tool, exit_status_or_None).  A non-None status > 1 means the
    caller (helper) should propagate it; the file-diff path treats that as the
    external diff dying.
    """
    tool, status = _get_configured_merge_tool(repo, gui_flag)
    if status > 1:
        return None, status
    if not tool:
        tool = _guess_merge_tool(repo)
        if tool is None:
            return None, 1
    return tool, None


def _translate_tool_path(tool):
    """translate_merge_tool_path: for a built-in tool, the real program name
    (e.g. bc -> bcompare); otherwise the tool name itself."""
    meta = _BUILTIN_TOOLS.get(_base_script(tool))
    if meta is not None and meta.available_check:
        return meta.available_check
    return tool


def _get_merge_tool_path(repo, tool):
    """get_merge_tool_path: resolve the program path for a tool, or die."""
    if not _valid_tool(repo, tool):
        _eprint(f"Unknown diff tool {tool}")
        return None
    path = gitconfig.get(repo, f"difftool.{tool}.path")
    if path is None:
        path = gitconfig.get(repo, f"mergetool.{tool}.path")
    if not path:
        path = _translate_tool_path(tool)
    if not _get_merge_tool_cmd(repo, tool) and shutil.which(path) is None:
        _eprint(f"The diff tool {tool} is not available as '{path}'")
        return None
    return path


# ---------------------------------------------------------------------------
# Materialising file versions, ports the GIT_EXTERNAL_DIFF temp-blob behaviour.


class _TempBlobs:
    """Owns the per-invocation /tmp/git-blob-XXXXXX directories that hold the
    materialised "old" (and tree/index "new") blob contents, mirroring git's
    diff temp-file handling.  Each blob lives in its own mkdtemp dir named by
    its basename, like git's prepare_temp_file()."""

    def __init__(self):
        self._dirs: List[str] = []

    def write(self, content: bytes, basename: str) -> str:
        d = tempfile.mkdtemp(prefix="git-blob-")
        self._dirs.append(d)
        p = os.path.join(d, os.path.basename(basename))
        with open(p, "wb") as f:
            f.write(content)
        return p

    def cleanup(self):
        for d in self._dirs:
            shutil.rmtree(d, ignore_errors=True)
        self._dirs = []


# ---------------------------------------------------------------------------
# The launcher — ports launch_merge_tool() + run_diff_cmd() from the helper.


def _config_bool_subshell(repo, key):
    """Mimic `git config --bool <key>`: returns "true"/"false" for a valid
    value; None when unset OR a bad boolean (the `||` fallback fires for both).
    A bad boolean additionally prints the fatal diagnostic to stderr, but the
    surrounding command substitution swallows the failure and continues."""
    raw = gitconfig.get(repo, key)
    if raw is None:
        return None
    parsed = _parse_bool(raw)
    if parsed is None:
        _eprint("fatal: bad boolean config value '%s' for '%s'" % (raw, key))
        return None
    return "true" if parsed else "false"


def _should_prompt(repo, prompt_flag):
    """should_prompt(): difftool.prompt overrides mergetool.prompt (default
    true); the -y/--prompt flag (GIT_DIFFTOOL_*_PROMPT) is the override.

    Mirrors the shell:
        prompt_merge=$(git config --bool mergetool.prompt || echo true)
        prompt=$(git config --bool difftool.prompt || echo $prompt_merge)
    """
    prompt_merge = _config_bool_subshell(repo, "mergetool.prompt")
    if prompt_merge is None:
        prompt_merge = "true"
    prompt = _config_bool_subshell(repo, "difftool.prompt")
    if prompt is None:
        prompt = prompt_merge
    if prompt == "true":
        # default behaviour is to prompt unless --no-prompt (-y) was given
        return prompt_flag != 0
    # default behaviour is no prompt unless --prompt was given
    return prompt_flag == 1


# Sentinel: initialize_merge_tool failed -> helper exits nonzero -> for the
# file-diff path git reports the external diff as having died.
_INIT_FAILED = -1000


def _run_tool_cmd(repo, tool, env):
    """initialize_merge_tool + run_diff_cmd: validate the tool then eval its
    cmd (user or built-in) via the shell.  Returns _INIT_FAILED if setup or
    path resolution fails (after emitting the matching error)."""
    ok, msg = _setup_tool(repo, tool)
    if not ok:
        _eprint(msg)
        return _INIT_FAILED
    cmd = _get_merge_tool_cmd(repo, tool)
    if cmd is None:
        meta = _BUILTIN_TOOLS.get(_base_script(tool))
        cmd = meta.diff_cmd if meta else None
    if cmd is None:
        return _INIT_FAILED
    path = _get_merge_tool_path(repo, tool)
    if path is None:
        return _INIT_FAILED
    run_env = dict(env)
    run_env["merge_tool_path"] = path
    run_env.setdefault("GIT_PREFIX", run_env.get("GIT_PREFIX") or ".")
    return subprocess.call(["/bin/sh", "-c", cmd], env=run_env)


def _launch(repo, dt, path, local, remote, counter, total):
    """launch_merge_tool: prompt, then run extcmd or the configured tool."""
    merged = path
    base = path
    if dt.prompt_active:
        _out("\nViewing (%s/%s): '%s'\n" % (counter, total, merged))
        label = dt.extcmd if dt.extcmd else dt.tool
        _out("Launch '%s' [Y/n]? " % label)
        try:
            ans = dt.read_answer()
        except EOFError:
            return 0
        if ans is None:
            return 0
        if ans == "n":
            return 0

    env = dict(os.environ)
    env["BASE"] = base
    if dt.extcmd:
        # eval $GIT_DIFFTOOL_EXTCMD '"$LOCAL"' '"$REMOTE"'
        env["LOCAL"] = local
        env["REMOTE"] = remote
        env["MERGED"] = merged
        env["GIT_DIFF_PATH_COUNTER"] = str(counter)
        env["GIT_DIFF_PATH_TOTAL"] = str(total)
        full = "%s %s %s" % (
            dt.extcmd, _shq(local), _shq(remote))
        return subprocess.call(["/bin/sh", "-c", full], env=env)
    env["LOCAL"] = local
    env["REMOTE"] = remote
    env["MERGED"] = merged
    env["GIT_DIFF_PATH_COUNTER"] = str(counter)
    env["GIT_DIFF_PATH_TOTAL"] = str(total)
    return _run_tool_cmd(repo, dt.tool, env)


def _shq(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


# ---------------------------------------------------------------------------
# Change computation — mirrors cli.cmd_diff's change list (same ordering and
# the per-side worktree flag that decides temp-blob vs worktree path).


class _Change:
    __slots__ = ("path", "src_path", "a", "b", "status")

    def __init__(self, path, a, b, status="M", src_path=None):
        self.path = path
        self.src_path = src_path or path
        self.a = a
        self.b = b
        self.status = status


def _compute_changes(repo, cached, revs, paths):
    """Replicate cli.cmd_diff's change list for the requested mode."""
    from . import cli as _cli
    from .index import read_index as _read_index

    _Side = _cli._Side
    _ABSENT = _cli._ABSENT
    side_obj = _cli._side_from_object
    side_wt = _cli._side_from_worktree
    from . import refs as _refs

    def tree_map_of(rev):
        """Resolve a rev to its tree's path map, treating an unborn ref or
        unresolvable rev as an empty tree (like git diff against no commit)."""
        sha = _refs.rev_parse(repo, rev)
        if not sha:
            return {}
        tree = _cli._commit_tree(repo, sha)
        if not tree:
            return {}
        return _cli._tree_map_full(repo, tree)

    idx = _read_index(repo).by_path()
    changes: List[_Change] = []

    def add(path, a, b, status):
        if a.sha != b.sha or a.mode != b.mode:
            changes.append(_Change(path, a, b, status))

    def status_of(a, b):
        return "A" if not a.present else ("D" if not b.present else "M")

    if len(revs) >= 2:
        a_map = tree_map_of(revs[0])
        b_map = tree_map_of(revs[1])
        for p in sorted(set(a_map) | set(b_map)):
            a = side_obj(repo, *a_map[p]) if p in a_map else _ABSENT
            b = side_obj(repo, *b_map[p]) if p in b_map else _ABSENT
            add(p, a, b, status_of(a, b))
    elif len(revs) == 1:
        a_map = tree_map_of(revs[0])
        if cached:
            for p in sorted(set(a_map) | set(idx)):
                a = side_obj(repo, *a_map[p]) if p in a_map else _ABSENT
                b = side_obj(repo, idx[p].mode_str(), idx[p].sha) if p in idx else _ABSENT
                add(p, a, b, status_of(a, b))
        else:
            for p in sorted(set(a_map) | set(idx)):
                a = side_obj(repo, *a_map[p]) if p in a_map else _ABSENT
                b = side_wt(repo, p)
                add(p, a, b, status_of(a, b))
    elif cached:
        head_map = tree_map_of("HEAD")
        for p in sorted(set(head_map) | set(idx)):
            if p in idx and idx[p].intent_to_add and p not in head_map:
                continue
            a = side_obj(repo, *head_map[p]) if p in head_map else _ABSENT
            b = side_obj(repo, idx[p].mode_str(), idx[p].sha) if p in idx else _ABSENT
            add(p, a, b, status_of(a, b))
    else:
        for p in sorted(idx):
            a = _ABSENT if idx[p].intent_to_add else side_obj(repo, idx[p].mode_str(), idx[p].sha)
            b = side_wt(repo, p)
            add(p, a, b, status_of(a, b))

    if paths:
        wanted = set(paths)
        changes = [c for c in changes
                   if c.path in wanted
                   or any(c.path.startswith(w.rstrip("/") + "/") for w in paths)]
    return changes


def _materialize(repo, change, blobs):
    """Compute LOCAL (old side) and REMOTE (new side) file paths for a change,
    matching git's GIT_EXTERNAL_DIFF temp-blob handling."""
    a, b = change.a, change.b
    if not a.present:
        local = os.devnull  # /dev/null
    else:
        local = blobs.write(a.data or b"", change.src_path)
    if not b.present:
        remote = os.devnull
    elif b.worktree and b.mode != "120000":
        # b reads straight from the working tree; git passes the real path.
        # Symlinks are the exception: git materialises a temp blob holding the
        # readlink text rather than letting the tool dereference the link.
        remote = change.path
    else:
        remote = blobs.write(b.data or b"", change.path)
    return local, remote


# ---------------------------------------------------------------------------
# --no-index file/dir traversal — ports the relevant slice of diff --no-index.


def _noindex_pairs(p1: str, p2: str):
    """Yield (path, local, remote, merged) tuples for `diff --no-index p1 p2`.

    Mirrors git's builtin/diff.c queue_diff: two files diff directly; two
    directories are walked with a per-directory name-sorted merge, recursing
    into subdirectories (so 'sub/x' sorts before 'sub.txt')."""
    d1 = os.path.isdir(p1)
    d2 = os.path.isdir(p2)
    if d1 and d2:
        yield from _noindex_dirs(p1, p2, "")
    else:
        # file vs file (git rejects dir-vs-file with an error before this)
        merged = p1 if p1 != os.devnull else os.devnull
        yield (p1, p1, p2, merged)


def _noindex_dirs(b1, b2, rel):
    """Recursively diff two directories, per-directory strcmp-sorted merge."""
    def listing(base):
        full = base if not rel else os.path.join(base, rel)
        try:
            return sorted(os.listdir(full))
        except OSError:
            return []
    names = sorted(set(listing(b1)) | set(listing(b2)))
    for name in names:
        sub = name if not rel else rel + "/" + name
        f1 = os.path.join(b1, sub)
        f2 = os.path.join(b2, sub)
        e1 = os.path.lexists(f1)
        e2 = os.path.lexists(f2)
        i1 = e1 and os.path.isdir(f1)
        i2 = e2 and os.path.isdir(f2)
        if i1 and i2:
            yield from _noindex_dirs(b1, b2, sub)
        elif i1 and not e2:
            yield from _noindex_dirs(b1, b2, sub)
        elif i2 and not e1:
            yield from _noindex_dirs(b1, b2, sub)
        else:
            local = f1 if e1 else os.devnull
            remote = f2 if e2 else os.devnull
            merged = f1 if e1 else os.devnull
            yield (sub, local, remote, merged)


# ---------------------------------------------------------------------------
# The difftool "options" carrier and stdin answer reader.


class _DT:
    def __init__(self):
        self.tool = None
        self.extcmd = None
        self.prompt_active = False
        self._stdin = None

    def read_answer(self):
        if self._stdin is None:
            self._stdin = sys.stdin
        line = self._stdin.readline()
        if line == "":
            raise EOFError
        return line.rstrip("\n")


_NOINDEX_USAGE = None


def _noindex_usage():
    global _NOINDEX_USAGE
    if _NOINDEX_USAGE is None:
        p = Path(__file__).with_name("_difftool_noindex_usage.txt")
        try:
            _NOINDEX_USAGE = p.read_text(encoding="utf-8")
        except OSError:
            _NOINDEX_USAGE = (
                "usage: git diff --no-index [<options>] <path> <path> "
                "[<pathspec>...]\n")
    return _NOINDEX_USAGE


# ---------------------------------------------------------------------------
# File-diff mode (the default): run the tool once per changed path.


def _run_file_diff(repo, dt, changes):
    """run_file_diff + the helper's per-path loop."""
    total = len(changes)
    blobs = _TempBlobs()
    try:
        counter = 0
        for ch in changes:
            counter += 1
            local, remote = _materialize(repo, ch, blobs)
            status = _launch(repo, dt, ch.path, local, remote, counter, total)
            if status == _INIT_FAILED:
                # The helper itself exited nonzero (tool init failure).
                return _external_diff_died(ch.path)
            if status >= 126:
                # command not found / not executable / signalled
                return _external_diff_died(ch.path)
            if status != 0 and dt.trust_exit_code:
                return _external_diff_died(ch.path)
        return 0
    finally:
        blobs.cleanup()


def _external_diff_died(path):
    _eprint("fatal: external diff died, stopping at %s" % path)
    return 128


def _run_noindex(repo, dt, p1, p2, pathspecs):
    """File-diff mode for --no-index."""
    import fnmatch
    pairs = list(_noindex_pairs(p1, p2))
    if pathspecs and os.path.isdir(p1) and os.path.isdir(p2):
        def matched(path):
            base = os.path.basename(path)
            for spec in pathspecs:
                if (fnmatch.fnmatch(path, spec) or fnmatch.fnmatch(base, spec)
                        or path == spec or path.startswith(spec.rstrip("/") + "/")):
                    return True
            return False
        pairs = [pr for pr in pairs if matched(pr[0])]

    # Drop identical files (diff --no-index emits nothing for them).
    diffs = []
    for path, local, remote, merged in pairs:
        if local != os.devnull and remote != os.devnull:
            try:
                if _same_file(local, remote):
                    continue
            except OSError:
                pass
        diffs.append((path, local, remote, merged))

    total = len(diffs)
    counter = 0
    for path, local, remote, merged in diffs:
        counter += 1
        status = _launch(repo, dt, merged, local, remote, counter, total)
        if status == _INIT_FAILED or status >= 126:
            _external_diff_died(merged)
            return 128
        if status != 0 and dt.trust_exit_code:
            _external_diff_died(merged)
            return 128
    return 1 if diffs else 0


def _same_file(a, b):
    with open(a, "rb") as fa, open(b, "rb") as fb:
        return fa.read() == fb.read()


# ---------------------------------------------------------------------------
# Dir-diff mode (-d): build left/right temp trees, run the tool once, copy back.


def _ensure_parent(path):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)


def _write_left(ldir, ch):
    """Checkout the old version of a changed path into the left tree."""
    if not ch.a.present:
        return
    p = os.path.join(ldir, ch.src_path)
    _ensure_parent(p)
    with open(p, "wb") as f:
        f.write(ch.a.data or b"")
    _apply_mode(p, ch.a.mode)


def _write_right(repo, rdir, wtdir, ch, use_symlinks):
    """Materialise the new version into the right tree (symlink/copy worktree
    files, checkout tree/index versions)."""
    if not ch.b.present:
        return
    dst = os.path.join(rdir, ch.path)
    # Symlinks (and submodules) are written as regular text files holding the
    # readlink/standin content, so a Git-unaware tool compares them as text.
    if ch.b.mode == "120000":
        _ensure_parent(dst)
        if os.path.lexists(dst):
            os.unlink(dst)
        with open(dst, "wb") as f:
            f.write(ch.b.data or b"")
        return
    if ch.b.worktree:
        src = os.path.join(wtdir, ch.path)
        # use_wt_file: reuse the worktree file directly.
        _ensure_parent(dst)
        if use_symlinks:
            try:
                os.symlink(os.path.abspath(src), dst)
            except OSError:
                pass
        else:
            try:
                st = os.stat(src)
                mode = st.st_mode
            except OSError:
                mode = 0o644
            shutil.copyfile(src, dst)
            os.chmod(dst, mode & 0o777)
    else:
        _ensure_parent(dst)
        with open(dst, "wb") as f:
            f.write(ch.b.data or b"")
        _apply_mode(dst, ch.b.mode)


def _apply_mode(path, mode):
    try:
        if mode == "100755":
            os.chmod(path, 0o755)
        elif mode == "100644":
            os.chmod(path, 0o644)
    except OSError:
        pass


def _run_dir_diff(repo, dt, changes):
    """run_dir_diff: ports builtin/difftool.c run_dir_diff()."""
    if not changes:
        return 0

    tmp = os.environ.get("TMPDIR") or "/tmp"
    tmp = tmp.rstrip("/") or "/"
    tmpdir = tempfile.mkdtemp(prefix="git-difftool.", dir=tmp)
    ldir = os.path.join(tmpdir, "left")
    rdir = os.path.join(tmpdir, "right")
    os.mkdir(ldir, 0o700)
    os.mkdir(rdir, 0o700)
    wtdir = str(repo.path)

    use_symlinks = dt.symlinks
    # Track worktree-file entries for the copy-back pass.
    wt_entries = []  # (name, original_worktree_content)
    for ch in changes:
        _write_left(ldir, ch)
        _write_right(repo, rdir, wtdir, ch, use_symlinks)
        # Only non-symlink worktree files participate in the copy-back pass
        # (git's wtindex is built from `rmode && !S_ISLNK(rmode)`).
        if ch.b.present and ch.b.worktree and ch.b.mode != "120000":
            wt_entries.append(ch.path)

    # The tool receives the directory paths with a trailing slash, exactly as
    # builtin/difftool.c keeps ldir/rdir set to ".../left/" and ".../right/".
    ldir_arg = ldir + "/"
    rdir_arg = rdir + "/"
    if dt.extcmd:
        ret = _run_dir_extcmd(dt.extcmd, ldir_arg, rdir_arg)
    else:
        ret = _run_dir_tool(repo, dt, ldir_arg, rdir_arg)

    # Copy-back: if a right-side file was modified by the tool and differs from
    # the worktree, copy it back (skip symlinks the tool edited in place).
    err = False
    for name in wt_entries:
        rpath = os.path.join(rdir, name)
        try:
            st = os.lstat(rpath)
        except OSError:
            continue
        if use_symlinks and _stat.S_ISLNK(st.st_mode):
            continue
        if not _stat.S_ISREG(st.st_mode):
            continue
        wtpath = os.path.join(wtdir, name)
        try:
            with open(rpath, "rb") as f:
                rcontent = f.read()
            with open(wtpath, "rb") as f:
                wcontent = f.read()
        except OSError:
            continue
        if rcontent != wcontent:
            try:
                os.unlink(wtpath)
                shutil.copyfile(rpath, wtpath)
            except OSError:
                _eprint("warning: could not copy '%s' to '%s'" % (rpath, wtpath))

    if err:
        _eprint("warning: temporary files exist in '%s'." % tmpdir)
        _eprint("warning: you may want to cleanup or recover these.")
        return 1
    shutil.rmtree(tmpdir, ignore_errors=True)
    if ret:
        _eprint("warning: failed: %d" % ret)
    # builtin/difftool.c: return (ret < 0) ? 1 : ret;
    return 1 if ret < 0 else ret


def _run_dir_extcmd(extcmd, ldir, rdir):
    """For dir-diff with extcmd, git execs extcmd directly (no shell) with the
    two dir arguments appended.

    prepare_cmd: if the program contains no '/', resolve it via PATH first and
    fail with 'error: cannot run <cmd>' if absent; otherwise exec it directly
    and let an exec failure surface as 'fatal: cannot exec <cmd>'."""
    program = extcmd
    if "/" not in program:
        resolved = shutil.which(program)
        if resolved is None:
            _eprint("error: cannot run %s: No such file or directory" % extcmd)
            return -1
        program = resolved
    try:
        return subprocess.call([program, ldir, rdir], env=dict(os.environ))
    except FileNotFoundError:
        _eprint("fatal: cannot exec '%s': No such file or directory" % extcmd)
        return -1
    except OSError as e:
        _eprint("fatal: cannot exec '%s': %s" % (extcmd, e.strerror))
        return -1


def _run_dir_tool(repo, dt, ldir, rdir):
    """For dir-diff without extcmd, the helper runs the tool once on the dirs.

    On init failure the helper exits 1 (after emitting the setup_tool error);
    run_dir_diff turns that into ret=1 and 'warning: failed: 1'."""
    ok, msg = _setup_tool(repo, dt.tool)
    if not ok:
        _eprint(msg)
        return 1
    cmd = _get_merge_tool_cmd(repo, dt.tool)
    if cmd is None:
        meta = _BUILTIN_TOOLS.get(_base_script(dt.tool))
        cmd = meta.diff_cmd if meta else None
    if cmd is None:
        return 1
    path = _get_merge_tool_path(repo, dt.tool)
    if path is None:
        return 1
    env = dict(os.environ)
    env["LOCAL"] = ldir
    env["REMOTE"] = rdir
    env["merge_tool_path"] = path
    env.setdefault("GIT_PREFIX", env.get("GIT_PREFIX") or ".")
    return subprocess.call(["/bin/sh", "-c", cmd], env=env)


# ---------------------------------------------------------------------------
# Usage / help text (byte-exact).

_USAGE_LINE = "usage: git difftool [<options>] [<commit> [<commit>]] [--] [<path>...]"

_HELP = _USAGE_LINE + "\n\n" + (
    "    -g, --[no-]gui        use `diff.guitool` instead of `diff.tool`\n"
    "    -d, --[no-]dir-diff   perform a full-directory diff\n"
    "    -y, --no-prompt       do not prompt before launching a diff tool\n"
    "    --[no-]symlinks       use symlinks in dir-diff mode\n"
    "    -t, --[no-]tool <tool>\n"
    "                          use the specified diff tool\n"
    "    --[no-]tool-help      print a list of diff tools that may be used with `--tool`\n"
    "    --[no-]trust-exit-code\n"
    "                          make 'git-difftool' exit when an invoked diff tool returns a non-zero exit code\n"
    "    -x, --[no-]extcmd <command>\n"
    "                          specify a custom command for viewing diffs\n"
    "    --no-index            passed to `diff`\n"
    "    --index               opposite of --no-index\n"
    "\n"
)


def _print_tool_help():
    """print_tool_help: git mergetool --tool-help=diff (ported)."""
    tab = "\t"
    lf = "\n"
    avail = []
    not_avail = []
    for name in sorted(_BUILTIN_TOOLS):
        meta = _BUILTIN_TOOLS[name]
        if not meta.can_diff:
            continue
        help_txt = meta.help
        if name not in _TERMINAL_TOOLS:
            help_txt += " (requires a graphical session)"
        line = "%s%s%-15s  %s" % (tab, tab, name, help_txt)
        if _is_available(name):
            avail.append(line)
        else:
            not_avail.append(line)
    out = []
    any_shown = False
    if avail:
        out.append("'git difftool --tool=<tool>' may be set to one of the following:")
        out.extend(avail)
        any_shown = True
    else:
        out.append("No suitable tool for 'git difftool --tool=<tool>' found.")
    if not_avail:
        out.append("")
        out.append("The following tools are valid, but not currently available:")
        out.extend(not_avail)
        any_shown = True
    if any_shown:
        out.append("")
        out.append("Some of the tools listed above only work in a windowed")
        out.append("environment. If run in a terminal-only session, they will fail.")
    sys.stdout.write("\n".join(out) + "\n")
    return 0


# ---------------------------------------------------------------------------
# Argument parsing + main entry point.


def _parse_value_opt(args, i, names):
    """Return (value, consumed) if args[i] matches a value option in names
    (either --opt=v, --opt v, or short -x v / -xv), else (None, 0)."""
    a = args[i]
    for short, long in names:
        if long and a == long:
            if i + 1 >= len(args):
                return ("", 1)  # missing value handled by caller
            return (args[i + 1], 2)
        if long and a.startswith(long + "="):
            return (a[len(long) + 1:], 1)
        if short and a == short:
            if i + 1 >= len(args):
                return ("", 1)
            return (args[i + 1], 2)
        if short and len(short) == 2 and a.startswith(short) and len(a) > 2:
            return (a[2:], 1)
    return (None, 0)


def cmd_difftool(argv):
    have_repo = True
    try:
        repo = _open_repo()
    except Exception:
        repo = None
        have_repo = False

    # difftool_config runs (and validates) before option parsing, so a bad
    # boolean dies even for -h / --tool-help.
    try:
        _validate_eager_config(repo)
    except _BadBool as e:
        _eprint("fatal: bad boolean config value '%s' for '%s'" % (e.value, e.key))
        return 128

    dt = _DT()
    use_gui = None  # -1 unset
    dir_diff = False
    prompt = -1
    tool_help = False
    no_index = False
    trust_exit_code = None  # config default applied later
    symlinks = None  # config default applied later
    difftool_cmd = None
    extcmd = None

    # Separate difftool options from passthrough (diff) args, mirroring
    # PARSE_OPT_KEEP_UNKNOWN_OPT | PARSE_OPT_KEEP_DASHDASH.
    passthrough = []
    i = 0
    n = len(argv)
    while i < n:
        a = argv[i]
        if a == "--":
            passthrough.append(a)
            i += 1
            while i < n:
                passthrough.append(argv[i])
                i += 1
            break
        if a in ("-h", "--help"):
            sys.stdout.write(_HELP)
            return 129
        if a in ("-g", "--gui"):
            use_gui = 1
            i += 1
            continue
        if a == "--no-gui":
            use_gui = 0
            i += 1
            continue
        if a in ("-d", "--dir-diff"):
            dir_diff = True
            i += 1
            continue
        if a == "--no-dir-diff":
            dir_diff = False
            i += 1
            continue
        if a in ("-y", "--no-prompt"):
            prompt = 0
            i += 1
            continue
        if a == "--prompt":
            prompt = 1
            i += 1
            continue
        if a == "--symlinks":
            symlinks = True
            i += 1
            continue
        if a == "--no-symlinks":
            symlinks = False
            i += 1
            continue
        if a == "--tool-help":
            tool_help = True
            i += 1
            continue
        if a == "--trust-exit-code":
            trust_exit_code = True
            i += 1
            continue
        if a == "--no-trust-exit-code":
            trust_exit_code = False
            i += 1
            continue
        if a == "--no-index":
            no_index = True
            i += 1
            continue
        if a == "--index":
            no_index = False
            i += 1
            continue
        val, consumed = _parse_value_opt(argv, i, [("-t", "--tool")])
        if consumed:
            difftool_cmd = val
            i += consumed
            continue
        val, consumed = _parse_value_opt(argv, i, [("-x", "--extcmd")])
        if consumed:
            extcmd = val
            i += consumed
            continue
        # Unknown option or positional: forward to diff.
        passthrough.append(a)
        i += 1

    if tool_help:
        return _print_tool_help()

    if not no_index and not have_repo:
        _eprint("fatal: difftool requires worktree or --no-index")
        return 128

    if no_index and dir_diff:
        _eprint("fatal: options '--dir-diff' and '--no-index' cannot be used together")
        return 128

    # die_for_incompatible_opt3(--gui, --tool, --extcmd)
    flags = []
    if use_gui == 1:
        flags.append("--gui")
    if difftool_cmd is not None:
        flags.append("--tool")
    if extcmd is not None:
        flags.append("--extcmd")
    if len(flags) >= 2:
        _eprint("fatal: options '%s' and '%s' cannot be used together" % (flags[0], flags[1]))
        return 128

    if difftool_cmd is not None and difftool_cmd == "":
        _eprint("fatal: no <tool> given for --tool=<tool>")
        return 128
    if extcmd is not None and extcmd == "":
        _eprint("fatal: no <cmd> given for --extcmd=<cmd>")
        return 128

    # Resolve config-driven defaults: difftool.trustexitcode, core.symlinks.
    if trust_exit_code is None:
        trust_exit_code = _config_bool(gitconfig.get(repo, "difftool.trustexitcode"), False)
    dt.trust_exit_code = trust_exit_code
    if symlinks is None:
        symlinks = _config_bool(gitconfig.get(repo, "core.symlinks"), True)
    dt.symlinks = symlinks

    dt.extcmd = extcmd

    # Resolve the prompt decision (file-diff only; dir-diff prompts too).
    dt.prompt_active = _should_prompt(repo, prompt)

    # Split passthrough into revs / paths for change computation.
    cached, revs, paths, noindex_paths = _split_diff_args(passthrough)

    if no_index:
        return _do_noindex(repo, dt, noindex_paths, difftool_cmd, use_gui)

    changes = _compute_changes(repo, cached, revs, paths)

    # git resolves/guesses the diff tool lazily, per file pair (the
    # "diff.tool is not configured" guess only fires when a pair is actually
    # processed). With no file pairs to show, difftool is silent and exits 0 —
    # so skip tool selection entirely on an empty diff.
    if not changes:
        return 0

    # Resolve the tool (unless using extcmd).
    if not extcmd:
        if difftool_cmd:
            dt.tool = difftool_cmd
        else:
            tool, status = _get_merge_tool(repo, _gui_flag(use_gui))
            if status is not None and status > 1:
                return status
            if tool is None:
                # guess failed -> file-diff: external diff dies on first path
                dt.tool = None
            else:
                dt.tool = tool

    if dir_diff:
        return _run_dir_diff(repo, dt, changes)
    return _run_file_diff(repo, dt, changes)


def _gui_flag(use_gui):
    if use_gui == 1:
        return True
    if use_gui == 0:
        return False
    return None


def _do_noindex(repo, dt, noindex_paths, difftool_cmd, use_gui):
    # git diff --no-index argument validation.
    if len(noindex_paths) < 2:
        sys.stderr.write(_noindex_usage())
        return 129
    if len(noindex_paths) > 2:
        # Extra args are only allowed as pathspecs when both paths are dirs.
        p1, p2 = noindex_paths[0], noindex_paths[1]
        if not (os.path.isdir(p1) and os.path.isdir(p2)):
            _eprint("warning: Limiting comparison with pathspecs is only "
                    "supported if both paths are directories.")
            sys.stderr.write(_noindex_usage())
            return 129
    p1 = noindex_paths[0]
    p2 = noindex_paths[1]
    pathspecs = noindex_paths[2:]
    # dir-vs-file: join the file's basename onto the directory.
    p1, p2, errpath = _noindex_normalize(p1, p2)
    if errpath is not None:
        _eprint("error: Could not access '%s'" % errpath)
        return 1
    if not dt.extcmd:
        if difftool_cmd:
            dt.tool = difftool_cmd
        else:
            tool, status = _get_merge_tool(repo, _gui_flag(use_gui))
            if status is not None and status > 1:
                return status
            dt.tool = tool
    return _run_noindex(repo, dt, p1, p2, pathspecs)


def _noindex_normalize(p1, p2):
    d1 = os.path.isdir(p1)
    d2 = os.path.isdir(p2)
    if d1 and not d2:
        if not os.path.lexists(p2):
            return p1, p2, p2
        joined = os.path.join(p1, os.path.basename(p2))
        if not os.path.lexists(joined):
            return p1, p2, joined
        return joined, p2, None
    if d2 and not d1:
        if not os.path.lexists(p1):
            return p1, p2, p1
        joined = os.path.join(p2, os.path.basename(p1))
        if not os.path.lexists(joined):
            return p1, p2, joined
        return p1, joined, None
    if not d1 and not d2:
        if not os.path.lexists(p1):
            return p1, p2, p1
        if not os.path.lexists(p2):
            return p1, p2, p2
    return p1, p2, None


def _split_diff_args(passthrough):
    """Split diff passthrough args into (cached, revs, paths, noindex_paths)."""
    cached = False
    revs = []
    paths = []
    noindex_paths = []
    after_dd = False
    for tok in passthrough:
        if after_dd:
            paths.append(tok)
            noindex_paths.append(tok)
        elif tok == "--":
            after_dd = True
        elif tok in ("--cached", "--staged"):
            cached = True
        elif tok.startswith("-"):
            continue
        else:
            revs.append(tok)
            noindex_paths.append(tok)
    return cached, revs, paths, noindex_paths


def _open_repo():
    from . import cli as _cli
    return _cli._repo()
