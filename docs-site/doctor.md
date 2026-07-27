# `sakrit doctor`

A static net for the "forgot to wrap" mistake. It scans your Python source with `ast` — **the code is
never imported or executed** — for consequential-looking calls that aren't lexically under a Sakrit
guard, and reports them. It's a heuristic, not a runtime guarantee: false positives are cheap by
design — review the call and either wrap it or annotate it and move on.

## Run it

```bash
# zero config, scans the current directory
pipx run sakrit doctor .

# fail a CI build if there are any findings
sakrit doctor --check .

# machine-readable output
sakrit doctor --format json .
sakrit doctor --format sarif -o sakrit.sarif .
```

`--format sarif` produces a SARIF 2.1.0 log you can upload to GitHub code scanning, so findings appear
as annotations right in a pull request. Run doctor with **repo-relative paths** (or a path under the
repo root) so the SARIF/JSON locations map to your files — an absolute path under the working directory
is relativized for you.

!!! note "Column numbering"
    JSON and the text output report a **0-based** column (matching Python's AST offset); SARIF reports
    a **1-based** `startColumn` (as the SARIF spec requires). If you convert a JSON `col` for a
    1-based editor, add 1.

## Exit codes (a stable contract)

| Exit | Meaning |
|---|---|
| `0` | Clean, **or** findings present without `--check` (report mode still prints them) |
| `1` | Findings present **and** `--check` was given (the CI gate fails) |
| `2` | Usage / argument error |

`--check` affects only the exit code, never the output. These codes and the JSON/SARIF shapes are a
frozen interface — CI consumers can depend on them.

## The rules

### `SAKRIT001` — unguarded consequential call

A call that looks like it changes the outside world sits outside any Sakrit guard. The catalog is
deliberately explicit — each entry is a shape the scanner can resolve *within one file*:

- HTTP mutations: `requests`/`httpx` `post`/`put`/`patch`/`delete` (and `request("POST", …)`), on the
  module or a client/session instance;
- SMTP sends: `smtplib` `sendmail` / `send_message`;
- Stripe mutations: `create`/`modify`/`delete`/`cancel`/`capture`/`confirm`/`pay`/`refund`;
- boto3 mutating verbs: `send_*`/`create_*`/`delete_*`/`put_*`/`update_*`/… plus `publish`/`invoke`;
- write-SQL `execute`/`executemany` (`INSERT`/`UPDATE`/`DELETE`/…).

**A call is considered covered** when it sits lexically inside a function that is (a) decorated with a
call-shaped `@…effect(…)`, (b) passed by name to `…guard(…)`/`…guard_async(…)` in the same file when
that name is unambiguous, or (c) marked safe (below). Coverage is purely lexical and per-file — a
helper *called from* a guarded function is still flagged; annotate it after review.

### `SAKRIT000` — file not verified

A file that could not be parsed, decoded, or found is reported as a loud finding, never a silent skip.
"Not verified" is different from "verified clean," and the doctor refuses to blur them — the same
fail-closed discipline as the library.

## Suppressing a reviewed finding

Suppression markers are **real comments only** (a marker inside a string literal never suppresses):

```python
resp = requests.post(url, json=body)  # sakrit: safe   ← reviewed, deliberately unguarded

# or, for a whole module that IS effect machinery (a ledger, a migration runner):
# sakrit: safe-file
```

You can also decorate a reviewed function with `@sakrit.safe` — runtime-inert, consumed only by the
doctor.

!!! tip "Tune the noise floor"
    On a repository that vendors a framework's persistence layer, the write-SQL rule can flag the
    framework's *own* checkpointer internals. That's correct-but-noisy; the intended fix is a single
    `# sakrit: safe-file` on those machinery modules — exactly what Sakrit's own ledger does.
