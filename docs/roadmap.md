# Sakrit

*Last updated: Sunday, July 26, 2026*

**Exactly-once effects for AI agents.**

Sakrit is a thin, framework-agnostic layer that sits between an AI agent and the tools it calls, guaranteeing that every action with real-world consequences — sending an email, charging a card, writing to a database — happens **exactly once**, even when the agent crashes, resumes, retries, or explores several plans in parallel.

This document is the execution plan. It's the map we're building from, and it's meant to be read by anyone joining the effort: engineers, design partners, and anyone deciding whether to depend on us.

---

## The problem, in one paragraph

Agent frameworks solved *checkpointing* — saving progress so a crashed run can resume. They did **not** solve *idempotent side effects*. When an agent resumes from a save point, it walks back to steps it already completed and, having no memory that it did them, does them again. The save point rewinds the agent but not the world: the email you already sent is still sent, the card you already charged is still charged. Every major framework's own documentation confirms this and pushes the burden onto the developer. The result in production is duplicate emails, double charges, duplicate tickets, and corrupted shared state. Sakrit closes that gap.

---

## How to read this plan

**There are no dates in this document, and that is deliberate.** The work is gated on *outcomes*, not on a calendar. Each Act ends with a **Gate** — a specific, checkable condition — and we do not move to the next Act until the gate is true. Several Acts also carry a **Stop-if** condition: a signal that would tell us the project should be killed or rethought. The gates exist as much to end the project early, cheaply, if it deserves ending, as they do to sequence the work.

Read each Act top to bottom. Do not start a later Act to "get ahead" — the gates encode real dependencies, and skipping one is how this kind of project quietly fails.

---

## Act I — Prove the problem before we write the fix

Before a single line of the library exists, we establish that the pain is real, specific, and expensive to the people who have it. This Act is cheap and it protects us from building something nobody needs.

1. **Reproduce the failure ourselves.** Build a minimal agent with a `send_email` tool and a human-approval step. Crash it mid-run. Watch it send the email twice. Record the screen. If this takes more than an afternoon, our core thesis is weaker than the evidence suggests — and we need to know that on day one, not month three.

2. **Go to the people who already filed the bug.** There are open tickets describing this exact failure. Comment on them, message the authors, ask one question and listen: *what did this cost you?* We are looking for concrete, painful, dollars-and-hours answers.

3. **Interview practitioners running agents that touch real systems.** Aim for ten teams. The answer we most want to hear is "we hand-rolled our own deduplication and it's fragile" — a team who has already validated the problem and built our competitor, badly, is the strongest possible signal.

4. **Fix the ownership structure now, not later.** If we want this to be adoptable *and* acquirable, the intellectual property has to be clean from the start: an open-source license with a contributor agreement in place before the first outside contribution. Retrofitting that onto dozens of contributors later is a common way acquisitions die in diligence — and it's invisible right up until it's fatal.

**Gate:** We hold at least five specific war stories, and we've found at least two hand-rolled partial fixes in the wild.

**Stop if:** Teams tell us "we just don't retry" — and mean it. That would mean the pain is being routed around rather than suffered, and the market is smaller than it looks.

---

## Act II — Design the contract, then build the smallest thing that honors it

Now we build — but only the narrowest possible core, and only after the developer experience is settled on paper. The whole product *is* the ergonomics; if adopting Sakrit isn't nearly effortless, it won't be adopted.

5. **Write the README before the code.** Describe the finished experience first. Then hand that README to someone outside the team. If they don't say "I need this," we rewrite the README — not the code — until they do.

6. **Settle the hard interface questions on paper.** How is an action's identity determined — derived automatically, or declared by the developer? What happens to a tool that wasn't wrapped: a safe default, or a loud failure? What, precisely, counts as "the same action"? These decisions become permanent the moment people depend on them, so we make them deliberately and up front.

7. **Build the core, and keep it narrow.** Just three things: assigning each action a unique identity, a durable record of what's been done (backed by a simple local store to start, then a production database), and the rule that re-running a completed action returns its saved result instead of firing again. **One framework adapter to begin with, not three.**

8. **Ship the demo.** Thirty seconds, start to finish: the agent sends the email twice; we add three lines; it sends once. This single asset is simultaneously our clearest marketing, our integration test, and our proof to ourselves that the core works.

**Gate:** Every war story from Act I is now demonstrably preventable with Sakrit.

---

## Act III — Survive reality

This is the Act that actually wins the project, and it's where the published research on this problem stopped: the leading academic prototype guarantees exactly-once behavior only within a single process's lifetime and explicitly leaves crash safety as future work. Everything below is the distance between a paper and infrastructure people trust with real money. We take our time here and we do not cut it short.

9. **Solve the dual-write problem.** The effect fires, and *then* the record of it fails to save. Now we've sent the email and have no memory of having done so — the exact failure we exist to prevent, reintroduced one level down. This is the oldest and nastiest problem in distributed systems, and it is the first thing any serious engineer will probe. We need a real answer — writing down the *intention* before acting, then reconciling on recovery — or we are a toy.

10. **Break it on purpose, everywhere.** Kill the process at every boundary — before the effect, during it, after it, before the record saves, after. Assert that exactly-once holds through all of them. Publish the results; the chaos-test suite is itself a credibility artifact.

11. **Handle contention.** Two parallel branches racing on the same action. Two agents reaching for the same resource at the same time. These must resolve correctly, not corrupt each other.

12. **Add the holding step for irreversible actions.** For the riskiest effects, don't fire immediately — hold them, and release only on commit or discard on abort. When an agent explores two plans in parallel and one loses, the losing branch releases nothing. It never touched the world at all.

**Gate:** We can kill Sakrit at any point in its execution and the exactly-once guarantee still holds.

**Do not launch until this gate is true.** A duplicate-charge bug in a library whose entire purpose is preventing duplicate charges is not a bug we recover from reputationally.

---

## Act IV — Become load-bearing

The library works and it survives failure. This Act turns a good library into something people structurally cannot remove.

13. **Design partners before a public launch.** Take the practitioners we interviewed in Act I and get a handful of them running Sakrit for real. Then wait — until at least one of them hits a genuine production crash and Sakrit holds. *That* is our launch story, and it's worth far more than any announcement we could write ourselves.

14. **Widen the surface so we live inside other stacks.** Add adapters for the other major agent frameworks and for plain tool-calling. The goal is to be a *dependency, not a destination* — the thin layer everyone imports without thinking about it. This is precisely how the successful infrastructure projects in this space won: by being imported, not visited.

15. **Publish the format as its own specification.** The way a tool declares itself safe-to-retry — the stamp and effect tag — should be a small, versioned standard, documented independently of our implementation. Code gets forked; formats get adopted. This is the line between shipping a library and becoming infrastructure.

16. **Ship the continuous-integration check.** A test that fails a build when it finds a consequential tool that isn't guarded by Sakrit. Once we're in a team's build pipeline, removing us requires a meeting — and that is exactly the position we want to be in.

17. **Launch in public.** The developer forums, the demo, and one deep technical write-up on how we solved the dual-write problem. That write-up is our credibility artifact — the thing that makes senior engineers decide to trust us.

**Gate:** Someone who isn't us implements our format.

---

## Act V — Make it worth buying

Sakrit is now depended upon. This Act builds the commercial layer and the acquisition logic on top of the open-source core, without ever compromising it.

18. **Build the audit trail.** Every effect Sakrit has ever settled, queryable and exportable. Regulated buyers need this for reasons entirely separate from our core thesis, which makes it the natural seam between the free core and a paid offering.

19. **Open the hosted version in private beta.** A managed, durable, multi-region record of settled effects — the part that is genuinely painful for teams to run themselves. We sell it first to the design partners who already trust us.

20. **Integrate with our future acquirers rather than pitching them.** Ship first-class integrations with the durable-execution platforms that own retries but not agent effects. Become the thing their users install to fill their gap. Acquisition conversations begin when we're already inside the account, not when we send a deck.

21. **Instrument adoption and publish it.** Downloads, production installations, and — the number that actually matters — total effects settled. That figure is the one that proves *dependency* rather than curiosity, and it's the one that makes the case for us.

---

## Who does what

With a small team, the work splits cleanly into three owners:

- **One person owns the core engine and the dual-write problem, full-time, and touches nothing else.** That is the moat, and it deserves undivided focus.
- **One person owns the adapters and the integration surface** — everything that determines whether Sakrit lives inside other people's stacks.
- **One person owns the demo, the documentation, the specification, and the channel where users report pain.** This role is the one that determines whether anyone ever finds us — and it is the one most likely to be under-resourced if we're not deliberate about it.

## The one moment that will tempt us to fail

It comes between Act III and Act IV: the pull to launch on the strength of the demo before crash safety is actually real. **We don't.** The demo earns us attention exactly once, from exactly the audience — production engineers — who will check our recovery semantics before they check anything else. We get one first impression with them, and we spend it only when the Act III gate is genuinely closed.

---

## Grounding

This plan is built on primary evidence, not vibes.

### Where the frameworks confirm the problem themselves

Every major agent and durable-execution framework documents, in its own words, that side effects will be re-run and that making them safe is the developer's job. This is not a fringe complaint — it is the vendors' own stated behavior.

**LangGraph** (the framework most agent builders use for human-in-the-loop workflows):

> "Because interrupts work by re-running the nodes they were called from, side effects called before interrupt should (ideally) be idempotent... it will be re-run multiple times when the node is resumed, potentially overwriting the initial update or creating duplicate records."
> — [LangGraph documentation, Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)

> "Absolutely avoid placing code with side effects (external API calls, database writes, sending emails, etc.) before the `interrupt(value)` function call within a node. This is a critical pitfall that can lead to unintended and potentially harmful consequences due to re-execution."
> — [LangGraph Cheatsheet, FAQs & Gotchas](https://sumanmichael.github.io/langgraph-cheatsheet/cheatsheet/faqs-gotchas/)

Open, unresolved GitHub issues confirm this is live, not theoretical:
- [`langchain-ai/langgraph` #6792](https://github.com/langchain-ai/langgraph/issues/6792) — resuming after an interrupt re-runs a completed step and duplicates its output.
- [`langchain-ai/langgraph` #6626](https://github.com/langchain-ai/langgraph/issues/6626) — parallel `interrupt()` calls generate identical IDs, breaking multi-interrupt resume.
- [`langchain-ai/langgraph` #6208](https://github.com/langchain-ai/langgraph/issues/6208) — opened by a LangGraph maintainer, still open: "Do not re-execute a node that interrupted unless all of its interrupts have been resumed."
- Related: [#4796](https://github.com/langchain-ai/langgraph/issues/4796) and [#6533](https://github.com/langchain-ai/langgraph/issues/6533).

**Temporal** (the leading durable-execution engine):

> "Without idempotence, this could cause duplicate charges in payment processing or create duplicate resources in infrastructure provisioning."
> — [Temporal Docs, Error Handling — Python SDK](https://docs.temporal.io/develop/python/best-practices/error-handling)

> "An Activity is idempotent if multiple Activity Task Executions do not change the state of the system beyond the first Activity Task Execution... Temporal recommends Activities be idempotent, or at least if they aren't, that executing an Activity more than once does not cause any unexpected or undesired side effects."
> — [Temporal Docs, Activity Definition](https://docs.temporal.io/activity-definition) and [What Is Idempotency? Why It Matters for Durable Systems](https://temporal.io/blog/idempotency-and-durable-execution)

**Dapr** (Microsoft's open-source distributed-application runtime):

> "The Dapr Workflow engine guarantees that each called activity is executed at least once as part of a workflow's execution. Because activities only guarantee at-least-once execution, it's recommended that activity logic be implemented as idempotent whenever possible."
> — [Dapr Docs, Workflow Features and Concepts](https://docs.dapr.io/developing-applications/building-blocks/workflow/workflow-features-concepts/)

**Inngest** (a widely used durable-function platform):

> "Re-running a step upon error requires its code to be idempotent, which means that running the same code multiple times won't have any side effect. For example, a step inserting a new user to the database is not idempotent while a step upserting a user is."
> — [Inngest Documentation, Errors & Retries](https://www.inngest.com/docs/guides/error-handling)

**Google Cloud Workflows**, on the same problem from a different vendor entirely:

> "Exactly-once request processing is a hard problem... Workflow handlers should be idempotent."
> — [Google Cloud Blog, Using single-execution calls with Workflows](https://cloud.google.com/blog/products/application-development/using-single-execution-calls-with-workflows)

The pattern across five independent vendors is identical: the platform guarantees the step will run again, and pushes the responsibility for making that safe entirely onto the developer, tool by tool, by hand. None of them ship a general answer. That gap is Sakrit.

### The research

- **Atomix** (arXiv 2602.14849) and **Cordon** (arXiv 2606.17573) — the closest academic work to this problem. Both demonstrate it quantitatively (naive retry-and-rollback *increased* duplicate sends in testing, rather than preventing them) and both stop at the edge of durable, cross-process crash safety — exactly where Sakrit's Act III begins.

### The pattern this plan follows

- The traction model — become a dependency other stacks import, not a destination people visit — is drawn from infrastructure projects that won this way rather than by standing alone.

---

*Sakrit — exactly-once effects for AI agents.*
