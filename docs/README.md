# rayspec documentation

| Page | What it covers |
|---|---|
| [concepts.md](concepts.md) | the mental model: workflow/steps/run, the DAG and its bodies, lexical scopes, outputs, runs and resume, isolation |
| [schema.md](schema.md) | every field with its default, the join truth table, expression vs template fields, Jinja traps, status vocabulary, exit codes |
| [templating.md](templating.md) | the context, filters, the shell env-ref rule, python bodies, inputs and coercion |
| [providers.md](providers.md) | the neutral adapter, the generated capability matrix, per-provider mapping tables, access levels, auth, pricing |
| [cli.md](cli.md) | every command, flags, exit codes, `--json` shapes |
| [runs-and-resume.md](runs-and-resume.md) | store layout, `run.json`, events, resume and approval flows |
| [policy.md](policy.md) | `policy.yaml` and its layers, the worktree change guard, trusted workflows, `network:`/`commands:`, and what is only advisory |
| [isolation.md](isolation.md) | worktrees, `--repo`, registered projects, locks |
| [extending.md](extending.md) | adding a provider via entry points, the step-kind seam, sinks, stores, embedding |
| [testing.md](testing.md) | testing workflows offline: `rayspec test`, the case format, `--junit` in CI, the golden corpus and the fault-injecting store |
| [ci.md](ci.md) | rayspec in CI: the dry-run check as a reusable workflow, `--locked` under CI, how rayspec itself is released and how the docs site is published |
| [examples.md](examples.md) | the example projects and the capability coverage matrix |
| [constitution.md](constitution.md) | the design constitution: admissibility test for new fields, filter policy, case law |
| [agent-skill.md](agent-skill.md) | the two Claude Code skills shipped with rayspec (`rayspec-workflows`, `rayspec-cli`): what each contains, `rayspec skill install|show|path`, `rayspec init`, how they are generated and kept fresh |

`CONTRACTS.md` at the repository root lists the module boundaries and public surfaces.
