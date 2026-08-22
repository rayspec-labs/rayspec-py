# SPDX-License-Identifier: Apache-2.0
"""A cassette is a fixture, not a transcript of somebody's laptop.

Boundary: text and structure assertions over the committed cassette files — no replay, no SDK.
They are the reason a recorded transcript can be committed at all: the file may describe the
*shape* of a turn and nothing about the machine that produced it. The scan is proved on a
poisoned string, because a hygiene check that cannot fail is decoration.
"""

from __future__ import annotations

import getpass
import json
import re
import socket
from pathlib import Path, PureWindowsPath

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


#: The contexts in which a bare account or host name identifies a machine. A bare name on its own
#: is not enough: ``test``, ``codex``, ``claude`` and ``report`` are all plausible login names and
#: all of them occur in these fixtures, so a substring match would red-flag a clean cassette on a
#: perfectly ordinary CI image — and a hygiene check that goes red for no reason gets disabled.
NAME_CONTEXTS: tuple[str, ...] = (
    "/home/{n}",
    "/Users/{n}",
    "\\Users\\{n}",
    "~{n}",
    "{n}@",
    "@{n}",
    "//{n}",
)


def unescaped(text: str) -> str:
    """The file's text with JSON's doubled backslashes collapsed to one.

    ``C:\\Users\\alice`` is how a Windows path is *stored* in a JSON string, so comparing the
    raw file text against ``str(Path.home())`` never matches on the platform where the check
    matters most.
    """
    return text.replace("\\\\", "\\")


def machine_strings() -> list[tuple[str, str]]:
    """Whole paths of *this* machine — long enough to mean something on their own."""
    found = [(str(Path.home()), "home directory"), (str(Path.cwd()), "working directory")]
    return [(value, what) for value, what in found if len(value) > 3]


def machine_names() -> list[tuple[str, str]]:
    """This machine's account and host name — only meaningful in a :data:`NAME_CONTEXTS` shape."""
    found = [(getpass.getuser(), "user name"), (socket.gethostname(), "host name")]
    return [(value, what) for value, what in found if value]


def problems(text: str) -> list[str]:
    """Every machine property, credential or path found in ``text`` (one message each)."""
    found = [
        f"line {number}: {what}: {line.strip()[:90]}"
        for number, line in enumerate(text.splitlines(), start=1)
        for pattern, what in [*SECRETS, *PATHS]
        if pattern.search(line)
    ]
    plain = unescaped(text)
    found += [f"{what} in the file: {value}" for value, what in machine_strings() if value in plain]
    found += [
        f"{what} in the file: {context}"
        for value, what in machine_names()
        for context in (form.format(n=value) for form in NAME_CONTEXTS)
        if context in plain
    ]
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
    account = f"{getpass.getuser()}@buildbox"
    assert any("user name" in problem for problem in problems(account)), account
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


@pytest.mark.parametrize("name", ["test", "codex", "claude", "report", "runner"])
def test_a_login_name_that_reads_like_fixture_vocabulary_is_not_a_finding(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``codex`` and ``claude`` are plausible container users — and words every cassette holds.

    A hygiene check that goes red on a normal CI image gets disabled, and this is the one class
    nobody can afford to have disabled. A bare name is therefore only a finding in a context that
    identifies a machine.
    """
    monkeypatch.setattr(getpass, "getuser", lambda: name)
    monkeypatch.setattr(socket, "gethostname", lambda: name)
    for tape in CASSETTES:
        text = tape.path.read_text(encoding="utf-8")
        assert not problems(text), f"{tape.id}\n" + "\n".join(problems(text))
    assert any("user name" in problem for problem in problems(f'"dir": "/home/{name}/notes"'))


def test_a_home_directory_json_escaped_into_the_file_is_still_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Windows path is doubled by JSON, which is exactly where this check matters most."""
    monkeypatch.setattr(Path, "home", lambda: PureWindowsPath(r"C:\Users\alice"))
    on_disk = json.dumps({"cwd": r"C:\Users\alice\project"})
    assert r"C:\Users\alice" not in on_disk, "the point of the test is that it is escaped"
    assert any("home directory" in problem for problem in problems(on_disk))
