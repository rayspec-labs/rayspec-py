"""The redaction boundary of the store seam: a plugin store never receives a secret.

The rule this file pins down: for a store rayspec did not write, redaction happens ONE LAYER
ABOVE it. Whatever the plugin does with what it is handed, the secret is already gone — which
is what makes an unvetted persistence backend safe to install.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from rayspec import registry
from rayspec.events.model import EventType, RunEvent, StreamRecord
from rayspec.redact import NULL_REDACTOR, Redactor
from rayspec.store.base import RunStore
from rayspec.store.model import RunRecord
from rayspec.store.redacting import READ_THROUGH, WRITE_THROUGH, RedactingStore

SECRET = "hunter2-super-secret-token"


class RecordingStore:
    """A plugin-shaped store that keeps every byte it is handed (and nothing else)."""

    #: assigned by the caller the way every store's is; the wrapper never uses it
    redactor: Redactor = NULL_REDACTOR

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or Path("/nowhere")
        self.records: list[RunRecord] = []
        self.outputs: list[tuple[str, str]] = []
        self.prompts: list[str] = []
        self.events: list[RunEvent] = []
        self.streams: list[StreamRecord] = []

    def create(self, run: RunRecord) -> None:
        self.records.append(run)

    def save(self, run: RunRecord) -> None:
        self.records.append(run)

    def load(self, run_id: str) -> RunRecord:
        return self.records[-1]

    def list_runs(self, *, limit: int | None = None) -> list[RunRecord]:
        return list(self.records)

    def run_dir(self, run_id: str) -> Path:
        return self.root / run_id

    def step_dir(self, run_id: str, step_path: str) -> Path:
        return self.root / run_id / step_path

    def write_output(self, run_id: str, step_path: str, content: str, *, kind: str) -> str:
        self.outputs.append((kind, content))
        return f"steps/{step_path}/output.{kind}"

    def read_output(self, run_id: str, output_ref: str) -> str:
        return self.outputs[-1][1]

    def write_prompt(self, run_id: str, step_path: str, text: str) -> str:
        self.prompts.append(text)
        return f"steps/{step_path}/prompt.txt"

    def append_event(self, run_id: str, event: RunEvent) -> None:
        self.events.append(event)

    def append_stream(self, run_id: str, step_path: str, record: StreamRecord) -> None:
        self.streams.append(record)

    def everything(self) -> str:
        """Every byte the store was handed, as one blob (what a test greps for a secret)."""
        parts = [run.model_dump_json() for run in self.records]
        parts += [content for _kind, content in self.outputs]
        parts += self.prompts
        parts += [event.to_json() for event in self.events]
        parts += [record.to_json() for record in self.streams]
        return "\n".join(parts)


def _run_record() -> RunRecord:
    return RunRecord(
        run_id="20260821-101010-aaaa",
        workflow_name="review",
        workflow_path=".rayspec/workflows/review.yaml",
        workflow_hash="deadbeef",
        project_slug="local/demo",
        project_root="/tmp/demo",
        inputs={"token": SECRET, "target": "src"},
        reason=f"failed while calling the API with {SECRET}",
    )


def _feed(store: Any) -> None:
    """Push a secret at every writer of the store surface, the way a run does."""
    store.create(_run_record())
    store.save(_run_record())
    store.write_output("r", "build", f"the token is {SECRET}\n", kind="text")
    store.write_output("r", "build", json.dumps({"token": SECRET, "ok": True}), kind="json")
    store.write_prompt("r", "build", f"use {SECRET} to authenticate")
    store.append_event(
        "r", RunEvent(type=EventType.STEP_STARTED, run_id="r", data={"cmd": f"curl {SECRET}"})
    )
    # split across two deltas: neither half contains the secret on its own
    half = len(SECRET) // 2
    store.append_stream("r", "build", StreamRecord(kind="stdout", text=SECRET[:half]))
    store.append_stream("r", "build", StreamRecord(kind="stdout", text=SECRET[half:] + " done\n"))
    # ends on a prefix of the secret: held back until the step finishes
    store.append_stream("r", "build", StreamRecord(kind="stdout", text="next " + SECRET[:6]))
    store.append_event(
        "r", RunEvent(type=EventType.STEP_FINISHED, run_id="r", step_path="build", data={})
    )


def test_a_plugin_store_is_handed_no_secret() -> None:
    inner = RecordingStore()
    store = RedactingStore(inner, Redactor.build({"token": SECRET}))
    _feed(store)

    blob = inner.everything()
    assert SECRET not in blob
    assert "[REDACTED:token]" in blob
    assert len(inner.streams) == 4  # three deltas plus the tail flushed by step.finished
    text = "".join(record.text for record in inner.streams)
    assert text == "[REDACTED:token] done\nnext " + SECRET[:6]
    # the json output stays a well-formed document with the value replaced
    kind, content = inner.outputs[1]
    assert kind == "json"
    assert json.loads(content) == {"token": "[REDACTED:token]", "ok": True}


def test_the_same_payloads_reach_an_unwrapped_store_unredacted() -> None:
    """The control: without the boundary the store sees the secret (the test can detect it)."""
    inner = RecordingStore()
    _feed(inner)
    assert SECRET in inner.everything()


def test_without_secrets_the_boundary_forwards_untouched() -> None:
    inner = RecordingStore()
    store = RedactingStore(inner)
    _feed(store)
    assert SECRET in inner.everything()  # nothing declared secret, nothing replaced
    assert len(inner.streams) == 3  # nothing is ever held back without a redactor


def test_the_wrapper_is_a_run_store_and_implements_every_protocol_member() -> None:
    """A protocol member added later must be implemented here — not delegated blindly."""
    store = RedactingStore(RecordingStore())
    assert isinstance(store, RunStore)
    members = {
        name
        for name in RunStore.__protocol_attrs__  # type: ignore[attr-defined]
        if name != "redactor"
    }
    assert members <= set(vars(RedactingStore))
    assert not members & (READ_THROUGH | WRITE_THROUGH)


def test_an_unreviewed_member_is_not_reachable_through_the_boundary() -> None:
    store = RedactingStore(RecordingStore())
    with pytest.raises(AttributeError, match="every store write must be redacted"):
        _ = store.record_step
    assert not hasattr(store, "record_step")
    assert store.root == Path("/nowhere")  # a reviewed read-only member is forwarded


def test_an_optional_writer_answers_for_the_wrapped_store() -> None:
    """``hasattr`` keeps answering for the WRAPPED store: an older store degrades as before."""

    class WithoutPrompt:
        """Only the protocol — no ``write_prompt`` (an in-memory or older store)."""

        def __getattr__(self, name: str) -> Any:
            raise AttributeError(name)

    assert not hasattr(RedactingStore(WithoutPrompt()), "write_prompt")
    assert hasattr(RedactingStore(RecordingStore()), "write_prompt")


def test_write_output_with_sha_is_redacted_too() -> None:
    class WithSha(RecordingStore):
        def write_output_with_sha(self, run_id: str, step_path: str, content: str, *, kind: str):
            self.outputs.append((kind, content))
            return content

    inner = WithSha()
    store = RedactingStore(inner, Redactor.build({"token": SECRET}))
    written = store.write_output_with_sha("r", "build", f"token {SECRET}", kind="text")
    assert SECRET not in written
    assert "[REDACTED:token]" in written


def test_create_store_wraps_a_plugin_store_but_not_the_builtin(tmp_path: Path) -> None:
    from rayspec.store.file import FileRunStore

    registry.register_store(
        registry.StoreRegistration("recording", "Recording", lambda ctx: RecordingStore(ctx.root))
    )
    context = registry.StoreContext(root=tmp_path / "p", home=tmp_path)
    plugin_store = registry.create_store("recording", context)
    assert isinstance(plugin_store, RedactingStore)
    assert isinstance(registry.create_store("file", context), FileRunStore)

    plugin_store.redactor = Redactor.build({"token": SECRET})
    _feed(plugin_store)
    assert SECRET not in plugin_store.inner.everything()
