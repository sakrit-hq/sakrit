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

## Before launch — two things to wire (founder)

1. **The waitlist form (decision C-8).** `index.html` currently shows a *client-side* confirmation and
   does **not** store the address — there's a clearly-marked `TODO(founder)` at the form. Pick one:
   - **A form service** (Formspree, Buttondown, ConvertKit): set the form's `action` to your endpoint
     and delete the JS submit interceptor at the bottom of the file.
   - **A serverless function** (e.g. a Vercel `/api/waitlist` route): POST the email there and store it.
2. **Links.** The nav/footer "Docs" links point at the `sakrit-hq/sakrit` GitHub repo for now —
   repoint them at the live docs site once Pages is published.

## Notes

- No analytics, no trackers, no external JS — consistent with Sakrit's no-telemetry posture (the page
   makes **zero third-party requests** — the fonts are self-hosted (latin-subset woff2 under
   `fonts/`, wired via `fonts.css`), so a page that says "no telemetry" doesn't itself phone out).
- Respects `prefers-reduced-motion`.
- The receipt figures match the real demo (`examples/money_agent/`): order #4471, $49.99, naive → 2
  charges, guarded → 1.
