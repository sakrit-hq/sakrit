# SPDX-License-Identifier: Apache-2.0
"""The LangGraph adapter's conformance gate.

Positional identity rests on one property: the adapter's ``call_site`` must be
**byte-stable across a resume** (so a re-executed node dedups) and **unique per
logical step** (so distinct actions don't collide). This test asserts exactly that
for the *shipped* ``LangGraphAdapter``, across linear, loop, and ``Send`` fan-out
topologies — the empirical gate behind ``docs/design.md`` §5 and Q12. Graduated
from ``research/repro/conformance.py``.

If a LangGraph upgrade changes how ``checkpoint_ns`` is derived, this test fails —
which is the signal to drain in-flight runs before adopting that version.
"""

import operator
from pathlib import Path
from typing import Annotated, Any, TypedDict

import pytest

pytest.importorskip("langgraph")

from langgraph.checkpoint.sqlite import SqliteSaver  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402
from langgraph.types import Command, Send, interrupt  # noqa: E402

from sakrit.adapters.langgraph import LangGraphAdapter  # noqa: E402
from sakrit.core import Coordinate  # noqa: E402

pytestmark = pytest.mark.integration

ADAPTER = LangGraphAdapter()


def _coord() -> Coordinate:
    c = ADAPTER.current_coordinate()
    assert c is not None, "the adapter returned no coordinate inside a node"
    return c


def _assert_stable_and_unique(captures: list[tuple[object, Coordinate]]) -> None:
    by_marker: dict[object, set[Coordinate]] = {}
    for marker, coord in captures:
        by_marker.setdefault(marker, set()).add(coord)

    # STABLE: a re-executed step (same marker across resume) → same coordinate.
    for marker, coords in by_marker.items():
        assert len(coords) == 1, f"call-site NOT stable across resume for {marker!r}: {coords}"

    # UNIQUE: distinct logical steps → distinct coordinates.
    per_step = {m: next(iter(cs)) for m, cs in by_marker.items()}
    assert len(set(per_step.values())) == len(per_step), (
        f"call-site NOT unique per step: {per_step}"
    )


def _run_resume(build: Any, ckpt: Path, *, initial: dict[str, Any]) -> None:
    with SqliteSaver.from_conn_string(str(ckpt)) as saver:
        graph = build(saver)
        config = {"configurable": {"thread_id": "conf"}}
        graph.invoke(initial, config)  # runs to the interrupt
        graph.invoke(Command(resume="go"), config)  # re-executes the paused step(s)


def test_callsite_linear(tmp_path: Path) -> None:
    caps: list[tuple[object, Coordinate]] = []

    def node(state: dict[str, Any]) -> dict[str, Any]:
        caps.append(("only", _coord()))
        interrupt({"pause": True})
        return {"done": True}

    def build(saver: Any) -> Any:
        g = StateGraph(dict)
        g.add_node("act", node)
        g.add_edge(START, "act")
        g.add_edge("act", END)
        return g.compile(checkpointer=saver)

    _run_resume(build, tmp_path / "ckpt.sqlite", initial={})
    assert sum(1 for m, _ in caps if m == "only") == 2  # ran on both invoke and resume
    _assert_stable_and_unique(caps)


def test_callsite_loop(tmp_path: Path) -> None:
    caps: list[tuple[object, Coordinate]] = []
    n, interrupt_at = 4, 2

    def step(state: dict[str, Any]) -> dict[str, Any]:
        i = state["i"]
        caps.append((("iter", i), _coord()))
        if i == interrupt_at:
            interrupt({"at": i})
        return {"i": i + 1}

    def route(state: dict[str, Any]) -> str:
        return "step" if state["i"] < n else END

    def build(saver: Any) -> Any:
        g = StateGraph(dict)
        g.add_node("step", step)
        g.add_edge(START, "step")
        g.add_conditional_edges("step", route, {"step": "step", END: END})
        return g.compile(checkpointer=saver)

    _run_resume(build, tmp_path / "ckpt.sqlite", initial={"i": 0})
    assert sum(1 for m, _ in caps if m == ("iter", interrupt_at)) == 2  # iteration re-ran
    _assert_stable_and_unique(caps)


def test_callsite_fanout(tmp_path: Path) -> None:
    caps: list[tuple[object, Coordinate]] = []
    recipients = ["alice", "bob", "carol"]

    class FanState(TypedDict, total=False):
        recipient: str
        results: Annotated[list[str], operator.add]

    def worker(state: FanState) -> dict[str, Any]:
        r = state["recipient"]
        caps.append((("recip", r), _coord()))
        if r == recipients[0]:
            interrupt({"for": r})  # one worker pauses → unambiguous resume
        return {"results": [r]}

    def dispatch(state: FanState) -> list[Send]:
        return [Send("worker", {"recipient": r}) for r in recipients]

    def build(saver: Any) -> Any:
        g = StateGraph(FanState)
        g.add_node("worker", worker)
        g.add_conditional_edges(START, dispatch, ["worker"])
        g.add_edge("worker", END)
        return g.compile(checkpointer=saver)

    _run_resume(build, tmp_path / "ckpt.sqlite", initial={"results": []})
    assert sum(1 for m, _ in caps if m == ("recip", recipients[0])) == 2  # worker re-ran
    _assert_stable_and_unique(caps)
