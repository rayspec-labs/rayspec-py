# SPDX-License-Identifier: Apache-2.0
"""A cassette is a fixture, not a transcript of somebody's laptop.

Boundary: text and structure assertions over the committed cassette files — no replay, no SDK.
They are the reason a recorded transcript can be committed at all: the file may describe the
*shape* of a turn and nothing about the machine that produced it. The scan is proved on a
poisoned string, because a hygiene check that cannot fail is decoration.
"""

from __future__ import annotations

import getpass
import re
import socket
from pathlib import Path

import pytest

from ._cassette import CAPTURES, Cassette, all_cassettes

CASSETTES = list(all_cassettes())

#: Credential, account and request identifiers. A transcript is recorded against a real service,
#: so these are the shapes that can travel with it.
SECRETS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bsk-[A-Za-z0-9_-]{6,}"), "api key"),
    (re.compile(r"\b(?:ghp|gho|ghs|ghr)_[A-Za-z0-9]{10,}"), "github token"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{10,}"), "github token"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "aws key id"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\."), "jwt"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private key"),
    (re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{8,}"), "bearer token"),
    (re.compile(r"\breq_[0-9a-f]{16,}\b"), "request id"),
    (re.compile(r"\bcf-ray\b", re.IGNORECASE), "trace id"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[A-Za-z]{2,}"), "email address"),
]

#: A filesystem path of any kind: cassettes name directories with ``<workspace>``, never with a
#: real path, so "looks like a path at all" is the rule — no allow-list to keep in sync.
PATHS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?<![\w:/])(?:/[A-Za-z0-9_.~-]+){2,}"), "absolute path"),
    (re.compile(r"\b[A-Za-z]:\\\\"), "windows path"),
    (re.compile(r"(?<![\w/])~/"), "home-relative path"),
]


def machine_strings() -> list[tuple[str, str]]:
    """Strings that identify *this* machine — the sharpest possible proof of a scrubbed file."""
    found = [
        (str(Path.home()), "home directory"),
        (str(Path.cwd()), "working directory"),
        (socket.gethostname(), "host name"),
        (getpass.getuser(), "user name"),
    ]
    return [(value, what) for value, what in found if len(value) > 3]


def problems(text: str) -> list[str]:
    """Every machine property, credential or path found in ``text`` (one message each)."""
    found = [
        f"line {number}: {what}: {line.strip()[:90]}"
        for number, line in enumerate(text.splitlines(), start=1)
        for pattern, what in [*SECRETS, *PATHS]
        if pattern.search(line)
    ]
    found += [f"{what} in the file: {value}" for value, what in machine_strings() if value in text]
    return found


def test_the_scan_catches_what_it_is_for() -> None:
    """Poisoned lines must be flagged — otherwise the assertions below prove nothing."""
    poisoned = "\n".join(
        [
            '"cwd": "/Users/someone/code/app"',
            '"key": "sk-ant-api03-notarealkeybutlooksliketone"',
            '"details": "cf-ray: abc123-FRA, request id: req_0123456789abcdef01"',
            '"author": "someone@example.org"',
            '"file": "~/.rayspec/config.yaml"',
        ]
    )
    assert len(problems(poisoned)) >= 5, problems(poisoned)
    assert problems(str(Path.home())), "the machine's own home directory must be caught"
    assert not problems('"cwd": "<workspace>", "url": "https://api.openai.com/v1/responses"')


def test_there_are_cassettes_for_both_shipped_providers() -> None:
    """A cassette directory that quietly emptied would turn every replay test into a no-op."""
    providers = {tape.provider for tape in CASSETTES}
    assert providers == {"claude", "codex"}, providers


@pytest.mark.parametrize("tape", CASSETTES, ids=str)
def test_cassette_carries_no_machine_property(tape: Cassette) -> None:
    text = tape.path.read_text(encoding="utf-8")
    assert not problems(text), f"{tape.id}\n" + "\n".join(problems(text))


@pytest.mark.parametrize("tape", CASSETTES, ids=str)
def test_cassette_says_what_it_is_and_where_it_came_from(tape: Cassette) -> None:
    """Provenance is part of the fixture: a transcript nobody can place cannot be refreshed."""
    assert tape.data["cassette"] == 1
    assert tape.provider == tape.path.parent.name
    assert str(tape.data["about"]).strip()
    source = dict(tape.data["source"])
    assert source["capture"] in CAPTURES, source
    assert str(source["from"]).strip()
    assert list(source["scrubbed"])
    assert tape.transcript, "an empty transcript replays into nothing"
    assert set(tape.expect) == {"events", "result"}
    assert tape.expect["result"], "a cassette pins the result, not only the events"
