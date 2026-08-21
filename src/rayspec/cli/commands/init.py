# SPDX-License-Identifier: Apache-2.0
"""`rayspec init [--kind code|content] [--force] [--no-skill] [--root DIR]` — scaffold a project.

Boundary: copies the packaged templates (:mod:`rayspec.cli.templates`) into ``<root>/.rayspec/``,
writes the packaged coding-agent skill (:mod:`rayspec.skill`) to ``<root>/.claude/skills/rayspec/``
unless ``--no-skill``, and prints next steps. No loader/engine logic lives here; the templates
are validated by tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Annotated, Literal

import typer
from rich.markup import escape

from rayspec.cli.commands._loader_common import console, err_console, fail
from rayspec.cli.commands._skill_common import print_install_result
from rayspec.skill import install_skill, project_skill_dir

#: The scaffold kinds, one template directory each under ``rayspec.cli.templates``.
TEMPLATE_KINDS: tuple[str, ...] = ("code", "content")

#: Where the scaffold lands, relative to the chosen root.
PROJECT_DIR = ".rayspec"

#: Sub-directories always created (even when a kind ships no file in them).
ALWAYS_DIRS: tuple[str, ...] = ("workflows", "agents", "prompts", "stubs")

ScaffoldAction = Literal["created", "overwritten", "skipped"]


class TemplateKind(StrEnum):
    """``--kind`` values."""

    code = "code"
    content = "content"


@dataclass(frozen=True, slots=True)
class ScaffoldFile:
    """One file of the scaffold: its path relative to the root and what ``scaffold`` did."""

    relative: str
    path: Path
    action: ScaffoldAction


def _template_root(kind: str) -> Traversable:
    if kind not in TEMPLATE_KINDS:
        choices = ", ".join(TEMPLATE_KINDS)
        raise ValueError(f"unknown template kind {kind!r} (choose from {choices})")
    return resources.files("rayspec.cli.templates") / kind


def _walk(node: Traversable, prefix: str = "") -> list[tuple[str, Traversable]]:
    """``[(relative posix path, file)]`` for every file below ``node``, sorted by path."""
    found: list[tuple[str, Traversable]] = []
    for child in node.iterdir():
        rel = f"{prefix}{child.name}"
        if child.is_dir():
            found.extend(_walk(child, f"{rel}/"))
        elif not child.name.startswith((".", "__")) and child.name != "__init__.py":
            found.append((rel, child))
    return sorted(found, key=lambda item: item[0])


def template_files(kind: str) -> list[tuple[str, Traversable]]:
    """The template files of ``kind`` as ``[(".rayspec/<path>", resource)]``."""
    return [(f"{PROJECT_DIR}/{rel}", node) for rel, node in _walk(_template_root(kind))]


#: ``{kind: (".rayspec/workflows/example.yaml", ...)}`` — what each kind writes.
SCAFFOLD_FILES: dict[str, tuple[str, ...]] = {
    kind: tuple(rel for rel, _ in template_files(kind)) for kind in TEMPLATE_KINDS
}


def scaffold(root: Path, *, kind: str = "code", force: bool = False) -> list[ScaffoldFile]:
    """Write the ``kind`` scaffold below ``root``; existing files are kept unless ``force``.

    Returns one :class:`ScaffoldFile` per template file with ``action`` ``created``,
    ``overwritten`` (existed, ``force``) or ``skipped`` (existed, no ``force``). Parent
    directories (and the standard ``.rayspec/{workflows,agents,prompts,stubs}`` set) are created.

    Raises :class:`NotADirectoryError` when ``root`` exists but is not a directory,
    :class:`IsADirectoryError` when a *directory* sits where a template file goes (with or
    without ``force``), and any other :class:`OSError` of the filesystem unchanged — the CLI
    maps them to ``error: …`` + exit 2.
    """
    if root.exists() and not root.is_dir():
        raise NotADirectoryError(f"{root} is not a directory")
    results: list[ScaffoldFile] = []
    for sub in ALWAYS_DIRS:
        (root / PROJECT_DIR / sub).mkdir(parents=True, exist_ok=True)
    for rel, node in template_files(kind):
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_dir():
            raise IsADirectoryError(f"{target} is a directory, expected a file (or nothing)")
        existed = target.exists()
        if existed and not force:
            results.append(ScaffoldFile(rel, target, "skipped"))
            continue
        target.write_text(node.read_text(encoding="utf-8"), encoding="utf-8")
        results.append(ScaffoldFile(rel, target, "overwritten" if existed else "created"))
    return results


def detect_kind(root: Path) -> str | None:
    """The kind whose ``workflows/example.yaml`` template is byte-identical to the one in ``root``.

    ``None`` when there is no example workflow or it was edited (or hand-written) — only an
    untouched scaffold is recognised, so the kind-switch warning never fires on user content.
    """
    existing = root / PROJECT_DIR / "workflows" / "example.yaml"
    try:
        text = existing.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    for kind in TEMPLATE_KINDS:
        for rel, node in template_files(kind):
            if rel == f"{PROJECT_DIR}/workflows/example.yaml":
                if node.read_text(encoding="utf-8") == text:
                    return kind
                break
    return None


def orphan_files(old_kind: str, new_kind: str) -> tuple[str, ...]:
    """Template files of ``old_kind`` that ``new_kind`` does not ship (left behind by a switch)."""
    new = set(SCAFFOLD_FILES[new_kind])
    return tuple(rel for rel in SCAFFOLD_FILES[old_kind] if rel not in new)


def in_git_checkout(path: Path) -> bool:
    """``True`` when ``path`` (or one of its parents) holds a ``.git`` directory *or* file (a
    worktree / submodule checkout). Pure path walk — ``git`` itself is not invoked and the
    directories need not exist yet."""
    return any((candidate / ".git").exists() for candidate in (path, *path.parents))


#: Scaffold kinds whose example workflow shells out to git (``files: git ls-files``).
GIT_DEPENDENT_KINDS: frozenset[str] = frozenset({"code"})


def non_git_warning(target: Path, kind: str) -> str | None:
    """The stderr warning for a git-dependent ``kind`` written outside a git checkout."""
    if kind not in GIT_DEPENDENT_KINDS or in_git_checkout(target):
        return None
    return (
        f"{target} is not a git repository — the scaffold's `files` step uses git ls-files, so "
        "the first real run of `example` would fail; run `git init` here or use "
        "`rayspec init --kind content` for a project that is not a code checkout"
    )


def next_steps(kind: str, *, skill: bool = True) -> list[str]:
    """The commands to try after ``rayspec init`` (printed, and reused by the docs)."""
    stubs = f"{PROJECT_DIR}/stubs/example.yaml"
    real = "rayspec run example" if kind == "code" else 'rayspec run example -i topic="..."'
    lines = [
        "rayspec doctor                          # SDKs, bundled CLIs, auth hints, git",
        "rayspec validate                        # schema, graph, references, capabilities",
        "rayspec plan example                    # inputs, agents/models, step order",
        f"rayspec run example --dry-run --stubs {stubs}   # scripted agents, no login needed",
        f"{real:<39} # a real run",
    ]
    if skill:
        lines.append(
            "open a fresh Claude Code session here   # the rayspec skill in "
            ".claude/skills/rayspec/ loads automatically (rayspec skill show)"
        )
    return lines


def register(app: typer.Typer) -> None:
    @app.command()
    def init(
        kind: Annotated[
            TemplateKind,
            typer.Option(
                "--kind",
                help="Scaffold flavour: `code` (review a checkout; default) or `content` "
                "(draft + review text, `isolation: none`, no shell steps).",
            ),
        ] = TemplateKind.code,
        force: Annotated[
            bool, typer.Option("--force", help="Overwrite files that already exist.")
        ] = False,
        no_skill: Annotated[
            bool,
            typer.Option(
                "--no-skill",
                help="Do not write the coding-agent skill to `.claude/skills/rayspec/`.",
            ),
        ] = False,
        root: Annotated[
            Path | None,
            typer.Option(
                "--root",
                help="Directory to initialise (the one that gets `.rayspec/`). Default: the cwd.",
                show_default=False,
            ),
        ] = None,
    ) -> None:
        """Scaffold `.rayspec/` (example workflow, reviewer agent, prompts, config, stubs) and
        the rayspec skill for coding agents (`.claude/skills/rayspec/`, unless --no-skill)."""
        target = (root or Path.cwd()).resolve()
        out = console()
        err = err_console()
        previous = detect_kind(target)
        try:
            results = scaffold(target, kind=kind.value, force=force)
        except OSError as exc:  # NotADirectoryError / IsADirectoryError / permissions …
            fail(f"cannot write the scaffold: {exc}")
            return  # unreachable: fail() raises typer.Exit
        skill_results = []
        if not no_skill:
            try:
                skill_results = install_skill(project_skill_dir(target), force=force)
            except OSError as exc:
                fail(
                    f"cannot write the skill: {exc} (the {PROJECT_DIR}/ scaffold was written; "
                    "re-run with --no-skill to skip the skill)"
                )
                return  # unreachable
        for item in results:
            if item.action == "skipped":
                out.print(
                    f"[yellow]exists [/yellow]  {item.relative} "
                    "[dim](skipped; use --force to overwrite)[/dim]"
                )
            else:
                verb = "created" if item.action == "created" else "overwrote"
                out.print(f"[green]{verb}[/green]  {item.relative}")
        created = sum(1 for r in results if r.action != "skipped")
        skipped = len(results) - created
        summary = f"{created} file(s) written"
        if skipped:
            summary += f", {skipped} kept"
        out.print(f"[bold]{kind.value}[/bold] scaffold in {target / PROJECT_DIR}: {summary}")
        if skill_results:
            print_install_result(skill_results, project_skill_dir(target), label="project")
            created += sum(1 for r in skill_results if r.action != "skipped")
            skipped += sum(1 for r in skill_results if r.action == "skipped")
        if skipped and not created:
            err.print(
                f"[yellow]warning:[/yellow] nothing written — all {skipped} file(s) exist; "
                "use --force to overwrite them"
            )
        if previous is not None and previous != kind.value:
            orphans = ", ".join(orphan_files(previous, kind.value)) or "none"
            if force:
                err.print(
                    f"[yellow]warning:[/yellow] the existing `{previous}` scaffold was replaced "
                    f"by `{kind.value}`; files only the `{previous}` kind ships are left over "
                    f"(delete them if unused): {orphans}"
                )
            else:
                err.print(
                    f"[yellow]warning:[/yellow] `{target / PROJECT_DIR}` holds a `{previous}` "
                    f"scaffold; --kind {kind.value} added only its extra files, so the "
                    f"project is now a mixed `{previous}`/`{kind.value}` scaffold — use "
                    f"--force to switch (`{previous}`-only files that stay: {orphans})"
                )
        warning = non_git_warning(target, kind.value)
        if warning is not None:
            err.print(f"[yellow]warning:[/yellow] {escape(warning)}", highlight=False)
        out.print("\nnext steps:")
        for line in next_steps(kind.value, skill=not no_skill):
            out.print(f"  {escape(line)}")
