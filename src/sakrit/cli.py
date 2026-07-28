# SPDX-License-Identifier: Apache-2.0
"""The ``sakrit`` console entry point.

- ``sakrit doctor [PATH …] [--check] [--format text|json|sarif] [-o FILE]`` — the
  static unguarded-consequential-call net (see :mod:`sakrit.doctor`). Report mode
  prints findings and exits 0; ``--check`` exits 1 on any finding (a CI gate);
  exit 2 is a usage error. ``--format`` selects text (default), the
  ``sakrit.doctor/1`` JSON envelope, or SARIF 2.1.0 (for GitHub code-scanning PR
  annotations). ``--check`` affects only the exit code, never the output. The exit
  codes and the JSON/SARIF shapes are a frozen contract — see
  docs/dev-notes/stage2-cloud/phase0/doctor-output-contract.md.
- ``sakrit audit LEDGER [filters] [--format json|csv] [-o FILE]`` — export the
  settled-effect history, provenance included (see :mod:`sakrit.audit`).
  Read-only by construction (``mode=ro`` connection).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from sakrit.__about__ import __version__
from sakrit.doctor import Finding, findings_to_json, findings_to_sarif, scan_paths


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
        epilog=(
            "exit codes:\n"
            "  0   report mode (findings printed), or --check with no findings\n"
            "  1   --check found at least one finding (incl. an unreadable file, SAKRIT000)\n"
            "  2   usage error, or --output (-o) write failed"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    doctor.add_argument("paths", nargs="*", type=Path, help="files or directories (default: .)")
    doctor.add_argument("--check", action="store_true", help="exit nonzero on any finding (for CI)")
    doctor.add_argument(
        "--format",
        choices=("text", "json", "sarif"),
        default="text",
        dest="fmt",
        help="output format: text (default), json (sakrit.doctor/1), or sarif (2.1.0)",
    )
    doctor.add_argument("-o", "--output", type=Path, help="write to a file (default: stdout)")

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
        return _run_doctor(
            args.paths or [Path()], check=args.check, fmt=args.fmt, output=args.output
        )
    if args.command == "audit":
        return _run_audit(args)
    parser.error(f"unknown command {args.command!r}")  # unreachable: subparsers validate

    return 2


def _run_doctor(
    paths: list[Path], *, check: bool, fmt: str = "text", output: Path | None = None
) -> int:
    findings, scanned = scan_paths(paths)
    body = _render_doctor(findings, scanned, fmt=fmt)
    if output is not None:
        try:
            output.write_text(body + "\n", encoding="utf-8")
        except OSError as exc:
            # A failed -o write must not masquerade as "1 = findings found" (a CI consumer would
            # misread it): report the write error loudly and exit 2 (usage/environment error).
            print(f"sakrit doctor: could not write {output}: {exc}", file=sys.stderr)
            return 2
    else:
        print(body)
    # The exit code is independent of --format; --check is the only thing that turns findings
    # into a nonzero exit (the doctor-output-contract note). A file that could not be verified
    # (SAKRIT000) is a finding, so it fails --check too.
    return 1 if (findings and check) else 0


def _render_doctor(findings: list[Finding], scanned: int, *, fmt: str) -> str:
    if fmt == "json":
        return json.dumps(findings_to_json(findings, scanned, version=__version__), indent=2)
    if fmt == "sarif":
        return json.dumps(findings_to_sarif(findings, version=__version__), indent=2)
    # text (default): findings, one per line, then a one-line summary.
    noun = "file" if scanned == 1 else "files"
    lines = [f.render() for f in findings]
    if findings:
        lines.append(f"sakrit doctor: {len(findings)} finding(s) in {scanned} {noun} scanned.")
    else:
        lines.append(f"sakrit doctor: no findings in {scanned} {noun} scanned.")
    return "\n".join(lines)


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
