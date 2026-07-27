# SPDX-License-Identifier: Apache-2.0
"""The docs quickstarts, executed verbatim (roadmap Stage 2 · Phase 0 · deliverable 4).

Each quickstart under ``examples/quickstarts/`` is the *same file* the docs site embeds, so this
test is what keeps the copy-paste code on the site from rotting: if a quickstart stops proving
exactly-once, CI goes red. The plain quickstart runs everywhere; the framework ones run under their
extras (skipped when the framework isn't installed).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

_QUICKSTARTS = Path(__file__).resolve().parents[2] / "examples" / "quickstarts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _QUICKSTARTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_plain_quickstart_runs() -> None:
    _load("plain_quickstart").main()  # asserts exactly-once internally


def test_langgraph_quickstart_runs() -> None:
    pytest.importorskip("langgraph")
    _load("langgraph_quickstart").main()


def test_openai_agents_quickstart_runs() -> None:
    pytest.importorskip("agents")
    _load("openai_agents_quickstart").main()
