# SED — the Sakrit Effect Declaration format

The versioned, language-neutral format by which a tool declares how it settles exactly-once. Code gets
forked; formats get adopted — publishing SED as a standalone spec, independent of the Python
implementation, is the line between shipping a library and becoming infrastructure. The Python
decorator is **one binding** that emits and consumes SED; it is not the format.

## Why a format, not a config file

1. **Versioning that fails closed on the axis that matters.** `sed` major is breaking; minors are
   additive-optional. A consumer MUST ignore unknown *optional* fields, and MUST reject a declaration
   whose `sed` major it doesn't implement.
2. **Code is referenced, never embedded.** `reconcile` / `refetch` / custom normalizers are
   per-language refs (`python:`, `node:`). Normalizers with *cross-language semantics* are builtin by
   name so fingerprints agree across a Python agent and a TypeScript agent guarding the same tool.
3. **Carriage everywhere.** Standalone `.sed.yaml` files, the Python decorator, and — the strategic
   one — **MCP tool manifests via `_meta.sakrit`**: an MCP server shipping its own SED means any
   SED-aware client gets exactly-once guarding with zero local configuration.

## The v1 document

```yaml
sed: 1                                  # spec major version
tool: crm.create_ticket                 # stable logical identity (decoupled from fn name)
level: l2r                              # l0am | l0al | l1 | l2 | l2r | l3
args:
  customer_id: { class: identity, normalize: trim }
  amount_cents: { class: identity, type: money }
  subject:     { class: identity, normalize: nfc-trim }
  body:        { class: content }
  request_ts:  { class: volatile }
  "*":         { class: identity }      # explicit default for undeclared args
provider_key:
  param: idempotency_key
  ttl_s: 86400
  encoding: hex64                       # fit the provider's charset/length
reconcile:
  ref: "python:myapp.recon:find_ticket"
  window_s: 30
  provider_read: eventual               # eventual | strong  (drives the on-absent default)
result:
  replay: value                         # value | marker | refetch
  refetch: { ref: "python:myapp.recon:refresh_status", fields: [status] }
ambiguous:
  default: halt                         # halt | retry
  escalate_after_s: 604800
fingerprint: { version: 1 }
```

## Field reference

### `tool` — logical identity

A stable logical name (`crm.create_ticket`), decoupled from the function name, so renames are free. A
semantic version change participates in the *fingerprint*, not the key — so a v1→v2 retry of the same
step trips `DivergentRetry` (you *should* look before replaying across a semantics change).

### `level` — the provider-cooperation rung

`l0am` (at-most-once, default irreversible) · `l0al` (at-least-once, opt-in) · `l1` (reconcile) · `l2`
(provider key) · `l2r` (**recommended for money**: key + reconcile) · `l3` (same-DB transaction). Full
semantics on [The guarantee (L0–L3)](guarantee.md).

### `args` — the identity classification

Each argument is `identity` (divergence → `DivergentRetry`), `content` (regeneratable; divergence
tolerated), or `volatile` (ignored). `"*"` sets the default for undeclared args (itself defaulting to
`identity` — the safe direction).

- **`normalize`** — a builtin normalizer applied before fingerprinting.
- **`type`** — a semantic type; `money`, `*_id`, and recipient/account patterns are *identity-typed*:
  classing one as `content` requires an explicit `content: { force: true }` that lints loudly forever
  (the anti-reflex guardrail).

### Builtin normalizers (cross-language, byte-identical by spec)

`trim` · `nfc-trim` · `email` · `phone-e164` · `url-canonical` · `money`. These MUST produce identical
bytes across every language binding — a Python `email` normalizer and a Node `email` normalizer agree
— or a shared fingerprint is meaningless. The builtins carry their own cross-language conformance suite
(a normative golden-vector file). Byte-level (no normalization) is the default.

### `provider_key` — L2/L2R injection

`param` (the provider's idempotency parameter name), `ttl_s` (the provider's key TTL — drives the
L2→L1 degradation past expiry), `encoding` (fit the provider's charset/length constraint).

### `reconcile` — L1/L2R recovery

`ref` (a language-bound code reference returning `SETTLED(record) | ABSENT | UNKNOWN`), `window_s`
(confirmation window), `provider_read` (`eventual | strong`; `strong` is required before an
irreversible tool may auto-retry on `ABSENT`).

### `result` — replay behavior

`replay: value | marker | refetch`; `refetch` re-reads volatile fields from the stored stable id at
replay time. Replay restores history, never the present.

### `ambiguous` — the L0 surface

`default: halt | retry` and `escalate_after_s` for time-boxed auto-escalation of an unresolved
`AMBIGUOUS` key.

### `fingerprint` — the evidence version

`version` on the fingerprint scheme, so an HMAC-secret or canonicalization change is a comparable,
migratable axis rather than a silent break.

## Versioning rules

- **`sed` major** — breaking. A consumer rejects a major it doesn't implement.
- **`sed` minor** — additive, optional. Unknown optional fields are ignored.
- **Identity-affecting changes** (arg class, normalizer, `tool` version) flow through the
  **fingerprint**, surfacing as a `DivergentRetry` a human resolves — never a silent change of dedup
  behavior.

## Trust model — a SED document can execute code

`reconcile` / `refetch` / normalizer refs are resolved per language — the Python binding resolves
`python:<module>:<attr>` by **importing the named module**, which is arbitrary code execution. That
is the standard trust model for developer-authored config (pytest plugins, `setup.py`), and the
refusal behavior around a bad ref is careful — but because SED is a *shareable, adopted* format, say
it plainly: **a SED document is code-equivalent; load only declarations you trust.** A consumer that
must load untrusted documents should resolve refs against an allow-list (the Python
`load_sed(resolve_ref=…)` parameter exists for exactly this) rather than importing by name.

## What does not belong here

Implementation detail — the ledger, the state machine, the claim protocol. SED is the *format* a tool
uses to declare itself, not how Sakrit honors it. That's [Architecture](architecture.md) and the code.
