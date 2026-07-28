# SPDX-License-Identifier: Apache-2.0
"""The exactly-once mechanism end to end: claim → execute → record, replay, divergence."""

import pytest

from sakrit.adapters import FakeAdapter
from sakrit.core import (
    AmbiguousOutcome,
    ArgClass,
    DivergentRetry,
    EffectDecl,
    EffectInFlightError,
    EffectState,
    Replayed,
    SqliteLedger,
    fingerprint,
    positional_key,
    settle,
)

DECL = EffectDecl("email.send", {"to": ArgClass.IDENTITY, "body": ArgClass.CONTENT})
SECRET = b"deployment-secret"


def _key(scope: str = "run-1", site: str = "send") -> str:
    return positional_key(FakeAdapter(scope).at(site), "email.send")


def _fp(**identity_and_content: object) -> str:
    return fingerprint(DECL, identity_and_content, secret=SECRET)


def test_executes_once_then_replays_reworded() -> None:
    led = SqliteLedger(":memory:")
    calls: list[dict[str, object]] = []

    def effect(**kw: object) -> dict[str, int]:
        calls.append(kw)
        return {"delivery_id": len(calls)}

    key = _key()
    r1 = settle(
        led,
        key=key,
        scope="run-1",
        tool="email.send",
        fingerprint=_fp(to="a@x.com", body="hello"),
        fn=effect,
        kwargs={"to": "a@x.com", "body": "hello"},
    )
    # A resume re-runs the node; the LLM rewords the body (content) → same key,
    # same fingerprint → replay the saved result, do NOT execute again.
    r2 = settle(
        led,
        key=key,
        scope="run-1",
        tool="email.send",
        fingerprint=_fp(to="a@x.com", body="HELLO, rephrased entirely"),
        fn=effect,
        kwargs={"to": "a@x.com", "body": "HELLO, rephrased entirely"},
    )
    assert r1 == r2 == {"delivery_id": 1}
    assert len(calls) == 1  # exactly once
    assert led.state_of(key) is EffectState.SUCCEEDED


def test_divergent_identity_raises_and_does_not_execute() -> None:
    led = SqliteLedger(":memory:")
    key = _key()
    settle(
        led,
        key=key,
        scope="run-1",
        tool="email.send",
        fingerprint=_fp(to="a@x.com", body="hi"),
        fn=lambda **k: "first",
        kwargs={"to": "a@x.com", "body": "hi"},
    )
    executed = False

    def must_not_run(**k: object) -> str:
        nonlocal executed
        executed = True
        return "second"

    with pytest.raises(DivergentRetry):
        settle(
            led,
            key=key,
            scope="run-1",
            tool="email.send",
            fingerprint=_fp(to="DIFFERENT@x.com", body="hi"),  # identity arg changed
            fn=must_not_run,
            kwargs={"to": "DIFFERENT@x.com", "body": "hi"},
        )
    assert executed is False


def test_claim_on_executing_refuses_then_recovery_surfaces_ambiguous() -> None:
    led = SqliteLedger(":memory:")
    key = _key()
    fp = _fp(to="a@x.com")
    # Simulate a crash between EXECUTING and record: claim + mark, never record.
    led.claim(key, "run-1", "email.send", fp)
    led.mark_executing(key)

    # A claim can't distinguish a crash artifact from a live executor → it refuses.
    with pytest.raises(EffectInFlightError):
        settle(led, key=key, scope="run-1", tool="email.send", fingerprint=fp, fn=lambda: "x")
    assert led.state_of(key) is EffectState.EXECUTING  # claim made no transition

    # Recovery has death-evidence and owns the transition.
    assert led.recover() == [key]
    assert led.state_of(key) is EffectState.AMBIGUOUS
    with pytest.raises(AmbiguousOutcome):
        settle(led, key=key, scope="run-1", tool="email.send", fingerprint=fp, fn=lambda: "x")


def test_recover_moves_executing_to_ambiguous() -> None:
    led = SqliteLedger(":memory:")
    key = _key()
    led.claim(key, "run-1", "email.send", "fp")
    led.mark_executing(key)
    assert led.recover() == [key]
    assert led.state_of(key) is EffectState.AMBIGUOUS


def test_undeclared_exception_is_ambiguous_not_failed() -> None:
    # An unclassified exception (a timeout may have executed) must NOT become a
    # retriable FAILED. The row stays EXECUTING; a retry surfaces as ambiguous.
    led = SqliteLedger(":memory:")
    key = _key()
    fp = _fp(to="a@x.com")

    def boom(**k: object) -> str:
        raise TimeoutError("the POST may or may not have landed")

    with pytest.raises(TimeoutError):
        settle(led, key=key, scope="run-1", tool="email.send", fingerprint=fp, fn=boom)
    assert led.state_of(key) is EffectState.EXECUTING  # not FAILED

    # In-process, a retry hits the refusing claim (no recovery has run yet)…
    with pytest.raises(EffectInFlightError):
        settle(led, key=key, scope="run-1", tool="email.send", fingerprint=fp, fn=lambda: "ok")
    # …and after recovery the ambiguity is surfaced, never silently re-executed.
    assert led.recover() == [key]
    with pytest.raises(AmbiguousOutcome):
        settle(led, key=key, scope="run-1", tool="email.send", fingerprint=fp, fn=lambda: "ok")


def test_declared_clean_failure_is_reattemptable() -> None:
    # A declared clean failure asserts non-execution → FAILED, safely re-claimable.
    led = SqliteLedger(":memory:")
    key = _key()
    fp = _fp(to="a@x.com")

    def boom(**k: object) -> str:
        raise ValueError("validation error, raised before any I/O")

    with pytest.raises(ValueError, match="validation"):
        settle(
            led,
            key=key,
            scope="run-1",
            tool="email.send",
            fingerprint=fp,
            fn=boom,
            clean_failures=(ValueError,),
        )
    assert led.state_of(key) is EffectState.FAILED

    out = settle(
        led,
        key=key,
        scope="run-1",
        tool="email.send",
        fingerprint=fp,
        fn=lambda: "ok",
        clean_failures=(ValueError,),
    )
    assert out == "ok"
    assert led.state_of(key) is EffectState.SUCCEEDED


def test_g2_plain_divergent_retry_of_a_failed_row_refuses() -> None:
    # G-2: a clean FAILED row retried with DIFFERENT identity args is a divergent retry — the
    # key names one action, not one tool. The plain path used to silently re-own it (fire the
    # new action); now it refuses loudly, matching the leased path and the documented model.
    led = SqliteLedger(":memory:")
    key = _key()

    def boom(**k: object) -> str:
        raise ValueError("v")

    with pytest.raises(ValueError):
        settle(
            led, key=key, scope="run-1", tool="email.send",
            fingerprint=_fp(to="a@x.com"), fn=boom, clean_failures=(ValueError,),
        )  # fmt: skip
    assert led.state_of(key) is EffectState.FAILED

    fired: list[int] = []
    with pytest.raises(DivergentRetry, match="divergent retry"):
        settle(
            led, key=key, scope="run-1", tool="email.send",
            fingerprint=_fp(to="b@x.com"), fn=lambda: fired.append(1),
            clean_failures=(ValueError,),
        )  # fmt: skip
    assert fired == []  # the divergent action never fired…
    assert led.state_of(key) is EffectState.FAILED  # …and the row is untouched (rolled back)


def test_g2_divergent_retry_of_failed_refuses_identically_on_both_paths() -> None:
    # G-2 consistency (Fable's cell): the same divergent retry of a provably-not-run FAILED row
    # raises DivergentRetry on BOTH the single-worker (plain) and multi-worker (leased) paths —
    # no dev→fleet semantic cliff.
    plain = SqliteLedger(":memory:")
    plain.claim("k", "s", "email.send", _fp(to="a@x.com"))
    plain.record_failure("k", ValueError("boom"))
    with pytest.raises(DivergentRetry):
        plain.claim("k", "s", "email.send", _fp(to="b@x.com"))

    leased = SqliteLedger(":memory:")
    leased.claim_leased(
        "k", "s", "email.send", _fp(to="a@x.com"), owner="A", lease_seconds=30.0, now=100.0
    )
    leased.record_failure("k", ValueError("boom"))
    with pytest.raises(DivergentRetry):
        leased.claim_leased(
            "k", "s", "email.send", _fp(to="b@x.com"), owner="B", lease_seconds=30.0, now=200.0
        )


def test_g2_plain_reown_divergence_gate_is_rotation_aware() -> None:
    # G-2 must not repeat C-1's rotation false-positive: a SAME-action retry of an old-signed
    # FAILED row, verified through the rotation window, re-owns (PROCEED) — not DivergentRetry;
    # a genuinely divergent action (verify=False) still refuses.
    from sakrit.core.ledger import ClaimKind

    ok = SqliteLedger(":memory:")
    ok.claim("k", "s", "email.send", "OLD-signed-fp")
    ok.record_failure("k", ValueError("x"))
    proceed = ok.claim("k", "s", "email.send", "NEW-signed-fp", verify=lambda stored, sid: True)
    assert proceed.kind is ClaimKind.PROCEED  # rotation-aware: same action re-owns

    div = SqliteLedger(":memory:")
    div.claim("k", "s", "email.send", "OLD-signed-fp")
    div.record_failure("k", ValueError("x"))
    with pytest.raises(DivergentRetry):
        div.claim("k", "s", "email.send", "NEW-signed-fp", verify=lambda stored, sid: False)


def test_g2_record_success_clears_stale_error_from_a_prior_failure() -> None:
    # G-2 bonus: a row that cleanly FAILED (error text stored) then succeeds on retry must not
    # carry the prior failure message onto its SUCCEEDED record (an audit-trail lie).
    led = SqliteLedger(":memory:")
    key = _key()
    fp = _fp(to="a@x.com")

    def boom(**k: object) -> str:
        raise ValueError("card 4242 declined")

    with pytest.raises(ValueError):
        settle(
            led, key=key, scope="run-1", tool="email.send",
            fingerprint=fp, fn=boom, clean_failures=(ValueError,),
        )  # fmt: skip
    assert led.conn.execute("SELECT error FROM effects WHERE key=?", (key,)).fetchone()[0]

    out = settle(
        led, key=key, scope="run-1", tool="email.send",
        fingerprint=fp, fn=lambda: "ok", clean_failures=(ValueError,),
    )  # fmt: skip
    assert out == "ok"
    row = led.conn.execute("SELECT state, error FROM effects WHERE key=?", (key,)).fetchone()
    assert row[0] == EffectState.SUCCEEDED.value
    assert row[1] is None  # stale failure text cleared


def test_unserializable_result_records_succeeded_and_replays_as_marker() -> None:
    # Execution truth outranks result fidelity: an unstorable result must still
    # record SUCCEEDED (never FAILED), and replay must not re-execute.
    led = SqliteLedger(":memory:")
    key = _key()
    fp = _fp(to="a@x.com")

    class Handle:  # not JSON-serializable
        pass

    handle = Handle()
    calls = 0

    def effect() -> Handle:
        nonlocal calls
        calls += 1
        return handle

    first = settle(led, key=key, scope="run-1", tool="email.send", fingerprint=fp, fn=effect)
    assert first is handle  # the real object on first execution
    assert led.state_of(key) is EffectState.SUCCEEDED  # NOT FAILED — the effect happened

    replayed = settle(led, key=key, scope="run-1", tool="email.send", fingerprint=fp, fn=effect)
    assert isinstance(replayed, Replayed)  # a marker, not the (unstorable) object
    assert calls == 1  # replay did not re-execute


def test_durable_across_reconnect(tmp_path: "object") -> None:
    # SUCCEEDED survives a fresh connection (simulating a process restart).
    import pathlib

    db = pathlib.Path(str(tmp_path)) / "ledger.sqlite"
    key = _key()
    fp = _fp(to="a@x.com")
    calls = 0

    def effect() -> str:
        nonlocal calls
        calls += 1
        return "sent"

    with SqliteLedger(db) as led:
        settle(led, key=key, scope="run-1", tool="email.send", fingerprint=fp, fn=effect)
    # New connection = new "process": replay, no second execution.
    with SqliteLedger(db) as led2:
        out = settle(led2, key=key, scope="run-1", tool="email.send", fingerprint=fp, fn=effect)
    assert out == "sent"
    assert calls == 1


# --- P4-8: divergence is uniform across row states, not just SUCCEEDED replay ---------
def _make_ambiguous(led: SqliteLedger, key: str, fp: str) -> None:
    """Drive a key to AMBIGUOUS the honest way: an L0 crash in the window (EXECUTING at
    recovery, no provider dedup, no reconcile → surfaced AMBIGUOUS)."""
    led.claim(key, "run-1", "email.send", fp)
    led.mark_executing(key)
    assert led.recover() == [key]
    assert led.state_of(key) is EffectState.AMBIGUOUS


def test_same_action_on_an_ambiguous_row_is_ambiguous() -> None:
    with SqliteLedger(":memory:") as led:
        key, fp = _key(), _fp(to="a@x.com")
        _make_ambiguous(led, key, fp)
        with pytest.raises(AmbiguousOutcome, match="outcome unknown"):
            settle(led, key=key, scope="run-1", tool="email.send", fingerprint=fp, fn=lambda: "x")


def test_different_action_on_an_ambiguous_row_is_divergent_not_ambiguous() -> None:
    # P4-8: a genuinely DIFFERENT action colliding on an AMBIGUOUS key must say "different
    # action", not send the operator to investigate "did my effect land?" for one they never
    # issued.
    with SqliteLedger(":memory:") as led:
        key = _key()
        _make_ambiguous(led, key, _fp(to="a@x.com"))
        with pytest.raises(DivergentRetry, match="different action collides"):
            settle(
                led,
                key=key,
                scope="run-1",
                tool="email.send",
                fingerprint=_fp(to="b@x.com"),  # a different recipient → different identity
                fn=lambda: "x",
            )
