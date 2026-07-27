# SPDX-License-Identifier: Apache-2.0
"""Smoke test for the perf benchmark (roadmap Stage 2 · Phase 0 · deliverable 7).

The perf page cites numbers from ``bench/benchmark.py``; this keeps that script runnable so the page
can't quote a benchmark that no longer works. It runs a tiny iteration count — checking it produces
sane, positive measurements, not that any particular speed is met.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

_BENCH = Path(__file__).resolve().parents[2] / "bench" / "benchmark.py"


def _load():
    spec = importlib.util.spec_from_file_location("benchmark", _BENCH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["benchmark"] = module
    spec.loader.exec_module(module)
    return module


def test_benchmark_runs_and_reports_positive_numbers() -> None:
    # run() self-checks (rows == calls) internally, so this also guards the F-1 replay-masquerade.
    result = _load().run(n=60)
    per_call = result["per_call_us"]
    assert set(per_call) >= {
        "raw_baseline",
        "guarded_record_wal_full",
        "guarded_record_wal_normal",
        "guarded_record_memory",
        "guarded_replay",
    }
    assert all(v > 0 for v in per_call.values())
    # A real write costs more than the raw call...
    assert per_call["guarded_record_wal_full"] > per_call["raw_baseline"]
    # ...and — the invariant the F-1 bug violated — a NORMAL *write* is clearly above a *replay*.
    # If "NORMAL" were secretly the replay path again (the shared-DB masquerade), this fails.
    # run() itself asserts this cross-config guard; we re-assert it here for documentation.
    assert per_call["guarded_record_wal_normal"] > per_call["guarded_replay"] * 1.5
    # FULL (fsync every commit) is the slowest write tier.
    assert per_call["guarded_record_wal_full"] > per_call["guarded_record_wal_normal"]
    # Tail percentiles are reported (p99 >= median), the F-7 fix.
    tail = result["tail_us"]["guarded_record_wal_full"]
    assert tail["p99"] >= tail["median"] and tail["max"] >= tail["p99"]
