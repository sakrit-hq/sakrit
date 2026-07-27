# SPDX-License-Identifier: Apache-2.0
"""The golden money demo, executed verbatim (roadmap Stage 2 · Phase 0 · deliverable 6).

The demo under ``examples/money_agent/`` is reused as the Phase 1 end-to-end fixture and the Phase 3
approval reference, so it must keep proving its claim: a naive agent double-charges on a retry, and
a Sakrit-guarded one charges exactly once — through a plain retry and through a crash in the
dual-write window. Framework-free, so it always runs here.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

_MONEY = Path(__file__).resolve().parents[2] / "examples" / "money_agent"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _MONEY / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_golden_money_demo_runs() -> None:
    _load("demo").main()  # asserts the three charge counts (2 / 1 / 1) internally


def test_provider_dedups_on_idempotency_key() -> None:
    provider = _load("provider").FakePaymentProvider()
    first = provider.charge(amount_cents=4999, currency="USD", idempotency_key="k1")
    again = provider.charge(amount_cents=4999, currency="USD", idempotency_key="k1")
    assert first == again  # same charge returned for a repeated key
    assert provider.charge_count == 1  # money moved once


def test_provider_reconcile_reports_absent_then_settled() -> None:
    provider_mod = _load("provider")
    provider = provider_mod.FakePaymentProvider()
    assert provider.reconcile("k2").verdict.value == "absent"
    provider.charge(amount_cents=100, currency="USD", idempotency_key="k2")
    assert provider.reconcile("k2").verdict.value == "settled"
