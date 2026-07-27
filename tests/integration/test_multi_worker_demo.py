# SPDX-License-Identifier: Apache-2.0
"""The multi-worker example, executed verbatim.

Proves the documented claim: N workers, each its own connection to a shared multi_worker ledger,
racing the same effect → it fires exactly once and every worker gets the same result. Run a few
rounds so a concurrency regression can't slip through as a lucky pass.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

_DEMO = Path(__file__).resolve().parents[2] / "examples" / "multi_worker" / "demo.py"


def _load():
    spec = importlib.util.spec_from_file_location("mw_demo", _DEMO)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["mw_demo"] = module
    spec.loader.exec_module(module)
    return module


def test_multi_worker_demo_fires_once() -> None:
    demo = _load()
    demo.main()  # asserts exactly one charge + all workers get the receipt


def test_multi_worker_race_is_exactly_once_over_several_rounds() -> None:
    demo = _load()
    for _ in range(5):
        n_charges, results = demo.run()
        assert n_charges == 1, f"race produced {n_charges} charges — not exactly-once"
        assert len(results) == demo.N_WORKERS
        assert all(r == {"charge_id": "ch_1"} for r in results)
