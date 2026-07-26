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

> **Not yet available.** The API below is the target experience (Act II: "write
> the README before the code"). It is here so the shape is visible and can be
> argued with — it does not run yet.

```python
from sakrit import effect


# Wrap the tool that touches the world. That's it.
@effect
def send_email(to: str, subject: str, body: str) -> None:
    smtp.send(to, subject, body)


# Crash, resume, retry, or run two plans in parallel:
# send_email fires exactly once. Re-runs return the saved result.
```

## Status

Pre-alpha. This repository currently holds project plumbing and structure only;
the narrow core lands in **Act II** of the roadmap. See
[`docs/roadmap.md`](docs/roadmap.md) for the full execution plan and
[`CONTRIBUTING.md`](CONTRIBUTING.md) to get involved (a signed CLA comes first).

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
