# SPDX-License-Identifier: Apache-2.0
"""The LangGraph adapter — ``call_site = checkpoint_ns``.

Sources the coordinate from ``get_config()`` inside a node: ``scope = thread_id``,
``call_site = checkpoint_ns`` (the ``"<node>:<deterministic-uuid>"`` the
conformance test proved byte-stable across resume and unique per step —
``docs/dev-notes/callsite-conformance.md``).

**Loud refusals must reach the app, not the model (Fable C-5 / A-3 class).** If you guard
inside a ``@tool`` run by a ``ToolNode``, mind ``handle_tool_errors``. On the supported
LangGraph 1.x the **default is safe** — it swallows only ``ToolInvocationError`` (the model
sent bad args) and re-raises everything else, so a :class:`~sakrit.core.errors.SakritError`
halt (``DivergentRetry`` / ``AmbiguousOutcome``) propagates out of the graph to your app
(conformance-gated). But ``handle_tool_errors=True`` (the old 0.x default, pervasive in
tutorials) turns the halt into a model-visible *"Error: … Please fix your mistakes."* — the
app never sees it and the model can mint a fresh tool call past the halt. So: **if you set
``handle_tool_errors``, exclude ``SakritError``** — either
``handle_tool_errors=(YourExpectedError, …)`` (a tuple that omits Sakrit's), or the callable
:func:`sakrit_handle_tool_errors`, which re-raises a ``SakritError`` and stringifies the rest.

Framework import lives here, never in ``sakrit.core``. Requires the ``langgraph``
extra (``pip install sakrit[langgraph]``); import this module explicitly so plain
``import sakrit`` stays framework-free.
"""

from __future__ import annotations

from langgraph.config import get_config

from sakrit.core.coordinate import Capabilities, Coordinate, Stability
from sakrit.core.errors import SakritError

# The LangGraph versions this adapter's call-site derivation is conformance-tested
# against (research/repro/conformance.py). Outside this range, run the conformance
# test before trusting positional keys — an ns-derivation change strands them.
SUPPORTED_LANGGRAPH = (">=1.0", "<2.0")


def sakrit_handle_tool_errors(exc: Exception) -> str:
    """A ``ToolNode(handle_tool_errors=…)`` handler that lets Sakrit's loud refusals through.

    LangGraph 1.x's *default* handler already re-raises a
    :class:`~sakrit.core.errors.SakritError` (only ``ToolInvocationError`` is stringified), so
    you need this only if you *also* want custom error text for ordinary tool errors while
    keeping the halt loud (Fable C-5). It re-raises a ``SakritError`` — so ``DivergentRetry`` /
    ``AmbiguousOutcome`` reaches the app instead of the model — and returns a model-visible
    string for any other exception (the message a capable model may reasonably retry)."""
    if isinstance(exc, SakritError):
        raise exc
    return f"Error: {exc}\n Please fix your mistakes."


class LangGraphAdapter:
    """A :class:`~sakrit.core.adapter.RuntimeAdapter` over LangGraph's checkpoints."""

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
        # A blank checkpoint_ns (or thread_id) cannot carry positional identity: an empty
        # call_site is not unique per step, so *every* guarded call in the thread would
        # collide onto one coordinate (scope + b"") — a silent swallow or DivergentRetry
        # distinguished only by tool name (P4-3). Refuse it and fall to the ladder (an
        # explicit key=, or a loud NoCoordinateError) rather than manufacture a colliding
        # coordinate. No supported LangGraph (>=1.0,<2.0) emits "" inside a node — it is
        # always "<node>:<uuid>", conformance-gated — but the guard is fail-safe if a version
        # ever changes that (the signal to re-run the conformance suite before adopting it).
        if not str(ns).strip() or not str(thread).strip():
            return None
        return Coordinate(scope=str(thread), call_site=str(ns).encode("utf-8"))

    # --- ReservedAdapter (P5-4: semantics unfixed, not yet consumed). on_recovery was
    # removed — the engine owns recovery (Sakrit.guard runs it once before the first claim),
    # so "the adapter decides when recovery runs" was already false.
    def stability_domain(self) -> Stability:
        # Resume replays the recorded path; a fresh invocation that re-plans is R3
        # (epochs, deferred). Within-run resume/retry is REPLAY|RETRY.
        return Stability.REPLAY | Stability.RETRY

    def capabilities(self) -> Capabilities:
        return Capabilities.NONE

    def scope_terminal(self, scope: str) -> bool:
        return False
