# SPDX-License-Identifier: Apache-2.0
"""The workflow library bundled with rayspec: where it lives, how its files are labelled, how one
is ejected into a project.

A leaf module — no rayspec imports — because discovery, the loader and the CLI all need it and
none of them may pull the others in for it.

The library is ``rayspec/workflows/defaults/<name>.yaml`` inside the installed package. A name
resolves project → user → bundled, so a bundled workflow is what runs when the project has no
file of that name, and a project (or user) file with the same stem shadows it.

A bundled file is labelled ``<bundled>/<name>.yaml`` rather than by its absolute path: the label
is mixed into the workflow hash, keyed in ``.rayspec/trusted.yaml`` and recorded in ``run.json``,
and none of those may depend on where ``site-packages`` happens to be on one machine.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Final

#: The :data:`rayspec.loader.discovery.Scope` of a bundled workflow.
BUNDLED_SCOPE: Final = "bundled"
#: What a bundled file's label starts with (``<bundled>/pr_review.yaml``).
BUNDLED_LABEL_PREFIX: Final = "<bundled>/"
#: The editor modeline the bundled files open with; an eject header goes after it, not before.
MODELINE_PREFIX: Final = "# yaml-language-server:"
#: The machine-readable line an ejected copy carries (one line, so a regex reads it back).
EJECT_HEADER_RE: Final = re.compile(
    r"^# rayspec-eject: version=(?P<version>\S+) workflow=(?P<workflow>\S+) "
    r"sha256=(?P<sha256>[0-9a-f]{64})$",
    re.MULTILINE,
)


@dataclass(frozen=True, slots=True)
class EjectHeader:
    """What ``rayspec workflows eject`` wrote at the top of a copy: the rayspec version it came
    from and the sha256 of the bundled bytes at the time (``rayspec workflows`` compares it with
    the bundled file it now ships to report drift)."""

    version: str
    workflow: str
    sha256: str


def bundled_dir() -> Path:
    """``rayspec/workflows/defaults/`` of the installed package."""
    return Path(str(resources.files("rayspec") / "workflows" / "defaults"))


def is_bundled(path: Path) -> bool:
    """Whether ``path`` lies in the bundled library (as given, or once resolved)."""
    root = bundled_dir()
    if path.is_relative_to(root):
        return True
    try:
        return path.resolve().is_relative_to(root.resolve())
    except OSError:
        return False


def bundled_label(path: Path) -> str | None:
    """``<bundled>/<name>.yaml`` for a file of the library, ``None`` for any other path."""
    return f"{BUNDLED_LABEL_PREFIX}{path.name}" if is_bundled(path) else None


def bundled_digest(path: Path) -> str:
    """The sha256 hex digest of ``path``'s bytes — what an eject header records."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def render_ejected(name: str, text: str, *, version: str, digest: str) -> str:
    """``text`` (a bundled document) with the eject header inserted.

    The header goes after a first-line editor modeline when there is one, so the schema hint
    keeps working, and at the very top otherwise.
    """
    header = (
        f"# rayspec-eject: version={version} workflow={name} sha256={digest}\n"
        f"# Ejected from the workflow library bundled with rayspec {version}; this copy shadows "
        f"the bundled `{name}` — edit it freely (`rayspec workflows` reports drift).\n"
    )
    first, sep, rest = text.partition("\n")
    if first.startswith(MODELINE_PREFIX):
        return first + sep + header + rest
    return header + text


def parse_eject_header(text: str) -> EjectHeader | None:
    """The eject header of a document, or ``None`` when it carries none."""
    match = EJECT_HEADER_RE.search(text)
    if match is None:
        return None
    return EjectHeader(match["version"], match["workflow"], match["sha256"])


__all__ = [
    "BUNDLED_LABEL_PREFIX",
    "BUNDLED_SCOPE",
    "EJECT_HEADER_RE",
    "MODELINE_PREFIX",
    "EjectHeader",
    "bundled_digest",
    "bundled_dir",
    "bundled_label",
    "is_bundled",
    "parse_eject_header",
    "render_ejected",
]
