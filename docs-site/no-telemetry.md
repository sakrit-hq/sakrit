# No telemetry, ever

**The Sakrit library phones nowhere.** No usage pings, no crash beacons, no "anonymous statistics," no
license check. Installing it and importing it makes zero network connections on its own. This is a
frozen design decision, not a current default we might quietly revisit.

## Why this is a promise, not a preference

Sakrit sits on the most sensitive path in your system — the one where money moves and messages send.
A library there that also opens its own outbound connections is a supply-chain and privacy liability,
no matter how well-intentioned the telemetry. The only trustworthy posture for infrastructure at that
position is: **it does exactly what you called, and nothing else.**

So we hold it as an invariant you can audit:

- `import sakrit` imports **no** network client and **no** framework.
- The library makes network calls **only** to endpoints you explicitly configure, only when you call
  the code that uses them.
- There is no build-time or runtime callback to us, ever.

## What about Sakrit Cloud?

A hosted layer is on the roadmap — a place a surfaced effect can land and get resolved. When it exists,
it will be **opt-in and explicit**: it activates only when you configure it, it is **metadata-only by
default** (hashes, states, coordinates — not your payloads), and the library's no-telemetry promise
above is *unchanged* by its existence. The cloud is something you point Sakrit at on purpose; it is
never something the library reaches for on its own.

If you never configure it, Sakrit is a complete, offline, account-free library — and it always will be.
