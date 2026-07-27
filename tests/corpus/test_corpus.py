# SPDX-License-Identifier: Apache-2.0
"""The doctor corpus test — crash-free is the bar (roadmap Stage 2 · Phase 0 · deliverable 5).

Opt-in and network-bound: run with ``pytest tests/corpus -m corpus``. It shallow-clones the pinned
repos in ``manifest.json`` and scans each with the real ``scan_path``; the assertion is that the
doctor **never raises** on real-world code (a malformed file must be a loud ``SAKRIT000`` finding,
not an escaped exception). Finding counts are informational — recorded, not asserted, so the test
doesn't churn as upstream repos change at their pinned SHA. If cloning fails (no network), the test
skips rather than failing a build on a connectivity blip.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# Sibling import: pytest (prepend mode) puts this file's directory on sys.path, so `harness`
# resolves without a package. It drives network + git subprocess, hence the mypy exclusion.
from harness import (  # type: ignore[import-not-found]
    CloneError,
    RepoReport,
    clone_pinned,
    load_manifest,
    scan_repo,
)

pytestmark = pytest.mark.corpus


@pytest.fixture(scope="session")
def _cache(tmp_path_factory: pytest.TempPathFactory) -> Path:
    # m14: honor SAKRIT_CORPUS_CACHE so the CI report step reuses these clones instead of
    # re-cloning all four repos. clone_pinned is idempotent when a .git already exists.
    shared = os.environ.get("SAKRIT_CORPUS_CACHE")
    if shared:
        path = Path(shared)
        path.mkdir(parents=True, exist_ok=True)
        return path
    return tmp_path_factory.mktemp("sakrit-corpus")


@pytest.mark.parametrize("entry", load_manifest(), ids=lambda e: e["repo"])
def test_doctor_is_crash_free_on_real_repo(entry: dict[str, str], _cache: Path) -> None:
    repo, sha = entry["repo"], entry["sha"]
    dest = _cache / repo.replace("/", "__")
    try:
        clone_pinned(repo, sha, dest)
    except CloneError as exc:
        pytest.skip(f"could not clone {repo}@{sha[:8]} (network?): {exc}")

    report: RepoReport = scan_repo(repo, sha, dest)

    assert report.files_scanned > 0, f"{repo}: nothing scanned — pin/clone is wrong"
    # THE BAR: not a single escaped exception across the whole repo.
    assert report.crashes == [], (
        f"{repo}: doctor crashed on {len(report.crashes)} file(s) — "
        f"first: {report.crashes[0] if report.crashes else ''}"
    )
