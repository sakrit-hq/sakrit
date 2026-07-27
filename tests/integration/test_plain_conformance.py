# SPDX-License-Identifier: Apache-2.0
"""The plain (framework-free) path's conformance gate — roadmap #14, C1.

Sakrit's "dependency, not destination" claim: with **no agent framework and no
adapter**, positional identity comes from the declared ladder rungs alone — an
explicit ``key=`` (rung 1) or ``step=``+``scope=`` (rung 3) — and exactly-once
must hold across a process restart. This gate asserts exactly that, with the
restart modeled honestly: the first engine's ledger is **closed** (releasing the
single-worker flock, as process death would) and a *fresh* ``Sakrit`` over the
same DB file re-runs the same code.

The same stable+unique property the LangGraph gate asserts, restated for the
plain path: a re-executed step (same rung inputs) must land on the SAME
coordinate (dedup → replay, no re-fire), and distinct logical steps (different
key/step/scope/occurrence — or different *rungs* of the same bytes, P4-6) must
land on DISTINCT coordinates (both fire).
"""

import asyncio
from pathlib import Path

import pytest

from sakrit import EffectDecl, Sakrit, SqliteLedger
from sakrit.core import ArgClass
from sakrit.core.errors import DivergentRetry, NoCoordinateError

pytestmark = pytest.mark.integration

SECRET = b"deployment-secret"
DECL = EffectDecl("notify.send", {"to": ArgClass.IDENTITY})


def _engine(db: Path, **ledger_kwargs: object) -> tuple[Sakrit, SqliteLedger]:
    ledger = SqliteLedger(db, **ledger_kwargs)  # type: ignore[arg-type]
    return Sakrit(ledger, secret=SECRET), ledger


def test_key_dedups_across_restart(tmp_path: Path) -> None:
    """Rung 1: the same business key re-run after a restart replays — the effect
    does not re-fire. No framework anywhere in sight."""
    db, fired = tmp_path / "ledger.db", []

    def run() -> object:
        sk, ledger = _engine(db)

        def send(to: str) -> str:
            fired.append(to)
            return "sent"

        try:
            return sk.guard(DECL, send, kwargs={"to": "ops@x.com"}, key="invoice-123")
        finally:
            ledger.close()  # the restart boundary: flock released, engine discarded

    first, second = run(), run()
    assert fired == ["ops@x.com"]  # fired exactly once across both "processes"
    assert first == "sent" and second == "sent"  # the replay serves the recorded result


def test_step_scope_stable_across_restart_and_unique_per_step(tmp_path: Path) -> None:
    """Rung 3: within one scope, each declared step fires once across a restart
    (stable), and distinct steps / distinct scopes each fire (unique)."""
    db, fired = tmp_path / "ledger.db", []

    def run(scope: str) -> None:
        sk, ledger = _engine(db)

        def send(to: str) -> str:
            fired.append((scope, to))
            return "sent"

        try:
            sk.guard(DECL, send, kwargs={"to": "a@x.com"}, step="send-a", scope=scope)
            sk.guard(DECL, send, kwargs={"to": "b@x.com"}, step="send-b", scope=scope)
        finally:
            ledger.close()

    run("run-1")
    run("run-1")  # the resume: both steps replay, nothing re-fires
    run("run-2")  # a different run: same step names are a fresh retry domain — both fire
    assert fired == [
        ("run-1", "a@x.com"),
        ("run-1", "b@x.com"),
        ("run-2", "a@x.com"),
        ("run-2", "b@x.com"),
    ]


def test_loop_resume_mid_way_fires_only_the_tail(tmp_path: Path) -> None:
    """The real resume shape for a plain script: a loop over recipients crashes
    after 2 of 3 sends; the rerun replays the settled iterations and fires only
    the tail. Iterations are separated with ``sk.step(occurrence=i)`` (P4-1)."""
    db, fired = tmp_path / "ledger.db", []
    recipients = ["a@x.com", "b@x.com", "c@x.com"]

    def run(upto: int) -> None:
        sk, ledger = _engine(db)

        @sk.effect(DECL, key="campaign-7")
        def send(to: str) -> str:
            fired.append(to)
            return "sent"

        try:
            for i, r in enumerate(recipients[:upto]):
                with sk.step(occurrence=i):
                    send(to=r)
        finally:
            ledger.close()

    run(upto=2)  # "crash" after the second send
    run(upto=3)  # restart re-runs the whole loop
    assert fired == recipients  # 0 and 1 replayed; only 2 fired on the rerun


def test_divergent_args_after_restart_refuse_loudly(tmp_path: Path) -> None:
    """Same coordinate, different identity args across the restart: that is a
    different action wearing a settled step's address — a loud ``DivergentRetry``,
    never a silent replay of the wrong result."""
    db, fired = tmp_path / "ledger.db", []

    def run(to: str) -> None:
        sk, ledger = _engine(db)

        def send(to: str) -> str:
            fired.append(to)
            return "sent"

        try:
            sk.guard(DECL, send, kwargs={"to": to}, key="invoice-123")
        finally:
            ledger.close()

    run("ops@x.com")
    with pytest.raises(DivergentRetry):
        run("attacker@evil.com")
    assert fired == ["ops@x.com"]  # the divergent retry fired nothing


def test_rungs_of_identical_bytes_are_distinct_actions(tmp_path: Path) -> None:
    """P4-6 end-to-end: ``key="X"`` (rung 1) and ``step="X"`` in the same scope
    (rung 3) are different intents — both must fire, neither may replay the other."""
    db, fired = tmp_path / "ledger.db", []
    sk, ledger = _engine(db)

    def send(to: str) -> str:
        fired.append(to)
        return "sent"

    try:
        # Rung 1's key= scope is "global" by construction — pin rung 3 to the same
        # scope so the only thing separating the coordinates is the rung tag.
        sk.guard(DECL, send, kwargs={"to": "ops@x.com"}, key="X")
        sk.guard(DECL, send, kwargs={"to": "ops@x.com"}, step="X", scope="global")
    finally:
        ledger.close()
    assert fired == ["ops@x.com", "ops@x.com"]  # two rows, two fires — no cross-rung swallow


def test_no_coordinate_refuses(tmp_path: Path) -> None:
    """Rung 4: with no adapter and no declared rung, the plain path refuses —
    it never fabricates identity from args."""
    sk, ledger = _engine(tmp_path / "ledger.db")
    try:
        with pytest.raises(NoCoordinateError):
            sk.guard(DECL, lambda to: "sent", kwargs={"to": "ops@x.com"})
    finally:
        ledger.close()


def test_async_plain_path_dedups_across_restart(tmp_path: Path) -> None:
    """The async twin of the rung-1 cell: an ``async def`` tool guarded via the
    decorator, replayed (not re-awaited) after the restart."""
    db, fired = tmp_path / "ledger.db", []

    async def run() -> object:
        sk, ledger = _engine(db)

        @sk.effect(DECL, key="invoice-123")
        async def send(to: str) -> str:
            fired.append(to)
            return "sent"

        try:
            return await send(to="ops@x.com")
        finally:
            ledger.close()

    async def scenario() -> tuple[object, object]:
        return await run(), await run()

    first, second = asyncio.run(scenario())
    assert fired == ["ops@x.com"]
    assert first == "sent" and second == "sent"


def test_multiworker_plain_path_one_fire_across_workers(tmp_path: Path) -> None:
    """The plain path against a multi-worker ledger: two workers (each its own
    connection, per V-3/V-6) guard the same business key — exactly one fires, the
    other replays the settled result via the leased protocol."""
    db, fired = tmp_path / "ledger.db", []

    def worker() -> object:
        sk, _ = _engine(db, multi_worker=True)

        def send(to: str) -> str:
            fired.append(to)
            return "sent"

        return sk.guard(DECL, send, kwargs={"to": "ops@x.com"}, key="invoice-123")

    first, second = worker(), worker()
    assert fired == ["ops@x.com"]  # the second worker replayed, not re-fired
    assert first == "sent" and second == "sent"
