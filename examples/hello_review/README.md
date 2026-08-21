# hello_review — the smallest useful workflow

One `prompt:` step on a read-only Claude agent, two typed inputs (a string and an enum), two
workflow `outputs:` (one of them shaped with `regex_search`), a tier model (`small` → `haiku`).

```
examples/hello_review/
├── .rayspec/workflows/hello_review.yaml   # the workflow
├── stubs.yaml                             # scripted answer for --dry-run
├── checks.yaml                            # what scripts/check_examples.py asserts
└── README.md
```

## Try it without credentials

```sh
cd examples/hello_review
rayspec validate                                   # schema, graph, references, capabilities
rayspec plan hello_review -i target=src/           # resolved agents/models, step order, inputs
rayspec run hello_review -i target=src/ --dry-run --stubs stubs.yaml
rayspec run hello_review -i target=src/ --dry-run --stubs stubs.yaml --verbose   # + step starts
rayspec run hello_review -i target=src/ --dry-run --stubs-init my_stubs.yaml     # scaffold a stub file
rayspec validate --root examples/hello_review      # from anywhere: --root picks the project
```

`--dry-run` swaps every provider for the scripted stub (`stubs.yaml` keys are step paths), skips
shell/python steps and auto-approves gates. Expected output of the run:

```
▶ run 20260820-175834-cuqy started (hello_review)
✓ review succeeded 7ms · 910 tok
■ run 20260820-175834-cuqy succeeded · 910 tok
outputs
name    ┃ value
━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
review  │ - `target` is only read, never validated — fine for a review tool.
        │ - Consider a `--focus` default in the docs.
        │ VERDICT: ok
        │
verdict │ ok
  tokens: 910 · run dir: ~/.rayspec/projects/<slug>/runs/20260820-175834-cuqy
```

Without `--stubs` the stub answers `[stub] <prompt…>`, so `verdict` falls back to `unknown`
(that is the second scenario in `checks.yaml`).

## Run it for real

```sh
rayspec run hello_review -i target=src/rayspec/loader -i focus=security
rayspec run hello_review --inputs-file inputs.yaml --json | tail -n 1 | jq .   # the summary object (status, exit_code, outputs, run_dir)
rayspec run hello_review --inputs-file inputs.yaml --json | jq -c 'select(.type == "run.finished")'   # the run.finished event (second-to-last line)
```

`--json` prints one JSONL event per line on stdout and the summary object as the **last** line
(`run.finished` is the line before it); warnings go to stderr. For example:

```
$ rayspec run hello_review -i target=src/ --dry-run --stubs stubs.yaml --json 2>/dev/null | tail -n 2
{"type":"run.finished","run_id":"20260820-175833-36hy","ts":"2026-08-20T17:58:33.938189Z","step_path":null,"data":{"status":"succeeded","reason":null,"usage":{"input":850,"cached_input":0,"cache_write":0,"output":60,"reasoning":0},"cost_usd":null,"outputs":{"review":"- `target` is only read, never validated — fine for a review tool.\n- Consider a `--focus` default in the docs.\nVERDICT: ok\n","verdict":"ok"}}}
{"run_id": "20260820-175833-36hy", "status": "succeeded", "exit_code": 0, "reason": null, "outputs": {"review": "- `target` is only read, never validated \u2014 fine for a review tool.\n- Consider a `--focus` default in the docs.\nVERDICT: ok\n", "verdict": "ok"}, "usage": {"input": 850, "cached_input": 0, "cache_write": 0, "output": 60, "reasoning": 0}, "cost_usd": null, "run_dir": "~/.rayspec/projects/<slug>/runs/20260820-175833-36hy", "workspace": {"isolation": "none", "workdir": "…/examples/hello_review", "branch": "main"}, "pause": null}
```

Needs a logged-in `claude` CLI (or `ANTHROPIC_API_KEY`). `isolation: none` keeps the run in place
(nothing is written). Runs are stored under `~/.rayspec/projects/<slug>/runs/<run-id>/`.
