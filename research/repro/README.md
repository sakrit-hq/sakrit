# repro/ — the raw reproduction

Act I, step 1: build a minimal agent with a `send_email` tool and a
human-approval step, crash it mid-run, and watch it send the email twice. Record
the screen. If this takes more than an afternoon, the core thesis is weaker than
the evidence suggests — and we want to know that on day one.

Drop the raw scripts here:

- `agent.py` — the minimal agent + `send_email` tool + approval step
- `run.py` — run it to the crash point
- `resume.py` — resume from the checkpoint (this is where the double-send happens)
- `check_outbox.py` — confirm the email went out twice

This is throwaway proof-of-problem, not product. The cleaned-up version graduates
to [`examples/send_email_langgraph/`](../../examples/send_email_langgraph/).
