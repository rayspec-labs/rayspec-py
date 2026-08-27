# rayspec

[![CI](https://github.com/rayspec-labs/rayspec-py/actions/workflows/ci.yml/badge.svg)](https://github.com/rayspec-labs/rayspec-py/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python 3.11–3.14](https://img.shields.io/badge/python-3.11%E2%80%933.14-blue.svg)](pyproject.toml)
[![Code of Conduct: Contributor Covenant 2.1](https://img.shields.io/badge/code%20of%20conduct-Contributor%20Covenant%202.1-blue.svg)](CODE_OF_CONDUCT.md)

Declarative, YAML-defined workflows for coding agents — running on the
[Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/quickstart) and the
[OpenAI Codex SDK](https://learn.chatgpt.com/docs/codex-sdk). A pure CLI: no server, no UI,
no database.

```bash
mkdir myproj && cd myproj
pip install rayspec       # the engine AND both agent CLIs — no Node, no npm, nothing else
rayspec quickstart        # check this machine, scaffold a project, prove it with a free dry run
```

> **YAML coordinates. Code computes. Agents judge.**

A workflow says *what runs, in what order, under which gates, with which agent*. Deterministic
`shell:`/`python:` steps do the computing (and their verdicts are authoritative); `prompt:` steps
hand judgement to an agent; the engine runs the DAG, fans out, loops, pauses for humans, keeps
every step's output as a file and resumes from it.

<!-- rayspec:run issue=123 -->
```yaml
# .rayspec/workflows/fix_issue.yaml
rayspec: 1
name: fix_issue
inputs:
  issue: { type: integer, required: true }
agents:
  triage:      { provider: claude, model: small, access: read-only }
  implementer: { provider: codex,  model: medium, effort: high }
steps:
  - id: fetch
    shell: gh issue view "$RAYSPEC_INPUT_ISSUE" --json title,body
  - id: assess
    needs: [fetch]
    agent: triage
    prompt: "{{ steps.fetch.output }}\nIs this real and worth fixing now?"
    output_schema: { type: object, properties: { verdict: { enum: [fix, skip] } }, required: [verdict] }
  - id: bail
    needs: [assess]
    when: steps.assess.output.verdict == 'skip'
    stop: { status: cancelled, reason: not worth fixing }
  - id: build
    needs: [assess]
    when: steps.assess.output.verdict == 'fix'
    loop:
      max_iterations: 3
      until: steps.check.ok
      steps:
        - id: implement
          agent: implementer
          session: implement
          prompt: "Fix the issue with the smallest change that works. {{ steps.fetch.output }}"
        - id: check
          needs: [implement]
          shell: pytest -q
          allow_failure: true
  - id: confirm
    needs: [build]
    approve: "Open a PR for the fix?"
  - id: pr
    needs: [confirm]
    shell: git push -u origin HEAD && gh pr create --fill
outputs:
  verdict: "{{ steps.assess.output.verdict }}"
```

What you get: an implicit DAG with parallel siblings, `each:` fan-out, `loop:` with an `until`
verdict, `when:` branches and `stop:`, `approve:` gates (TTY prompt, else pause + resume),
`include:` for reusable blocks, named agents, strict Jinja templating with loud failures, Claude
*and* Codex behind one capability-checked adapter, file-based runs with resume, and a git
worktree per run by default.

## Quickstart

```bash
mkdir myproj && cd myproj                  # quickstart scaffolds where you stand — not in $HOME
pip install rayspec       # the engine AND both agent CLIs — no Node, no npm, nothing else to install
rayspec quickstart        # check this machine, scaffold a project, prove it with a free dry run
```

That is the whole first five minutes. `rayspec quickstart` prints what the machine has (Python,
`git`, both bundled agent CLIs, whether you are logged in), offers the two things it cannot
decide for you — a provider login, and `git init` when you are not in a repository — scaffolds
`.rayspec/`, and then runs a workflow end to end with **scripted agents: no credentials, no
network, no cost**. It finishes by naming the commands that matter next and saying which one
spends money.

Nothing above needs an account. The whole authoring loop — write, validate, plan, dry-run — is
free, and only a real agent run needs a login. `rayspec quickstart --no-interactive` asks
nothing and is safe in a `Dockerfile`; `rayspec doctor` answers "what is missing?" on its own at
any time.

<details>
<summary>Other ways in</summary>

```bash
uv tool install rayspec             # or: pipx install rayspec
uvx rayspec version                 # one-off, nothing installed
uv tool install git+https://github.com/rayspec-labs/rayspec-py      # from source, over HTTPS
uv tool install git+ssh://git@github.com/rayspec-labs/rayspec-py    # from source, over SSH
uvx --from git+https://github.com/rayspec-labs/rayspec-py rayspec version   # one-off, from source
uv tool install <path-to-checkout>  # from a local clone (or `uv tool install .` inside it)
```

</details>

### What `quickstart` set up, and what to do with it

```bash
rayspec version                     # prints `rayspec <version>`
rayspec doctor                      # Python, RAYSPEC_HOME, git/uv, SDKs, bundled CLIs, auth hints; --probe runs one real turn per provider

# the project quickstart scaffolded (rayspec init does this on its own)
#   .rayspec/{workflows/example.yaml, agents/reviewer.yaml, prompts/, config.yaml, stubs/example.yaml, tests/example/approves.yaml}
#   + .claude/skills/{rayspec-workflows,rayspec-cli}/ (the coding-agent skills; --no-skill to skip)
#   --kind content for a non-code project; rayspec init --force to overwrite

# check it
rayspec workflows                   # discovered workflows: yours, then the bundled library
rayspec validate                    # schema, graph, references, provider capabilities
rayspec test                        # the scaffolded case — a scripted dry run, no login needed
rayspec plan example                # inputs, resolved agents/models, step order, capability report

# dry-run it — providers become a scripted stub, shell steps are skipped, no login needed
rayspec run example --dry-run --stubs .rayspec/stubs/example.yaml     # ✓ files · ✓ review · outputs table

# run it for real (the example reviews in place; delete its `isolation: none` line to get a git worktree per run)
rayspec run example --input target=src
```

For real agent runs you need a logged-in `claude` (claude.ai) or `codex` (`codex login`) — both
CLIs ship with rayspec, and `rayspec quickstart` offers to run the login for you, by absolute
path. Dry runs need neither.

Prefer to start from a blank file? A workflow is one YAML document; `--stubs-init` writes the
stub answers for a dry run from it:

```bash
mkdir -p .rayspec/workflows
cat > .rayspec/workflows/review.yaml <<'EOF'
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
EOF

# 3. check it
rayspec workflows                 # discovered workflows
rayspec validate                  # schema, graph, references, provider capabilities
rayspec plan review               # inputs, resolved agents/models, step order, capability report

# 4. dry-run it — providers become a scripted stub, shell steps are skipped, no login needed
rayspec run review --dry-run                            # ✓ files · ✓ review · outputs table
rayspec run review --dry-run --stubs-init stubs.yaml    # optional: scaffold stub answers to edit …
rayspec run review --dry-run --stubs stubs.yaml         # … and replay them

# 5. run it for real (a git worktree on branch rayspec/review-<id> is created for the run)
rayspec run review --input target=src
```

A run prints one line per step, the `outputs:` table, the worktree path and branch, tokens/cost
and the run directory (`~/.rayspec/projects/<slug>/runs/<run-id>/`). Exit codes: `0` succeeded ·
`1` failed · `2` usage/validation error · `3` paused at an approval gate · `4` cancelled ·
`130` interrupted. Inspect runs with `rayspec runs`, `rayspec show <run>` and `rayspec logs <run>`;
continue an interrupted or paused run with `rayspec resume <run>` (or `rayspec run review
--resume <run>`), decide a gate without a terminal with `rayspec approve|reject <run>`.

## Use rayspec from a coding agent

rayspec ships two [Claude Code skills](docs/agent-skill.md) — each a skill file plus compressed
references to these docs — that teach an agent to author, validate, dry-run, run and debug
workflows without reading this repository: `rayspec-workflows` for the YAML DSL and `rayspec-cli`
for the command line. Each points at the other. `rayspec init` writes both into the project; you
can also install them by hand:

```bash
rayspec skill install             # <project>/.claude/skills/{rayspec-workflows,rayspec-cli}/
rayspec skill install --global    # ~/.claude/skills/…                    (every project)
rayspec skill install rayspec-cli # just one of them
```

Open a fresh Claude Code session afterwards — the skills load automatically. `rayspec skill show`
tells you whether the installed copies are up to date; `rayspec skill install --force` refreshes
them after upgrading rayspec.

You need not name a workflow either: "fix issue 42" or "review PR 118 from a security angle" makes
the agent list what is installed (`rayspec workflows --json`), pick one, fill its inputs and
propose the `rayspec run` line — running a read-only workflow directly and asking before anything
that writes ([docs/agent-skill.md](docs/agent-skill.md#selecting-a-workflow-from-a-request)).

## Documentation

- [docs/concepts.md](docs/concepts.md) — the mental model (workflow/steps/run, DAG + bodies, scopes, outputs, runs, isolation)
- [docs/schema.md](docs/schema.md) — every field and default, the join truth table, statuses, exit codes, Jinja traps
- [docs/templating.md](docs/templating.md) — context, filters, the shell env-ref rule, python bodies, inputs
- [docs/providers.md](docs/providers.md) — the neutral adapter, the generated capability matrix, Claude/Codex mapping, auth, pricing
- [docs/cli.md](docs/cli.md) — every command, flag and `--json` shape
- [docs/runs-and-resume.md](docs/runs-and-resume.md) — the run directory, `run.json`, events, resume, approval gates
- [docs/policy.md](docs/policy.md) — `policy.yaml`, the worktree change guard, trusted workflows, and what is only advisory
- [docs/isolation.md](docs/isolation.md) — worktrees, `--repo`, registered projects, locks
- [docs/extending.md](docs/extending.md) — adding a provider via entry points, sinks, stores, embedding
- [docs/examples.md](docs/examples.md) — the example projects and what each one shows
- [docs/dogfooding.md](docs/dogfooding.md) — running rayspec on rayspec: bootstrap, policy, budgets, resume/eject, writing a PRD
- [docs/testing.md](docs/testing.md) — `rayspec test`: declarative cases, `--junit` in CI, the golden corpus
- [docs/ci.md](docs/ci.md) — rayspec in CI: the dry-run check as a reusable workflow, and how rayspec is released
- [docs/constitution.md](docs/constitution.md) — why the schema is narrow (admissibility test, case law)
- [docs/agent-skill.md](docs/agent-skill.md) — the Claude Code skill: what it contains, `rayspec skill install|show|path`, how it is generated
- [docs/README.md](docs/README.md) — index of the above

Module boundaries are documented in `CONTRACTS.md`.

## Status

**Released** — `rayspec version` prints the build you run, and [`CHANGELOG.md`](CHANGELOG.md) has
the history.

Shipped: the schema, loader and validator, templating, the Claude and Codex adapters, the engine
(DAG, loop/each/include, approval, resume, dry run, the per-workdir path lock), run-level caps
(`budget_usd`, `max_tokens`, `timeout_total`), step-level `artifacts:`, the file store, worktree
isolation and `--repo`, the Rich live console (`rayspec run` on a TTY; one line per step
otherwise), `secret: true` inputs, extension entry points for commands, stores, sinks and approval
prompts, the two packaged Claude Code skills (`rayspec-workflows` for authoring the YAML and
`rayspec-cli` for operating the engine), and the `examples/` gallery.

Released on PyPI: [`rayspec`](https://pypi.org/project/rayspec/) — `pip install rayspec` (or
`uv tool install rayspec`) gets you this build, and it brings the Claude Code and Codex CLIs with
it, so no separate install of either is needed.

Commands: `quickstart`, `init`, `new`, `doctor`, `run`, `resume`, `approve`, `reject`, `cancel`,
`validate`, `plan`, `test`, `explain`, `eval`, `show`, `logs`, `audit`, `runs`, `costs`, `lock`,
`workflows`, `agents`, `providers`, `plugins`, `projects`, `worktrees`, `trust`, `schema`,
`skill`, `completion`, `version`.

## Development

```bash
uv sync --all-groups
uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest -q -m 'not live'
uv run python scripts/gen_capability_matrix.py     # regenerate the matrix in docs/providers.md
uv run python scripts/gen_skill.py                 # regenerate both skills' references/ from docs/ + mirror .claude/skills/ (--check in the gate)
```

Python ≥ 3.11, anyio-only concurrency.

## Contributing

Bug reports, patches and questions are all welcome. [CONTRIBUTING.md](CONTRIBUTING.md) has the
setup, the one-line quality gate, what a test is expected to look like, and the (deliberately high)
bar for new schema fields. Everyone taking part is expected to follow the
[Code of Conduct](CODE_OF_CONDUCT.md).

Found something exploitable? Please do not open an issue — [SECURITY.md](SECURITY.md) has the
private reporting channel, the 90-day disclosure window and the threat model, which is unusual
enough to be worth reading first: executing what a workflow declares is the product, so a hostile
workflow file is a hostile script, while a leaked `secret: true` input is a real vulnerability.

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

Contributions are accepted under the [Developer Certificate of Origin](https://developercertificate.org/):
sign off each commit with `git commit -s`, which adds a `Signed-off-by:` trailer certifying you have the
right to submit the work under this license.
