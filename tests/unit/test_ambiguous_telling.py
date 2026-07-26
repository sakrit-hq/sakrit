# SPDX-License-Identifier: Apache-2.0
"""P1-6: ambiguity is never silent. Every transition into AMBIGUOUS logs a warning
and fires the optional on_ambiguous alert/metric hook — the differentiating half of
the guarantee ("prevents the duplicate *or tells you it couldn't*")."""

import logging

import pytest

from sakrit.core import EffectState, SqliteLedger

LEASE = 30.0


def _l0_executing(led: SqliteLedger, key: str = "k") -> None:
    led.claim(key, "s", "t", "fp")
    led.mark_executing(key)


def test_recover_l0_fires_the_hook() -> None:
    told: list[str] = []
    led = SqliteLedger(on_ambiguous=told.append)
    _l0_executing(led)
    assert led.recover() == ["k"]
    assert told == ["k"]


def test_ambiguate_fires_the_hook() -> None:
    told: list[str] = []
    led = SqliteLedger(on_ambiguous=told.append)
    _l0_executing(led)
    led.ambiguate("k")
    assert told == ["k"]


def test_leased_l0_takeover_fires_the_hook() -> None:
    told: list[str] = []
    led = SqliteLedger(on_ambiguous=told.append)
    led.claim_leased("k", "s", "t", "fp", owner="A", now=100.0, lease_seconds=LEASE)
    led.fence("k", 1, EffectState.EXECUTING)
    led.claim_leased("k", "s", "t", "fp", owner="B", now=200.0, lease_seconds=LEASE)
    assert told == ["k"]


def test_fence_ambiguous_fires_the_hook() -> None:
    told: list[str] = []
    led = SqliteLedger(on_ambiguous=told.append)
    led.claim_leased("k", "s", "t", "fp", owner="A", now=100.0, lease_seconds=LEASE)
    led.fence("k", 1, EffectState.EXECUTING)
    assert led.fence("k", 1, EffectState.AMBIGUOUS) is True
    assert told == ["k"]


def test_hook_exception_never_breaks_the_ledger() -> None:
    def boom(key: str) -> None:
        raise RuntimeError("alert pipeline down")

    led = SqliteLedger(on_ambiguous=boom)
    _l0_executing(led)
    assert led.recover() == ["k"]  # the hook raised, but recovery did not
    assert led.state_of("k") is EffectState.AMBIGUOUS  # row is durably ambiguous


def test_ambiguity_always_logs_even_without_a_hook(caplog: pytest.LogCaptureFixture) -> None:
    led = SqliteLedger()  # no hook configured
    _l0_executing(led)
    with caplog.at_level(logging.WARNING, logger="sakrit"):
        led.recover()
    assert "AMBIGUOUS" in caplog.text
    assert "k" in caplog.text
