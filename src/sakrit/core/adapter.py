# SPDX-License-Identifier: Apache-2.0
"""The runtime adapter contract, and the coordinate ladder.

The adapter is the *entire* framework-facing surface. The core depends only on
this Protocol — never on a concrete framework. An adapter sources a
:class:`Coordinate` from its runtime; where it can't, the coordinate falls to the
developer-declared ladder rungs, else a loud refusal.

See ``docs/design.md`` §4 (ladder), §5 (agnosticism), §11 (the seam).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from sakrit.core.coordinate import Capabilities, Coordinate, Stability
from sakrit.core.errors import NoCoordinateError

# Scope used for an explicit business key: its stability domain is the business
# domain itself, so it is global by construction (design.md §4, rung 3).
_BUSINESS_SCOPE = "global"


@runtime_checkable
class RuntimeAdapter(Protocol):
    """The **v1 stable** adapter surface — exactly what the core consumes (P5-4).

    Only ``current_coordinate()`` is called anywhere in the core (``resolve_coordinate``), so
    it is the whole frozen contract. The four other methods a runtime *might* one day provide
    (stability domain, capabilities, retention, recovery scheduling) are **not** frozen — they
    gate machinery that doesn't exist yet, and freezing semantics never exercised would make an
    Act IV adapter author implement to a guess the core later contradicts. They live on the
    ``ReservedAdapter`` appendix below, to grow into ``RuntimeAdapter`` one method at a time as
    the core adds a caller for each.
    """

    def current_coordinate(self) -> Coordinate | None:
        """The coordinate for the call in progress, or ``None`` → try the ladder."""
        ...


@runtime_checkable
class ReservedAdapter(Protocol):
    """**Reserved — semantics unfixed, NOT part of the v1 contract (P5-4).** Methods a runtime
    may implement for future machinery that the core does not yet call; each moves onto the
    stable ``RuntimeAdapter`` only when a real caller lands. Do not depend on their meaning.

    (Note: ``on_recovery`` — "the adapter decides *when* recovery runs" — was deliberately
    **removed**: the engine owns recovery (``Sakrit.guard`` runs it once before the first
    claim), so that contract was already false and must not freeze. A recovery-scheduling hook,
    if ever needed, will be designed against how recovery actually runs.)
    """

    def stability_domain(self) -> Stability:
        """RESERVED. The re-execution regimes across which ``call_site`` is stable (gates the
        R3/plan-epoch machinery that does not exist yet)."""
        ...

    def capabilities(self) -> Capabilities:
        """RESERVED. Optional capabilities the core would feature-gate on."""
        ...

    def scope_terminal(self, scope: str) -> bool:
        """RESERVED. Whether a scope is terminal (would drive retention/archival)."""
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

    1. **Explicit business key** — ``key`` (globally scoped by construction).
    2. **Runtime coordinate** — ``adapter.current_coordinate()``.
    3. **Developer-declared step id** — ``step`` (requires a ``scope``).
    4. **Refuse** — :class:`NoCoordinateError`.

    An explicit ``key`` outranks the runtime coordinate (P4-2): *a business key names
    one action, not one tool.* When the developer asserts identity directly, that is
    the ground truth — the adapter's positional guess must not override it, or two
    call sites for the same business action would mint two keys. Combining ``key`` with
    ``step`` is contradictory (two identities for one effect) and refuses.

    Args-hashing appears on no rung: where identity can't be established, we refuse
    loudly rather than fabricate a wrong identity.
    """
    # Rung 1 — explicit business key. It names one action globally, so it outranks
    # even a runtime coordinate. A step alongside it is a second, conflicting identity.
    if key is not None:
        if step is not None:
            raise NoCoordinateError(
                f"both key={key!r} and step={step!r} were given — two different identities "
                "for one effect. A business key names one action globally; a step names a "
                "position within a run. Pass exactly one."
            )
        return Coordinate(
            scope=scope or _BUSINESS_SCOPE, call_site=key.encode("utf-8"), occurrence=occurrence
        )

    # Rung 2 — runtime coordinate.
    if adapter is not None:
        coord = adapter.current_coordinate()
        if coord is not None:
            return coord

    # Rung 3 — developer-declared step id. A step names a position *within a run*,
    # so it needs a scope; without one it can't isolate runs and we won't guess.
    if step is not None:
        if scope is None:
            raise NoCoordinateError(
                f"a declared step ({step!r}) needs a scope to bound its retry domain; "
                "pass scope=<run id>, or use key=<business key> for a global effect"
            )
        return Coordinate(scope=scope, call_site=step.encode("utf-8"), occurrence=occurrence)

    # Rung 4 — refuse.
    raise NoCoordinateError(
        "no coordinate for a consequential effect: supply a key= business key, an "
        "adapter (runtime coordinate), or a step= id (with scope=)"
    )
