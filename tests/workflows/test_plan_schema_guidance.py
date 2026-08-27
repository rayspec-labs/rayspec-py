# SPDX-License-Identifier: Apache-2.0
"""P1 regression (found dogfooding prd_to_pr on PRD-08): the ``plan`` step must guide the model
to fill ``test_plan``/``unresolved`` rather than cram the whole plan into ``summary``.

On a rich PRD the planner otherwise writes a multi-thousand-character prose essay into ``summary``
and leaves ``test_plan``/``unresolved`` empty. Both are ``required``, so ``StructuredOutput``
rejects it — the run then either degenerates to a placeholder plan (``summary: "test"``) that a
human catches at the gate, or fails after the retry limit ("Failed to provide valid structured
output after 5 attempts"). Field descriptions (surfaced to the model by StructuredOutput) plus a
prompt that names the three fields and forbids putting tests in ``summary`` are the fix.
"""

from __future__ import annotations

import yaml

from rayspec.loader.bundled import bundled_dir


def _plan_step() -> dict:
    text = (bundled_dir() / "prd_to_pr.yaml").read_text(encoding="utf-8")
    workflow = yaml.safe_load(text)
    for step in workflow["steps"]:
        if step.get("id") == "plan":
            return step
    raise AssertionError("prd_to_pr has no 'plan' step")


def test_plan_schema_fields_carry_guiding_descriptions() -> None:
    props = _plan_step()["output_schema"]["properties"]
    # every field the planner must populate names what belongs in it
    for field in ("summary", "test_plan", "unresolved"):
        assert props[field].get("description", "").strip(), (
            f"plan.output_schema.{field} needs a description telling the model what goes there"
        )
    # summary's description steers content toward test_plan and away from the essay-in-summary
    # failure mode the dogfood hit
    assert "test_plan" in props["summary"]["description"]
    # test_plan is named as the home of the per-requirement tests
    assert "test" in props["test_plan"]["description"].lower()
    # the per-requirement item fields are described too, so tests land as a list of test ideas
    item = props["test_plan"]["items"]["properties"]
    assert item["tests"].get("description", "").strip()
    assert item["requirement"].get("description", "").strip()


def test_plan_prompt_names_the_three_fields_and_forbids_tests_in_summary() -> None:
    prompt = _plan_step()["prompt"]
    for field in ("summary", "test_plan", "unresolved"):
        assert field in prompt, f"the plan prompt should name the {field!r} field explicitly"
    # the prompt tells the model to keep summary short and put the tests in test_plan
    low = prompt.lower()
    assert "test_plan" in prompt and "short" in low
