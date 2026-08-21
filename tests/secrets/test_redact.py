"""The Redactor: exact-match replacement at every writer, chunk boundaries,
opt-in builtin detectors."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import BaseModel

from rayspec.redact import (
    MIN_REDACTABLE_LEN,
    NULL_REDACTOR,
    REDACTION,
    Redactor,
    StreamRedactor,
    detector_patterns,
)

SECRET = "ghp_SECRETTOKEN_ABCDEF"


def _r(**secrets: str) -> Redactor:
    return Redactor.build(secrets)


def test_empty_redactor_is_falsey_and_a_no_op() -> None:
    assert not NULL_REDACTOR
    assert NULL_REDACTOR.redact("anything") == "anything"
    assert not Redactor.build({})


def test_exact_match_is_replaced_by_a_named_marker() -> None:
    red = _r(token=SECRET)
    assert red.redact(f"got {SECRET} ok") == "got [REDACTED:token] ok"
    assert REDACTION.format(name="token") == "[REDACTED:token]"


def test_every_occurrence_is_replaced() -> None:
    red = _r(token=SECRET)
    assert red.redact(f"{SECRET}/{SECRET}") == "[REDACTED:token]/[REDACTED:token]"


def test_longer_secrets_are_replaced_first() -> None:
    red = _r(short="abcdef", long="abcdefghij")
    assert red.redact("abcdefghij") == "[REDACTED:long]"


def test_json_escaped_form_is_redacted_too() -> None:
    """``run.json`` is redacted as serialised text: the escaped form must match as well."""
    value = 'a"b\\c\nd-secret'
    red = _r(token=value)
    payload = json.dumps({"v": value})
    assert value not in red.redact(payload)
    assert "[REDACTED:token]" in red.redact(payload)


def test_very_short_values_are_not_redacted() -> None:
    """A 1-3 character value would corrupt every log; it is skipped and reported."""
    red = Redactor.build({"tiny": "ab"})
    assert not red
    assert red.skipped == ("tiny",)
    assert MIN_REDACTABLE_LEN == 4


def test_redact_obj_walks_containers() -> None:
    red = _r(token=SECRET)
    out = red.redact_obj({"a": [SECRET, {"b": SECRET}], "n": 1, "k": None})
    assert out == {"a": ["[REDACTED:token]", {"b": "[REDACTED:token]"}], "n": 1, "k": None}


def test_redact_obj_redacts_mapping_keys_too() -> None:
    """A provider that returns ``{"<token>": …}`` puts the value in the KEY position; a store
    that only walks values writes it out raw."""
    red = _r(token=SECRET)
    out = red.redact_obj({SECRET: "v", "outer": {f"k-{SECRET}": [SECRET]}, 3: SECRET})
    assert out == {
        "[REDACTED:token]": "v",
        "outer": {"k-[REDACTED:token]": ["[REDACTED:token]"]},
        3: "[REDACTED:token]",
    }


def test_non_string_values_are_stringified_for_matching() -> None:
    red = Redactor.build({"num": 1234567})
    assert red.redact("id=1234567") == "id=[REDACTED:num]"


# -- chunk boundaries -----------------------------------------------------------------------


def test_stream_redactor_catches_a_secret_split_across_two_chunks() -> None:
    red = _r(token=SECRET)
    stream = StreamRedactor(red)
    first = stream.feed(f"before {SECRET[:8]}")
    second = stream.feed(f"{SECRET[8:]} after")
    tail = stream.flush()
    joined = first + second + tail
    assert SECRET not in joined
    assert joined == "before [REDACTED:token] after"


def test_stream_redactor_splits_at_every_offset() -> None:
    red = _r(token=SECRET)
    text = f"xx{SECRET}yy"
    for cut in range(len(text) + 1):
        stream = StreamRedactor(red)
        out = stream.feed(text[:cut]) + stream.feed(text[cut:]) + stream.flush()
        assert out == "xx[REDACTED:token]yy", cut


@pytest.mark.parametrize("secret", ["4242424242", "abcabcabc", "aaaa", "xyxyxy"])
def test_a_self_overlapping_secret_is_never_cut_in_half(secret: str) -> None:
    """A value whose own prefix is also its suffix (a repeating token, a numeric PIN) used to
    fool the boundary buffer: the hold was measured against the longest *partial* prefix, so a
    COMPLETE match that started before the cut had its head emitted raw. The buffer now waits
    for the value it may still be in the middle of, so the marker arrives on the flush."""
    red = _r(tok=secret)
    stream = red.stream()
    assert stream.feed(secret) + stream.flush() == "[REDACTED:tok]"
    text = f"[{secret}]"
    for cut in range(len(text) + 1):
        stream = red.stream()
        out = stream.feed(text[:cut]) + stream.feed(text[cut:]) + stream.flush()
        assert out == "[[REDACTED:tok]]", (secret, cut)


def _assert_split_invariant(red: Redactor, text: str) -> None:
    """The documented invariant: chunking may move boundaries, never change the result."""
    whole = red.redact(text)
    for cut in range(len(text) + 1):
        stream = red.stream()
        out = stream.feed(text[:cut]) + stream.feed(text[cut:]) + stream.flush()
        assert out == whole, (cut, out)
    stream = red.stream()
    assert "".join(stream.feed(char) for char in text) + stream.flush() == whole


def test_a_secret_that_is_a_prefix_of_another_is_never_half_released() -> None:
    """Two secrets where one starts with the other — a database user and the DSN that embeds
    it — are ordinary configuration. Replacing the short one as soon as it is whole destroys
    the prefix the boundary needs in order to wait for the long one, and the long value's tail
    then goes out raw."""
    red = Redactor.build({"user": "dbuser", "dsn": "dbuser:pw@host"})
    stream = red.stream()
    assert stream.feed("dbuser") + stream.feed(":pw@host") + stream.flush() == "[REDACTED:dsn]"
    _assert_split_invariant(red, "connecting as dbuser:pw@host now")


def test_a_secret_ending_in_a_backslash_is_its_own_prefix_pair() -> None:
    """One secret is enough to hit the same case: a value ending in a backslash registers the
    raw and the JSON-escaped form, and the raw one is a proper PREFIX of the escaped one. A
    writer of raw text that serialises the value (a step that prints a JSON document) writes
    the escaped form, so the boundary has to wait for the second backslash."""
    red = Redactor.build({"tok": "s3cret-value\\"})
    _assert_split_invariant(red, "path=s3cret-value\\ done")
    _assert_split_invariant(red, json.dumps({"path": "s3cret-value\\"}))


def test_a_self_overlapping_secret_fed_one_character_at_a_time(secret: str = "4242424242") -> None:
    red = _r(tok=secret)
    stream = red.stream()
    out = "".join(stream.feed(char) for char in f"a{secret}b") + stream.flush()
    assert out == "a[REDACTED:tok]b"


def test_stream_redactor_preserves_text_without_secrets() -> None:
    stream = StreamRedactor(_r(token=SECRET))
    out = "".join(stream.feed(c) for c in "hello world") + stream.flush()
    assert out == "hello world"


def test_stream_redactor_of_an_empty_redactor_holds_nothing_back() -> None:
    stream = StreamRedactor(NULL_REDACTOR)
    assert stream.feed("abc") == "abc"
    assert stream.flush() == ""


# -- detectors ------------------------------------------------------------------------------


def test_detectors_are_off_by_default() -> None:
    red = Redactor.build({})
    assert red.redact("ghp_0123456789abcdefghij") == "ghp_0123456789abcdefghij"


@pytest.mark.parametrize(
    ("name", "sample"),
    [
        ("github", "ghp_0123456789abcdefghij0123"),
        ("openai", "sk-0123456789abcdefghij0123"),
        ("aws", "AKIAIOSFODNN7EXAMPLE"),
        ("jwt", "eyJhbGciOi.eyJzdWIiOiJ4.SflKxwRJSM"),
    ],
)
def test_opt_in_detectors_replace_their_shapes(name: str, sample: str) -> None:
    red = Redactor.build({}, detectors=[name])
    assert red.redact(f"x {sample} y") == f"x [REDACTED:{name}] y"


def test_pem_detector_spans_lines() -> None:
    pem = "-----BEGIN RSA PRIVATE KEY-----\nAAAA\nBBBB\n-----END RSA PRIVATE KEY-----"
    red = Redactor.build({}, detectors=["pem"])
    assert red.redact(pem) == "[REDACTED:pem]"


def test_unknown_detector_names_are_ignored_by_the_builder() -> None:
    assert detector_patterns(["nope"]) == ()


def test_a_pem_block_split_across_two_chunks_is_still_redacted() -> None:
    """The promise is 'a value split across two streamed chunks is caught too' — a real key is
    longer than one pipe read, so the boundary case IS the common case."""
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        + "".join("MIIEow" for _ in range(340))
        + "\n-----END RSA PRIVATE KEY-----"
    )
    assert len(pem) > 2000
    stream = StreamRedactor(Redactor.build({}, detectors=["pem"]))
    cut = len(pem) // 2
    out = stream.feed(f"before {pem[:cut]}") + stream.feed(f"{pem[cut:]} after") + stream.flush()
    assert "BEGIN RSA PRIVATE KEY" not in out
    assert out == "before [REDACTED:pem] after"


def test_ordinary_text_is_not_held_back() -> None:
    """Live monitoring: a chunk that cannot be part of a secret is emitted immediately."""
    stream = StreamRedactor(_r(token=SECRET))
    assert stream.feed("progress: 1%\n") == "progress: 1%\n"
    assert stream.flush() == ""


def test_ordinary_text_is_not_held_back_with_detectors_on() -> None:
    stream = StreamRedactor(Redactor.build({"token": SECRET}, detectors=["pem", "github"]))
    assert stream.feed("waiting for the build to finish\n") == "waiting for the build to finish\n"


def test_only_a_partial_secret_prefix_is_held_back() -> None:
    stream = StreamRedactor(_r(token=SECRET))
    assert stream.feed(f"line\n{SECRET[:6]}") == "line\n"
    assert stream.feed(f"{SECRET[6:]}\n") == "[REDACTED:token]\n"


# -- redact_dump ------------------------------------------------------------------------------


class _Doc(BaseModel):
    """A record shape with one structural number and one free-form container."""

    n: int
    free: dict[str, Any]


def test_redact_dump_keeps_a_free_form_number_redacted_and_a_structural_one_intact() -> None:
    red = _r(pin="4242")
    dumped = red.redact_dump(_Doc(n=4242, free={"pin": 4242, "note": "id 4242"}))
    assert dumped == {"n": 4242, "free": {"pin": "[REDACTED:pin]", "note": "id [REDACTED:pin]"}}
    assert _Doc.model_validate(dumped)


def test_redact_dump_without_secrets_is_the_plain_dump() -> None:
    assert NULL_REDACTOR.redact_dump(_Doc(n=1, free={"a": 2})) == {"n": 1, "free": {"a": 2}}
