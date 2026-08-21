"""``docs/cli.md`` documents exactly the commands the Typer app exposes (and every option)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import typer
from typer.main import get_command

from rayspec.cli.app import app

_COMMAND_HEADING_RE = re.compile(r"^###\s+`rayspec ([a-z][a-z0-9 -]*?)`\s*$", re.MULTILINE)


def cli_commands() -> dict[str, Any]:
    """``{"run": cmd, "projects add": cmd, ...}`` for every leaf command of the app.

    Duck-typed over the (vendored) click objects: a group has a ``commands`` mapping.
    """
    root = get_command(app)
    assert hasattr(root, "commands")
    found: dict[str, Any] = {}

    def walk(group: Any, prefix: str) -> None:
        for name in sorted(group.commands):
            cmd = group.commands[name]
            full = f"{prefix}{name}"
            if hasattr(cmd, "commands"):
                # a group that runs without a subcommand (`rayspec runs`) is itself a command
                if getattr(cmd, "invoke_without_command", False):
                    found[full] = cmd
                walk(cmd, f"{full} ")
            else:
                found[full] = cmd

    walk(root, "")
    return found


def documented_commands(docs_dir: Path) -> dict[str, str]:
    """``{command: section text}`` for every ``### \\`rayspec <command>\\``` heading in cli.md."""
    text = (docs_dir / "cli.md").read_text(encoding="utf-8")
    matches = list(_COMMAND_HEADING_RE.finditer(text))
    sections: dict[str, str] = {}
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections[match.group(1).strip()] = text[match.start() : end]
    return sections


def test_every_cli_command_is_documented(docs_dir: Path) -> None:
    missing = sorted(set(cli_commands()) - set(documented_commands(docs_dir)))
    assert not missing, f"docs/cli.md lacks a `### rayspec <cmd>` section for: {missing}"


def test_every_documented_command_exists(docs_dir: Path) -> None:
    extra = sorted(set(documented_commands(docs_dir)) - set(cli_commands()))
    assert not extra, f"docs/cli.md documents commands the CLI does not have: {extra}"


def test_every_option_is_mentioned_in_its_section(docs_dir: Path) -> None:
    sections = documented_commands(docs_dir)
    problems: list[str] = []
    for name, cmd in cli_commands().items():
        section = sections.get(name, "")
        for param in cmd.params:
            if getattr(param, "param_type_name", "") != "option" or param.name == "help":
                continue
            flags = [o for o in param.opts if o.startswith("--")]
            flags += [o for o in param.secondary_opts if o.startswith("--")]
            if not any(flag in section for flag in flags):
                problems.append(f"rayspec {name}: option {flags} not mentioned")
    assert not problems, "\n".join(problems)


def test_typer_app_is_the_documented_entry_point() -> None:
    assert isinstance(app, typer.Typer)
    assert "run" in cli_commands()
