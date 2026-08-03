# Sakrit landing + waitlist page

The single, self-contained launch page — the only cloud-facing artifact in Phase 0. It converts launch
attention into design-partner / waitlist candidates. Pure static HTML/CSS with a little vanilla JS; no
build step, no dependencies.

## Preview locally

```bash
# any static server works
python -m http.server -d landing 8000
# then open http://localhost:8000
```

## Deploy

It's a single static file — deploy `landing/` anywhere:

- **Vercel:** `vercel deploy landing --prod` (or point a project's root at `landing/`).
- **GitHub Pages / Netlify / Cloudflare Pages:** publish the `landing/` directory.

## Before launch — to wire (founder)

> **Tracked in [`../docs/ui/launch-checklist.md`](../docs/ui/launch-checklist.md)** — that file is the
> canonical, checkbox-tracked list. Keep it in sync with this section.

1. **The waitlist form (decision C-8 — Formspree, wired).** The form does a **real AJAX POST** to
   Formspree with success/error states (no fake confirmation, no silently dropped lead). **One step to
   go live:** create a form at [formspree.io](https://formspree.io) and replace `REPLACE_WITH_FORM_ID`
   in the form's `action` with your form ID. Until then, a submit resolves to the honest error path by
   design. No JS changes needed.
2. **`og:image` absolute URL.** The share image ships at `landing/og.png` and the tags are wired, but
   `og:image` / `twitter:image` use a `REPLACE-ME.example` host — crawlers (Slack, X, LinkedIn) need an
   **absolute** URL. Swap the host for the real domain once it's set; the file serves at `/og.png`.
3. **Docs links — deferred (GitHub Pages billing).** Not publishing docs to Pages yet (it charges);
   founder will set it up later. Page links stay honestly labeled **GitHub ↗** / **README** until then;
   add a **Docs** link (nav + footer) pointing at the live `site_url` once a docs host is up.

### Regenerate the OG image

`og.png` (1200×630) is rendered from `og-card.html`, which reuses the page's brand + self-hosted fonts.
Edit the card, then re-render with headless Chrome:

```bash
cd landing
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --headless=new --hide-scrollbars --window-size=1200,630 --virtual-time-budget=2500 \
  --screenshot="$(pwd)/og.png" "file://$(pwd)/og-card.html"
```

## Notes

- No analytics, no trackers, no external JS — consistent with Sakrit's no-telemetry posture (the page
   makes **zero third-party requests** — the fonts are self-hosted (latin-subset woff2 under
   `fonts/`, wired via `fonts.css`), so a page that says "no telemetry" doesn't itself phone out).
- Respects `prefers-reduced-motion`.
- The receipt figures match the real demo (`examples/money_agent/`): order #4471, $49.99, naive → 2
  charges, guarded → 1.
- The **Examples gallery** (`#examples`) is a tabbed, auto-advancing showcase of four real,
  CI-executed scripts — the money demo, the six-worker race (`examples/multi_worker/`), the
  honest-halt / ambiguity flow (`examples/ambiguous/`), and the plain-Python email quickstart
  (`examples/quickstarts/plain_quickstart.py`). Tabs are accessible (real `role="tablist"`, arrow-key
  roving focus), auto-advance pauses on hover/focus/off-screen, and it's no-JS safe (first panel
  shows by default). Keep the code snippets and printed outputs in sync with those scripts.

## Positioning (see `docs/ui/landing-content-review.md`)

The page was restructured off that content review. The message hierarchy is now: **the gap**
(hero — "Your agent resumed. The world didn't rewind.") → **the proof** (receipts) → **try it**
(`#start` — `pip install sakrit` + a 5-line snippet) → **what you get** (pillars, honesty first) →
**objections** (checkpointing / idempotency keys / retries) → **breadth** (examples) → **evidence
strip** → **design partners** (waitlist). Two deliberate rules to preserve if you edit copy:

- **The funnel is split and honest.** The library is open (`pip install sakrit`, primary CTA "Get
  started"); the waitlist gates only the *design-partner program + hosted review queue*, never "the
  library." Don't reintroduce "early access to the library" — it contradicts Apache-2.0.
- **Honesty leads.** The headline promise is "exactly once — **or it tells you**," not a flat
  "exactly once." Money is framed as one use case, not the product.
