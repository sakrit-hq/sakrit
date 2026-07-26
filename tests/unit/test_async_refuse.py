# SPDX-License-Identifier: Apache-2.0
"""P1-1: guarding an async tool *synchronously* would record success before the effect
runs. The sync path refuses it loudly — sk.guard rejects async, and settle has a
belt-and-braces awaitable check. (Real async support lives via guard_async; see
test_async_support.py.)"""

import inspect
from collections.abc import Generator

import pytest

from sakrit import EffectDecl, Sakrit, SqliteLedger
from sakrit.core import ArgClass, EffectState, SakritError, positional_key, settle
from sakrit.core.coordinate import Coordinate

DECL = EffectDecl("t.send", {"to": ArgClass.IDENTITY})
SECRET = b"deployment-secret"


def test_effect_decorator_accepts_async_and_yields_async_wrapper() -> None:
    # Move 2 (P1-1b): the decorator now *supports* async — it routes to guard_async and
    # returns an async wrapper, rather than rejecting at decoration.
    sk = Sakrit(SqliteLedger(":memory:"), secret=SECRET)

    @sk.effect(DECL, key="k")
    async def send(to: str) -> None: ...

    assert inspect.iscoroutinefunction(send)


def test_sync_guard_still_rejects_async() -> None:
    sk = Sakrit(SqliteLedger(":memory:"), secret=SECRET)

    async def send(to: str) -> str:
        return "x"

    with pytest.raises(SakritError, match="async"):
        sk.guard(DECL, send, kwargs={"to": "a@x.com"}, key="k")


class _FakeAwaitable:
    """Awaitable but not a coroutine function — dodges the decoration-time check."""

    def __await__(self) -> Generator[None, None, None]:
        yield


def test_settle_refuses_awaitable_result_before_recording() -> None:
    led = SqliteLedger(":memory:")

    def sync_wrapper() -> _FakeAwaitable:  # not a coroutine *function* → passes _reject_async
        return _FakeAwaitable()

    key = positional_key(Coordinate("global", b"k"), "t.send")
    with pytest.raises(SakritError, match="did not run"):
        settle(led, key=key, scope="global", tool="t.send", fingerprint="fp", fn=sync_wrapper)
    # The critical invariant: it did NOT record SUCCEEDED before the effect ran.
    assert led.state_of(key) is not EffectState.SUCCEEDED
