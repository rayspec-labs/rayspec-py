"""``run.json`` carries who launched the run, and every decision carries who decided."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from rayspec.cli.app import app
from rayspec.store.file import FileRunStore

from .conftest import only_store


@pytest.fixture
def paused(
    cli: CliRunner, project: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, FileRunStore]:
    monkeypatch.setenv("RAYSPEC_ACTOR", "launcher@example.invalid")
    result = cli.invoke(app, ["run", "gate", "--root", str(project), "--no-interactive"])
    assert result.exit_code == 3, result.output
    store = only_store(home)
    (run_id,) = store.list_run_ids()
    return run_id, store


def test_run_json_records_the_launching_actor(paused: tuple[str, FileRunStore]) -> None:
    run_id, store = paused
    run = store.load(run_id)
    assert run.actor is not None
    assert run.actor.id == "launcher@example.invalid"
    assert run.actor.source == "env"


def test_a_decision_records_who_made_it_and_resume_keeps_the_launcher(
    cli: CliRunner,
    project: Path,
    paused: tuple[str, FileRunStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, store = paused
    monkeypatch.setenv("RAYSPEC_ACTOR", "reviewer@example.invalid")
    result = cli.invoke(app, ["approve", run_id, "ship it", "--root", str(project)])
    assert result.exit_code == 0, result.output
    run = store.load(run_id)
    # the run keeps naming whoever started it, even though somebody else resumed it
    assert run.actor is not None and run.actor.id == "launcher@example.invalid"
    decisions = [e for e in store.read_events(run_id) if e.type.value == "run.decision"]
    assert decisions, "the resumed gate must emit a decision event"
    actor = decisions[-1].data.get("actor")
    assert actor is not None and actor["id"] == "reviewer@example.invalid"
    assert decisions[-1].data["by"] == "cli"


def test_a_decision_is_stamped_before_the_gate_consumes_it(
    cli: CliRunner, project: Path, paused: tuple[str, FileRunStore], monkeypatch
) -> None:
    from rayspec.cli.commands.approve import record_decision

    run_id, store = paused
    monkeypatch.setenv("RAYSPEC_ACTOR", "reviewer@example.invalid")
    run = store.load(run_id)
    decision = record_decision(store, run, approved=True, comment="ok")
    assert decision.actor is not None and decision.actor.id == "reviewer@example.invalid"
    reloaded = store.load(run_id)
    assert reloaded.pause is not None and reloaded.pause.decision is not None
    assert reloaded.pause.decision.actor is not None
    assert reloaded.pause.decision.actor.id == "reviewer@example.invalid"


def test_a_terminal_approval_is_attributed_to_the_run_actor(
    cli: CliRunner, project: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RAYSPEC_ACTOR", "launcher@example.invalid")
    result = cli.invoke(app, ["run", "gate", "--root", str(project), "--yes"])
    assert result.exit_code == 0, result.output
    store = only_store(home)
    (run_id,) = store.list_run_ids()
    decisions = [e for e in store.read_events(run_id) if e.type.value == "run.decision"]
    assert decisions and decisions[-1].data["by"] == "--yes"
    actor = decisions[-1].data.get("actor")
    assert actor is not None and actor["id"] == "launcher@example.invalid"


POISON_WORKFLOW = """
rayspec: 1
name: poison
isolation: none
steps:
  - {id: rewrite, shell: 'git config user.email ci-bot@corp.invalid'}
  - {id: ok, needs: [rewrite], approve: "ship it?"}
"""


@pytest.fixture
def poison_project(repo: Path) -> Path:
    """A git project whose first step rewrites the repository's ``user.email``."""
    workflows = repo / ".rayspec" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "poison.yaml").write_text(POISON_WORKFLOW, encoding="utf-8")
    return repo


def test_a_step_cannot_choose_who_approved(
    cli: CliRunner, poison_project: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the run's own worktree shares .git/config with the repository, so a shell step can set
    # user.email; the identity stamped on a later human approval must not come from there
    result = cli.invoke(app, ["run", "poison", "--root", str(poison_project), "--no-interactive"])
    assert result.exit_code == 3, result.output
    from .conftest import git

    assert git("config", "--get", "user.email", cwd=poison_project) == "ci-bot@corp.invalid"
    store = only_store(home)
    (run_id,) = store.list_run_ids()
    result = cli.invoke(app, ["approve", run_id, "LGTM", "--root", str(poison_project)])
    assert result.exit_code == 0, result.output
    decisions = [e for e in store.read_events(run_id) if e.type.value == "run.decision"]
    actor = decisions[-1].data.get("actor")
    assert actor is not None
    assert actor["id"] != "ci-bot@corp.invalid"
    assert actor["source"] != "git"


GLOBAL_POISON_WORKFLOW = """
rayspec: 1
name: poison_global
isolation: none
steps:
  - {id: rewrite, shell: 'git config --global user.email attacker@evil.invalid'}
  - {id: ok, needs: [rewrite], approve: "ship it?"}
"""


@pytest.fixture
def global_poison_project(repo: Path) -> Path:
    """A project whose first step rewrites the *user's own* ``user.email``."""
    workflows = repo / ".rayspec" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "poison_global.yaml").write_text(GLOBAL_POISON_WORKFLOW, encoding="utf-8")
    return repo


def test_a_step_cannot_choose_who_approved_through_the_global_config(
    cli: CliRunner, global_poison_project: Path, home: Path
) -> None:
    # a `shell:` step runs with the user's own HOME — `workspace-write` is a permission mode of
    # the provider, not an OS sandbox — so ~/.gitconfig is as reachable as the repository's
    # config was. The identity on a later human approval must not come from either.
    root = str(global_poison_project)
    result = cli.invoke(app, ["run", "poison_global", "--root", root, "--no-interactive"])
    assert result.exit_code == 3, result.output
    from .conftest import git

    assert git("config", "--global", "--get", "user.email", cwd=global_poison_project) == (
        "attacker@evil.invalid"
    )
    store = only_store(home)
    (run_id,) = store.list_run_ids()
    # the documented default path: a human approves with no RAYSPEC_ACTOR set
    result = cli.invoke(app, ["approve", run_id, "human LGTM", "--root", root])
    assert result.exit_code == 0, result.output
    decisions = [e for e in store.read_events(run_id) if e.type.value == "run.decision"]
    actor = decisions[-1].data.get("actor")
    assert actor is not None
    assert actor["id"] != "attacker@evil.invalid"
    assert actor["source"] != "git"


HOME_ENV_POISON_WORKFLOW = """
rayspec: 1
name: poison_home_env
isolation: none
steps:
  - id: rewrite
    shell: |
      printf 'RAYSPEC_ACTOR=alice@corp.invalid\\n' > "$RAYSPEC_HOME/.env"
  - {id: ok, needs: [rewrite], approve: "ship it?"}
"""


@pytest.fixture
def home_env_poison_project(tmp_path: Path) -> Path:
    """A project whose first step writes an identity into ``$RAYSPEC_HOME/.env``."""
    root = tmp_path / "home-env-poison"
    workflows = root / ".rayspec" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "poison_home_env.yaml").write_text(HOME_ENV_POISON_WORKFLOW, encoding="utf-8")
    return root


def test_a_step_cannot_choose_who_approved_through_the_home_env_file(
    cli: CliRunner, home_env_poison_project: Path, home: Path
) -> None:
    # `$RAYSPEC_HOME` is exported into every step, and `~/.rayspec/.env` is applied to the
    # process environment of every command — so a step can write the variable the approver is
    # about to be identified by. A file the audited run can write is not an identity source.
    root = str(home_env_poison_project)
    result = cli.invoke(app, ["run", "poison_home_env", "--root", root, "--no-interactive"])
    assert result.exit_code == 3, result.output
    assert "RAYSPEC_ACTOR=alice@corp.invalid" in (home / ".env").read_text()
    store = only_store(home)
    (run_id,) = store.list_run_ids()
    # the documented default path: a human approves with no RAYSPEC_ACTOR set
    result = cli.invoke(app, ["approve", run_id, "human LGTM", "--root", root])
    assert result.exit_code == 0, result.output
    decisions = [e for e in store.read_events(run_id) if e.type.value == "run.decision"]
    actor = decisions[-1].data.get("actor")
    assert actor is not None
    assert actor["id"] != "alice@corp.invalid"
    assert actor["source"] == "os"


def test_a_poisoned_home_env_does_not_stamp_the_next_run(
    cli: CliRunner, home_env_poison_project: Path, home: Path, project: Path
) -> None:
    # the poisoning persists on disk, so the header of every LATER run would read the planted
    # identity as if the machine had derived it
    root = str(home_env_poison_project)
    result = cli.invoke(app, ["run", "poison_home_env", "--root", root, "--no-interactive"])
    assert result.exit_code == 3, result.output
    result = cli.invoke(app, ["run", "gate", "--root", str(project), "--no-interactive"])
    assert result.exit_code == 3, result.output
    stores = [FileRunStore(p) for p in (home / "projects").glob("*/*") if (p / "runs").is_dir()]
    runs = [
        store.load(run_id)
        for store in stores
        for run_id in store.list_run_ids()
        if store.load(run_id).workflow_name == "gate"
    ]
    (run,) = runs
    assert run.actor is not None
    assert run.actor.id != "alice@corp.invalid"
    assert run.actor.source == "os"


PROJECT_ENV_POISON_WORKFLOW = """
rayspec: 1
name: poison_project_env
isolation: none
steps:
  - id: rewrite
    shell: |
      printf 'RAYSPEC_ACTOR=security-team@corp.invalid\\n' > .rayspec/.env
  - {id: ok, needs: [rewrite], approve: "ship it?"}
"""


@pytest.fixture
def project_env_poison_project(tmp_path: Path) -> Path:
    """A project whose first step writes an identity into its own ``.rayspec/.env``."""
    root = tmp_path / "project-env-poison"
    workflows = root / ".rayspec" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "poison_project_env.yaml").write_text(
        PROJECT_ENV_POISON_WORKFLOW, encoding="utf-8"
    )
    return root


def test_a_step_cannot_choose_who_approved_through_the_project_env_file(
    cli: CliRunner, project_env_poison_project: Path, home: Path
) -> None:
    # `approve`/`reject`/`resume`/`run` opt the checkout's `.rayspec/.env` in by name, and the
    # checkout is exactly what the run has write access to
    root = str(project_env_poison_project)
    result = cli.invoke(app, ["run", "poison_project_env", "--root", root, "--no-interactive"])
    assert result.exit_code == 3, result.output
    planted = (project_env_poison_project / ".rayspec" / ".env").read_text()
    assert "RAYSPEC_ACTOR=security-team@corp.invalid" in planted
    store = only_store(home)
    (run_id,) = store.list_run_ids()
    result = cli.invoke(app, ["approve", run_id, "human LGTM", "--root", root])
    assert result.exit_code == 0, result.output
    decisions = [e for e in store.read_events(run_id) if e.type.value == "run.decision"]
    actor = decisions[-1].data.get("actor")
    assert actor is not None
    assert actor["id"] != "security-team@corp.invalid"
    assert actor["source"] == "os"


BOTH_POISON_WORKFLOW = """
rayspec: 1
name: poison_all
isolation: none
steps:
  - id: rewrite
    shell: |
      printf 'RAYSPEC_ACTOR=alice@corp.invalid\\n' > "$RAYSPEC_HOME/.env"
      printf 'RAYSPEC_ACTOR=security-team@corp.invalid\\n' > .rayspec/.env
      git config user.email ci-bot@corp.invalid
      git config --global user.email attacker@evil.invalid
  - {id: ok, needs: [rewrite], approve: "ship it?"}
"""


@pytest.fixture
def all_poison_project(repo: Path) -> Path:
    """A project whose first step writes every identity source it can reach."""
    workflows = repo / ".rayspec" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "poison_all.yaml").write_text(BOTH_POISON_WORKFLOW, encoding="utf-8")
    return repo


def test_no_file_a_step_can_write_names_the_approver(
    cli: CliRunner, all_poison_project: Path, home: Path
) -> None:
    root = str(all_poison_project)
    result = cli.invoke(app, ["run", "poison_all", "--root", root, "--no-interactive"])
    assert result.exit_code == 3, result.output
    store = only_store(home)
    (run_id,) = store.list_run_ids()
    result = cli.invoke(app, ["approve", run_id, "human LGTM", "--root", root])
    assert result.exit_code == 0, result.output
    decisions = [e for e in store.read_events(run_id) if e.type.value == "run.decision"]
    actor = decisions[-1].data.get("actor")
    assert actor is not None
    planted = {
        "alice@corp.invalid",
        "security-team@corp.invalid",
        "ci-bot@corp.invalid",
        "attacker@evil.invalid",
    }
    assert actor["id"] not in planted
    assert actor["source"] == "os"


LEGITIMATE_ENV_WORKFLOW = """
rayspec: 1
name: uses_env
isolation: none
steps:
  - {id: a, shell: 'printf %s "$MY_PROJECT_SETTING/$MY_HOME_SETTING"'}
  - {id: ok, needs: [a], approve: "ship it?"}
"""


def test_env_files_still_supply_ordinary_configuration(
    cli: CliRunner, tmp_path: Path, home: Path
) -> None:
    # the rule is about identity only: a project's `.env` is how a project supplies its own
    # configuration, and narrowing the rule must not take that away
    root = tmp_path / "uses-env"
    workflows = root / ".rayspec" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "uses_env.yaml").write_text(LEGITIMATE_ENV_WORKFLOW, encoding="utf-8")
    (root / ".rayspec" / ".env").write_text("MY_PROJECT_SETTING=from-project\n", encoding="utf-8")
    (home / ".env").write_text("MY_HOME_SETTING=from-home\n", encoding="utf-8")
    result = cli.invoke(app, ["run", "uses_env", "--root", str(root), "--no-interactive"])
    assert result.exit_code == 3, result.output
    store = only_store(home)
    (run_id,) = store.list_run_ids()
    step = store.load(run_id).steps["a"]
    assert step.output_ref is not None
    assert store.read_output(run_id, step.output_ref) == "from-project/from-home"


def test_a_refused_env_identity_is_said_out_loud_and_kept_as_a_claim(
    cli: CliRunner, project_env_poison_project: Path, home: Path
) -> None:
    # refusing quietly would leave both readers wrong: the person who set RAYSPEC_ACTOR in
    # .rayspec/.env on purpose, and the person who needs to see that something set it
    root = str(project_env_poison_project)
    result = cli.invoke(app, ["run", "poison_project_env", "--root", root, "--no-interactive"])
    assert result.exit_code == 3, result.output
    store = only_store(home)
    (run_id,) = store.list_run_ids()
    result = cli.invoke(app, ["approve", run_id, "human LGTM", "--root", root])
    assert result.exit_code == 0, result.output
    assert "RAYSPEC_ACTOR" in result.stderr
    assert "is not used as an identity" in result.stderr
    assert ".rayspec/.env" in result.stderr
    decisions = [e for e in store.read_events(run_id) if e.type.value == "run.decision"]
    actor = decisions[-1].data.get("actor")
    assert actor is not None
    assert actor["declared_id"] == "security-team@corp.invalid"
    # and the ledger a person reads shows the refusal next to the identity that was used
    result = cli.invoke(app, ["audit", run_id, "--root", root])
    assert result.exit_code == 0, result.output
    assert "security-team@corp.invalid" in result.output
    assert "not an identity" in result.output
