# Sakrit — private preview

*You have early access to Sakrit before it's public. Thank you — your job here is to break it and
tell us. This page gets you from zero to a finding in about two minutes. (Pre-launch artifact; it
goes away at public launch.)*

**Sakrit** makes an AI agent's side-effecting tool calls take effect **exactly once** — through
crashes, retries, and resumes — and tells you honestly when exactly-once is impossible. No account,
no telemetry, works offline.

## 1. Install (about 30 seconds)

You have read access to the private repo, so install straight from git:

```bash
pip install "git+https://github.com/nagaraju-oruganti/sakrit.git"
# framework adapters are optional extras:
pip install "sakrit[langgraph] @ git+https://github.com/nagaraju-oruganti/sakrit.git"
```

If git prompts for credentials, use a GitHub personal access token with `repo` read scope as the
password (or `gh auth login` first). No PyPI package exists yet — that lands at launch.

## 2. Scan your own agent (about 90 seconds)

Point the doctor at a repo that calls tools with real-world effects (charges, emails, DB writes). It
reads your source with `ast` — **it never imports or runs your code** — and flags consequential calls
that aren't under a Sakrit guard:

```bash
sakrit doctor .
# or machine-readable / CI:
sakrit doctor --format sarif -o sakrit.sarif .   # upload to GitHub code scanning
sakrit doctor --check .                            # exit 1 if there are findings
```

Findings look like: `app/pay.py:42:4: SAKRIT001 unguarded consequential call: stripe.PaymentIntent.create(…)`.
It's a heuristic net — false positives are cheap by design; review each and either wrap it or annotate
`# sakrit: safe`.

## 3. Guard one effect (about 30 seconds)

The whole idea, in one wrap — declare which args are *identity* (a different value = a different
action) vs *content* (the model may reword them on a retry):

```python
from sakrit import Sakrit, SqliteLedger, EffectDecl, ArgClass

sk = Sakrit(SqliteLedger("sakrit.db"), secret=b"<per-deployment-secret>")


@sk.effect(EffectDecl("email.send", {"to": ArgClass.IDENTITY, "body": ArgClass.CONTENT}))
def send_email(to, body):
    return smtp.send(to, body)


# Crash, resume, or retry: send_email fires exactly once.
```

Runnable examples to copy from: `examples/quickstarts/` (plain Python, LangGraph, OpenAI Agents SDK)
and `examples/money_agent/` (an agent that charges a card exactly once, even through a crash).

## What we'd love from you

- **Run `sakrit doctor` on your real agent** and tell us: was the signal useful? Too noisy? Miss
  anything obvious? (The false-positive rate on real app code is exactly what this preview is tuning.)
- **Wrap one real effect** and tell us where the API fought you.
- **Anything that crashes, confuses, or feels wrong.** A silent duplicate or a lost effect is our
  highest-severity class — lead with those.

## Where to report

- **Bugs / findings / questions:** open a [Discussion](https://github.com/nagaraju-oruganti/sakrit/discussions)
  or an [Issue](https://github.com/nagaraju-oruganti/sakrit/issues) on the repo.
- **Security-flavored issues:** please use private reporting (see `SECURITY.md`), not a public issue.
- **Direct line:** you also have the founder's contact from your invite — use it freely.

More depth when you want it: `README.md`, the guarantee ladder and architecture in the docs
(`docs-site/`), and the assurance write-up (how we kill our own process at every boundary).
