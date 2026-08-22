<!-- Generated from docs/templating.md by scripts/gen_skill.py — do not edit here. -->
<!-- Canonical source: https://github.com/rayspec-labs/rayspec-py/blob/main/docs/templating.md -->
<!-- Sibling references in this directory: concepts.md · schema.md · templating.md · cli.md · providers.md · examples.md -->

# Templating and expressions

rayspec uses Jinja2 — adopted wholesale, in a sandboxed environment — for **templates** (prompts,
instructions, scripts, messages, `outputs:`, `with:`, `env:`, `cwd`) and for **expressions**
(`when:`, `until:`, `each:`). Every Jinja builtin filter and test is available, plus three
rayspec filters. Templates are compiled and their references checked at load time
(`rayspec validate`); rendering happens when the step runs.

## The context

| Root | Contents |
|---|---|
| `inputs.<name>` | the run's inputs (fixed per run; inside an `include:` body, the included workflow's inputs bound via `with:`) |
| `steps.<id>` | a *step view* of a visible, finished step (see below) |
| `run` | `id`, `workflow`, `workdir`, `artifacts_dir`, `state_dir`, `branch`, `base_branch`, `started_at` |
| `project` | `root` (where workflows are loaded from), `name`, `slug` |
| `env.<VAR>` | the process environment (after `.env` files were loaded) |
| `iteration` | inside a `loop:` body: `n` (1-based), `max`, `first`, `prev.<body id>` (the previous iteration's step view; undefined on the first iteration) |
| `each` | inside an `each:` body: `index` (0-based), `total` |
| `<as>` | inside an `each:` body: the current item (`item` by default) |

`steps.<id>` resolves innermost scope first (a body sees the outside; the outside never sees a
body's steps). Missing names produce an error *naming the fix* — e.g. `steps.review` from
outside a loop → "is inside loop 'build'; use steps.build.output.review".

### Step views: `steps.<id>.<attribute>`

| Attribute | Meaning |
|---|---|
| `output` | text, or the parsed value for `output_schema` / composites. Undefined (with a hint) when the step did not succeed or produced nothing — guard with `steps.x.status == 'succeeded'` or `\| default(...)` |
| `status` | `succeeded`, `failed`, `skipped`, … (plain string) |
| `ok` | `true` when succeeded; `false` for a failed (also tolerated) step. **Undefined (with a hint) for a skipped step** — it never answered; guard with `steps.x.status == 'succeeded'` or `\| default(false)` |
| `exit_code`, `stderr` | shell/python steps |
| `duration_s`, `cost_usd`, `usage` (`{input, cached_input, cache_write, output, reasoning}`) | when recorded |
| `session`, `model` | prompt steps |
| `approved` | approve steps |
| `iterations`, `converged` | loop steps |
| `items` | each steps: `[{index, item, status, output, error}]` |
| `id`, `kind`, `skip_reason`, `error`, `tolerated` | bookkeeping |

Attributes that are not set for a step are undefined-with-hint, so `| default(...)` and
`is defined` work on them. `.field` on a **text** output says "this step has no output_schema
(try | fromjson)".

### Attribute lookup on mappings

`inputs.items` is the input called `items` (item lookup first); only `items`/`keys`/`values`/`get`
fall back to the mapping methods. `{{ x.keys }}` without parentheses is an error (no
`<bound method>` ever lands in a prompt).

## Text templates

Prompts, instructions, `approve.message`, `stop.reason`, `cwd`, `outputs:`, `with:`, `env:`.

- `{{ x }}` renders: text as-is, booleans as `true`/`false`, numbers as text, lists/mappings as
  pretty JSON; `None`, undefined values and callables are errors ("use `| default(...)`").
- A template that is **exactly one `{{ expr }}`** keeps the expression's Python type. That is how
  `outputs:`/`with:`/`env:` pass structured values through; `env:` values are str-coerced
  afterwards (`None` is an error there too).
- `outputs:`, `with:` and `env:` are deep-rendered: strings are templates, lists and mappings are
  recursed, other scalars pass through.
- `trim_blocks`, `lstrip_blocks` and `keep_trailing_newline` are on.

## Shell bodies

Every `{{ expr }}` in a `shell:` body renders to an **environment-variable reference**
`${RAYSPEC_V<n>}`; the value is placed in the step's environment, never spliced into the script.
Bash's own quoting rules therefore apply, and a value such as `$(rm -rf /)` stays inert.

```yaml
- id: pr
  shell: |
    gh pr create --base "{{ inputs.base }}" --title "fix: #{{ inputs.issue }}" \
      --body "{{ steps.build.output.review }}"
```

renders as

```bash
gh pr create --base "${RAYSPEC_V1}" --title "fix: #${RAYSPEC_V2}" \
  --body "${RAYSPEC_V3}"
```

with `RAYSPEC_V1=main`, `RAYSPEC_V2=123`, `RAYSPEC_V3=<the review text>` exported.

Consequences:

- **Quote them**: `"{{ x }}"` is one word even when the value has spaces; bare `{{ x }}` splits.
- Single quotes and quoted heredocs (`<<'EOF'`) do **not** expand: `echo '{{ x }}'` prints the
  literal `${RAYSPEC_V1}`. Use double quotes or an unquoted heredoc.
- Values over 64 KiB spill to a file under the run's `tmp/` and render as `$(cat '<path>')`.
- Lists and mappings render as JSON text — pipe them to `jq`.
- Comment delimiters are `{{# ... #}}`, so `${#VAR}` is plain bash.
- Literal `{{` (Go templates, `printf '{{'`) → `{% raw %}docker ps --format '{{.ID}}'{% endraw %}`.
- `${{ x }}` is a lint error (GitHub-Actions syntax).
- `{% macro %}`, `{% call %}`, `{% filter %}` and `{% set x %}…{% endset %}` are compile
  errors in code bodies (their captured output would be substituted twice); use
  `{% set x = expr %}` and inline filters.

Inputs are also available without templating: `$RAYSPEC_INPUT_<NAME>` (see the
[environment](#environment-of-shell-and-python-steps)).

## Python bodies

`{{ expr }}` renders as a **Python literal** (`repr` of a JSON-like value: `str`, `int`,
`float`, `bool`, `None`, lists, dicts with string keys). Anything else is an error. The literal
is a Python *expression*: `data = {{ steps.fetch.output | fromjson }}` is right; putting
`{{ x }}` inside a quoted string puts the repr, quotes included, into that string. Oversized
literals spill to a JSON file and render as a `json.loads(Path(...).read_text())` call. Same
comment delimiters and the same rejected block constructs as shell.

The script runs as `sys.executable -` (stdin), or `uv run --no-project --with <dep>... python -`
when `deps:` is set. `cwd` defaults to `run.workdir`.

## Expressions: `when`, `until`, `each`

Bare Jinja expressions; `{{`/`{%` inside them is a lint error. Try one against a finished run
before you commit it: `rayspec eval <run> "steps.a.output | length" --step build[2]/implement`
([cli.md](cli.md#rayspec-eval)).

- `when` and `until` must evaluate to **exactly** a boolean: `steps.check.ok`,
  `steps.a.output.verdict == 'fix'`, `steps.list.output | length > 0`. A non-boolean result or an
  evaluation error fails the step (for `until`: fails the loop) with a hint.
- `each` must evaluate to a list (tuples are fine; a mapping is rejected — use `.values()` or
  `.items()`; JSON text needs `| fromjson`).
- `when:` on a step whose upstream was skipped: **both** `steps.x.output` and `steps.x.ok` fail
  loudly with the same hint ("step 'x' was skipped (when_false) — guard with
  `steps.x.status == 'succeeded'`"). A skipped step never answered, so `ok` is undefined rather
  than `false`; write `steps.x.status == 'succeeded'` to mean "ran and succeeded", or
  `steps.x.ok | default(false)` to keep the old reading. (Changed after 1.0.0 — before that
  `steps.x.ok` silently read `false` for a skipped step. In `RAYSPEC_CONTEXT`
  (`steps/<path>/context.json`) such a step's `ok` is `null`.)

## Filters and tests

All Jinja builtins (`default`, `length`, `join`, `tojson`, `lower`, `trim`, `replace`, `select`,
`map`, …) plus:

| Filter | Meaning |
|---|---|
| `fromjson` | parse JSON text (`steps.fetch.output \| fromjson`); structured input is an error ("access fields directly") |
| `regex_search(pattern, group=0)` | `re.search`; returns the group, or an undefined (usable with `\| default(...)` / `is defined`) when there is no match |
| `has_signal(name)` | `true` when a whole line equals `name` after stripping whitespace and `*`/`_`/backticks, or `<signal>name</signal>` appears anywhere; case-sensitive; "not DONE yet" is not a signal; structured input is an error |

`has_signal` is also a test: `{% if steps.review.output is has_signal('BUILD-CLEAN') %}`.

`tojson` serialises a whole context root as well — `{{ inputs | tojson }}`, `{{ steps | tojson }}`
— using the same conversion as `RAYSPEC_CONTEXT` (`steps/<path>/context.json`), so a script reads
the identical shape whichever of the two it is handed. A reference to something that is not
there still fails loudly rather than becoming `null`.

Growth policy ([constitution.md](https://github.com/rayspec-labs/rayspec-py/blob/main/docs/constitution.md)): a filter is added only if it is not a
one-liner from builtins, is pure/total/deterministic and *shapes* rather than *judges* data.
Anything else is a `python:` step.

## Environment of shell and python steps

| Variable | Value |
|---|---|
| `RAYSPEC_INPUT_<NAME>` | every defined input, upper-cased name; scalars as text (`true`/`false`), lists/objects as JSON |
| `RAYSPEC_RUN_ID`, `RAYSPEC_WORKDIR`, `RAYSPEC_ARTIFACTS_DIR`, `RAYSPEC_STATE_DIR` | the run id, working directory, `<run dir>/artifacts`, the run directory |
| `RAYSPEC_CONTEXT` | path to `steps/<path>/context.json`, a JSON dump of the step's template context **without the `env` root** (the script already has the real environment; API keys must not end up in the run directory) — step views as objects, undefined as `null`; the `jq` escape hatch |
| `RAYSPEC_STEP_PATH` | the step's record path (`build[2]/check`) |
| `RAYSPEC_V<n>` | the `{{ }}` slots of a shell body |
| plus | the process environment and the step's own `env:` (templated, str-coerced) |

```yaml
- id: summary
  shell: jq -r '.steps.review.output.summary' "$RAYSPEC_CONTEXT"
```

## Inputs

Declared under `inputs:` ([schema.md](schema.md#inputs)). At run time a value comes from, in
order: `--input name=value` (repeatable) > `--inputs-file file.yaml|json` > the environment
variable `RAYSPEC_INPUT_<NAME>` > the declared `default`. Text values are coerced by type:

| type | coercion of text |
|---|---|
| `string` | as-is |
| `integer` / `number` | parsed (`"12"` → `12`); otherwise an error |
| `boolean` | `true/false/yes/no/1/0` (case-insensitive) |
| `array` | a JSON array (`'["a","b"]'`), or one element per repeated `--input tags=a --input tags=b` (elements coerced via `items.type`) |
| `object` | a JSON object |

Repeating a non-array `--input` is an error; unknown names get a did-you-mean; every missing
required input is reported in one go; the final values are validated against the compiled JSON
Schema (`enum`, `items`, `properties`). Inputs are fixed for the life of a run: `--resume` refuses
`--inputs-file` and any `--input` that is not a secret input. Inputs (like outputs and
transcripts) are persisted in clear text in the run directory — **unless declared `secret:
true`**, in which case the value is never stored and reaches `shell:`/`python:` steps through
`RAYSPEC_INPUT_<NAME>` (or the step's `env:` mapping) only; every other template that names it —
or uses `inputs` as a whole (`inputs | tojson`, `inputs.get(...)`, `inputs.items()`) — is a
load-time error, and every resume entry (`resume`/`approve`/`reject`/`run --resume`) re-obtains
it from `--input name=value` / `RAYSPEC_INPUT_<NAME>`. In template contexts (`inputs.<name>`,
`context.json`) a secret stands as the string `"<secret>"`. See
[schema.md § Secret inputs](schema.md#secret-inputs).

## Errors you will see

Every problem while rendering becomes a step failure (`error.type: render`, `when`, `until`,
`each`, `with`, `outputs`) whose message names the fix — `value is null; use | default(...)`,
`step 'x' was skipped (upstream_failed) — guard with steps.x.status == 'succeeded'`,
`expression ... must evaluate to true/false, got str 'yes'; compare explicitly`. At load time
`rayspec validate` reports syntax errors and bad references with `file:line`.
