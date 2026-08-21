# SPDX-License-Identifier: Apache-2.0
"""Provider adapters: the neutral seam between the engine and agent SDKs.

The engine only ever imports :mod:`rayspec.providers.base` and :mod:`rayspec.providers.registry`.
Concrete adapters (``claude``, ``codex``, ``stub``) live in sibling modules and are looked up by id.
"""
