# SPDX-License-Identifier: Apache-2.0
"""Sakrit core — the exactly-once engine. THE MOAT.

Invariant: **no framework imports are allowed in this package, ever.** Nothing
under ``sakrit.core`` may import langgraph, openai-agents, crewai, or any other
agent framework. The core knows only about actions, keys, and durable records;
framework-specific glue lives in ``sakrit.stores``. This boundary is what lets
the guarantee be framework-agnostic — keep it clean.
"""
