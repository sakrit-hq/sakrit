# SPDX-License-Identifier: Apache-2.0
"""P5-3: ledger row provenance (key_version / fingerprint_version / secret_id) and the
schema_version marker — stamped cheaply now so a scheme change or secret rotation is
*detectable* later instead of a fleet-wide DivergentRetry storm."""

from pathlib import Path

import pytest

from sakrit import Sakrit, SqliteLedger
from sakrit.core import ArgClass, EffectDecl, SakritError, SchemeMismatch
from sakrit.core.fingerprint import FINGERPRINT_VERSION, secret_id
from sakrit.core.keys import KEY_VERSION

SECRET = b"deployment-secret"


def _row_provenance(led: SqliteLedger, key: str) -> tuple[object, object, object]:
    row = led.conn.execute(
        "SELECT key_version, fingerprint_version, secret_id FROM effects WHERE key = ?",
        (key,),
    ).fetchone()
    return (row[0], row[1], row[2])


# --- the columns are stamped at claim -------------------------------------
def test_claim_stamps_row_provenance() -> None:
    with SqliteLedger(":memory:") as led:
        led.claim("k1", "scope", "tool", "fp", secret_id="sid-abc")
        assert _row_provenance(led, "k1") == (KEY_VERSION, FINGERPRINT_VERSION, "sid-abc")


def test_claim_leased_stamps_row_provenance(tmp_path: Path) -> None:
    with SqliteLedger(tmp_path / "l.sqlite", multi_worker=True) as led:
        led.claim_leased(
            "k1", "scope", "tool", "fp", owner=led.owner, lease_seconds=30, secret_id="sid-xyz"
        )
        assert _row_provenance(led, "k1") == (KEY_VERSION, FINGERPRINT_VERSION, "sid-xyz")


def test_missing_secret_id_stamps_null_but_still_versions() -> None:
    # secret_id is optional (the keyring is Act IV); the scheme versions always stamp.
    with SqliteLedger(":memory:") as led:
        led.claim("k1", "scope", "tool", "fp")
        assert _row_provenance(led, "k1") == (KEY_VERSION, FINGERPRINT_VERSION, None)


# --- the schema_version marker --------------------------------------------
def test_schema_version_is_written() -> None:
    with SqliteLedger(":memory:") as led:
        row = led.conn.execute("SELECT v FROM sakrit_meta WHERE k = 'schema_version'").fetchone()
        assert row is not None and int(row[0]) >= 1


def test_a_newer_schema_version_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "l.sqlite"
    with SqliteLedger(path) as led:
        led.conn.execute("UPDATE sakrit_meta SET v = '999' WHERE k = 'schema_version'")
    with pytest.raises(SakritError, match="newer than this Sakrit understands"):
        SqliteLedger(path)


# --- scheme-mismatch detect-and-refuse ------------------------------------
def test_divergent_key_scheme_is_refused_not_silently_compared() -> None:
    with SqliteLedger(":memory:") as led:
        led.claim("k1", "scope", "tool", "fp", secret_id="sid")
        # Simulate a row written under an older key scheme.
        led.conn.execute("UPDATE effects SET key_version = 'v2' WHERE key = 'k1'")
        with pytest.raises(SchemeMismatch, match="keyed under scheme"):
            led.claim("k1", "scope", "tool", "fp", secret_id="sid")


def test_divergent_fingerprint_scheme_is_refused() -> None:
    with SqliteLedger(":memory:") as led:
        led.claim("k1", "scope", "tool", "fp", secret_id="sid")
        led.conn.execute("UPDATE effects SET fingerprint_version = 'old-scheme' WHERE key = 'k1'")
        with pytest.raises(SchemeMismatch, match="fingerprinted under scheme"):
            led.claim("k1", "scope", "tool", "fp", secret_id="sid")


def test_legacy_null_provenance_is_not_a_false_mismatch() -> None:
    # A pre-P5-3 row carries NULL versions — unknown provenance, never a manufactured mismatch.
    with SqliteLedger(":memory:") as led:
        led.claim("k1", "scope", "tool", "fp")
        led.conn.execute(
            "UPDATE effects SET key_version = NULL, fingerprint_version = NULL WHERE key = 'k1'"
        )
        # SUCCEEDED so the next claim REPLAYs (exercises the existing-row path) without raising.
        led.mark_executing("k1")
        led.record_success("k1", {"ok": True})
        claim = led.claim("k1", "scope", "tool", "fp")
        assert claim.result == {"ok": True}


def test_leased_takeover_refuses_across_schemes(tmp_path: Path) -> None:
    with SqliteLedger(tmp_path / "l.sqlite", multi_worker=True) as led:
        led.claim_leased("k1", "scope", "tool", "fp", owner=led.owner, lease_seconds=30)
        led.conn.execute("UPDATE effects SET key_version = 'v2' WHERE key = 'k1'")
        with pytest.raises(SchemeMismatch):
            led.claim_leased("k1", "scope", "tool", "fp", owner=led.owner, lease_seconds=30)


def test_pre_p5_3_table_is_migrated_in_place(tmp_path: Path) -> None:
    # A ledger file created before P5-3 has an `effects` table without the provenance columns
    # and no `sakrit_meta`. Opening it must ADD the columns (CREATE TABLE IF NOT EXISTS would
    # otherwise leave the old table untouched → "no such column"), and its NULL provenance must
    # not read as a scheme mismatch.
    import sqlite3

    path = tmp_path / "legacy.sqlite"
    c = sqlite3.connect(path)
    c.execute(
        "CREATE TABLE effects (key TEXT PRIMARY KEY, scope TEXT, tool TEXT, fingerprint TEXT, "
        "state TEXT, provider_dedup INTEGER, provider_ttl_s REAL, reconcilable INTEGER, "
        "result TEXT, result_ref TEXT, error TEXT, created_at TEXT, settled_at TEXT, "
        "fencing_token INTEGER, lease_owner TEXT, lease_expires REAL, resolved_by TEXT)"
    )
    c.execute(
        "INSERT INTO effects (key, scope, tool, fingerprint, state, created_at) "
        "VALUES ('k', 's', 't', 'fp', 'SUCCEEDED', '2026-01-01')"
    )
    c.commit()
    c.close()

    with SqliteLedger(path) as led:
        assert _row_provenance(led, "k") == (None, None, None)  # legacy row: unknown provenance
        claim = led.claim("k", "s", "t", "fp")  # REPLAY, not a false SchemeMismatch
        assert claim.kind.value == "replay"
        # a *new* claim on this migrated ledger stamps current provenance
        led.claim("k2", "s", "t", "fp2", secret_id="sid")
        assert _row_provenance(led, "k2") == (KEY_VERSION, FINGERPRINT_VERSION, "sid")


# --- secret_id derivation -------------------------------------------------
def test_secret_id_is_stable_and_rotation_detectable() -> None:
    assert secret_id(SECRET) == secret_id(SECRET)  # stable
    assert secret_id(SECRET) != secret_id(b"rotated-secret")  # rotation changes it
    # non-reversible short digest, never the raw secret
    sid = secret_id(SECRET)
    assert len(sid) == 16 and SECRET.decode() not in sid


# --- the engine stamps its secret's id end-to-end -------------------------
def test_engine_stamps_secret_id_on_the_row(tmp_path: Path) -> None:
    led = SqliteLedger(tmp_path / "l.sqlite")
    sk = Sakrit(led, secret=SECRET)
    decl = EffectDecl(tool="pay", classes={"amount": ArgClass.IDENTITY})

    def pay(amount: int) -> str:
        return "ok"

    sk.guard(decl, pay, args=(100,), key="pay-1", scope="run-1")
    # The row was stamped with the *engine's* secret id, not a placeholder.
    stamped = led.conn.execute(
        "SELECT secret_id FROM effects WHERE secret_id IS NOT NULL"
    ).fetchone()
    assert stamped is not None and stamped[0] == secret_id(SECRET)
    led.close()
