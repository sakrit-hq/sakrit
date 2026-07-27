# Running across multiple workers (and agents)

Real deployments run more than one worker: a fleet of agent processes, a pool of threads, several
replicas behind a queue. The dangerous question is the obvious one — *if two of them hit the same
effect at the same instant, does it fire twice?* With Sakrit, no. Multi-worker mode makes concurrent
workers safe by construction.

## Turn it on

```python
ledger = SqliteLedger("sakrit.db", multi_worker=True)
sk = Sakrit(ledger, secret=b"<per-deployment-secret>")
```

`multi_worker=True` switches the engine onto the **leased protocol**. When several workers claim the
same effect at once:

- **one worker wins a lease and executes** the effect;
- the others see the live lease, **wait, and replay** the winner's recorded result — they never
  re-execute;
- a worker that stalls past its lease and returns late is **fenced** — its stale write is rejected —
  so a returning "zombie" can't double-fire;
- if the lease-holder dies mid-flight, the next worker **takes over by ladder** (L2 re-dispatches and
  the provider dedups; L1 reconciles — "did it happen?" — rather than re-dispatching blind; L0
  surfaces a loud `AMBIGUOUS`). This is proven under a real `os._exit` in the chaos suite.

## The two rules

**1. One ledger connection per worker.** Each worker opens its *own* `SqliteLedger(shared_path,
multi_worker=True)` and its own `Sakrit` engine. The atomic claim relies on a per-worker connection —
sharing one engine across threads breaks it.

**2. Share the coordinate across workers on the same logical effect.** Two workers only dedup against
each other if they agree on the effect's coordinate. Two ways to make them agree:

- **A business `key`** (globally unique): `key="order-4471-charge"`. Its stability domain is the
  business domain itself, so any worker anywhere charging that order lands on the same key.
- **A framework adapter + explicit `scope`**: the adapter supplies the per-step coordinate, and you
  supply `scope` = a **stable, persisted run identity**. Every worker on that run agrees. (Do *not*
  let scope come from an ambient/per-process value — a different scope per worker means zero dedup
  while looking guarded.)

## The whole thing, runnable

Six workers racing one charge → it fires once, and all six get the same receipt:

```python
--8<-- "examples/multi_worker/demo.py"
```

```
6 workers raced the same charge → 1 charge landed
all 6 workers returned the same receipt: {'charge_id': 'ch_1'}
```

## Honest limits

- **Shared file required.** `multi_worker=True` refuses `:memory:` and stamps a coordination mode into
  the DB header, so a multi-worker file can't be reopened single-worker by mistake.
- **Protocol vs. scale.** The SQLite multi-worker path *verifies the protocol* — leases, fencing,
  takeover-by-ladder, all chaos-tested. For fleet **scale**, the Postgres backend is the path (a
  roadmap item); the same leased semantics carry over by construction, and the entire chaos +
  conformance suite is the acceptance bar for it.
- **Single-worker is enforced too.** Without `multi_worker=True`, a file ledger takes an OS lock
  (kernel-released on death) and refuses a second worker loudly — you never *accidentally* run
  multi-worker.

See also **[The guarantee (L0–L3)](guarantee.md)** for what "takeover by ladder" means per level, and
**[Assurance](assurance.md)** for the kill-at-every-boundary evidence behind these claims.
