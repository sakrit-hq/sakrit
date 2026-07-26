# SPDX-License-Identifier: Apache-2.0
"""V-1: a generator / async-generator tool returns a lazy iterator and runs NONE of its
body — so guarding it would record SUCCEEDED before any effect ran (the silent-lost-effect
inversion). Refuse it: at decoration, in guard / guard_async, and belt-and-braces in
settle / settle_async for a *returned* generator that dodged the function-level check."""

import asyncio
from collections.abc import AsyncIterator, Iterator

import pytest

from sakrit import EffectDecl, Sakrit, SqliteLedger
from sakrit.core import (
    ArgClass,
    EffectState,
    SakritError,
    positional_key,
    settle,
    settle_async,
)
from sakrit.core.coordinate import Coordinate

DECL = EffectDecl("t.send", {"to": ArgClass.IDENTITY})
SECRET = b"deployment-secret"


def test_decorator_refuses_sync_generator_at_decoration() -> None:
    sk = Sakrit(SqliteLedger(), secret=SECRET)
    with pytest.raises(SakritError, match="generator"):

        @sk.effect(DECL, key="k")
        def send(to: str):  # type: ignore[no-untyped-def]
            yield to  # a generator: body runs only on iteration


def test_decorator_refuses_async_generator_at_decoration() -> None:
    sk = Sakrit(SqliteLedger(), secret=SECRET)
    with pytest.raises(SakritError, match="generator"):

        @sk.effect(DECL, key="k")
        async def send(to: str):  # type: ignore[no-untyped-def]
            yield to


def test_sync_guard_refuses_generator() -> None:
    sk = Sakrit(SqliteLedger(), secret=SECRET)

    def send(to: str):  # type: ignore[no-untyped-def]
        yield to

    with pytest.raises(SakritError, match="generator"):
        sk.guard(DECL, send, kwargs={"to": "a@x.com"}, key="k")


def test_guard_async_refuses_async_generator() -> None:
    sk = Sakrit(SqliteLedger(), secret=SECRET)

    async def send(to: str):  # type: ignore[no-untyped-def]
        yield to

    with pytest.raises(SakritError, match="generator"):
        asyncio.run(sk.guard_async(DECL, send, kwargs={"to": "a@x.com"}, key="k"))


def test_settle_belt_refuses_a_returned_generator_before_recording() -> None:
    # A plain function (NOT a generator function) that hands back a generator object —
    # dodges the decoration check; the settle belt must still refuse it.
    led = SqliteLedger()

    def wrapper() -> Iterator[int]:
        def g() -> Iterator[int]:
            yield 1

        return g()  # a generator object; wrapper itself is a normal function

    key = positional_key(Coordinate("global", b"k"), "t.send")
    with pytest.raises(SakritError, match="generator"):
        settle(led, key=key, scope="global", tool="t.send", fingerprint="fp", fn=wrapper)
    assert led.state_of(key) is not EffectState.SUCCEEDED  # never recorded before it ran


def test_settle_async_belt_refuses_a_returned_async_generator() -> None:
    led = SqliteLedger()

    async def wrapper() -> AsyncIterator[int]:
        async def ag() -> AsyncIterator[int]:
            yield 1

        return ag()  # an async_generator object; wrapper is a coroutine function

    key = positional_key(Coordinate("global", b"k"), "t.send")

    async def go() -> object:
        return await settle_async(
            led, key=key, scope="global", tool="t.send", fingerprint="fp", fn=wrapper
        )

    with pytest.raises(SakritError, match="generator"):
        asyncio.run(go())
    assert led.state_of(key) is not EffectState.SUCCEEDED
