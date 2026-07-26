# SPDX-License-Identifier: Apache-2.0
"""P1-1b: real async support. guard_async / settle_async await the effect, then record —
so an async tool runs exactly once, replays on retry, and never records before it runs."""

import asyncio

import pytest

from sakrit import EffectDecl, Sakrit, SqliteLedger, current_key
from sakrit.core import ArgClass, DivergentRetry, EffectState, positional_key, settle_async
from sakrit.core.coordinate import Coordinate

DECL = EffectDecl("t.send", {"to": ArgClass.IDENTITY})
SECRET = b"deployment-secret"


def test_async_effect_runs_exactly_once_and_replays() -> None:
    sk = Sakrit(SqliteLedger(), secret=SECRET)
    calls: list[str] = []

    @sk.effect(DECL, key="welcome")
    async def send(to: str) -> str:
        await asyncio.sleep(0)  # a real await point
        calls.append(to)
        return f"sent:{to}"

    async def scenario() -> tuple[object, object]:
        first = await send(to="a@x.com")
        second = await send(to="a@x.com")  # retry → replay, no re-send
        return first, second

    first, second = asyncio.run(scenario())
    assert first == "sent:a@x.com"
    assert second == "sent:a@x.com"  # replayed
    assert calls == ["a@x.com"]  # the effect fired exactly once


def test_async_divergent_identity_arg_refuses() -> None:
    sk = Sakrit(SqliteLedger(), secret=SECRET)

    @sk.effect(DECL, key="welcome")
    async def send(to: str) -> str:
        return f"sent:{to}"

    async def scenario() -> None:
        await send(to="a@x.com")
        await send(to="b@x.com")  # same key, different identity arg → DivergentRetry

    with pytest.raises(DivergentRetry):
        asyncio.run(scenario())


def test_async_tool_can_read_current_key() -> None:
    sk = Sakrit(SqliteLedger(), secret=SECRET)
    seen: dict[str, object] = {}

    @sk.effect(DECL, key="order-9")
    async def send(to: str) -> str:
        await asyncio.sleep(0)
        seen["key"] = current_key()  # the contextvar survives the await
        return "ok"

    asyncio.run(send(to="a@x.com"))
    expected = positional_key(Coordinate("global", b"order-9"), "t.send")
    assert seen["key"] == expected


def test_settle_async_records_only_after_the_await() -> None:
    # The invariant P1-1 protects: SUCCEEDED is written *after* the effect, never before.
    led = SqliteLedger()
    key = positional_key(Coordinate("global", b"k"), "t.send")
    order: list[str] = []

    async def effect() -> str:
        order.append("effect-ran")
        return "done"

    async def go() -> object:
        return await settle_async(
            led, key=key, scope="global", tool="t.send", fingerprint="fp", fn=effect
        )

    result = asyncio.run(go())
    assert result == "done"
    assert order == ["effect-ran"]
    assert led.state_of(key) is EffectState.SUCCEEDED


def test_async_clean_failure_is_reclaimable() -> None:
    led = SqliteLedger()
    key = positional_key(Coordinate("global", b"k"), "t.send")

    class Rejected(Exception):
        pass

    async def effect_fail() -> str:
        raise Rejected("provider 400 — nothing done")

    async def go() -> object:
        return await settle_async(
            led,
            key=key,
            scope="global",
            tool="t.send",
            fingerprint="fp",
            fn=effect_fail,
            clean_failures=(Rejected,),
        )

    with pytest.raises(Rejected):
        asyncio.run(go())
    assert led.state_of(key) is EffectState.FAILED  # declared-clean → re-claimable
