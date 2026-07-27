# SPDX-License-Identifier: Apache-2.0
"""The OpenAI Agents SDK adapter's conformance gate.

The same property the LangGraph gate asserts: the adapter's coordinate must be
**byte-stable across a resume** and **unique per logical step** — here, across the
SDK's durable regime: serialize a ``RunState`` at an approval interruption, then
resume the *same saved string* twice (crash-after-resume / double-approval — the
retry that must dedup). Driven by a scripted fake model; no API key, no network
(the SDK's tracing exporter is disabled for the run).

If an SDK upgrade changes call-id recording or RunState semantics, this gate
fails — the signal to re-run conformance before trusting positional keys on it.
"""

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("agents")

from agents import (  # noqa: E402
    Agent,
    RunConfig,
    Runner,
    RunState,
    function_tool,
    set_trace_processors,
)
from agents.items import ModelResponse  # noqa: E402
from agents.models.interface import Model  # noqa: E402
from agents.tool_context import ToolContext  # noqa: E402
from agents.usage import Usage  # noqa: E402
from openai.types.responses import (  # noqa: E402
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)

from sakrit import EffectDecl, Sakrit, SqliteLedger  # noqa: E402
from sakrit.adapters.openai_agents import OpenAIAgentsAdapter, tool_boundary  # noqa: E402
from sakrit.core import ArgClass, Coordinate  # noqa: E402
from sakrit.core.errors import SakritError  # noqa: E402

pytestmark = pytest.mark.integration

ADAPTER = OpenAIAgentsAdapter()
# Traces are still created (the adapter's scope source) but never exported — no
# network, no API key, no 401 noise from the SDK's background exporter.
set_trace_processors([])


def _tool_call(call_id: str, name: str, args: str) -> ResponseFunctionToolCall:
    return ResponseFunctionToolCall(
        type="function_call",
        call_id=call_id,
        name=name,
        arguments=args,
        id=f"fc_{call_id}",
        status="completed",
    )


def _final(text: str) -> ResponseOutputMessage:
    return ResponseOutputMessage(
        type="message",
        id="msg_final",
        role="assistant",
        status="completed",
        content=[ResponseOutputText(type="output_text", text=text, annotations=[])],
    )


class ScriptedModel(Model):
    """Emits the given output batches in order; repeats the last one after."""

    def __init__(self, *batches: list[Any]) -> None:
        self._batches = list(batches)
        self._i = 0

    async def get_response(self, *args: Any, **kwargs: Any) -> ModelResponse:
        batch = self._batches[min(self._i, len(self._batches) - 1)]
        self._i += 1
        return ModelResponse(output=list(batch), usage=Usage(), response_id=None)

    def stream_response(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError


async def _interrupt_and_save(agent: Agent, prompt: str) -> tuple[Any, str]:
    """Run to the approval interruption and serialize the state — the checkpoint."""
    result = await Runner.run(agent, prompt)
    assert result.interruptions, "the approval-gated tool never interrupted"
    return result, result.to_state().to_string()


async def _resume(agent: Agent, saved: str, interruptions: list[Any]) -> Any:
    """Deserialize the saved state, approve every pending call, run to completion."""
    state = await RunState.from_string(agent, saved)
    for item in interruptions:
        state.approve(item)
    return await Runner.run(agent, state)


def test_coordinate_stable_across_double_resume_and_unique_per_call() -> None:
    import asyncio

    caps: list[tuple[str, Coordinate | None]] = []

    @function_tool(needs_approval=True)
    async def act(ctx: ToolContext, label: str) -> str:
        with tool_boundary(ctx):
            caps.append((label, ADAPTER.current_coordinate()))
        return label

    async def scenario() -> None:
        model = ScriptedModel(
            [
                _tool_call("call_A", "act", '{"label": "a"}'),
                _tool_call("call_B", "act", '{"label": "b"}'),
            ],
            [_final("done")],
        )
        agent = Agent(name="conf", instructions="i", model=model, tools=[act])
        result, saved = await _interrupt_and_save(agent, "go")
        await _resume(agent, saved, result.interruptions)  # resume 1 — both calls run
        await _resume(agent, saved, result.interruptions)  # resume 2 of the SAME state

    asyncio.run(scenario())

    by_label: dict[str, set[Coordinate]] = {}
    for label, coord in caps:
        assert coord is not None, f"no coordinate inside the tool for {label!r}"
        by_label.setdefault(label, set()).add(coord)
    # STABLE: each call re-executed on the second resume at the SAME coordinate.
    assert len(caps) == 4  # 2 calls × 2 resumes
    for label, coords in by_label.items():
        assert len(coords) == 1, f"coordinate NOT stable across resume for {label!r}: {coords}"
    # UNIQUE: distinct calls → distinct coordinates.
    distinct = {next(iter(c)) for c in by_label.values()}
    assert len(distinct) == 2, f"coordinates NOT unique per call: {by_label}"
    # And the scope came from the trace, shared by both calls of the run.
    scopes = {c.scope for c in distinct}
    assert len(scopes) == 1 and next(iter(scopes)).startswith("trace_")


def test_exactly_once_through_sakrit_across_double_resume(tmp_path: Path) -> None:
    """The money cell: a Sakrit-guarded effect inside an SDK tool fires once even
    when the same saved state is resumed twice (crash-after-resume retry)."""
    import asyncio

    fired: list[str] = []
    decl = EffectDecl("mail.send", {"to": ArgClass.IDENTITY})
    ledger = SqliteLedger(tmp_path / "ledger.db")
    sk = Sakrit(ledger, secret=b"s3cret", adapter=OpenAIAgentsAdapter())

    def _send(to: str) -> str:
        fired.append(to)
        return f"sent:{to}"

    @function_tool(needs_approval=True)
    async def send_email(ctx: ToolContext, to: str) -> str:
        with tool_boundary(ctx):
            return str(sk.guard(decl, _send, kwargs={"to": to}))

    async def scenario() -> None:
        model = ScriptedModel(
            [_tool_call("call_M", "send_email", '{"to": "ops@x.com"}')],
            [_final("done")],
        )
        agent = Agent(name="mailer", instructions="i", model=model, tools=[send_email])
        result, saved = await _interrupt_and_save(agent, "send it")
        r1 = await _resume(agent, saved, result.interruptions)
        r2 = await _resume(agent, saved, result.interruptions)
        assert r1.final_output == "done" and r2.final_output == "done"

    try:
        asyncio.run(scenario())
    finally:
        ledger.close()
    assert fired == ["ops@x.com"]  # second resume replayed — the effect did not re-fire


def test_tracing_disabled_refuses_noop_scope_and_falls_back_to_group_id() -> None:
    import asyncio

    caps: list[Coordinate | None] = []

    @function_tool  # no approval needed — scope sourcing doesn't require the resume regime
    async def act(ctx: ToolContext) -> str:
        with tool_boundary(ctx):
            caps.append(ADAPTER.current_coordinate())
        return "ok"

    def _agent() -> Agent:
        model = ScriptedModel([_tool_call("call_T", "act", "{}")], [_final("done")])
        return Agent(name="conf", instructions="i", model=model, tools=[act])

    # tracing disabled + no group_id → the "no-op" trace is refused → no coordinate
    # (the guarded call falls to the ladder rather than sharing one global scope).
    asyncio.run(Runner.run(_agent(), "go", run_config=RunConfig(tracing_disabled=True)))
    assert caps == [None]

    # tracing disabled + group_id → the SDK's own conversation id is the scope.
    caps.clear()
    asyncio.run(
        Runner.run(_agent(), "go", run_config=RunConfig(tracing_disabled=True, group_id="conv-42"))
    )
    assert caps and caps[0] is not None and caps[0].scope == "conv-42"
    assert caps[0].call_site == b"call_T"


def test_outside_tool_boundary_returns_none() -> None:
    assert ADAPTER.current_coordinate() is None  # falls to the ladder, never guesses


def test_blank_tool_call_id_is_refused_loudly() -> None:
    class _Stub:
        tool_call_id = "   "

    boundary = tool_boundary(_Stub())  # type: ignore[arg-type]
    with pytest.raises(SakritError, match="tool_call_id"):
        boundary.__enter__()
