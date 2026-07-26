# SPDX-License-Identifier: Apache-2.0
"""Sakrit — exactly-once effects for AI agents.

A thin, framework-agnostic layer that sits between an AI agent and the tools it
calls, guaranteeing that every action with real-world consequences happens
exactly once — even across crashes, resumes, retries, and parallel plans.

Importing ``sakrit`` never imports a framework. Framework adapters live under
``sakrit.adapters`` and are imported explicitly (e.g.
``from sakrit.adapters.langgraph import LangGraphAdapter``).
"""

from sakrit.__about__ import __version__
from sakrit.core import EffectDecl, SqliteLedger
from sakrit.engine import Sakrit

__all__ = ["EffectDecl", "Sakrit", "SqliteLedger", "__version__"]
