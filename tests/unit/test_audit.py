# SPDX-License-Identifier: Apache-2.0
"""The Act V audit trail — Gate E: every settled effect is queryable and exportable,
the export includes P5-3 provenance, and the surface is provably read-only (the
audit connection is ``mode=ro``, so writes are refused by the database itself)."""

import asyncio
import csv
import io
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from sakrit import EffectDecl, Sakrit, SqliteLedger
from sakrit.audit import AuditQuery, audit_asgi
from sakrit.cli import main
from sakrit.core import ArgClass
from sakrit.core.errors import SakritError

DECL_MAIL = EffectDecl("notify.send", {"to": ArgClass.IDENTITY})
DECL_PAY = EffectDecl("payments.charge", {"amount": ArgClass.IDENTITY})
SECRET = b"deployment-secret"


@pytest.fixture()
def ledger_db(tmp_path: Path) -> Path:
    """A real history: settle effects through the actual engine, then audit the file."""
    db = tmp_path / "ledger.db"
    ledger = SqliteLedger(db)
    sk = Sakrit(ledger, secret=SECRET)
    sk.guard(DECL_MAIL, lambda to: f"mail:{to}", kwargs={"to": "a@x.com"}, key="mail-1")
    sk.guard(DECL_MAIL, lambda to: f"mail:{to}", kwargs={"to": "b@x.com"}, key="mail-2")
    sk.guard(DECL_PAY, lambda amount: {"charged": amount}, kwargs={"amount": 100}, key="pay-1")
    ledger.close()
    return db


def test_every_settled_effect_is_queryable_with_provenance(ledger_db: Path) -> None:
    with AuditQuery(ledger_db) as q:
        rows = list(q.rows())
    assert len(rows) == 3
    assert {r.state for r in rows} == {"SUCCEEDED"}
    assert {r.tool for r in rows} == {"notify.send", "payments.charge"}
    for r in rows:
        # P5-3 provenance rides along on every row.
        assert r.key_version and r.fingerprint_version and r.secret_id
        assert r.created_at and r.settled_at


def test_filters_combine(ledger_db: Path) -> None:
    with AuditQuery(ledger_db) as q:
        assert len(list(q.rows(tool="notify.send"))) == 2
        assert len(list(q.rows(tool="notify.send", limit=1))) == 1
        assert len(list(q.rows(scope="global", state="SUCCEEDED"))) == 3
        assert len(list(q.rows(state="AMBIGUOUS"))) == 0
        assert len(list(q.rows(until="1970-01-01T00:00:00+00:00"))) == 0
        assert len(list(q.rows(since="1970-01-01T00:00:00+00:00"))) == 3


def test_unknown_state_refuses_instead_of_empty(ledger_db: Path) -> None:
    with AuditQuery(ledger_db) as q, pytest.raises(SakritError, match="unknown state"):
        list(q.rows(state="SUCEEDED"))  # the typo must be loud, not an empty "all clear"


def test_naive_datetime_filter_refuses(ledger_db: Path) -> None:
    from datetime import datetime

    with AuditQuery(ledger_db) as q, pytest.raises(SakritError, match="timezone-aware"):
        list(q.rows(since=datetime(2026, 1, 1)))


def test_missing_ledger_refuses(tmp_path: Path) -> None:
    from sakrit.audit import AuditLedgerNotFound

    with pytest.raises(AuditLedgerNotFound, match="no ledger"):  # typed (A-9)
        AuditQuery(tmp_path / "nope.db")


def test_string_time_filter_that_is_not_iso_refuses(ledger_db: Path) -> None:
    # A-5: a non-ISO string must fail closed, not silently match nothing ("all clear").
    with AuditQuery(ledger_db) as q, pytest.raises(SakritError, match="not ISO-8601"):
        list(q.rows(since="yesterday"))


def test_naive_string_time_filter_refuses(ledger_db: Path) -> None:
    # A-5: a tz-less ISO string is refused too (would shift the window).
    with AuditQuery(ledger_db) as q, pytest.raises(SakritError, match="timezone-aware"):
        list(q.rows(since="2026-01-01T00:00:00"))


def test_z_suffix_string_filter_is_accepted_and_normalized(ledger_db: Path) -> None:
    # A-5: a ...Z spelling compares identically to the stored +00:00 form.
    with AuditQuery(ledger_db) as q:
        assert len(list(q.rows(since="1970-01-01T00:00:00Z"))) == 3


def test_missing_meta_table_reads_as_legacy(ledger_db: Path) -> None:
    # B-4: a "no such table: sakrit_meta" means pre-P5-3 legacy → understood, not refused.
    q = AuditQuery.__new__(AuditQuery)

    class _Conn:
        def execute(self, *a: object) -> object:
            raise sqlite3.OperationalError("no such table: sakrit_meta")

    q._conn = _Conn()  # type: ignore[assignment]
    q._enforce_schema_version(ledger_db)  # returns without raising


def test_locked_db_during_schema_check_is_not_swallowed(ledger_db: Path) -> None:
    # B-4: a transient OperationalError ("database is locked") must NOT be misread as legacy —
    # narrowing the catch to no-such-table means it propagates instead of skipping the guard.
    q = AuditQuery.__new__(AuditQuery)

    class _Conn:
        def execute(self, *a: object) -> object:
            raise sqlite3.OperationalError("database is locked")

    q._conn = _Conn()  # type: ignore[assignment]
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        q._enforce_schema_version(ledger_db)


def test_newer_schema_version_is_refused(ledger_db: Path) -> None:
    # A-4: the read-only compliance surface must refuse a future format, like the writer does.
    con = sqlite3.connect(ledger_db)
    con.execute("UPDATE sakrit_meta SET v = '999' WHERE k = 'schema_version'")
    con.commit()
    con.close()
    with pytest.raises(SakritError, match="newer than this Sakrit"):
        AuditQuery(ledger_db)


def test_audit_works_against_a_live_ledger(tmp_path: Path) -> None:
    """The real ops scenario: query while the agent is running. The audit reader is
    not a worker — it must not take the single-worker flock — and WAL admits it
    concurrently with the live writer."""
    db = tmp_path / "ledger.db"
    ledger = SqliteLedger(db)
    sk = Sakrit(ledger, secret=SECRET)
    try:
        sk.guard(DECL_MAIL, lambda to: "sent", kwargs={"to": "a@x.com"}, key="live-1")
        with AuditQuery(db) as q:  # the worker's flock is held right now
            rows = list(q.rows())
        assert len(rows) == 1 and rows[0].state == "SUCCEEDED"
        # And the worker keeps working after the audit pass.
        sk.guard(DECL_MAIL, lambda to: "sent", kwargs={"to": "b@x.com"}, key="live-2")
    finally:
        ledger.close()


def test_export_json_round_trips_results(ledger_db: Path) -> None:
    buf = io.StringIO()
    with AuditQuery(ledger_db) as q:
        count = q.export(buf, fmt="json")
    assert count == 3
    exported = json.loads(buf.getvalue())
    assert len(exported) == 3
    by_tool = {e["tool"]: e for e in exported}
    # The stored JSON result comes back as its value, not a double-encoded string.
    assert by_tool["payments.charge"]["result"] == {"charged": 100}
    assert by_tool["payments.charge"]["secret_id"]  # provenance in the export


def test_export_csv_has_header_and_rows(ledger_db: Path) -> None:
    buf = io.StringIO()
    with AuditQuery(ledger_db) as q:
        count = q.export(buf, fmt="csv")
    assert count == 3
    reader = list(csv.reader(io.StringIO(buf.getvalue())))
    assert reader[0][:4] == ["key", "scope", "tool", "state"]
    assert len(reader) == 4  # header + 3 rows
    with AuditQuery(ledger_db) as q, pytest.raises(SakritError, match="unknown export format"):
        q.export(io.StringIO(), fmt="xml")


def test_surface_is_readonly_by_construction(ledger_db: Path) -> None:
    q = AuditQuery(ledger_db)
    try:
        # The database itself refuses writes through the audit connection.
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            q._conn.execute("UPDATE effects SET state = 'FAILED'")
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            q._conn.execute("DELETE FROM effects")
    finally:
        q.close()
    # Belt: after a full query+export pass, the ledger is bit-identical in content.
    before = (
        sqlite3.connect(ledger_db).execute("SELECT key, state FROM effects ORDER BY key").fetchall()
    )
    buf = io.StringIO()
    with AuditQuery(ledger_db) as q2:
        list(q2.rows())
        q2.export(buf, fmt="json")
        q2.export(buf, fmt="csv")
    after = (
        sqlite3.connect(ledger_db).execute("SELECT key, state FROM effects ORDER BY key").fetchall()
    )
    assert before == after


# --- the ASGI handler ----------------------------------------------------------------
def _http_get(app: Any, query: str = "", method: str = "GET") -> tuple[int, Any]:
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    async def receive() -> dict[str, Any]:  # pragma: no cover — GET has no body
        return {"type": "http.request"}

    scope = {"type": "http", "method": method, "query_string": query.encode()}
    asyncio.run(app(scope, receive, send))
    status: int = sent[0]["status"]
    body = json.loads(sent[1]["body"].decode())
    return status, body


def test_asgi_get_returns_rows(ledger_db: Path) -> None:
    app = audit_asgi(ledger_db)
    status, body = _http_get(app, "tool=notify.send")
    assert status == 200
    assert isinstance(body, list) and len(body) == 2
    status_all, body_all = _http_get(app, "limit=1")
    assert status_all == 200 and len(body_all) == 1


def test_asgi_refusals_are_loud(ledger_db: Path, tmp_path: Path) -> None:
    app = audit_asgi(ledger_db)
    status, body = _http_get(app, method="POST")
    assert status == 405
    status, body = _http_get(app, "state=SUCEEDED")
    assert status == 400 and "unknown state" in str(body)
    status, body = _http_get(app, "limit=ten")
    assert status == 400
    status, body = _http_get(audit_asgi(tmp_path / "nope.db"))
    assert status == 404


# --- CLI -----------------------------------------------------------------------------
def test_cli_audit_exports(
    ledger_db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "audit.json"
    assert main(["audit", str(ledger_db), "-o", str(out)]) == 0
    assert len(json.loads(out.read_text())) == 3

    assert main(["audit", str(ledger_db), "--format", "csv", "--tool", "payments.charge"]) == 0
    captured = capsys.readouterr()
    assert "payments.charge" in captured.out

    assert main(["audit", str(tmp_path / "nope.db")]) == 1  # missing ledger is loud, rc != 0
