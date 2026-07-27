# Versioning &amp; Stability Policy

Sakrit is load-bearing infrastructure: other systems depend on it to guarantee that real-world
effects happen exactly once. That role only works if the contract is predictable. This document
states exactly what we promise to keep stable, what we don't, and how we change things when we must.

## Versioning

Sakrit follows [Semantic Versioning 2.0.0](https://semver.org/). Given `MAJOR.MINOR.PATCH`:

- **PATCH** — bug fixes and internal changes that don't affect the public contract.
- **MINOR** — new, backward-compatible surface (a new adapter, a new optional field, a new CLI flag).
- **MAJOR** — a breaking change to the public contract.

### Pre-1.0 (the 0.x series)

We are pre-1.0. Under SemVer, 0.x makes no general backward-compatibility guarantee, and we keep that
escape hatch honestly: **a 0.MINOR bump may contain a breaking change** while we harden the design
against real usage. When it does, the release notes and CHANGELOG will call it out explicitly, and —
where the break touches persisted state — a forward migration will be provided (see promise 1).

**But three things are stable from 0.1 regardless of the 0.x escape hatch.** These are the promises
buyers and integrators can build on today, because breaking them silently would break exactly the
trust Sakrit exists to earn:

## The three stability promises (in force from 0.1)

### 1. Ledger on-disk format — forward-migratable, never silently broken

Your ledger is the durable record of what has and hasn't happened — it is the moat, and often the
only copy of that evidence. We promise:

- The on-disk schema is **versioned** (`schema_version` in the ledger), and any release that changes
  it ships the **forward migration** with it. Opening an older ledger with a newer Sakrit upgrades it
  in place; it never requires a manual dump/reload and never silently misreads old rows. (Today the
  machinery is a version stamp, a refuse-to-open-newer guard, and an additive-column shim; a
  general in-place migration runner arrives with the first release that needs one.)
- We will **never** ship a change that makes an existing ledger unreadable without a migration path.
- No automatic destructive operation on your data: Sakrit does not auto-delete effect rows, and any
  future garbage-collection refuses to remove unshipped or non-terminal rows by construction.

### 2. SED v1 — the tool-declaration format is a stable contract

The Sakrit Effect Declaration (SED) format — how you declare a tool's identity/content argument
classes, ladder level, provider key, and related metadata — is versioned. We promise:

- **SED v1 declarations keep working.** Additions to the format are additive and carry a version;
  they do not change the meaning of an existing v1 declaration.
- Validation is **fail-closed**: an unknown or malformed field is refused loudly at declaration time,
  never silently ignored in a way that would weaken the guarantee.
- A format change that isn't purely additive is a new SED major version, announced as such.

### 3. Public names — decorators, adapters, and CLI

The names you type are part of the contract. Stable from 0.1:

- **Public API names**: `Sakrit`, `SqliteLedger`, `EffectDecl`, `ArgClass`, the `@sk.effect`
  decorator and `guard()` / `sk.step()` entry points, and the documented adapter classes
  (`LangGraphAdapter`, the OpenAI Agents adapter). Renames go through deprecation, not a silent swap.
- **CLI surface**: the `sakrit` command and its subcommands (`doctor`, `audit`, …), their documented
  flags, and — importantly for CI consumers — the **doctor exit-code contract** and the
  **`--format json` / `--format sarif` output shapes**. These are consumed by other people's
  pipelines; we treat their shapes as frozen interfaces and version them like any other contract.

## What is *not* covered

- Anything under an underscore, in an `internal`/`_impl` module, or documented as experimental.
- Exact log/message wording (not for machine consumption; the structured surfaces above are).
- The Sakrit Cloud service, which does not exist yet — it will get its own compatibility statement
  when it ships.
- Behavior under configurations we explicitly refuse or mark unsupported.

## How we make breaking changes when we must

1. Announce it in the CHANGELOG and release notes with a migration note.
2. For persisted state, ship the forward migration in the same release (promise 1).
3. For public names, prefer a deprecation window (keep the old name working, warn, remove in a later
   MAJOR — or, pre-1.0, a clearly-announced 0.MINOR) over a silent rename.
4. Never break a promise above *silently*. "Never silent" is the product's spine; it applies to our
   own release process too.
