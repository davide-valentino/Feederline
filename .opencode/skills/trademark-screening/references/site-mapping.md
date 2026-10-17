# Site mapping — UKIPO and TMview

The exact entry points, form controls, and gotchas for the two registers.
Wasted navigation is the main cost in a screening run; these URLs skip it.

---

## UKIPO (UK register)

Free UK search. Slow server-rendered app, behind bot-protection.

### Entry points (the ones that work)

- Search results page: `https://trademarks.ipo.gov.uk/ipo-tmtext/page/Results`
- New search form: `https://trademarks.ipo.gov.uk/ipo-tmtext?reset`

Dead / misleading entry points — do **not** start here:

- `https://trademarks.ipo.gov.uk` → 302s to `gov.uk` (the informational page).
- `https://trademarks.ipo.gov.uk/ipo/fto` → 404 (retired URL).

### Navigation inside the tool

Top nav links (refs change each load):

- **New search** → `/ipo-tmtext/?reset` (clears everything)
- **Amend search** → `/ipo-tmtext` (returns the form; you land on Results after
  each search, with the form hidden — click Amend to get it back)
- **View shortlist** → `/ipo-tmtext/page/Shortlist/`

### The bot-check

The first interaction often returns HTTP 403, a "Security check" page
("When we notice a high number of visits from a single user..."). It
auto-resolves via a JS challenge in ~8 seconds. Do:
`browser_wait_for({ time: 8 })` → re-snapshot → continue. Do not re-navigate,
change IP assumptions, or treat it as a hard block.

### Form controls

Two separate "type" concepts — don't confuse them:

- **"Search by"** combobox: `Word` (default) / `Image` / `Word and Image` /
  `Word or Image`. Leave on **Word** for name screening.
- **"Search type"** combobox: `Word` / `Exact` / `Similar`.
  - `Exact` = the term matches exactly → direct blockers.
  - `Similar` = confusingly similar → the real risk. Very fuzzy (edit-distance);
    500+ results is normal. Narrow with **Status = Live** and, where possible,
    the class filter.
- **"Status"** combobox: `All` / `Live`. Use **Live** for the risk picture.
- **"Results per page"**: set to `50` to reduce paging.
- **Class filter**: a combobox widget ("Click here to select a class...").
  Fiddly to drive via the UI; easier to filter client-side after parsing.

### Reading results

After a search you land on Results; the form is gone. Result markup (YAML
accessibility tree) repeats per mark, and each value sits on the line **after**
its `"Label:"` marker at the same indent:

```
- generic: "Mark text:"
- generic: VELAR
- generic: "Classes:"
- generic: 9, 38, 42
- generic: "Status:"
- generic: Registered
```

Parse by scanning for the label line, then reading `lines[i+1]`. Useful labels:
`Mark text`, `Classes`, `Status`, `File date`. A heading like
`Your search found N marks filed between ...` gives the total count.

---

## TMview (EUIPO + UK + US + 90+ offices, ~143M marks)

JS single-page app. The big efficiency win: results are **deep-linkable**.

### Entry points

- Official tool: `https://www.tmdn.org/tmview/welcome` → loads the SPA at
  `https://www.tmdn.org/tmview/#/tmview`.
- `https://www.tmview.org.uk/` does **not** resolve — wrong domain.

Cookie banner: click **"No thanks"** to dismiss (ref varies per load).

### Deep-link navigation (skip the form)

```
https://www.tmdn.org/tmview/#/tmview/results?page=1&pageSize=30&criteria=<CODE>&basicSearch=<TERM>
```

`criteria` codes:

- `I` = **Is** (exact match)
- `F` = **Fuzzy search** (confusingly similar)
- (also `C` Contains, `W` Word, `B` Begins with, `E` Ends with — via the
  "Contains" dropdown on the search page)

### THE STALE-RESULTS GOTCHA (critical)

The SPA does **not** re-fetch on a pure hash change. Navigating between two
fuzzy deep-links in a fast loop silently returns the **first** term's results
for the second term. Symptoms: RELAYA and VELRA "fuzzy" both render FEEDERLINE
rows.

Mitigations (pick one):

- Full reload per query: `browser_navigate({ url: "about:blank" })` → wait →
  `browser_navigate({ url: <results url> })` → `browser_wait_for({ time: 6..7 })`.
- Or one query per navigation with a generous wait, and sanity-check the
  rendered rows actually match the term before trusting them.

Exact (`criteria=I`) deep-links are more reliable but apply the same caution.
A fuzzy search can also redirect to `tmdn.org/error/revise.html` if the query
is malformed or too aggressive — verify the URL after navigation.

### Reading results

Result rows use `link "NAME"` (the `mark [ref]: NAME` form appears only in some
views). Structure per `row`:

- mark-name cell → `link "NAME"` whose `/url` is `#/tmview/detail/<OFFICE><NUM>`
- date cell → `paragraph: 19/01/2026`
- class cell → `paragraph: "9, 38, 42"` (quoted, comma-separated — may be empty)
- status cell → `gridcell "Registered"` (or `Expired` / `Filed` / `Pending` / ...)
- office cell → `link "United States - USPTO"` pointing to the national office
  site (uspto, euipo, dpma, ipaustralia, gob.mx/impi, inpi, cipo, jpo, kiipo,
  cnipa, ipindia, ...). Use the office host to identify the jurisdiction.

Because class numbers come as a quoted comma list, flag relevant marks with:
`/(^|, )(9|38|42)(,|$)/.test(cls)`.

### Practical screening order per name

1. TMview exact `criteria=I` — direct blockers across all offices.
2. TMview fuzzy `criteria=F` (with a reload barrier) — near-misses.
3. UKIPO exact + similar (Live) — the UK-specific picture and class filter.
4. Intersect the two, keep only **live** marks in the **target classes**, and
   note the single closest named obstacle for each candidate.
