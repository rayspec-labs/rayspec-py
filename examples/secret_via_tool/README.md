# secret_via_tool — give an agent a capability, not a credential

The honest way to let an agent act on a private system: **a `shell:`/`python:` step (or an MCP
server) holds the credential and exposes a capability; the agent only ever sees the result.**
The transcript, the run store and the console then contain no credential at all — not because a
redactor scrubbed it, but because it was never there.

```
examples/secret_via_tool/
├── .rayspec/config.yaml                       # secrets: GITHUB_TOKEN ← env RAYSPEC_EXAMPLE_GITHUB_TOKEN
├── .rayspec/workflows/secret_via_tool.yaml    # the tool step + the agent step
├── stubs.yaml                                 # scripted answer for --dry-run
├── checks.yaml                                # what scripts/check_examples.py asserts
└── README.md
```

| Feature | Where |
|---|---|
| `secrets:` block in `config.yaml` (`env` source, `required: false`) | `.rayspec/config.yaml` |
| a config secret delivered as `$GITHUB_TOKEN` to a `shell:` step — and to nothing else | `open_issues` |
| `output_schema:` turning the tool's stdout into structured data | `open_issues` |
| the agent consuming the *capability result*, never the key | `triage` |
| `when:` skipping the agent when the tool has no credential | `triage` |

## Try it without credentials

```sh
cd examples/secret_via_tool
rayspec run secret_via_tool --dry-run --stubs stubs.yaml     # no token, no network, no model call
rayspec doctor                                               # lists the source, never the value
```

`rayspec doctor` prints a row like

```
secrets   ok   GITHUB_TOKEN ← env RAYSPEC_EXAMPLE_GITHUB_TOKEN (absent, optional) · redact detectors: off (default)
```

## Try it for real

```sh
export RAYSPEC_EXAMPLE_GITHUB_TOKEN=ghp_…            # or: use a file:/cmd: source instead
cd examples/secret_via_tool
rayspec run secret_via_tool --input repo=owner/name
grep -r "$RAYSPEC_EXAMPLE_GITHUB_TOKEN" ~/.rayspec   # nothing
```

Swap the source without touching the workflow:

```yaml
secrets:
  GITHUB_TOKEN: { file: ~/.secrets/github_token }        # must be chmod 600 or tighter
  GITHUB_TOKEN: { cmd: "op read op://private/github/token" }   # 1Password, pass, security find-…
```

## Why not just put the token in the prompt?

Because a prompt is *stored*. rayspec refuses it at load time: naming a `secret: true` input in
a prompt body, an expression, `outputs:` or a prompt step's `env:` is a validation error, not a
warning. See [docs/schema.md § Secret inputs](https://github.com/rayspec-labs/rayspec-py/blob/main/docs/schema.md#secret-inputs).

And a `prompt:` step's `env:` is refused for secrets even though the schema allows `env:` there,
because one of the two supported CLIs writes the child's environment to a `0644` file outside
the run store — the verification and its evidence are in
[docs/providers.md § Giving an agent a secret](https://github.com/rayspec-labs/rayspec-py/blob/main/docs/providers.md#giving-an-agent-a-secret-tools-not-prompts).

## The MCP variant

The same shape with an MCP server instead of a shell step: the server process holds the
credential and publishes one narrow tool. Put the credential in `~/.rayspec/.env` (which
`run`/`resume`/`approve`/`reject` load into the environment the adapter inherits) or in the
server's own launcher — never in a rayspec template:

```yaml
agents:
  triager:
    provider: claude
    mcp:
      github:
        transport: stdio
        command: my-github-mcp          # reads GITHUB_TOKEN from its own environment
```

The rule is the same either way: **the credential never appears in a template.**
