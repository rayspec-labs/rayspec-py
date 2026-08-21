# Isolation: worktrees, `--repo`, projects

Agents edit files. By default rayspec keeps those edits away from your checkout.

## Worktree by default

For a workflow with `isolation: worktree` (the default) run inside a git repository, `rayspec run`
creates a **git worktree** before the first step:

```
git worktree add --no-track -b rayspec/<workflow>-<shortid> \
    ~/.rayspec/projects/<slug>/worktrees/<workflow>-<shortid> <base>
```

- `<shortid>` is the last segment of the run id (`…-ikd7` → `review-ikd7`); on a name collision
  the full run id is used;
- `<base>` is `--base` if given, else the **current branch** of the checkout (`HEAD` when detached;
  `base_branch` is then `null`); `--base` must be an existing branch, tag or commit;
- steps run in the worktree: `run.workdir` is its path, `cwd:` of shell/python steps defaults to
  it, and agents get it as their working directory; workflows, agents and prompt files are still
  loaded from `project.root` (your checkout);
- `run.json` records `workspace: {isolation, workdir, branch, base_branch, base_sha, head_sha}`;
  `base_sha` is the commit the worktree was created from and `head_sha` the **tip of the run
  workdir at the last record write** — refreshed when the run pauses, when it ends and when a
  resume starts, so after the agent commits `rayspec show` prints that commit as `head` (they
  are equal only while nothing has been committed); a `workspace.created` event is emitted and
  the console prints the path and branch;
- the worktree is **kept** after the run (succeeded or not) so you can inspect, push or merge the
  branch; `rayspec worktrees clean` removes it later.

### The branch lives in the worktree

The run branch `rayspec/<workflow>-<shortid>` is **checked out in the worktree**, not in your
clone. Git allows a branch to be checked out in one working tree at a time, so
`git checkout rayspec/<workflow>-<shortid>` in your main checkout fails with
`fatal: 'rayspec/…' is already used by worktree at '…/worktrees/<workflow>-<shortid>'` — that is
git being correct, not a broken run. The run summary therefore prints where the worktree is and
what to do with it:

```
  worktree: ~/.rayspec/projects/<slug>/worktrees/review-ikd7 (branch rayspec/review-ikd7, checked out there)
  hint: cd ~/.rayspec/projects/<slug>/worktrees/review-ikd7 · rayspec worktrees list|clean · git worktree remove ~/.rayspec/projects/<slug>/worktrees/review-ikd7
```

- **use it**: `cd <worktree>` and commit/push from there (`git push -u origin rayspec/…`), or
  merge/cherry-pick the branch from your clone (`git merge rayspec/…`, `git log rayspec/…`,
  `git diff main...rayspec/…` all work without checking it out);
- **list/clean**: `rayspec worktrees list` shows every run worktree of the project with age,
  dirty/merged state; `rayspec worktrees clean` removes the merged, clean ones (see below);
- **remove one by hand**: `git worktree remove <worktree>` (add `--force` when it is dirty), then
  `git branch -D rayspec/…` if you do not want the branch either. To get the branch into your
  clone instead, remove the worktree first — then `git checkout rayspec/…` works.

Opt out per workflow with `isolation: none`, per run with `--no-worktree`; force a worktree for an
`isolation: none` workflow with `--worktree`. A directory that is not a git repository always runs
in place (a notice is printed); a repository without commits cannot host a worktree (error with a
hint to commit or use `--no-worktree`). `--dry-run` runs in place unless `--exec-shell` is given.
A `--base` given for an in-place run is ignored and reported.

Templates see the difference as `run.workdir` (the worktree or the root) vs `project.root`.

## `--repo`

`rayspec run <wf> --repo <source>` runs against another project. `<source>` is resolved in this
order:

1. an explicit path form (`./x`, `/abs/x`, `~/x`, anything with a path separator) → that
   checkout becomes `project.root` (workflows are loaded from its `.rayspec/`); worktree by default;
2. a **registered project name** (`rayspec projects add`) → its source (path or URL) and its
   default `base`;
3. a **git URL** (`https://…`, `git@host:owner/repo.git`, `ssh://…`) → a **bare** clone under
   `~/.rayspec/projects/<slug>/source.git` (created on first use, `git fetch --prune` on every
   later use; nothing is ever checked out in it). URL sources **always** run in a worktree
   (`--no-worktree` is ignored with a notice); the worktree is the project root, so the repo's own
   `.rayspec/workflows/` are what you can run; the base is `--base` (mapped to `origin/<base>` for a
   bare branch name), else the registered base, else `origin/HEAD`;
4. a bare name that is an existing directory → like 1;
5. otherwise an error listing the registered projects.

Whatever the form, the run is stored under the **source's** project slug — the same
`~/.rayspec/projects/<slug>/` that holds its `source.git`, `worktrees/` and `locks/` (a `file://`
or otherwise unrecognised URL gets the stable `local/<name>-<sha1(url)[:8]>` slug of the URL):
every run of one repository lands in one project, so `rayspec runs --all`, `show` and the
per-workdir locks see them together. `rayspec worktrees list|clean --repo <source>` inspect that
project's worktrees.

## Registered projects

```
rayspec projects add myapp git@github.com:me/myapp.git --base main
rayspec projects add local ./checkouts/other          # stored absolute; must exist
rayspec projects list [--json]
rayspec projects remove myapp                          # clones and worktrees are kept
```

stored in `~/.rayspec/config.yaml`:

```yaml
projects:
  - { name: myapp, source: git@github.com:me/myapp.git, base: main }
```

Names match `[A-Za-z0-9][A-Za-z0-9._-]*`; adding an existing name updates it in place.

## Cleaning up

```
rayspec worktrees list [--root DIR] [--repo SOURCE] [--json]
rayspec worktrees clean [--older-than 7d] [--merged] [--merged-into REF] [--force] [--dry-run] [--json]
```

`clean` is safe by default: it removes only worktrees whose branch is merged into `origin/HEAD`
(else `HEAD`; `--merged-into` overrides), that are clean and not locked; everything else is listed
as skipped with the reason. `--force` also removes unmerged (committed work), dirty and locked
worktrees (`git worktree remove --force --force` + `git branch -D`). Age is the mtime of the
worktree's `.git` pointer (a `git worktree move|repair` resets it).

## Locks

The workspace layer ships a per-workdir lock (`fcntl.flock` on
`~/.rayspec/projects/<slug>/locks/<sha1(workdir)>.lock`, holding `{run_id, pid, workdir,
started_at}` while held, released automatically when the process dies) so two runs never share a
working directory. The engine acquires it before the run record is touched (`rayspec run`,
`resume`, `approve`, `reject`), releases it on every final status — a paused run holds no lock —
and takes it again on resume. A second `rayspec run --no-worktree` (or resume) in the same
directory fails with exit 2: `<workdir> is already locked by run <id> (pid <n>)`. Pure dry runs
(no `--exec-shell`) touch nothing and take no lock; without `fcntl` (Windows) runs are unguarded.
`rayspec cancel` of a paused run clears a stale lock file best effort (`lock_released` in its
`--json`); `rayspec worktrees clean` unlinks the lock file of every worktree it removes (a lock a
live run still holds is left alone), so `locks/` does not collect one file per historical
worktree.

### Projects below the git top level

When `.rayspec/` lives in a sub-directory of a repository (`packages/foo/.rayspec` in a
monorepo), the project root is that sub-directory (workflows/agents/prompts load from there) while
the worktree checks out the whole repository; the run's `workdir` — and therefore every step's
default `cwd`, `run.workdir` and the approval panel's `git diff` — is the matching
`<worktree>/packages/foo`.

## Slugs and the project directory

| Remote | Slug |
|---|---|
| `git@github.com:rayspec-labs/rayspec-py.git` | `github.com/rayspec-labs/rayspec-py` |
| `https://gitlab.com/team/app` | `gitlab.com/team/app` |
| `ssh://git@host:2222/owner/repo` | `host/owner/repo` |
| no `origin` / not a repo | `local/<dirname>-<sha1(abspath)[:8]>` |

`~/.rayspec/projects/<slug>/` holds `runs/`, `worktrees/`, `source.git/` and `locks/` for that
project.

### Permissions

Everything rayspec creates under `~/.rayspec` is private regardless of your umask: directories
`0700` (`~/.rayspec` itself when rayspec creates it, `projects/<slug>/`, `runs/`, `worktrees/`,
`locks/`, the parent of `source.git/`) and the files rayspec writes `0600` (`run.json`, events,
step outputs and logs, lock files, `config.yaml` — also when `rayspec projects add|remove`
rewrites it). Directories that already exist — a `~/.rayspec` you made by hand — are never
re-chmodded. What git creates keeps git's modes: the worktree checkout content under
`worktrees/<name>/` (your files, as committed, under your umask) and the bare `source.git/`.
