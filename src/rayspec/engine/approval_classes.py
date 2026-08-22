# SPDX-License-Identifier: Apache-2.0
"""Approval classes — *what may approve a gate*, and what may never approve it automatically.

An `approve:` step names a **class** (`approve: {class: release}`); an operator's policy says
what that class permits. The split is the whole point: a workflow decides *that* a gate exists,
an operator decides *how strictly* it is held. A workflow can name a class but cannot define
one, so it can never loosen a rule the operator set — which is what makes it safe to leave a
workflow running on a schedule that is also allowed to publish a release.

The converse is the limit of the mechanism, and it is not hidden: a class **nothing in force
defines** — because there is no policy, because the operator spelled it differently, or because
the workflow was edited — keeps the permissive default. The scope of the control is therefore
chosen by name on both sides, and a name that does not match is reported by every caller that
can see it (:func:`class_not_held`, `plan --risk`, and the gate itself) rather than passing for
a lock. Holding *every* gate regardless of name is an operator-policy question, not one this
module can answer.

Two rules, in increasing strictness:

``allow_yes: false``
    the gate is never approved **automatically**: not by ``--yes``, not by ``--dry-run``, not by
    ``--approve-class``, not by ``auto_if``, and not by any combination of them. A human
    deciding this one gate — at the terminal, or with ``rayspec approve <run>`` — still works;
    that is a decision, not a waiver.

``require_tty: true``
    stricter: the decision must come from the built-in terminal prompt of the process running
    the workflow. A decision recorded out of band by ``rayspec approve``/``rayspec reject`` is
    not accepted (it can be scripted), and neither is a configured approval extension.

A **rejection** is never constrained by a class. Refusing to approve is the fail-closed
direction, and a gate nobody can reject is a gate nobody can get out of.

Module boundary: this module owns the rules and the decision they imply. It asks nobody
anything (that is the approval prompt's job), it reads no files, and it holds no state — the
policy file's shape and its layering belong to :mod:`rayspec.policy`, which reaches this module
only through :func:`rules_from_policy`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

#: ``decision.by`` values for the automatic approval paths (``tty``/``cli`` are the human ones).
BY_YES: Final = "--yes"
BY_DRY_RUN: Final = "dry-run"
BY_APPROVE_CLASS: Final = "--approve-class"
BY_AUTO_IF: Final = "auto_if"
BY_TTY: Final = "tty"


@dataclass(frozen=True, slots=True)
class ClassRules:
    """What one approval class permits. The defaults are today's behaviour: everything."""

    allow_yes: bool = True
    require_tty: bool = False

    @property
    def named(self) -> str:
        """The rules that are set, as they read in the policy file (for a refusal message)."""
        parts = []
        if not self.allow_yes:
            parts.append("allow_yes: false")
        if self.require_tty:
            parts.append("require_tty: true")
        return " and ".join(parts) or "the defaults"


#: The rules a gate gets when it names no class, or names one the policy says nothing about.
DEFAULT_RULES: Final = ClassRules()


@dataclass(frozen=True)
class ApprovalClasses:
    """The class rules in force for one run, plus what this invocation pre-authorised.

    ``rules`` come from the operator's policy file, ``pre_approved`` from ``--approve-class``
    on the command line, and ``terminal_prompt`` says whether this process's approval prompt is
    the built-in terminal one (it is false when ``extensions.approval`` replaced it).

    ``policy_loaded`` is the separate question the messages need: whether a policy file is in
    force *at all*. "No operator policy is in force" and "the policy in force does not define
    this class" are different problems with different fixes, and a run that printed the path of
    its policy file two lines earlier must not then claim it has none.
    """

    rules: Mapping[str, ClassRules] = field(default_factory=dict)
    pre_approved: frozenset[str] = frozenset()
    terminal_prompt: bool = True
    policy_loaded: bool = False

    @property
    def policy_in_force(self) -> bool:
        """Whether an operator policy is in force (whether or not it defines any class).

        Rules imply a policy, so a caller that only knows the rules — the test harness, a
        hand-built instance — still gets the right answer without setting ``policy_loaded``.
        """
        return self.policy_loaded or bool(self.rules)

    def rules_for(self, class_name: str | None) -> ClassRules:
        """The rules governing a gate of this class (the permissive default when unknown)."""
        if class_name is None:
            return DEFAULT_RULES
        return self.rules.get(class_name, DEFAULT_RULES)

    def unheld(self, class_name: str | None) -> bool:
        """Whether this gate names a class nothing in force says anything about.

        Such a gate keeps the permissive default — a workflow naming a class the operator never
        heard of must not be able to *invent* a restriction either — but every caller that can
        say so out loud must, because the name reads like a lock and is not one.
        """
        return class_name is not None and class_name not in self.rules

    def may_approve_automatically(self, class_name: str | None) -> bool:
        """Whether a gate of this class may be approved without a human answering it."""
        rules = self.rules_for(class_name)
        return rules.allow_yes and not rules.require_tty

    def may_decide_out_of_band(self, class_name: str | None) -> bool:
        """Whether ``rayspec approve <run>`` may approve a gate of this class."""
        return not self.rules_for(class_name).require_tty

    def may_prompt(self, class_name: str | None, *, at_a_terminal: bool = True) -> bool:
        """Whether this process's approval prompt may be asked about a gate of this class.

        ``require_tty`` needs both halves of its name: the built-in terminal prompt
        (``terminal_prompt``) *and* a process that really is attached to a terminal
        (``at_a_terminal``, probed by the caller at the moment of asking rather than taken from
        a flag). What it cannot tell apart is a person and a pty — see the caveat in
        ``docs/runs-and-resume.md``.
        """
        if not self.rules_for(class_name).require_tty:
            return True
        return self.terminal_prompt and at_a_terminal


def automatic_by(
    classes: ApprovalClasses, class_name: str | None, *, yes: bool, dry_run: bool
) -> str | None:
    """The blanket approval this invocation carries for a gate of this class, or ``None``.

    Purely about what was *asked for* — whether the class permits it is
    :meth:`ApprovalClasses.may_approve_automatically`, and the caller must check that first.
    """
    if yes:
        return BY_YES
    if dry_run:
        return BY_DRY_RUN
    if class_name is not None and class_name in classes.pre_approved:
        return BY_APPROVE_CLASS
    return None


def _decide_instead(rules: ClassRules) -> str:
    if rules.require_tty:
        return "answer it at the terminal running the workflow (`rayspec resume <run>`)"
    return (
        "answer it at the terminal, or decide it with `rayspec approve <run>` / "
        "`rayspec reject <run>`"
    )


def waiver_refused(
    class_name: str | None, rules: ClassRules, *, waiver: str, step_path: str
) -> str:
    """Why an automatic approval did not apply to this gate, and what to do instead."""
    return (
        f"{step_path}: {waiver} does not approve approval class {class_name!r} "
        f"({rules.named}); {_decide_instead(rules)}"
    )


def class_not_held(class_name: str, *, step_path: str, policy_in_force: bool) -> str:
    """Why naming this class did not hold the gate: nothing in force defines it."""
    where = f"steps.{step_path}.approve.class"
    waivers = "every automatic approval (--yes, --approve-class, auto_if) applies to it"
    if policy_in_force:
        return (
            f"{where}: {class_name!r} is not defined by the operator policy, so the gate is not "
            f"held — {waivers}; add the class to the policy, or check the spelling"
        )
    return (
        f"{where}: names approval class {class_name!r}, but no operator policy is in force, so "
        f"the gate is not held — {waivers}"
    )


def gate_held(class_name: str | None, rules: ClassRules, *, step_path: str) -> str:
    """Why this gate is waiting rather than being answered the usual way.

    Emitted when a class constrains a gate that pauses *without* anybody having asked for a
    waiver: a control that only speaks when it refuses something is invisible in the event
    stream, and a reader of ``stream.jsonl`` cannot tell a held pause from an ordinary one.
    """
    return (
        f"{step_path}: approval class {class_name!r} holds this gate ({rules.named}); "
        f"{_decide_instead(rules)}"
    )


def unheld_classes(gates: Iterable[tuple[str, str | None]], classes: ApprovalClasses) -> list[str]:
    """One :func:`class_not_held` warning per ``(step path, class name)`` that is not held."""
    return [
        class_not_held(name, step_path=path, policy_in_force=classes.policy_in_force)
        for path, name in gates
        if name is not None and classes.unheld(name)
    ]


def out_of_band_refused(class_name: str | None, *, step_path: str) -> str:
    """Why a recorded ``rayspec approve`` decision was not accepted for this gate."""
    return (
        f"{step_path}: approval class {class_name!r} requires a terminal (require_tty: true), "
        "so a decision recorded by `rayspec approve` / `rayspec reject` is not accepted; "
        "answer the gate with `rayspec resume <run>` from a terminal"
    )


def prompt_not_a_terminal(class_name: str | None, *, step_path: str) -> str:
    """Why a configured approval extension was not asked about this gate."""
    return (
        f"{step_path}: approval class {class_name!r} requires a terminal (require_tty: true), "
        "so the configured approval extension was not asked; the gate is paused — answer it "
        "with `rayspec resume <run>` from a terminal"
    )


def no_terminal(class_name: str | None, *, step_path: str) -> str:
    """Why the built-in prompt was not asked: this process has no terminal to ask at."""
    return (
        f"{step_path}: approval class {class_name!r} requires a terminal (require_tty: true) "
        "and this process has none; the gate is paused — answer it with `rayspec resume <run>` "
        "from a terminal"
    )


def _entry_rules(entry: Any) -> ClassRules:
    if isinstance(entry, ClassRules):
        return entry
    if isinstance(entry, Mapping):
        return ClassRules(
            allow_yes=bool(entry.get("allow_yes", True)),
            require_tty=bool(entry.get("require_tty", False)),
        )
    return ClassRules(
        allow_yes=bool(getattr(entry, "allow_yes", True)),
        require_tty=bool(getattr(entry, "require_tty", False)),
    )


def rules_from_policy(policy: Any) -> dict[str, ClassRules]:
    """The class rules of a loaded policy — the ONE thing this module reads from a policy.

    ``policy`` is the merged ``approvals:`` block :mod:`rayspec.policy` produced
    (:attr:`~rayspec.policy.EffectivePolicy.approvals`), or ``None`` when no policy file is in
    force. Only its ``classes`` mapping is touched, and only the two keys above are read from
    each entry. Validating the file, layering the project one over the user one and deciding
    which of two values is the more restrictive are the policy module's job, not this one's —
    which is why this takes ``Any`` and reads one attribute rather than importing that package.
    """
    entries = getattr(policy, "classes", None) if policy is not None else None
    if not isinstance(entries, Mapping):
        return {}
    return {str(name): _entry_rules(entry) for name, entry in entries.items()}


__all__ = [
    "BY_APPROVE_CLASS",
    "BY_AUTO_IF",
    "BY_DRY_RUN",
    "BY_TTY",
    "BY_YES",
    "DEFAULT_RULES",
    "ApprovalClasses",
    "ClassRules",
    "automatic_by",
    "class_not_held",
    "gate_held",
    "no_terminal",
    "out_of_band_refused",
    "prompt_not_a_terminal",
    "rules_from_policy",
    "unheld_classes",
    "waiver_refused",
]
