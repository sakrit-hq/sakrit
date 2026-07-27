# Quickstart — LangGraph

The classic LangGraph pitfall: a node that performs a side effect *before* a human-approval
`interrupt()` re-runs the whole node when the graph resumes — so the side effect happens twice. Sakrit
fixes it with three lines and no change to your graph shape.

## Install

```bash
pip install "sakrit[langgraph]"
```

## The whole thing

Add a `LangGraphAdapter` to the engine and decorate the tool. The adapter reads LangGraph's stable
per-step coordinate (`checkpoint_ns`), so you don't supply a key — positional identity is automatic:

```python
--8<-- "examples/quickstarts/langgraph_quickstart.py"
```

Run it:

```bash
python langgraph_quickstart.py
# fired exactly once across the interrupt/resume: ['customer@example.com']
```

## What's happening

- **`adapter=LangGraphAdapter()`** lets Sakrit source the coordinate from the runtime — the
  checkpoint namespace is byte-stable across a resume and unique per step, which is exactly what
  positional dedup needs.
- On the first superstep the node sends and the graph pauses at the `interrupt()`. On resume the node
  **re-runs** — but the guarded `send_email` finds its record and replays, so the customer is emailed
  once.
- Remove Sakrit and the identical graph sends **twice**. That "before/after" is the demo.

!!! note "This file is tested"
    Executed verbatim in CI under the `langgraph` extra. See
    `examples/quickstarts/langgraph_quickstart.py`.
