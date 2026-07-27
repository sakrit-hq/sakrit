# SPDX-License-Identifier: Apache-2.0
"""LangGraph ToolNode × Sakrit loud-refusal propagation (Fable C-5 / the A-3 class).

Pins the favorable behavior as a *tested contract* rather than luck: the default ToolNode
propagates a SakritError out of the graph (so a DivergentRetry/AmbiguousOutcome halt reaches
the app, not the model). Also documents the hazard — handle_tool_errors=True swallows the
halt into model-visible text — and verifies the sakrit_handle_tool_errors mitigation. If a
future LangGraph flips the default (0.x→1.x flipped once already), the first test fails loudly.
"""

from typing import Annotated, Any, TypedDict

import pytest

pytest.importorskip("langgraph")

from langchain_core.messages import AIMessage  # noqa: E402
from langchain_core.tools import tool  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402
from langgraph.graph.message import add_messages  # noqa: E402
from langgraph.prebuilt import ToolNode  # noqa: E402

from sakrit.adapters.langgraph import sakrit_handle_tool_errors  # noqa: E402
from sakrit.core.errors import AmbiguousOutcome  # noqa: E402

pytestmark = pytest.mark.integration


class _State(TypedDict):
    messages: Annotated[list, add_messages]


@tool
def refuse() -> str:
    """A guarded tool whose Sakrit guard raised a loud halt."""
    raise AmbiguousOutcome("mail-k1: a prior attempt crashed after dispatch; outcome unknown")


@tool
def ordinary() -> str:
    """A tool that failed for a mundane reason the model may retry."""
    raise ValueError("transient upstream hiccup")


def _invoke(node: ToolNode, name: str) -> Any:
    """Run the ToolNode inside a compiled graph (it needs the runtime context), driving it with
    an AIMessage that carries a single tool call — the shape a model would emit."""
    g = StateGraph(_State)
    g.add_node("tools", node)
    g.add_edge(START, "tools")
    g.add_edge("tools", END)
    graph = g.compile()
    call = AIMessage(content="", tool_calls=[{"name": name, "args": {}, "id": "c1"}])
    return graph.invoke({"messages": [call]})


def test_default_toolnode_propagates_a_sakrit_halt() -> None:
    # The contract: LangGraph 1.x's default handler re-raises a SakritError (only
    # ToolInvocationError is stringified), so the halt reaches the app, not the model.
    with pytest.raises(AmbiguousOutcome):
        _invoke(ToolNode([refuse]), "refuse")


def test_handle_tool_errors_true_swallows_the_halt_to_the_model() -> None:
    # The documented hazard (the old 0.x default, pervasive in tutorials): the halt becomes a
    # model-visible ToolMessage instead of propagating — the app never sees it.
    out = _invoke(ToolNode([refuse], handle_tool_errors=True), "refuse")
    content = out["messages"][-1].content
    assert "AmbiguousOutcome" in content  # swallowed into text, NOT raised


def test_sakrit_handle_tool_errors_reraises_sakrit_but_stringifies_others() -> None:
    # The mitigation for apps that want custom text for ordinary errors while keeping the halt
    # loud: a SakritError propagates; an ordinary error becomes a model-visible message.
    with pytest.raises(AmbiguousOutcome):
        _invoke(ToolNode([refuse], handle_tool_errors=sakrit_handle_tool_errors), "refuse")

    out = _invoke(ToolNode([ordinary], handle_tool_errors=sakrit_handle_tool_errors), "ordinary")
    assert "transient upstream hiccup" in out["messages"][-1].content  # stringified for the model
