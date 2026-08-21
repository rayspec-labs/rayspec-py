# Security policy

rayspec runs coding agents and executes the steps a workflow file declares. That makes its threat
model unusual, so please read [Threat model](#threat-model) before you decide whether what you
found is a bug or a feature — it will save us both a round trip.

## Supported versions

| Version    | Supported                                                        |
| ---------- | ---------------------------------------------------------------- |
| latest 1.x | Yes — fixes land on `main` and ship in the next 1.x release       |
| older 1.x  | No — upgrade; fixes are released forward, not backported          |
| < 1.0      | No — 1.0.0 is the first release                                   |

There is no long-term-support branch, so "supported" means the latest 1.x. `rayspec version`
prints what you are running.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting:

**<https://github.com/rayspec-labs/rayspec-py/security/advisories/new>**

The report is visible to the maintainer only and opens a draft advisory we can work in. Please do
not open a public issue, a discussion or a pull request for anything you believe is exploitable:
the tracker is public, and for this project a reproduction *is* a working exploit.

If that form is not available to you — GitHub offers private reporting on public repositories only,
and a maintainer can switch it off — open an issue saying that you have a security report and how
to reach you, and nothing else: no reproduction, no affected version, no details. I will open the
private thread from there.

Useful in a report, roughly in order:

- the output of `rayspec doctor` and `rayspec version` (doctor prints sources, paths and versions —
  never a secret value);
- the smallest workflow file that reproduces it, plus the exact command line;
- what you expected, what happened, and what an attacker gets out of it;
- OS, Python version and which provider was involved.

If you already have a fix, attach the patch to the report rather than opening a pull request.

## Disclosure timeline

- **3 working days** — I acknowledge the report.
- **10 working days** — you get an assessment: accepted, with a severity and a fix plan, or
  declined, with the reason.
- **90 days** — the coordinated-disclosure window. I aim to publish a fix and an advisory well
  inside it; if a fix will take longer I will tell you why and when, and you are free to disclose
  once the 90 days are up either way. If something is being exploited in the wild, we move faster
  and agree on publication together.

One person maintains this project. The dates above are real, but they are one person's real — if a
thread goes quiet, ping it.

## Threat model

Three facts decide nearly every triage call.

**1. Declared execution is the product.** A workflow's `shell:` and `python:` steps run on your
machine, in your shell and your interpreter, with the environment rayspec was launched with. Agent
steps get whatever filesystem and tool access the workflow grants them (`access: read-only |
workspace-write | full`). Running a workflow you have not read is exactly as safe as running a
script you have not read. A malicious workflow file is not a vulnerability in rayspec, for the same
reason a malicious shell script is not a vulnerability in your shell.

**2. `secret: true` inputs are the real surface.** A secret input is never persisted, never
printed, and reaches `shell:`/`python:` steps through `RAYSPEC_INPUT_<NAME>` or those steps' own
`env:` — and nowhere else. Delivery is run-wide, not per-step: every `shell:`/`python:` step of
the run receives every secret the run was given, included bodies too, whether or not it names one
(`RunContext.secret_env` ignores the scope on purpose), so including a body you have not read is a
decision about your credentials. Every other placement (a prompt body, an agent's instructions,
a `when:`/`until:`/`each:` expression, `outputs:`, `cwd:`, an approval message, an include
`with:`) is refused with a load-time error naming the step and field, and a redactor sits under those
refusals as a net for values a step prints itself. The rules are documented in
[docs/schema.md § Secret inputs](docs/schema.md#secret-inputs). Any path by which a `secret: true`
input reaches a prompt, a template, an expression, an output, an event, the console or the run
store **is** a vulnerability.

**3. The engine itself opens no network sockets.** Every packet rayspec is responsible for comes
from an agent SDK talking to its vendor, or from a step you wrote. Notification sinks spawn a
command rather than making a request for exactly this reason
([docs/constitution.md](docs/constitution.md)). A rayspec process connecting anywhere on its own —
telemetry, an update check, a workflow shipped somewhere — is a report.

### In scope

- Any leak of a `secret: true` input or a resolved `secrets:` entry into `run.json`,
  `context.json`, `events.jsonl`, `stream.jsonl`, an output file, a prompt, `--json` output or the
  console — including a writer that reaches the run directory without passing through the redactor.
- A placement rule that fails open: a template position that should be refused at load time and is
  not, or a resume path that stores a re-supplied secret.
- Permissions on the run store: every directory and file rayspec creates for a *run* under
  `$RAYSPEC_HOME` is `0700`/`0600` regardless of umask — `runs/<id>/`, `steps/<path>/`, `locks/`,
  `run.json`, `events.jsonl`, `stream.jsonl`, `context.json`, `prompt.txt`, `output.txt|json`,
  `stdout.log`/`stderr.log` and the workdir lock files
  ([docs/runs-and-resume.md § Security notes](docs/runs-and-resume.md#security-notes)). The one
  documented exception is `worktrees/`: rayspec creates that directory `0700`, but the checkout
  inside it belongs to git and keeps your umask — and no run data is written there. A file with
  run data created group- or world-readable, or a writer that follows a symlink out of the store,
  is a bug worth reporting privately.
- `rayspec test` executing a `shell:`/`python:` body without `--exec-shell`. That flag exists so
  that pointing `rayspec test` at a checkout you have not read cannot execute anything
  ([docs/testing.md § Real shell steps](docs/testing.md#real-shell-steps)).
- An inspection command (`doctor`, `validate`, `plan`, `workflows`, `agents`, `runs`, `show`,
  `logs`, `explain`, …) loading a checkout's `.rayspec/.env` or otherwise running something from a
  repository you only looked at.
- Terminal-escape or control-character injection through agent output, step output or input values
  anywhere except `rayspec logs --raw`, which is documented as the unescaped escape hatch.
- `rayspec cancel` signalling a process that is not the run it names.
- A path traversal that lets a run write outside its workdir and its run directory — through a run
  id, a step path, an include path or any other name rayspec turns into a filesystem path.
- Anything shipped from this repository that executes unexpectedly: the packaged agent skill, the
  generated JSON schemas, provider discovery through entry points.

### Out of scope

- A workflow file, an agent definition or a `.rayspec/config.yaml` doing damage when you run it —
  see fact 1. The same goes for a checkout's `.rayspec/.env`: `run`, `resume`, `approve` and
  `reject` apply it and say so on stderr, inspection commands never do, and `rayspec doctor` lists
  the file so you can read it first.
- An agent doing something destructive inside the access a workflow granted it. `access:`,
  `tools:`, `approve:` gates and worktree isolation are the controls; widening them is a choice.
- A secret your own step printed. Redaction is exact-match and best effort: it catches the value,
  not a transformation of it, and values shorter than four characters are not redacted at all (the
  run warns when that happens). See
  [docs/schema.md § Redaction](docs/schema.md#redaction-exact-match-best-effort).
- Loose permissions on a `~/.rayspec` that existed before rayspec did. Directories rayspec did not
  create are never re-chmodded; `chmod -R go-rwx ~/.rayspec` is yours to run.
- Vulnerabilities in Claude Code, the Codex CLI, their SDKs, or a model's behaviour — report those
  to their vendors. If rayspec's *use* of them makes things worse (we hand them something we should
  not, we relax a sandbox you did not ask us to relax), that one is ours.
- Resource exhaustion from a workflow you wrote yourself — a wide `each:`, a long `loop:`, a
  runaway agent. `defaults.budget_usd` and `defaults.max_tokens` are the circuit breakers.
- Scanner output with no exploit path.

## While you are testing

Test against your own machine, your own repositories and your own provider accounts. Do not pull in
third parties, and do not use a finding to reach data that is not yours.

## Credit

Reporters are credited in the advisory and in `CHANGELOG.md` unless they prefer not to be. There is
no bug bounty — this is a one-person project, and I would rather be honest about that than imply a
budget that does not exist.
