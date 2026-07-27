# SPDX-License-Identifier: Apache-2.0
"""Doctor corpus harness — run ``sakrit doctor`` over a pinned set of real OSS agent repos.

The bar (roadmap Stage 2 · Phase 0 · deliverable 5) is **crash-free**: the doctor must scan
every file without raising. A malformed/undecodable file must surface as a loud ``SAKRIT000``
finding, never an escaped exception — so this harness calls the *real* ``scan_path`` per file
inside a catch-all and records anything that escapes as a **crash** (a doctor robustness bug),
separately from findings. Finding density is recorded to tune the false-positive floor pre-launch.

Run it directly to print a Markdown report:

    python tests/corpus/harness.py             # scan all pinned repos, print the report
    python tests/corpus/harness.py --json      # machine-readable

Clones are shallow, pinned to the manifest SHA, cached under a scratch dir (never committed).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Import the real doctor entry points — the whole point is to exercise the shipped code path.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

from sakrit.doctor import PARSE_FAILURE, Finding, scan_path  # noqa: E402

MANIFEST = Path(__file__).with_name("manifest.json")


class CloneError(RuntimeError):
    """A pinned repo could not be fetched/checked out (e.g. no network)."""


@dataclass
class RepoReport:
    repo: str
    sha: str
    files_scanned: int = 0
    findings: list[Finding] = field(default_factory=list)
    crashes: list[tuple[str, str]] = field(default_factory=list)  # (path, exception repr)

    @property
    def by_code(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for f in self.findings:
            counts[f.code] = counts.get(f.code, 0) + 1
        return counts

    @property
    def parse_failures(self) -> int:
        return self.by_code.get(PARSE_FAILURE, 0)

    @property
    def real_findings(self) -> int:
        # Everything that isn't a "couldn't-verify" SAKRIT000 — the flags a human would review.
        return len(self.findings) - self.parse_failures


def _run(cmd: list[str], cwd: Path) -> None:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise CloneError(f"{' '.join(cmd)} failed: {proc.stderr.strip()[:300]}")


def clone_pinned(repo: str, sha: str, dest: Path) -> Path:
    """Shallow-fetch a single pinned commit into ``dest`` (idempotent if already present)."""
    if (dest / ".git").exists():
        return dest
    dest.mkdir(parents=True, exist_ok=True)
    url = f"https://github.com/{repo}.git"
    _run(["git", "init", "-q"], cwd=dest)
    _run(["git", "fetch", "-q", "--depth", "1", url, sha], cwd=dest)
    _run(["git", "checkout", "-q", "FETCH_HEAD"], cwd=dest)
    return dest


def _iter_py(root: Path):
    skip = {".git", "__pycache__", "site-packages", "node_modules", "build", "dist", "venv"}
    for path in sorted(root.rglob("*.py")):
        if any(p in skip or p.startswith(".") for p in path.relative_to(root).parts):
            continue
        yield path


def scan_repo(repo: str, sha: str, root: Path) -> RepoReport:
    """Scan a checked-out repo file-by-file with the real ``scan_path``, catching escapes."""
    report = RepoReport(repo=repo, sha=sha)
    for file in _iter_py(root):
        report.files_scanned += 1
        try:
            report.findings.extend(scan_path(file))
        except Exception as exc:  # noqa: BLE001 — a crash IS the failure we're hunting for
            report.crashes.append((str(file.relative_to(root)), repr(exc)))
    return report


def load_manifest() -> list[dict[str, str]]:
    data = json.loads(MANIFEST.read_text())
    return data["repos"]


def run_corpus(cache_dir: Path) -> list[RepoReport]:
    reports: list[RepoReport] = []
    for entry in load_manifest():
        repo, sha = entry["repo"], entry["sha"]
        dest = cache_dir / repo.replace("/", "__")
        clone_pinned(repo, sha, dest)
        reports.append(scan_repo(repo, sha, dest))
    return reports


def render_markdown(reports: list[RepoReport]) -> str:
    lines = ["# Doctor corpus run", ""]
    lines.append("| Repo | Files | SAKRIT001 | SAKRIT000 | Crashes |")
    lines.append("|---|--:|--:|--:|--:|")
    tot_files = tot_real = tot_pf = tot_crash = 0
    for r in reports:
        tot_files += r.files_scanned
        tot_real += r.real_findings
        tot_pf += r.parse_failures
        tot_crash += len(r.crashes)
        lines.append(
            f"| `{r.repo}` | {r.files_scanned} | {r.real_findings} | {r.parse_failures} "
            f"| {len(r.crashes)} |"
        )
    lines.append(
        f"| **total** | **{tot_files}** | **{tot_real}** | **{tot_pf}** | **{tot_crash}** |"
    )
    if tot_files:
        lines += [
            "",
            f"Raw flag density: {tot_real} SAKRIT001 / {tot_files} files "
            f"= {tot_real / tot_files:.3f} per file.",
        ]
    for r in reports:
        if r.crashes:
            lines += ["", f"### CRASHES in `{r.repo}` (doctor robustness bug)"]
            lines += [f"- `{p}` — {e}" for p, e in r.crashes[:50]]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Run the doctor over the pinned OSS corpus.")
    ap.add_argument("--cache", type=Path, default=Path("/tmp/sakrit-corpus-cache"))
    ap.add_argument("--json", action="store_true", help="emit JSON instead of Markdown")
    args = ap.parse_args()
    try:
        reports = run_corpus(args.cache)
    except CloneError as exc:
        print(f"corpus: clone failed (network?): {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "repo": r.repo,
                        "sha": r.sha,
                        "files": r.files_scanned,
                        "by_code": r.by_code,
                        "crashes": r.crashes,
                    }
                    for r in reports
                ],
                indent=2,
            )
        )
    else:
        print(render_markdown(reports))
    return 1 if any(r.crashes for r in reports) else 0


if __name__ == "__main__":
    raise SystemExit(main())
