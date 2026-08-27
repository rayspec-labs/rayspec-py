<!-- Generated from docs/dogfooding.md by scripts/gen_skill.py — do not edit here. -->
<!-- Canonical source: https://github.com/rayspec-labs/rayspec-py/blob/main/docs/dogfooding.md -->
<!-- Sibling references in this directory: cli.md · providers.md · testing.md · policy.md · runs-and-resume.md · isolation.md · ci.md · dogfooding.md -->

# Running rayspec on rayspec

rayspec develops rayspec. The bundled workflows — `prd_to_pr`, `fix_issue`, `pr_review`,
`refactor_safely` — run against this repository the same way they run against any other, and this
page is the operator's guide to doing that safely. It is written for this repo, but every rule
here is a rule for using rayspec on itself, so it doubles as the worked example the other pages
point at.

## Bootstrap

A fresh worktree has no virtualenv. Bootstrap it before anything runs:

```
uv sync --all-groups
```

`prd_to_pr`'s `setup_command` does this for you when it is set (an input or a line in the PRD's
rayspec block) — its `baseline` step then typechecks and tests against real dependencies rather
than a red tree of import errors. For a manual run in a fresh clone, run it yourself first.

## Running a workflow on this repo

The repo's own commands are the typecheck and the test suite:

```
# implement a PRD, test-first, into a reviewed PR (worktree isolation by default)
rayspec run prd_to_pr \
  --input prd=next_release/rayspec-prds/PRD-08-docker-image.md \
  --input typecheck_command='uv run ruff check . && uv run ruff format --check . && uv run pyright' \
  --input test_command="uv run pytest -q -m 'not live'" \
  --detach
rayspec logs <id> --follow            # watch it; --exit-code to wait and learn its outcome

# fix a GitHub issue
rayspec run fix_issue --input issue=42 --detach
```

Long runs want `--detach` (PRD-07): the launcher backgrounds the run and prints its id, and
`rayspec logs <id> --follow` streams it. Answer a gate with `rayspec approve <id> "…"` /
`rayspec reject <id> …`; list runs with `rayspec runs`; stop one with `rayspec cancel <id>` (or
`--now` to interrupt a wedged step). `rayspec show <id>` reports the heartbeat age and, for a
detached run, where its launch log is.

## The environment a run exports (and why the suite scrubs it)

A run exports its own coordinates into every `shell:`/`python:` step it launches: `RAYSPEC_HOME`,
`RAYSPEC_POLICY`, `RAYSPEC_INPUT_<NAME>`, `RAYSPEC_RUN_ID`, `RAYSPEC_STATE_DIR`, … When a
`prd_to_pr` run's `test_command` is *this project's own suite*, those variables land in the
suite's process. rayspec's test suite therefore scrubs them all in one place
(`tests/conftest.py::_no_ambient_env`), and `rayspec test` clears `RAYSPEC_POLICY` per case — so
the suite's outcome never depends on being run from inside another run. If you write a test that
shells out to rayspec, remember it inherits this environment.

## Policy: three layers, most-restrictive wins

Ceilings come from `.rayspec/policy.yaml` (project) under `~/.rayspec/policy.yaml` (user) under
`RAYSPEC_POLICY` (a file you point at for one invocation). This repo ships a project policy:

```yaml
# .rayspec/policy.yaml
budget:
  per_run: 40                 # a run may not spend more than $40 without an explicit override
approvals:
  classes:
    scope:  { allow_yes: false }   # the plan gate always asks a human — the cheapest moment to catch a misread PRD
```

To raise the per-run cap for one expensive run, point `RAYSPEC_POLICY` at a scratch file with a
higher `budget.per_run` (or edit the project file and restore it) — and record that you did.

## Budgets and caps

An agent's `budget_usd`/`max_turns` are a hard per-step stop; the run-level `defaults.budget_usd`
/ `max_tokens` / `timeout_total` are the circuit breakers. The bundled workflows expose their
agent budgets as **inputs** (`prd_to_pr`'s `tester_budget_usd`/`implementer_budget_usd`/
`reviewer_budget_usd`), so you raise one with `--input implementer_budget_usd=50` instead of
ejecting the workflow — the one numeric field that accepts `{{ inputs.<name> }}`. `rayspec plan
<wf>` shows every agent's resolved budget and each step's description before a token is spent.

## Answering, resuming, re-running

- A gate pauses the run (exit 3). Answer it: `rayspec approve <id> "GHCR, not Docker Hub"`.
- `prd_to_pr`'s `blocked` gate is answered the same way, **or** by editing the acceptance tests in
  the worktree and approving with an empty comment — that amends the contract and the loop
  continues.
- `rayspec resume <id>` continues a paused/failed run, replaying the steps that succeeded.
- `rayspec resume <id> --rerun 'build[*]/implement'` re-runs matching steps instead of replaying
  them — for when a fingerprint cannot see that an input moved.

## The resume/eject rule

A resume reloads the file the run **recorded** (`run.json`'s `workflow_path`). Ejecting a bundled
workflow (`rayspec eject prd_to_pr`) changes what `rayspec run prd_to_pr` resolves from now on, not
what `rayspec resume <id>` of an earlier run loads — that keeps loading the copy it recorded.
`resume`/`approve`/`reject` print a `note:` when they notice you have an ejected copy of a name the
run recorded as bundled; to continue on the ejected copy, start a new run or
`rayspec run <name> --resume <id> --force`.

## Writing a PRD for `prd_to_pr`

`size_check` reads the requirements (marked `**R1 —**` / `### R1`, or the bullets under a
`Requirements` heading), the acceptance criteria (mandatory — one bullet each under
`## Acceptance criteria`), and a settings block that wins over the inputs:

```markdown
<!-- rayspec
setup_command: uv sync --all-groups
test_command: uv run pytest -q -m 'not live'
typecheck: uv run ruff check . && uv run ruff format --check . && uv run pyright
max_requirements: 8
-->
```

One PRD is one pull request: a document with more than `max_requirements` requirements is refused
(exit 4). Split a bigger one, or raise the limit with `--input max_requirements=N` or the block.

## Housekeeping

Isolated runs work in a git worktree under `~/.rayspec`; `rayspec worktrees list` shows them and
`rayspec worktrees clean` removes the finished ones. A run's store is under
`~/.rayspec/projects/<slug>/runs/<id>/`; `rayspec runs`/`show`/`logs`/`explain` read it. Nothing a
run does touches your working checkout unless you pass `--no-worktree`.
