---
name: Trademark Screening via Playwright MCP
description: Use when screening candidate brand names against the UKIPO trade mark register and EUIPO/TMview using the Playwright MCP browser tools. Covers driving these two specific sites (correct URLs, deep-link navigation, exact vs similar/fuzzy search, the UKIPO bot-check, result-grid parsing), plus the reusable Playwright MCP patterns that keep these runs token-efficient. Built for the arqade-core rebrand (target classes 9/38/42) but the mechanics generalise to any DIY trademark knockout screen.
---

# Trademark Screening via Playwright MCP

DIY trademark clearance: run candidate names through **UKIPO** (UK register)
and **TMview** (EUIPO + UK + US + 90+ offices, 143M marks), looking for exact
blockers and confusingly-similar live marks in the relevant classes.

This is **screening, not legal clearance**. It narrows the field; it does not
replace a professional knockout search or an attorney's opinion.

The two sites behave very differently and both are hostile to the naive
approach. UKIPO is a slow server-rendered app behind bot-protection; TMview is
a JS SPA whose results can be deep-linked directly. The patterns below exist
because the obvious approach (dump full snapshots, click the first ref, trust
each render, loop navigations) burns tokens and silently returns stale data.

## When to load which reference

- `references/playwright-mcp-patterns.md` — the reusable Playwright MCP rules
  (tool signatures, `execute` context isolation, navigation-timeout handling,
  stale-ref discipline, grid parsing). Load this first; it applies everywhere.
- `references/site-mapping.md` — the exact URLs, form controls, search-type
  codes, and per-site gotchas for UKIPO and TMview. Load this once you know
  *what* you're driving.

## What you need before starting

- The candidate name list.
- The target trademark classes. For a media-orchestration / broadcast-SaaS
  control plane (this repo) they are **Class 9** (software), **Class 38**
  (telecoms / streaming / broadcasting), **Class 42** (SaaS / cloud).
- A fresh Playwright session. Discover tool paths with
  `search({ namespace: "playwright" })` — never guess a tool name.

## Golden rules

1. **Two search types, always both.** An exact-match search finds direct
   blockers but is a trap on its own — founders who only search the exact name
   miss the real risk. Always follow with a **similar / fuzzy** search, because
   examiners refuse marks that are *confusingly similar*, not just identical.
2. **Filter fuzzy results to Live + relevant classes.** Fuzzy search is very
   broad (500+ hits is normal) and spans every industry. Only live marks in the
   target classes matter. Dead/expired marks and other classes are noise.
3. **Read the phonetic neighbourhood, not just the count.** A name can have
   zero exact matches yet sit inside a dense thicket of same-root marks (e.g.
   dozens of live `RELAY*` registrations in classes 9/38/42). The *composition*
   of the similar results is the signal.
4. **TMview deep-links are the efficiency win.** Skip the form; navigate
   straight to the results URL (see site-mapping). But re-fetch cleanly between
   queries or you get stale results.
5. **Never loop navigations without a reload barrier on TMview.** The SPA does
   not re-fetch on a pure hash change — a fast loop silently shows the previous
   term's results. One query per navigation, with a full reload and a wait.
6. **UKIPO's bot-check is normal — wait it out.** The first interaction may
   return a 403 "Security check" page. It auto-resolves in ~8s. `browser_wait_for`
   and continue; do not re-navigate or conclude you are blocked.
7. **Navigations time out; the action still landed.** These are slow
   server-rendered pages. When `browser_click` throws a navigation timeout,
   the click already happened — wait, then snapshot. Do **not** click again.
8. **Every `execute` call is a fresh sandbox.** Helper functions and variables
   do not persist between calls. Redefine your parser each time (or inline it).
9. **Report the trade-off, not just the verdict.** The easiest-to-register name
   is often the weakest brand (a common noun) and vice versa. Surface that
   tension with the data; don't hide it behind a single green/amber/red.

## Core loop

```
for each candidate:
  UKIPO  → exact search (Live) → similar search (Live, note relevant classes)
  TMview → deep-link exact (criteria=I) → deep-link fuzzy (criteria=F)
  parse near-misses in target classes → score risk → record named obstacles
then: rank survivors on registrability vs brand-strength, list next checks
     (Companies House, domains, professional clearance)
```

## Output the caller actually wants

For each candidate: exact-match result, fuzzy/similar near-misses *in the
target classes* (with the mark name, status, class, office), and a risk call.
Then a combined UK+EU table and an honest registrability-vs-distinctiveness
trade-off. End with concrete next steps (Companies House, domain availability,
and — for any survivor with a named class-38/42 obstacle — a professional
clearance search).
