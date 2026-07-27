# The guarantee (L0–L3)

Exactly-once execution of an arbitrary side effect against a **non-cooperating** external system is
*impossible*: the effect and the record of it live in different failure domains, so a crash between
them is undecidable (the Two Generals problem). Sakrit does not pretend otherwise. Instead it tells
you, honestly and per-tool, exactly what you get — and guarantees that where it can't prevent a
duplicate, it will **surface** the ambiguity rather than guess.

The guarantee is scoped two ways, and both are **declared, never guessed**.

## By provider cooperation — the ladder

Every tool is declared onto a rung. The delivery semantics follow mechanically from what the provider
can do.

| Level | Mechanism | What you get | Requires |
|---|---|---|---|
| **L3** | same-DB transaction (classic outbox) | **true exactly-once** | the effect target *is* the ledger DB |
| **L2+R** *(recommended for money)* | provider idempotency key **+** reconcile | effectively-exactly-once, no *silent* TTL cliff | a provider key and a "did this happen?" query |
| **L2** | provider idempotency key pass-through | effectively-exactly-once **within the provider's key TTL**; loud `AMBIGUOUS` beyond it | key TTL longer than your retry horizon |
| **L1** | reconcile on recovery ("did this happen?") | effectively-exactly-once after a confirmation window | a queryable provider read, consistency declared |
| **L0** *(default, irreversible)* | write-ahead + halt on ambiguity | **at-most-once, ambiguity surfaced**, self-healing via late evidence | someone watches the resolution surface |
| **L0-AL** *(opt-in)* | write-ahead + retry | at-least-once, duplicates **counted** | duplicates are tolerable |

The **dual-write window** — a crash after the effect commits but before Sakrit records it — is why
L0's floor is *at-most-once with a loud `AMBIGUOUS`*, not exactly-once. With zero provider
cooperation, no library on earth can know whether the effect happened. Sakrit's answer there is to
**say so**, loudly, and let a human or a late piece of evidence resolve it — never to silently retry
(a possible duplicate) or silently drop (a possible lost effect).

!!! tip "Money tools want L2+R"
    A provider idempotency key alone (L2) degrades to ambiguity once the key's TTL expires — and
    human-in-the-loop approvals routinely outlast a 24h TTL. Adding a `reconcile` read (L2+R) removes
    that silent cliff: past the TTL, recovery *asks the provider* whether the charge landed instead of
    re-dispatching blind.

## By re-execution regime — the theorem

Agents don't just retry; they *regenerate*. Sakrit's identity model is sound under two regimes and
honest about the third:

- **R1 — replay** (the same step re-runs after a resume): sound. The recorded result is replayed; the
  effect does not re-fire.
- **R2 — content regeneration** (the model rewords a `content` argument on retry): sound. A reworded
  body is recognized as the *same* action.
- **R3 — plan regeneration** (the agent produces a genuinely different plan): *undecidable* in
  general. Here the degraded guarantee is **at-most-once-with-confirmation** — cross-plan collisions
  are caught and surfaced (`RegeneratedDuplicate`), never silently executed.

## What "never silent" means concretely

Every ambiguous outcome is *told*, not merely left in a state a later pass might notice:

- a transition into `AMBIGUOUS` logs a warning **and** fires an optional `on_ambiguous(key)` hook for
  your alerting/metrics;
- a mismatched retry raises a typed `DivergentRetry` at call time;
- an effect with no resolvable identity raises `NoCoordinateError` rather than inventing one.

See **[Architecture](architecture.md)** for how identity is derived, and **[Assurance](assurance.md)**
for the kill-at-every-boundary evidence behind these claims.
