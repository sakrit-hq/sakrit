# SPDX-License-Identifier: Apache-2.0
"""Sakrit core — the exactly-once engine. THE MOAT.

Invariant: **no framework imports are allowed in this package, ever.** Nothing
under ``sakrit.core`` may import langgraph, openai-agents, crewai, or any other
agent framework — enforced by the FakeAdapter rule (``docs/design.md`` §11). The
core knows only about coordinates, keys, and durable records; framework-specific
glue lives in ``sakrit.adapters``. This boundary is what lets the guarantee be
framework-agnostic — keep it clean.
"""

from sakrit.core.adapter import ReservedAdapter, RuntimeAdapter, resolve_coordinate
from sakrit.core.canonical import canonicalize
from sakrit.core.context import current_key
from sakrit.core.coordinate import Capabilities, Coordinate, Stability
from sakrit.core.declaration import ArgClass, EffectDecl
from sakrit.core.errors import (
    AmbiguousOutcome,
    CanonicalizationError,
    DivergentRetry,
    EffectInFlightError,
    NoCoordinateError,
    RegeneratedDuplicate,
    SakritError,
    SchemeMismatch,
)
from sakrit.core.fingerprint import fingerprint
from sakrit.core.keys import positional_key
from sakrit.core.leased import settle_leased, settle_leased_async
from sakrit.core.ledger import EffectState, Replayed, SqliteLedger
from sakrit.core.metrics import Metrics
from sakrit.core.protocols import LeasedLedger, Ledger
from sakrit.core.reconcile import Reconciliation, Verdict
from sakrit.core.settle import settle, settle_async

__all__ = [
    "AmbiguousOutcome",
    "ArgClass",
    "CanonicalizationError",
    "Capabilities",
    "Coordinate",
    "DivergentRetry",
    "EffectDecl",
    "EffectInFlightError",
    "EffectState",
    "LeasedLedger",
    "Ledger",
    "Metrics",
    "NoCoordinateError",
    "Reconciliation",
    "RegeneratedDuplicate",
    "Replayed",
    "ReservedAdapter",
    "RuntimeAdapter",
    "SakritError",
    "SchemeMismatch",
    "SqliteLedger",
    "Stability",
    "Verdict",
    "canonicalize",
    "current_key",
    "fingerprint",
    "positional_key",
    "resolve_coordinate",
    "settle",
    "settle_async",
    "settle_leased",
    "settle_leased_async",
]
