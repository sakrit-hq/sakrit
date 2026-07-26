# SPDX-License-Identifier: Apache-2.0
"""The exactly-once mechanism end to end: claim → execute → record, replay, divergence."""

import pytest

from sakrit.adapters import FakeAdapter
from sakrit.core import (
    AmbiguousOutcome,
    ArgClass,
    DivergentRetry,
    EffectDecl,
    EffectState,
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
    led = SqliteLedger()
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
    led = SqliteLedger()
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


def test_crash_in_window_surfaces_ambiguous() -> None:
    led = SqliteLedger()
    key = _key()
    fp = _fp(to="a@x.com")
    # Simulate a crash between EXECUTING and record: claim + mark, never record.
    led.claim(key, "run-1", "email.send", fp)
    led.mark_executing(key)

    with pytest.raises(AmbiguousOutcome):
        settle(led, key=key, scope="run-1", tool="email.send", fingerprint=fp, fn=lambda: "x")
    assert led.state_of(key) is EffectState.AMBIGUOUS


def test_recover_moves_executing_to_ambiguous() -> None:
    led = SqliteLedger()
    key = _key()
    led.claim(key, "run-1", "email.send", "fp")
    led.mark_executing(key)
    assert led.recover() == [key]
    assert led.state_of(key) is EffectState.AMBIGUOUS


def test_failure_recorded_then_reattemptable() -> None:
    led = SqliteLedger()
    key = _key()
    fp = _fp(to="a@x.com")

    def boom(**k: object) -> str:
        raise ValueError("smtp down")

    with pytest.raises(ValueError, match="smtp down"):
        settle(led, key=key, scope="run-1", tool="email.send", fingerprint=fp, fn=boom)
    assert led.state_of(key) is EffectState.FAILED

    # A crash-before-dispatch / failed attempt never completed → retry re-owns it.
    out = settle(led, key=key, scope="run-1", tool="email.send", fingerprint=fp, fn=lambda: "ok")
    assert out == "ok"
    assert led.state_of(key) is EffectState.SUCCEEDED


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
