from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "tests" / "git_parity" / "manifest" / "git-2.54.0.json"


def _manifest_command_names() -> set[str]:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest["git_version"] == "2.54.0"
    return {command["name"] for command in manifest["commands"]}


def test_all_c_git_254_builtins_are_registered_by_exact_name():
    from pythongit.cli import _COMMANDS

    missing = sorted(_manifest_command_names() - set(_COMMANDS))
    assert missing == []


def test_pythongit_command_extensions_are_explicitly_classified():
    from pythongit.cli import _COMMANDS, _PYGIT_EXTENSION_COMMANDS

    c_git_names = _manifest_command_names()
    runtime_extensions = set(_COMMANDS) - c_git_names
    assert runtime_extensions == set(_PYGIT_EXTENSION_COMMANDS)


def test_c_git_compatible_registry_excludes_only_extensions():
    from pythongit.cli import _PYGIT_EXTENSION_COMMANDS, cgit_compatible_commands

    c_git_names = _manifest_command_names()
    compatible = cgit_compatible_commands()
    assert c_git_names <= compatible
    assert compatible.isdisjoint(_PYGIT_EXTENSION_COMMANDS)
