# SPDX-License-Identifier: Apache-2.0
"""Templating layer: Jinja environments, lexical scopes/step views, filters, expressions.

Public surface (see CONTRACTS.md): :class:`TemplateEngine` (three environments, render/eval/
compile/references), :class:`Scope` + :class:`StepView` + :func:`build_context`,
:func:`export_env` / :func:`write_context_file`, :class:`RayspecUndefined`, the errors, the
filters and the pure lint helpers.
"""

from rayspec.templating.engine import (
    REFERENCE_ROOTS,
    SPILL_THRESHOLD,
    Ref,
    ReferenceKind,
    RenderedScript,
    TemplateEngine,
    TemplateKind,
    stringify_text,
)
from rayspec.templating.errors import TemplateCompileError, TemplateRenderError
from rayspec.templating.filters import FILTERS, TESTS, fromjson, has_signal, regex_search
from rayspec.templating.lints import has_braces, has_gha_syntax
from rayspec.templating.scope import (
    STEP_ATTRIBUTES,
    Namespace,
    Scope,
    StepsNamespace,
    StepView,
    build_context,
    export_env,
    stringify_scalar,
    to_jsonable,
    write_context_file,
)
from rayspec.templating.undefined import RayspecUndefined

__all__ = [
    "FILTERS",
    "REFERENCE_ROOTS",
    "SPILL_THRESHOLD",
    "STEP_ATTRIBUTES",
    "TESTS",
    "Namespace",
    "RayspecUndefined",
    "Ref",
    "ReferenceKind",
    "RenderedScript",
    "Scope",
    "StepView",
    "StepsNamespace",
    "TemplateCompileError",
    "TemplateEngine",
    "TemplateKind",
    "TemplateRenderError",
    "build_context",
    "export_env",
    "fromjson",
    "has_braces",
    "has_gha_syntax",
    "has_signal",
    "regex_search",
    "stringify_scalar",
    "stringify_text",
    "to_jsonable",
    "write_context_file",
]
