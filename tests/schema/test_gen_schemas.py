# SPDX-License-Identifier: Apache-2.0
"""The checked-in JSON Schemas are generated, current, and describe the real files."""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
from jsonschema import Draft202012Validator

from rayspec.loader.yaml import load_yaml
from rayspec.schemagen import (
    SCHEMA_BASE_URL,
    SCHEMA_KINDS,
    build_schema,
    modeline,
    schema_id,
    schema_text,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMAS_DIR = REPO_ROOT / "schemas"
SCRIPT = REPO_ROOT / "scripts" / "gen_schemas.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gen_schemas", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _workflow_files() -> Iterator[Path]:
    """Every workflow document this repo ships — including the `rayspec init` scaffolds, which
    carry the modeline and must therefore validate against the schema it points at."""
    yield from sorted((REPO_ROOT / "src/rayspec/cli/templates").glob("*/workflows/*.yaml"))
    yield from sorted((REPO_ROOT / "examples").glob("*/.rayspec/workflows/*.yaml"))
    yield from sorted((REPO_ROOT / ".rayspec" / "workflows").glob("*.yaml"))


def test_check_is_a_no_op_on_a_clean_tree() -> None:
    script = _load_script()
    assert script.main(["--check"]) == 0


def test_check_fails_when_a_schema_is_stale(tmp_path: Path) -> None:
    script = _load_script()
    for kind in SCHEMA_KINDS:
        (tmp_path / f"{kind}.schema.json").write_text("{}\n", encoding="utf-8")
    assert script.main(["--check", "--out", str(tmp_path)]) == 1


def test_writing_into_a_fresh_directory_reproduces_the_checked_in_files(tmp_path: Path) -> None:
    script = _load_script()
    assert script.main(["--out", str(tmp_path)]) == 0
    for kind in SCHEMA_KINDS:
        written = (tmp_path / f"{kind}.schema.json").read_text(encoding="utf-8")
        assert written == (SCHEMAS_DIR / f"{kind}.schema.json").read_text(encoding="utf-8")


@pytest.mark.parametrize("kind", SCHEMA_KINDS)
def test_every_id_derives_from_the_single_base_url(kind: str) -> None:
    schema = json.loads((SCHEMAS_DIR / f"{kind}.schema.json").read_text(encoding="utf-8"))
    assert schema["$id"] == schema_id(kind) == f"{SCHEMA_BASE_URL}{kind}.schema.json"
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    Draft202012Validator.check_schema(schema)


def test_the_schema_url_resolves_to_the_checked_in_file() -> None:
    """The modeline URL a scaffold carries names a file that exists in this repo (no network)."""
    url = modeline().split("$schema=", 1)[1].strip()
    assert url == schema_id("workflow")
    assert url.startswith("https://")
    assert (SCHEMAS_DIR / url.rsplit("/", 1)[1]).is_file()


def test_a_model_change_makes_the_generated_schema_differ() -> None:
    """The generator reads the live models: touching one changes the output (that is the drift
    the ``--check`` gate catches)."""
    from rayspec.store.model import RunRecord

    before = schema_text("run")
    original = dict(RunRecord.model_fields)
    try:
        del RunRecord.model_fields["dry_run"]
        RunRecord.model_rebuild(force=True)
        assert schema_text("run") != before
    finally:
        RunRecord.model_fields.clear()
        RunRecord.model_fields.update(original)
        RunRecord.model_rebuild(force=True)
    assert schema_text("run") == before


@pytest.mark.parametrize(
    "path", list(_workflow_files()), ids=lambda p: f"{p.parent.parent.name}-{p.name}"
)
def test_every_packaged_workflow_validates_against_the_workflow_schema(path: Path) -> None:
    validator = Draft202012Validator(build_schema("workflow"))
    data = load_yaml(path.read_text(encoding="utf-8"), source=str(path))
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
    assert not errors, [f"{list(e.path)}: {e.message}" for e in errors[:5]]


def test_the_workflow_schema_still_rejects_real_mistakes() -> None:
    validator = Draft202012Validator(build_schema("workflow"))
    bad = {"rayspec": 2, "name": "x", "steps": [{"id": "a", "shell": "hi", "timeoutt": "5m"}]}
    assert list(validator.iter_errors(bad))


def test_durations_money_and_token_caps_accept_their_string_spellings() -> None:
    validator = Draft202012Validator(build_schema("workflow"))
    doc = {
        "rayspec": 1,
        "name": "x",
        "defaults": {"timeout": "30m", "budget_usd": "$1.50", "max_tokens": "500k"},
        "steps": [
            {"id": "a", "shell": "hi", "timeout": 30, "retry": {"attempts": 2, "delay": "3s"}}
        ],
    }
    assert not list(validator.iter_errors(doc))
    bad = {"rayspec": 1, "name": "x", "steps": [{"id": "a", "shell": "hi", "timeout": "soon"}]}
    assert list(validator.iter_errors(bad))


def test_run_events_and_stream_schemas_describe_the_real_records() -> None:
    from rayspec.events.model import EventType, RunEvent, StreamRecord
    from rayspec.store.model import RunRecord

    run = RunRecord(
        run_id="20260821-101010-abcd",
        workflow_name="wf",
        workflow_path="/tmp/wf.yaml",
        workflow_hash="deadbeef",
        project_slug="local/x",
        project_root="/tmp",
    )
    Draft202012Validator(build_schema("run")).validate(json.loads(run.model_dump_json()))
    event = RunEvent(type=EventType.RUN_STARTED, run_id=run.run_id)
    Draft202012Validator(build_schema("events")).validate(json.loads(event.to_json()))
    record = StreamRecord(kind="stdout", text="hi")
    Draft202012Validator(build_schema("stream")).validate(json.loads(record.to_json()))


def _modeline_files() -> Iterator[Path]:
    yield from _workflow_files()


@pytest.mark.parametrize(
    "path", list(_modeline_files()), ids=lambda p: f"{p.parent.parent.name}-{p.name}"
)
def test_every_scaffold_and_example_workflow_opens_with_the_modeline(path: Path) -> None:
    first = path.read_text(encoding="utf-8").splitlines()[0]
    assert first == modeline(), path


def test_check_reports_a_workflow_whose_modeline_is_missing(tmp_path: Path) -> None:
    script = _load_script()
    workflow = tmp_path / ".rayspec" / "workflows" / "wf.yaml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("rayspec: 1\nname: wf\nsteps: []\n", encoding="utf-8")
    assert script.stale_modelines([workflow]) == [workflow]
    assert script.sync_modelines([workflow]) == [workflow]
    assert workflow.read_text(encoding="utf-8").splitlines()[0] == modeline()
    assert script.stale_modelines([workflow]) == []
    assert script.sync_modelines([workflow]) == []


def test_an_outdated_modeline_is_replaced_not_duplicated(tmp_path: Path) -> None:
    script = _load_script()
    workflow = tmp_path / "wf.yaml"
    workflow.write_text(
        "# yaml-language-server: $schema=https://example.invalid/old.json\nrayspec: 1\n",
        encoding="utf-8",
    )
    assert script.sync_modelines([workflow]) == [workflow]
    lines = workflow.read_text(encoding="utf-8").splitlines()
    assert lines == [modeline(), "rayspec: 1"]


def _step(kind: str, extra: dict[str, object]) -> dict[str, object]:
    body: dict[str, object] = {"id": "a", **extra}
    if kind == "prompt":
        body["prompt"] = "hi"
    else:
        body[kind] = "echo hi" if kind == "shell" else "x = 1"
    return body


DIFFERENTIAL: list[tuple[str, dict[str, object]]] = [
    ("plain", {}),
    ("env-string", {"env": {"NAME": "value"}}),
    ("env-int", {"env": {"PORT": 8080}}),
    ("env-bool", {"env": {"DEBUG": True}}),
    ("env-float", {"env": {"RATIO": 1.5}}),
    ("env-template", {"env": {"REF": "{{ inputs.x }}"}}),
]


@pytest.mark.parametrize("kind", ["prompt", "shell", "python"])
@pytest.mark.parametrize("case,extra", DIFFERENTIAL, ids=[c for c, _ in DIFFERENTIAL])
def test_the_schema_accepts_exactly_what_the_models_accept(
    kind: str, case: str, extra: dict[str, object]
) -> None:
    """The published schema is an editor aid — it must not red-line a legal document."""
    from rayspec.schema import SchemaError, parse_workflow

    doc = {"rayspec": 1, "name": "x", "steps": [_step(kind, extra)]}
    try:
        parse_workflow(doc, source="wf.yaml")
    except SchemaError:
        model_ok = False
    else:
        model_ok = True
    schema_ok = not list(Draft202012Validator(build_schema("workflow")).iter_errors(doc))
    assert model_ok == schema_ok, f"{kind}/{case}: model={model_ok} schema={schema_ok}"


def test_an_unresolved_out_directory_is_still_the_default_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--out schemas` / `--out ./schemas/` must check the modelines like the bare invocation."""
    script = _load_script()
    assert script.main(["--check"]) == 0
    default = capsys.readouterr().out
    assert script.main(["--check", "--out", str(REPO_ROOT / "src" / ".." / "schemas")]) == 0
    assert capsys.readouterr().out == default
    assert "0 modeline(s)" not in default
