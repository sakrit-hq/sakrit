# Sakrit

[![CI](https://github.com/sakrit-hq/sakrit/actions/workflows/ci.yml/badge.svg)](https://github.com/sakrit-hq/sakrit/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/sakrit.svg)](https://pypi.org/project/sakrit/)
[![Python](https://img.shields.io/pypi/pyversions/sakrit.svg)](https://pypi.org/project/sakrit/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

**Exactly-once effects for AI agents.**

Sakrit is a thin, framework-agnostic layer that sits between an AI agent and the
tools it calls, guaranteeing that every action with real-world consequences —
sending an email, charging a card, writing to a database — happens **exactly
once**, even when the agent crashes, resumes, retries, or explores several plans
in parallel.

> Agent frameworks solved *checkpointing* — saving progress so a crashed run can
> resume. They did **not** solve *idempotent side effects*. When an agent resumes
> from a save point, it re-runs steps it already completed. The save point rewinds
> the agent but not the world. Sakrit closes that gap.

The honest promise: **effectively-exactly-once wherever a provider can cooperate, and
never silent when it can't.** Where exactly-once is genuinely impossible (a crash in the
window between an effect and its record), Sakrit *surfaces* the ambiguity for a human
instead of guessing — never a silent duplicate, never a silent drop.

**No telemetry, ever.** The library phones nowhere — `import sakrit` opens no network
connection and imports no framework. It talks only to endpoints you configure, when you
call the code that uses them.

## Installation

```bash
pip install sakrit
```

The core is dependency-free. Framework adapters are optional extras:

```bash
pip install "sakrit[langgraph]"       # LangGraph adapter
pip install "sakrit[openai-agents]"   # OpenAI Agents SDK adapter
```

Requires Python 3.10–3.14.

## Usage

Wrap the tool that touches the world. Declare which arguments are *identity* (a
different value means a different action) and which are *content* (the model may
reword them on a retry). Add a framework adapter and Sakrit reads the runtime's stable
per-step coordinate automatically:

```python
from sakrit import Sakrit, SqliteLedger, EffectDecl, ArgClass
from sakrit.adapters.langgraph import LangGraphAdapter  # pip install "sakrit[langgraph]"

sk = Sakrit(
    SqliteLedger("sakrit.db"), secret=b"<per-deployment-secret>", adapter=LangGraphAdapter()
)


@sk.effect(
    EffectDecl(
        "email.send",
        {
            "to": ArgClass.IDENTITY,  # a different recipient is a different email
            "subject": ArgClass.IDENTITY,
            "body": ArgClass.CONTENT,  # a reworded body is the *same* email
        },
    )
)
def send_email(to: str, subject: str, body: str) -> str:
    return smtp.send(to, subject, body)


# Called inside a LangGraph node: crash, resume, or retry → send_email fires exactly once.
# A re-run returns the saved result instead of sending again.
```

**No agent framework?** Supply the action's identity yourself with a business `key`
(no extra needed) — this is the whole thing, dependency-free:

```python
from sakrit import Sakrit, SqliteLedger, EffectDecl, ArgClass

sk = Sakrit(SqliteLedger("sakrit.db"), secret=b"<per-deployment-secret>")

CHARGE = EffectDecl(
    "payment.charge", {"customer": ArgClass.IDENTITY, "amount_cents": ArgClass.IDENTITY}
)


def charge_card(customer: str, amount_cents: int) -> dict:
    # Your real provider call goes here, e.g.
    #   return stripe.PaymentIntent.create(customer=customer, amount=amount_cents)
    # (Stripe amounts are in cents.) A runnable stand-in so this block copy-pastes and runs:
    return {"id": "pi_demo", "customer": customer, "amount": amount_cents}


order_id = "4471"  # from your domain — the order this charge settles

# `key` names the intent — one per logical charge, supplied per call. Crash/retry of the
# same order dedups; a different order gets a different key and charges.
sk.guard(
    CHARGE,
    charge_card,
    kwargs={"customer": "cus_8815", "amount_cents": 4999},  # $49.99
    key=f"order-{order_id}-charge",
)
```

> **Where does the `key` come from?** It names the *intent*, so it's derived from your **domain**
> (`f"order-{order_id}-charge"`) — never from the call's arguments, and never a payload hash. That's
> what makes repeats correct in both directions: the *same* order retried (crash, timeout, an agent
> re-running the step) reuses the key and dedups; a genuinely *new* charge has a new order and a new
> key, so it fires — **even if the arguments are byte-identical** (`cus_8815` buying the same item
> twice is two orders → two keys → two charges; a payload-hash key would silently swallow the second
> one). The `IDENTITY` args don't *set* the key — they **verify** it: if a call arrives under an
> existing key with a different `amount_cents`, that's not a retry, and Sakrit raises `DivergentRetry`
> instead of charging again or replaying the old result. *Key = which intent; identity args = proof
> it's that intent.* (Same model as Stripe idempotency keys — caller-supplied per operation, never a
> payload hash.)

> **The sequential-repeat trap.** Calling the *same* guarded tool again at the *same*
> call site with the *same* identity args (e.g. deliberately sending one reminder twice)
> replays the recorded result — the effect does **not** re-fire. That is exactly right for
> a crash/resume retry, but a silent no-op for a deliberate repeat. To fire it again, give
> each call its own position:
>
> ```python
> for i, recipient in enumerate(recipients):
>     with sk.step(occurrence=i):
>         send_email(to=recipient, subject=..., body=...)
> ```
>
> Every replay is logged at INFO (and fires the ledger's `on_replay` hook), so it is
> *told*, not silent. A *concurrent* overlapping second call is loud (`EffectInFlightError`);
> only the sequential same-args repeat swallows.

### Find unprotected effects in your code

`sakrit doctor` statically scans your source (never importing or running it) for
consequential calls — HTTP mutations, Stripe charges, SMTP sends, boto3 verbs, write-SQL —
that aren't under a Sakrit guard:

```bash
sakrit doctor .                    # zero-config scan
sakrit doctor --check .            # exit nonzero on findings (for CI)
sakrit doctor --format sarif .     # upload to GitHub code scanning
```

Not installed? `pipx run sakrit doctor .` runs the scan without installing anything.

### Runnable examples

Each is a real script, executed verbatim in CI:

- [`examples/quickstarts/`](examples/quickstarts/) — plain Python, LangGraph, and OpenAI
  Agents SDK.
- [`examples/money_agent/`](examples/money_agent/) — an agent that charges a card **exactly
  once**, even through a crash in the dual-write window: `python examples/money_agent/demo.py`.
- [`examples/multi_worker/`](examples/multi_worker/) — **six workers racing the same charge**
  concurrently, and it still fires once (leases + fencing): `python examples/multi_worker/demo.py`.

## The guarantee (L0–L3)

Exactly-once against a *non-cooperating* external system is impossible — the effect and its
record live in different failure domains. Sakrit is honest about that: you declare each tool
onto a rung, and the semantics follow.

| Level | Mechanism | What you get |
|---|---|---|
| **L3** | same-DB transaction | true exactly-once |
| **L2+R** *(for money)* | provider idempotency key + reconcile | effectively-exactly-once, no silent TTL cliff |
| **L2 / L1** | provider key / reconcile-on-recovery | effectively-exactly-once |
| **L0** *(default)* | write-ahead + halt on ambiguity | at-most-once, ambiguity **surfaced** |

"Never silent" is the part nobody else ships: at L0, a crash in the ambiguous window becomes
a loud, surfaced `AMBIGUOUS` for a human to resolve — not a guess.

### Resolving an ambiguous effect

When a run raises `AmbiguousOutcome`, Sakrit is telling you the truth: a crash landed
between an effect firing and its record, so it *can't* know whether the effect happened —
and it refuses to guess. Every retry keeps raising until you supply the missing evidence.
You resolve it out of band, on the ledger, once you've checked what actually happened at the
provider. Do it as a one-off script with the worker stopped, so the ledger's single-writer
lock is free:

```python
from sakrit import SqliteLedger
from sakrit.core import EffectState

ledger = SqliteLedger("sakrit.db")

# 1. List what halted. `sakrit audit sakrit.db --state AMBIGUOUS` shows each key's tool + scope.
for key in ledger.keys_in(EffectState.AMBIGUOUS):
    print(key)

# 2. Check the provider for a given key, then record the truth — pick the *honest* one:
#    - it provably never ran → free the row so a retry can fire it:
ledger.accept_late_evidence(some_key, failed=True)
#    - it DID run and you have the result → record it, and future calls replay it:
ledger.accept_late_evidence(some_key, result={"id": "pi_..."})
```

Call `failed=True` only when you've confirmed the effect did **not** happen, and `result=…`
only when you have the real evidence that it did — that honesty is the whole guarantee.
There is deliberately no automatic resolver: the one thing a machine cannot know is what
happened in the gap, so a human closes it.

## Status

**Pre-1.0, but the core guarantee is built and hardened.** Exactly-once (or a loud, surfaced
ambiguity) across crashes, retries, and resumes — for **single- and multi-worker** agents
(leases + fencing) — with the full **L0–L3 provider ladder**, a write-ahead SQLite ledger,
replay, and a startup recovery scan. Adapters for **LangGraph, OpenAI Agents SDK, and plain
Python**. Correctness is proven by a **kill-at-every-boundary chaos suite** (the do-not-launch
gate) and multiple adversarial-review rounds.

APIs are stabilizing under three explicit promises from 0.1 (ledger on-disk format, the SED
tool-declaration format, and the public names) — see [`STABILITY.md`](STABILITY.md).

On the roadmap, pull-gated: a Postgres ledger backend (fleet scale), declarative approval
holds, and a hosted "inbox for surfaced effects" layer.

## Development

Requires Python ≥ 3.10. This project uses [uv](https://docs.astral.sh/uv/).

```bash
uv sync --all-extras --dev    # install deps + framework extras + dev tools
uv run pytest tests/unit      # fast suite
uv run ruff check . && uv run mypy   # lint + types
```

See [`CONTRIBUTING.md`](CONTRIBUTING.md) to get involved (a signed CLA comes first).

## License

Apache-2.0.
