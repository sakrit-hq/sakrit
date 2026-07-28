# SPDX-License-Identifier: Apache-2.0
"""P3-1: the CLAIMED window is not a re-ownable state in the claim path.

Single-worker is not single-thread: parallel branches / concurrent tool-calls can
hold a *live* CLAIMED row. Claiming has no death-evidence, so it must refuse a
CLAIMED (or EXECUTING) row rather than blindly re-owning it — which would
re-dispatch a still-running effect. Re-ownership belongs to recover(), which runs
at startup and therefore *does* have death-evidence: it blesses a crash-leftover
CLAIMED as INTENDED, and only then is it re-claimable.
"""

import pytest

from sakrit.core import EffectState, SqliteLedger
from sakrit.core.errors import DivergentRetry, EffectInFlightError
from sakrit.core.ledger import ClaimKind


def _claim(led: SqliteLedger, key: str = "k") -> None:
    led.claim(key, "run-1", "t.send", "fp")


def test_claim_on_claimed_row_refuses() -> None:
    """A second concurrent claim of a live CLAIMED row must not re-own it."""
    led = SqliteLedger(":memory:")
    _claim(led)  # A: INTENDED-absent → CLAIMED, PROCEED
    assert led.state_of("k") is EffectState.CLAIMED
    with pytest.raises(EffectInFlightError, match="CLAIMED"):
        _claim(led)  # B: no death-evidence → refuse


def test_recovery_blesses_claimed_leftover_as_intended() -> None:
    """A crash between claim and dispatch leaves CLAIMED; recovery makes it INTENDED."""
    led = SqliteLedger(":memory:")
    _claim(led)  # crash right after claim, before mark_executing
    assert led.recover() == []  # a bare CLAIMED leftover is not AMBIGUOUS
    assert led.state_of("k") is EffectState.INTENDED


def test_intended_is_re_claimable() -> None:
    """Recovery-blessed INTENDED re-owns to PROCEED on a retry of the *same* action.

    A genuine retry recomputes the same fingerprint (identity args are the same) → it re-owns.
    """
    led = SqliteLedger(":memory:")
    _claim(led)
    led.recover()  # CLAIMED → INTENDED
    claim = led.claim("k", "run-1", "t.send", "fp")  # same action → same fingerprint
    assert claim.kind is ClaimKind.PROCEED
    assert led.state_of("k") is EffectState.CLAIMED  # re-owned, fresh fingerprint


def test_divergent_retry_of_intended_refuses() -> None:
    """G-2: re-owning an INTENDED row with DIFFERENT identity args is a divergent retry.

    A fingerprint only differs when the identity args differ — i.e. a genuinely different
    action colliding on the key. The key names one action, not one tool, so refuse it (loudly,
    matching the leased path and the documented model) rather than fire a different action.
    """
    led = SqliteLedger(":memory:")
    _claim(led)  # signs "fp"
    led.recover()  # CLAIMED → INTENDED
    with pytest.raises(DivergentRetry, match="divergent retry"):
        led.claim("k", "run-1", "t.send", "fp-different")
    assert led.state_of("k") is EffectState.INTENDED  # untouched (rolled back)
