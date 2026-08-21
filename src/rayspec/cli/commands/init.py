# SPDX-License-Identifier: Apache-2.0
"""`rayspec init [--kind code|content | --from EXAMPLE] [--force] [--no-skill] [--root DIR]`.

Boundary: copies the packaged templates (:mod:`rayspec.cli.templates`) — or, with ``--from``, one
of the packaged example projects — into ``<root>/.rayspec/``, writes the packaged coding-agent
skill (:mod:`rayspec.skill`) to ``<root>/.claude/skills/rayspec/`` unless ``--no-skill``, and
prints next steps. No loader/engine logic lives here; the templates and examples are validated by
tests.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from enum import StrEnum
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Annotated, Any, Literal

import typer
import yaml
from rich.markup import escape

from rayspec.cli.commands._loader_common import console, err_console, fail
from rayspec.cli.commands._skill_common import print_install_result
from rayspec.schema.base import suggest
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


#: The packaged example corpus, relative to the ``rayspec`` package (see ``pyproject.toml``:
#: ``[tool.hatch.build.targets.wheel.force-include]`` maps the repository's ``examples/`` here).
EXAMPLES_DIR = "examples"

#: Files of an example that belong to the repository's test harness, not to a user's project.
EXAMPLE_SKIP = frozenset({"checks.yaml"})


def examples_root() -> Traversable | None:
    """The example corpus: the packaged copy, or the repository's ``examples/`` in a checkout.

    An installed wheel carries the corpus as package data, so the packaged copy is tried first
    and is the only one a `uv tool install rayspec` user ever has. In a source checkout (an
    editable install) that directory does not exist and the repository's own ``examples/`` — four
    levels above this module, next to ``pyproject.toml`` — stands in for it. ``None`` when
    neither is present (a partial install), which callers report as an empty catalogue.
    """
    packaged = resources.files("rayspec") / EXAMPLES_DIR
    if packaged.is_dir():
        return packaged
    checkout = Path(__file__).resolve().parents[4]
    candidate = checkout / EXAMPLES_DIR
    if (checkout / "pyproject.toml").is_file() and candidate.is_dir():
        return candidate
    return None


def _walk_project(node: Traversable, prefix: str = "") -> list[tuple[str, Traversable]]:
    """``[(relative posix path, file)]`` below ``node``, sorted; ``.rayspec/`` is kept.

    Unlike :func:`_walk` this keeps dot-directories (an example *is* a ``.rayspec/`` project) and
    drops only build artefacts and the files of :data:`EXAMPLE_SKIP`.
    """
    found: list[tuple[str, Traversable]] = []
    for child in node.iterdir():
        rel = f"{prefix}{child.name}"
        if child.is_dir():
            if child.name == "__pycache__":
                continue
            found.extend(_walk_project(child, f"{rel}/"))
        elif rel not in EXAMPLE_SKIP:
            found.append((rel, child))
    return sorted(found, key=lambda item: item[0])


def example_names() -> tuple[str, ...]:
    """Sorted names of the shipped example projects (``rayspec init --from <name>``).

    An example is a directory holding a ``.rayspec/`` project; ``examples/README.md`` and any
    other loose file is not one. Empty when the corpus is missing.
    """
    root = examples_root()
    if root is None:
        return ()
    names = [
        child.name
        for child in root.iterdir()
        if child.is_dir() and (child / PROJECT_DIR).is_dir() and not child.name.startswith(".")
    ]
    return tuple(sorted(names))


def example_files(name: str) -> list[tuple[str, Traversable]]:
    """The files of example ``name`` as ``[(relative posix path, resource)]``.

    Paths are relative to the project root the example scaffolds into, so ``.rayspec/…`` files
    keep their place and ``stubs.yaml`` / ``README.md`` land beside them. Raises
    :class:`LookupError` for an unknown name — the CLI turns that into the catalogue.
    """
    root = examples_root()
    if root is None or name not in example_names():
        raise LookupError(name)
    return _walk_project(root / name)


def scaffold_example(root: Path, name: str, *, force: bool = False) -> list[ScaffoldFile]:
    """Copy example ``name`` below ``root``; existing files are kept unless ``force``.

    The same contract as :func:`scaffold` (same :class:`ScaffoldFile` actions, same errors), so
    both paths of ``rayspec init`` report identically. Raises :class:`LookupError` for an unknown
    example name.
    """
    return _place(root, example_files(name), force=force)


def example_catalogue() -> list[tuple[str, str]]:
    """``[(name, description)]`` for the catalogue an unknown ``--from`` prints.

    The description is the first workflow's ``description:`` — read with a plain YAML load, not
    the loader, because the catalogue must render even for an example that deliberately fails to
    validate (``unsupported_demo``).
    """
    return [(name, _example_description(name)) for name in example_names()]


def _example_description(name: str) -> str:
    for rel, node in example_files(name):
        if not rel.startswith(f"{PROJECT_DIR}/workflows/"):
            continue
        data = _load_yaml(node)
        description = data.get("description") if isinstance(data, dict) else None
        if isinstance(description, str) and description.strip():
            return description.strip()
    return ""


def _load_yaml(node: Traversable) -> Any:
    try:
        return yaml.safe_load(node.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None


def example_dry_run(name: str) -> str | None:
    """The example's own scripted dry run as a shell command, or ``None`` when it has none.

    An example ships a ``checks.yaml`` of scenarios (workflow, inputs, stub script) that the
    repository asserts on every commit; the first scenario that scripts the agents is therefore a
    command that is *known* to be green, which is exactly what a fresh project needs as its first
    step. Scenarios with non-scalar inputs are skipped — they cannot be written as ``-i k=v``.
    """
    root = examples_root()
    if root is None:
        return None
    data = _load_yaml(root / name / "checks.yaml")
    checks = data.get("checks") if isinstance(data, dict) else None
    if not isinstance(checks, list):
        return None
    for case in checks:
        command = _dry_run_command(case)
        if command is not None:
            return command
    return None


def _dry_run_command(case: Any) -> str | None:
    if not isinstance(case, dict) or "validate" in case or case.get("run") is False:
        return None
    workflow, stubs = case.get("workflow"), case.get("stubs")
    if not isinstance(workflow, str) or not isinstance(stubs, str):
        return None
    parts = ["rayspec", "run", workflow]
    for key, raw in (case.get("inputs") or {}).items():
        if isinstance(raw, bool):
            value = "true" if raw else "false"
        elif isinstance(raw, str | int | float):
            value = str(raw)
        else:
            return None  # a list/mapping input needs --inputs-file, not -i
        parts += ["-i", shlex.quote(f"{key}={value}")]
    if case.get("allow_unsupported") is True:
        parts.append("--allow-unsupported")
    parts += ["--dry-run", "--stubs", shlex.quote(stubs)]
    return " ".join(parts)


def example_next_steps(name: str, *, skill: bool = True) -> list[str]:
    """The commands to try after ``rayspec init --from <name>``."""
    lines = ["rayspec validate                        # schema, graph, references, capabilities"]
    dry_run = example_dry_run(name)
    if dry_run is not None:
        lines.append(f"{dry_run}   # scripted agents, no login needed")
    lines.append(
        "open README.md                          # what this example shows, and a real run"
    )
    if skill:
        lines.append(
            "open a fresh Claude Code session here   # the rayspec skill in "
            ".claude/skills/rayspec/ loads automatically (rayspec skill show)"
        )
    return lines


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
    return _place(root, template_files(kind), force=force)


def _place(root: Path, files: list[tuple[str, Traversable]], *, force: bool) -> list[ScaffoldFile]:
    """Copy ``files`` below ``root`` (byte for byte) and report what happened to each.

    The one writer behind :func:`scaffold` and :func:`scaffold_example`, so a template scaffold
    and an example scaffold report and fail identically. The standard
    ``.rayspec/{workflows,agents,prompts,stubs}`` directories are always created.
    """
    if root.exists() and not root.is_dir():
        raise NotADirectoryError(f"{root} is not a directory")
    for sub in ALWAYS_DIRS:
        (root / PROJECT_DIR / sub).mkdir(parents=True, exist_ok=True)
    results: list[ScaffoldFile] = []
    for rel, node in files:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_dir():
            raise IsADirectoryError(f"{target} is a directory, expected a file (or nothing)")
        existed = target.exists()
        if existed and not force:
            results.append(ScaffoldFile(rel, target, "skipped"))
            continue
        target.write_bytes(node.read_bytes())
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


def unknown_example_hint(name: str) -> str:
    """The ``hint:`` line an unknown ``--from`` prints: the catalogue plus a did-you-mean."""
    rows = example_catalogue()
    if not rows:
        return "no examples are packaged with this build"
    width = max(len(row[0]) for row in rows)
    listing = "\n".join(f"  {row[0]:<{width}}  {_ellipsis(row[1])}" for row in rows)
    return f"available examples (rayspec init --from <name>):\n{listing}"


def _ellipsis(text: str, limit: int = 72) -> str:
    """``text`` shortened to ``limit`` characters on a word boundary (the catalogue is a hint,
    not the README)."""
    if len(text) <= limit:
        return text
    head = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:—-")
    return f"{head} …"


def register(app: typer.Typer) -> None:
    @app.command()
    def init(
        kind: Annotated[
            TemplateKind | None,
            typer.Option(
                "--kind",
                help="Scaffold flavour: `code` (review a checkout; the default) or `content` "
                "(draft + review text, `isolation: none`, no shell steps).",
                show_default=False,
            ),
        ] = None,
        from_: Annotated[
            str | None,
            typer.Option(
                "--from",
                metavar="EXAMPLE",
                help="Scaffold one of the packaged example projects instead of the generic "
                "template; an unknown name lists them.",
                show_default=False,
            ),
        ] = None,
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
        """Scaffold `.rayspec/` (example workflow, reviewer agent, prompts, config, stubs) — or a
        whole packaged example with --from — and the rayspec skill for coding agents."""
        if from_ is not None and kind is not None:
            fail(
                "--from and --kind are mutually exclusive: an example ships its own workflow",
                hint=f"rayspec init --from {from_}",
            )
        if from_ is not None and from_ not in example_names():
            hint = unknown_example_hint(from_)
            match = suggest(from_, list(example_names()))
            message = f"unknown example {from_!r}"
            if match is not None:
                message += f"; did you mean {match!r}?"
            fail(message, hint=hint)
        target = (root or Path.cwd()).resolve()
        out = console()
        err = err_console()
        label = from_ if from_ is not None else (kind or TemplateKind.code).value
        previous = None if from_ is not None else detect_kind(target)
        try:
            if from_ is not None:
                results = scaffold_example(target, from_, force=force)
            else:
                results = scaffold(target, kind=label, force=force)
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
        where = target if from_ is not None else target / PROJECT_DIR
        out.print(f"[bold]{label}[/bold] scaffold in {where}: {summary}")
        if skill_results:
            print_install_result(skill_results, project_skill_dir(target), label="project")
            created += sum(1 for r in skill_results if r.action != "skipped")
            skipped += sum(1 for r in skill_results if r.action == "skipped")
        if skipped and not created:
            err.print(
                f"[yellow]warning:[/yellow] nothing written — all {skipped} file(s) exist; "
                "use --force to overwrite them"
            )
        if previous is not None and previous != label:
            orphans = ", ".join(orphan_files(previous, label)) or "none"
            if force:
                err.print(
                    f"[yellow]warning:[/yellow] the existing `{previous}` scaffold was replaced "
                    f"by `{label}`; files only the `{previous}` kind ships are left over "
                    f"(delete them if unused): {orphans}"
                )
            else:
                err.print(
                    f"[yellow]warning:[/yellow] `{target / PROJECT_DIR}` holds a `{previous}` "
                    f"scaffold; --kind {label} added only its extra files, so the "
                    f"project is now a mixed `{previous}`/`{label}` scaffold — use "
                    f"--force to switch (`{previous}`-only files that stay: {orphans})"
                )
        warning = None if from_ is not None else non_git_warning(target, label)
        if warning is not None:
            err.print(f"[yellow]warning:[/yellow] {escape(warning)}", highlight=False)
        out.print("\nnext steps:")
        steps = (
            example_next_steps(from_, skill=not no_skill)
            if from_ is not None
            else next_steps(label, skill=not no_skill)
        )
        for line in steps:
            out.print(f"  {escape(line)}")
