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


# --- P4-3 edge cells: multi-turn re-invocation and the empty-namespace guard ---------
def test_callsite_second_invoke_same_thread_is_a_distinct_step(tmp_path: Path) -> None:
    """The standard multi-turn chat deployment: the SAME thread is invoked again with new
    input. Each turn re-executes the node; the conformance property must hold *across* turns,
    not only across a resume within one run — a second turn must get its OWN coordinate, or its
    guarded effect would silently replay the first turn's (a same-args swallow). Within a turn,
    invoke→interrupt→resume must still dedup to one coordinate."""
    caps: list[tuple[object, Coordinate]] = []

    def node(state: dict[str, Any]) -> dict[str, Any]:
        turn = state["turn"]
        caps.append((("turn", turn), _coord()))
        interrupt({"pause": turn})
        return {"done": turn}

    def build(saver: Any) -> Any:
        g = StateGraph(dict)
        g.add_node("act", node)
        g.add_edge(START, "act")
        g.add_edge("act", END)
        return g.compile(checkpointer=saver)

    with SqliteSaver.from_conn_string(str(tmp_path / "ckpt.sqlite")) as saver:
        graph = build(saver)
        config = {"configurable": {"thread_id": "chat"}}
        graph.invoke({"turn": 1}, config)  # turn 1 → interrupt
        graph.invoke(Command(resume="go"), config)  # turn 1 resume (re-runs the node)
        graph.invoke({"turn": 2}, config)  # turn 2, new input → interrupt
        graph.invoke(Command(resume="go"), config)  # turn 2 resume

    assert sum(1 for m, _ in caps if m == ("turn", 1)) == 2  # turn 1 ran on invoke + resume
    assert sum(1 for m, _ in caps if m == ("turn", 2)) == 2  # turn 2 ran on invoke + resume
    # STABLE within a turn, UNIQUE across turns — so turn 2 cannot swallow turn 1.
    _assert_stable_and_unique(caps)


def test_callsite_functional_api(tmp_path: Path) -> None:
    """The functional API (``@entrypoint`` / ``@task``) — named in P4-3 as a candidate
    empty-namespace source. The entrypoint body must be resume-stable, each ``@task`` call
    must get its own coordinate, and none may be blank. (Probed: entrypoint → ``flow:<uuid>``
    stable across resume; each task → ``flow:<uuid>|do:<uuid>`` distinct per call.)"""
    from langgraph.func import entrypoint, task

    caps: list[tuple[object, Coordinate]] = []

    @task  # type: ignore[misc]
    def do(i: int) -> int:
        caps.append((("task", i), _coord()))
        return i

    with SqliteSaver.from_conn_string(str(tmp_path / "f.sqlite")) as saver:

        @entrypoint(checkpointer=saver)  # type: ignore[misc]
        def flow(n: int) -> list[int]:
            caps.append((("entry", None), _coord()))
            interrupt({"pause": True})
            return [do(i).result() for i in range(n)]

        config = {"configurable": {"thread_id": "F"}}
        flow.invoke(3, config)  # entrypoint → interrupt
        flow.invoke(Command(resume="go"), config)  # resume: re-runs entrypoint, runs the tasks

    assert sum(1 for m, _ in caps if m == ("entry", None)) == 2  # entrypoint re-ran on resume
    assert sum(1 for m, _ in caps if m[0] == "task") == 3  # three distinct task calls
    assert all(coord.call_site.strip() for _, coord in caps), "a blank ns leaked from the func API"
    _assert_stable_and_unique(caps)


def test_no_topology_emits_an_empty_checkpoint_ns(tmp_path: Path) -> None:
    """The empty-namespace hazard, asserted directly against the shipped adapter: across the
    linear/loop/fan-out/multi-turn topologies above, LangGraph must never hand a node a blank
    ``checkpoint_ns`` (which would collapse every step onto one coordinate). This is the
    empirical half of the P4-3 guard — the other half (that a blank ns is *refused*, not
    silently collided) is ``test_empty_checkpoint_ns_is_refused`` below."""
    seen: list[bytes] = []

    def node(state: dict[str, Any]) -> dict[str, Any]:
        seen.append(_coord().call_site)
        return {"n": state.get("n", 0) + 1}

    def build(saver: Any) -> Any:
        g = StateGraph(dict)
        g.add_node("act", node)
        g.add_edge(START, "act")
        g.add_edge("act", END)
        return g.compile(checkpointer=saver)

    with SqliteSaver.from_conn_string(str(tmp_path / "ckpt.sqlite")) as saver:
        graph = build(saver)
        config = {"configurable": {"thread_id": "chat"}}
        graph.invoke({}, config)
        graph.invoke({}, config)  # a second turn, too

    assert seen, "the node never ran"
    assert all(cs.strip() for cs in seen), f"a blank checkpoint_ns leaked into a coordinate: {seen}"


def test_empty_checkpoint_ns_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A blank ``checkpoint_ns`` cannot serve positional identity (it is not unique per step),
    so the adapter must return ``None`` — falling to the ladder (an explicit ``key=`` or a loud
    ``NoCoordinateError``) — rather than manufacture a colliding ``scope + b""`` coordinate.
    Simulated by pinning ``get_config`` to the empty-namespace shape LangGraph could yield."""
    import sakrit.adapters.langgraph as lg

    def _cfg(ns: str, thread: str) -> dict[str, Any]:
        return {"configurable": {"checkpoint_ns": ns, "thread_id": thread}}

    for ns in ("", "   ", "\t"):
        monkeypatch.setattr(lg, "get_config", lambda ns=ns: _cfg(ns, "T"))
        coord = lg.LangGraphAdapter().current_coordinate()
        assert coord is None, f"blank ns {ns!r} was not refused"

    # A blank thread_id is equally fail-unsafe (every thread would share one scope) → also refused.
    monkeypatch.setattr(lg, "get_config", lambda: _cfg("act:uuid", ""))
    assert lg.LangGraphAdapter().current_coordinate() is None
