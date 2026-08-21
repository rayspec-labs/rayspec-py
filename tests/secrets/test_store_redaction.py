"""Every ``FileRunStore`` writer goes through the Redactor.

A writer that bypasses the store leaks; these tests pin the store side so that anything
persisting through it is covered automatically.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from rayspec.events.model import EventType, RunEvent, StreamRecord
from rayspec.redact import NULL_REDACTOR, Redactor
from rayspec.store.file import FileRunStore
from rayspec.store.model import RunRecord, StepRecord

SECRET = "ghp_SECRETTOKEN_ABCDEF"


def _store(tmp_path: Path, redactor: Redactor | None = None) -> FileRunStore:
    store = FileRunStore(tmp_path / "project")
    if redactor is not None:
        store.redactor = redactor
    return store


def _run(run_id: str = "r1") -> RunRecord:
    return RunRecord(
        run_id=run_id,
        workflow_name="wf",
        workflow_path="/tmp/p/.rayspec/workflows/wf.yaml",
        workflow_hash="sha256:h",
        project_slug="local/p",
        project_root="/tmp/p",
        started_at=datetime.now(UTC),
    )


def test_default_store_has_the_null_redactor(tmp_path: Path) -> None:
    assert _store(tmp_path).redactor is NULL_REDACTOR


def test_run_json_is_redacted(tmp_path: Path) -> None:
    store = _store(tmp_path, Redactor.build({"token": SECRET}))
    run = _run()
    run.inputs = {"echoed": f"value {SECRET}"}
    store.create(run)
    text = (store.run_dir("r1") / "run.json").read_text()
    assert SECRET not in text
    assert "[REDACTED:token]" in text


def test_run_json_is_redacted_in_its_escaped_form(tmp_path: Path) -> None:
    value = 'quote" and \\ and\nnewline-secret'
    store = _store(tmp_path, Redactor.build({"token": value}))
    run = _run()
    run.inputs = {"echoed": value}
    store.create(run)
    text = (store.run_dir("r1") / "run.json").read_text()
    assert value not in text and json.dumps(value)[1:-1] not in text


def test_output_files_are_redacted_and_the_sha_matches(tmp_path: Path) -> None:
    store = _store(tmp_path, Redactor.build({"token": SECRET}))
    run = _run()
    store.create(run)
    written = store.write_output_with_sha("r1", "s", f"out {SECRET}\n", kind="text")
    assert written.path.read_text() == "out [REDACTED:token]\n"
    import hashlib

    assert written.sha256 == hashlib.sha256(written.path.read_bytes()).hexdigest()


def test_json_outputs_are_redacted(tmp_path: Path) -> None:
    store = _store(tmp_path, Redactor.build({"token": SECRET}))
    store.create(_run())
    store.write_output("r1", "s", json.dumps({"k": SECRET}), kind="json")
    text = store.read_output("r1", "steps/s/output.json")
    assert SECRET not in text and "[REDACTED:token]" in text


def test_record_step_redacts_output_and_run_json(tmp_path: Path) -> None:
    store = _store(tmp_path, Redactor.build({"token": SECRET}))
    run = _run()
    store.create(run)
    record = StepRecord(id="s", path="s", kind="shell")
    record.error = None
    store.record_step(run, record, f"printed {SECRET}", kind="text")
    assert SECRET not in (store.run_dir("r1") / "run.json").read_text()
    assert SECRET not in store.read_output("r1", "steps/s/output.txt")


def test_events_are_redacted(tmp_path: Path) -> None:
    store = _store(tmp_path, Redactor.build({"token": SECRET}))
    store.create(_run())
    store.append_event(
        "r1", RunEvent(type=EventType.WARNING, run_id="r1", data={"message": f"saw {SECRET}"})
    )
    text = (store.run_dir("r1") / "events.jsonl").read_text()
    assert SECRET not in text and "[REDACTED:token]" in text


def test_stream_records_are_redacted(tmp_path: Path) -> None:
    store = _store(tmp_path, Redactor.build({"token": SECRET}))
    store.create(_run())
    store.append_stream("r1", "s", StreamRecord(kind="stdout", text=f"{SECRET}\n"))
    store.flush_streams("r1")
    text = (store.step_dir("r1", "s") / "stream.jsonl").read_text()
    assert SECRET not in text and "[REDACTED:token]" in text


def test_a_secret_split_across_two_stream_records_is_still_redacted(tmp_path: Path) -> None:
    store = _store(tmp_path, Redactor.build({"token": SECRET}))
    store.create(_run())
    store.append_stream("r1", "s", StreamRecord(kind="text", text=f"a{SECRET[:9]}"))
    store.append_stream("r1", "s", StreamRecord(kind="text", text=f"{SECRET[9:]}b"))
    store.flush_streams("r1", "s")
    text = (store.step_dir("r1", "s") / "stream.jsonl").read_text()
    assert SECRET not in text
    joined = "".join(json.loads(line).get("text") or "" for line in text.splitlines())
    assert joined == "a[REDACTED:token]b"


def test_record_step_flushes_the_held_back_stream_tail(tmp_path: Path) -> None:
    store = _store(tmp_path, Redactor.build({"token": SECRET}))
    run = _run()
    store.create(run)
    store.append_stream("r1", "s", StreamRecord(kind="text", text="tail-without-secret"))
    store.record_step(run, StepRecord(id="s", path="s", kind="prompt"))
    text = (store.step_dir("r1", "s") / "stream.jsonl").read_text()
    joined = "".join(json.loads(line).get("text") or "" for line in text.splitlines())
    assert joined == "tail-without-secret"


def test_without_a_redactor_nothing_is_held_back(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create(_run())
    store.append_stream("r1", "s", StreamRecord(kind="text", text="hello"))
    line = json.loads((store.step_dir("r1", "s") / "stream.jsonl").read_text().strip())
    assert line["text"] == "hello"


@pytest.mark.parametrize("kind", ["text", "stdout", "stderr", "tool_call"])
def test_every_stream_kind_is_redacted(tmp_path: Path, kind: str) -> None:
    store = _store(tmp_path, Redactor.build({"token": SECRET}))
    store.create(_run())
    store.append_stream(
        "r1", "s", StreamRecord(kind=kind, text=f"x{SECRET}x", data={"arg": SECRET})
    )
    store.flush_streams("r1")
    assert SECRET not in (store.step_dir("r1", "s") / "stream.jsonl").read_text()


def test_the_step_finished_event_flushes_the_held_back_stream_tail(tmp_path: Path) -> None:
    """The engine never calls ``record_step``/``flush_streams``; it emits events. The store
    flushes on the event a step ends with, so no tail is lost on the live path."""
    store = _store(tmp_path, Redactor.build({"token": SECRET}))
    store.create(_run())
    store.append_stream("r1", "s", StreamRecord(kind="stdout", text=f"line\n{SECRET[:6]}"))
    store.append_event(
        "r1", RunEvent(type=EventType.STEP_FINISHED, run_id="r1", step_path="s", data={})
    )
    text = (store.step_dir("r1", "s") / "stream.jsonl").read_text()
    joined = "".join(json.loads(line).get("text") or "" for line in text.splitlines())
    assert joined == f"line\n{SECRET[:6]}"


def test_the_run_finished_event_flushes_every_step(tmp_path: Path) -> None:
    store = _store(tmp_path, Redactor.build({"token": SECRET}))
    store.create(_run())
    for step in ("a", "b"):
        store.append_stream("r1", step, StreamRecord(kind="stdout", text=SECRET[:6]))
    store.append_event("r1", RunEvent(type=EventType.RUN_FINISHED, run_id="r1", data={}))
    for step in ("a", "b"):
        text = (store.step_dir("r1", step) / "stream.jsonl").read_text()
        joined = "".join(json.loads(line).get("text") or "" for line in text.splitlines())
        assert joined == SECRET[:6]


def test_a_bare_token_secret_keeps_a_json_output_well_formed(tmp_path: Path) -> None:
    """Redacting the serialised text turns ``{"pin": 12345678}`` into a broken document; the
    store parses first and redacts the VALUE."""
    store = _store(tmp_path, Redactor.build({"pin": "12345678"}))
    store.create(_run())
    written = store.write_output_with_sha(
        "r1", "s", '{"pin": 12345678, "ok": true, "note": "id 12345678"}', kind="json"
    )
    text = (store.step_dir("r1", "s") / "output.json").read_text()
    assert json.loads(text) == {
        "pin": "[REDACTED:pin]",
        "ok": True,
        "note": "id [REDACTED:pin]",
    }
    assert "12345678" not in text
    assert written.sha256 == hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_the_redactor_is_part_of_the_run_store_protocol(tmp_path: Path) -> None:
    """The subprocess pump reads it off the store it was handed; a rename must be a type error,
    not a silent fallback to "no redaction"."""
    from rayspec.store.base import RunStore

    assert "redactor" in RunStore.__annotations__
    store: RunStore = _store(tmp_path, Redactor.build({"token": SECRET}))
    assert store.redactor.redact(SECRET) == "[REDACTED:token]"


# -- the record is redacted as a VALUE, never as serialised text --------------------------------


def test_a_numeric_secret_keeps_run_json_parseable(tmp_path: Path) -> None:
    """Redacting the serialised text rewrites ``"budget": 4242`` to an unquoted marker and the
    checkpoint stops parsing — taking ``show``/``resume``/``runs`` down with it."""
    store = _store(tmp_path, Redactor.build({"pin": "4242"}))
    run = _run()
    run.outputs = {"budget": 4242, "note": "account 4242 is the one"}
    store.create(run)
    text = (store.run_dir("r1") / "run.json").read_text()
    assert json.loads(text)  # parses at all
    assert "4242" not in text
    assert store.load("r1").outputs == {
        "budget": "[REDACTED:pin]",
        "note": "account [REDACTED:pin] is the one",
    }


def test_a_numeric_secret_does_not_eat_a_longer_number(tmp_path: Path) -> None:
    """``91234`` merely CONTAINS the secret; replacing the digits inside it would leave
    ``9[REDACTED:pin]`` where a JSON number belongs."""
    store = _store(tmp_path, Redactor.build({"pin": "1234"}))
    run = _run()
    run.outputs = {"other": 91234, "exact": 1234}
    store.create(run)
    text = (store.run_dir("r1") / "run.json").read_text()
    assert json.loads(text)
    assert store.load("r1").outputs == {"other": 91234, "exact": "[REDACTED:pin]"}


def test_a_structural_number_equal_to_a_secret_is_left_alone(tmp_path: Path) -> None:
    """A step duration that happens to equal the secret is a coincidence, not a leak: turning it
    into a marker string would only make the record unreadable."""
    store = _store(tmp_path, Redactor.build({"pin": "1234"}))
    run = _run()
    run.steps["s"] = StepRecord(id="s", path="s", kind="shell", duration_ms=1234)
    run.outputs = {"leak": 1234}
    store.create(run)
    reloaded = store.load("r1")
    assert reloaded.steps["s"].duration_ms == 1234
    assert reloaded.outputs == {"leak": "[REDACTED:pin]"}


def test_a_secret_with_quotes_and_newlines_never_reaches_run_json(tmp_path: Path) -> None:
    value = 'a"b\\c\nd\te'
    store = _store(tmp_path, Redactor.build({"token": value}))
    run = _run()
    run.outputs = {"echoed": f"<<{value}>>"}
    run.reason = f"failed on {value}"
    store.create(run)
    text = (store.run_dir("r1") / "run.json").read_text()
    assert json.loads(text)
    assert value not in text and json.dumps(value)[1:-1] not in text


def test_a_unicode_secret_never_reaches_run_json(tmp_path: Path) -> None:
    value = "pässwörd-ﬁ-🔐"
    store = _store(tmp_path, Redactor.build({"token": value}))
    run = _run()
    run.outputs = {"echoed": value}
    store.create(run)
    text = (store.run_dir("r1") / "run.json").read_text()
    assert json.loads(text)
    assert value not in text


def test_a_numeric_secret_keeps_events_jsonl_parseable(tmp_path: Path) -> None:
    store = _store(tmp_path, Redactor.build({"pin": "4242"}))
    store.create(_run())
    store.append_event(
        "r1", RunEvent(type=EventType.WARNING, run_id="r1", data={"budget": 4242, "n": 94242})
    )
    line = (store.run_dir("r1") / "events.jsonl").read_text().strip()
    assert json.loads(line)["data"] == {"budget": "[REDACTED:pin]", "n": 94242}
    assert list(store.read_events("r1"))  # still readable through the store


def test_a_numeric_secret_keeps_stream_jsonl_parseable(tmp_path: Path) -> None:
    store = _store(tmp_path, Redactor.build({"pin": "4242"}))
    store.create(_run())
    store.append_stream(
        "r1", "s", StreamRecord(kind="tool_call", name="t 4242", data={"arg": 4242})
    )
    store.flush_streams("r1")
    line = (store.step_dir("r1", "s") / "stream.jsonl").read_text().strip()
    parsed = json.loads(line)
    assert parsed["data"] == {"arg": "[REDACTED:pin]"}
    assert parsed["name"] == "t [REDACTED:pin]"
    assert parsed["attempt"] == 1


def test_a_secret_used_as_a_json_key_does_not_reach_run_json(tmp_path: Path) -> None:
    """A structured provider result is stored as parsed JSON, so a secret can arrive in the KEY
    position; nothing else redacts it on the way in."""
    store = _store(tmp_path, Redactor.build({"token": SECRET}))
    run = _run()
    run.outputs = {"probe": {SECRET: "value", "nested": {f"{SECRET}-suffix": 1}}}
    store.create(run)
    text = (store.run_dir("r1") / "run.json").read_text()
    assert SECRET not in text
    assert json.loads(text)["outputs"]["probe"] == {
        "[REDACTED:token]": "value",
        "nested": {"[REDACTED:token]-suffix": 1},
    }


def test_a_secret_used_as_an_event_data_key_is_redacted(tmp_path: Path) -> None:
    store = _store(tmp_path, Redactor.build({"token": SECRET}))
    store.create(_run())
    store.append_event(
        "r1", RunEvent(type=EventType.WARNING, run_id="r1", data={SECRET: "seen", "n": SECRET})
    )
    text = (store.run_dir("r1") / "events.jsonl").read_text()
    assert SECRET not in text
    assert json.loads(text)["data"] == {"[REDACTED:token]": "seen", "n": "[REDACTED:token]"}


def test_a_stream_records_call_id_is_redacted(tmp_path: Path) -> None:
    """``call_id`` is provider-supplied and used to be covered by redacting the serialised
    line; it needs its own pass now that the line is built from redacted values."""
    store = _store(tmp_path, Redactor.build({"token": SECRET}))
    store.create(_run())
    store.append_stream("r1", "s", StreamRecord(kind="tool_call", call_id=f"call-{SECRET}"))
    store.flush_streams("r1")
    assert SECRET not in (store.step_dir("r1", "s") / "stream.jsonl").read_text()


def test_a_secret_matching_a_field_name_does_not_drop_the_field(tmp_path: Path) -> None:
    """A field name names a place in the record, it never carries a value. Rewriting one drops
    the field on the way back in — pydantic ignores what it does not know — so a short secret
    that happens to appear in a field name would silently empty the record it was protecting."""
    store = _store(tmp_path, Redactor.build({"pw": "inputs"}))
    run = _run()
    run.inputs = {"kept": "value"}
    store.create(run)
    text = (store.run_dir("r1") / "run.json").read_text()
    assert '"inputs"' in text
    assert store.load("r1").inputs == {"kept": "value"}


def test_a_secret_matching_a_step_path_keeps_the_steps_addressable(tmp_path: Path) -> None:
    """``steps`` is keyed by step path — the name of a directory under the run, not a value."""
    store = _store(tmp_path, Redactor.build({"pw": "build"}))
    run = _run()
    run.steps = {"build": StepRecord(id="build", path="build", kind="shell")}
    store.create(run)
    assert set(store.load("r1").steps) == {"build"}
