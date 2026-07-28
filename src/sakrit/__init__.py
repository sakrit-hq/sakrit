# SPDX-License-Identifier: Apache-2.0
"""Sakrit — exactly-once effects for AI agents.

A thin, framework-agnostic layer that sits between an AI agent and the tools it
calls, guaranteeing that every action with real-world consequences happens
exactly once — even across crashes, resumes, retries, and parallel plans.

Importing ``sakrit`` never imports a framework. Framework adapters live under
``sakrit.adapters`` and are imported explicitly (e.g.
``from sakrit.adapters.langgraph import LangGraphAdapter``).
"""

from collections.abc import Callable
from typing import TypeVar

from sakrit.__about__ import __version__
from sakrit.core import ArgClass, EffectDecl, Metrics, SqliteLedger, current_key
from sakrit.engine import Sakrit

__all__ = [
    "ArgClass",
    "EffectDecl",
    "Metrics",
    "Sakrit",
    "SqliteLedger",
    "__version__",
    "current_key",
    "safe",
]

_F = TypeVar("_F", bound=Callable[..., object])


def safe(fn: _F) -> _F:
    """Mark a callable as *reviewed and deliberately unguarded*.

    Runtime-inert — returns ``fn`` unchanged, no wrapper, no cost. Its consumer is
    ``sakrit doctor`` (static scan), which suppresses findings inside a function
    decorated ``@sakrit.safe``. Use it to annotate-and-move-on after reviewing a
    doctor finding that is genuinely non-consequential or guarded elsewhere; the
    marker records that a human decided so.
    """
    return fn


# G-8: ``Callable``/``TypeVar`` are only needed to construct ``_F`` above; drop them from the
# package namespace so ``sakrit.Callable`` / ``sakrit.TypeVar`` don't leak as tab-completable
# re-exports (they're stdlib, not part of the public surface — STABILITY promise 3).
del Callable, TypeVar
