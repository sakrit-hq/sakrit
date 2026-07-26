# SPDX-License-Identifier: Apache-2.0
"""P3-8: the engine drives the leased protocol. A multi-worker ledger routes guard /
@sk.effect through settle_leased (leases + fencing + takeover), so multi-worker
exactly-once is reachable from the public 'three lines' surface — no hand-fed keys."""

import asyncio
import threading
from pathlib import Path

import pytest

from sakrit import EffectDecl, Sakrit, SqliteLedger
from sakrit.core import ArgClass, SakritError

SECRET = b"deployment-secret"
DECL = EffectDecl("notify.remind", {"to": ArgClass.IDENTITY})


def _token(led: SqliteLedger) -> int:
    return int(led.conn.execute("SELECT fencing_token FROM effects").fetchone()[0])


def test_leased_engine_takes_the_fenced_path(tmp_path: Path) -> None:
    led = SqliteLedger(tmp_path / "l.sqlite", multi_worker=True)
    sk = Sakrit(led, secret=SECRET)

    @sk.effect(DECL, key="k")
    def do(to: str) -> str:
        return "ok"

    assert do(to="a@x.com") == "ok"
    assert _token(led) >= 1  # the leased path fenced the row; unfenced settle leaves 0
    led.close()


def test_single_worker_engine_takes_the_unfenced_path() -> None:
    led = SqliteLedger()  # single-worker :memory:
    sk = Sakrit(led, secret=SECRET)

    @sk.effect(DECL, key="k")
    def do(to: str) -> str:
        return "ok"

    do(to="a@x.com")
    assert _token(led) == 0  # settle never touches the fencing token


def test_leased_engine_replays_on_second_call(tmp_path: Path) -> None:
    led = SqliteLedger(tmp_path / "l.sqlite", multi_worker=True)
    sk = Sakrit(led, secret=SECRET)
    calls: list[str] = []

    @sk.effect(DECL, key="k")
    def do(to: str) -> str:
        calls.append(to)
        return "ok"

    first = do(to="x@y.com")
    second = do(to="x@y.com")  # replay through the leased loop
    assert first == second == "ok"
    assert calls == ["x@y.com"]  # fired exactly once
    led.close()


def test_leased_engine_concurrent_workers_fire_once(tmp_path: Path) -> None:
    # The real proof: N workers, each its own Sakrit + connection to a shared file
    # ledger, racing the same key through the public surface → the effect fires once.
    db = tmp_path / "l.sqlite"
    n = 6
    fired: list[str] = []
    fired_lock = threading.Lock()
    results: dict[int, object] = {}
    start = threading.Barrier(n)

    def worker(i: int) -> None:
        led = SqliteLedger(db, multi_worker=True)
        sk = Sakrit(led, secret=SECRET)

        @sk.effect(DECL, key="the-effect")
        def do(to: str) -> str:
            with fired_lock:
                fired.append(to)
            return "sent"

        try:
            start.wait()  # release all workers together
            results[i] = do(to="a@x.com")
        finally:
            led.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(fired) == 1  # exactly one worker executed the effect
    assert len(results) == n and all(r == "sent" for r in results.values())  # all got the result


def test_leased_engine_captures_provider_ttl(tmp_path: Path) -> None:
    # P1-5 (leased): the TTL flows decl → guard → settle_leased → claim_leased onto the row,
    # so a later L2 takeover past the horizon can surface instead of silently duplicating.
    led = SqliteLedger(tmp_path / "l.sqlite", multi_worker=True)
    sk = Sakrit(led, secret=SECRET)
    decl = EffectDecl(
        "pay.charge", {"amt": ArgClass.IDENTITY}, provider_key_param="idk", provider_ttl_s=86400
    )

    @sk.effect(decl, key="o1")
    def charge(amt: int) -> dict[str, str]:
        return {"id": "c1"}

    charge(amt=100)
    stored = led.conn.execute("SELECT provider_ttl_s FROM effects").fetchone()[0]
    assert stored == 86400
    led.close()


def test_guard_async_refuses_in_leased_mode(tmp_path: Path) -> None:
    # settle_leased is a blocking sync loop and cannot await an async tool; refuse loudly
    # rather than run the coroutine unawaited (which would re-open the P1-1 hole).
    led = SqliteLedger(tmp_path / "l.sqlite", multi_worker=True)
    sk = Sakrit(led, secret=SECRET)

    @sk.effect(DECL, key="k")
    async def do(to: str) -> str:
        return "ok"

    with pytest.raises(SakritError, match="leased"):
        asyncio.run(do(to="a@x.com"))
    led.close()
