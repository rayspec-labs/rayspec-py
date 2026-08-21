# SPDX-License-Identifier: Apache-2.0
"""Templating errors: load-time compile errors and run-time render errors.

Module boundary: depends only on :mod:`rayspec.errors`.
"""

from __future__ import annotations

from rayspec.errors import RayspecError


class TemplateCompileError(RayspecError):
    """A template or expression failed to parse at load time.

    ``where`` names the offending field in loader terms (``steps[2] (id: review).prompt``),
    ``lineno`` is the 1-based line inside the template text when known.
    """

    def __init__(self, where: str, message: str, lineno: int | None = None):
        self.where = where
        self.message = message
        self.lineno = lineno
        suffix = f" (line {lineno})" if lineno is not None else ""
        super().__init__(f"{where}: {message}{suffix}")


class TemplateRenderError(RayspecError):
    """Rendering or evaluating a template failed (undefined value, null, bad type, sandbox...).

    The message always names the fix where one exists (``use | default(...)``, ``guard with
    steps.x.status == 'succeeded'`` ...); ``hint`` repeats it for CLI display.
    """

    def __init__(self, message: str, *, hint: str | None = None):
        super().__init__(message, hint=hint)
        self.message = message


__all__ = ["TemplateCompileError", "TemplateRenderError"]
