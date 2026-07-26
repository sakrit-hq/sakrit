# SPDX-License-Identifier: Apache-2.0
"""The runtime adapter contract, and the coordinate ladder.

The adapter is the *entire* framework-facing surface. The core depends only on
this Protocol — never on a concrete framework. An adapter sources a
:class:`Coordinate` from its runtime; where it can't, the coordinate falls to the
developer-declared ladder rungs, else a loud refusal.

See ``docs/design.md`` §4 (ladder), §5 (agnosticism), §11 (the seam).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

from sakrit.core.coordinate import Capabilities, Coordinate, Stability
from sakrit.core.errors import NoCoordinateError

# Scope used for an explicit business key: its stability domain is the business
# domain itself, so it is global by construction (design.md §4, rung 3).
_BUSINESS_SCOPE = "global"


@runtime_checkable
class RuntimeAdapter(Protocol):
    """The complete adapter surface. Everything framework-shaped lives behind this."""

    def current_coordinate(self) -> Coordinate | None:
        """The coordinate for the call in progress, or ``None`` → try the ladder."""
        ...

    def stability_domain(self) -> Stability:
        """The re-execution regimes across which ``call_site`` is stable."""
        ...

    def capabilities(self) -> Capabilities:
        """Optional capabilities the core feature-gates on."""
        ...

    def on_recovery(self, scan: Callable[[], None]) -> None:
        """Register the recovery scan; the adapter decides *when* it runs."""
        ...

    def scope_terminal(self, scope: str) -> bool:
        """Whether a scope is terminal (drives retention/archival)."""
        ...


def resolve_coordinate(
    adapter: RuntimeAdapter | None = None,
    *,
    scope: str | None = None,
    step: str | None = None,
    key: str | None = None,
    occurrence: int = 1,
) -> Coordinate:
    """Resolve a coordinate from the first available ladder rung.

    1. **Runtime coordinate** — ``adapter.current_coordinate()``.
    2. **Developer-declared step id** — ``step`` (requires a ``scope``).
    3. **Explicit business key** — ``key`` (globally scoped by construction).
    4. **Refuse** — :class:`NoCoordinateError`.

    Args-hashing appears on no rung: where identity can't be established, we refuse
    loudly rather than fabricate a wrong identity.
    """
    # Rung 1 — runtime coordinate.
    if adapter is not None:
        coord = adapter.current_coordinate()
        if coord is not None:
            return coord

    # Rung 2 — developer-declared step id. A step names a position *within a run*,
    # so it needs a scope; without one it can't isolate runs and we won't guess.
    if step is not None:
        if scope is None:
            raise NoCoordinateError(
                f"a declared step ({step!r}) needs a scope to bound its retry domain; "
                "pass scope=<run id>, or use key=<business key> for a global effect"
            )
        return Coordinate(scope=scope, call_site=step.encode("utf-8"), occurrence=occurrence)

    # Rung 3 — explicit business key (self-scoping).
    if key is not None:
        return Coordinate(
            scope=scope or _BUSINESS_SCOPE, call_site=key.encode("utf-8"), occurrence=occurrence
        )

    # Rung 4 — refuse.
    raise NoCoordinateError(
        "no coordinate for a consequential effect: supply an adapter (runtime "
        "coordinate), a step= id (with scope=), or a key= business key"
    )
