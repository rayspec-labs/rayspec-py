# SPDX-License-Identifier: Apache-2.0
"""Approval classes — *what may approve a gate*, and what may never approve it automatically.

An `approve:` step names a **class** (`approve: {class: release}`); an operator's policy file
says what that class permits. The split is the whole point: a workflow decides *that* a gate
exists, an operator decides *how strictly* it is held. A workflow can therefore never relax its
own gate, which is what makes it safe to leave a workflow running on a schedule that is also
allowed to publish a release.

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

from collections.abc import Mapping
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
    """

    rules: Mapping[str, ClassRules] = field(default_factory=dict)
    pre_approved: frozenset[str] = frozenset()
    terminal_prompt: bool = True

    def rules_for(self, class_name: str | None) -> ClassRules:
        """The rules governing a gate of this class (the permissive default when unknown)."""
        if class_name is None:
            return DEFAULT_RULES
        return self.rules.get(class_name, DEFAULT_RULES)

    def may_approve_automatically(self, class_name: str | None) -> bool:
        """Whether a gate of this class may be approved without a human answering it."""
        rules = self.rules_for(class_name)
        return rules.allow_yes and not rules.require_tty

    def may_decide_out_of_band(self, class_name: str | None) -> bool:
        """Whether ``rayspec approve <run>`` may approve a gate of this class."""
        return not self.rules_for(class_name).require_tty

    def may_prompt(self, class_name: str | None) -> bool:
        """Whether this process's approval prompt may be asked about a gate of this class."""
        return self.terminal_prompt or not self.rules_for(class_name).require_tty


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

    ``policy`` is whatever :mod:`rayspec.policy` loaded (``None`` when there is no policy file);
    only its ``classes`` mapping is touched, and only the two keys above are read from each
    entry. Validating the file, layering the project one over the user one and deciding which
    of two values is the more restrictive are the policy module's job, not this one's.
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
    "out_of_band_refused",
    "prompt_not_a_terminal",
    "rules_from_policy",
    "waiver_refused",
]
