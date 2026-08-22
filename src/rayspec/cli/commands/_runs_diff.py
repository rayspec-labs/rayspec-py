# SPDX-License-Identifier: Apache-2.0
"""Compare two runs of ONE workflow — the model behind ``rayspec runs diff``.

Module boundary: pure comparison and rendering over two :class:`~rayspec.store.model.RunRecord`
values and their stores. It resolves nothing (the command does the lookup) and decides nothing
about exit codes beyond :attr:`RunDiff.changed`.

What counts as a *difference* is deliberately narrow, because ``--exit-code`` is meant to work as
a CI gate: the run status, the set of recorded steps, each step's status, output hash and
fingerprint, and the workflow outputs. Duration, tokens and cost are reported but never set
``changed`` — two identical runs of a real agent differ there every time.
"""

from __future__ import annotations

import difflib
import json
from dataclasses import dataclass, field
from typing import Any

from rich.console import Console
from rich.text import Text

from rayspec.cli import _runs_common as common
from rayspec.cli.commands._loader_common import new_table
from rayspec.store.file import FileRunStore
from rayspec.store.model import RunRecord, StepRecord
from rayspec.textsafe import safe_text

#: Lines of unified diff shown per changed step output (``--outputs``).
DIFF_CONTEXT = 3


def _delta(a: float | None, b: float | None) -> float | None:
    if a is None or b is None:
        return None
    return b - a


@dataclass(frozen=True, slots=True)
class StepDiff:
    """One step path as the two runs recorded it (either side may be missing)."""

    path: str
    a: StepRecord | None
    b: StepRecord | None

    @property
    def change(self) -> str:
        """``added`` (only in b) · ``removed`` (only in a) · ``changed`` · ``same``."""
        if self.a is None:
            return "added"
        if self.b is None:
            return "removed"
        return "changed" if self.reasons else "same"

    @property
    def reasons(self) -> tuple[str, ...]:
        """Why this step is ``changed`` — the fields a CI gate should care about."""
        if self.a is None or self.b is None:
            return ()
        out: list[str] = []
        if self.a.status is not self.b.status:
            out.append("status")
        if self.a.output_sha256 != self.b.output_sha256:
            out.append("output")
        if self.a.fingerprint != self.b.fingerprint:
            out.append("fingerprint")
        return tuple(out)

    def to_json(self) -> dict[str, Any]:
        """The ``--json`` row for this step."""

        def side(rec: StepRecord | None, attr: str) -> Any:
            if rec is None:
                return None
            value = getattr(rec, attr)
            return value.value if hasattr(value, "value") else value

        tokens_a = self.a.usage.total if self.a else None
        tokens_b = self.b.usage.total if self.b else None
        return {
            "path": self.path,
            "kind": side(self.a, "kind") or side(self.b, "kind"),
            "change": self.change,
            "reasons": list(self.reasons),
            "status": {"a": side(self.a, "status"), "b": side(self.b, "status")},
            "output": {
                "a": side(self.a, "output_sha256"),
                "b": side(self.b, "output_sha256"),
                "changed": "output" in self.reasons,
            },
            "fingerprint": {
                "a": side(self.a, "fingerprint"),
                "b": side(self.b, "fingerprint"),
                "changed": "fingerprint" in self.reasons,
            },
            "duration_ms": {
                "a": side(self.a, "duration_ms"),
                "b": side(self.b, "duration_ms"),
                "delta": _delta(side(self.a, "duration_ms"), side(self.b, "duration_ms")),
            },
            "tokens": {"a": tokens_a, "b": tokens_b, "delta": _delta(tokens_a, tokens_b)},
            "cost_usd": {
                "a": side(self.a, "cost_usd"),
                "b": side(self.b, "cost_usd"),
                "delta": _delta(side(self.a, "cost_usd"), side(self.b, "cost_usd")),
            },
        }


@dataclass(frozen=True, slots=True)
class LoopDiff:
    """Iteration/item counts of one composite step in both runs."""

    path: str
    kind: str
    a: dict[str, Any] | None
    b: dict[str, Any] | None

    @property
    def changed(self) -> bool:
        return self.a != self.b

    def to_json(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "a": self.a,
            "b": self.b,
            "changed": self.changed,
        }


def _counts(rec: StepRecord | None) -> dict[str, Any] | None:
    if rec is None:
        return None
    if rec.loop is not None:
        return {"iterations": rec.loop.iterations, "converged": rec.loop.converged}
    if rec.each is not None:
        return {
            "total": rec.each.total,
            "succeeded": rec.each.succeeded,
            "failed": rec.each.failed,
        }
    return None


@dataclass(slots=True)
class RunDiff:
    """Everything ``runs diff`` knows about two runs of one workflow."""

    store_a: FileRunStore
    run_a: RunRecord
    store_b: FileRunStore
    run_b: RunRecord
    steps: list[StepDiff] = field(default_factory=list)
    loops: list[LoopDiff] = field(default_factory=list)

    @property
    def workflow(self) -> str:
        return self.run_a.workflow_name

    @property
    def status_changed(self) -> bool:
        return self.run_a.status is not self.run_b.status

    @property
    def outputs(self) -> dict[str, dict[str, Any]]:
        """``{name: {a, b, changed}}`` over the union of both runs' workflow outputs."""
        a = self.run_a.outputs or {}
        b = self.run_b.outputs or {}
        return {
            name: {"a": a.get(name), "b": b.get(name), "changed": a.get(name) != b.get(name)}
            for name in sorted(set(a) | set(b))
        }

    @property
    def changed(self) -> bool:
        """Whether anything a CI gate should fail on differs (see the module docstring)."""
        if self.status_changed:
            return True
        if any(step.change != "same" for step in self.steps):
            return True
        return any(entry["changed"] for entry in self.outputs.values())

    def output_diff(self, step: StepDiff) -> list[str]:
        """Unified diff of one step's stored output text (``--outputs``); ``[]`` when equal."""
        if step.a is None or step.b is None:
            return []
        text_a = common.read_output_text(self.store_a, self.run_a, step.a) or ""
        text_b = common.read_output_text(self.store_b, self.run_b, step.b) or ""
        if text_a == text_b:
            return []
        return list(
            difflib.unified_diff(
                safe_text(text_a).splitlines(),
                safe_text(text_b).splitlines(),
                fromfile=f"{self.run_a.run_id}:{safe_text(step.path)}",
                tofile=f"{self.run_b.run_id}:{safe_text(step.path)}",
                lineterm="",
                n=DIFF_CONTEXT,
            )
        )

    def to_json(self) -> dict[str, Any]:
        """The ``--json`` payload."""
        usage_a, usage_b = self.run_a.total_usage(), self.run_b.total_usage()
        cost_a, cost_b = self.run_a.total_cost_usd(), self.run_b.total_cost_usd()
        ms_a = common.run_duration_ms(self.run_a)
        ms_b = common.run_duration_ms(self.run_b)
        return {
            "workflow": self.workflow,
            "a": _side(self.run_a),
            "b": _side(self.run_b),
            "workflow_hash_changed": self.run_a.workflow_hash != self.run_b.workflow_hash,
            "status": {
                "a": self.run_a.status.value,
                "b": self.run_b.status.value,
                "changed": self.status_changed,
            },
            "duration_ms": {"a": ms_a, "b": ms_b, "delta": _delta(ms_a, ms_b)},
            "tokens": {
                "a": usage_a.total,
                "b": usage_b.total,
                "delta": usage_b.total - usage_a.total,
            },
            "cost_usd": {"a": cost_a, "b": cost_b, "delta": _delta(cost_a, cost_b)},
            "steps": [step.to_json() for step in self.steps],
            "loops": [loop.to_json() for loop in self.loops],
            "outputs": self.outputs,
            "changed": self.changed,
        }


def _side(run: RunRecord) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "status": run.status.value,
        "reason": run.reason,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "dry_run": run.dry_run,
        "workflow_hash": run.workflow_hash,
        "project_slug": run.project_slug,
    }


def build_diff(
    store_a: FileRunStore, run_a: RunRecord, store_b: FileRunStore, run_b: RunRecord
) -> RunDiff:
    """Compare two runs of the same workflow (the caller has already refused mismatched names)."""
    diff = RunDiff(store_a=store_a, run_a=run_a, store_b=store_b, run_b=run_b)
    for path in sorted(set(run_a.steps) | set(run_b.steps)):
        rec_a, rec_b = run_a.steps.get(path), run_b.steps.get(path)
        diff.steps.append(StepDiff(path=path, a=rec_a, b=rec_b))
        counts_a, counts_b = _counts(rec_a), _counts(rec_b)
        if counts_a is not None or counts_b is not None:
            either = rec_a if rec_a is not None else rec_b
            kind = either.kind if either is not None else ""
            diff.loops.append(LoopDiff(path=path, kind=kind, a=counts_a, b=counts_b))
    return diff


# --------------------------------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------------------------------


def _signed(value: float | None, unit: str = "") -> str:
    if value is None or value == 0:
        return ""
    return f"({value:+g}{unit})"


def render(diff: RunDiff, out: Console, *, show_steps: bool, show_outputs: bool) -> None:
    """Print the human report: header, changed steps, loop counts, outputs, optional diffs."""
    out.print(f"runs diff — workflow {safe_text(diff.workflow)}", markup=False, highlight=False)
    header = new_table()
    header.add_column("")
    header.add_column(f"a: {diff.run_a.run_id}")
    header.add_column(f"b: {diff.run_b.run_id}")
    header.add_column("delta")
    payload = diff.to_json()
    if diff.run_a.project_slug != diff.run_b.project_slug:
        # `--across-projects`: the report is only meaningful if the reader can see this
        header.add_row(
            "project",
            safe_text(diff.run_a.project_slug),
            safe_text(diff.run_b.project_slug),
            "changed",
        )
    header.add_row(
        "status",
        diff.run_a.status.value,
        diff.run_b.status.value,
        "changed" if diff.status_changed else "",
    )
    header.add_row(
        "duration",
        common.fmt_duration(payload["duration_ms"]["a"]),
        common.fmt_duration(payload["duration_ms"]["b"]),
        _signed(payload["duration_ms"]["delta"], "ms"),
    )
    header.add_row(
        "tokens",
        common.fmt_tokens(payload["tokens"]["a"]),
        common.fmt_tokens(payload["tokens"]["b"]),
        _signed(payload["tokens"]["delta"]),
    )
    header.add_row(
        "cost",
        common.fmt_cost(
            payload["cost_usd"]["a"], common.run_cost_source(diff.run_a), diff.run_a.total_usage()
        ),
        common.fmt_cost(
            payload["cost_usd"]["b"], common.run_cost_source(diff.run_b), diff.run_b.total_usage()
        ),
        _signed(payload["cost_usd"]["delta"]),
    )
    out.print(header)
    if payload["workflow_hash_changed"]:
        out.print(
            "  note: the workflow changed between these runs (different workflow_hash)",
            markup=False,
            highlight=False,
        )

    shown = [s for s in diff.steps if show_steps or s.change != "same"]
    if shown:
        table = new_table(title="steps")
        table.add_column("path")
        table.add_column("a")
        table.add_column("b")
        table.add_column("change")
        for step in shown:
            row = step.to_json()
            change = step.change
            if change == "changed":
                change = f"changed: {', '.join(step.reasons)}"
            table.add_row(
                Text(safe_text(step.path)),
                str(row["status"]["a"] or "-"),
                str(row["status"]["b"] or "-"),
                change,
            )
        out.print(table)

    loops = [loop for loop in diff.loops if loop.changed or show_steps]
    if loops:
        table = new_table(title="loops / each")
        table.add_column("path")
        table.add_column("a")
        table.add_column("b")
        for loop in loops:
            table.add_row(
                Text(safe_text(loop.path)),
                json.dumps(loop.a, ensure_ascii=False) if loop.a else "-",
                json.dumps(loop.b, ensure_ascii=False) if loop.b else "-",
            )
        out.print(table)

    outputs = {
        name: entry
        for name, entry in diff.outputs.items()
        if entry["changed"] or show_outputs or show_steps
    }
    if outputs:
        table = new_table(title="outputs")
        table.add_column("name")
        table.add_column("a")
        table.add_column("b")
        for name, entry in outputs.items():
            table.add_row(
                Text(safe_text(str(name))),
                Text(common.value_text(entry["a"])),
                Text(common.value_text(entry["b"])),
            )
        out.print(table)

    if show_outputs:
        for step in diff.steps:
            lines = diff.output_diff(step)
            if not lines:
                continue
            out.print(f"--- step {safe_text(step.path)}", markup=False, highlight=False)
            for line in lines:
                out.print(line, markup=False, highlight=False)

    if not diff.changed:
        out.print("no differences", markup=False, highlight=False)


__all__ = [
    "DIFF_CONTEXT",
    "LoopDiff",
    "RunDiff",
    "StepDiff",
    "build_diff",
    "render",
]
