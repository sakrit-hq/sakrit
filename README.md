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

sk = Sakrit(SqliteLedger("sakrit.db"), secret=b"<per-deployment-secret>", adapter=LangGraphAdapter())


@sk.effect(
    EffectDecl(
        "email.send",
        {
            "to": ArgClass.IDENTITY,       # a different recipient is a different email
            "subject": ArgClass.IDENTITY,
            "body": ArgClass.CONTENT,      # a reworded body is the *same* email
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
sk = Sakrit(SqliteLedger("sakrit.db"), secret=b"<per-deployment-secret>")


@sk.effect(EffectDecl("payment.charge", {"customer": ArgClass.IDENTITY, "amount": ArgClass.IDENTITY}),
           key="order-4471-charge")   # this charge's identity
def charge_card(customer: str, amount: int) -> dict:
    return stripe.PaymentIntent.create(customer=customer, amount=amount)


charge_card(customer="cus_8815", amount=4999)   # charges exactly once, across any retry
```

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
pipx run sakrit doctor .            # zero-config scan
sakrit doctor --check .            # exit nonzero on findings (for CI)
sakrit doctor --format sarif .     # upload to GitHub code scanning
```

### Runnable examples

Each is a real script, executed verbatim in CI:

- [`examples/quickstarts/`](examples/quickstarts/) — plain Python, LangGraph, and OpenAI
  Agents SDK.
- [`examples/money_agent/`](examples/money_agent/) — an agent that charges a card **exactly
  once**, even through a crash in the dual-write window: `python examples/money_agent/demo.py`.

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
