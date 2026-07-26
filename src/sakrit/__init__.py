# SPDX-License-Identifier: Apache-2.0
"""Sakrit — exactly-once effects for AI agents.

A thin, framework-agnostic layer that sits between an AI agent and the tools it
calls, guaranteeing that every action with real-world consequences happens
exactly once — even across crashes, resumes, retries, and parallel plans.

The public surface is intentionally empty for now; the narrow core lands in
Act II (see docs/roadmap_v1.md).
"""

from sakrit.__about__ import __version__

__all__ = ["__version__"]
