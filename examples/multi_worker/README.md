# Multi-worker exactly-once

Several agents/workers hitting the **same** effect at the same instant — and it still fires **exactly
once**.

```bash
python examples/multi_worker/demo.py
```

```
6 workers raced the same charge → 1 charge landed
all 6 workers returned the same receipt: {'charge_id': 'ch_1'}
```

## What it shows

Six worker threads (separate processes in production) each open their own connection to one shared
ledger and race the same guarded charge. Under the hood Sakrit runs the **leased protocol**: one
worker acquires a lease and executes; the others see the live lease, wait, and **replay** the
winner's recorded result. A worker that stalls and returns late is **fenced** — its stale write is
rejected — so a returning "zombie" can't double-charge. The chaos suite proves this survives a real
`os._exit` mid-flight, with a peer taking over to exactly-once (`tests/chaos/`).

## The two rules

1. **One ledger connection per worker.** Each worker opens its own `SqliteLedger(shared_path,
   multi_worker=True)` and its own `Sakrit` engine. The atomic claim relies on a per-worker
   connection — don't share one engine across threads.
2. **Share the coordinate across workers on the same logical effect.** Here it's a business `key`
   (globally unique — the order). With a framework adapter, the shared piece is `scope` = your
   stable, persisted **run identity**, so every worker on that run agrees on the coordinate.

## Scope note

`SqliteLedger(..., multi_worker=True)` requires a **shared file** database (it refuses `:memory:`) and
enforces one coordination mode per file. The SQLite multi-worker path *verifies the protocol* — for
fleet **scale**, the Postgres backend is the path (a roadmap item); the same leased semantics carry
over by construction. This file is executed verbatim in CI (`tests/integration/test_multi_worker_demo.py`).
