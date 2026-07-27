# SPDX-License-Identifier: Apache-2.0
"""Sakrit performance benchmark — measure the overhead, honestly and reproducibly.

Buyers ask "what does the guarantee cost?" — so we measure it before they do, with a committed
script anyone can rerun. It reports:

- **Guard overhead**: the added time per call to record an effect, versus calling the raw function.
- **The fsync tax**: how much of that is SQLite durability — WAL + ``synchronous=FULL`` (the
  power-crash-safe default) vs. WAL + ``NORMAL`` (process-crash-safe, opt-in) vs. ``:memory:``.
- **Tail**: median AND p95/p99/max — for an fsync-bound path the tail is the real story.
- **Replay**: the cost of the read path — a re-run that dedups and returns the recorded result.

**Every write config gets its own fresh DB file and its own fresh key sequence.** That structural
isolation is the real protection against an earlier bug where configs shared one file, so later
configs silently hit already-SUCCEEDED rows and measured the *replay* path. Two guards back it up:
a per-config row-count tripwire in `_record` (catches pre-seeded or missing rows — but NOT a
same-key masquerade, whose replays make `rows == calls` hold coincidentally), and a **cross-config
assertion in `run()`** (`WAL+NORMAL median clearly above replay`) that a masquerade cannot satisfy.

Run it::

    python bench/benchmark.py                 # human-readable table
    python bench/benchmark.py --json          # machine-readable
    python bench/benchmark.py -n 5000         # more iterations

Numbers are machine-specific; the *shape* (fsync dominates FULL; NORMAL is much cheaper; replay is a
read) is what transfers.
"""

from __future__ import annotations

import argparse
import json
import platform
import sqlite3
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from sakrit import EffectDecl, Sakrit, SqliteLedger
from sakrit.core import ArgClass

DECL = EffectDecl("bench.noop", {"i": ArgClass.IDENTITY})
SECRET = b"bench-secret"
US = 1_000_000.0


def _measure(fn: Callable[[], None], n: int, warmup: int) -> list[float]:
    for _ in range(warmup):
        fn()
    samples: list[float] = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - t0)
    return samples


def _pct(samples: list[float], p: float) -> float:
    s = sorted(samples)
    k = min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1))))
    return s[k]


def _stats_us(samples: list[float]) -> dict[str, float]:
    return {
        "median": round(_pct(samples, 50) * US, 2),
        "p95": round(_pct(samples, 95) * US, 2),
        "p99": round(_pct(samples, 99) * US, 2),
        "max": round(max(samples) * US, 2),
    }


def _row_count(ledger: SqliteLedger) -> int:
    return int(ledger.conn.execute("SELECT COUNT(*) FROM effects").fetchone()[0])


def _raw_baseline(n: int, warmup: int) -> list[float]:
    ctr = {"i": 0}

    def noop(i: int) -> int:
        return i

    def call() -> None:
        ctr["i"] += 1
        noop(ctr["i"])

    return _measure(call, n, warmup)


def _record(n: int, warmup: int, db: str, **ledger_kwargs: object) -> list[float]:
    """Time a *fresh* record per call (unique key each call — the write path). Self-checks that
    every call actually inserted a row, so a replay-masquerade (the F-1 bug) fails loudly."""
    ledger = SqliteLedger(db, **ledger_kwargs)  # type: ignore[arg-type]
    sk = Sakrit(ledger, secret=SECRET)
    ctr = {"i": 0}

    def noop(i: int) -> int:
        return i

    def call() -> None:
        ctr["i"] += 1
        sk.guard(DECL, noop, kwargs={"i": ctr["i"]}, key=f"bench-{ctr['i']}")

    try:
        samples = _measure(call, n, warmup)
        # Secondary tripwire: catches pre-seeded or missing rows. It does NOT catch a same-key
        # masquerade (those calls replay, so rows == made holds coincidentally) — the fresh-file
        # isolation above and the cross-config assert in run() are the real protection.
        rows, made = _row_count(ledger), ctr["i"]
        if rows != made:
            raise AssertionError(
                f"bench tripwire FAILED for {db}: {rows} rows for {made} calls — "
                f"the ledger was not fresh/empty for this config."
            )
        return samples
    finally:
        ledger.close()


def _replay(n: int, warmup: int, db: str) -> list[float]:
    """Time replay of an already-recorded effect (same key — the read path). Row count stays 1."""
    ledger = SqliteLedger(db)
    sk = Sakrit(ledger, secret=SECRET)

    def noop(i: int) -> int:
        return i

    sk.guard(DECL, noop, kwargs={"i": 1}, key="bench-replay")  # record once

    def call() -> None:
        sk.guard(DECL, noop, kwargs={"i": 1}, key="bench-replay")  # every call replays

    try:
        samples = _measure(call, n, warmup)
        if _row_count(ledger) != 1:
            raise AssertionError("replay bench unexpectedly inserted rows")
        return samples
    finally:
        ledger.close()


def run(n: int = 2000, warmup: int = 50) -> dict[str, object]:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        baseline = _raw_baseline(n, warmup)
        # Each config: its OWN fresh file (or :memory:) and its OWN key sequence.
        full = _record(n, warmup, str(tmp / "full.db"))
        normal = _record(n, warmup, str(tmp / "normal.db"), i_accept_data_loss=True)
        mem = _record(n, warmup, ":memory:")
        replay = _replay(n, warmup, str(tmp / "replay.db"))

    per = {
        "raw_baseline": _stats_us(baseline),
        "guarded_record_wal_full": _stats_us(full),
        "guarded_record_wal_normal": _stats_us(normal),
        "guarded_record_memory": _stats_us(mem),
        "guarded_replay": _stats_us(replay),
    }
    med = {k: v["median"] for k, v in per.items()}
    # Load-bearing structural guard (Fable): a shared-DB/same-key masquerade would measure the
    # replay path for a "write" config, dropping its median to the replay level. A real WAL+NORMAL
    # write (fsync-ish) sits well above a replay (pure read); require it. The per-config row-count
    # check cannot catch that masquerade — this cross-config comparison can.
    if med["guarded_record_wal_normal"] <= med["guarded_replay"] * 1.5:
        raise AssertionError(
            f"WAL+NORMAL median ({med['guarded_record_wal_normal']}µs) is not clearly above replay "
            f"({med['guarded_replay']}µs) — a write config may be measuring the replay path."
        )
    return {
        "env": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
            "iterations": n,
        },
        "per_call_us": med,  # medians, for the headline table + the test's invariant checks
        "tail_us": per,  # median/p95/p99/max per config
        "overhead_us": {
            "record_wal_full": round(med["guarded_record_wal_full"] - med["raw_baseline"], 2),
            "record_wal_normal": round(med["guarded_record_wal_normal"] - med["raw_baseline"], 2),
            "fsync_tax": round(
                med["guarded_record_wal_full"] - med["guarded_record_wal_normal"], 2
            ),
            "replay": round(med["guarded_replay"] - med["raw_baseline"], 2),
        },
    }


def _table(r: dict[str, object]) -> str:
    env = r["env"]  # type: ignore[assignment]
    tail = r["tail_us"]  # type: ignore[assignment]
    ov = r["overhead_us"]  # type: ignore[assignment]
    rows = [
        ("raw function (no Sakrit)", "raw_baseline"),
        ("guarded record, WAL+FULL default", "guarded_record_wal_full"),
        ("guarded record, WAL+NORMAL", "guarded_record_wal_normal"),
        ("guarded record, :memory:", "guarded_record_memory"),
        ("guarded replay (dedup+return)", "guarded_replay"),
    ]
    lines = [
        f"Sakrit benchmark — {env['platform']}, Python {env['python']}, "
        f"SQLite {env['sqlite']}, {env['iterations']} iters/measurement",
        "",
        f"{'':34}{'median':>10}{'p95':>10}{'p99':>10}{'max':>10}   (microseconds)",
    ]
    for label, key in rows:
        s = tail[key]  # type: ignore[index]
        lines.append(f"  {label:32}{s['median']:>10}{s['p95']:>10}{s['p99']:>10}{s['max']:>10}")
    lines += [
        "",
        "Overhead over the raw call (median microseconds):",
        f"  record @ WAL+FULL                 {ov['record_wal_full']:>10}",  # type: ignore[index]
        f"  record @ WAL+NORMAL               {ov['record_wal_normal']:>10}",  # type: ignore[index]
        f"  of which fsync tax (FULL−NORMAL)  {ov['fsync_tax']:>10}",  # type: ignore[index]
        f"  replay                            {ov['replay']:>10}",  # type: ignore[index]
    ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Benchmark Sakrit's per-call overhead.")
    ap.add_argument("-n", type=int, default=2000, help="iterations per measurement")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    args = ap.parse_args()
    result = run(args.n)
    print(json.dumps(result, indent=2) if args.json else _table(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
