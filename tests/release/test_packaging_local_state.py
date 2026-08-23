# SPDX-License-Identifier: Apache-2.0
"""Neither published artefact may carry a *used* checkout's local state.

A checkout that has been used is not a clean checkout. `rayspec run` writes run records,
`rayspec trust add` writes a `trusted.yaml`, `rayspec lock` writes a `rayspec.lock`, a coding
agent leaves a `.claude/`, and the release job writes the bill of materials into `provenance/`.
The release publishes the sdist as well as the wheel, the sdist is what a distributor rebuilds
the wheel from, and a published artefact cannot be recalled — so the build has to drop all of it
rather than trust the tree it happens to run in.

The sharp end is `.rayspec/trusted.yaml`: it carries a digest of the workflow beside it, so a copy
that ships inside the corpus is still *valid* in the project `rayspec init --from` scaffolds. A
stranger's `rayspec trust check` then exits 0 on a workflow they never reviewed, and a
`trust.require` policy layer is satisfied by a file the package put there.

The assertion is an equality, not a deny-list of suffixes: what the artefacts carry has to be
exactly what git tracks. An enumeration only catches the local state somebody already thought of,
and this is a repository that has shipped an enumeration believing it had a total rule.

The staging is parametrised over whether `.gitignore` is present because the two halves protect
different things and only one of them is the build's own doing. Git's `.env` pattern is
unanchored and already covers every `.env` in the tree, so with `.gitignore` staged an assertion
about `.env` passes whether or not `[tool.hatch.build] exclude` mentions it at all — and an
assertion that cannot fail is worse than no assertion. Without it, every pattern in that list is
the only thing between the file and PyPI.
"""

from __future__ import annotations

import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

from rayspec.skill import SKILLS, SKILLS_SUBDIR

REPO = Path(__file__).resolve().parents[2]

#: The example the state is planted in — any of them would do.
EXAMPLE = "pr_review"

#: What a used checkout collects, keyed by its path relative to the repository root. Planted at
#: the root *and* next to an example, because the root copies are matched by anchored patterns and
#: the example copies by `examples/**` ones — `.gitignore` happens to cover some of the root half
#: and none of the other. Every value is fabricated, and distinctive enough to be recognisable in
#: a failure message.
PLANTED: dict[str, str] = {
    # written by a coding agent
    f"examples/{EXAMPLE}/.claude/settings.local.json": '{"permissions": {"allow": []}}\n',
    f"examples/{EXAMPLE}/.claude/skills/rayspec/SKILL.md": "---\nname: planted\n---\n",
    ".claude/settings.local.json": '{"permissions": {"allow": ["Bash(planted:*)"]}}\n',
    # written by `rayspec run`
    f"examples/{EXAMPLE}/.rayspec/runs/r1/record.json": '{"run_id": "r1"}\n',
    f"examples/{EXAMPLE}/.rayspec/runs/r1/events.jsonl": '{"token": "sk-ant-planted"}\n',
    ".rayspec/runs/r1/record.json": '{"run_id": "r1"}\n',
    # written by `rayspec trust add`
    f"examples/{EXAMPLE}/.rayspec/trusted.yaml": "trusted:\n  - name: planted\n",
    ".rayspec/trusted.yaml": "trusted:\n  - name: planted\n",
    # written by `rayspec lock`
    f"examples/{EXAMPLE}/.rayspec/rayspec.lock": "version: 1\nagents: {}\n",
    ".rayspec/rayspec.lock": "version: 1\nagents: {}\n",
    # a secret, at every depth one can land at
    f"examples/{EXAMPLE}/.env": "GH_TOKEN=ghp_planted\n",
    f"examples/{EXAMPLE}/.rayspec/.env": "GH_TOKEN=ghp_planted\n",
    ".rayspec/.env": "GH_TOKEN=ghp_planted\n",
    ".env": "GH_TOKEN=ghp_planted\n",
    # written into the checkout by the release job
    "provenance/rayspec-1.0.0.cdx.json": '{"bomFormat": "CycloneDX"}\n',
    "provenance/release-notes.md": "planted\n",
}


def tracked() -> list[str]:
    """Every file git tracks — the whole of what a release is entitled to publish."""
    out = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [name for name in out.split("\0") if name]


def _to_wheel(rel: str) -> str | None:
    """A repository path in the wheel's namespace, or ``None`` if the wheel never carries it."""
    if rel.startswith("src/rayspec/"):
        return rel.removeprefix("src/")
    if rel.startswith("examples/"):
        return f"rayspec/{rel}"
    return None


def _stage(tmp_path: Path, *, gitignore: bool) -> tuple[Path, list[str]]:
    """A checkout the builders can be pointed at: every tracked file, then the local state.

    Staging from ``git ls-files`` rather than copying the directory keeps the expectation and the
    input the same set, so the planted files are the only difference between them however dirty
    the tree this runs in happens to be.
    """
    staged = [rel for rel in tracked() if gitignore or rel != ".gitignore"]
    assert "pyproject.toml" in staged and "src/rayspec/cli/commands/init.py" in staged, staged[:20]

    stage = tmp_path / "repo"
    for rel in staged:
        target = stage / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / rel, target)
    for rel, body in PLANTED.items():
        target = stage / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    return stage, staged


def _built(stage: Path, out: Path) -> tuple[set[str], set[str]]:
    """``(wheel members, sdist members)``, generated metadata dropped from each."""
    from hatchling.builders.sdist import SdistBuilder  # the build backend, a dev dependency
    from hatchling.builders.wheel import WheelBuilder

    wheels = list(WheelBuilder(str(stage)).build(directory=str(out / "wheel")))
    sdists = list(SdistBuilder(str(stage)).build(directory=str(out / "sdist")))
    assert len(wheels) == 1, wheels
    assert len(sdists) == 1, sdists

    wheel = {n for n in zipfile.ZipFile(wheels[0]).namelist() if ".dist-info/" not in n}
    with tarfile.open(sdists[0], "r:gz") as tar:
        members = [m.name for m in tar.getmembers() if m.isfile()]
    prefix = f"{Path(sdists[0]).name.removesuffix('.tar.gz')}/"
    sdist = {n.removeprefix(prefix) for n in members} - {"PKG-INFO"}
    return wheel, sdist


@pytest.mark.parametrize("gitignore", [True, False], ids=["with-gitignore", "no-gitignore"])
def test_no_published_artefact_carries_local_state(tmp_path: Path, gitignore: bool) -> None:
    stage, staged = _stage(tmp_path, gitignore=gitignore)
    wheel, sdist = _built(stage, tmp_path / "dist")

    # Named first, because "a `trusted.yaml` shipped" deserves to be the failure message rather
    # than one line in a set difference.
    leaked = sorted(
        f"{kind}:{name}"
        for rel in PLANTED
        for kind, names, name in (
            ("sdist", sdist, rel),
            ("wheel", wheel, _to_wheel(rel) or ""),
        )
        if name and name in names
    )
    assert leaked == [], f"local state reached a published artefact: {leaked}"

    # And the total rule the deny-list is only one implementation of: what ships is exactly what
    # git tracks. Whatever a used checkout starts collecting *next* fails here too, without
    # anybody having had to think of it first.
    for kind, shipped, expected in (
        ("sdist", sdist, set(staged)),
        ("wheel", wheel, {name for rel in staged if (name := _to_wheel(rel))}),
    ):
        extra, missing = sorted(shipped - expected), sorted(expected - shipped)
        assert extra == [], f"the {kind} carries files git does not track: {extra}"
        assert missing == [], f"the {kind} is missing tracked files: {missing}"

    # The corpus really is in there, so a build that shipped nothing cannot pass by leaking
    # nothing — and `rayspec init --from` has something to copy.
    assert "rayspec/cli/commands/init.py" in wheel
    assert f"rayspec/examples/{EXAMPLE}/.rayspec/workflows/{EXAMPLE}.yaml" in wheel
    # Derived from the registry, not spelled out: a skill added or renamed later changes this
    # assertion with it. Naming the directory here is how this went stale the first time.
    missing_mirrors = [
        rel
        for skill in SKILLS
        if (rel := f"{SKILLS_SUBDIR.as_posix()}/{skill.name}/SKILL.md") not in sdist
    ]
    assert missing_mirrors == [], (
        f"the tracked skill mirrors `scripts/gen_skill.py --check` compares against are missing "
        f"({missing_mirrors}), so the suite cannot run from an unpacked sdist"
    )
