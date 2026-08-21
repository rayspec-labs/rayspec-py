# The rayspec skill for coding agents

rayspec ships a [Claude Code skill](https://code.claude.com/docs/en/skills) named `rayspec` so
that a fresh coding-agent session — Claude Code, or any agent that reads the
[Agent Skills](https://agentskills.io) format — can author, validate, dry-run, run and debug
rayspec workflows without reading this repository. The skill is part of the Python package
(`rayspec/skill/rayspec/`), so every install of rayspec carries the version of the skill that
matches it.

## What it contains

```
.claude/skills/rayspec/
  SKILL.md                  hand-written core (~350 lines): frontmatter `name: rayspec` +
                            `description:` (what the agent sees to decide to load the skill), then
                            the mental model, the authoring loop, a YAML cheat-sheet with every
                            step kind, the join truth table, inputs (incl. `secret: true`), agents,
                            the templating rules that bite (env-ref rule, expression vs template
                            fields, strict undefined, scopes), a CLI table with exit codes, the
                            stub file format, provider/capability rules, pitfalls, and which
                            reference to read when
  references/concepts.md    verbatim copies of docs/concepts.md, docs/schema.md,
  references/schema.md      docs/templating.md, docs/cli.md, docs/providers.md and
  references/templating.md  docs/examples.md — each with a three-line "generated from …" header;
  references/cli.md         links between them stay relative, links to other pages
  references/providers.md   (runs-and-resume.md, isolation.md, …) point at the published
  references/examples.md    docs on GitHub
```

`SKILL.md` is deliberately short: it tells the agent the rules that are easy to get wrong (quote
`{{ }}` in shell bodies, `when:` is a bare expression, `steps.review` is not visible outside its
loop, never pass a secret as a plain input, dry-run before a real run, ask before a run that
edits or spends) and sends it to `references/` for every field and flag. Everything the skill
states is taken from the docs; `tests/skill/` validates its cheat-sheet workflows with the real
loader, dry-runs them with the stub provider, checks every command and flag of its CLI table
against the Typer app, and replays a "fresh agent" workflow written from the skill alone through
`validate` → `plan` → `--dry-run --stubs-init` → `--dry-run --stubs`.

## Installing it

| How | Where it lands | When |
|---|---|---|
| `rayspec init` | `<root>/.claude/skills/rayspec/` (next to the new `.rayspec/`) | every new project; `--no-skill` opts out |
| `rayspec skill install` | `<project>/.claude/skills/rayspec/` (`--root DIR`; default: the nearest directory with `.rayspec/`, then `.git`, else the cwd) | an existing project — commit the directory so teammates' agents get it too |
| `rayspec skill install --global` | `~/.claude/skills/rayspec/` | once per machine, for every project you work in |

Both forms are idempotent: existing files are kept (`exists … (skipped; use --force to
overwrite)`), `--force` overwrites. Afterwards open a **fresh** Claude Code session in the
project (or anywhere, for a global install) — Claude Code picks up skills at start-up; the
agent loads `SKILL.md` when a request matches its description (workflows, `.rayspec/` files,
agents, stubs, running or debugging rayspec) and reads the references on demand.

`rayspec skill show` prints the packaged skill (path, rayspec version, a 12-hex-digit content
digest, file count) and whether the project and global installs are `up to date`, differ, or are
missing; `rayspec skill path` prints the packaged directory (useful for agents other than Claude
Code: point them at it, or copy it wherever that agent looks for skills). Details and the
`--json` shape are in [cli.md](cli.md#rayspec-skill-install).

## Updating after upgrading rayspec

An installed copy is a snapshot. After `uv tool upgrade rayspec` (or reinstalling from git) run
`rayspec skill show` — a copy written by the previous version shows `differs from the packaged
skill` — and refresh it with `rayspec skill install --force` (add `--global` for the user-wide
copy). Local edits to an installed `SKILL.md` are overwritten by `--force`; keep project-specific
guidance in your `CLAUDE.md` instead and let the skill stay generic.

## How it is generated and kept fresh

`SKILL.md` is hand-written in `src/rayspec/skill/rayspec/SKILL.md`. The reference files are
generated — never edited by hand — by `scripts/gen_skill.py`, which copies the six `docs/*.md`
pages verbatim, prepends the header, rewrites relative links, and mirrors the whole skill
directory to `.claude/skills/rayspec/` at the repository root (so this repository's own
coding-agent sessions use exactly the packaged skill):

```bash
uv run python scripts/gen_skill.py          # after editing docs/*.md or SKILL.md
uv run python scripts/gen_skill.py --check  # exit 1 when a reference or the mirror is stale
```

`tests/skill/test_skill_fresh.py` runs the check in the test suite, so a docs change that is not
regenerated fails the gate (`uv run pytest -q tests/skill`). The wheel includes the skill files
as package data (`importlib.resources` reads them, so `rayspec skill install` works from a
`uv tool install` without a checkout).
