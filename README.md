# Sakrit

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

## Quickstart

Wrap the tool that touches the world. Declare which arguments are *identity* (a
different value means a different action) and which are *content* (the model may
reword them on a retry). That's it.

```python
from sakrit import Sakrit, SqliteLedger, EffectDecl
from sakrit.core import ArgClass
from sakrit.adapters.langgraph import LangGraphAdapter

sk = Sakrit(
    SqliteLedger("sakrit.db"),
    secret=b"<per-deployment secret>",
    adapter=LangGraphAdapter(),
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


# Crash, resume, or retry: send_email fires exactly once.
# A re-run returns the saved result instead of sending again.
```

## Status

**Pre-alpha — the Act II core works.** Exactly-once for single-worker agents on
LangGraph: positional identity, the coordinate ladder, a fingerprint over identity
args, a write-ahead SQLite ledger, replay, and a startup recovery scan. Verified
end-to-end (the guarded double-email sends once across a real interrupt/resume).

Not yet: multi-worker (leases/fencing, Postgres), the provider-cooperation ladder
beyond L0/replay, the effect outbox / approval gating, and plan-epoch handling —
these are Act III. See [`CONTRIBUTING.md`](CONTRIBUTING.md) to get involved (a
signed CLA comes first).

## Development

Requires Python ≥ 3.10. This project uses [uv](https://docs.astral.sh/uv/).

```bash
# Install dependencies (including dev tools)
uv sync

# Run the test suite
uv run pytest

# Lint and type-check
uv run ruff check .
uv run mypy
```

## License

Apache-2.0.
