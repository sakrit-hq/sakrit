# SPDX-License-Identifier: Apache-2.0
"""FakeAdapter — the in-memory reference adapter.

The FakeAdapter rule (``docs/design.md`` §11): the entire core test suite must pass
against this adapter with **no framework imported**. If a core test needs
LangGraph, the seam has leaked. It is therefore three things at once — the proof of
framework-agnosticism, the hermetic test harness, and the reference the second real
adapter is written against.

It models a deterministic runtime: the test places the adapter at a logical step
via :meth:`at`, and *re-placing* it at the same step (as a replay would) yields the
same coordinate — so the core's dedup can be exercised without any real framework.
"""

from __future__ import annotations

from sakrit.core.coordinate import Capabilities, Coordinate, Stability


class FakeAdapter:
    """A controllable, framework-free :class:`~sakrit.core.adapter.RuntimeAdapter`."""

    def __init__(self, scope: str = "fake-run") -> None:
        self._scope = scope
        self._current: Coordinate | None = None
        self._terminal = False

    # --- test controls ----------------------------------------------------
    def at(self, call_site: str, *, occurrence: int = 1, plan_epoch: int = 0) -> Coordinate:
        """Place the adapter at a logical step (deterministic; re-callable to model
        a replay re-executing the same step)."""
        self._current = Coordinate(
            scope=self._scope,
            call_site=call_site.encode("utf-8"),
            occurrence=occurrence,
            plan_epoch=plan_epoch,
        )
        return self._current

    def clear(self) -> None:
        """Simulate being outside any step (``current_coordinate`` → ``None``)."""
        self._current = None

    def mark_terminal(self) -> None:
        self._terminal = True

    # --- RuntimeAdapter (v1 stable surface) -------------------------------
    def current_coordinate(self) -> Coordinate | None:
        return self._current

    # --- ReservedAdapter (P5-4: semantics unfixed, not yet consumed) ------
    def stability_domain(self) -> Stability:
        # A deterministic in-memory runtime replays exactly; no re-planning.
        return Stability.REPLAY | Stability.RETRY

    def capabilities(self) -> Capabilities:
        return Capabilities.NONE

    def scope_terminal(self, scope: str) -> bool:
        return self._terminal
