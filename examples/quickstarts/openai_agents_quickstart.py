# SPDX-License-Identifier: Apache-2.0
"""OpenAI Agents SDK quickstart — exactly-once across an approval interruption.

An approval-gated function tool re-runs when a saved run state is resumed. Wrap the effect with
Sakrit and it fires once. The integration you write is small: an ``OpenAIAgentsAdapter`` on the
engine, and a ``tool_boundary(ctx, scope=<your stable run id>)`` around the guarded call so the
adapter can read the SDK's per-call coordinate under an explicit run scope.

Run directly (needs the ``openai-agents`` extra: ``pip install "sakrit[openai-agents]"``)::

    python examples/quickstarts/openai_agents_quickstart.py

It runs **offline** — a scripted fake model stands in for the LLM, so there is no API key and no
network. Executed verbatim in CI (tests/integration/test_quickstarts.py) under the extra.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

from agents import Agent, Runner, RunState, function_tool, set_trace_processors
from agents.items import ModelResponse
from agents.models.interface import Model
from agents.tool_context import ToolContext
from agents.usage import Usage
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)

from sakrit import EffectDecl, Sakrit, SqliteLedger
from sakrit.adapters.openai_agents import OpenAIAgentsAdapter, tool_boundary
from sakrit.core import ArgClass

SEND = EffectDecl(
    "email.send",
    {"to": ArgClass.IDENTITY, "subject": ArgClass.IDENTITY, "body": ArgClass.CONTENT},
)
RUN_ID = "run-8815"  # your stable, persisted run identity — what `scope` must be
set_trace_processors([])  # don't export traces (no network / no 401 noise)


def main() -> None:
    sent: list[str] = []

    with tempfile.TemporaryDirectory() as tmp:
        ledger = SqliteLedger(Path(tmp) / "sakrit.db")
        sk = Sakrit(ledger, secret=b"<per-deployment-secret>", adapter=OpenAIAgentsAdapter())

        # --- the integration you write --------------------------------------------------
        @sk.effect(SEND)
        def send_email(to: str, subject: str, body: str) -> dict[str, int]:
            sent.append(to)  # stand-in for a real send
            return {"delivery_id": len(sent)}

        @function_tool(needs_approval=True)
        async def email_customer(ctx: ToolContext, to: str) -> str:
            # tool_boundary supplies the explicit run scope the adapter needs.
            with tool_boundary(ctx, scope=RUN_ID):
                send_email(to=to, subject="Your order shipped", body="on its way")
            return "sent"

        agent = Agent(
            name="shipping",
            instructions="Email the customer.",
            model=_scripted(),
            tools=[email_customer],
        )
        # --------------------------------------------------------------------------------

        asyncio.run(_run_with_double_resume(agent))
        ledger.close()

    assert sent == ["customer@example.com"], f"expected one send, got {sent}"
    print(f"fired exactly once across approval + double resume: {sent}")


# --- offline test harness: a scripted model + resume driver (not part of your app) ----------


class _ScriptedModel(Model):
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


def _scripted() -> Model:
    call = ResponseFunctionToolCall(
        type="function_call",
        call_id="call_1",
        name="email_customer",
        arguments='{"to": "customer@example.com"}',
        id="fc_1",
        status="completed",
    )
    final = ResponseOutputMessage(
        type="message",
        id="msg_final",
        role="assistant",
        status="completed",
        content=[ResponseOutputText(type="output_text", text="done", annotations=[])],
    )
    return _ScriptedModel([call], [final])


async def _run_with_double_resume(agent: Agent) -> None:
    result = await Runner.run(agent, "email the customer")
    assert result.interruptions, "the approval-gated tool never interrupted"
    saved = result.to_state().to_string()
    for _ in range(2):  # resume the SAME saved state twice — the guard dedups the re-run
        state = await RunState.from_string(agent, saved)
        for item in result.interruptions:
            state.approve(item)
        await Runner.run(agent, state)


if __name__ == "__main__":
    main()
