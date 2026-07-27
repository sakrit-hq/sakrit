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
from sakrit.core.ledger import EffectState

__all__ = ["AuditQuery", "AuditRow", "audit_asgi"]

_VALID_STATES = frozenset(s.value for s in EffectState)


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

    A naive ``datetime`` is refused: the stored timestamps are timezone-aware UTC,
    and comparing a naive local time against them would silently shift the window.
    """
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            raise SakritError(
                "audit time filters need timezone-aware datetimes (the ledger stores UTC); "
                "a naive datetime would silently shift the window by your UTC offset"
            )
        return ts.isoformat()
    return ts


class AuditQuery:
    """Read-only queries over a ledger file. Use as a context manager, or ``close()``."""

    def __init__(self, path: str | Path) -> None:
        p = Path(path)
        if not p.exists():
            # mode=ro would raise its own (cryptic) error; refuse loudly first so a
            # typo'd path can never read as "empty history, all clear".
            raise SakritError(f"no ledger at {p} — nothing to audit (is the path right?)")
        # mode=ro makes read-only a *database-enforced* property of this surface.
        self._conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        self._conn.row_factory = sqlite3.Row

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
        count. Streaming — the history is never materialized in memory."""
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

    Refusals are loud: non-GET → 405; a bad filter (unknown state, naive
    datetime) → 400 with the reason; a missing ledger → 404.
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
        except SakritError as exc:
            status = 404 if "no ledger" in str(exc) else 400
            await _respond(send, status, {"error": str(exc)})
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
