# SPDX-License-Identifier: Apache-2.0
"""``RayspecUndefined`` — chainable on access, strict on use, hint-bearing.

Module boundary: depends only on jinja2. The hint-producing ``getattr`` logic lives in
:mod:`rayspec.templating.engine` (environment) and :mod:`rayspec.templating.scope` (views);
this module only defines the value type and its error message.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, NoReturn

from jinja2 import ChainableUndefined, UndefinedError
from jinja2.utils import missing


def describe_object(obj: Any) -> str:
    """Human name of the object an attribute was looked up on (for undefined messages).

    Objects may carry a ``_rayspec_path`` attribute (``steps.build.output``) which is used
    verbatim; plain values are described by shape ("text value", "mapping", "list", ...).
    """
    path = getattr(obj, "_rayspec_path", None)
    if isinstance(path, str):
        return repr(path)
    if isinstance(obj, ChainableUndefined):
        return "undefined value"
    if isinstance(obj, str):
        return "text value"
    if isinstance(obj, Mapping):
        return "mapping"
    if isinstance(obj, list | tuple):
        return "list"
    if isinstance(obj, bool):
        return "boolean"
    if isinstance(obj, int | float):
        return "number"
    return type(obj).__name__


def _available_keys(obj: Any) -> str:
    if isinstance(obj, Mapping):
        try:
            keys = [str(k) for k in obj]
        except Exception:  # pragma: no cover - defensive: exotic mappings
            return ""
        if not keys:
            return " (it is empty)"
        shown = ", ".join(keys[:12]) + (", ..." if len(keys) > 12 else "")
        return f" (available: {shown})"
    return ""


class RayspecUndefined(ChainableUndefined):
    """Undefined that chains on ``.attr``/``[item]`` access but raises on every use.

    ``a.b.c | default(x)`` and ``a.b is defined`` work; ``str()``, iteration, ``len()``,
    truthiness, ``==``/``!=``, hashing, ``in`` and calling all raise
    :class:`jinja2.UndefinedError`. The message names the object and attribute that were
    missing and appends ``rayspec_hint`` (an actionable fix) when one was supplied by the
    environment's hint-bearing ``getattr``. Chained accesses return ``self`` so the original
    cause (and hint) survive ``steps.x.output.a.b.c``.
    """

    __slots__ = ("_rayspec_hint",)

    def __init__(
        self,
        hint: str | None = None,
        obj: Any = missing,
        name: str | None = None,
        exc: type[UndefinedError] = UndefinedError,
        *,
        rayspec_hint: str | None = None,
    ) -> None:
        super().__init__(hint, obj, name, exc)
        self._rayspec_hint = rayspec_hint

    @property
    def rayspec_hint(self) -> str | None:
        """The actionable hint attached when this undefined was produced (may be ``None``)."""
        return self._rayspec_hint

    @property
    def _undefined_message(self) -> str:
        if self._undefined_hint:
            base = self._undefined_hint
        elif self._undefined_obj is missing:
            base = f"{self._undefined_name!r} is undefined"
        elif not isinstance(self._undefined_name, str):
            base = f"{describe_object(self._undefined_obj)} has no element {self._undefined_name!r}"
        else:
            base = (
                f"{describe_object(self._undefined_obj)} has no attribute "
                f"{self._undefined_name!r}{_available_keys(self._undefined_obj)}"
            )
        if self._rayspec_hint:
            return f"{base}; {self._rayspec_hint}"
        return base

    def _fail_with_undefined_error(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise self._undefined_exception(self._undefined_message)

    def __getattr__(self, name: str) -> RayspecUndefined:
        if name[:2] == "__" and name[-2:] == "__":
            raise AttributeError(name)
        return self

    __getitem__ = __getattr__  # type: ignore[assignment]

    # strict on use --------------------------------------------------------------------
    __iter__ = __str__ = __len__ = _fail_with_undefined_error  # type: ignore[assignment]
    __eq__ = __ne__ = __bool__ = __hash__ = _fail_with_undefined_error  # type: ignore[assignment]
    __contains__ = _fail_with_undefined_error  # type: ignore[assignment]

    def __html__(self) -> str:
        self._fail_with_undefined_error()


__all__ = ["RayspecUndefined", "describe_object"]
