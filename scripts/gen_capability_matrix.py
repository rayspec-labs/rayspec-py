#!/usr/bin/env python3
"""Generate the provider capability matrix in ``docs/providers.md`` from the registry.

The table between ``<!-- capability-matrix:start -->`` and ``<!-- capability-matrix:end -->``
is the registry's declared :class:`~rayspec.providers.base.ProviderCapabilities` of every
builtin provider (``rayspec providers`` prints the same data). Run it after changing a
capability table::

    uv run python scripts/gen_capability_matrix.py          # rewrite docs/providers.md
    uv run python scripts/gen_capability_matrix.py --check  # exit 1 when the doc is stale

``tests/docs/test_capability_matrix.py`` runs the check in the test suite.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rayspec.cli.commands.providers import format_capability
from rayspec.providers.base import ProviderCapabilities
from rayspec.providers.registry import BUILTIN_REGISTRATIONS

REPO_ROOT = Path(__file__).resolve().parents[1]
DOC_PATH = REPO_ROOT / "docs" / "providers.md"
START_MARKER = "<!-- capability-matrix:start -->"
END_MARKER = "<!-- capability-matrix:end -->"

#: One-line meaning of every capability (documentation column of the generated table).
CAPABILITY_NOTES: dict[str, str] = {
    "structured_output": "`output_schema` on prompt steps (`enforced`: the SDK returns JSON; "
    "`best_effort`: the engine asks for JSON and extracts it; `none`: unsupported)",
    "session_resume": "`session: <step>` continues an earlier step's session",
    "session_fork": "forking a session (reserved for the engine; no YAML field yet)",
    "instructions_modes": "`instructions_mode: append` / `replace`",
    "access_levels": "`access:` levels the provider can enforce",
    "tool_groups": "neutral groups accepted in `tools.allow` / `tools.deny`",
    "raw_tool_names": "provider-native names as `<provider>:<Name>` in `tools`",
    "max_turns": "`max_turns` on the agent",
    "budget_usd": "`budget_usd` on the agent",
    "cost_reporting": "the provider reports USD cost itself (else the pricing table is used)",
    "effort_levels": "`effort:` values accepted",
    "effort_aliases": "effort values rewritten with a warning",
    "thinking": "`thinking: true` / `false` on the agent",
    "mcp_servers": "`mcp:` servers on the agent",
    "env_injection": "`env:` on prompt steps",
    "images": "image inputs (not used by any YAML field in v1)",
    "extra": "provider-specific extras (none declared)",
}


def capability_rows() -> tuple[str, ...]:
    """Row order: the ``ProviderCapabilities`` fields in declaration order (frozen contract).

    ``rayspec providers`` renders the same rows in the same order; any new capability field
    shows up here automatically (and ``tests/docs`` insists on a note for it).
    """
    return tuple(ProviderCapabilities.__dataclass_fields__)


def _cell(caps: ProviderCapabilities, attr: str) -> str:
    """One table cell: the CLI's rendering, with set members comma-separated."""
    text = format_capability(caps, attr)
    if isinstance(getattr(caps, attr), frozenset | set):
        return ", ".join(text.split())
    return text


def render_matrix() -> str:
    """The markdown table: one row per capability, one column per builtin provider."""
    regs = list(BUILTIN_REGISTRATIONS)
    header = "| capability | " + " | ".join(f"`{r.id}`" for r in regs) + " | meaning |"
    sep = "|---|" + "---|" * len(regs) + "---|"
    lines = [header, sep]
    for attr in capability_rows():
        cells = [_cell(r.capabilities, attr) for r in regs]
        note = CAPABILITY_NOTES.get(attr, "")
        lines.append(f"| `{attr}` | " + " | ".join(cells) + f" | {note} |")
    return "\n".join(lines)


def replace_block(text: str, table: str) -> str:
    """Return ``text`` with the content between the markers replaced by ``table``."""
    start = text.find(START_MARKER)
    end = text.find(END_MARKER)
    if start < 0 or end < 0 or end < start:
        raise ValueError(f"{START_MARKER} / {END_MARKER} markers not found (in that order)")
    head = text[: start + len(START_MARKER)]
    tail = text[end:]
    return f"{head}\n{table}\n{tail}"


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns the process exit code."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="do not write; exit 1 when stale")
    parser.add_argument("--doc", type=Path, default=DOC_PATH, help="markdown file to update")
    args = parser.parse_args(argv)
    doc: Path = args.doc
    current = doc.read_text(encoding="utf-8")
    updated = replace_block(current, render_matrix())
    if updated == current:
        shown = doc.relative_to(REPO_ROOT) if doc.is_relative_to(REPO_ROOT) else doc
        print(f"{shown}: up to date")
        return 0
    if args.check:
        print(
            f"{doc}: capability matrix is stale; run "
            "`uv run python scripts/gen_capability_matrix.py`",
            file=sys.stderr,
        )
        return 1
    doc.write_text(updated, encoding="utf-8")
    print(f"{doc}: capability matrix updated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
