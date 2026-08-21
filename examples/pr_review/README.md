# pr_review — a reusable review block, named agents, Claude + Codex side by side

Two workflows: `review_block` (lint → Claude review ‖ Codex review → judge) and `pr_review`,
which `include:`s it with `with:` inputs and reads its `outputs:`.

```
examples/pr_review/.rayspec/
├── config.yaml                 # tiers, @aliases, pricing (~$), provider settings
├── agents/reviewer.yaml        # Claude, read-only, instructions_file, thinking, budget_usd, tools, mcp
├── agents/codex_reviewer.yaml  # model "@fast" (alias pins provider=codex), instructions_mode: replace
├── prompts/reviewer.md         # Jinja-templated instructions (project.*)
├── prompts/review_prompt.md    # prompt_file for the Claude review step
└── workflows/{review_block,pr_review}.yaml
```

| Feature | Where |
|---|---|
| named agents in `.rayspec/agents/` (`rayspec agents` lists them) | `agents/*.yaml` |
| `instructions_file`, `instructions_mode: append` / `replace` | both agents |
| `tools: {allow: [read, mcp:github], deny: [web]}` and `access: read-only` | `reviewer.yaml` |
| `thinking`, `budget_usd`, `effort` | `reviewer.yaml` |
| `mcp:` server on an agent (GitHub MCP over stdio) | `reviewer.yaml` |
| `provider_options` (raw pass-through per provider) | both agents |
| `model: "@fast"` / `"@deep"` — aliases from `config.yaml` | `codex_reviewer.yaml`, `judge` |
| `agent: {extends: reviewer, model: "@deep", budget_usd: 3.0}` | `review_block` `judge` |
| `prompt_file:` | `claude_review` |
| `\| fromjson` on text output (Codex strict mode rejects `minimum`/`maximum`, so the Codex step returns JSON text) | `judge` |
| `include: review_block` + `with:` (validated against the block's `inputs:`) | `pr_review` `review` |
| `steps.review.output.<k>` — only the block's `outputs:` are visible | `pr_review` |
| `when: inputs.post and …` on a `type: boolean` input | `comment` |
| `config.yaml`: `tiers`, `aliases`, `pricing`, `providers` | `.rayspec/config.yaml` |

## Try it without credentials

```sh
cd examples/pr_review
rayspec workflows                               # review_block and pr_review with their descriptions
rayspec agents                                  # the two named agents
rayspec plan pr_review -i pr=17                 # included steps show as review/<id>; judge "extends reviewer"
rayspec run review_block -i target=src/ --dry-run --stubs stubs.yaml
rayspec run pr_review -i pr=17 -i post=true --dry-run --stubs stubs.yaml
```

Stub keys use globs (`*judge`) so the same file serves the block on its own (`judge`) and inside
the include (`review/judge`). Expected:

```
✓ review/lint succeeded
✓ review/claude_review succeeded · 2.2k tok
✓ review/codex_review succeeded · 1.9k tok · ~$0.00     ← "~" = estimated from the pricing table
✓ review/judge succeeded · 2.2k tok
✓ review succeeded
✓ comment succeeded
outputs: verdict=request_changes findings=[…] commented=true
```

## Run it for real

```sh
rayspec run pr_review -i pr=17                  # needs gh, claude, codex logins
rayspec run pr_review -i pr=17 -i depth=deep -i post=true --yes
```

The GitHub MCP server in `reviewer.yaml` needs the `github-mcp-server` binary on `PATH` and
`GITHUB_PERSONAL_ACCESS_TOKEN` in the environment (`.rayspec/.env` is loaded automatically);
remove the `mcp:` block and `mcp:github` from `tools.allow` if you do not have it — the review
works without it.
