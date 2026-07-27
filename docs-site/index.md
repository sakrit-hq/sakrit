# Sakrit

**Exactly-once effects for AI agents.**

An AI agent's retry is a double charge. Sakrit is a thin, framework-agnostic layer that sits between
an agent and the tools it calls, so that every action with real-world consequences — sending an
email, charging a card, writing to a database — happens **exactly once**, even when the agent
crashes, resumes, retries, or explores several plans in parallel. And when exactly-once is genuinely
impossible, Sakrit **tells you** instead of guessing.

!!! quote "The honest claim"
    Sakrit delivers **effectively-exactly-once wherever provider cooperation exists, and never fails
    silently where it doesn't.** "Never silent" is the part nobody else ships — every duplicate today
    is silent, and a system that either prevents the duplicate or *tells you it couldn't* is a
    categorical improvement.

## The problem

Agent frameworks solved *checkpointing* — saving progress so a crashed run can resume. They did
**not** solve *idempotent side effects*. When an agent resumes from a save point, it re-runs steps it
already completed. The save point rewinds the agent but not the world. Sakrit closes that gap.

## Thirty seconds

Wrap the tool that touches the world. Declare which arguments are *identity* (a different value means
a different action) and which are *content* (the model may reword them on a retry).

```python
from sakrit import Sakrit, SqliteLedger, EffectDecl
from sakrit.core import ArgClass

sk = Sakrit(SqliteLedger("sakrit.db"), secret=b"<per-deployment-secret>")


@sk.effect(EffectDecl("email.send", {
    "to": ArgClass.IDENTITY,       # a different recipient is a different email
    "subject": ArgClass.IDENTITY,
    "body": ArgClass.CONTENT,      # a reworded body is the *same* email
}))
def send_email(to, subject, body):
    return smtp.send(to, subject, body)

# Crash, resume, or retry: send_email fires exactly once.
# A re-run returns the saved result instead of sending again.
```

## See it charge a card once

The flagship example — an agent that charges a card, and charges it **exactly once** through a retry
*and* through a crash at the worst possible moment:

```
python examples/money_agent/demo.py

1. naive agent, retried:            2 charges  ← the bug (double bill)
2. Sakrit-guarded, retried:         1 charge   ← replayed, not re-charged
3. Sakrit-guarded, crashed in window: 1 charge   ← recovery reconciled the charge
```

Source and walkthrough: `examples/money_agent/`. It's executed verbatim in CI.

## Where to go next

- **[The guarantee (L0–L3)](guarantee.md)** — exactly what Sakrit promises at each level of provider
  cooperation. Read this first; it's the whole honest picture.
- **Quickstarts** — copy-paste, runnable, and executed verbatim in CI:
  [plain Python](quickstart/plain.md), [LangGraph](quickstart/langgraph.md),
  [OpenAI Agents SDK](quickstart/openai-agents.md).
- **[Architecture](architecture.md)** — positional identity, the coordinate ladder, never-silent.
- **[Assurance](assurance.md)** — how we kill our own process at every dangerous boundary and measure
  what happens. The trust artifact.
- **[What Sakrit is not](what-sakrit-is-not.md)** — how it relates to durable-execution engines and
  tracing tools.

## Design principles

- **Never silent, never dishonest.** Every failure mode is a loud halt, never a silent duplicate or a
  silent drop.
- **[No telemetry, ever](no-telemetry.md).** The library phones nowhere.
- **A dependency, not a destination.** `import sakrit` never imports a framework; the core stays
  dependency-free.
