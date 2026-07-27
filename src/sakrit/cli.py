# SPDX-License-Identifier: Apache-2.0
"""The ``sakrit`` console entry point.

One subcommand today: ``sakrit doctor [PATH …] [--check]`` — the static
unguarded-consequential-call net (see :mod:`sakrit.doctor`). Plain mode prints
findings and exits 0 (a report); ``--check`` exits 1 on any finding (a CI gate).
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

    args = parser.parse_args(argv)
    if args.command == "doctor":
        return _run_doctor(args.paths or [Path()], check=args.check)
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


if __name__ == "__main__":  # pragma: no cover — exercised via the console script
    sys.exit(main())
