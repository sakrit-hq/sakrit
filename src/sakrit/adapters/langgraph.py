# SPDX-License-Identifier: Apache-2.0
"""The LangGraph adapter — ``call_site = checkpoint_ns``.

Sources the coordinate from ``get_config()`` inside a node: ``scope = thread_id``,
``call_site = checkpoint_ns`` (the ``"<node>:<deterministic-uuid>"`` the
conformance test proved byte-stable across resume and unique per step —
``docs/dev-notes/callsite-conformance.md``).

Framework import lives here, never in ``sakrit.core``. Requires the ``langgraph``
extra (``pip install sakrit[langgraph]``); import this module explicitly so plain
``import sakrit`` stays framework-free.
"""

from __future__ import annotations

from collections.abc import Callable

from langgraph.config import get_config

from sakrit.core.coordinate import Capabilities, Coordinate, Stability

# The LangGraph versions this adapter's call-site derivation is conformance-tested
# against (research/repro/conformance.py). Outside this range, run the conformance
# test before trusting positional keys — an ns-derivation change strands them.
SUPPORTED_LANGGRAPH = (">=1.0", "<2.0")


class LangGraphAdapter:
    """A :class:`~sakrit.core.adapter.RuntimeAdapter` over LangGraph's checkpoints."""

    def __init__(self) -> None:
        self._scan: Callable[[], None] | None = None

    def current_coordinate(self) -> Coordinate | None:
        try:
            conf = get_config().get("configurable", {})
        except RuntimeError:
            # Not inside a runnable context → fall to the coordinate ladder.
            return None
        ns = conf.get("checkpoint_ns")
        thread = conf.get("thread_id")
        if ns is None or thread is None:
            return None
        return Coordinate(scope=str(thread), call_site=str(ns).encode("utf-8"))

    def stability_domain(self) -> Stability:
        # Resume replays the recorded path; a fresh invocation that re-plans is R3
        # (epochs, deferred). Within-run resume/retry is REPLAY|RETRY.
        return Stability.REPLAY | Stability.RETRY

    def capabilities(self) -> Capabilities:
        return Capabilities.NONE

    def on_recovery(self, scan: Callable[[], None]) -> None:
        # LangGraph has no global startup hook; the integrator runs the scan at
        # process startup (``ledger.recover()``). Stashed here for convenience.
        self._scan = scan

    def scope_terminal(self, scope: str) -> bool:
        return False
