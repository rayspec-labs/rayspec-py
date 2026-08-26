<!-- Generated from docs/examples.md by scripts/gen_skill.py — do not edit here. -->
<!-- Canonical source: https://github.com/rayspec-labs/rayspec-py/blob/main/docs/examples.md -->
<!-- Sibling references in this directory: concepts.md · schema.md · templating.md · examples.md -->

# Examples

Runnable example projects live under `examples/` in the repository (each one is a `.rayspec/`
tree with its own README, a `stubs.yaml` and a `checks.yaml`, so it runs with `--dry-run --stubs`
and no login; `scripts/check_examples.py` drives every `checks.yaml`). The rayspec repository also
dogfoods itself through its own `.rayspec/workflows/`. The table below is the coverage matrix
(`examples/README.md` holds the full one) — every capability is exercised by at least one example.
The examples are projects to copy from; the workflows *bundled with rayspec* (`rayspec workflows`
lists them, `rayspec workflows eject <name>` copies one into `.rayspec/workflows/`) are the ones
that run by name in any project without copying anything.

| Example | Shows | Try |
|---|---|---|
| `hello_review` | a single `prompt:` step, `inputs` (string + enum), `outputs:`, a tier model, `rayspec plan`/`validate`, `--dry-run --stubs` | `rayspec run hello_review --dry-run --stubs stubs.yaml` |
| `fix_issue` | `shell:` → structured output (`output_schema`) → `when:` + `stop:` → `loop:` self-heal (`until` with `has_signal`, `iteration.prev`, `session:` continuation, `allow_failure`, `on_exhausted`) → `approve:` → a PR `shell:` step; worktree isolation by default | `rayspec plan fix_issue --input issue=123` |
| `triage_fanout` | `each:` fan-out with `max_parallel`, `on_failure: continue`, `items`, `join: any`/`always`, per-item structured output, a `python:` step with `deps` | `rayspec run triage_fanout --dry-run --stubs stubs.yaml` |
| `review_block` + `pr_review` | `include:` with `with:`/`outputs:`, named agents in `.rayspec/agents/`, `instructions_file`, a `tools` policy, `access: read-only`, Claude **and** Codex agents side by side, `provider_options`, a pricing table (`~$`), an `@alias` | `rayspec plan pr_review` |
| `review_sweep` | `defaults.on_step_failure: continue` — three independent review branches, one of them fails, the others still finish (the run still fails); declared `artifacts:` kept with the run; `join: always` | `rayspec run review_sweep --dry-run --stubs stubs.yaml` |
| `unsupported_demo` | `max_turns`/`tools` on a Codex agent → the capability error, and `--allow-unsupported` | `rayspec validate unsupported_demo` (exit 2) vs `--allow-unsupported` |
| `release_check` | `isolation: none`, `retry`/`timeout`, a `join: always` cleanup step, `env:`, `RAYSPEC_CONTEXT` + `jq`, `--repo` | `rayspec run release_check -i tag=v0.4.0 --dry-run --stubs stubs.yaml` |
| `notify_webhook` | a `secret: true` input: the webhook URL reaches the `shell:` step as `RAYSPEC_INPUT_WEBHOOK_URL` only, is never persisted (`<secret>` in `run.json`/`plan`/`show`) and is supplied again on resume | `rayspec run notify_webhook -i message="hi" --dry-run --stubs stubs.yaml` |
| `secret_via_tool` | a `secrets:` block in `config.yaml` (`env`/`file`/`cmd` source): the credential reaches a `shell:` **tool** that exposes a capability, and the agent consumes the tool's result — never the key | `rayspec run secret_via_tool --dry-run --stubs stubs.yaml` |

## Bundled workflows

These run by name in any project — `rayspec run <name>` with no `.rayspec/` at all — and
`rayspec workflows eject <name>` copies one into `.rayspec/workflows/` when you want to change it.
Every one of them is self-contained (inline agents, no prompt files) and covered offline by
`tests/workflows/checks.yaml` in the repository. Each description also says what the workflow is
*not* for — that is what the `rayspec-cli` skill routes a plain request by.

| Workflow | Does | Inputs |
|---|---|---|
| `fix_issue` | triage a GitHub issue, fix it in a self-healing loop, gate a PR (class `chore`) | `issue` (required), `base`, `mode`, `test_command` |
| `pr_review` | check out a PR, run `review_block` on it, optionally post the verdict | `pr` (required), `depth`, `post` |
| `review_block` | the includable Claude + Codex review with a judge; `include: review_block` | `target` (required), `depth` |
| `release_check` | tests, release notes, a human gate (class `release`), tag and notify | `tag` (required), `min_coverage`, `push` |
| `resolve_conflicts` | merge a base branch, classify every conflicted file with a read-only analyst, resolve in a loop until no marker is left and `test_command` passes, commit; a `risk: high` verdict pauses at a gate of class `risky` **before** any file is touched; a clean merge stops with `cancelled` (exit 4), giving up leaves the merge in progress for a human (exit 1, nothing committed) | `base`, `test_command`, `max_attempts` |
| `review_panel` | review the diff against `base` (or a checked-out PR) from several independent angles at once — one read-only reviewer per lens, none seeing the others — then a chair merges them into one verdict with `raised_by` per finding and named `disagreements`; a lost reviewer is counted (`reviewed`, `lost`), never fatal; `post: true` comments the verdict on the PR | `base`, `pr`, `lenses`, `post` |
| `validate_pr` | run `test_command` on `base` and on the PR's head in a worktree (detached checkouts, put back afterwards) and classify the difference deterministically — `clean`, `regression`, `preexisting`, `fixed` — with `newly_failing` / `newly_passing` named, before a read-only judge explains what the delta means; always exit 0, the verdict is `status` | `pr` (required), `base`, `test_command`, `failure_pattern` |
| `create_issue` | draft a GitHub issue from a description, search the tracker with the draft's own terms, let a read-only judge say whether it duplicates one of the real results (cancelled, exit 4, naming it), show the draft at a gate of class `chore`, then `gh issue create` with labels from a whitelist | `description` (required), `labels` |
| `architect` | a read-only survey: map the tree, pick the `max_areas` largest source directories, one read-only surveyor per area with a single `focus` (`coupling`, `layering`, `dead_code`), an architect writes the report into the run's `artifacts/`; a lost surveyor is counted, and the ceiling (`budget_usd` per agent, `max_tokens` on the run) is hard | `focus`, `max_areas` |
| `refactor_safely` | typecheck + tests before anything is touched (a red baseline stops the run, exit 1), a plan behind a gate of class `risky`, an edit → typecheck → test loop until both are green (at most `max_attempts`), then a fresh read-only reviewer reads the diff: shape only → a local commit, behaviour changed → exit 1 with the changes left staged | `goal` (required), `typecheck_command` (required), `test_command`, `max_attempts` |
| `prd_to_pr` | a Markdown PRD in, a reviewed PR out: count the requirements (too many, or no acceptance criteria → exit 4), a green baseline (red → exit 1), a plan with its open questions at a gate of class `scope`, tests written first by one agent and proven red by code (green or nothing written → exit 1), an implement → typecheck → test loop (at most `max_attempts`; a modified acceptance test or a blocked implementer stops it), a fresh reviewer reporting covered / uncovered / unrequested, a gate of class `chore` (self-approving on a clean review), then push + `gh pr create` | `prd` (required), `typecheck_command` (required), `test_command`, `max_requirements`, `max_attempts`, `base` |

`resolve_conflicts` always runs in a worktree, so neither the merge nor the agent's edits touch
your checkout; the merge commit lands on the run's `rayspec/resolve_conflicts-<id>` branch and
pushing or opening a PR is the caller's job. Hold its gate shut in your policy — a class a
workflow names is only as strict as the operator's policy makes it ([policy.md](https://github.com/rayspec-labs/rayspec-py/blob/main/docs/policy.md#approval-classes)):

```yaml
# .rayspec/policy.yaml
approvals:
  classes:
    risky: { allow_yes: false }   # never approved by --yes, --dry-run or --approve-class; a human still can
```

`review_panel` is Archon's five-reviewer pattern as an `each:` fan-out: the lenses are an input
(`--input lenses=security --input lenses=migrations` — drop `api_design` for a repository with no
public API, no fork needed), every reviewer is told its own angle and what the others cover, and
none of them sees another's findings. A reviewer that fails leaves a `null` slot the chair is
told about (`on_failure: continue`), so a verdict from three of five lenses says so in `reviewed`
/ `lost` instead of looking like a full one; only when every reviewer is lost does the run fail
(exit 1) — an empty diff stops it before anyone is asked (exit 4). Each agent carries a
`budget_usd`; the verdict is advisory and enforcing it is the caller's or CI's job.

`validate_pr` measures both sides — CI runs the tests once, on the head, and cannot tell a
failure the PR introduced from one `main` already had. `compare` is a `python:` step and is
authoritative: it parses each transcript with `failure_pattern` (pytest's `FAILED <id> - …` short
summary by default; set it for another runner) and classifies the pair of exit codes and failure
sets; the judge only interprets that result, with the PR's diff at hand. Both checkouts are
detached, because the run's worktree sits on its own branch and git refuses to check out a branch
that is checked out elsewhere, and a `join: always` step puts the checkout back whatever happened.
A run that completes exits 0 — `status` is the verdict; read it from `--json`. Pass
`--no-worktree` only on a clean tree you are not using: the workflow switches branches in it.

`create_issue` is the filing counterpart of `fix_issue`, and the duplicate check is what makes it
safe to run: the draft supplies the search terms, `gh issue list --search` (open and closed)
supplies the candidates, and the judge decides only against that real list. A duplicate ends the
run `cancelled` (exit 4) naming the issue; anything new waits at a gate of class `chore` — `--yes`
or `--approve-class chore` wave it through, and a policy with `chore: { allow_yes: false }` holds
it for a repository that must never receive an unattended issue. Labels come from the `labels`
input only (a whitelist; `[]` files it unlabelled): the agent proposes, the workflow constrains.

`architect` produces a document, not a change: `isolation: none` and every agent read-only, so
nothing is written to the repository; the report is `outputs.report` and a file in the run's
`artifacts/` directory (`outputs.report_path`). The areas are computed (the largest source
directories, vendored and generated trees left out), the surveyors run four at a time with
`on_failure: continue` (a lost area is named to the architect and counted in `lost`; only when
every surveyor is lost does the run fail), and the cost has a hard ceiling — `budget_usd` on each
agent and `defaults.max_tokens` on the run — reaching which ends the run `failed` (exit 1) rather
than finishing on credit. A tree with no source files stops with exit 4. A dry run simulates the
area scan as an empty list; `--exec-shell` shows the survey fan out.

`refactor_safely` is `fix_issue` with the opposite contract: nothing may change what the code
does. The typecheck and the tests run *before* anything is touched — if either is red the run
stops (exit 1), because a red tree afterwards would carry no information — and again after every
edit, as two separate steps, so the refactorer is told "does not typecheck" apart from
"typechecks, but the tests fail" (`typecheck_command` has no default on purpose). The plan waits
at a gate of class `risky` (hold it shut as for `resolve_conflicts`), the loop gives up after
`max_attempts` (exit 1, nothing committed), and a *fresh* read-only reviewer — never the agent
that made the change — reads the diff against the starting commit: shape only → a local commit
in the worktree, behaviour changed → exit 1 with the changes left staged for a human. An agent
that changed nothing ends the run with exit 4.

`prd_to_pr` is the workflow between `fix_issue` and `review_panel` in ambition: a product
requirements document goes in, one reviewed pull request comes out, and the loop that builds it
ends on an exit code, never on an agent's opinion. The prose is converted into something
executable *before* implementation starts: a planner turns the acceptance criteria into a test
plan and names every question the document leaves open with the assumption it would act on; a
human sees both at a gate of class `scope` — the cheapest moment to catch a misread PRD — and
the approval comment (`rayspec approve <run> "GHCR, not Docker Hub"`) is handed to the test
writer, the implementer and the reviewer as the answers. One agent then writes only the tests,
in its own session, and code decides what happened: the suite must be red afterwards (green
means vacuous tests, nothing written means no tests — both exit 1). A second agent implements
against them (`session: implement` across attempts) until the typecheck and the tests are green
— at most `max_attempts`, exit 1 when the bound is hit, with the branch and the worktree kept
for inspection — and code checks after every attempt that the acceptance tests were not edited
(exit 1 if they were: the tests are the contract). An implementer that hits an ambiguity the
plan did not surface answers `blocked` instead of guessing, and the run is cancelled with the
question (exit 4): amend the PRD and run again. A *fresh* reviewer reads the diff against the
document and reports every requirement as covered or uncovered and every behaviour nobody asked
for as `unrequested`; the PR opens either way, with the review, the coverage and the
assumptions in its body — a `partial` verdict only asks at the PR gate (class `chore`), which
approves itself on a clean review. Fully unattended is `--approve-class scope --approve-class
chore`; that accepts the planner's assumptions unread — they are still in the run
(`outputs.unresolved`) and in the PR body. The run stays on its `rayspec/prd_to_pr-<id>`
worktree branch and pushes it as `prd/<prd file stem>-<id>`; under `--no-worktree` the two
commits land on your current branch. Before any of that, `size_check` refuses a document with
more than `max_requirements` requirements or without acceptance criteria (exit 4): one PRD, one
pull request — a bigger document is several.

### Writing a PRD for `prd_to_pr`

The workflow reads any Markdown, and reads better when the document follows three conventions:
requirements marked `**R1 —**` … (or `### R1` headings; failing those, the bullets under a
`Requirements` heading are counted), one bullet per criterion under an `## Acceptance criteria`
heading (mandatory — the tests are derived from them), and an `## Open questions` section for
what is undecided (the planner surfaces every one as a stated assumption). A settings block wins
over the workflow's inputs when present:

```markdown
<!-- rayspec
test_command: pytest -q
typecheck: mypy src
max_requirements: 6
-->
```

## Minimal workflow to copy

```yaml
# .rayspec/workflows/review.yaml
rayspec: 1
name: review
description: Review the working tree and summarise findings.
inputs:
  target: { type: string, default: "." }
agents:
  reviewer: { provider: claude, model: small, access: read-only,
              instructions: You are a meticulous code reviewer. Be concrete. }
steps:
  - id: files
    shell: git ls-files "{{ inputs.target }}" | head -50
  - id: review
    needs: [files]
    agent: reviewer
    prompt: |
      Review these files and list findings:
      {{ steps.files.output }}
    output_schema:
      type: object
      properties: { verdict: { enum: [approve, request_changes] }, summary: { type: string } }
      required: [verdict, summary]
outputs:
  verdict: "{{ steps.review.output.verdict }}"
  summary: "{{ steps.review.output.summary }}"
```

```
rayspec validate
rayspec plan review
rayspec run review --dry-run --stubs-init stubs.yaml   # scaffold, then edit stubs.yaml
rayspec run review --dry-run --stubs stubs.yaml         # no provider login needed
rayspec run review --input target=src                   # the real thing (worktree by default)
```

## Patterns

- **Self-healing loop**: `loop:` with `until: steps.review.output | has_signal('BUILD-CLEAN')`;
  the implementer keeps its `session:`; a `shell: pytest -q` step with `allow_failure: true` is
  the authoritative verdict the reviewer sees (`{{ steps.check.exit_code }}`).
- **Branch and stop**: a structured `assess` step, then `when: steps.assess.output.verdict ==
  'skip'` → `stop: {status: cancelled, reason: ...}` and the happy path on the other branch.
- **Fan-out with partial failure**: `each:` over `steps.list.output | fromjson` with
  `on_failure: continue`; the downstream step reads `steps.triage.items` and skips the `null`
  slots.
- **Reusable block**: put a review block in `.rayspec/workflows/review_block.yaml` with its own
  `inputs:`/`outputs:` and `include:` it with `with:`; the including workflow only sees
  `steps.<include id>.output.<key>`.
- **Finally step**: `join: always` on a cleanup `shell:` step so it runs even when a sibling failed
  or the run is draining.
