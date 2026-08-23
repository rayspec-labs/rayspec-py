# The rayspec skills for coding agents

rayspec ships two [Claude Code skills](https://code.claude.com/docs/en/skills) so that a fresh
coding-agent session — Claude Code, or any agent that reads the
[Agent Skills](https://agentskills.io) format — can author, validate, dry-run, run and debug
rayspec workflows without reading this repository. They are part of the Python package
(`rayspec/skill/<name>/`), so every install of rayspec carries the versions of the skills that
match it.

There are two because authoring a workflow and operating a run are two different jobs, with two
different vocabularies, and an agent doing one of them should not have to page in the other:

| Skill | What it teaches | Load it when |
|---|---|---|
| `rayspec-workflows` | the YAML DSL: every step kind and field, templating and scoping, agents, prompts, includes, secrets, the `.rayspec/` files you write by hand | writing or editing a workflow, agent or prompt |
| `rayspec-cli` | the CLI: every command, flag, `--json` shape and exit code, plus the stub file, providers and capabilities, cost, policy and runs | validating, planning, running, resuming, auditing or debugging |

Each names the other one in its `description:` and in its text, so an agent that loaded one is
told the other exists: `rayspec-workflows` ends its authoring loop with *now validate, plan and
dry-run — load the `rayspec-cli` skill*, and `rayspec-cli` sends every question about a YAML
field back to `rayspec-workflows`.

## What they contain

```
.claude/skills/rayspec-workflows/
  SKILL.md                    hand-written core: frontmatter `name: rayspec-workflows` +
                              `description:` (what the agent sees to decide to load it), then the
                              mental model, the authoring loop, a YAML cheat-sheet with every step
                              kind, the join truth table, inputs (incl. `secret: true`), agents,
                              the templating rules that bite (env-ref rule, expression vs template
                              fields, strict undefined, scopes), a field index that lists every
                              field the schema defines, best practices, three complete worked
                              workflows, the authoring commands, and pitfalls
  references/concepts.md      verbatim copies of docs/concepts.md, docs/schema.md,
  references/schema.md        docs/templating.md and docs/examples.md — each with a three-line
  references/templating.md    "generated from …" header; links between them stay relative, links
  references/examples.md      to any other page point at the published docs on GitHub

.claude/skills/rayspec-cli/
  SKILL.md                    hand-written core: frontmatter `name: rayspec-cli` +
                              `description:`, then the run/isolation/lock mental model, a CLI
                              table covering every command with its key flags and exit codes, the
                              exit codes and `--json` contract, the operating loops (check before
                              you spend, offline tests, record-and-replay, debugging), a safety
                              class for every command, governance and trust, the stub file format,
                              providers/capabilities/cost, and the operational pitfalls
  references/cli.md           verbatim copies of docs/cli.md, docs/providers.md, docs/testing.md,
  references/providers.md     docs/policy.md, docs/runs-and-resume.md, docs/isolation.md and
  references/testing.md       docs/ci.md, with the same header and link handling
  references/policy.md
  references/runs-and-resume.md
  references/isolation.md
  references/ci.md
```

A docs page belongs to exactly one skill. A page the other skill needs is **linked** at its
published URL, never duplicated — that is what the link rewriter in `scripts/gen_skill.py` does
for any target outside the skill being generated. Four pages ship with neither skill and are
online only: `README.md` (this directory's index), `agent-skill.md` (this page — it is written
for the person installing the skills), `extending.md` (writing rayspec plugins) and
`constitution.md` (why the DSL refuses fields).

Both `SKILL.md` files are deliberately short: they state the rules that are easy to get wrong
(quote `{{ }}` in shell bodies, `when:` is a bare expression, `steps.review` is not visible
outside its loop, never pass a secret as a plain input, dry-run before a real run, ask before a
run that edits or spends) and send the agent to `references/` for every field and flag.

Everything the skills state is taken from the docs, and `tests/skill/` holds both directions of
the agreement:

- **soundness** — every command and flag a CLI table names exists in the Typer app, every ```yaml
  fence parses with the real loader, every workflow the authoring page shows validates without
  warnings and dry-runs with the stub provider, and a "fresh agent" workflow from the skills alone
  replays `validate` → `plan` → `--dry-run --stubs-init` → `--dry-run --stubs`;
- **completeness** — every leaf command and invokable group of the CLI is in exactly one skill's
  table, every `docs/*.md` page is in exactly one skill's references or in a named online-only
  list with a reason, and every field of every schema model appears in `rayspec-workflows`. A
  deliberate omission is a named, justified entry in a deny-list, so adding a command, a page or
  a field forces a decision instead of silently passing.

## Installing them

| How | Where it lands | When |
|---|---|---|
| `rayspec init` | `<root>/.claude/skills/rayspec-workflows/` and `…/rayspec-cli/` (next to the new `.rayspec/`) | every new project; `--no-skill` opts out of both |
| `rayspec skill install` | `<project>/.claude/skills/` (`--root DIR`; default: the nearest directory with `.rayspec/`, then `.git`, else the cwd) | an existing project — commit the directories so teammates' agents get them too |
| `rayspec skill install --global` | `~/.claude/skills/` | once per machine, for every project you work in |

All three write **both** skills. Every subcommand takes an optional name to act on one of them
(`rayspec skill install rayspec-cli`); an unknown name is exit 2 with a did-you-mean.

Both forms are idempotent: existing files are kept (`exists … (skipped; use --force to
overwrite)`), `--force` overwrites. Afterwards open a **fresh** Claude Code session in the
project (or anywhere, for a global install) — Claude Code picks up skills at start-up; the agent
loads a `SKILL.md` when a request matches its description and reads the references on demand.

`rayspec skill show` prints, for each skill, the packaged copy (path, rayspec version, a
12-hex-digit content digest, file count) and whether the project and global installs are `up to
date`, differ, or are missing; `rayspec skill path` prints the packaged directories (useful for
agents other than Claude Code: point them at one, or copy it wherever that agent looks for
skills). Details and the `--json` shape are in [cli.md](cli.md#rayspec-skill-install).

## Updating after upgrading rayspec

An installed copy is a snapshot. After `uv tool upgrade rayspec` (or reinstalling from git) run
`rayspec skill show` — a copy written by the previous version shows `differs from the packaged
skill` — and refresh it with `rayspec skill install --force` (add `--global` for the user-wide
copy). Local edits to an installed `SKILL.md` are overwritten by `--force`; keep project-specific
guidance in your `CLAUDE.md` instead and let the skills stay generic.

## How they are generated and kept fresh

Each `SKILL.md` is hand-written in `src/rayspec/skill/<name>/SKILL.md`. The reference files are
generated — never edited by hand — by `scripts/gen_skill.py`, which copies that skill's `docs/*.md`
pages verbatim, prepends the header, rewrites relative links, and mirrors both skill directories
to `.claude/skills/<name>/` at the repository root (so this repository's own coding-agent sessions
use exactly the packaged skills):

```bash
uv run python scripts/gen_skill.py          # after editing docs/*.md or a SKILL.md
uv run python scripts/gen_skill.py --check  # exit 1 when a reference or a mirror is stale
```

`tests/skill/test_skill_fresh.py` runs the check in the test suite, so a docs change that is not
regenerated fails the gate (`uv run pytest -q tests/skill`). The wheel includes the skill files as
package data (`importlib.resources` reads them, so `rayspec skill install` works from a
`uv tool install` without a checkout).
