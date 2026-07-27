# What Sakrit is not

Sakrit does one thing — guarantee that a side effect happens exactly once, or tells you it couldn't.
It's easy to mistake that for adjacent tools that solve *different* problems. Here's where the lines
are, stated plainly.

## Not a durable-execution engine (it complements them)

Temporal, Restate, Inngest, DBOS and friends make your **code** survive crashes: they checkpoint a
workflow and replay it deterministically so it runs to completion. That's real and valuable — and it's
a different guarantee. They certify **that your code ran**; Sakrit certifies **that the world changed
once**.

A durable workflow that replays a step will happily re-execute a non-idempotent side effect inside it
unless *you* made that effect idempotent — which is exactly the gap Sakrit fills. So these engines are
**adapter targets, not competitors**: a durable-execution runtime supplies a stable execution
coordinate, which is precisely what Sakrit's positional identity needs. Run both — the engine keeps
your workflow alive; Sakrit keeps its effects exactly-once.

## Not a tracing / observability tool (it links out to them)

LangSmith, OpenTelemetry, and the like answer *"what happened, and how slow was it?"* Sakrit answers
*"did this effect happen exactly once, and if we can't be sure, who needs to look?"* They're
complementary: a trace shows you the run; Sakrit's record shows you the *effects* of the run and their
settlement state. Sakrit reserves a `trace` field to **link out** to your tracing system — it never
tries to render traces itself.

## Not a policy engine or a circuit breaker

Sakrit will never silently allow or deny an effect based on inferred rules, rate anomalies, or a
model's suggestion. Guessing a verdict *is* guessing with extra steps. Where a human decision is
required, Sakrit **surfaces** it and waits; it does not invent one. Anomaly-based auto-blocking is
refused, permanently.

## Adapter status, honestly

Sakrit ships adapters where a framework exposes a **stable, byte-identical execution coordinate across
re-execution** — that's the bar, and it's a correctness bar, not a popularity one.

- **LangGraph** — supported. The checkpoint namespace is byte-stable across resume and unique per step.
- **OpenAI Agents SDK** — supported, with an explicit run scope you supply (the SDK's call id is stable,
  but scope must be your persisted run identity, not the ambient trace).
- **Plain Python** — supported via an explicit business `key=` or `sk.step(...)`. No framework needed.
- **CrewAI** — **not shipped.** Our evaluation didn't find a coordinate that is stable *and* unique per
  step across CrewAI's re-execution model at the bar Sakrit's guarantee requires. Shipping an adapter
  that dedups incorrectly would be worse than shipping none — it would look guarded while silently
  failing. We publish the non-ship rather than ship a guess; if CrewAI exposes (or we find) a
  conforming coordinate, the verdict changes. Until then, use the plain-Python path with an explicit
  `key=`.

The bar is the same for every framework: it must pass the adapter conformance gate — a coordinate
byte-stable across a resume and unique per logical step — or it doesn't ship.
