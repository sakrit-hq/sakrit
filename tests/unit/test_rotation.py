# SPDX-License-Identifier: Apache-2.0
"""The software dual-secret rotation window: rotate the HMAC secret without a fleet-wide
DivergentRetry storm. Sign new writes with the new secret; keep old secrets in a verify
keyring (keyed by the P5-3 secret_id) so an in-flight row signed by the old secret still
replays during the drain. A genuinely different action still diverges; a secret rotated
clean out of the window is refused loudly, never silently mis-compared."""

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
