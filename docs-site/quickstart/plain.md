# Quickstart — plain Python

Sakrit is a *dependency, not a destination*: you don't need an agent framework to use it. With no
framework and no adapter, positional identity comes from a business **`key=`** you supply — a stable
string that names *this* action.

## Install

```bash
pip install sakrit
```

## The whole thing

Declare the tool's argument classes, wrap the call with `sk.guard(...)`, and pass a `key`. Run it, and
the effect fires once across a simulated restart:

```python
--8<-- "examples/quickstarts/plain_quickstart.py"
```

Run it:

```bash
python plain_quickstart.py
# fired exactly once: ['ops@example.com']
# the retry replayed the recorded result: 'delivered to ops@example.com'
```

## What's happening

- **`EffectDecl`** declares which arguments are `IDENTITY` (a different value is a different action)
  and which are `CONTENT` (the model may reword them on a retry — still the same action).
- **`key="welcome-ops"`** is the action's identity. The first `guard` records intent, runs the effect,
  and stores the result; the second `guard` with the same key finds the record and **replays** the
  stored result instead of re-running.
- **`ledger.close()`** models a process restart — the durable SQLite ledger is the only thing carried
  across, exactly as it would be if the process had crashed and restarted.

For a coordinate that isn't a single business key (e.g. a loop), use `sk.step(...)`, described in
**[Architecture](../architecture.md)**.

!!! note "This file is tested"
    This exact script is executed verbatim in CI, so it can't rot. See
    `examples/quickstarts/plain_quickstart.py`.
