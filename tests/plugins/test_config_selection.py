"""``extensions:`` in ``config.yaml``: a run picks up an installed sink and approval prompt.

These are end-to-end: a fake distribution publishes the entry point, ``config.yaml`` names the
id, and ``rayspec run`` is what proves the wiring — the engine is handed the plugin's objects
without knowing anything about plugins.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from .conftest import InstallPlugin

WORKFLOW = """
rayspec: 1
name: demo
isolation: none
steps:
  - {id: hello, shell: echo hi}
"""

APPROVE_WORKFLOW = """
rayspec: 1
name: gated
isolation: none
steps:
  - {id: hello, shell: echo hi}
  - {id: gate, needs: [hello], approve: {message: "ship it?"}}
"""

SINK_MODULE = '''
from pathlib import Path

from rayspec.registry import SinkRegistration


class FileSink:
    """Appends one line per event to the file named in the plugin's settings."""

    def __init__(self, context):
        self.path = Path(context.settings["path"])

    async def emit(self, event):
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(f"{event.type.value}\\n")

    async def emit_stream(self, step_path, record):
        pass

    async def aclose(self):
        pass


SINK = SinkRegistration("recorder", "Recording sink", FileSink)
'''

APPROVAL_MODULE = '''
from pathlib import Path

from rayspec.engine.approval import ApprovalAnswer
from rayspec.registry import ApprovalRegistration


class PolicyApproval:
    """Approves every gate and writes down what it was asked."""

    def __init__(self, context):
        self.path = Path(context.settings["path"])

    async def __call__(self, request):
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(f"{request.step_path}: {request.message}\\n")
        return ApprovalAnswer(approved=True, comment="policy says yes")


APPROVAL = ApprovalRegistration("policy", "Policy approval", PolicyApproval)
'''


def _project(tmp_path: Path, workflow: str, config: str) -> Path:
    root = tmp_path / "proj"
    (root / ".rayspec" / "workflows").mkdir(parents=True)
    (root / ".rayspec" / "workflows" / "demo.yaml").write_text(workflow, encoding="utf-8")
    (root / ".rayspec" / "config.yaml").write_text(config, encoding="utf-8")
    return root


def _run(args: list[str]):
    from rayspec.cli.app import build_app

    return CliRunner().invoke(build_app(), args)


def test_a_configured_sink_observes_the_run(
    install_plugin: InstallPlugin, tmp_path: Path, home: Path
) -> None:
    events = tmp_path / "events.log"
    install_plugin(
        "acme-rayspec",
        modules={"acme_sink": SINK_MODULE},
        entry_points={"rayspec.sinks": {"recorder": "acme_sink:SINK"}},
    )
    project = _project(
        tmp_path,
        WORKFLOW,
        f"extensions:\n  sinks: [recorder]\n  settings:\n    recorder: {{path: {events}}}\n",
    )
    result = _run(["run", "demo", "--root", str(project)])
    assert result.exit_code == 0, result.output
    lines = events.read_text(encoding="utf-8").splitlines()
    assert "run.started" in lines
    assert "run.finished" in lines


def test_a_configured_approval_prompt_answers_the_gate(
    install_plugin: InstallPlugin, tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked = tmp_path / "asked.log"
    install_plugin(
        "acme-rayspec",
        modules={"acme_approval": APPROVAL_MODULE},
        entry_points={"rayspec.approvals": {"policy": "acme_approval:APPROVAL"}},
    )
    project = _project(
        tmp_path,
        APPROVE_WORKFLOW,
        f"extensions:\n  approval: policy\n  settings:\n    policy: {{path: {asked}}}\n",
    )
    monkeypatch.setattr("rayspec.cli._runs_common.stdin_is_tty", lambda: True)
    result = _run(["run", "demo", "--root", str(project)])
    assert result.exit_code == 0, result.output
    assert asked.read_text(encoding="utf-8").startswith("gate: ship it?")


def test_an_unknown_id_fails_with_did_you_mean(tmp_path: Path, home: Path) -> None:
    project = _project(tmp_path, WORKFLOW, "extensions:\n  sinks: [consle]\n")
    result = _run(["run", "demo", "--root", str(project)])
    assert result.exit_code == 2
    assert "unknown sink 'consle'" in result.output
    assert "did you mean 'console'?" in result.output
