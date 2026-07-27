# Architecture

Three ideas carry the whole design: **positional identity**, the **coordinate ladder**, and
**never-silent** failure. This page is the conceptual tour; the guarantee itself is on
[The guarantee (L0–L3)](guarantee.md).

## Positional identity, evidential fingerprint

The naïve approach derives an action's identity from its **arguments**. That cannot work for agents,
because agents *regenerate* arguments — the same intended email gets a different body on retry, so
hashing the arguments changes the key and it sends twice.

Sakrit inverts it: **identity is positional; arguments are evidence.**

```
key         = SHA-256("v3" ∥ scope ∥ call_site ∥ tool_identity ∥ occurrence)
fingerprint = HMAC(secret, canonical(identity_args))   # stored on the row, never in the key
```

- The **key** answers *"which step of which run is this?"* — it rests on the runtime's stable
  execution coordinate, not on argument bytes.
- The **fingerprint** answers *"does what I'm being asked to do match what was recorded?"* — checked
  on replay.

This flips the failure direction of *every* mistake from silent-duplicate to a **loud halt**. When a
claim lands on a row that already exists:

| Row state | Fingerprint | Action |
|---|---|---|
| `SUCCEEDED` | matches | replay the recorded result |
| `SUCCEEDED` | differs on **identity** args | raise `DivergentRetry` — never merge, never re-execute |
| `SUCCEEDED` | differs only on **content** args | replay — the action already happened; the model merely reworded it |
| non-terminal | any | normal claim flow |

Because a forgotten argument declaration can no longer change the *key* (it's positional), it cannot
mint a duplicate. The worst a mistake does is a spurious — but **loud** — `DivergentRetry` you fix by
declaring the argument.

## Identity: which args are "the same action"?

Three argument classes, declared per tool (see [SED](sed.md)):

- **identity** — divergence means a *different action* → `DivergentRetry` (e.g. `amount`,
  `customer_id`, `recipient`).
- **content** — regeneratable; divergence tolerated (e.g. `body`, `subject`).
- **volatile** — ignored entirely (timestamps, trace ids, nonces).

Undeclared arguments default to **identity** — the safe direction (a spurious loud halt, not a silent
duplicate). Rule of thumb: *a field is identity-bearing iff two calls differing only in it are two
distinct intended effects.*

!!! note "The anti-reflex guardrail"
    To stop `DivergentRetry`-fatigue from rebuilding the hole by hand, an argument that looks
    identity-typed (`money`, `*_id`, account/recipient patterns) **cannot** be reclassified as
    `content` without an explicit `force` flag that lints loudly forever. Marking a `body` as content
    is one keystroke; marking an `amount` as content is a permanent, visible confession.

## The coordinate ladder

Positional identity needs a coordinate `(scope, call_site, occurrence)` that is deterministic,
byte-stable across re-execution of the same step, unique per step within scope, and available before
the effect dispatches. It comes from the first available rung — **argument-hashing appears nowhere**:

1. **Runtime coordinate** via an adapter (the default path — LangGraph, OpenAI Agents SDK).
2. **Developer-declared step id** — `with sk.step("welcome-email", occurrence=i): ...`. You are the
   adapter; your checkpoint position is the coordinate.
3. **Explicit business key** — `key="invoice-8841-charge"`, a coordinate whose stability domain is the
   business domain itself.
4. **Refuse** — a consequential effect with no coordinate **fails loud at call time**
   (`NoCoordinateError`, naming the three ways to supply one).

Precedence when more than one is present is **key > adapter > step > ladder**: when you assert identity
directly with `key=`, that is ground truth and the adapter's positional guess must not override it.

> Where identity cannot be established, the answer is a **loud refusal**, not a wrong identity.

### Where the business key comes from — and why not the arguments

Rung 3 is worth dwelling on, because "give the effect a key" invites a wrong instinct: *derive the key
from the arguments.* Don't. The key answers **"which intent is this?"** — and the arguments can't
answer that.

- **A payload-hash key fails silently on a real repeat.** A customer legitimately buys the same $49.99
  item twice today. Both calls have byte-identical arguments but are two genuinely different charges.
  Keyed by `hash(customer, amount)`, the second is deduplicated as a "retry" of the first — the second
  order is never charged, and nothing tells you. Silent wrong behavior on a money path, the exact
  failure Sakrit exists to abolish.
- **It fails open in the other direction too.** A retry recomputes the arguments and gets a slightly
  different value (a price updated between attempts, a re-fetched rate, a timestamp). Now the retry of
  the *same* intent hashes to a *new* key, looks fresh, and executes — a double charge.

The key is derived from the **domain**, not the payload: `f"order-{order_id}-charge"`. That resolves
both cases — the same order retried reuses the key and dedups; a genuinely new order (or a deliberate
`order-8841-charge-2` for a second charge on one order) gets a new key and fires. The `IDENTITY`
arguments are not the *source* of identity; they are the **cross-check** on it: same key must mean
same identity args, so if a call arrives under an existing key with a different `amount_cents`, that's
a bug wearing a retry's clothes and settle raises `DivergentRetry` — never a silent re-charge, never a
silently-served stale result. **Key = which intent; identity args = proof it's that intent.** Same
model as a provider's own idempotency keys (Stripe's included): caller-supplied, one per logical
operation, never a payload hash.

## Framework-agnostic by construction

`import sakrit` never imports a framework. The core knows only about coordinates, keys, and durable
records; framework glue lives under `sakrit.adapters` and is imported explicitly. This boundary — the
"FakeAdapter rule," enforced in the test suite — is what lets the guarantee be framework-agnostic.

## Recovery

On startup or resume, a recovery scan resolves every non-terminal row **per its ladder level**: L2/L3
re-dispatch (the provider deduplicates), L1 reconciles ("did this happen?"), and L0 becomes
`AMBIGUOUS` — a loud halt, surfaced for a human or a late piece of evidence. See
[Assurance](assurance.md) for the crash-boundary evidence.
