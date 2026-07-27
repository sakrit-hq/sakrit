# SPDX-License-Identifier: Apache-2.0
"""The OpenAI Agents SDK adapter's conformance gate.

The property the LangGraph gate asserts, plus the ambient-variation cells the first
version of this gate lacked (Fable A-1/A-2/A-3): the adapter's coordinate must be
**byte-stable across a resume** and **unique per logical step** across the SDK's durable
regime — and stable *regardless of the resume environment* (a ``with trace(...)`` wrapper,
a tracing-config change), because ``scope`` is now an explicit, app-supplied run identity,
not the ambient trace. Driven by a scripted fake model; no API key, no network.

If an SDK upgrade changes call-id recording or RunState semantics, this gate fails — the
signal to re-run conformance before trusting positional keys on it.
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
    trace,
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
from sakrit.adapters.openai_agents import (  # noqa: E402
    OpenAIAgentsAdapter,
    sakrit_failure_error_function,
    tool_boundary,
)
from sakrit.core import ArgClass, Coordinate, DivergentRetry  # noqa: E402
from sakrit.core.errors import SakritError  # noqa: E402

pytestmark = pytest.mark.integration

ADAPTER = OpenAIAgentsAdapter()
RUN_SCOPE = "run-conf-1"  # the app's stable, persisted run identity (what scope must be)
set_trace_processors([])  # traces still created; never exported → no network / 401 noise


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
        with tool_boundary(ctx, scope=RUN_SCOPE):
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
    assert len(caps) == 4  # 2 calls × 2 resumes
    for label, coords in by_label.items():  # STABLE across resume
        assert len(coords) == 1, f"coordinate NOT stable across resume for {label!r}: {coords}"
    distinct = {next(iter(c)) for c in by_label.values()}  # UNIQUE per call
    assert len(distinct) == 2, f"coordinates NOT unique per call: {by_label}"
    assert {c.scope for c in distinct} == {RUN_SCOPE}  # scope is the explicit run identity


def test_coordinate_is_stable_under_ambient_trace_variation() -> None:
    """Fable A-1 regression: the SAME saved state resumed plain, then inside
    ``with trace("workflow")``, then under a tracing-config flip must yield the SAME
    coordinate every time — because scope is the explicit run identity, not the ambient
    trace the earlier build read (which varied per resume and re-fired the effect)."""
    import asyncio

    caps: list[Coordinate | None] = []

    @function_tool(needs_approval=True)
    async def act(ctx: ToolContext) -> str:
        with tool_boundary(ctx, scope=RUN_SCOPE):
            caps.append(ADAPTER.current_coordinate())
        return "ok"

    async def scenario() -> None:
        model = ScriptedModel([_tool_call("call_X", "act", "{}")], [_final("done")])
        agent = Agent(name="conf", instructions="i", model=model, tools=[act])
        result, saved = await _interrupt_and_save(agent, "go")
        await _resume(agent, saved, result.interruptions)  # plain ambient
        with trace("workflow"):  # SDK-recommended grouping — the A-1 killer
            await _resume(agent, saved, result.interruptions)
        await Runner.run(  # tracing-config flip
            agent,
            await _approved(agent, saved, result.interruptions),
            run_config=RunConfig(tracing_disabled=True),
        )

    async def _approved(agent: Agent, saved: str, interruptions: list[Any]) -> Any:
        state = await RunState.from_string(agent, saved)
        for item in interruptions:
            state.approve(item)
        return state

    asyncio.run(scenario())
    assert len(caps) == 3 and all(c is not None for c in caps)
    assert len({c for c in caps}) == 1  # identical across every ambient — no drift


def test_exactly_once_through_sakrit_survives_a_trace_wrapped_resume(tmp_path: Path) -> None:
    """The money cell + the A-1 killer: a Sakrit-guarded effect fires once even when the
    second resume of the same saved state runs inside ``with trace(...)`` (the pattern that
    previously minted a fresh scope and re-fired)."""
    import asyncio

    fired: list[str] = []
    decl = EffectDecl("mail.send", {"to": ArgClass.IDENTITY})
    ledger = SqliteLedger(tmp_path / "ledger.db")
    sk = Sakrit(ledger, secret=b"s3cret", adapter=OpenAIAgentsAdapter())

    def _send(to: str) -> str:
        fired.append(to)
        return f"sent:{to}"

    @function_tool(needs_approval=True, failure_error_function=None)
    async def send_email(ctx: ToolContext, to: str) -> str:
        with tool_boundary(ctx, scope=RUN_SCOPE):
            return str(sk.guard(decl, _send, kwargs={"to": to}))

    async def scenario() -> None:
        model = ScriptedModel(
            [_tool_call("call_M", "send_email", '{"to": "ops@x.com"}')],
            [_final("done")],
        )
        agent = Agent(name="mailer", instructions="i", model=model, tools=[send_email])
        result, saved = await _interrupt_and_save(agent, "send it")
        await _resume(agent, saved, result.interruptions)  # resume 1
        with trace("workflow"):
            await _resume(agent, saved, result.interruptions)  # resume 2, wrapped

    try:
        asyncio.run(scenario())
    finally:
        ledger.close()
    assert fired == ["ops@x.com"]  # wrapped resume replayed — the effect did not re-fire


def test_distinct_runs_get_distinct_scopes_even_with_colliding_call_ids(tmp_path: Path) -> None:
    """Fable A-2 regression: with deterministic (non-OpenAI) call ids that repeat across runs
    (``call_0``), two *different* logical runs must not collide — because each supplies its own
    explicit scope, so the coordinates differ even when the call ids match."""
    import asyncio

    fired: list[str] = []
    decl = EffectDecl("mail.send", {"to": ArgClass.IDENTITY})
    ledger = SqliteLedger(tmp_path / "ledger.db")
    sk = Sakrit(ledger, secret=b"s3cret", adapter=OpenAIAgentsAdapter())

    def _send(to: str) -> str:
        fired.append(to)
        return f"sent:{to}"

    def _tool(run_scope: str) -> Any:
        @function_tool(failure_error_function=None)
        async def send_email(ctx: ToolContext, to: str) -> str:
            with tool_boundary(ctx, scope=run_scope):
                return str(sk.guard(decl, _send, kwargs={"to": to}))

        return send_email

    async def run_once(run_scope: str, to: str) -> None:
        model = ScriptedModel(
            [_tool_call("call_0", "send_email", f'{{"to": "{to}"}}')], [_final("done")]
        )
        agent = Agent(name="m", instructions="i", model=model, tools=[_tool(run_scope)])
        await Runner.run(agent, "go")

    try:
        asyncio.run(run_once("run-A", "a@x.com"))
        asyncio.run(run_once("run-B", "b@x.com"))  # same call_0, different run scope + args
    finally:
        ledger.close()
    assert fired == ["a@x.com", "b@x.com"]  # both genuinely-new actions fired — no collision


def test_sakrit_refusal_propagates_to_the_app_not_the_model(tmp_path: Path) -> None:
    """Fable A-3 regression: a ``SakritError`` raised in a guarded tool must reach the app
    loudly, not be stringified into model-visible 'please try again'. With
    ``failure_error_function=sakrit_failure_error_function`` the run RAISES (the SDK surfaces
    the re-raised error to the app as a ``UserError`` whose ``__cause__`` is the SakritError —
    loud, operator-visible, never model-visible); an ordinary error is still stringified for
    the model, so that run completes. That discrimination is the whole point."""
    import asyncio

    from agents.exceptions import AgentsException

    @function_tool(failure_error_function=sakrit_failure_error_function)
    async def refuse(ctx: ToolContext) -> str:
        with tool_boundary(ctx, scope=RUN_SCOPE):
            raise DivergentRetry("a different action is wearing this settled key")

    @function_tool(failure_error_function=sakrit_failure_error_function)
    async def ordinary(ctx: ToolContext) -> str:
        with tool_boundary(ctx, scope=RUN_SCOPE):
            raise ValueError("just a normal tool error")

    def _agent(tool: Any, name: str) -> Agent:
        model = ScriptedModel([_tool_call(f"c_{name}", tool.name, "{}")], [_final("done")])
        return Agent(name=name, instructions="i", model=model, tools=[tool])

    # Sakrit refusal → propagates out of Runner.run (the app halts). The SDK wraps a re-raised
    # non-AgentsException, so the halt arrives as an AgentsException carrying the SakritError.
    with pytest.raises(AgentsException) as ei:
        asyncio.run(Runner.run(_agent(refuse, "refuse"), "go"))
    causes = []
    cur: BaseException | None = ei.value
    while cur is not None:
        causes.append(cur)
        cur = cur.__cause__
    assert any(isinstance(c, DivergentRetry) for c in causes), (
        "the SakritError did not reach the app"
    )

    # Ordinary error → stringified for the model, run completes (the discrimination).
    result = asyncio.run(Runner.run(_agent(ordinary, "ordinary"), "go"))
    assert result.final_output == "done"


def test_no_scope_yields_no_coordinate_and_falls_to_the_ladder() -> None:
    """Without an explicit scope the adapter never fabricates one from the ambient trace — it
    yields no coordinate, so a positional guard falls to the ladder (needs key=)."""
    import asyncio

    caps: list[Coordinate | None] = []

    @function_tool
    async def act(ctx: ToolContext) -> str:
        with tool_boundary(ctx):  # no scope
            caps.append(ADAPTER.current_coordinate())
        return "ok"

    model = ScriptedModel([_tool_call("call_T", "act", "{}")], [_final("done")])
    agent = Agent(name="conf", instructions="i", model=model, tools=[act])
    asyncio.run(Runner.run(agent, "go"))
    assert caps == [None]  # no scope → no coordinate → ladder


def test_outside_tool_boundary_returns_none() -> None:
    assert ADAPTER.current_coordinate() is None  # falls to the ladder, never guesses


def test_blank_tool_call_id_is_refused_loudly() -> None:
    class _Stub:
        tool_call_id = "   "

    boundary = tool_boundary(_Stub())  # type: ignore[arg-type]
    with pytest.raises(SakritError, match="tool_call_id"):
        boundary.__enter__()
