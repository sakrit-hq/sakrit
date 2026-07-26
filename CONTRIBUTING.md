# Contributing to Sakrit

Thank you for considering a contribution. Sakrit aims to be *load-bearing*
infrastructure — a dependency other stacks import without thinking about it —
so we hold a high bar for correctness and keep the intellectual property clean
from day one.

## The CLA comes first

**Before your first contribution can be merged, you must sign our Contributor
License Agreement (CLA).**

This is deliberate and it is not negotiable. If we want Sakrit to be both
adoptable *and* acquirable, the IP has to be unambiguous from the start.
Retrofitting a CLA onto dozens of contributors later is a common way
acquisitions die in diligence — invisible right up until it's fatal
(see [Act I, step 4 of the roadmap](docs/roadmap.md)).

- The CLA is checked automatically on every pull request.
- You sign once; it covers all your future contributions.
- Signing assigns the necessary rights to the project while leaving you full
  ownership and use of your own work.

> The CLA bot / signing link will be wired up before the repository accepts
> outside contributions. Until then, this document records the intent.

## Ground rules for the code

### The core is the moat — protect its boundary

Nothing under `src/sakrit/core/` may import an agent framework (langgraph,
openai-agents, crewai, …). The core knows only about actions, keys, and durable
records. Framework glue lives in `src/sakrit/stores/`. PRs that cross this
boundary will be asked to move code before review.

### Before you open a PR

```bash
uv sync
uv run ruff format .      # format
uv run ruff check .       # lint
uv run mypy               # types (strict)
uv run pytest tests/unit  # fast tests
```

CI runs all of the above across Python 3.10–3.13. The chaos suite
(`tests/chaos/`) runs on a schedule, not per-PR.

### Commits and reviews

- Keep changes small and focused; one concern per PR.
- Describe *why*, not just *what*. Correctness arguments matter here more than
  in most projects — this library's entire purpose is a guarantee.
- New behavior needs a test. Changes to the exactly-once guarantee need a chaos
  test.

## Reporting problems

Open an issue. If you've found a case where an effect fires twice, that is the
most valuable report we can receive — include the framework, a minimal
reproduction, and where in the run the crash occurred.
