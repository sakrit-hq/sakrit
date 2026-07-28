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
    crash landed in the dual-write window at L0. `audit` is read-only — it shows you the halted rows but
    cannot heal them. Once you've checked the provider and know the truth, record it out of band with
    the worker stopped:

    ```python
    from sakrit import SqliteLedger
    from sakrit.core import EffectState

    ledger = SqliteLedger("sakrit.db")
    for key in ledger.keys_in(EffectState.AMBIGUOUS):
        # For each key, check the provider, then call *one* of these:
        #   ledger.accept_late_evidence(key, failed=True)              # never ran → retryable
        #   ledger.accept_late_evidence(key, result={"id": "pi_..."})  # it ran → record; replays
        print(key)
    ```

    See the README's "Resolving an ambiguous effect" for the full walk (listing keys, the honesty rule).
