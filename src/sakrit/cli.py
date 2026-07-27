# SPDX-License-Identifier: Apache-2.0
"""The ``sakrit`` console entry point.

- ``sakrit doctor [PATH …] [--check]`` — the static unguarded-consequential-call
  net (see :mod:`sakrit.doctor`). Plain mode prints findings and exits 0 (a
  report); ``--check`` exits 1 on any finding (a CI gate).
- ``sakrit audit LEDGER [filters] [--format json|csv] [-o FILE]`` — export the
  settled-effect history, provenance included (see :mod:`sakrit.audit`).
  Read-only by construction (``mode=ro`` connection).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from sakrit.__about__ import __version__
from sakrit.doctor import scan_paths


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sakrit", description="Sakrit — exactly-once effects for AI agents."
    )
    parser.add_argument("--version", action="version", version=f"sakrit {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser(
        "doctor",
        help="statically scan for consequential calls not under a Sakrit guard",
        description=(
            "Scan Python source (never imported or executed) for consequential-looking "
            "calls — HTTP mutations, SMTP sends, Stripe mutations, boto3 mutating verbs, "
            "write-SQL execute — that are not lexically under a Sakrit guard. A heuristic "
            "net for 'forgot to wrap', not a runtime guarantee: review each finding and "
            "either wrap it or annotate it `# sakrit: safe` / @sakrit.safe."
        ),
    )
    doctor.add_argument("paths", nargs="*", type=Path, help="files or directories (default: .)")
    doctor.add_argument("--check", action="store_true", help="exit nonzero on any finding (for CI)")

    audit = sub.add_parser(
        "audit",
        help="query/export the settled-effect history from a ledger (read-only)",
        description=(
            "Query a Sakrit ledger's effect history — every row with its P5-3 provenance "
            "(key/fingerprint scheme versions, secret id). Read-only by construction: the "
            "ledger is opened mode=ro, so this command cannot mutate it."
        ),
    )
    audit.add_argument("ledger", type=Path, help="path to the ledger database file")
    audit.add_argument("--scope", help="filter: exact scope")
    audit.add_argument("--tool", help="filter: exact tool name")
    audit.add_argument("--state", help="filter: effect state (e.g. SUCCEEDED, AMBIGUOUS)")
    audit.add_argument("--since", help="filter: created_at >= this UTC ISO-8601 timestamp")
    audit.add_argument("--until", help="filter: created_at < this UTC ISO-8601 timestamp")
    audit.add_argument("--limit", type=int, help="cap the number of rows")
    audit.add_argument("--format", choices=("json", "csv"), default="json", dest="fmt")
    audit.add_argument("-o", "--output", type=Path, help="write to a file (default: stdout)")

    args = parser.parse_args(argv)
    if args.command == "doctor":
        return _run_doctor(args.paths or [Path()], check=args.check)
    if args.command == "audit":
        return _run_audit(args)
    parser.error(f"unknown command {args.command!r}")  # unreachable: subparsers validate

    return 2


def _run_doctor(paths: list[Path], *, check: bool) -> int:
    findings, scanned = scan_paths(paths)
    for finding in findings:
        print(finding.render())
    noun = "file" if scanned == 1 else "files"
    if findings:
        print(f"sakrit doctor: {len(findings)} finding(s) in {scanned} {noun} scanned.")
        return 1 if check else 0
    print(f"sakrit doctor: no findings in {scanned} {noun} scanned.")
    return 0


def _run_audit(args: argparse.Namespace) -> int:
    from sakrit.audit import AuditQuery  # local: keep `sakrit doctor` import-light
    from sakrit.core.errors import SakritError

    filters = {
        name: getattr(args, name)
        for name in ("scope", "tool", "state", "since", "until", "limit")
        if getattr(args, name) is not None
    }
    try:
        with AuditQuery(args.ledger) as query:
            if args.output is not None:
                with open(args.output, "w", encoding="utf-8", newline="") as dest:
                    count = query.export(dest, fmt=args.fmt, **filters)
            else:
                count = query.export(sys.stdout, fmt=args.fmt, **filters)
    except SakritError as exc:
        print(f"sakrit audit: {exc}", file=sys.stderr)
        return 1
    print(f"sakrit audit: {count} row(s) exported.", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover — exercised via the console script
    sys.exit(main())
