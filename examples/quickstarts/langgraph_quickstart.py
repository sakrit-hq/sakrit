# SPDX-License-Identifier: Apache-2.0
"""LangGraph quickstart — the classic double-send, fixed.

A LangGraph node that sends an email *before* a human-approval ``interrupt()`` re-runs on resume, so
the email sends twice. Wrap the tool with Sakrit and it sends once — the guard replays the recorded
result on the resumed superstep instead of re-executing.

Run directly (needs the ``langgraph`` extra: ``pip install "sakrit[langgraph]"``)::

    python examples/quickstarts/langgraph_quickstart.py

Executed verbatim in CI (tests/integration/test_quickstarts.py) under the langgraph extra.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from sakrit import EffectDecl, Sakrit, SqliteLedger
from sakrit.adapters.langgraph import LangGraphAdapter
from sakrit.core import ArgClass

SEND = EffectDecl(
    "email.send",
    {"to": ArgClass.IDENTITY, "subject": ArgClass.IDENTITY, "body": ArgClass.CONTENT},
)
CONFIG = {"configurable": {"thread_id": "order-4471"}}
ORDER = {"to": "customer@example.com", "subject": "Your order shipped", "body": "on its way"}


def main() -> None:
    sent: list[str] = []

    with tempfile.TemporaryDirectory() as tmp:
        ledger = SqliteLedger(Path(tmp) / "sakrit.db")
        # The adapter reads LangGraph's stable per-step coordinate (checkpoint_ns) — no key needed.
        sk = Sakrit(ledger, secret=b"<per-deployment-secret>", adapter=LangGraphAdapter())

        @sk.effect(SEND)
        def send_email(to: str, subject: str, body: str) -> dict[str, int]:
            sent.append(to)  # stand-in for a real send
            return {"delivery_id": len(sent)}

        def approve_and_send(state: dict[str, str]) -> dict[str, bool]:
            # Side effect BEFORE the interrupt — the classic pitfall, now guarded.
            send_email(to=state["to"], subject=state["subject"], body=state["body"])
            decision = interrupt({"question": "Approve?"})
            return {"approved": decision == "approve"}

        with SqliteSaver.from_conn_string(str(Path(tmp) / "ckpt.sqlite")) as saver:
            graph = StateGraph(dict)
            graph.add_node("approve_and_send", approve_and_send)
            graph.add_edge(START, "approve_and_send")
            graph.add_edge("approve_and_send", END)
            app = graph.compile(checkpointer=saver)
            app.invoke(ORDER, CONFIG)  # sends, then pauses at the approval interrupt
            app.invoke(Command(resume="approve"), CONFIG)  # the node re-runs on resume

        ledger.close()

    assert sent == ["customer@example.com"], f"expected one send, got {sent}"
    print(f"fired exactly once across the interrupt/resume: {sent}")


if __name__ == "__main__":
    main()
