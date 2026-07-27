# Performance

Buyers ask what the guarantee costs, so we measured it — with a
[committed script](https://github.com/sakrit-hq/sakrit/blob/main/bench/benchmark.py) you can
rerun. The short answer: **exactly-once costs about one durable disk commit per effect** — ~0.15 ms at
the median on the power-crash-safe default — and next to a real side effect (a Stripe or SMTP call is
tens to hundreds of *milliseconds*) that's well under 1% at the median. The honest caveat is the
**tail**: fsync stalls of several milliseconds happen, so budget for p99, not the median.

## Measured overhead

Representative run — macOS (Apple silicon), Python 3.14, SQLite 3.53, 3000 iterations per
measurement, single-threaded. Each write config uses a **fresh database file and fresh keys**, so every
number below is a real record, not a replay.

| Per call | median | p95 | p99 | max |
|---|--:|--:|--:|--:|
| raw function, no Sakrit | ~0.1 µs | 0.1 | 0.2 | 0.4 |
| **guarded record, WAL + `FULL` (default)** | **~154 µs** | 244 | 559 | **~11 ms** |
| guarded record, WAL + `NORMAL` (opt-in) | ~69 µs | 114 | 446 | ~2.4 ms |
| guarded record, `:memory:` (no disk) | ~37 µs | 42 | 46 | 70 µs |
| guarded replay (dedup + return) | ~23 µs | 24 | 28 | 36 µs |

Breaking the default down (medians):

| Overhead vs. the raw call | Time |
|---|--:|
| record @ WAL + `FULL` | ~154 µs |
| record @ WAL + `NORMAL` | ~69 µs |
| **of which: the fsync tax** (`FULL` − `NORMAL`) | **~85 µs** |
| replay | ~23 µs |

## Reading the numbers

- **The median cost is the fsync.** ~85 µs of the ~154 µs default is a durable SQLite commit — the
  price of the record that *is* the guarantee. The key-derivation, HMAC fingerprint, and claim logic
  together are the ~24 µs at the `:memory:` end (no disk at all).
- **The tail is the real story on an fsync-bound path.** The default's p99 is ~0.5 ms and its *max*
  ~11 ms — a macOS fsync stall. That ~11 ms is ~11% of a 100 ms charge, not "noise." Size latency
  budgets against p99, and don't put a guarded effect on a hard sub-millisecond deadline.
- **You choose the durability, explicitly.** The default is WAL + `synchronous=FULL` — safe against a
  power loss, not just a process crash. If you only need process-crash safety, opt into WAL + `NORMAL`
  (`SqliteLedger(..., i_accept_data_loss=True)`) — ~2× cheaper at the median because fsync batches to
  WAL checkpoints instead of firing on every commit. Nothing is hidden: the ledger reports its
  configured `fault_model()` ("process-and-power-crash-safe (WAL+FULL)" or "process-crash-safe
  (WAL+NORMAL)"), a *checked* pragma, not a hope.
- **`:memory:` is the fastest write** (no disk), which is why it sits below WAL+NORMAL — it isolates
  the pure compute cost of the guard. It is ephemeral and never a durable option; it's here only to
  show how much of the file numbers is disk.
- **Replay is cheap.** A deduplicated re-run is a read — no fsync — so resumes and retries cost about
  a fifth of the `FULL` write path. Crash-recovery and retry-heavy agents pay the write cost once.
- **Perspective.** The effects Sakrit guards are network calls that move money or send messages —
  routinely 10–500 ms each. A ~0.15 ms median durable record in front of a 100 ms charge is cheap, and
  it's the difference between "charged once" and "charged twice."

## Reproduce it

```bash
python bench/benchmark.py            # the table above (median + p95/p99/max)
python bench/benchmark.py --json     # machine-readable
python bench/benchmark.py -n 5000    # more iterations
```

The script gives **each config its own fresh DB file and fresh keys** — an earlier version reused one
file and silently measured the replay path for the "NORMAL" row, understating it ~3×. That structural
isolation is the fix; a cross-config assertion (a real WAL+NORMAL write must sit well above a replay)
backs it up so the same mistake can't ship silently again. Numbers are machine-specific — fsync latency
depends on your disk — so rerun it on your own hardware; that's why the script is in the repo.
