# SPDX-License-Identifier: Apache-2.0
"""`rayspec schema [kind] [--out DIR]` — print or write the published JSON Schemas."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rayspec.cli.app import app
from rayspec.schemagen import SCHEMA_KINDS, schema_id, schema_text

runner = CliRunner()


@pytest.mark.parametrize("kind", SCHEMA_KINDS)
def test_printing_one_schema_is_the_generated_document(kind: str) -> None:
    res = runner.invoke(app, ["schema", kind])
    assert res.exit_code == 0, res.output
    assert json.loads(res.output) == json.loads(schema_text(kind))


def test_bare_invocation_lists_every_kind_with_its_id() -> None:
    res = runner.invoke(app, ["schema"])
    assert res.exit_code == 0, res.output
    for kind in SCHEMA_KINDS:
        assert kind in res.output
        assert schema_id(kind) in res.output


def test_unknown_kind_exits_2_with_a_did_you_mean() -> None:
    res = runner.invoke(app, ["schema", "workflows"])
    assert res.exit_code == 2
    assert "unknown schema" in res.output
    assert "workflow" in res.output


def test_out_writes_every_schema(tmp_path: Path) -> None:
    res = runner.invoke(app, ["schema", "--out", str(tmp_path)])
    assert res.exit_code == 0, res.output
    for kind in SCHEMA_KINDS:
        assert (tmp_path / f"{kind}.schema.json").read_text(encoding="utf-8") == schema_text(kind)
    assert str(tmp_path) in res.output


def test_out_with_a_kind_writes_only_that_schema(tmp_path: Path) -> None:
    res = runner.invoke(app, ["schema", "workflow", "--out", str(tmp_path)])
    assert res.exit_code == 0, res.output
    assert sorted(p.name for p in tmp_path.iterdir()) == ["workflow.schema.json"]


def test_out_is_created_when_missing_and_reports_the_modeline(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "dir"
    res = runner.invoke(app, ["schema", "workflow", "--out", str(target)])
    assert res.exit_code == 0, res.output
    assert (target / "workflow.schema.json").is_file()
    assert "yaml-language-server: $schema=" in res.output


def test_out_that_is_a_file_exits_2(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    res = runner.invoke(app, ["schema", "--out", str(blocker)])
    assert res.exit_code == 2
