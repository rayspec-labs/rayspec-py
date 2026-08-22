#!/usr/bin/env python
# SPDX-License-Identifier: Apache-2.0
"""Print the CHANGELOG section of one version as release notes.

    uv run python scripts/release_notes.py v1.0.0                 # to stdout
    uv run python scripts/release_notes.py 1.0.0 -o notes.md      # to a file

Boundary: text in, text out — no git, no network, no GitHub API. ``.github/workflows/release.yml``
runs it so the notes of a tag are the changelog entry that was reviewed, never a hand-copied
paste. A version with no section (or an empty one) exits 2 and names it: the release stops
*before* anything is published rather than announcing a build nobody described.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

#: ``## [1.2.3] — 2026-08-20`` / ``## [Unreleased]`` — Keep a Changelog's version heading.
_HEADING_RE = re.compile(r"^## \[(?P<version>[^\]]+)\]", re.MULTILINE)

#: A link-reference definition (``[1.2.3]: https://…``). Keep a Changelog collects one per
#: version in a block at the foot of the *file*, which is what :func:`_strip_foot_link_refs`
#: removes — a definition sitting next to the prose that reads it is part of that note and stays.
_LINK_REF_RE = re.compile(r"^\[[^\]]+\]:\s+\S+\s*$")


class NoSuchVersion(LookupError):
    """The changelog has no non-empty section for the requested version."""


def normalise(version: str) -> str:
    """``v1.2.3`` and ``1.2.3`` name the same section — tags carry the ``v``, headings do not."""
    return version[1:] if version.startswith(("v", "V")) else version


def _strip_foot_link_refs(changelog: str) -> str:
    """Drop the block of link-reference definitions at the foot of *changelog*.

    It belongs to the file rather than to the last version, so it is removed once, here, instead
    of anywhere a definition happens to appear.
    """
    lines = changelog.rstrip().splitlines()
    while lines and (not lines[-1].strip() or _LINK_REF_RE.match(lines[-1])):
        lines.pop()
    return "\n".join(lines) + "\n"


def sections(changelog: str) -> dict[str, str]:
    """Map every ``## [version]`` heading of *changelog* to its body (headings excluded)."""
    found: dict[str, str] = {}
    changelog = _strip_foot_link_refs(changelog)
    matches = list(_HEADING_RE.finditer(changelog))
    for index, match in enumerate(matches):
        newline = changelog.find("\n", match.end())
        start = len(changelog) if newline == -1 else newline + 1
        end = matches[index + 1].start() if index + 1 < len(matches) else len(changelog)
        found[match.group("version")] = changelog[start:end].strip()
    return found


def notes_for(version: str, changelog: str) -> str:
    """The release notes of *version*, or raise :class:`NoSuchVersion`.

    ``Unreleased`` is not a version: a tag that points at it would publish notes that say the
    change has not shipped yet.
    """
    wanted = normalise(version)
    if wanted.lower() == "unreleased":
        raise NoSuchVersion(
            "`Unreleased` is not a version — roll its entries into a `## [x.y.z]` heading in "
            "CHANGELOG.md and tag that"
        )
    body = sections(changelog).get(wanted, "")
    if not body:
        raise NoSuchVersion(
            f"CHANGELOG.md has no notes for {wanted} — add a `## [{wanted}]` section "
            f"(the `## [Unreleased]` entries are the ones waiting to be rolled into it)"
        )
    return body


def main(argv: list[str] | None = None) -> int:
    """Write the notes of ``version`` to stdout or ``--output``; 2 when there are none."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").partition("\n")[0])
    parser.add_argument("version", help="the released version, with or without the tag's `v`")
    parser.add_argument(
        "--changelog", type=Path, default=Path("CHANGELOG.md"), help="path to CHANGELOG.md"
    )
    parser.add_argument("-o", "--output", type=Path, default=None, help="write here, not stdout")
    args = parser.parse_args(argv)

    try:
        changelog = args.changelog.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"error: cannot read {args.changelog}: {exc}", file=sys.stderr)
        return 2
    try:
        notes = notes_for(args.version, changelog)
    except NoSuchVersion as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.output is None:
        print(notes)
    else:
        args.output.write_text(notes + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
