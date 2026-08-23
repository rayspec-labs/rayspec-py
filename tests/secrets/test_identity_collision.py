# SPDX-License-Identifier: Apache-2.0
"""A `secret: true` value that EQUALS one of the strings a run is recorded under.

The run's addresses — its id, its workflow's name and file, its project root, its workspace
directory — are what `resume`, `approve` and `explain` resolve it by, so the record keeps them in
clear (see `test_record_identity.py`). Redaction used to rewrite them everywhere else anyway: the
live console said `run 20260823-… started ([REDACTED:token])` and `events.jsonl` agreed, while
`show`, `runs` and `audit` printed the workflow's real name from `run.json`.

That disagreement is worse than either half. It protects nothing — the string is a file name in
the project, readable by anyone who can read the run directory — and it DISCLOSES: a
`[REDACTED:token]` standing where the reader can look the true content up one file over says
exactly which public string the secret is. So rayspec does not half-hide it. The value is not
redacted anywhere, and the run says so by name, the same answer `MIN_REDACTABLE_LEN` gives to the
other value redaction cannot help with.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rayspec.cli.app import app
from rayspec.cli.commands.run import project_slug_for
from rayspec.redact import Redactor
from rayspec.store.file import FileRunStore
from rayspec.store.model import RunRecord, WorkspaceInfo, identity_strings

WORKFLOW = """
rayspec: 1
name: {name}
isolation: none
inputs:
  token: {{ type: string, secret: true, required: true }}
steps:
  - id: echo
    shell: 'printf "%s" "$RAYSPEC_INPUT_TOKEN"'
outputs:
  v: "{{{{ steps.echo.output }}}}"
"""


@pytest.fixture
def cli() -> CliRunner:
    return CliRunner()


def _project(tmp_path: Path, name: str = "deploykey") -> Path:
    root = tmp_path / "proj"
    (root / ".rayspec" / "workflows").mkdir(parents=True, exist_ok=True)
    (root / ".rayspec" / "workflows" / f"{name}.yaml").write_text(
        textwrap.dedent(WORKFLOW.format(name=name))
    )
    return root


def _run(cli: CliRunner, project: Path, name: str, token: str):
    return cli.invoke(app, ["run", name, "--root", str(project), "-i", f"token={token}"])


def _store(home: Path, project: Path) -> FileRunStore:
    return FileRunStore(home / "projects" / project_slug_for(project))


def _files(store: FileRunStore, run_id: str) -> dict[str, str]:
    """The run's stored text, per file — what a reader of the run directory actually sees."""
    directory = store.run_dir(run_id)
    return {
        path.relative_to(directory).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def _addresses(tmp_path: Path, home: Path, project: Path, name: str, run_id: str) -> list[str]:
    """Every address a finished record of this run declares — read off the declarations.

    A probe run with a secret that collides with nothing, then
    :func:`~rayspec.store.model.identity_strings` of what it wrote. So a record that grows a new
    ``redaction_identity`` field turns into a case below the moment it declares one, and nothing
    here has to be kept in step by hand.
    """
    from rayspec.config import Config
    from rayspec.engine.runner import Runner
    from rayspec.loader import load_workflow

    store = FileRunStore(tmp_path / "probe")
    resolved = load_workflow(name, project_root=project, home=home, config=Config())
    result = Runner(
        resolved,
        inputs={"token": "a-value-that-collides-with-nothing"},
        store=store,
        project_root=project,
        run_id=run_id,
    ).run_sync()
    assert result.exit_code == 0, result.reason
    found = sorted(identity_strings(store.load(run_id)))
    assert found, "the record declares no addresses — this guard has stopped working"
    return found


def test_every_address_this_run_is_recorded_under_is_answered_the_same_way(
    tmp_path: Path, home: Path
) -> None:
    """The rule, over every address the RECORD declares — not over a list written out here.

    For each of them, a run whose secret value IS that string must write it identically
    everywhere: no file of the run may show a marker where another shows the value. The run id is
    pinned so that the secret can be made to equal it too — it is minted per run and no caller
    could arrange the collision otherwise.
    """
    from rayspec.config import Config
    from rayspec.engine.runner import Runner
    from rayspec.loader import load_workflow

    name = "deploykey"
    project = _project(tmp_path, name)
    run_id = "20260823-000000-abcd"
    resolved = load_workflow(name, project_root=project, home=home, config=Config())

    for index, address in enumerate(_addresses(tmp_path, home, project, name, run_id)):
        store = FileRunStore(tmp_path / f"store{index}")
        result = Runner(
            resolved,
            inputs={"token": address},
            store=store,
            project_root=project,
            run_id=run_id,
        ).run_sync()
        assert result.exit_code == 0, (address, result.reason)
        stored = _files(store, run_id)
        marked = sorted(rel for rel, text in stored.items() if "[REDACTED:token]" in text)
        clear = sorted(rel for rel, text in stored.items() if address in text)
        assert clear, (address, sorted(stored))
        assert not marked, (address, marked)


def test_the_console_and_the_stored_record_name_the_same_workflow(
    tmp_path: Path, home: Path, cli: CliRunner
) -> None:
    """The reported symptom: `run … started ([REDACTED:token])` on screen, `deploykey` in `show`."""
    name = "deploykey"
    project = _project(tmp_path, name)
    res = _run(cli, project, name, name)  # the secret value IS the workflow name
    assert res.exit_code == 0, res.output
    assert f"started ({name})" in res.output
    assert "[REDACTED:token]" not in res.output

    run_id = _store(home, project).list_run_ids()[0]
    for command in (["show", run_id], ["runs"], ["audit", run_id]):
        out = cli.invoke(app, [*command, "--root", str(project)])
        assert out.exit_code == 0, out.output
        assert name in out.output and "[REDACTED:token]" not in out.output


def test_the_cli_covers_the_same_addresses_on_the_sinks_it_wraps(
    tmp_path: Path, home: Path, cli: CliRunner
) -> None:
    """The engine teaches the STORE its addresses; the CLI has to teach the SINKS too.

    `rayspec run` builds one redactor and hands it to both the store and every wrapped sink
    (`RedactingSink`), and the sink's copy is frozen at that moment — the engine's later word
    reaches the store alone. So the CLI must know the addresses at build time, and this asserts
    it does, over the same declaration-derived list as the engine-level guard above (minus the
    run id, which is minted per run and cannot be arranged from a command line).
    """
    name = "deploykey"
    project = _project(tmp_path, name)
    probe = _run(cli, project, name, "a-value-that-collides-with-nothing")
    assert probe.exit_code == 0, probe.output
    store = _store(home, project)
    record = store.load(store.list_run_ids()[0])
    addresses = sorted(identity_strings(record) - {record.run_id})
    assert addresses, "no address is arrangeable from the CLI — this guard has stopped working"

    for address in addresses:
        res = _run(cli, project, name, address)
        assert res.exit_code == 0, (address, res.output)
        assert "[REDACTED:token]" not in res.output, (address, res.output)


def test_the_run_says_which_secret_it_could_not_redact(
    tmp_path: Path, home: Path, cli: CliRunner
) -> None:
    """Not redacting a declared secret is defensible; doing it silently is not."""
    name = "deploykey"
    project = _project(tmp_path, name)
    res = _run(cli, project, name, name)
    assert res.exit_code == 0, res.output
    assert "warning" in res.output
    assert "token is one of the names this run is recorded under" in res.output
    assert "not redacted" in res.output


def test_a_secret_that_collides_with_nothing_is_still_redacted_everywhere(
    tmp_path: Path, home: Path, cli: CliRunner
) -> None:
    """The other half: the exemption is the collision and nothing else."""
    name = "deploykey"
    project = _project(tmp_path, name)
    secret = "s3cret-value-nobody-shares"
    res = _run(cli, project, name, secret)
    assert res.exit_code == 0, res.output
    assert "one of the names this run is recorded under" not in res.output
    store = _store(home, project)
    run_id = store.list_run_ids()[0]
    stored = _files(store, run_id)
    assert not [rel for rel, text in stored.items() if secret in text], sorted(stored)
    assert [rel for rel, text in stored.items() if "[REDACTED:token]" in text]


def test_an_embedded_run_gets_the_same_answer_without_the_cli(
    tmp_path: Path, home: Path, cli: CliRunner
) -> None:
    """The engine installs the boundary itself for a caller that wired none (`docs/extending.md`).

    It does that BEFORE the record exists — the lock file is written first — so the addresses are
    taught in the one step before the first record is written. Without that, an embedded run
    would still write `[REDACTED:token]` into `events.jsonl` and `deploykey` into `run.json`.
    """
    from rayspec.config import Config
    from rayspec.engine.runner import Runner
    from rayspec.loader import load_workflow

    name = "deploykey"
    project = _project(tmp_path, name)
    store = FileRunStore(tmp_path / "store")
    resolved = load_workflow(name, project_root=project, home=home, config=Config())
    result = Runner(resolved, inputs={"token": name}, store=store, project_root=project).run_sync()
    assert result.exit_code == 0, result.reason
    stored = _files(store, result.run_id)
    assert not [rel for rel, text in stored.items() if "[REDACTED:token]" in text], sorted(stored)
    assert json.loads(stored["run.json"])["workflow_name"] == name


# --------------------------------------------------------------------------------------------------
# unit level
# --------------------------------------------------------------------------------------------------


def test_build_drops_a_colliding_value_and_names_it() -> None:
    redactor = Redactor.build(
        {"token": "deploykey", "other": "s3cretvalue"}, identities=["deploykey"]
    )
    assert redactor.collisions == ("token",)
    assert redactor.redact("deploykey and s3cretvalue") == "deploykey and [REDACTED:other]"


def test_with_identities_undoes_a_literal_learned_before_the_addresses_were_known() -> None:
    """An embedded run builds its redactor first and learns its own addresses afterwards."""
    early = Redactor.build({"token": "deploykey"})
    assert early.redact("deploykey") == "[REDACTED:token]"
    late = early.with_identities(["deploykey"])
    assert late.redact("deploykey") == "deploykey"
    assert late.collisions == ("token",)
    assert late.with_identities(["deploykey"]) is late  # nothing to do twice


def test_a_colliding_value_counts_as_covered_so_the_run_still_starts() -> None:
    """`Runner._install_redactor` refuses to write a byte when a value is uncovered. A value
    rayspec has decided not to redact must not read as a store that dropped the redactor."""
    redactor = Redactor.build({"token": "deploykey"}, identities=["deploykey"])
    assert redactor.covers("deploykey")
    assert redactor.uncovered({"token": "deploykey"}) == ()


def test_extend_applies_the_same_rule_to_a_value_learned_later() -> None:
    """The engine adds the run's own secrets to whatever the caller installed."""
    base = Redactor.build({}, identities=["deploykey"])
    grown = base.extend({"token": "deploykey", "other": "s3cretvalue"})
    assert grown.collisions == ("token",)
    assert grown.redact("deploykey s3cretvalue") == "deploykey [REDACTED:other]"


def test_identity_strings_reads_the_declarations_not_a_list() -> None:
    record = RunRecord(
        run_id="20260823-000000-abcd",
        workflow_name="wf",
        workflow_path=".rayspec/workflows/wf.yaml",
        workflow_hash="0" * 64,
        project_slug="local/x",
        project_root="/tmp/proj",
        workspace=WorkspaceInfo(workdir="/tmp/work"),
    )
    found = identity_strings(record)
    for holder in (record, record.workspace):
        for field in type(holder).redaction_identity:
            value = getattr(holder, field)
            if isinstance(value, str) and value:
                assert value in found, field
    # declared as content on purpose: a secret equal to either is far likelier a leak
    assert record.workflow_hash not in found and record.project_slug not in found
