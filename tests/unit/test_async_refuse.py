# SPDX-License-Identifier: Apache-2.0
"""P1-1a: guarding an async tool synchronously would record success before the
effect runs — so we refuse it, loudly, at decoration time (and belt-and-braces in
settle for wrappers that dodge the signature check)."""

from collections.abc import Generator

import pytest

from sakrit import EffectDecl, Sakrit, SqliteLedger
from sakrit.core import ArgClass, EffectState, SakritError, positional_key, settle
from sakrit.core.coordinate import Coordinate

DECL = EffectDecl("t.send", {"to": ArgClass.IDENTITY})
SECRET = b"deployment-secret"


def test_effect_decorator_rejects_async_at_decoration() -> None:
    sk = Sakrit(SqliteLedger(), secret=SECRET)
    with pytest.raises(SakritError, match="async"):

        @sk.effect(DECL, key="k")
        async def send(to: str) -> None: ...


def test_guard_rejects_async() -> None:
    sk = Sakrit(SqliteLedger(), secret=SECRET)

    async def send(to: str) -> str:
        return "x"

    with pytest.raises(SakritError, match="async"):
        sk.guard(DECL, send, kwargs={"to": "a@x.com"}, key="k")


class _FakeAwaitable:
    """Awaitable but not a coroutine function — dodges the decoration-time check."""

    def __await__(self) -> Generator[None, None, None]:
        yield


def test_settle_refuses_awaitable_result_before_recording() -> None:
    led = SqliteLedger()

    def sync_wrapper() -> _FakeAwaitable:  # not a coroutine *function* → passes _reject_async
        return _FakeAwaitable()

    key = positional_key(Coordinate("global", b"k"), "t.send")
    with pytest.raises(SakritError, match="awaitable"):
        settle(led, key=key, scope="global", tool="t.send", fingerprint="fp", fn=sync_wrapper)
    # The critical invariant: it did NOT record SUCCEEDED before the effect ran.
    assert led.state_of(key) is not EffectState.SUCCEEDED
