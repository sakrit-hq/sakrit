# SPDX-License-Identifier: Apache-2.0
"""The Act V audit trail — queryable, exportable settled-effect history (roadmap #18).

Read-only **by construction**, not by convention (Q7 — no raw CRUD): the audit
surface opens its own SQLite connection with ``mode=ro``, so a write attempted
through it — bug or misuse — is refused by the database itself, and the workers'
ledger connections are never touched (WAL admits concurrent readers).

Every row carries the P5-3 provenance columns (``key_version``,
``fingerprint_version``, ``secret_id``) so an export answers not just *what
settled* but *under which identity scheme* — the precondition for auditing across
key-scheme migrations and secret rotations.

Filters fail closed: an unknown ``state`` raises instead of returning an empty
(and thus "everything is fine"-looking) result set; a missing ledger file raises
instead of reading as an empty history.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from collections.abc import Iterator
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import IO, Any

from sakrit.core.errors import SakritError
from sakrit.core.ledger import _SCHEMA_VERSION, EffectState

__all__ = ["AuditLedgerNotFound", "AuditQuery", "AuditRow", "audit_asgi"]

_VALID_STATES = frozenset(s.value for s in EffectState)


class AuditLedgerNotFound(SakritError):
    """No ledger file at the given path (A-9: a *typed* not-found, so the ASGI 404 does not
    depend on substring-matching an error message that could drift)."""


@dataclass(frozen=True)
class AuditRow:
    """One effect's audit record — the ledger row, provenance included.

    ``result`` and ``error`` are the stored TEXT verbatim (``result`` is the
    ledger's JSON document); timestamps are the stored UTC ISO-8601 strings.
    """

    key: str
    scope: str
    tool: str
    state: str
    fingerprint: str
    result: str | None
    error: str | None
    created_at: str
    settled_at: str | None
    resolved_by: str | None
    provider_dedup: bool
    reconcilable: bool
    key_version: str | None
    fingerprint_version: str | None
    secret_id: str | None

    def as_dict(self) -> dict[str, object]:
        """The row as a plain dict, with ``result`` parsed back to its JSON value
        (it was stored via ``json.dumps``; a NULL stays ``None``)."""
        d: dict[str, object] = asdict(self)
        if self.result is not None:
            d["result"] = json.loads(self.result)
        return d


_COLUMNS = ", ".join(f.name for f in fields(AuditRow))


def _iso(ts: str | datetime) -> str:
    """Normalize a time filter to the stored comparison format (UTC ISO-8601).

    Both a ``datetime`` and a *string* filter are validated and fail closed (A-5): a naive
    (tz-less) value is refused — the stored timestamps are timezone-aware UTC, and comparing a
    naive local time against them would silently shift the window — and a string that is not
    ISO-8601 (``"yesterday"``) is refused rather than silently matching nothing (an empty,
    "all clear"-looking result). The parsed value is re-emitted via ``.isoformat()`` so a
    ``…Z`` and a ``…+00:00`` spelling of the same instant compare identically against the
    stored ``+00:00`` form."""
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))  # 3.10-safe Z handling
        except ValueError as exc:
            raise SakritError(
                f"audit time filter {ts!r} is not ISO-8601 ({exc}); pass e.g. "
                "'2026-07-27T00:00:00+00:00' or a tz-aware datetime — refused rather than "
                "silently matching nothing"
            ) from exc
    if ts.tzinfo is None:
        raise SakritError(
            "audit time filters need a timezone-aware value (the ledger stores UTC); a naive "
            "value would silently shift the window by your UTC offset"
        )
    return ts.isoformat()


class AuditQuery:
    """Read-only queries over a ledger file. Use as a context manager, or ``close()``."""

    def __init__(self, path: str | Path) -> None:
        p = Path(path)
        if not p.exists():
            # mode=ro would raise its own (cryptic) error; refuse loudly first so a
            # typo'd path can never read as "empty history, all clear".
            raise AuditLedgerNotFound(f"no ledger at {p} — nothing to audit (is the path right?)")
        # mode=ro makes read-only a *database-enforced* property of this surface.
        self._conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        self._conn.row_factory = sqlite3.Row
        self._enforce_schema_version(p)

    def _enforce_schema_version(self, p: Path) -> None:
        """Refuse a ledger written by a newer Sakrit than this build understands (A-4).

        The write path (``SqliteLedger``) already refuses a newer ``sakrit_meta.schema_version``;
        the read-only *compliance* surface must too, or a future on-disk format that changed
        column semantics would be silently misread and misreported — the exact silent-misread
        P5-3's marker exists to prevent. A pre-P5-3 ledger with no marker is legacy, not newer,
        so it reads fine."""
        try:
            row = self._conn.execute(
                "SELECT v FROM sakrit_meta WHERE k = 'schema_version'"
            ).fetchone()
        except sqlite3.OperationalError:
            return  # no sakrit_meta table → pre-P5-3 legacy ledger, understood as-is
        if row is None:
            return
        on_disk = int(row[0])
        if on_disk > _SCHEMA_VERSION:
            self._conn.close()
            raise SakritError(
                f"ledger at {p} has schema_version {on_disk}, newer than this Sakrit "
                f"understands ({_SCHEMA_VERSION}). Upgrade Sakrit before auditing it — a "
                "newer format may have changed what these columns mean."
            )

    def rows(
        self,
        *,
        scope: str | None = None,
        tool: str | None = None,
        state: str | None = None,
        since: str | datetime | None = None,
        until: str | datetime | None = None,
        limit: int | None = None,
    ) -> Iterator[AuditRow]:
        """Iterate effects matching the filters, oldest first (by ``created_at``).

        ``since``/``until`` bound ``created_at`` (inclusive/exclusive). All filters
        combine with AND; no filter → the full history, terminal and in-flight
        states alike (an operator auditing an incident wants ``EXECUTING`` and
        ``AMBIGUOUS`` rows *especially*).
        """
        clauses: list[str] = []
        params: list[object] = []
        if state is not None:
            if state not in _VALID_STATES:
                raise SakritError(
                    f"unknown state {state!r} — valid states: {sorted(_VALID_STATES)}. "
                    "(Refused rather than returning a misleading empty result.)"
                )
            clauses.append("state = ?")
            params.append(state)
        for column, value in (("scope", scope), ("tool", tool)):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        if since is not None:
            clauses.append("created_at >= ?")
            params.append(_iso(since))
        if until is not None:
            clauses.append("created_at < ?")
            params.append(_iso(until))
        sql = f"SELECT {_COLUMNS} FROM effects"  # the column list is static, values are bound
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at, key"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        for raw in self._conn.execute(sql, params):
            yield AuditRow(
                key=raw["key"],
                scope=raw["scope"],
                tool=raw["tool"],
                state=raw["state"],
                fingerprint=raw["fingerprint"],
                result=raw["result"],
                error=raw["error"],
                created_at=raw["created_at"],
                settled_at=raw["settled_at"],
                resolved_by=raw["resolved_by"],
                provider_dedup=bool(raw["provider_dedup"]),
                reconcilable=bool(raw["reconcilable"]),
                key_version=raw["key_version"],
                fingerprint_version=raw["fingerprint_version"],
                secret_id=raw["secret_id"],
            )

    def export(self, dest: IO[str], *, fmt: str = "json", **filters: Any) -> int:
        """Stream matching rows to ``dest`` as ``json`` (an array of row objects,
        ``result`` parsed) or ``csv`` (header + stored-text cells). Returns the row
        count. Streaming — the history is never materialized in memory.

        **Two stated decisions (A-9):** (1) CSV cells are written *verbatim* — a stored value
        beginning ``=``/``+``/``-``/``@`` is not neutralized against spreadsheet formula
        injection. An audit export is a fidelity artifact: it must reproduce exactly what
        settled, so the caller sanitizes on ingestion into a spreadsheet, not here. (2) A
        mid-stream error (e.g. a disk-full) leaves a *partial* file — for JSON, an unterminated
        array. The error is loud (it propagates), but the artifact is malformed; export to a
        temp path and rename on success if you need atomicity."""
        if fmt == "json":
            count = 0
            dest.write("[")
            for row in self.rows(**filters):
                if count:
                    dest.write(",\n ")
                json.dump(row.as_dict(), dest)
                count += 1
            dest.write("]\n")
            return count
        if fmt == "csv":
            writer = csv.writer(dest)
            writer.writerow(f.name for f in fields(AuditRow))
            count = 0
            for row in self.rows(**filters):
                writer.writerow(getattr(row, f.name) for f in fields(AuditRow))
                count += 1
            return count
        raise SakritError(f"unknown export format {fmt!r} — use 'json' or 'csv'")

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> AuditQuery:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def audit_asgi(path: str | Path) -> Any:
    """A minimal, dependency-free ASGI app serving
    ``GET /…?scope=&tool=&state=&since=&until=&limit=``.

    Returns the matching audit rows as a JSON array. Read-only end to end (each
    request opens its own ``mode=ro`` connection). **Carries no authentication** —
    effect history can contain business data, so mount it behind your own auth
    layer; it is an ops surface, not a public API. A full hosted service is Act V
    scope and deliberately not this.

    Refusals are loud and typed: non-GET is 405; a bad filter (unknown state, non-ISO or
    naive time) is 400 with the reason; a missing ledger is 404 (via the typed
    :class:`AuditLedgerNotFound`, not a message-substring match, A-9). **The SQLite calls are
    synchronous** — this handler blocks the event loop while querying: fine for a low-traffic
    ops surface, but do not mount it on a hot path without offloading to a threadpool.
    """
    from urllib.parse import parse_qs  # stdlib; imported here to keep module import lean

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":  # lifespan etc. — not ours
            raise RuntimeError(f"audit_asgi only speaks http, got {scope['type']!r}")
        if scope["method"] != "GET":
            await _respond(send, 405, {"error": "audit is read-only: GET only"})
            return
        qs = parse_qs(scope.get("query_string", b"").decode("utf-8"))
        filters: dict[str, Any] = {
            name: qs[name][0] for name in ("scope", "tool", "state", "since", "until") if name in qs
        }
        if "limit" in qs:
            try:
                filters["limit"] = int(qs["limit"][0])
            except ValueError:
                await _respond(
                    send, 400, {"error": f"limit must be an int, got {qs['limit'][0]!r}"}
                )
                return
        try:
            with AuditQuery(path) as query:
                rows = [row.as_dict() for row in query.rows(**filters)]
        except AuditLedgerNotFound as exc:
            await _respond(send, 404, {"error": str(exc)})  # typed, not substring-matched (A-9)
            return
        except SakritError as exc:
            await _respond(send, 400, {"error": str(exc)})
            return
        await _respond(send, 200, rows)

    return app


async def _respond(send: Any, status: int, body: object) -> None:
    payload = json.dumps(body).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": payload})
