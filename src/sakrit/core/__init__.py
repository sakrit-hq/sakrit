# SPDX-License-Identifier: Apache-2.0
"""Sakrit core — the exactly-once engine. THE MOAT.

Invariant: **no framework imports are allowed in this package, ever.** Nothing
under ``sakrit.core`` may import langgraph, openai-agents, crewai, or any other
agent framework — enforced by the FakeAdapter rule (``docs/design.md`` §11). The
core knows only about coordinates, keys, and durable records; framework-specific
glue lives in ``sakrit.adapters``. This boundary is what lets the guarantee be
framework-agnostic — keep it clean.
"""

from sakrit.core.adapter import RuntimeAdapter, resolve_coordinate
from sakrit.core.canonical import canonicalize
from sakrit.core.coordinate import Capabilities, Coordinate, Stability
from sakrit.core.declaration import ArgClass, EffectDecl
from sakrit.core.errors import (
    AmbiguousOutcome,
    CanonicalizationError,
    DivergentRetry,
    NoCoordinateError,
    RegeneratedDuplicate,
    SakritError,
)
from sakrit.core.fingerprint import fingerprint
from sakrit.core.keys import positional_key
from sakrit.core.ledger import EffectState, SqliteLedger
from sakrit.core.settle import settle

__all__ = [
    "AmbiguousOutcome",
    "ArgClass",
    "CanonicalizationError",
    "Capabilities",
    "Coordinate",
    "DivergentRetry",
    "EffectDecl",
    "EffectState",
    "NoCoordinateError",
    "RegeneratedDuplicate",
    "RuntimeAdapter",
    "SakritError",
    "SqliteLedger",
    "Stability",
    "canonicalize",
    "fingerprint",
    "positional_key",
    "resolve_coordinate",
    "settle",
]
