"""The Redactor: exact-match replacement at every writer, chunk boundaries,
opt-in builtin detectors."""

from __future__ import annotations

import json

import pytest

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
