# Quickstart — OpenAI Agents SDK

An approval-gated function tool re-runs when a saved run state is resumed. Sakrit makes the effect
inside it fire once.

## Install

```bash
pip install "sakrit[openai-agents]"
```

## The integration you write

Two pieces: an `OpenAIAgentsAdapter` on the engine, and a `tool_boundary(ctx, scope=<run id>)` around
the guarded call. The sketch below shows the shape; the **exact, tested** version is the full script
embedded further down (it can't drift — CI runs it).

```python
sk = Sakrit(SqliteLedger("sakrit.db"), secret=b"<secret>", adapter=OpenAIAgentsAdapter())

@sk.effect(SEND)
def send_email(to, subject, body):
    ...

@function_tool(needs_approval=True)
async def email_customer(ctx: ToolContext, to: str) -> str:
    with tool_boundary(ctx, scope=run_id):   # run_id: your stable, persisted run identity
        send_email(to=to, subject="Your order shipped", body="on its way")
    return "sent"
```

!!! warning "Scope must be an explicit run identity"
    The adapter uses the SDK's `tool_call_id` as the call site, but **you** must supply `scope` — a
    stable id you persist alongside the run state. Do *not* rely on the ambient trace: it varies per
    resume, which would silently defeat dedup. Pass the run id you already keep.

## The full runnable example

This version runs **offline** — a scripted fake model stands in for the LLM, so there's no API key and
no network. It proves the effect fires once across an approval interruption resumed twice:

```python
--8<-- "examples/quickstarts/openai_agents_quickstart.py"
```

Run it:

```bash
python openai_agents_quickstart.py
# fired exactly once across approval + double resume: ['customer@example.com']
```

!!! note "This file is tested"
    Executed verbatim in CI under the `openai-agents` extra. See
    `examples/quickstarts/openai_agents_quickstart.py`.
