#!/usr/bin/env python
# SPDX-License-Identifier: Apache-2.0
"""Generate the checked-in JSON Schemas under ``schemas/`` from the Pydantic models.

    uv run python scripts/gen_schemas.py            # rewrite schemas/*.schema.json
    uv run python scripts/gen_schemas.py --check    # exit 1 when a file is stale (the gate)
    uv run python scripts/gen_schemas.py --out DIR  # write somewhere else

Every schema is built by :mod:`rayspec.schemagen`; this script only decides where the bytes go.
``tests/schema/test_gen_schemas.py`` runs ``--check`` so a model change that is not mirrored
into ``schemas/`` fails the normal test run, not just CI.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from rayspec.schemagen import (  # noqa: E402
    MODELINE_PREFIX,
    SCHEMA_KINDS,
    modeline,
    schema_filename,
    schema_text,
)

#: Default output directory (the checked-in copies).
SCHEMAS_DIR = REPO_ROOT / "schemas"

#: Workflow documents that ship with rayspec and must open with the editor modeline: the `init`
#: scaffolds, the packaged examples and this repo's own dogfood workflows. Keeping them in sync
#: here is what makes SCHEMA_BASE_URL a one-line edit.
MODELINE_GLOBS = (
    "src/rayspec/cli/templates/*/workflows/*.yaml",
    "examples/*/.rayspec/workflows/*.yaml",
    ".rayspec/workflows/*.yaml",
)


def modeline_files() -> list[Path]:
    """Every packaged workflow document that carries the modeline."""
    out: list[Path] = []
    for pattern in MODELINE_GLOBS:
        out.extend(sorted(REPO_ROOT.glob(pattern)))
    return out


def with_modeline(text: str) -> str:
    """``text`` with the current modeline as its first line (an outdated one is replaced)."""
    lines = text.splitlines(keepends=True)
    if lines and lines[0].startswith(MODELINE_PREFIX):
        lines = lines[1:]
    return modeline() + "\n" + "".join(lines)


def stale_modelines(paths: list[Path]) -> list[Path]:
    """Documents whose first line is not the current modeline."""
    out: list[Path] = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        if text != with_modeline(text):
            out.append(path)
    return out


def sync_modelines(paths: list[Path]) -> list[Path]:
    """Write the current modeline into every document that lacks it; returns the changed paths."""
    changed: list[Path] = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        fixed = with_modeline(text)
        if fixed != text:
            path.write_text(fixed, encoding="utf-8")
            changed.append(path)
    return changed


def expected(out_dir: Path) -> dict[Path, str]:
    """``{path: content}`` for every published schema."""
    return {out_dir / schema_filename(kind): schema_text(kind) for kind in SCHEMA_KINDS}


def stale(out_dir: Path) -> list[Path]:
    """Files that are missing or differ from what the models say they should be."""
    out: list[Path] = []
    for path, content in expected(out_dir).items():
        try:
            current = path.read_text(encoding="utf-8")
        except OSError:
            out.append(path)
            continue
        if current != content:
            out.append(path)
    return out


def write(out_dir: Path) -> list[Path]:
    """Write every schema; returns the paths whose content changed."""
    out_dir.mkdir(parents=True, exist_ok=True)
    changed: list[Path] = []
    for path, content in expected(out_dir).items():
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            path.write_text(content, encoding="utf-8")
            changed.append(path)
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--check", action="store_true", help="Only report stale files (exit 1).")
    parser.add_argument("--out", type=Path, default=SCHEMAS_DIR, help="Output directory.")
    args = parser.parse_args(argv)
    out_dir: Path = args.out
    # resolved: `--out schemas` / `--out ./schemas/` must sync the modelines like the default does
    default_out = out_dir.resolve() == SCHEMAS_DIR.resolve()
    documents = modeline_files() if default_out else []
    if args.check:
        outdated = stale(out_dir) + stale_modelines(documents)
        for path in outdated:
            print(f"stale: {path}")
        if outdated:
            print("run: uv run python scripts/gen_schemas.py")
            return 1
        print(f"{len(SCHEMA_KINDS)} schema(s) and {len(documents)} modeline(s) up to date")
        return 0
    changed = write(out_dir) + sync_modelines(documents)
    for path in changed:
        print(f"wrote: {path}")
    if not changed:
        print(f"{len(SCHEMA_KINDS)} schema(s) already up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
