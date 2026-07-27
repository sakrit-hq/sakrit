# SPDX-License-Identifier: Apache-2.0
"""The OpenAI Agents SDK adapter — ``call_site = tool_call_id``.

**Identity source (conformance-probed at openai-agents 0.18):** the model records
each tool call with a ``tool_call_id`` *before* it executes; the SDK's durable
interruption/resume regime (``RunState.to_string``/``from_string``, the HITL
approval flow) re-dispatches the **recorded** call — same ``tool_call_id`` — on
every resume of a saved state. Re-resuming a stale state (crash after resume,
double approval) therefore re-executes the step at the *same* coordinate, which
is exactly what positional dedup needs. Distinct calls get distinct model-issued
ids, so coordinates are unique per step; a deliberate model repeat is a *new*
call id, so it never collides (no sequential-repeat trap on this rung).

``scope`` is the ambient trace id — the SDK serializes it inside ``RunState``, so
it survives resume and the app cannot supply it inconsistently. With tracing
disabled the SDK substitutes a constant ``"no-op"`` trace: that is one shared
scope for every run everywhere, so it is refused (the blank-``checkpoint_ns``
analog) and ``RunConfig.group_id`` — the SDK's own conversation-id knob — is the
fallback; set it if you disable tracing. No honest scope → fall to the ladder.

**Integration:** the SDK passes ``ToolContext`` as a tool *argument* (there is no
ambient accessor), so bridge it with :func:`tool_boundary` inside the tool body::

    @function_tool
    async def send_email(ctx: ToolContext, to: str) -> str:
        with tool_boundary(ctx):
            return sk.guard(SEND_DECL, _send, kwargs={"to": to})

**Limit (same shape as LangGraph's plan-epoch gap):** a run that is *re-run from
scratch* (no saved state) re-asks the model, which mints new call ids — that is a
re-plan (R3), outside positional identity by design. Use a business ``key=`` for
effects that must dedup across re-planning.

Framework import lives here, never in ``sakrit.core``. Requires the
``openai-agents`` extra; import this module explicitly so plain ``import
sakrit`` stays framework-free.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from agents.tool_context import ToolContext
from agents.tracing import get_current_trace

from sakrit.core.coordinate import Capabilities, Coordinate, Stability
from sakrit.core.errors import SakritError

# The SDK versions this adapter's identity derivation is conformance-tested against
# (tests/integration/test_openai_agents_conformance.py). The SDK is pre-1.0 and minors
# can break: outside the tested range, run the conformance suite before trusting
# positional keys — a call-id or RunState change strands them.
SUPPORTED_OPENAI_AGENTS = (">=0.18", "<1.0")

# The active tool invocation, bridged from the tool's ToolContext argument. Per-task
# via contextvars, so concurrent tool executions don't clobber each other.
_tool_ctx: ContextVar[ToolContext | None] = ContextVar("sakrit_oa_tool_ctx", default=None)


@contextmanager
def tool_boundary(ctx: ToolContext) -> Iterator[None]:
    """Make ``ctx`` the coordinate source for guarded calls in this block.

    Call it first thing in the tool body, passing the tool's ``ToolContext``
    argument. Refuses a context with no usable ``tool_call_id`` loudly — a blank
    call id cannot carry positional identity, and proceeding would coin one
    colliding coordinate for every call.
    """
    call_id = str(getattr(ctx, "tool_call_id", "") or "").strip()
    if not call_id:
        raise SakritError(
            "tool_boundary needs a ToolContext with a tool_call_id — got none. Pass the "
            "tool's ToolContext argument (declare it as the first parameter of the tool)."
        )
    token = _tool_ctx.set(ctx)
    try:
        yield
    finally:
        _tool_ctx.reset(token)


class OpenAIAgentsAdapter:
    """A :class:`~sakrit.core.adapter.RuntimeAdapter` over the Agents SDK's recorded
    tool calls (see the module docstring for the conformance basis)."""

    def current_coordinate(self) -> Coordinate | None:
        ctx = _tool_ctx.get()
        if ctx is None:
            return None  # not inside a tool_boundary → fall to the coordinate ladder
        call_id = str(ctx.tool_call_id).strip()
        if not call_id:
            return None  # belt — tool_boundary already refuses this loudly
        scope = _current_scope(ctx)
        if scope is None:
            return None  # no honest retry domain → ladder (key= / step=), never fabricate
        return Coordinate(scope=scope, call_site=call_id.encode("utf-8"))

    # --- ReservedAdapter (P5-4: semantics unfixed, not yet consumed) ------------------
    def stability_domain(self) -> Stability:
        # Resuming a saved RunState re-dispatches the recorded calls; a from-scratch
        # re-run re-plans (R3, epochs — deferred). Within the saved-state regime the
        # coordinate survives resume and retry.
        return Stability.REPLAY | Stability.RETRY

    def capabilities(self) -> Capabilities:
        return Capabilities.NONE

    def scope_terminal(self, scope: str) -> bool:
        return False


def _current_scope(ctx: ToolContext) -> str | None:
    """The retry domain: real trace id first, ``group_id`` fallback, else None.

    The trace id is serialized inside ``RunState`` (probe-verified stable across a
    double resume), so it outranks ``group_id``, which the app must re-supply
    consistently on every resume. The tracing-disabled ``"no-op"`` trace is one
    constant shared by every run — refused, like a blank ``checkpoint_ns``.
    """
    trace = get_current_trace()
    trace_id = str(getattr(trace, "trace_id", "") or "").strip()
    if trace_id and trace_id.lower() != "no-op":
        return trace_id
    group = getattr(getattr(ctx, "run_config", None), "group_id", None)
    group_id = str(group or "").strip()
    return group_id or None
