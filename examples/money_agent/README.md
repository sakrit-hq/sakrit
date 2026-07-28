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
   (the row ends SUCCEEDED), and the app's retry replays it. Still one charge.

   `demo.py` models this crash *in-process* for readability. The **real thing** — a child process
   hard-killed (`os._exit`) in the dual-write window — is `crash_worker.py`, asserted by
   `tests/integration/test_money_demo.py`: the kill fires (exit 137), one charge landed, and recovery
   resolves it to exactly one charge with the row SUCCEEDED.

### Run the real crash yourself

`crash_worker.py` charges once per invocation against a durable ledger (`MONEY_DB`) and a durable
provider "world" (`MONEY_WORLD`, one landed charge per line). Kill it mid-write, then restart it:

```bash
export MONEY_DB=/tmp/money.db MONEY_WORLD=/tmp/world.jsonl

# 1. Inject the kill: the charge lands, then the process is hard-killed before the ledger records it.
SAKRIT_TESTING=1 SAKRIT_CRASH_AT=after_dispatch python examples/money_agent/crash_worker.py
echo "exit $?"                 # → 137 (killed), and world.jsonl already has 1 charge

# 2. Restart with no kill: recovery reconciles the ambiguous row, the app replays.
python examples/money_agent/crash_worker.py    # → worker: charged {...}

wc -l /tmp/world.jsonl         # → 1  (exactly one charge, despite the crash + retry)
```

(The ledger leaves `/tmp/money.db` plus `-wal`/`-shm`/`.lock` sidecars; delete them to start fresh.)

## The pieces

- **`provider.py`** — a Stripe-shaped fake payment provider: it deduplicates on an idempotency key and
  is queryable for a reconcile read, and it can inject failures (a timeout, a decline, and the nasty
  *commit-then-timeout* that models the dual-write window). It's deliberately a clean, importable seam
  — the same fixture is reused for the Phase 1 end-to-end tests and the Phase 3 approval reference.
- **`demo.py`** — the agent and the three scenarios. Framework-free: positional identity comes from a
  business `key=`, so there's no LangGraph or OpenAI SDK in sight.

This file is executed verbatim in CI (`tests/integration/test_money_demo.py`), so it can't rot.
