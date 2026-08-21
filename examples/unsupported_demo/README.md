# unsupported_demo — the capability error and how to downgrade it

Every provider declares a `ProviderCapabilities` record (`rayspec providers`). A workflow that
uses a feature its resolved provider lacks is refused at validate time with a four-line error that
names the field, the capability and the fix. This example asks a **Codex** agent for `max_turns`
and a `tools.deny` of the `edit` group — both governed only by Claude.

```sh
cd examples/unsupported_demo
rayspec validate                 # exit 2
```

```
unsupported_demo (.rayspec/workflows/unsupported_demo.yaml): FAILED
errors:
  - unsupported: agents.fixer.max_turns = 40
      provider 'codex' does not support `max_turns` (capability max_turns=False)
      fix: remove it, use a provider that supports it (claude, stub), or set defaults.on_unsupported: warn / --allow-unsupported
      at .rayspec/workflows/unsupported_demo.yaml:18
  - unsupported: agents.fixer.tools.deny = edit
      provider 'codex' does not support `edit tools` (capability tool_groups=['web'])
      fix: remove it, use a provider that supports it (claude, stub), or set defaults.on_unsupported: warn / --allow-unsupported
      at .rayspec/workflows/unsupported_demo.yaml:19
unsupported_warn (.rayspec/workflows/unsupported_warn.yaml): OK
warnings:
  - unsupported: agents.fixer.max_turns = 40
      …
      at .rayspec/workflows/unsupported_warn.yaml:18
  - unsupported: agents.fixer.tools.deny = edit
      …
      at .rayspec/workflows/unsupported_warn.yaml:19
2 workflow(s) validated, 1 with errors (2 error(s))
```

(`rayspec validate` checks every workflow of the project, so the `unsupported_warn` variant from
step 2 below is listed too — as warnings.)

`rayspec run unsupported_demo --dry-run` is refused the same way (capabilities are checked against
the real providers before the stub swap). Three ways out:

1. `--allow-unsupported` on `validate` / `plan` / `run` → warnings, the fields are ignored:
   ```sh
   rayspec run unsupported_demo --dry-run --stubs stubs.yaml --allow-unsupported
   ```
2. `defaults: {on_unsupported: warn}` in the workflow — `unsupported_warn.yaml` is that variant:
   ```sh
   rayspec validate unsupported_warn                        # exit 0, two warnings
   rayspec run unsupported_warn --dry-run --stubs stubs.yaml
   ```
3. Move the knobs to a provider that has them (`provider: claude`) or drop them.

All three scenarios are asserted by `checks.yaml` (`validate: error`, `allow_unsupported: true`,
and the `unsupported_warn` workflow).
