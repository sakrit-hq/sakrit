# SPDX-License-Identifier: Apache-2.0
"""The coordinate — the opaque address of a logical step.

Positional identity rests on a **coordinate** the runtime can supply: a stable,
unique address for "which step of which run is this?" The core treats ``call_site``
as opaque bytes — it never parses it. Everything framework-shaped (a LangGraph
``checkpoint_ns``, a Temporal activity id) lives behind an adapter and arrives here
already reduced to these fields.

See ``docs/design.md`` §11 (the seam) and §4 (the coordinate ladder).
"""

from __future__ import annotations

from enum import Flag, auto
from typing import NamedTuple


class Coordinate(NamedTuple):
    """A logical step's address, as supplied by an adapter or a ladder fallback."""

    scope: str
    """The retry domain and retention axis (a run/thread id, or a business domain)."""

    call_site: bytes
    """Opaque, deterministic, byte-stable-across-re-execution, unique-within-scope.
    The core never parses this."""

    occurrence: int = 1
    """Distinguishes intended repeats of the same call site (default 1). Adapters
    MAY fold repetition into ``call_site`` and then never vary this."""

    plan_epoch: int = 0
    """Bumped when re-execution goes through re-planning (R3) rather than replay.
    0 = the runtime has no epochs / R3 is outside its stability domain."""


class Stability(Flag):
    """The re-execution regimes across which an adapter's ``call_site`` is stable.

    "Stable" is not a boolean — an adapter declares which regimes it covers.
    """

    REPLAY = auto()  # the runtime re-runs the recorded path (R1)
    RETRY = auto()  # the same step re-executes on transient error (within R1/R2)
    REPLAN = auto()  # the coordinate survives re-planning (R3) — rare


class Capabilities(Flag):
    """Optional adapter capabilities the core feature-gates on.

    A coordinate-only adapter (``NONE``) is a valid, complete adapter.
    """

    NONE = 0
    BRANCHES = auto()  # speculation / HITL gating hooks
    PLAN_EPOCHS = auto()  # can signal replay-vs-replan for the R3 mechanism
