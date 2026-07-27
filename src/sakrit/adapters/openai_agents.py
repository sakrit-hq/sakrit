# SPDX-License-Identifier: Apache-2.0
"""The OpenAI Agents SDK adapter — ``call_site = tool_call_id``, ``scope`` explicit.

**Identity source (conformance-probed at openai-agents 0.18):** the model records each
tool call with a ``tool_call_id`` *before* it executes; the SDK's durable interruption/
resume regime (``RunState.to_string``/``from_string``, the HITL approval flow) re-dispatches
the **recorded** call — same ``tool_call_id`` — on every resume of a saved state. Re-resuming
a stale state (crash after resume, double approval) therefore re-executes the step at the
*same* ``call_site``, which is what positional dedup needs. Distinct calls get distinct
model-issued ids, so call sites are unique per step; a deliberate model repeat is a *new*
call id, so it never collides.

**Scope is explicit — and must be (Fable A-1/A-2).** The ``call_site`` is state-carried, but
the SDK exposes **no state-carried retry domain at tool time**: ``ToolContext`` carries the
call id, ``run_config`` and the user context — not the ``RunState``'s serialized trace or
conversation id. The earlier build sourced scope from the *ambient* ``get_current_trace()``,
which is controlled by the re-execution environment, not the recorded state: an app that
resumes inside ``with trace(...)`` (the SDK's own recommended grouping) mints a fresh trace id
per resume → a fresh scope → **zero dedup while looking guarded**. That is the "identity from
ambient, not from state" category error the whole design forbids. So this adapter refuses to
fabricate a scope: you pass a **stable run identity** you already persist alongside the
``RunState`` string::

    @function_tool(failure_error_function=None)          # see "Loud refusals" below
    async def send_email(ctx: ToolContext, to: str) -> str:
        with tool_boundary(ctx, scope=run_id):           # run_id: your stable, persisted id
            return sk.guard(SEND_DECL, _send, kwargs={"to": to})

Without a ``scope`` the adapter yields **no coordinate** (falls to the ladder) — so a guarded
call must then carry an explicit business ``key=`` or it refuses loudly; it never silently
dedups on a fabricated scope. ``call_site`` (the recorded call id) is only used *together*
with an explicit scope.

**Loud refusals must reach the app, not the model (Fable A-3).** ``function_tool``'s default
error handler catches a raised exception and returns *"An error occurred… try again"* as the
tool's output **to the model** — which would turn Sakrit's ``DivergentRetry`` /
``AmbiguousOutcome`` halts into a suggestion an LLM can retry around. Guarded tools MUST let a
:class:`~sakrit.core.errors.SakritError` propagate: set ``failure_error_function=None``
(re-raise everything) or ``failure_error_function=`` :func:`sakrit_failure_error_function`
(re-raise Sakrit errors, stringify ordinary ones).

**Limit (same shape as LangGraph's plan-epoch gap):** a run *re-run from scratch* (no saved
state) re-asks the model, minting new call ids — a re-plan (R3), outside positional identity
by design. Use a business ``key=`` for effects that must dedup across re-planning.

Framework import lives here, never in ``sakrit.core``. Requires the ``openai-agents`` extra;
import this module explicitly so plain ``import sakrit`` stays framework-free.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from agents.tool_context import ToolContext

from sakrit.core.coordinate import Capabilities, Coordinate, Stability
from sakrit.core.errors import SakritError

# The SDK versions this adapter's identity derivation is conformance-tested against
# (tests/integration/test_openai_agents_conformance.py). The SDK is pre-1.0 and minors
# can break: outside the tested range, run the conformance suite before trusting
# positional keys — a call-id or RunState change strands them.
SUPPORTED_OPENAI_AGENTS = (">=0.18", "<1.0")

# The active (tool context, explicit scope) bridged from the tool body. Per-task via a
# contextvar so concurrent tool executions don't clobber each other.
_tool_ctx: ContextVar[tuple[ToolContext, str | None] | None] = ContextVar(
    "sakrit_oa_tool_ctx", default=None
)


@contextmanager
def tool_boundary(ctx: ToolContext, *, scope: str | None = None) -> Iterator[None]:
    """Make ``ctx`` the coordinate source for guarded calls in this block.

    Call it first thing in the tool body, passing the tool's ``ToolContext`` argument and a
    **stable run identity** as ``scope`` — an id you persist alongside the ``RunState`` string
    (a conversation id, a workflow run id), so it is byte-identical on every resume. The
    adapter deliberately does not derive scope from the ambient trace (Fable A-1): that is
    controlled by the resume *environment*, not the recorded state, and varies across a
    ``with trace(...)`` wrapper or a tracing-config change.

    Refuses a context with no usable ``tool_call_id`` loudly — a blank call id cannot carry
    positional identity. ``scope=None`` is allowed but yields no coordinate (the guard must
    then supply an explicit ``key=``), never a fabricated one.
    """
    call_id = str(getattr(ctx, "tool_call_id", "") or "").strip()
    if not call_id:
        raise SakritError(
            "tool_boundary needs a ToolContext with a tool_call_id — got none. Pass the "
            "tool's ToolContext argument (declare it as the first parameter of the tool)."
        )
    token = _tool_ctx.set((ctx, scope))
    try:
        yield
    finally:
        _tool_ctx.reset(token)


def sakrit_failure_error_function(ctx: object, error: Exception) -> str:
    """A ``function_tool(failure_error_function=...)`` that lets Sakrit's loud refusals through.

    ``function_tool`` otherwise converts a raised tool exception into model-visible text, which
    would silence a :class:`~sakrit.core.errors.SakritError` halt (Fable A-3). This re-raises a
    ``SakritError`` (so the halt reaches the app) while stringifying an ordinary tool error the
    model may reasonably retry. (Pass ``failure_error_function=None`` instead to re-raise
    *every* tool error.)

    **How the halt surfaces:** the SDK wraps a re-raised non-``AgentsException`` tool error in
    ``agents.exceptions.UserError`` with the original as ``__cause__``. So the app catches an
    ``AgentsException`` (a ``UserError``) whose ``__cause__`` is the ``SakritError`` — loud,
    operator-visible, and never fed back to the model. Catch ``UserError`` and inspect
    ``__cause__`` (or walk the cause chain) to recover the ``DivergentRetry`` /
    ``AmbiguousOutcome``.
    """
    if isinstance(error, SakritError):
        raise error
    return f"An error occurred while running the tool: {error}"


class OpenAIAgentsAdapter:
    """A :class:`~sakrit.core.adapter.RuntimeAdapter` over the Agents SDK's recorded tool calls
    (see the module docstring for the conformance basis and the explicit-scope contract)."""

    def current_coordinate(self) -> Coordinate | None:
        bound = _tool_ctx.get()
        if bound is None:
            return None  # not inside a tool_boundary → fall to the coordinate ladder
        ctx, scope = bound
        call_id = str(ctx.tool_call_id).strip()
        if not call_id:
            return None  # belt — tool_boundary already refuses this loudly
        if not scope:
            # No state-carried retry domain and no explicit one → do NOT fabricate from the
            # ambient trace (A-1). Fall to the ladder: the guard must carry an explicit key=.
            return None
        return Coordinate(scope=scope, call_site=call_id.encode("utf-8"))

    # --- ReservedAdapter (P5-4: semantics unfixed, not yet consumed) ------------------
    def stability_domain(self) -> Stability:
        # Resuming a saved RunState re-dispatches the recorded calls; a from-scratch re-run
        # re-plans (R3, epochs — deferred). Within the saved-state regime the call id survives
        # resume and retry; scope is app-supplied and equally stable by contract.
        return Stability.REPLAY | Stability.RETRY

    def capabilities(self) -> Capabilities:
        return Capabilities.NONE

    def scope_terminal(self, scope: str) -> bool:
        return False
