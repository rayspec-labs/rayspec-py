# rayspec in CI

A workflow is a file in the repository, so what CI can check is what a reviewer checks: does it
still load, does the graph still resolve, does every agent still get the prompt it is supposed to
get. `rayspec run --dry-run` answers all three **without a provider credential, without a token
and without spending anything** — every agent is replaced by the scripted stub, `shell:`/`python:`
steps are not executed, and gates auto-approve.

| Check | Command | What it catches |
|---|---|---|
| it still loads | `rayspec validate <workflow>` | a schema error, an unknown reference, a capability the provider does not have |
| the graph still runs | `rayspec run <workflow> --dry-run --stubs <file>` | a join that never satisfies, a template pointing at a step that is gone |
| the cases still hold | `rayspec test --junit report.xml` | an assertion about what an agent was asked ([testing.md](testing.md)) |

Two CI-only behaviours are worth knowing before the first red build:

- **`--locked` is on by default under `CI`.** If the project has a `.rayspec/rayspec.lock`, a run
  whose agent resolves to a different model or effort is refused, naming both
  ([`rayspec lock`](cli.md#rayspec-lock)). A project with no lockfile is not affected — there is
  nothing to enforce — so the default never breaks a repository that did not opt in.
- **Nothing may wait for a person.** Pass `--no-interactive` so an `approve:` gate pauses the run
  (exit 3) instead of prompting into a log nobody is reading. A dry run approves a gate by itself
  and usually runs straight through — but not when an operator policy holds the gate's approval
  class (`allow_yes: false`): that is never waived, by a rehearsal as much as by `--yes`, so a dry
  run can pause at exit 3 too. Treat 3 as an outcome your job may see, not one it cannot.

## The dry-run check, as a workflow you can call

rayspec ships the check as a reusable workflow. It installs rayspec from PyPI, runs one dry run
and reports the result — as a pull-request comment that is edited in place on every push, and
always into the run's job summary.

**Before the first release.** There is no `v1` tag yet, and the `rayspec` name on PyPI still holds
only a 0.0.1 placeholder — so the `uses:` ref below is pinned to a commit, which is what to keep
using until the tag exists, and the version it asks for cannot install until 1.0.0 is published.

<!-- rayspec:skip a GitHub Actions workflow, not a rayspec one -->
```yaml
# .github/workflows/rayspec.yml
name: rayspec
on: [pull_request]

jobs:
  dry-run:
    permissions:
      contents: read
      pull-requests: write
    uses: rayspec-labs/rayspec-py/.github/workflows/rayspec-dry-run.yml@87b46107f9ac9dcdc0e7ae5c4af10549f5dde7c2
    with:
      workflow: example                    # what `rayspec init` scaffolds — swap in your own
      stubs: .rayspec/stubs/example.yaml   # and the stub script it writes next to it
      rayspec-version: "1.0.0"
```

| Input | Default | Meaning |
|---|---|---|
| `workflow` | — (required) | the workflow to dry-run: a discovered name or a path |
| `project-dir` | `.` | where `.rayspec/` lives, for a repository that keeps it in a subdirectory |
| `stubs` | — | the stub script that supplies the agents' answers, relative to `project-dir` |
| `inputs-file` | — | YAML/JSON file of workflow inputs, relative to `project-dir` |
| `rayspec-version` | `>=1.0.0,<2` | the version to install — **pin it**, so a release never changes a check |
| `python-version` | `3.12` | the interpreter rayspec runs on |
| `comment` | `true` | post the result on the pull request |
| `comment-tag` | `default` | distinguishes the comments of several calls in one pull request |
| `fail-on-error` | `true` | fail the check when the dry run did not succeed |

**Outputs.** Two of them, set for every run and not only for the one that went well: `status`
is `succeeded`, `failed`, `paused`, `cancelled`, `not started (usage error)`, `interrupted`, or
`exit <code>` for a code no release of rayspec has a name for, and `exit-code` is
the code rayspec returned ([the table in `rayspec run`](cli.md#exit-codes)). Because they are
always there, `fail-on-error: false` genuinely hands the verdict over — the check stays green and
the calling workflow decides what a run that did not succeed means:

<!-- rayspec:skip a GitHub Actions workflow, not a rayspec one -->
```yaml
jobs:
  dry-run:
    permissions:
      contents: read
      pull-requests: write
    uses: rayspec-labs/rayspec-py/.github/workflows/rayspec-dry-run.yml@87b46107f9ac9dcdc0e7ae5c4af10549f5dde7c2
    with:
      workflow: example
      stubs: .rayspec/stubs/example.yaml
      rayspec-version: "1.0.0"
      fail-on-error: false

  triage:
    needs: dry-run
    if: needs.dry-run.outputs.status != 'succeeded'
    runs-on: ubuntu-latest
    steps:
      - run: echo "the dry run ${{ needs.dry-run.outputs.status }}, exit ${{ needs.dry-run.outputs.exit-code }}"
```

**Permissions.** The comment needs `pull-requests: write` on the *calling* job, as in the snippet
above. The reusable workflow requests nothing of its own: a called workflow that asks for a
permission its caller did not grant has the call rejected before any job starts, and a forgotten
line should not read as an unexplained failure. Without the grant the run still reports into the
job summary and prints a warning saying which permission is missing; the check does not fail for
that reason alone.

**Forks.** A pull request from a fork gets a read-only token, so the comment is skipped there by
design. The job summary carries the same report. Do not reach for `pull_request_target` to work
around it: that runs the fork's code with a writable token.

**Pinning.** `@v1` will be a tag that moves with the 1.x line — set by hand, as the last step of a
release (below); until it exists, and afterwards if you would rather not track a moving tag, pin a
commit sha as the snippets above do. `rayspec-version` defaults to `>=1.0.0,<2`, the same 1.x line,
deliberately rather than to *latest*: the `rayspec` name on PyPI is parked with a 0.0.1 placeholder
until the first release lands, so "whatever is newest" has a wrong answer available and an unpinned
check could quietly install a stub. Pin an exact version anyway — a check that silently changes
with someone else's release is a check nobody trusts. A bare version (`1.0.0`) is pinned exactly;
anything starting with an operator (`>=1.2,<2`) is passed through as a specifier; an empty string
is refused rather than resolved.

## How rayspec itself is released

The release runs from a tag and needs no credential of mine:

- `uv build` produces the sdist and the wheel — with a **pinned `uv`**, because the tool that
  builds what gets signed is part of what a release pins, and pinning the action that installs it
  to a commit does not pin the tool. The workflow refuses to continue when the tag and the version
  in `pyproject.toml` disagree, and checks the metadata with `twine check --strict` before
  installing the wheel into a clean environment and running it once.
- **PyPI is reached through Trusted Publishing** — a short-lived OIDC token minted for this
  repository and exchanged for an upload token. There is no PyPI token in this repository, so
  there is none to leak or to rotate.
- A **CycloneDX bill of materials** is generated from the locked runtime environment and attached
  to the GitHub release.
- The sdist and the wheel are **signed with Sigstore**; the `.sigstore.json` bundles are attached
  next to them.
- The **release notes are the CHANGELOG section** of that version
  ([`scripts/release_notes.py`](../scripts/release_notes.py)). A tag whose version nobody
  described stops the run before anything is published.

`workflow_dispatch` on the same workflow is the rehearsal: it builds, checks, generates the bill
of materials and the notes, and stops short of the two irreversible steps.

Three things stay manual on purpose, and the run's summary says so when it finishes: yanking the
placeholder that holds the `rayspec` name on PyPI, moving the `v1` tag onto the released commit
so every repository calling the dry-run workflow at `@v1` gets the new release, and rolling the
next `## [Unreleased]` heading into `CHANGELOG.md` — together with the released version's link
reference, which has no target to point at until the tag is pushed.

## The documentation site

The site is these same pages, built with MkDocs and published on GitHub Pages from `main`. A
pull request builds it with `--strict`, so a link that resolves on GitHub but not on the site
(or the other way round) fails the build. Locally:

```bash
uv run --group docs mkdocs serve    # live preview
uv run --group docs mkdocs build    # the strict build CI runs
```

Publishing needs Settings → Pages → Source set to **GitHub Actions** once, and a public
repository (or an Enterprise plan). Until then the build job still runs; only the deploy step
cannot.
