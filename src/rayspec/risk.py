# SPDX-License-Identifier: Apache-2.0
"""Static risk analysis of a resolved workflow — what a run would be *allowed* to do.

This is the analysis behind ``rayspec plan --risk``. It answers one question: before anyone
approves this run, what can it reach? Agents that can leave the workspace, shell bodies that
push, merge, delete or fetch code, MCP servers that start a local command or talk to a remote
one, steps that work outside the workspace, and gates that anything at all could waive.

**It reads. It never runs.** No step body is executed, no provider is contacted, no socket is
opened, no file is written — the whole report is derived from the workflow document as the
loader resolved it. That is what makes it safe to run against a workflow you have not read yet,
which is exactly when you need it. The cost of reading rather than running is that the analysis
is textual: a body is matched as it is written, before templates are rendered, so a command
assembled at run time is not seen. The report says what a workflow *declares*, not everything it
could conceivably do.

Module boundary: pure functions over a :class:`~rayspec.loader.ResolvedWorkflow`. Rendering
belongs to the CLI; deciding what to do about a finding belongs to a human.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any, Final

from rayspec.engine.approval_classes import ApprovalClasses
from rayspec.loader import ResolvedWorkflow
from rayspec.schema import ApproveStep, PythonStep, ShellStep, StepModel, ToolsSpec

#: Severities, most serious first — the order the report is printed in.
SEVERITIES: Final = ("high", "medium", "low")

_SEVERITY_RANK: Final = {name: i for i, name in enumerate(SEVERITIES)}

#: How much of a matching line is quoted as evidence.
_EVIDENCE_CAP: Final = 120


@dataclass(frozen=True, slots=True)
class Finding:
    """One thing a reviewer should know before approving a run.

    ``where`` is the step path, agent name or workflow the finding belongs to, ``detail`` is
    the evidence (the matching line, the declared value) and ``advice`` says what to do about it.
    """

    severity: str
    category: str
    where: str
    detail: str
    advice: str

    def to_json(self) -> dict[str, str]:
        """The ``--json`` shape of one finding."""
        return {
            "severity": self.severity,
            "category": self.category,
            "where": self.where,
            "detail": self.detail,
            "advice": self.advice,
        }


@dataclass(frozen=True, slots=True)
class _Rule:
    """A pattern over a step body, and what a match means.

    ``ignore_raw`` skips matches inside a ``{% raw %}`` block — right for the rule that reports
    *templating*, wrong for every other rule, because a shell still runs what a raw block holds.
    """

    category: str
    severity: str
    pattern: re.Pattern[str]
    advice: str
    ignore_raw: bool = False


def _rule(
    category: str, severity: str, source: str, advice: str, *, ignore_raw: bool = False
) -> _Rule:
    return _Rule(category, severity, re.compile(source, re.IGNORECASE), advice, ignore_raw)


#: A ``{% raw %}…{% endraw %}`` block: text the template engine hands through untouched.
_RAW_BLOCK: Final = re.compile(r"\{%\s*raw\s*%\}.*?\{%\s*endraw\s*%\}", re.DOTALL)


#: Patterns applied to every ``shell:`` and ``python:`` body. A ``python:`` body reaches the
#: shell through ``subprocess``, so the shell patterns are worth running over it too.
_BODY_RULES: Final[tuple[_Rule, ...]] = (
    _rule(
        "shell-pipe-to-shell",
        "high",
        r"\|\s*(?:sudo\s+)?(?:sh|bash|zsh|ksh|python[0-9.]*)\b(?!\s+-[cm]\b)",
        "code downloaded at run time is executed; fetch it to a file, check it, then run it",
    ),
    _rule(
        "shell-push",
        "high",
        r"\b(?:git\s+push|git\s+merge|git\s+rebase|gh\s+pr\s+merge)\b",
        "a shared branch is changed; put the step behind an approve: gate with a class the "
        "policy marks allow_yes: false",
    ),
    _rule(
        "shell-force",
        "high",
        r"--force(?:-with-lease)?\b|\bgit\s+reset\s+--hard\b|\bgit\s+clean\s+-[a-z]*f",
        "work that is not committed, or history other people have, can be destroyed; review the "
        "body and gate the step",
    ),
    _rule(
        "shell-delete",
        "high",
        r"\brm\s+-[a-z]*r[a-z]*f\b|\brm\s+-[a-z]*f[a-z]*r\b|\bgit\s+branch\s+-D\b"
        r"|\bfind\b[^\n]*\s-delete\b",
        "files are deleted recursively without asking; make sure the path cannot resolve "
        "outside the workspace",
    ),
    _rule(
        "shell-publish",
        "high",
        r"\b(?:npm\s+publish|yarn\s+publish|twine\s+upload|cargo\s+publish|gem\s+push"
        r"|docker\s+push|gh\s+release\s+create)\b",
        "the run can publish an artefact the world can install; this is the step to gate with "
        "an approval class the policy locks",
    ),
    _rule(
        "shell-privilege",
        "high",
        r"\bsudo\b|\bchmod\s+(?:-R\s+)?777\b|\bchown\b",
        "the step changes ownership or runs as another user; a workflow should not need to",
    ),
    _rule(
        "outside-workspace",
        "high",
        r"(?:^|\s)~/|\$HOME\b|\bPath\.home\(\)|\bos\.path\.expanduser\b"
        r"|\bcd\s+/(?:etc|usr|var|bin|opt|Library|System)\b"
        # an absolute redirect target, but not the two every script writes to
        r"|(?:^|\s)>>?\s*/(?!dev/|tmp/)"
        # any absolute path token — a READ leaves the workspace too (`cat /etc/passwd`)
        r"|(?:^|[\s\"'])/(?!dev\b|tmp\b)[A-Za-z0-9_.]"
        # and a relative escape out of the step's working directory
        r"|(?:^|[\s\"'=(])\.\./",
        "the step reads or writes outside the workspace, so worktree isolation does not contain "
        "it; use a path relative to the step's working directory",
    ),
    _rule(
        "shell-network",
        "medium",
        # anchored on a COMMAND position: `cat ~/.ssh/id_rsa` names ssh but does not run it,
        # and filing a private-key read under the network rule buries it at the wrong severity
        r"(?:^|[;&|(]|\s)(?:sudo\s+)?(?:curl|wget|nc|ncat|ssh|scp|sftp|rsync|telnet)(?=\s|$)"
        r"|\b(?:urllib|requests|httpx|http\.client|socket)\.",
        "the step reaches the network itself; whatever it fetches is not visible to this report",
    ),
    _rule(
        "shell-install",
        "medium",
        r"\b(?:pip3?\s+install|uv\s+(?:add|pip\s+install)|npm\s+(?:i|install|ci)|yarn\s+add"
        r"|pnpm\s+add|apt(?:-get)?\s+install|brew\s+install|cargo\s+install|gem\s+install"
        r"|go\s+install)\b",
        "the step installs code it did not bring with it; pin the versions or vendor them",
    ),
    _rule(
        "shell-credentials",
        "medium",
        r"\b(?:gh\s+auth|docker\s+login|npm\s+login|aws\s+configure)\b",
        "the step authenticates a tool for the whole machine, not just for this run",
    ),
    _rule(
        "python-process",
        "medium",
        r"\bsubprocess\.|\bos\.system\(|\bos\.popen\(|\bos\.exec",
        "the step shells out, so what it actually runs is assembled in Python and cannot be "
        "read off the workflow",
    ),
    _rule(
        "templated-body",
        "medium",
        r"\{\{.*?\}\}|\{%.*?%\}",
        "the command is assembled at run time, so what this step runs is not what is written "
        "here and is not covered by this report; read it with `rayspec plan <workflow> --render`",
        ignore_raw=True,
    ),
)

#: How many matches of one rule in one body are quoted before the rest are counted.
_MATCH_CAP: Final = 3

#: Tool groups that let an agent change something rather than only read it.
_COMMAND_TOOLS: Final = ("shell", "edit")


def _evidence(body: str, match: re.Match[str]) -> str:
    """The line a rule matched, trimmed to something quotable."""
    start = body.rfind("\n", 0, match.start()) + 1
    end = body.find("\n", match.end())
    line = body[start : end if end != -1 else len(body)].strip()
    return line[:_EVIDENCE_CAP] + ("…" if len(line) > _EVIDENCE_CAP else "")


def _body_of(step: StepModel) -> tuple[str, str] | None:
    if isinstance(step, ShellStep):
        return "shell", step.shell
    if isinstance(step, PythonStep):
        return "python", step.python
    return None


def _body_findings(path: str, kind: str, body: str) -> Iterator[Finding]:
    """Every rule that matches this body, and every distinct line each one matched.

    The evidence line is what a reviewer reads instead of the body, so quoting only the first
    ``rm -rf build`` of a body that also holds ``rm -rf /`` would mislead in the direction that
    matters. Repeats of the same line are collapsed and matches beyond :data:`_MATCH_CAP` are
    counted rather than quoted.
    """
    raw = [m.span() for m in _RAW_BLOCK.finditer(body)]
    for rule in _BODY_RULES:
        lines: list[str] = []
        for match in rule.pattern.finditer(body):
            if rule.ignore_raw and any(a <= match.start() < b for a, b in raw):
                continue
            line = _evidence(body, match)
            if line not in lines:
                lines.append(line)
        shown, extra = lines[:_MATCH_CAP], max(len(lines) - _MATCH_CAP, 0)
        for index, line in enumerate(shown):
            more = f"  (+{extra} more)" if extra and index == len(shown) - 1 else ""
            yield Finding(
                severity=rule.severity,
                category=rule.category,
                where=path,
                detail=f"{kind}: {line}{more}",
                advice=rule.advice,
            )


def _cwd_findings(path: str, cwd: str | None) -> Iterator[Finding]:
    """``cwd:`` that names a directory the workspace does not contain."""
    if not cwd:
        return
    if "{{" in cwd or "{%" in cwd:
        # a rendered cwd is not known before the run — which is itself worth saying out loud,
        # since the directory the step runs in is chosen at run time
        yield Finding(
            severity="medium",
            category="templated-body",
            where=path,
            detail=f"cwd: {cwd}",
            advice="the working directory is chosen at run time, so this report cannot tell "
            "whether the step stays in the workspace",
        )
        return
    if cwd.startswith(("/", "~")) or any(part == ".." for part in cwd.split("/")):
        yield Finding(
            severity="high",
            category="outside-workspace",
            where=path,
            detail=f"cwd: {cwd}",
            advice="the step runs outside the workspace, so worktree isolation does not contain "
            "it; use a path relative to the workspace",
        )


def _command_tools(tools: ToolsSpec) -> list[str]:
    """The groups of :data:`_COMMAND_TOOLS` an agent's allow/deny lists leave available.

    An empty ``allow`` is not a restriction: it means the provider's own default tool set, which
    includes running commands and editing files.
    """
    deny, allow = set(tools.deny), set(tools.allow)
    return [g for g in _COMMAND_TOOLS if g not in deny and (not allow or g in allow)]


def _agent_findings(rw: ResolvedWorkflow) -> Iterator[Finding]:
    for key, agent in rw.agents.items():
        used_by = sorted(p for p, k in rw.step_agents.items() if k == key)
        used = f" (used by {', '.join(used_by)})" if used_by else ""
        tools = _command_tools(agent.tools)
        if tools:
            how = "allowed" if agent.tools.allow else "the provider's defaults"
            yield Finding(
                severity="medium",
                category="agent-tools",
                where=f"agent {agent.name}",
                detail=f"tools: {', '.join(tools)} ({how}){used}",
                advice="the agent chooses its own commands, so nothing this agent does is "
                "covered by this report; deny the groups it does not need "
                "(tools: {deny: [shell, edit]}) and gate the steps that follow it",
            )
        if agent.access == "full":
            yield Finding(
                severity="high",
                category="agent-access",
                where=f"agent {agent.name}",
                detail=f"access: full{used}",
                advice="the agent may read and write anywhere the process can, not just the "
                "workspace; use access: workspace-write unless it truly needs the machine",
            )
        for name, server in sorted(agent.mcp.items()):
            if server.transport == "stdio":
                command = " ".join([server.command or "", *server.args]).strip()
                yield Finding(
                    severity="high",
                    category="mcp-command",
                    where=f"agent {agent.name}",
                    detail=f"mcp {name}: runs {command}",
                    advice="the agent's tools come from a program started on this machine; "
                    "whatever that program can do, the run can do",
                )
            else:
                yield Finding(
                    severity="medium",
                    category="mcp-remote",
                    where=f"agent {agent.name}",
                    detail=f"mcp {name}: {server.transport} {server.url}",
                    advice="the agent's tools are defined by a remote server, so they can change "
                    "without this workflow changing",
                )


def _lock_advice(class_name: str | None, classes: ApprovalClasses) -> str:
    """What would actually make this gate a real gate — which depends on what it already says."""
    if class_name is None:
        return "give it a class the operator policy marks allow_yes: false"
    if classes.policy_in_force:
        return (
            f"the operator policy does not hold approval class {class_name!r}; add "
            f"allow_yes: false for it (or check the spelling on both sides)"
        )
    return (
        f"no operator policy in force defines approval class {class_name!r}, so naming it "
        "restricts nothing; the rule that would hold this gate is allow_yes: false for it"
    )


def _gate_findings(path: str, step: ApproveStep, classes: ApprovalClasses) -> Iterator[Finding]:
    spec = step.approve
    named = f"class {spec.class_}" if spec.class_ else "no approval class"
    if spec.on_reject == "continue":
        yield Finding(
            severity="medium",
            category="reject-ignored",
            where=path,
            detail="on_reject: continue",
            advice="rejecting this gate does not stop the run; use the default (cancel) if a "
            "rejection should mean stop",
        )
    if not classes.may_approve_automatically(spec.class_):
        return  # the policy already holds this gate shut
    if spec.auto_if is not None:
        yield Finding(
            severity="medium",
            category="self-approving-gate",
            where=path,
            detail=f"auto_if: {spec.auto_if} ({named})",
            advice="the gate approves itself whenever the condition holds; "
            + _lock_advice(spec.class_, classes),
        )
        return
    if classes.unheld(spec.class_):
        # a named class reads like a lock; when nothing defines it, saying "give it a class"
        # would advise the reader to add what is already there
        yield Finding(
            severity="medium",
            category="unheld-class",
            where=path,
            detail=f"{named} (not held)",
            advice=_lock_advice(spec.class_, classes),
        )
        return
    yield Finding(
        severity="low",
        category="waivable-gate",
        where=path,
        detail=named,
        advice="--yes (and --approve-class, for a named class) approves this gate without "
        "asking; " + _lock_advice(spec.class_, classes),
    )


def _workflow_findings(rw: ResolvedWorkflow) -> Iterator[Finding]:
    if rw.workflow.isolation == "none":
        yield Finding(
            severity="low",
            category="no-isolation",
            where=rw.workflow.name,
            detail="isolation: none",
            advice="steps run in the project directory itself, so a destructive step touches "
            "your working tree; use isolation: worktree unless the run must edit in place",
        )


def analyse(rw: ResolvedWorkflow, *, classes: ApprovalClasses | None = None) -> list[Finding]:
    """Every finding for one resolved workflow, most serious first.

    ``classes`` are the approval-class rules in force (the operator's policy): a gate the
    policy already holds shut is not reported as waivable. Nothing here runs, opens a socket or
    writes a file.
    """
    classes = classes if classes is not None else ApprovalClasses()
    findings: list[Finding] = [*_workflow_findings(rw), *_agent_findings(rw)]
    for path, step in rw.all_steps():
        body = _body_of(step)
        if body is not None:
            findings.extend(_body_findings(path, body[0], body[1]))
        findings.extend(_cwd_findings(path, getattr(step, "cwd", None)))
        if isinstance(step, ApproveStep):
            findings.extend(_gate_findings(path, step, classes))
    return sort_findings(findings)


def sort_findings(findings: Iterable[Finding]) -> list[Finding]:
    """Most serious first, then stable by category and location."""
    return sorted(
        findings,
        key=lambda f: (_SEVERITY_RANK.get(f.severity, len(SEVERITIES)), f.category, f.where),
    )


def counts(findings: Sequence[Finding]) -> dict[str, int]:
    """``{"high": 3, "medium": 1, "low": 0}`` — every severity, whether or not it occurred."""
    tally: dict[str, int] = dict.fromkeys(SEVERITIES, 0)
    for finding in findings:
        if finding.severity in tally:
            tally[finding.severity] += 1
    return tally


def to_json(findings: Sequence[Finding]) -> list[dict[str, Any]]:
    """The ``plan --risk --json`` payload."""
    return [finding.to_json() for finding in findings]


__all__ = [
    "SEVERITIES",
    "Finding",
    "analyse",
    "counts",
    "sort_findings",
    "to_json",
]
