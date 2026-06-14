#!/usr/bin/env python3
"""Extract built-in command and option metadata from a C Git source tree.

This intentionally reads C Git's implementation files instead of hand-maintained
documentation. The manifest is not a complete parser for parse-options, but it
captures the command registry flags and the option spellings that drive CLI
parity tests.
"""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


COMMAND_RE = re.compile(
    r'\{\s*"(?P<name>[^"]+)"\s*,\s*'
    r"(?P<function>cmd_[A-Za-z0-9_]+)"
    r"(?:\s*,\s*(?P<flags>[^{}]*?))?\s*\}",
    re.S,
)
FUNCTION_RE = re.compile(r"\bint\s+(cmd_[A-Za-z0-9_]+)\s*\(")
FUNCTION_DEF_RE = re.compile(
    r"(?m)^\s*(?:static\s+)?(?:const\s+)?(?:int|void|enum\s+\w+|struct\s+\w+\s*\*?|[A-Za-z_][A-Za-z0-9_]*\s*\*?)"
    r"\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\([^;{}]*?\)\s*\{",
    re.S,
)
OPTION_ARRAY_RE = re.compile(
    r"\b(?:static\s+)?(?:const\s+)?struct\s+option\s+"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\[\]\s*=\s*\{"
)
OPT_CALL_RE = re.compile(r"\b(OPT_[A-Z0-9_]+)\s*\(")
STRUCT_OPTION_RE = re.compile(r"\{(?P<body>[^{}]*?\.long_name\s*=[^{}]*?)\}", re.S)

CONVENIENCE_OPTIONS: dict[str, tuple[tuple[str, ...], bool]] = {
    "OPT__ABBREV": (("--abbrev",), True),
    "OPT__COLOR": (("--color",), True),
    "OPT__DRY_RUN": (("-n", "--dry-run"), False),
    "OPT__FORCE": (("-f", "--force"), False),
    "OPT__QUIET": (("-q", "--quiet"), False),
    "OPT__VERBOSE": (("-v", "--verbose"), False),
    "OPT_DATE": (("--date",), True),
    "OPT_DIFF_INTERHUNK_CONTEXT": (("--inter-hunk-context",), True),
    "OPT_DIFF_UNIFIED": (("-U", "--unified"), True),
    "OPT_IPVERSION": (("-4", "-6"), False),
    "OPT_PATHSPEC_FILE_NUL": (("--pathspec-file-nul",), False),
    "OPT_PATHSPEC_FROM_FILE": (("--pathspec-from-file",), True),
}

VALUE_MACRO_MARKERS = (
    "CALLBACK",
    "COUNTUP",
    "DATE",
    "DOUBLE",
    "EXPIRY_DATE",
    "FILENAME",
    "INTEGER",
    "MAGNITUDE",
    "NUMBER",
    "OPTIONAL_VALUE",
    "PATH",
    "STRING",
    "STRING_LIST",
    "TIME",
    "UINT",
    "ULONG",
    "VALUE",
)


@dataclass(frozen=True)
class SourceText:
    path: Path
    text: str
    display_path: str | None = None

    def line_for(self, offset: int) -> int:
        return self.text.count("\n", 0, offset) + 1

    def source_at(self, offset: int) -> str:
        path = self.display_path or self.path.as_posix()
        return f"{path}:{self.line_for(offset)}"


def _strip_comments(text: str) -> str:
    out: list[str] = []
    i = 0
    state = "code"
    while i < len(text):
        c = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if state == "code":
            if c == '"':
                state = "string"
                out.append(c)
            elif c == "'":
                state = "char"
                out.append(c)
            elif c == "/" and nxt == "/":
                out.extend("  ")
                i += 2
                while i < len(text) and text[i] != "\n":
                    out.append(" ")
                    i += 1
                continue
            elif c == "/" and nxt == "*":
                out.extend("  ")
                i += 2
                while i < len(text) - 1 and not (text[i] == "*" and text[i + 1] == "/"):
                    out.append("\n" if text[i] == "\n" else " ")
                    i += 1
                if i < len(text) - 1:
                    out.extend("  ")
                    i += 2
                continue
            else:
                out.append(c)
        elif state == "string":
            out.append(c)
            if c == "\\" and nxt:
                out.append(nxt)
                i += 1
            elif c == '"':
                state = "code"
        elif state == "char":
            out.append(c)
            if c == "\\" and nxt:
                out.append(nxt)
                i += 1
            elif c == "'":
                state = "code"
        i += 1
    return "".join(out)


def _find_matching(text: str, open_at: int, opener: str = "(", closer: str = ")") -> int:
    depth = 0
    state = "code"
    i = open_at
    while i < len(text):
        c = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if state == "code":
            if c == '"':
                state = "string"
            elif c == "'":
                state = "char"
            elif c == opener:
                depth += 1
            elif c == closer:
                depth -= 1
                if depth == 0:
                    return i
        elif state == "string":
            if c == "\\" and nxt:
                i += 1
            elif c == '"':
                state = "code"
        elif state == "char":
            if c == "\\" and nxt:
                i += 1
            elif c == "'":
                state = "code"
        i += 1
    raise ValueError(f"unmatched {opener!r} at offset {open_at}")


def _extract_initializer(text: str, marker: str) -> str:
    start = text.index(marker)
    open_at = text.index("{", start)
    close_at = _find_matching(text, open_at, "{", "}")
    return text[open_at + 1 : close_at]


def _split_top_level_args(raw: str) -> list[str]:
    args: list[str] = []
    start = 0
    depth = 0
    state = "code"
    i = 0
    while i < len(raw):
        c = raw[i]
        nxt = raw[i + 1] if i + 1 < len(raw) else ""
        if state == "code":
            if c == '"':
                state = "string"
            elif c == "'":
                state = "char"
            elif c in "([{":
                depth += 1
            elif c in ")]}":
                depth -= 1
            elif c == "," and depth == 0:
                args.append(raw[start:i].strip())
                start = i + 1
        elif state == "string":
            if c == "\\" and nxt:
                i += 1
            elif c == '"':
                state = "code"
        elif state == "char":
            if c == "\\" and nxt:
                i += 1
            elif c == "'":
                state = "code"
        i += 1
    tail = raw[start:].strip()
    if tail:
        args.append(tail)
    return args


def _string_literal(arg: str) -> str | None:
    match = re.match(r'\s*"((?:\\.|[^"\\])*)"', arg, re.S)
    if not match:
        return None
    return bytes(match.group(1), "utf-8").decode("unicode_escape")


def _char_literal(arg: str) -> str | None:
    match = re.match(r"\s*'((?:\\.|[^'\\])*)'", arg, re.S)
    if not match:
        return None
    value = bytes(match.group(1), "utf-8").decode("unicode_escape")
    return value if len(value) == 1 else None


def _short_name(arg: str) -> str | None:
    char = _char_literal(arg)
    if char and char != "\0":
        return f"-{char}"
    stripped = arg.strip()
    if stripped in {"0", "NULL"}:
        return None
    if re.fullmatch(r"-?\d+", stripped) and int(stripped) != 0:
        return f"-{chr(int(stripped))}"
    return None


def _macro_arg_positions(macro: str) -> tuple[int | None, int | None]:
    if macro in CONVENIENCE_OPTIONS:
        return None, None
    if macro in {"OPT_SUBCOMMAND", "OPT_SUBCOMMAND_F"}:
        return None, 0
    if macro == "OPT_ALIAS":
        return 0, 1
    if macro.startswith("OPT_"):
        return 0, 1
    return None, None


def _names_from_macro(macro: str, args: list[str]) -> list[str]:
    names: list[str] = []
    if macro in CONVENIENCE_OPTIONS:
        return list(CONVENIENCE_OPTIONS[macro][0])

    short_pos, long_pos = _macro_arg_positions(macro)
    if short_pos is not None and short_pos < len(args):
        short = _short_name(args[short_pos])
        if short:
            names.append(short)
    if long_pos is not None and long_pos < len(args):
        long = _string_literal(args[long_pos])
        if long:
            if macro in {"OPT_SUBCOMMAND", "OPT_SUBCOMMAND_F"}:
                names.append(long)
            else:
                names.append(f"--{long}")
    return names


def _takes_value(macro: str, raw: str) -> bool:
    if macro in CONVENIENCE_OPTIONS:
        return CONVENIENCE_OPTIONS[macro][1]
    if "PARSE_OPT_OPTARG" in raw:
        return True
    if "PARSE_OPT_NOARG" in raw:
        return False
    return any(marker in macro for marker in VALUE_MACRO_MARKERS)


def _parse_flags(raw_flags: str | None) -> list[str]:
    if not raw_flags:
        return []
    return [part.strip() for part in raw_flags.split("|") if part.strip()]


def _parse_command_registry(source: SourceText) -> list[dict[str, Any]]:
    marker = "static struct cmd_struct commands[]"
    start = source.text.index(marker)
    open_at = source.text.index("{", start)
    close_at = _find_matching(source.text, open_at, "{", "}")
    body_start = open_at + 1
    body = source.text[body_start:close_at]
    commands: list[dict[str, Any]] = []
    for match in COMMAND_RE.finditer(body):
        commands.append(
            {
                "name": match.group("name"),
                "function": match.group("function"),
                "flags": _parse_flags(match.group("flags")),
                "registry_source": source.source_at(body_start + match.start()),
            }
        )
    return commands


def _parse_option_calls(source: SourceText, start: int = 0, end: int | None = None) -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    pos = start
    limit = len(source.text) if end is None else end
    while True:
        match = OPT_CALL_RE.search(source.text, pos, limit)
        if not match:
            break
        macro = match.group(1)
        open_at = source.text.index("(", match.end() - 1)
        close_at = _find_matching(source.text, open_at)
        if close_at >= limit:
            break
        raw_args = source.text[open_at + 1 : close_at]
        raw = source.text[match.start() : close_at + 1]
        pos = close_at + 1
        if macro in {"OPT_END", "OPT_GROUP"}:
            continue
        args = _split_top_level_args(raw_args)
        names = _names_from_macro(macro, args)
        if not names and macro not in {"OPT_SUBCOMMAND", "OPT_SUBCOMMAND_F"}:
            continue
        options.append(
            {
                "kind": "subcommand" if macro in {"OPT_SUBCOMMAND", "OPT_SUBCOMMAND_F"} else "option",
                "macro": macro,
                "names": names,
                "takes_value": _takes_value(macro, raw),
                "hidden": "HIDDEN" in macro or "PARSE_OPT_HIDDEN" in raw,
                "optional_value": "PARSE_OPT_OPTARG" in raw,
                "no_negated_form": "PARSE_OPT_NONEG" in raw,
                "source": source.source_at(match.start()),
                "raw": " ".join(raw.split()),
            }
        )
    return options


def _parse_struct_options(source: SourceText, start: int = 0, end: int | None = None) -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    limit = len(source.text) if end is None else end
    for match in STRUCT_OPTION_RE.finditer(source.text, start, limit):
        body = match.group("body")
        names: list[str] = []
        short_match = re.search(r"\.short_name\s*=\s*([^,\n]+)", body)
        if short_match:
            short = _short_name(short_match.group(1))
            if short:
                names.append(short)
        long_match = re.search(r'\.long_name\s*=\s*"([^"]+)"', body)
        if long_match:
            names.append(f"--{long_match.group(1)}")
        if not names:
            continue
        options.append(
            {
                "kind": "option",
                "macro": "STRUCT_OPTION",
                "names": names,
                "takes_value": "OPTION_STRING" in body or "OPTION_INTEGER" in body or "OPTION_CALLBACK" in body,
                "hidden": "PARSE_OPT_HIDDEN" in body,
                "optional_value": "PARSE_OPT_OPTARG" in body,
                "no_negated_form": "PARSE_OPT_NONEG" in body,
                "source": source.source_at(match.start()),
                "raw": " ".join(body.split()),
            }
        )
    return options


def _dedupe_options(options: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()
    deduped: list[dict[str, Any]] = []
    for option in options:
        key = (
            option["kind"],
            option["macro"],
            tuple(option["names"]),
            option["source"],
            option["raw"],
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(option)
    return deduped


def _function_spans(source: SourceText) -> dict[str, tuple[int, int]]:
    spans: dict[str, tuple[int, int]] = {}
    for match in FUNCTION_DEF_RE.finditer(source.text):
        name = match.group("name")
        open_at = source.text.rfind("{", match.start(), match.end())
        if open_at < 0:
            continue
        try:
            close_at = _find_matching(source.text, open_at, "{", "}")
        except ValueError:
            continue
        spans[name] = (open_at, close_at + 1)
    return spans


def _enclosing_function(spans: dict[str, tuple[int, int]], offset: int) -> str | None:
    for name, (start, end) in spans.items():
        if start <= offset < end:
            return name
    return None


def _option_arrays(source: SourceText, spans: dict[str, tuple[int, int]]) -> list[dict[str, Any]]:
    arrays: list[dict[str, Any]] = []
    for match in OPTION_ARRAY_RE.finditer(source.text):
        name = match.group("name")
        open_at = source.text.rfind("{", match.start(), match.end())
        if open_at < 0:
            continue
        try:
            close_at = _find_matching(source.text, open_at, "{", "}")
        except ValueError:
            continue
        arrays.append(
            {
                "name": name,
                "start": match.start(),
                "end": close_at + 1,
                "function": _enclosing_function(spans, match.start()),
                "options": _dedupe_options(
                    [
                        *_parse_option_calls(source, open_at, close_at + 1),
                        *_parse_struct_options(source, open_at, close_at + 1),
                    ]
                ),
            }
        )
    return arrays


def _called_functions(source: SourceText, span: tuple[int, int], function_names: set[str]) -> set[str]:
    body = source.text[span[0] : span[1]]
    called: set[str] = set()
    for name in function_names:
        if re.search(rf"\b{re.escape(name)}\s*\(", body):
            called.add(name)
    return called


def _subcommand_callbacks(option: dict[str, Any]) -> set[str]:
    if option["kind"] != "subcommand":
        return set()
    match = re.match(r"OPT_SUBCOMMAND(?:_F)?\((.*)\)$", option["raw"], re.S)
    if not match:
        return set()
    args = _split_top_level_args(match.group(1))
    if len(args) < 3:
        return set()
    callback = args[2].strip()
    return {callback} if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", callback) else set()


def _options_for_function(source: SourceText, function: str) -> list[dict[str, Any]]:
    spans = _function_spans(source)
    arrays = _option_arrays(source, spans)
    if function not in spans:
        return []

    function_names = set(spans)
    reachable = {function}
    selected_arrays: set[int] = set()
    changed = True
    while changed:
        changed = False
        for name in list(reachable):
            for called in _called_functions(source, spans[name], function_names):
                if called not in reachable:
                    reachable.add(called)
                    changed = True

        reachable_body = "\n".join(source.text[spans[name][0] : spans[name][1]] for name in reachable)
        for idx, array in enumerate(arrays):
            if array["function"] in reachable or re.search(rf"\b{re.escape(array['name'])}\b", reachable_body):
                if idx not in selected_arrays:
                    selected_arrays.add(idx)
                    changed = True
                    for option in array["options"]:
                        for callback in _subcommand_callbacks(option):
                            if callback in spans and callback not in reachable:
                                reachable.add(callback)
                                changed = True

    selected: list[dict[str, Any]] = []
    for idx, array in enumerate(arrays):
        if idx in selected_arrays:
            selected.extend(array["options"])
    return _dedupe_options(selected)


def _builtin_sources(git_source: Path) -> dict[str, SourceText]:
    sources: dict[str, SourceText] = {}
    for path in sorted((git_source / "builtin").glob("*.c")):
        display_path = path.relative_to(git_source).as_posix()
        sources[path.as_posix()] = SourceText(path, _strip_comments(path.read_text(encoding="utf-8")), display_path)
    return sources


def _function_sources(sources: Iterable[SourceText]) -> dict[str, SourceText]:
    mapping: dict[str, SourceText] = {}
    for source in sources:
        for match in FUNCTION_RE.finditer(source.text):
            mapping.setdefault(match.group(1), source)
    return mapping


def build_manifest(git_source: Path, version: str) -> dict[str, Any]:
    git_source = git_source.resolve()
    git_c = SourceText(
        git_source / "git.c",
        _strip_comments((git_source / "git.c").read_text(encoding="utf-8")),
        "git.c",
    )
    builtin_sources = _builtin_sources(git_source)
    function_sources = _function_sources(builtin_sources.values())
    commands = _parse_command_registry(git_c)

    option_cache: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for command in commands:
        source = function_sources.get(command["function"])
        if source is None:
            command["source"] = None
            command["options"] = []
            continue
        source_key = source.path.as_posix()
        command["source"] = source.display_path or source_key
        cache_key = (source_key, command["function"])
        if cache_key not in option_cache:
            option_cache[cache_key] = _options_for_function(source, command["function"])
        command["options"] = option_cache[cache_key]

    return {
        "git_version": version,
        "source": git_source.name,
        "command_count": len(commands),
        "commands": commands,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--git-source", required=True, type=Path, help="path to a C Git source checkout")
    parser.add_argument("--version", default="2.54.0", help="C Git version label for the manifest")
    parser.add_argument("--output", type=Path, help="write manifest JSON to this path")
    args = parser.parse_args(argv)

    manifest = build_manifest(args.git_source, args.version)
    rendered = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
