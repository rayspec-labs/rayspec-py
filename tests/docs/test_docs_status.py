"""The user-facing docs describe the build that ships.

Boundary: plain-text assertions over ``README.md``, ``docs/*.md`` and ``examples/README.md`` —
no CLI calls. They pin the wording drifts found while preparing v1.0.0 so they cannot creep
back: shipped commands listed as roadmap, "Pre-alpha", a single install form, the stale
resume clause, "silently ignored" cross-provider tool names, and the missing secrets warning.
"""

from __future__ import annotations

import re
from pathlib import Path

from typer.main import get_command

from rayspec.skill import SKILLS

from .conftest import DOCS_DIR, README, REPO_ROOT

EXAMPLES_README = REPO_ROOT / "examples" / "README.md"
SKILL_SRC = REPO_ROOT / "src" / "rayspec" / "skill"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_readme_status_does_not_list_shipped_features_as_roadmap() -> None:
    """doctor, init and the Rich live console ship; the README must not call them planned.

    Since the 1.0.0 tag the Status section must also read *released*, not *candidate*.
    """
    text = _text(README)
    status = text.split("## Status", 1)[1].split("\n## ", 1)[0]
    assert "Pre-alpha" not in text
    assert "Released" in status
    assert "candidate" not in status, "1.0.0 is tagged — Status must not call the build a candidate"
    for stale in ("On the roadmap: `doctor`", "not yet wired", "`doctor`, `init`"):
        assert stale not in status, stale
    assert not re.search(r"\bv?\d+\.\d+\.\d+\b", status), (
        "the Status section must not pin a version number — it drifts on every release; "
        "`rayspec version` and CHANGELOG.md are the sources of truth"
    )


def test_readme_status_points_only_at_files_that_exist() -> None:
    """Every `*.md` the README names must actually be in the repo."""
    text = _text(README)
    for ref in re.findall(r"`([\w./-]+\.md)`", text):
        assert (REPO_ROOT / ref).is_file(), f"README refers to `{ref}` which is not in the repo"
    for ref in re.findall(r"\]\(([\w./-]+\.md)\)", text):
        assert (REPO_ROOT / ref).is_file(), f"README links `{ref}` which is not in the repo"


def test_extending_md_describes_quiet_as_a_separate_sink() -> None:
    """`--quiet` swaps ConsoleSink for the problems-only line sink; it does not degrade ConsoleSink."""
    extending = _text(DOCS_DIR / "extending.md")
    assert "terminal or with `--quiet`" not in extending
    assert "problems-only" in extending


def test_providers_and_examples_readme_do_not_list_doctor_as_roadmap() -> None:
    """providers.md 'Auth and health'/'Roadmap' and examples/README.md 'Not yet covered'."""
    providers = _text(DOCS_DIR / "providers.md")
    assert "is on the [roadmap]" not in providers
    assert "- `rayspec doctor [--probe]` (healthchecks" not in providers
    examples = _text(EXAMPLES_README)
    assert "Not yet covered" not in examples
    assert "`rayspec init` and `rayspec doctor`." not in examples
    assert "still prints the quiet" not in examples
    extending = _text(DOCS_DIR / "extending.md")
    assert "wiring the tree is on the README" not in extending


def test_readme_status_lists_exactly_the_commands_that_exist() -> None:
    """The Status section's command list must match `rayspec --help`, both ways.

    It drifted once: seven shipped commands were missing and two shipped features were still
    described as roadmap, on the page a stranger reads first. Nothing caught it, because every
    other guard here checks for known-stale *phrases* rather than comparing against the CLI.
    """
    from rayspec.cli.app import app

    status = _text(README).split("## Status", 1)[1].split("\n## ", 1)[0]
    listed = set(re.findall(r"`([a-z][a-z-]*)`", status.split("Commands:", 1)[1]))
    listed.discard("rayspec")
    real = {
        command.name
        for command in get_command(app).commands.values()  # type: ignore[attr-defined]
        if command.name and not command.hidden
    }
    assert listed == real, (
        f"README Status command list is out of date — "
        f"missing: {sorted(real - listed)}, not real commands: {sorted(listed - real)}"
    )


def test_readme_install_spells_out_both_git_forms() -> None:
    """https and ssh, the uvx one-off and a local path — every way in is on the README."""
    text = _text(README)
    assert "uv tool install git+https://github.com/rayspec-labs/rayspec-py" in text
    assert "uv tool install git+ssh://git@github.com/rayspec-labs/rayspec-py" in text
    assert "uvx --from git+https://github.com/rayspec-labs/rayspec-py rayspec version" in text
    assert "uv tool install <path-to-checkout>" in text or "uv tool install ." in text


def test_cli_md_resume_note_has_no_stale_cross_reference() -> None:
    """`rayspec run --json` prints the summary last too; the stale 'unlike …' clause is gone."""
    text = _text(DOCS_DIR / "cli.md")
    assert "issues/25" not in text
    assert "unlike `rayspec run --json`" not in text


def test_cross_provider_raw_tool_names_are_documented_as_warned_not_silent() -> None:
    """Raw names addressed to another provider are ignored *with a warning*."""
    for name in ("schema.md", "providers.md"):
        text = _text(DOCS_DIR / name)
        assert "silently ignored by other providers" not in text, name
        assert "ignored silently, so one agent file" not in text, name
        assert "with a warning" in text, name


def test_secrets_warning_is_present_where_inputs_and_the_run_store_are_introduced() -> None:
    """Inputs, outputs and transcripts persist in clear text; ``schema.md`` documents
    ``secret: true`` (the way out) and both pages point at where the remaining gap lives.

    Now that the secret sources, the redactor and the tools-not-prompts pattern have shipped,
    the right pointer is the roadmap (rayspec holding credentials itself, redaction that
    survives a transformation). The clear-text warning and the ``secret: true`` answer must
    both stay put either way.
    """
    ahead = ("roadmap",)
    schema = _text(DOCS_DIR / "schema.md")
    assert "Secret inputs" in schema and "secret: true" in schema
    assert "clear text" in schema
    assert any(ref in schema for ref in ahead), "schema.md must say where the remaining gap lives"
    concepts = _text(DOCS_DIR / "concepts.md")
    assert "Secrets" in concepts
    assert "clear text" in concepts
    assert "secret: true" in concepts, "concepts.md must name the field, not call it roadmap"
    assert any(ref in concepts for ref in ahead), "concepts.md must say where the gap lives"


def test_the_shell_slot_pages_do_not_promise_an_export_above_the_spill_threshold() -> None:
    """A slot is exported only up to 64 KiB; a spilled one is a plain shell variable.

    Three places state the slot rule before a reader ever reaches the paragraph that explains
    spilling — the packaged skill's one-line summary, the opening of the "Shell bodies" section
    and the environment table, which is read out of order. Each used to say the value is in the
    step's environment full stop. A workflow author who believes that hands a slot to a child
    process through the environment; it works for every value they test with and fails only
    above the threshold. The behaviour itself is pinned in
    ``tests/properties/test_templating_slots.py``; this pins the pages that describe it.
    """
    # Whichever packaged skill states the rule must qualify it. Derived from the registry rather
    # than from a path, and the count is asserted: if the heading is ever renamed this fails loudly
    # instead of passing over an empty list.
    stating = [
        (skill.name, text)
        for skill in SKILLS
        if "**Shell env-ref rule**" in (text := _text(SKILL_SRC / skill.name / "SKILL.md"))
    ]
    assert len(stating) == 1, f"exactly one skill should state the slot rule, got {stating!r}"
    for name, text in stating:
        rule = text.split("**Shell env-ref rule**", 1)[1].split("\n- ", 1)[0]
        assert "64 KiB" in rule, (
            f"{name}: the slot rule must name the threshold the export stops at"
        )
        assert "the value exported" not in rule, f"{name}: the export is not unconditional"
        assert "$(cat" not in rule, f"{name}: a spilled slot no longer renders as a substitution"

    templating = _text(DOCS_DIR / "templating.md")
    opening = templating.split("## Shell bodies", 1)[1].split("\n\n", 1)[1]
    assert "64 KiB" in opening, "the Shell bodies opening must qualify where the value lives"
    assert "the value is placed in the step's environment, never spliced" not in templating

    table_row = next(
        line for line in templating.splitlines() if line.startswith("| `RAYSPEC_V<n>` |")
    )
    assert "64 KiB" in table_row, "the environment table must not list a spilled slot unqualified"


def test_templating_md_states_both_halves_of_the_nul_failure() -> None:
    """NUL fails loudly below the threshold and silently above it — the doc must say both.

    "cannot reach a step at all" reads as "rayspec refuses it". Below the threshold that is
    right: the step never starts. Above it the step succeeds with the NUL removed and every
    other byte intact, which is the half a reader has to act on, and the half the sentence
    denied. Both are pinned as behaviour in ``tests/properties/test_templating_slots.py``.
    """
    templating = _text(DOCS_DIR / "templating.md")
    assert "cannot reach a step at all" not in templating
    bullet = next(para for para in templating.split("\n- ") if para.startswith("A NUL byte"))
    assert "embedded null byte" in bullet, "the loud half: the step never starts below 64 KiB"
    assert "one byte short" in bullet, "the silent half: above it the step runs on without it"


def test_cli_md_describes_where_an_oversize_value_is_elided_in_a_preview() -> None:
    """The slot of an oversize value keeps its reference; the placeholder stands for the PATH.

    While a spilled value was spliced into the body as ``$(cat '<path>')`` the placeholder did
    stand where the slot was, so "its slot reads <N bytes …>" was accurate. It no longer is:
    the slot reads ``${RAYSPEC_V<n>}`` and the elided path sits in the preamble line above the
    body. What matters — no scratch path in a preview — is unchanged and pinned in
    ``tests/engine/test_context_rebuild.py``.
    """
    cli = _text(DOCS_DIR / "cli.md")
    assert "its slot reads `<N bytes" not in cli
    assert "too large to inline here" in cli
