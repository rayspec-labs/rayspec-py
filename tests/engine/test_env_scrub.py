"""Shell/python steps do not inherit launcher-only variables (VIRTUAL_ENV & co.)."""

from __future__ import annotations

import os
import sys

import pytest

from rayspec.engine.executors._process import LAUNCHER_ENV_VARS, scrub_launcher_env
from rayspec.engine.scheduler import run_graph
from rayspec.schema import StepStatus

from .conftest import Harness, make_graph_harness

pytestmark = pytest.mark.anyio


def test_scrub_drops_named_vars_and_anything_pointing_at_the_launcher_venv() -> None:
    env = {
        "VIRTUAL_ENV": "/repo/.venv",
        "VIRTUAL_ENV_PROMPT": "(rayspec)",
        "UV_PROJECT_ENVIRONMENT": "/repo/.venv",
        "PYTHONHOME": "/usr/lib/python",
        "PYTHONPATH": "/repo/.venv/lib/python3.12/site-packages",
        "MY_TOOL_HOME": "/repo/.venv/share/tool",
        "PATH": "/repo/.venv/bin:/usr/bin",
        "HOME": "/home/me",
        "UNRELATED": "/repo/.venvs-are-fun",
    }
    out = scrub_launcher_env(env, venvs=("/repo/.venv",))
    assert "VIRTUAL_ENV" not in out and "VIRTUAL_ENV_PROMPT" not in out
    assert "UV_PROJECT_ENVIRONMENT" not in out and "PYTHONHOME" not in out
    # values pointing INTO the launcher's venv go too (a generic rule, not a fixed list)
    assert "PYTHONPATH" not in out and "MY_TOOL_HOME" not in out
    # PATH is kept as is (only the variables above are launcher-only), so are plain values
    assert out["PATH"] == "/repo/.venv/bin:/usr/bin"
    assert out["HOME"] == "/home/me" and out["UNRELATED"] == "/repo/.venvs-are-fun"
    assert {"VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "PYTHONHOME"} <= LAUNCHER_ENV_VARS
    # the input mapping is never mutated
    assert "VIRTUAL_ENV" in env


def test_scrub_filters_list_values_entry_by_entry() -> None:
    """Review fix: a PATH-like list loses only the entries inside the venv, never the rest."""
    sep = os.pathsep
    env = {
        "PYTHONPATH": sep.join(["/repo/.venv/lib/python3.12/site-packages", "/home/me/lib"]),
        "LD_LIBRARY_PATH": sep.join(["/usr/lib", "/repo/.venv/lib", "/opt/lib"]),
        "ALL_VENV": sep.join(["/repo/.venv/a", "/repo/.venv/b"]),
        "PATH": sep.join(["/repo/.venv/bin", "/usr/bin"]),
        "NOT_A_LIST": "a:b" if sep != ":" else "http://x/y",
    }
    out = scrub_launcher_env(env, venvs=("/repo/.venv",))
    assert out["PYTHONPATH"] == "/home/me/lib"
    assert out["LD_LIBRARY_PATH"] == sep.join(["/usr/lib", "/opt/lib"])
    # every entry pointed into the venv: the variable goes
    assert "ALL_VENV" not in out
    # PATH is still untouched; values that merely contain the separator are kept verbatim
    assert out["PATH"] == env["PATH"] and out["NOT_A_LIST"] == env["NOT_A_LIST"]


def test_scrub_uses_the_running_interpreter_prefix_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "prefix", "/fake/venv")
    monkeypatch.setattr(sys, "base_prefix", "/usr")
    out = scrub_launcher_env({"X_HOME": "/fake/venv/share/x", "KEEP": "1", "Y": "/usr/share"})
    assert out == {"KEEP": "1", "Y": "/usr/share"}
    # not inside a venv: sys.prefix is not a launcher venv (only $VIRTUAL_ENV counts)
    monkeypatch.setattr(sys, "base_prefix", "/fake/venv")
    out = scrub_launcher_env({"X_HOME": "/fake/venv/share/x", "KEEP": "1"})
    assert out == {"X_HOME": "/fake/venv/share/x", "KEEP": "1"}
    out = scrub_launcher_env({"VIRTUAL_ENV": "/other", "X": "/other/bin/x", "KEEP": "1"})
    assert out == {"KEEP": "1"}


async def test_shell_and_python_steps_do_not_see_virtual_env_unless_set(harness: Harness) -> None:
    harness.workflow(
        "t",
        """
        rayspec: 1
        name: t
        steps:
          - id: inherited
            shell: 'printf "%s|%s|%s" "${VIRTUAL_ENV-unset}" "${UV_PROJECT_ENVIRONMENT-unset}" "${PYTHONHOME-unset}"'
          - id: explicit
            env: {VIRTUAL_ENV: /my/own/venv}
            shell: 'printf "%s" "${VIRTUAL_ENV-unset}"'
          - id: py
            python: |
              import os
              print(os.environ.get("VIRTUAL_ENV", "unset"), os.environ.get("HOME_KEPT", "unset"))
        """,
    )
    g = make_graph_harness(harness, harness.load("t"), fake_leaf=False)
    g.ctx.env = {
        **g.ctx.env,
        "PATH": "/usr/bin:/bin",
        "VIRTUAL_ENV": "/rayspec/.venv",
        "UV_PROJECT_ENVIRONMENT": "/rayspec/.venv",
        "PYTHONHOME": "/rayspec/.venv",
        "HOME_KEPT": "yes",
    }
    outcomes = await run_graph(g.graph, g.scope, g.ctx)
    assert {k: o.record.status for k, o in outcomes.items()} == {
        "inherited": StepStatus.SUCCEEDED,
        "explicit": StepStatus.SUCCEEDED,
        "py": StepStatus.SUCCEEDED,
    }
    assert outcomes["inherited"].output == "unset|unset|unset"
    assert outcomes["explicit"].output == "/my/own/venv"
    assert outcomes["py"].output == "unset yes"
