# The rayspec workflow-language constitution

rayspec's YAML is a *coordination* language, not a programming language. This page is the tie-breaker
for every "can we add a field for X?" discussion. It is adapted from Archon's workflow-language
constitution and rewritten for rayspec's choices.

## The one-line rule

> **YAML coordinates. Code computes. Agents judge.**

The workflow file decides *what runs, in what order, under which gates, with which agent*. Anything
that is a computation belongs in a `shell:` or `python:` step. Anything that is a judgement belongs in
a `prompt:` step. The engine must be able to govern a run without understanding the content of
prompts or scripts.

## Admissibility test for new schema fields

A proposed field is admissible only if **all three** hold:

1. **Governance** — the engine needs to *see* the value to schedule, gate, isolate, retry, or
   account for a step. If only a provider or a script would read it, it is not a step field.
2. **Data, not evaluation** — the value is data the engine reads, not an expression it has to
   evaluate beyond the existing Jinja surface (`when:`, `until:`, `each:`, templates).
3. **No existing escape hatch** — it cannot already be expressed with a `shell:`/`python:` step,
   an agent definition, `provider_options:`, or `defaults:`.

If a field fails the test, the answer is one of: put it on the **agent** (`agents:`), put it in
`provider_options:` (raw pass-through), or compute it in a **step**.

## Named smells (and their levers)

| Smell | What it looks like | Lever |
|---|---|---|
| Expression creep | "just add `contains()` to `when:`" | `when:`/`until:`/`each:` are plain Jinja expressions — the language is adopted *wholesale*; rayspec adds filters only under the filter policy below. Anything else → `python:` step that emits structured output and gate on that. |
| Composition metastasis | a second loop form, a second include form | One loop form (`loop:` with a body), one composition form (load-time `include:`). Dynamic fan-out is `each:`. Static parallelism is the DAG. |
| Workaround pressure | workflows copying 9 steps because a block can't be reused | `include:` with `with:`/`outputs:`; named agents in `.rayspec/agents/`. |
| Schema width | provider knobs as step fields | Step fields are the governance set only. Provider knobs live on agents; unknown ones in `provider_options`. |
| Implicit magic | silent `''` on a missing reference, auto-JSON detection | Strict: missing field / skipped producer / `None` / non-JSON all fail the consuming step with a message naming the fix. |

## Filter/test policy (Jinja)

rayspec ships every Jinja builtin plus exactly: `fromjson`, `regex_search`, `has_signal`.
A new filter or test is added only if it

- cannot be written in one line from builtins, **and**
- is pure, total and deterministic (no IO), **and**
- *shapes* data rather than *judges* it.

Otherwise it is a `python:` step.

## Capability discipline

Every provider declares a `ProviderCapabilities` record. Every YAML field that depends on provider
behaviour maps to exactly one capability, and the validator refuses (or, with
`defaults.on_unsupported: warn` / `--allow-unsupported`, warns about) a workflow that uses a feature
its resolved provider lacks. New provider features therefore enter as **capabilities first**, then as
agent fields, never as ad-hoc step fields.

## Concurrency discipline

The engine and providers use the anyio API on the asyncio backend only. Raw `asyncio` cancellation
primitives are banned by ruff (`flake8-tidy-imports` `banned-api`) because the Claude Agent SDK's
shielded subprocess cleanup and the Codex SDK's worker-thread model are only safe under
anyio-originated cancellation.

## Case law

| Request | Decision | Why |
|---|---|---|
| `on_reject: {prompt: …, max_attempts: n}` on approval gates | Rejected | A second loop form. Use `loop:` + `approve:` with `on_reject: continue`. |
| Auto-detect JSON in shell stdout | Rejected | `123`/`true`/`null` would silently change type. Use `output_schema:` or `\| fromjson`. |
| Auto shell-quoting of `{{ }}` in `shell:` bodies | Rejected in favour of env-var references | `shlex.quote` inside an already-quoted string is data-dependent breakage. `{{ x }}` renders as `${RAYSPEC_V<n>}` and bash's own quoting rules apply. |
| Loop exhaustion = success | Rejected (default `on_exhausted: fail`) | Loud failure principle; `continue` is explicit. |
| Per-step `provider:`/`model:` overrides | Rejected as step fields | Use `agent: {extends: name, model: …}` — the knob stays on the agent. |
| A sink that POSTs to ntfy/Slack/a webhook from the rayspec process | Rejected — sinks are **`exec:` only** | The engine may spawn a command; it may never open a socket. rayspec's claim that "the only network activity is what agents do through their SDKs and what workflow shell steps run themselves" is a guarantee, not a description. An `exec:` sink keeps it literally true, keeps timeouts/retries/proxies/TLS/outbound-secret handling out of the engine, and needs no dependency. `examples/notify_webhook` already delivers a webhook from a `shell:` step — that is the idiom. |
