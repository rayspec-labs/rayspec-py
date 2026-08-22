# SPDX-License-Identifier: Apache-2.0
"""Recorded output that is published carries placeholders, never a real machine's values.

Boundary: a text scan of ``docs/`` and ``tests/golden/`` — no CLI calls, no network.

``run.json`` records ``socket.gethostname()``, so anything pasted out of a real run names the
machine it was made on. Every golden fixture and the capture helper that writes them
(``tests/golden/_capture.py``) already normalise it to ``<host>``, and the documented JSON block
normalises its own secret to ``<secret>`` — a host left as it came off somebody's laptop is the
one field that got away, and it is the kind of thing a reader is invited to compare their own
output against.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

#: ``"host": "…"`` — in a JSON document and in a Python dict written the same way.
_HOST_RE = re.compile(r'"host":\s*"([^"]*)"')

#: What a published host has to be, the value ``tests/golden/_capture.py`` writes.
PLACEHOLDER = "<host>"

#: Everything a stranger reads: the documentation, and the fixtures the docs point at.
SEARCHED = ("docs", "tests/golden")


def _text_files() -> list[Path]:
    found: list[Path] = []
    for where in SEARCHED:
        found += [path for path in sorted((REPO_ROOT / where).rglob("*")) if path.is_file()]
    return found


def test_every_recorded_host_is_the_placeholder() -> None:
    offenders: list[str] = []
    seen = 0
    for path in _text_files():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:  # pragma: no cover - no binary fixture today
            continue
        for number, line in enumerate(lines, start=1):
            for value in _HOST_RE.findall(line):
                seen += 1
                if value != PLACEHOLDER:
                    where = path.relative_to(REPO_ROOT)
                    offenders.append(f'{where}:{number}: "host": "{value}"')
    assert seen, f"nothing under {'/, '.join(SEARCHED)}/ records a host — the scan matched nothing"
    assert not offenders, "a real machine name is published as recorded output:\n" + "\n".join(
        offenders
    )
