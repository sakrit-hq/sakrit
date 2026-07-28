# Security Policy

Sakrit aims to be load-bearing infrastructure — a library other stacks depend on to guarantee that
real-world effects (charges, emails, database writes) happen exactly once. A security issue in that
guarantee is serious, and we would rather hear about it from you than from an incident. Thank you for
helping keep Sakrit and its users safe.

## Reporting a vulnerability

**Please report vulnerabilities privately — do not open a public issue for a security problem.**

Preferred channel: **[GitHub Private Vulnerability Reporting](https://github.com/sakrit-hq/sakrit/security/advisories/new)**
(the "Report a vulnerability" button on the repository's Security tab). This keeps the report,
discussion, and fix private until a coordinated disclosure, and requires no email exchange.

<!-- TODO before public launch (phase0 C-5): confirm a monitored security contact address and list
     it here as a secondary channel, e.g. security@<domain>. Until then, GitHub Private Vulnerability
     Reporting above is the sole channel — enable it in repo Settings → Security → Advisories. -->

When you report, please include as much as you can:

- a description of the issue and the impact you believe it has (e.g. a path to a silent duplicate or
  a lost effect — the two failure classes Sakrit exists to prevent);
- the version / commit affected and the environment (OS, Python version, adapter, ledger backend);
- a minimal reproduction if possible — a failing test cell in the style of `tests/chaos/` is ideal.

## Our response commitment

We are a small project and we hold ourselves to a stated, honest SLA rather than a heroic one:

- **Acknowledge** your report within **3 business days**.
- **Triage** and give you an initial assessment (severity, whether we can reproduce, likely
  timeline) within **7 business days**.
- Keep you updated as we work a fix, and coordinate public disclosure timing with you.

<!-- TODO before public launch (phase0 C-6): confirm these SLA numbers. -->

We will credit reporters who wish to be credited in the advisory and release notes. If you prefer to
remain anonymous, that is fine too.

## Scope

In scope: the `sakrit` library — the exactly-once guarantee, the ledger and its migrations, the
identity/fingerprint scheme, the adapters, the doctor, and the CLI.

Out of scope for now: the Sakrit Cloud service does not exist yet (it arrives in later roadmap
phases); this policy will be extended to cover it when it ships. Third-party frameworks Sakrit
adapts (LangGraph, OpenAI Agents SDK, etc.) should be reported to their respective projects.

## What the ledger stores (data at rest)

The SQLite ledger is your durable record of what did and didn't happen. By design it stores **no raw
argument bytes** — identity arguments are kept only as a non-reversible HMAC fingerprint. But two
things it *does* store as **plaintext**:

- **effect results** — whatever your guarded tool returns (JSON-encoded), so a replay can return it;
- **error text** — for a declared clean failure, `"<ExceptionType>: <message>"`, and exception
  messages routinely embed argument values.

So treat the ledger file (`sakrit.db`, its `-wal`/`-shm` sidecars, and the `.lock` file next to it)
with the same care as the data your tools return and receive: where it lives — laptop, shared volume,
backup set — is a data-handling decision. Nothing leaves the machine (Sakrit has no telemetry and
opens no network connection, verified by an import-time tripwire test), but the file itself is as
sensitive as its contents. Encryption-at-rest for results is a legitimate later feature, not a
preview guarantee.

## Supported versions

Sakrit is pre-1.0. Security fixes land on the latest released version. Once we reach 1.0 we will
document a support window here.
