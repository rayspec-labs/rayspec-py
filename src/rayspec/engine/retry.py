# SPDX-License-Identifier: Apache-2.0
"""Retry policy helpers: kind defaults, transient classification, backoff.

Module boundary: pure functions over :class:`~rayspec.schema.RetryPolicy` and
:class:`~rayspec.store.model.ErrorInfo`; the retry *loop* lives in the scheduler's leaf runner.
"""

from __future__ import annotations

from rayspec.providers.base import AgentError, ProviderError
from rayspec.schema import DEFAULT_PROMPT_RETRY, PromptStep, RetryPolicy, StepModel
from rayspec.store.model import ErrorInfo

#: ``ErrorInfo.type`` used for per-attempt timeouts (transient only with ``on_error: all``).
TIMEOUT_ERROR_TYPE = "timeout"


def policy_for(step: StepModel) -> RetryPolicy | None:
    """The effective retry policy: ``retry:`` if set, else the kind default
    (:data:`DEFAULT_PROMPT_RETRY` for prompt steps, none for shell/python)."""
    retry = getattr(step, "retry", None)
    if retry is not None:
        return retry
    if isinstance(step, PromptStep):
        return DEFAULT_PROMPT_RETRY
    return None


def should_retry(policy: RetryPolicy | None, attempts_done: int, error: ErrorInfo | None) -> bool:
    """True when another attempt is allowed after ``attempts_done`` attempts ended in ``error``."""
    if policy is None or error is None:
        return False
    if attempts_done >= policy.attempts:
        return False
    if policy.on_error == "all":
        return True
    return bool(error.transient) and error.type != TIMEOUT_ERROR_TYPE


def delay_for(policy: RetryPolicy, retry_number: int) -> float:
    """Backoff before retry ``retry_number`` (1-based): ``delay`` doubling each time."""
    return float(policy.delay) * (2 ** max(0, retry_number - 1))


def classify_agent_error(error: AgentError) -> ErrorInfo:
    """:class:`AgentError` (a result-level failure) → :class:`ErrorInfo`."""
    return ErrorInfo(type=str(error.kind), message=error.message, transient=bool(error.transient))


def classify_provider_error(exc: ProviderError) -> ErrorInfo:
    """:class:`ProviderError` (an infrastructure failure) → :class:`ErrorInfo`."""
    message = str(exc)
    if exc.hint:
        message = f"{message} (fix: {exc.hint})"
    return ErrorInfo(type=str(exc.kind), message=message, transient=bool(exc.transient))


__all__ = [
    "TIMEOUT_ERROR_TYPE",
    "classify_agent_error",
    "classify_provider_error",
    "delay_for",
    "policy_for",
    "should_retry",
]
