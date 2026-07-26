# SPDX-License-Identifier: Apache-2.0
"""P4-1: repeats at one call site need distinct occurrences, or the second is silently
swallowed (identical args) / loudly refused (differing args). Tier-2 ships a manual
mechanism — sk.step() — and pins the un-built automatic folding (tier-3) with an xfail."""

import pytest

from sakrit import Sakrit, SqliteLedger
from sakrit.core import ArgClass, EffectDecl

SECRET = b"deployment-secret"
DECL = EffectDecl("notify.remind", {"to": ArgClass.IDENTITY})


def _sk_and_sink() -> tuple[Sakrit, list[str]]:
    sk = Sakrit(SqliteLedger(), secret=SECRET)
    return sk, []


def test_step_makes_identical_repeats_each_fire() -> None:
    sk, fired = _sk_and_sink()

    @sk.effect(DECL, key="reminder")
    def remind(to: str) -> str:
        fired.append(to)
        return "sent"

    for i in range(3):
        with sk.step(occurrence=i):
            remind(to="ops@example.com")  # identical args every time
    assert fired == ["ops@example.com"] * 3  # all fired — none swallowed


def test_step_loop_over_recipients_all_fire() -> None:
    sk, fired = _sk_and_sink()

    @sk.effect(DECL, key="reminder")
    def remind(to: str) -> str:
        fired.append(to)
        return "sent"

    recipients = ["a@x.com", "b@x.com", "c@x.com"]
    for i, r in enumerate(recipients):
        with sk.step(occurrence=i):
            remind(to=r)  # differing args — would be DivergentRetry without distinct occ
    assert fired == recipients


def test_same_occurrence_still_replays_once() -> None:
    # Idempotency is preserved *within* an occurrence: a genuine retry replays.
    sk, fired = _sk_and_sink()

    @sk.effect(DECL, key="reminder")
    def remind(to: str) -> str:
        fired.append(to)
        return "sent"

    with sk.step(occurrence=0):
        remind(to="a@x.com")
    with sk.step(occurrence=0):
        remind(to="a@x.com")  # same occurrence + same args → replay
    assert fired == ["a@x.com"]  # fired exactly once


def test_unwrapped_identical_repeat_is_swallowed_today() -> None:
    # Pinning the P4-1 trap: without sk.step, two identical calls collide on one key and
    # the second is silently swallowed. This is the behavior the tier-3 fix must change.
    sk, fired = _sk_and_sink()

    @sk.effect(DECL, key="reminder")
    def remind(to: str) -> str:
        fired.append(to)
        return "sent"

    remind(to="a@x.com")
    remind(to="a@x.com")
    assert fired == ["a@x.com"]  # only once — the silent swallow


@pytest.mark.xfail(
    strict=True,
    reason="P4-1 tier-3: automatic occurrence folding is not built; use sk.step() until "
    "it lands (docs/dev-notes/occurrence.md).",
)
def test_auto_occurrence_folding_repeats_fire() -> None:
    # The desired future behavior: two deliberate identical calls at one site each fire,
    # with no manual sk.step. Not implemented — expected to fail until folding ships.
    sk, fired = _sk_and_sink()

    @sk.effect(DECL, key="reminder")
    def remind(to: str) -> str:
        fired.append(to)
        return "sent"

    remind(to="a@x.com")
    remind(to="a@x.com")
    assert fired == ["a@x.com", "a@x.com"]
