"""The files a stranger reads before trusting the project — and before reporting a bug in it.

Boundary: plain-text, TOML and YAML assertions over ``SECURITY.md``, ``CONTRIBUTING.md``,
``CODE_OF_CONDUCT.md``, the ``.github/`` templates and the badge row of ``README.md`` — no CLI
calls, no network. They pin the claims that rot silently: the gate command CONTRIBUTING promises
(against the one CI actually runs), the disclosure window and the private channel SECURITY names,
the secret contract it repeats from ``docs/schema.md``, the supported Python range in the badges
(against ``pyproject.toml``), and the fact that no template placeholder survived the copy-paste.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

import yaml

from .conftest import DOCS_DIR, README, REPO_ROOT
from .test_links import anchors_of, links_of

SECURITY = REPO_ROOT / "SECURITY.md"
CONTRIBUTING = REPO_ROOT / "CONTRIBUTING.md"
CODE_OF_CONDUCT = REPO_ROOT / "CODE_OF_CONDUCT.md"
GITHUB_DIR = REPO_ROOT / ".github"
CODEOWNERS = GITHUB_DIR / "CODEOWNERS"
PR_TEMPLATE = GITHUB_DIR / "PULL_REQUEST_TEMPLATE.md"
BUG_REPORT = GITHUB_DIR / "ISSUE_TEMPLATE" / "bug_report.yml"
FEATURE_REQUEST = GITHUB_DIR / "ISSUE_TEMPLATE" / "feature_request.yml"
ISSUE_CONFIG = GITHUB_DIR / "ISSUE_TEMPLATE" / "config.yml"
CI_WORKFLOW = GITHUB_DIR / "workflows" / "ci.yml"

# GitHub only picks these up under exactly these names, in exactly these places.
COMMUNITY_FILES = [
    SECURITY,
    CONTRIBUTING,
    CODE_OF_CONDUCT,
    CODEOWNERS,
    PR_TEMPLATE,
    BUG_REPORT,
    FEATURE_REQUEST,
    ISSUE_CONFIG,
]

ADVISORY_URL = "https://github.com/rayspec-labs/rayspec-py/security/advisories/new"

# Left behind when a template is filled in carelessly. Case-sensitive on purpose: GitHub issue
# forms use a lowercase `placeholder:` key, which is not one of these.
_PLACEHOLDER_RE = re.compile(r"\bTODO\b|\bTBD\b|\bFIXME\b|PLACEHOLDER|\[INSERT|<your-|XXX")
_FENCE_RE = re.compile(r"```bash\n(.*?)```", re.DOTALL)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _flat(text: str) -> str:
    """One line, single spaces — so a pinned sentence survives re-wrapping the paragraph."""
    return " ".join(text.split())


def _normalise(command: str) -> str:
    """Compare shell commands ignoring quote style and pytest's (redundant) ``-q``."""
    return " ".join(command.replace('"', "'").replace(" -q ", " ").split())


def _bash_blocks(text: str) -> list[str]:
    return _FENCE_RE.findall(text)


def _gate_command(text: str) -> str:
    """The one chained command line CONTRIBUTING.md presents as *the* gate."""
    for block in _bash_blocks(text):
        for line in block.splitlines():
            if line.startswith("uv run ruff check"):
                return line.strip()
    return ""


def _ci_check_commands() -> list[str]:
    """Every check the CI ``test`` job runs (``uv sync``/``uv python install`` are setup)."""
    workflow: Any = yaml.safe_load(_text(CI_WORKFLOW))
    steps = workflow["jobs"]["test"]["steps"]
    return [
        step["run"]
        for step in steps
        if "run" in step
        and step["run"].startswith("uv run")
        and not step["run"].startswith(("uv sync", "uv python"))
    ]


def test_every_community_health_file_is_where_github_looks_for_it() -> None:
    missing = [str(p.relative_to(REPO_ROOT)) for p in COMMUNITY_FILES if not p.is_file()]
    assert not missing, f"GitHub will not find: {missing}"


def test_no_placeholder_survived_the_copy_paste() -> None:
    problems = [
        f"{path.relative_to(REPO_ROOT)}: {match.group(0)!r}"
        for path in COMMUNITY_FILES
        if path.is_file()
        for match in [_PLACEHOLDER_RE.search(_text(path))]
        if match
    ]
    assert not problems, "\n".join(problems)


def test_relative_links_in_the_community_files_resolve() -> None:
    problems: list[str] = []
    for md in (SECURITY, CONTRIBUTING, CODE_OF_CONDUCT, PR_TEMPLATE):
        for target in links_of(md):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            file_part, _, anchor = target.partition("#")
            dest = md if not file_part else (md.parent / file_part).resolve()
            if not dest.exists():
                problems.append(f"{md.name}: broken link {target!r} (missing {dest})")
            elif anchor and dest.suffix == ".md" and anchor not in anchors_of(dest):
                problems.append(f"{md.name}: no heading for anchor {target!r} in {dest.name}")
    assert not problems, "\n".join(problems)


def test_security_names_the_private_channel_and_the_disclosure_window() -> None:
    """A researcher must find the channel and the deadline without reading past the fold."""
    text = _text(SECURITY)
    assert "private vulnerability reporting" in text.lower()
    assert ADVISORY_URL in text, "SECURITY.md must link the private reporting form directly"
    assert "90 days" in text, "the coordinated-disclosure window must be stated in days"
    assert "public issue" in text, "SECURITY.md must say not to use the public tracker"


def test_security_states_the_supported_version_line() -> None:
    """Honest support promise: the 1.x line, not 'the latest commit'."""
    assert "1.x" in _text(SECURITY)


def test_security_states_the_two_halves_of_the_threat_model() -> None:
    """The unusual part first (declared execution is the product), then the real surface."""
    flat = _flat(_text(SECURITY))
    by_design = (
        "A malicious workflow file is not a vulnerability in rayspec, for the same reason a "
        "malicious shell script is not a vulnerability in your shell."
    )
    leak = (
        "Any path by which a `secret: true` input reaches a prompt, a template, an expression, "
        "an output, an event, the console or the run store **is** a vulnerability."
    )
    assert by_design in flat, "SECURITY.md must say that declared execution is by design"
    assert leak in flat, "SECURITY.md must say that a secret leak is in scope"


def test_security_repeats_the_secret_contract_as_the_schema_docs_state_it() -> None:
    """If the contract in ``docs/schema.md`` moves, the security page must move with it."""
    security = _text(SECURITY)
    schema = _text(DOCS_DIR / "schema.md")
    for claim in ("RAYSPEC_INPUT_", "never persisted", "load-time"):
        assert claim in security, f"SECURITY.md drops the secret-contract claim {claim!r}"
        assert claim in schema, f"docs/schema.md no longer states {claim!r} — SECURITY.md drifted"


def test_security_states_that_the_engine_opens_no_socket() -> None:
    flat = _flat(_text(SECURITY))
    assert "The engine itself opens no network sockets" in flat


def test_contributing_quotes_the_gate_exactly_as_ci_runs_it() -> None:
    """The copy-pasteable gate must contain every check CI would fail the PR on."""
    gate = _normalise(_gate_command(_text(CONTRIBUTING)))
    assert gate, "CONTRIBUTING.md must present the gate as one copy-pasteable command line"
    for command in _ci_check_commands():
        assert _normalise(command) in gate, f"the gate in CONTRIBUTING.md omits CI's `{command}`"


def test_contributing_names_the_generated_artifact_checks_that_exist() -> None:
    """`docs/`, `examples/` and `schema/` changes have their own regeneration checks."""
    text = _text(CONTRIBUTING)
    for script in ("check_examples.py", "gen_skill.py", "gen_schemas.py"):
        assert f"scripts/{script}" in text, f"CONTRIBUTING.md does not mention scripts/{script}"
    for named in set(re.findall(r"scripts/[\w.-]+\.py", text)):
        assert (REPO_ROOT / named).is_file(), f"CONTRIBUTING.md names {named}, which does not exist"


def test_contributing_is_honest_about_the_review_bar() -> None:
    """A contributor must learn the schema line *before* writing the PR, not after."""
    text = _text(CONTRIBUTING)
    assert "docs/constitution.md" in text, "the admissibility test must be linked"
    assert "admissib" in text.lower()
    assert "CONTRACTS.md" in text
    assert "additive" in text.lower(), "the frozen-module rule must be stated"


def test_contributing_states_how_a_commit_is_accepted() -> None:
    text = _text(CONTRIBUTING)
    assert "git commit -s" in text, "DCO sign-off must be spelled out"
    assert "developercertificate.org" in text
    assert "Conventional Commits" in text


def test_contributing_explains_how_to_run_the_live_tests() -> None:
    """They are deselected by default and need credentials — say both, or nobody runs them."""
    text = _text(CONTRIBUTING)
    assert "RAYSPEC_LIVE=1" in text
    assert "-m live" in text


def test_code_of_conduct_is_the_covenant_21_with_a_working_contact() -> None:
    text = _text(CODE_OF_CONDUCT)
    assert "Contributor Covenant" in text
    assert "version 2.1" in text, "the Covenant version must stay stated in the attribution"
    for heading in (
        "## Our Pledge",
        "## Our Standards",
        "## Enforcement Responsibilities",
        "## Scope",
        "## Enforcement",
        "## Enforcement Guidelines",
        "## Attribution",
    ):
        assert heading in text, f"the Covenant section {heading!r} is missing"
    enforcement = text.split("## Enforcement", 1)[1].split("## Enforcement Guidelines", 1)[0]
    assert ADVISORY_URL in enforcement, "the enforcement contact must be a channel that exists"


def test_codeowners_gives_every_path_the_same_single_owner() -> None:
    rules = [
        line.split()
        for line in _text(CODEOWNERS).splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert len(rules) == 1, f"CODEOWNERS must have one rule for everything, found {rules}"
    pattern, *owners = rules[0]
    assert pattern == "*"
    assert len(owners) == 1 and owners[0].startswith("@"), owners


def test_bug_report_asks_for_the_two_things_that_make_a_report_diagnosable() -> None:
    """``rayspec doctor`` output and the run id turn most reports into a five-minute diagnosis."""
    form: Any = yaml.safe_load(_text(BUG_REPORT))
    assert form["name"] and form["description"]
    fields = form["body"]
    blob = _flat(yaml.safe_dump(fields))
    assert "rayspec doctor" in blob, "the bug form must ask for `rayspec doctor` output"
    assert "run id" in blob.lower(), "the bug form must ask for the run id"
    required = [
        field
        for field in fields
        if field.get("validations", {}).get("required")
        and "rayspec doctor" in _flat(yaml.safe_dump(field))
    ]
    assert required, "the `rayspec doctor` field must be required"


def test_feature_request_sends_the_author_to_the_admissibility_test_first() -> None:
    form: Any = yaml.safe_load(_text(FEATURE_REQUEST))
    blob = _flat(yaml.safe_dump(form))
    assert "constitution" in blob, (
        "a feature form that never mentions the constitution wastes both sides' time"
    )


def test_issue_config_routes_security_reports_out_of_the_public_tracker() -> None:
    config: Any = yaml.safe_load(_text(ISSUE_CONFIG))
    assert config["blank_issues_enabled"] is False
    links = config["contact_links"]
    security = [link for link in links if link["url"] == ADVISORY_URL]
    assert security, f"config.yml must offer the private security channel, has {links}"
    assert "SECURITY.md" in security[0]["about"]


def test_pull_request_template_mirrors_the_sections_a_good_pr_here_has() -> None:
    text = _text(PR_TEMPLATE)
    for heading in (
        "## What changed",
        "## Why",
        "## Contract changes",
        "## Changelog",
        "## Test plan",
    ):
        assert heading in text, f"the PR template lacks {heading!r}"
    gate = _gate_command(_text(CONTRIBUTING))
    assert _normalise(gate) in _normalise(text), "the PR template's gate must match CONTRIBUTING's"


def test_readme_carries_the_badges_and_the_links_a_public_repo_needs() -> None:
    text = _text(README)
    for link in ("CONTRIBUTING.md", "CODE_OF_CONDUCT.md", "SECURITY.md"):
        assert f"]({link})" in text, f"README.md does not link {link}"
    assert "](LICENSE)" in text


def test_readme_python_badge_matches_pyproject() -> None:
    """The badge is a promise about supported interpreters; pyproject is where that promise lives."""
    meta: Any = tomllib.loads(_text(REPO_ROOT / "pyproject.toml"))
    floor = meta["project"]["requires-python"].lstrip(">=")
    versions = sorted(
        classifier.rsplit(" ", 1)[-1]
        for classifier in meta["project"]["classifiers"]
        if classifier.startswith("Programming Language :: Python :: 3.")
    )
    badge = [line for line in _text(README).splitlines() if "img.shields.io/badge/python" in line]
    assert badge, "README.md has no Python badge"
    assert floor in badge[0], f"the Python badge does not name the {floor} floor"
    assert versions[-1] in badge[0], f"the Python badge does not name {versions[-1]}"


def test_readme_ci_badge_points_at_the_workflow_that_exists() -> None:
    text = _text(README)
    badges = re.findall(r"actions/workflows/([\w.-]+)/badge\.svg", text)
    assert badges, "README.md has no CI badge"
    for name in badges:
        assert (GITHUB_DIR / "workflows" / name).is_file(), f"badge points at a missing {name}"
