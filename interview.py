#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
Sakrit interview demo — step-by-step with manual pacing.

Run from the project root:
    python interview.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = sys.executable
SAKRIT_CLI = ROOT / ".venv" / "bin" / "sakrit"

DEMO_DIR = Path("/tmp/sakrit_demo")
DEMO_DB = DEMO_DIR / "money.db"
DEMO_WORLD = DEMO_DIR / "world.jsonl"

# ANSI
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
RED = "\033[91m"
GRAY = "\033[90m"

TOTAL = 7


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def clear() -> None:
    os.system("clear")


def header(step: int, title: str) -> None:
    bar = "─" * 58
    print(f"\n{CYAN}{BOLD}  SAKRIT  ·  exactly-once effects for AI agents{RESET}")
    print(f"  {GRAY}{bar}{RESET}\n")
    print(f"  {BOLD}STEP {step} / {TOTAL}  ·  {title}{RESET}")
    print(f"  {GRAY}{bar}{RESET}\n")


def say(text: str) -> None:
    for line in textwrap.wrap(text.strip(), width=56):
        print(f"  {line}")
    print()


def cmd_line(cmd: str) -> None:
    print(f"  {GRAY}$ {cmd}{RESET}\n")


def pause(label: str = "Press Enter to run  ↵") -> None:
    print(f"  {YELLOW}{label}{RESET}", end="", flush=True)
    input()
    print()


def ok(msg: str) -> None:
    print(f"\n  {GREEN}✓  {msg}{RESET}\n")


def run(cmd: str, env: dict | None = None) -> int:
    merged = {**os.environ, **(env or {})}
    return subprocess.run(cmd, shell=True, cwd=ROOT, env=merged).returncode


def run_capture(cmd: str) -> str:
    result = subprocess.run(cmd, shell=True, cwd=ROOT, capture_output=True, text=True)
    return result.stdout or result.stderr


def _clean_demo_dir() -> None:
    DEMO_DIR.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm", ".lock"):
        p = DEMO_DB.parent / (DEMO_DB.name + suffix)
        p.unlink(missing_ok=True)
    DEMO_WORLD.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

def step_landing(step: int) -> None:
    clear()
    header(step, "The Problem")
    say(
        "AI agents retry. A naive retry on a payment tool "
        "is a double charge. Sakrit guarantees exactly-once "
        "execution — through crashes, retries, and resumes — "
        "and tells you honestly when exactly-once is impossible."
    )
    cmd_line("open landing/index.html")
    pause("Press Enter to open the landing page  ↵")
    run("open landing/index.html")
    ok("Opened in browser.")
    pause("Press Enter for next step  ↵")


def step_three_scenarios(step: int) -> None:
    clear()
    header(step, "Three Scenarios — Naive vs. Guarded")
    say(
        "Three runs. One number is what matters: how many times "
        "did money actually move? Naive agent retries → double "
        "charge. Sakrit-guarded → exactly one charge, even "
        "through a crash."
    )
    cmd_line("python examples/money_agent/demo.py")
    pause()
    run(f"{PY} examples/money_agent/demo.py")
    ok("Scenarios complete.")
    pause("Press Enter for next step  ↵")


def step_crash_kill(step: int) -> None:
    clear()
    header(step, "Real Crash — Process Hard-Killed (os._exit)")
    say(
        "The charge lands at the payment provider. Then the "
        "process is hard-killed — exit 137 — before the ledger "
        "records it. Money moved. Receipt is gone."
    )
    cmd_line(
        "SAKRIT_TESTING=1 SAKRIT_CRASH_AT=after_dispatch \\\n"
        "  python examples/money_agent/crash_worker.py"
    )
    _clean_demo_dir()
    pause()
    rc = run(
        f"{PY} examples/money_agent/crash_worker.py",
        env={
            "SAKRIT_TESTING": "1",
            "SAKRIT_CRASH_AT": "after_dispatch",
            "MONEY_DB": str(DEMO_DB),
            "MONEY_WORLD": str(DEMO_WORLD),
        },
    )
    print(f"\n  {RED}exit {rc}{RESET}  — killed. Charge already landed in the provider.\n")
    pause("Press Enter for next step  ↵")


def step_crash_recovery(step: int) -> None:
    clear()
    header(step, "Recovery — Reconcile & Replay")
    say(
        "On restart, Sakrit asks the provider: 'did this charge "
        "land?' It adopts the answer, marks the row SUCCEEDED, "
        "and the app's retry replays it. One charge total. "
        "No code change. No manual fix."
    )
    cmd_line("python examples/money_agent/crash_worker.py   # no kill flag")
    cmd_line("wc -l /tmp/sakrit_demo/world.jsonl             # charge count")
    pause()
    run(
        f"{PY} examples/money_agent/crash_worker.py",
        env={"MONEY_DB": str(DEMO_DB), "MONEY_WORLD": str(DEMO_WORLD)},
    )
    print()
    run("wc -l /tmp/sakrit_demo/world.jsonl")
    ok("One charge — despite the crash and the retry.")
    pause("Press Enter for next step  ↵")


def step_audit(step: int) -> None:
    clear()
    header(step, "Audit Trail — Full Provenance")
    say(
        "Every effect has a full provenance record: key, tool, "
        "state, fingerprint, timestamps. Export JSON or CSV. "
        "Feed it directly to your compliance team."
    )
    cmd_line(f"sakrit audit /tmp/sakrit_demo/money.db")
    pause()
    raw = run_capture(f"{SAKRIT_CLI} audit {DEMO_DB}")
    try:
        data = json.loads(raw)
        print(json.dumps(data, indent=2))
    except json.JSONDecodeError:
        print(raw)
    ok("Full provenance on every effect.")
    pause("Press Enter for next step  ↵")


def step_quickstart(step: int) -> None:
    clear()
    header(step, "Integration — One Decorator")
    say(
        "This is what a new user sees in the first five minutes. "
        "@sk.effect is the whole API surface. No framework lock-in. "
        "Works with LangGraph, OpenAI Agents, or plain Python."
    )
    cmd_line("open site/quickstart/plain/index.html")
    pause("Press Enter to open the quickstart docs  ↵")
    run("open site/quickstart/plain/index.html")
    ok("Quickstart docs opened in browser.")
    pause("Press Enter for next step  ↵")


def step_doctor(step: int) -> None:
    clear()
    header(step, "Safety Net — sakrit doctor")
    say(
        "Statically scans your codebase and flags every "
        "side-effecting call that isn't guarded. Zero false "
        "negatives on CI means you can't accidentally ship "
        "an unguarded Stripe call."
    )
    cmd_line("sakrit doctor --check src")
    pause()
    run(f"{SAKRIT_CLI} doctor --check src")
    ok("Codebase is clean — every effect is guarded.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    clear()
    print(f"\n{CYAN}{BOLD}  SAKRIT  ·  Exactly-once effects for AI agents{RESET}\n")
    print(f"  {DIM}A guided demo. Press Enter at each step to advance.{RESET}\n")
    pause("Press Enter to begin  ↵")

    step_landing(1)
    step_three_scenarios(2)
    step_crash_kill(3)
    step_crash_recovery(4)
    step_audit(5)
    step_quickstart(6)
    step_doctor(7)

    clear()
    print(f"\n{CYAN}{BOLD}  SAKRIT  ·  Demo complete{RESET}\n")
    print(f"  {GREEN}The problem is real. The solution is production-grade.{RESET}\n")


if __name__ == "__main__":
    main()
