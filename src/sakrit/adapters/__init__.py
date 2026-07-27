# SPDX-License-Identifier: Apache-2.0
"""Runtime adapters — the framework-facing surface.

Each adapter sources a :class:`~sakrit.core.coordinate.Coordinate` from its
runtime and satisfies :class:`~sakrit.core.adapter.RuntimeAdapter`. Framework
imports belong *here*, never in ``sakrit.core``.

:class:`~sakrit.adapters.fake.FakeAdapter` is the in-memory reference the whole
core test suite runs against — see the FakeAdapter rule in ``docs/design.md`` §11.

Shipped adapters (each behind its extra, each with a conformance gate):

- ``sakrit.adapters.langgraph`` — ``call_site = checkpoint_ns`` (interrupt/resume).
- ``sakrit.adapters.openai_agents`` — ``call_site = tool_call_id`` (RunState resume).

**CrewAI: no adapter ships, deliberately.** Probed empirically at crewai 1.15.7:
an adapter is only honest if the runtime exposes a step identity that is stable
across its durable re-execution regime, and CrewAI exposes none ambiently —
``Task.id`` is a fresh uuid per process and ``Crew.replay`` maps stored outputs
to tasks *by index*, while the Flow contextvars (``current_flow_id``) carry a
fresh per-kickoff execution id even when ``@persist`` restores state under the
app-supplied durable id. A coordinate built on either would re-key on every
restart — a guard that dedups nothing. Integrate CrewAI through the ladder
instead: a business ``key=``, or ``with sk.step(name=..., scope=<the same
durable id you give @persist>)``. Re-evaluate if CrewAI ever exposes its
persisted state id ambiently.
"""

from sakrit.adapters.fake import FakeAdapter

__all__ = ["FakeAdapter"]
