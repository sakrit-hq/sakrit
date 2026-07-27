# The golden money demo

The clearest thing Sakrit does, in one runnable file: an agent charges a card, and it charges it
**exactly once** — even when it retries, and even when it crashes at the worst possible moment.

```bash
python examples/money_agent/demo.py
```

```
1. naive agent, retried:            2 charges  ← the bug (double bill)
2. Sakrit-guarded, retried:         1 charge   ← replayed, not re-charged
3. Sakrit-guarded, crashed in window: 1 charge   ← recovery reconciled the charge
```

## What it shows

Three scenarios, each asserting how many times money actually moved:

1. **Naive (no Sakrit).** A retry — the everyday consequence of a crash, a resume, or an agent
   re-running a step — re-invokes an un-keyed charge, and the customer is billed twice. This is the
   bug the whole project exists to kill.
2. **Guarded, retried.** The same tool wrapped with Sakrit. The retry finds the recorded charge and
   **replays** it; the provider is never called again.
3. **Guarded, crashed in the dual-write window.** The provider *captures* the charge and then the
   response is lost — the process dies believing nothing happened. On restart, recovery **reconciles**
   the ambiguous row: at **L2+R** it asks the provider "did this charge land?", adopts the answer
   (the row ends SUCCEEDED), and the app's retry replays it. Still one charge. (This is an in-process
   model of the crash; the real kill-at-every-boundary evidence is the chaos suite, `tests/chaos/`.)

## The pieces

- **`provider.py`** — a Stripe-shaped fake payment provider: it deduplicates on an idempotency key and
  is queryable for a reconcile read, and it can inject failures (a timeout, a decline, and the nasty
  *commit-then-timeout* that models the dual-write window). It's deliberately a clean, importable seam
  — the same fixture is reused for the Phase 1 end-to-end tests and the Phase 3 approval reference.
- **`demo.py`** — the agent and the three scenarios. Framework-free: positional identity comes from a
  business `key=`, so there's no LangGraph or OpenAI SDK in sight.

This file is executed verbatim in CI (`tests/integration/test_money_demo.py`), so it can't rot.
