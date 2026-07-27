# Assurance

This is the page most vendors can't write. Sakrit's claim is a safety claim, so the evidence is
adversarial: we **kill our own process at every dangerous boundary**, restart, and measure what
happened to both the ledger and the world.

> The one sentence we defend to a distributed-systems audience: **every kill we inject resolves to
> {exactly once, or a loud `AMBIGUOUS`} — never a silent duplicate, never a silent drop.**

## What "kill at a boundary" means

A hard kill (`os._exit(137)`, modelling SIGKILL / OOM / a deploy eviction) is injected at a named seam
in the *real* code path. Seams are no-ops in production; under test a subprocess is launched with the
crash point set in the environment, so `os._exit` really terminates the process and the OS file lock is
kernel-released — a monkeypatch wouldn't survive. **Each cell asserts the kill actually fired** (the
process exited 137 and the clean restart exited 0), so a boundary that was never reached fails the cell
rather than passing vacuously.

The world is a durable, `fsync`'d log with its own seam, so we can kill in the true ambiguous window:
**the delivery is committed, then the process dies before Sakrit records it.**

## The matrix (single worker) — all green

| # | Scenario | Kill at | World | Ledger | What it proves |
|---|---|---|--:|---|---|
| 1 | L0 | after world write | **1** | `AMBIGUOUS` | **honesty** — the effect happened; we surface it, never guess |
| 2 | L2 | after world write | **1** | `SUCCEEDED` | **money** — recovery re-dispatches; the provider deduplicates |
| 3 | L0 | after claim (pre-dispatch) | **1** | `SUCCEEDED` | **clean retry** — a pre-dispatch crash recovers |
| 4 | L0 | after mark-executing | **0** | `AMBIGUOUS` | **post-intent floor** — L0 can't prove it didn't fire → surface |
| 5 | L2 | after mark-executing | **1** | `SUCCEEDED` | **safe re-dispatch** — provider dedups a re-issued key |
| 6 | L1 | after world write | **1** | `SUCCEEDED` | **reconcile resolves** — recovery queries the world and adopts it |
| 7 | L1 | after mark-executing | **0** | `AMBIGUOUS` | **no false re-fire** — reconcile says ABSENT; surface |
| 8 | L0 | after record | **1** | `SUCCEEDED` | **settled means settled** — replay, no re-execution |
| 9 | L0 | mid-recovery (double kill) | **1** | `AMBIGUOUS` | **recovery is idempotent over itself** |
| 10 | *control (no Sakrit)* | after world write | **2** | — | **the bug is real** — the one deliberately red cell |

Nine green rows and one red control, every kill verified to have fired. Every transition into
`AMBIGUOUS` also logs a warning and fires an optional `on_ambiguous(key)` hook — "loud" means telling,
not merely a state a later pass might notice.

## Multi-worker contention

The contention protocol is verified deterministically (owner ids, the clock, and fencing tokens are
controlled): one worker wins the lease and executes; others see a live lease and replay the winner's
result; on lease expiry the next claim **takes over by ladder** — L2 re-dispatches, L1 reconciles
rather than re-dispatching blind, L0 surfaces `AMBIGUOUS`; a returning zombie's stale-token write is
fenced; a slow-but-alive owner heartbeats its lease so it isn't presumed dead. The leased L1 takeover
is also **killed for real**: a worker is hard-killed mid-dispatch, and after its lease expires another
takes over, reconciles, and adopts the delivery — exactly-once across a real kill *and* a real
takeover.

## The certified fault model (stated precisely)

- **Fault covered:** process crash (hard kill, no cleanup) at the boundaries the matrix enumerates.
- **Durability:** the ledger defaults to WAL + `synchronous=FULL` — process- *and* power-crash-safe.
  `WAL+NORMAL` (process-crash-safe only) is opt-in.
- **Single-worker is enforced** across processes by an OS file lock (kernel-released on death) and
  across threads by a single-thread-bound connection. Multi-worker mode requires a shared database and
  cannot share a file with single-worker mode.
- **The honest limit:** with zero provider cooperation (L0) and a crash in the ambiguous window, no
  library can know whether the effect happened. Our floor there is at-most-once with a loud
  `AMBIGUOUS` — the true limit of the problem, stated rather than hidden.

## Reproduce it yourself

```bash
pytest tests/chaos -m chaos          # the kill-at-every-boundary matrix
pytest tests/unit/test_contention.py # the contention protocol
```

The suite *is* the spec: the harness lives in `tests/chaos/`, and the chaos matrix runs as a
merge-blocking gate on every pull request, plus a published daily run.
