# SPDX-License-Identifier: Apache-2.0
"""The demo, on the real core + real LangGraph: the double-email now sends once.

This is the Act II payoff and an end-to-end integration test — it exercises the
LangGraph adapter (``call_site = checkpoint_ns``), the coordinate ladder, the
positional key, the fingerprint, and the ledger, across a real interrupt/resume.
"""

from pathlib import Path

import pytest

pytest.importorskip("langgraph")

from langgraph.checkpoint.sqlite import SqliteSaver  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402
from langgraph.types import Command, interrupt  # noqa: E402

from sakrit import EffectDecl, Sakrit, SqliteLedger  # noqa: E402
from sakrit.adapters.langgraph import LangGraphAdapter  # noqa: E402
from sakrit.core import ArgClass  # noqa: E402

SEND_DECL = EffectDecl(
    "email.send",
    {"to": ArgClass.IDENTITY, "subject": ArgClass.IDENTITY, "body": ArgClass.CONTENT},
)
CONFIG = {"configurable": {"thread_id": "demo-1"}}
MESSAGE = {"to": "customer@example.com", "subject": "Your order shipped", "body": "on its way"}


def _run_interrupt_resume(node: object, ckpt: Path) -> None:
    """Run a one-node graph to its interrupt, then resume it (a new superstep)."""
    with SqliteSaver.from_conn_string(str(ckpt)) as saver:
        graph = StateGraph(dict)
        graph.add_node("approve_and_send", node)  # type: ignore[arg-type]
        graph.add_edge(START, "approve_and_send")
        graph.add_edge("approve_and_send", END)
        compiled = graph.compile(checkpointer=saver)
        compiled.invoke(MESSAGE, CONFIG)  # sends, pauses at the approval interrupt
        compiled.invoke(Command(resume="approve"), CONFIG)  # node re-runs on resume


@pytest.mark.integration
def test_guarded_send_fires_once_across_interrupt_resume(tmp_path: Path) -> None:
    outbox: list[dict[str, str]] = []
    ledger = SqliteLedger(tmp_path / "ledger.sqlite")
    sk = Sakrit(ledger, secret=b"demo-secret", adapter=LangGraphAdapter())

    @sk.effect(SEND_DECL)
    def send_email(to: str, subject: str, body: str) -> dict[str, int]:
        outbox.append({"to": to, "subject": subject})
        return {"delivery_id": len(outbox)}

    def node(state: dict[str, str]) -> dict[str, bool]:
        # Side effect BEFORE the interrupt — the classic pitfall. Now guarded.
        send_email(to=state["to"], subject=state["subject"], body=state["body"])
        decision = interrupt({"question": "Approve?"})
        return {"approved": decision == "approve"}

    _run_interrupt_resume(node, tmp_path / "ckpt.sqlite")

    assert len(outbox) == 1  # exactly once — the guard replayed on resume
    ledger.close()


@pytest.mark.integration
def test_unguarded_control_sends_twice(tmp_path: Path) -> None:
    # The "before" picture: the identical graph without Sakrit re-sends on resume.
    outbox: list[dict[str, str]] = []

    def send_email(to: str, subject: str, body: str) -> None:
        outbox.append({"to": to, "subject": subject})

    def node(state: dict[str, str]) -> dict[str, bool]:
        send_email(to=state["to"], subject=state["subject"], body=state["body"])
        decision = interrupt({"question": "Approve?"})
        return {"approved": decision == "approve"}

    _run_interrupt_resume(node, tmp_path / "ckpt.sqlite")

    assert len(outbox) == 2  # the bug: the node re-ran and sent again
