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
            if getattr(param, "hidden", False):
                continue  # an internal option (e.g. --detached-child) is not user documentation
            flags = [o for o in param.opts if o.startswith("--")]
            flags += [o for o in param.secondary_opts if o.startswith("--")]
            if not any(flag in section for flag in flags):
                problems.append(f"rayspec {name}: option {flags} not mentioned")
    assert not problems, "\n".join(problems)


def test_typer_app_is_the_documented_entry_point() -> None:
    assert isinstance(app, typer.Typer)
    assert "run" in cli_commands()


def test_fail_fast_says_it_overrides_the_workflow_failure_policy(docs_dir: Path) -> None:
    """``--fail-fast`` beats ``defaults.on_step_failure`` and may only ever tighten it.

    That is the only place the *only-tightens* rule shows up in a user's day: they set a policy in
    the workflow, pass the flag, and get something other than what the file says. Neither the help
    text nor the option table said the two interact at all, which left the schema reference as the
    only way to find out — and nobody reads it to look up a flag.
    """
    commands = cli_commands()
    for name in ("run", "resume"):
        flag = [param for param in commands[name].params if "--fail-fast" in param.opts]
        assert flag, f"rayspec {name} has no --fail-fast"
        help_text = flag[0].help or ""
        assert "on_step_failure" in help_text, (
            f"rayspec {name}: --fail-fast never says it overrides `defaults.on_step_failure` "
            f"— its help is {help_text!r}"
        )

    rows = [
        line
        for line in documented_commands(docs_dir)["run"].splitlines()
        if line.startswith("| `--fail-fast`")
    ]
    assert len(rows) == 1, rows
    assert "on_step_failure" in rows[0], "the option table does not say what the flag overrides"
    assert "tighten" in rows[0], "the option table does not say the override may only tighten"


def test_detached_child_is_a_hidden_run_option() -> None:
    """PRD-07 R1: ``--detached-child`` is the launcher→child handshake, never a user knob — it
    must exist (the launcher passes it) yet be hidden from ``--help`` and from the docs."""
    run = cli_commands()["run"]
    hidden = {
        opt
        for param in run.params
        if getattr(param, "hidden", False)
        for opt in param.opts
        if opt.startswith("--")
    }
    assert "--detached-child" in hidden, "the child option must be present but hidden"
