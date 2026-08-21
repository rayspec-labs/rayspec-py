"""The user-facing docs describe the build that ships.

Boundary: plain-text assertions over ``README.md``, ``docs/*.md`` and ``examples/README.md`` —
no CLI calls. They pin the wording drifts found while preparing v1.0.0 so they cannot creep
back: shipped commands listed as roadmap, "Pre-alpha", a single install form, the stale
resume clause, "silently ignored" cross-provider tool names, and the missing secrets warning.
"""

from __future__ import annotations

import re
from pathlib import Path

from .conftest import DOCS_DIR, README, REPO_ROOT

EXAMPLES_README = REPO_ROOT / "examples" / "README.md"


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
