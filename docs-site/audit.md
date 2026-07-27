# `sakrit audit`

Query and export the settled-effect history from a ledger — every row with its provenance. It is
**read-only by construction**: the ledger is opened `mode=ro`, so the command cannot mutate it.

## Run it

```bash
# export the whole history as JSON
sakrit audit sakrit.db

# filter, and write CSV to a file
sakrit audit sakrit.db --tool email.send --state SUCCEEDED --format csv -o history.csv
```

## Filters

| Flag | Meaning |
|---|---|
| `--scope` | exact scope |
| `--tool` | exact tool name |
| `--state` | effect state (e.g. `SUCCEEDED`, `AMBIGUOUS`, `FAILED`) |
| `--since` / `--until` | `created_at` window (UTC ISO-8601; `--until` is exclusive) |
| `--limit` | cap the number of rows |
| `--format` | `json` (default) or `csv` |
| `-o` / `--output` | write to a file (default: stdout) |

Each row carries its **provenance** — the key and fingerprint scheme versions and the secret id — so a
ledger stays interpretable across scheme migrations. The ledger is your durable record of what did and
didn't happen; the audit command is how you read it.

!!! info "Investigating an `AMBIGUOUS`"
    Filter `--state AMBIGUOUS` to list effects Sakrit halted on rather than guessed. Each is a place a
    crash landed in the dual-write window at L0 — resolve it with real evidence (the effect either did
    or didn't happen), and the row heals.
