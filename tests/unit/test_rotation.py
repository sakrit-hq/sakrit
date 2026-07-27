# SPDX-License-Identifier: Apache-2.0
"""The software dual-secret rotation window: rotate the HMAC secret without a fleet-wide
DivergentRetry storm. Sign new writes with the new secret; keep old secrets in a verify
keyring (keyed by the P5-3 secret_id) so an in-flight row signed by the old secret still
replays during the drain. A genuinely different action still diverges; a secret rotated
clean out of the window is refused loudly, never silently mis-compared."""

from collections.abc import Mapping
from pathlib import Path

import pytest

from sakrit import EffectDecl, Sakrit, SqliteLedger
from sakrit.core import ArgClass, DivergentRetry, SchemeMismatch
from sakrit.core.fingerprint import fingerprint, matches_fingerprint, secret_id

DECL = EffectDecl("mail.send", {"to": ArgClass.IDENTITY})
OLD, NEW, OTHER = b"old-secret", b"new-secret", b"unrelated-secret"


# --- matches_fingerprint (the verification pivot) ------------------------------------
def test_verifies_against_the_signing_secret_not_the_primary() -> None:
    args = {"to": "a@x.com"}
    stored = fingerprint(DECL, args, secret=OLD)  # the row was signed by OLD
    keyring = {secret_id(NEW): NEW, secret_id(OLD): OLD}
    # Same action, verified under OLD (found via the row's secret_id) though primary is NEW.
    assert matches_fingerprint(
        DECL, args, stored, secret_id(OLD), keyring=keyring, primary_secret=NEW
    )
    # A genuinely different action does not match, even within the window.
    assert not matches_fingerprint(
        DECL, {"to": "b@x.com"}, stored, secret_id(OLD), keyring=keyring, primary_secret=NEW
    )


def test_legacy_null_secret_id_verifies_against_primary() -> None:
    args = {"to": "a@x.com"}
    stored = fingerprint(DECL, args, secret=NEW)  # a pre-keyring row, signed by the primary
    assert matches_fingerprint(
        DECL, args, stored, None, keyring={secret_id(NEW): NEW}, primary_secret=NEW
    )


def test_secret_rotated_out_of_window_raises_scheme_mismatch() -> None:
    args = {"to": "a@x.com"}
    stored = fingerprint(DECL, args, secret=OLD)
    keyring = {secret_id(NEW): NEW}  # OLD has been dropped from the window
    with pytest.raises(SchemeMismatch, match="rotated out of the window"):
        matches_fingerprint(DECL, args, stored, secret_id(OLD), keyring=keyring, primary_secret=NEW)


# --- end to end through the engine, across a "redeploy" ------------------------------
def _settle_under(
    db: Path, secret: bytes, to: str, calls: list[str], *, verify_secrets: tuple[bytes, ...] = ()
) -> object:
    def send(to: str) -> str:
        calls.append(to)
        return f"sent:{to}"

    with SqliteLedger(db) as led:
        sk = Sakrit(led, secret=secret, verify_secrets=verify_secrets)
        return sk.guard(DECL, send, kwargs={"to": to}, key="k1")


def test_rotation_replays_a_row_signed_by_the_old_secret(tmp_path: Path) -> None:
    db = tmp_path / "l.db"
    calls: list[str] = []
    _settle_under(db, OLD, "a@x.com", calls)  # deploy 1: signed by OLD
    assert calls == ["a@x.com"]
    # deploy 2: rotate to NEW, keep OLD in the verify window → the row replays, not re-fires.
    out = _settle_under(db, NEW, "a@x.com", calls, verify_secrets=(OLD,))
    assert out == "sent:a@x.com"
    assert calls == ["a@x.com"]  # NOT re-fired despite the secret change — no storm


def test_rotation_still_catches_a_divergent_action(tmp_path: Path) -> None:
    db = tmp_path / "l.db"
    calls: list[str] = []
    _settle_under(db, OLD, "a@x.com", calls)
    # deploy 2 (rotated) guarding the SAME key with a DIFFERENT recipient still diverges loudly.
    with pytest.raises(DivergentRetry):
        _settle_under(db, NEW, "b@x.com", calls, verify_secrets=(OLD,))
    assert calls == ["a@x.com"]  # the divergent action did not fire


def test_secret_rotated_clean_out_of_window_refuses_loudly(tmp_path: Path) -> None:
    db = tmp_path / "l.db"
    calls: list[str] = []
    _settle_under(db, OLD, "a@x.com", calls)  # signed by OLD
    # deploy 2 has rotated past OLD (window holds only OTHER) → the OLD-signed row is refused,
    # not silently mis-compared into a DivergentRetry.
    with pytest.raises(SchemeMismatch, match="rotated out of the window"):
        _settle_under(db, NEW, "a@x.com", calls, verify_secrets=(OTHER,))


# --- C-1: rotation must not brick old-signed non-terminal leased rows -----------------
from sakrit.core.ledger import ClaimKind, EffectState  # noqa: E402

ARGS = {"to": "a@x.com"}


def _leased_verify(args: Mapping[str, object], keyring: dict[str, bytes], primary: bytes):  # type: ignore[no-untyped-def]
    return lambda sfp, sid: matches_fingerprint(
        DECL, args, sfp, sid, keyring=keyring, primary_secret=primary
    )


def _old_signed_row(led: SqliteLedger, key: str, state: EffectState) -> None:
    """Create a leased row signed by OLD in the given non-terminal state (lease expires at 130)."""
    old_fp = fingerprint(DECL, ARGS, secret=OLD)
    c = led.claim_leased(
        key,
        "s",
        DECL.tool,
        old_fp,
        owner="A",
        lease_seconds=30,
        now=100.0,
        secret_id=secret_id(OLD),
    )
    assert c.kind is ClaimKind.PROCEED
    if state is EffectState.EXECUTING:
        assert led.fence(key, c.fencing_token, EffectState.EXECUTING)
    elif state is EffectState.FAILED:
        assert led.fence(key, c.fencing_token, EffectState.FAILED)  # terminal → lease released
    # CLAIMED: leave as-is (crash before dispatch, nothing ran).


@pytest.mark.parametrize("state", [EffectState.CLAIMED, EffectState.FAILED, EffectState.EXECUTING])
def test_rotation_does_not_brick_old_signed_leased_takeover(
    tmp_path: Path, state: EffectState
) -> None:
    keyring = {secret_id(OLD): OLD, secret_id(NEW): NEW}
    new_fp = fingerprint(DECL, ARGS, secret=NEW)
    # Bug shape (no window): the NEW-signed same-action taker is refused by the raw byte-gate.
    with SqliteLedger(tmp_path / f"nb-{state.value}.db", multi_worker=True) as led:
        _old_signed_row(led, "k", state)
        with pytest.raises(DivergentRetry):
            led.claim_leased(
                "k",
                "s",
                DECL.tool,
                new_fp,
                owner="B",
                lease_seconds=30,
                now=200.0,
                secret_id=secret_id(NEW),
            )
    # Fixed (window): verified under the row's OLD secret → takeover proceeds, no false divergence
    # (CLAIMED/FAILED re-own → PROCEED; EXECUTING-L0 → AMBIGUOUS, still not a DivergentRetry).
    with SqliteLedger(tmp_path / f"win-{state.value}.db", multi_worker=True) as led:
        _old_signed_row(led, "k", state)
        c = led.claim_leased(
            "k",
            "s",
            DECL.tool,
            new_fp,
            owner="B",
            lease_seconds=30,
            now=200.0,
            secret_id=secret_id(NEW),
            verify=_leased_verify(ARGS, keyring, NEW),
        )
        if state in (EffectState.CLAIMED, EffectState.FAILED):
            assert c.kind is ClaimKind.PROCEED  # provably-never-ran → re-ownable across rotation


def test_rotation_leased_takeover_still_refuses_a_divergent_action(tmp_path: Path) -> None:
    keyring = {secret_id(OLD): OLD, secret_id(NEW): NEW}
    with SqliteLedger(tmp_path / "div.db", multi_worker=True) as led:
        _old_signed_row(led, "k", EffectState.CLAIMED)
        diff = _leased_verify({"to": "b@x.com"}, keyring, NEW)  # a genuinely different action
        with pytest.raises(DivergentRetry):
            led.claim_leased(
                "k",
                "s",
                DECL.tool,
                fingerprint(DECL, {"to": "b@x.com"}, secret=NEW),
                owner="B",
                lease_seconds=30,
                now=200.0,
                secret_id=secret_id(NEW),
                verify=diff,
            )


def test_rotation_leased_takeover_of_a_rotated_out_secret_is_loud(tmp_path: Path) -> None:
    keyring = {secret_id(NEW): NEW}  # OLD rotated fully out
    with SqliteLedger(tmp_path / "out.db", multi_worker=True) as led:
        _old_signed_row(led, "k", EffectState.CLAIMED)
        with pytest.raises(SchemeMismatch, match="rotated out of the window"):
            led.claim_leased(
                "k",
                "s",
                DECL.tool,
                fingerprint(DECL, ARGS, secret=NEW),
                owner="B",
                lease_seconds=30,
                now=200.0,
                secret_id=secret_id(NEW),
                verify=_leased_verify(ARGS, keyring, NEW),
            )


# --- D-1: the zombie-adopt guard must verify across a rotation, not byte-compare ------
from sakrit.core.leased import _record_success_fenced  # noqa: E402


def _succeeded_old_row(led: SqliteLedger, key: str) -> int:
    """A row driven to SUCCEEDED by a peer signing OLD; returns the (now-stale) token."""
    old_fp = fingerprint(DECL, ARGS, secret=OLD)
    c = led.claim_leased(
        key,
        "s",
        DECL.tool,
        old_fp,
        owner="A",
        lease_seconds=30,
        now=100.0,
        secret_id=secret_id(OLD),
    )
    assert led.fence(key, c.fencing_token, EffectState.EXECUTING)
    assert led.fence(key, c.fencing_token, EffectState.SUCCEEDED, result="peer-result")
    return c.fencing_token


def test_adopt_guard_verifies_a_same_action_peer_across_rotation(tmp_path: Path) -> None:
    # A NEW-signed taker lost its lease mid-flight; the peer already settled the SAME action
    # under OLD. Adopting must NOT false-diverge — verify recomputes under the peer's secret.
    keyring = {secret_id(OLD): OLD, secret_id(NEW): NEW}
    new_fp = fingerprint(DECL, ARGS, secret=NEW)
    with SqliteLedger(tmp_path / "adopt-win.db", multi_worker=True) as led:
        token = _succeeded_old_row(led, "k")  # fence with this token is now rejected (terminal)
        out = _record_success_fenced(
            led, "k", token, "my-result", new_fp, _leased_verify(ARGS, keyring, NEW)
        )
        assert out == "peer-result"  # adopted the peer's recorded result, no DivergentRetry
    # Bug shape (no window): the byte-compare falsely diverges on the same action.
    with SqliteLedger(tmp_path / "adopt-nb.db", multi_worker=True) as led:
        token = _succeeded_old_row(led, "k")
        with pytest.raises(DivergentRetry):
            _record_success_fenced(led, "k", token, "my-result", new_fp)  # verify=None


def test_adopt_guard_still_refuses_a_divergent_peer_across_rotation(tmp_path: Path) -> None:
    keyring = {secret_id(OLD): OLD, secret_id(NEW): NEW}
    with SqliteLedger(tmp_path / "adopt-div.db", multi_worker=True) as led:
        token = _succeeded_old_row(led, "k")  # peer settled action {"to": "a@x.com"}
        diverging = _leased_verify({"to": "b@x.com"}, keyring, NEW)  # taker ran a different action
        with pytest.raises(DivergentRetry):
            _record_success_fenced(
                led,
                "k",
                token,
                "my-result",
                fingerprint(DECL, {"to": "b@x.com"}, secret=NEW),
                diverging,
            )


def test_no_rotation_window_is_byte_identical_behavior(tmp_path: Path) -> None:
    # With no verify_secrets, the verifier is None → plain byte-compare, exactly as before.
    db = tmp_path / "l.db"
    calls: list[str] = []
    _settle_under(db, NEW, "a@x.com", calls)
    out = _settle_under(db, NEW, "a@x.com", calls)  # same secret, replay
    assert out == "sent:a@x.com" and calls == ["a@x.com"]
    # A different secret with NO window → the old byte-compare divergence (no keyring lookup).
    with pytest.raises(DivergentRetry):
        _settle_under(db, OLD, "a@x.com", calls)
