# notify_webhook — a `secret: true` input, delivered through the environment only

One `prompt:` step drafts an announcement, one `shell:` step POSTs it to a webhook. The webhook
URL is an input declared `secret: true`: rayspec never writes its value anywhere under
`RAYSPEC_HOME` (`run.json` holds `"<secret>"`, `context.json` too, events and `plan`/`show` print
the placeholder) and hands it to the shell step **only** as `$RAYSPEC_INPUT_WEBHOOK_URL` (or
through that step's `env:` mapping). Naming it anywhere else — a prompt, `when:`, `outputs:`,
an `approve:` message — is a load-time error.

```
examples/notify_webhook/
├── .rayspec/workflows/notify_webhook.yaml   # the workflow (inputs.webhook_url is secret: true)
├── stubs.yaml                               # scripted answer for --dry-run
├── checks.yaml                              # what scripts/check_examples.py asserts
└── README.md
```

| Feature | Where |
|---|---|
| `secret: true` input (optional) | `inputs.webhook_url` |
| `RAYSPEC_INPUT_<NAME>` as the only way a secret reaches a step; unset ⇒ skipped inside the script | `notify` |
| `env:` mapping on a shell step (the other allowed place for a secret) | `notify` (`TEXT` carries a public value here) |
| `(secret)` marker in `rayspec plan` / `rayspec validate`, `<secret>` in `rayspec show` | see below |
| resume re-supplies the secret (`--input webhook_url=…` or `RAYSPEC_INPUT_WEBHOOK_URL`) | `rayspec resume\|approve\|reject` |

## Try it without credentials

```sh
cd examples/notify_webhook
rayspec validate                                                   # marks webhook_url (secret)
rayspec plan notify_webhook -i message="rayspec 1.0 is out" -i webhook_url=https://hooks.example.invalid/T000/B000
rayspec run notify_webhook -i message="rayspec 1.0 is out" --dry-run --stubs stubs.yaml
RAYSPEC_INPUT_WEBHOOK_URL=https://hooks.example.invalid/T000/B000 \
  rayspec run notify_webhook -i message="rayspec 1.0 is out" --dry-run --stubs stubs.yaml
```

`rayspec validate` prints the marker under the workflow line:

```
notify_webhook (.rayspec/workflows/notify_webhook.yaml): OK
  secret inputs: webhook_url (secret; env-only, never persisted)
1 workflow(s) validated, no errors
```

`rayspec plan` shows the placeholder, never the value:

```
inputs
  message = rayspec 1.0 is out  (string)
  webhook_url = <secret>  (string, secret)
```

Expected output of the dry run (the stub answers from `stubs.yaml`; shell steps are skipped in a
dry run, so `notify` succeeds with an empty output and `sent` is `false`):

```
▶ run 20260820-200129-wgsd started (notify_webhook)
✓ draft succeeded 8ms · 144 tok
✓ notify succeeded 0ms
■ run 20260820-200129-wgsd succeeded · 144 tok
outputs
name         ┃ value
━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
announcement │ rayspec 1.0 is out: declarative agent workflows on Claude and Codex, with resume.
sent         │ false
  tokens: 144 · run dir: ~/.rayspec/projects/<slug>/runs/20260820-200129-wgsd
```

Afterwards `grep -r hooks.example ~/.rayspec/projects/<slug>/runs/<run-id>` finds nothing, and
`rayspec show <run-id>` prints `inputs: {"message": "rayspec 1.0 is out", "webhook_url":
"<secret>"}`. Add `--exec-shell` to really run `notify`: without a webhook it prints
`no webhook_url — skipped` (exit 0); with one it POSTs the drafted text (needs `curl` and `jq`).

## Run it for real

```sh
rayspec run notify_webhook -i message="rayspec 1.0 is out" \
  -i webhook_url=https://hooks.slack.com/services/T000/B000/XXXX      # the value never hits the run store
RAYSPEC_INPUT_WEBHOOK_URL=https://hooks.slack.com/services/T000/B000/XXXX \
  rayspec run notify_webhook -i message="rayspec 1.0 is out"          # or from the environment
rayspec resume <run-id> -i webhook_url=https://…                      # secrets are re-supplied on every resume
```

Needs a logged-in `claude` CLI (or `ANTHROPIC_API_KEY`). What a step *prints* is its output and
is stored like any other output — `echo "$RAYSPEC_INPUT_WEBHOOK_URL"` would persist the URL in
`steps/notify/output.txt`; this step prints `sent` / `skipped` only. Agent (prompt) steps cannot
receive secrets in v1: there is no `{{ inputs.webhook_url }}` in a prompt, by design.
