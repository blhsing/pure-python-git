from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_manifest_tool_extracts_registry_flags_and_option_spellings(tmp_path: Path):
    source = tmp_path / "git-src"
    builtin = source / "builtin"
    builtin.mkdir(parents=True)
    (source / "git.c").write_text(
        """
        static struct cmd_struct commands[] = {
            { "add", cmd_add, RUN_SETUP | NEED_WORK_TREE },
            { "remote", cmd_remote, RUN_SETUP },
        };
        """,
        encoding="utf-8",
    )
    (builtin / "add.c").write_text(
        """
        int cmd_add(int argc, const char **argv, const char *prefix, struct repository *repo)
        {
            static struct option builtin_add_options[] = {
                OPT__DRY_RUN(&show_only, N_("dry run")),
                OPT_BOOL('i', "interactive", &add_interactive, N_("interactive picking")),
                OPT_STRING(0, "chmod", &chmod_arg, "(+|-)x", N_("override executable bit")),
                OPT_PATHSPEC_FROM_FILE(&pathspec_from_file),
                OPT_END(),
            };
            return 0;
        }
        """,
        encoding="utf-8",
    )
    (builtin / "remote.c").write_text(
        """
        int cmd_remote(int argc, const char **argv, const char *prefix, struct repository *repo)
        {
            struct option options[] = {
                OPT__VERBOSE(&verbose, N_("be verbose")),
                OPT_SUBCOMMAND("add", &fn, add),
                OPT_SUBCOMMAND("show", &fn, show),
                OPT_END()
            };
            return 0;
        }
        """,
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "git_cli_manifest.py"), "--git-source", str(source)],
        text=True,
        capture_output=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    manifest = json.loads(result.stdout)
    assert manifest["git_version"] == "2.54.0"
    assert manifest["command_count"] == 2

    commands = {command["name"]: command for command in manifest["commands"]}
    assert commands["add"]["function"] == "cmd_add"
    assert commands["add"]["flags"] == ["RUN_SETUP", "NEED_WORK_TREE"]

    add_names = {name for option in commands["add"]["options"] for name in option["names"]}
    assert {"-n", "--dry-run", "-i", "--interactive", "--chmod", "--pathspec-from-file"} <= add_names

    remote_entries = commands["remote"]["options"]
    remote_names = {name for option in remote_entries for name in option["names"]}
    assert {"-v", "--verbose", "add", "show"} <= remote_names
    assert any(option["kind"] == "subcommand" and option["names"] == ["add"] for option in remote_entries)
