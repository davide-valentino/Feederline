# Playwright MCP — reusable patterns for this workflow

These rules apply to every Playwright MCP session, not just trademark screens.
They exist because the naive approach burns tokens and returns wrong data.

## Tool discovery and calling

- Discover exact tool paths/signatures with
  `search({ namespace: "playwright", limit: 30 })` inside `execute`. Never
  guess a tool name — the visible catalog is partial (12 of 25 shown).
- In Code Mode call tools as `tools.playwright.<tool>({...})`.
- Interaction tools take `target` (the ref), **not** `ref`:
  - `browser_click({ element?, target })`
  - `browser_type({ element?, target, text, submit })` — use `submit: false`
    then click Search separately, so you can set other form fields first.
  - `browser_select_option({ element?, target, values: ["..."] })` — `values`
    is an **array** even for a single selection.
  - `browser_snapshot({})`, `browser_find({ text })` / `browser_find({ regex })`,
    `browser_wait_for({ time })`, `browser_navigate({ url })`,
    `browser_evaluate({ function })`.

## `execute` context isolation

- Each `execute` call runs in a **fresh sandbox**. Functions and variables from
  a previous call are gone. Redefine any parser/helper inside every call (or
  inline it). Forgetting this throws `ReferenceError: Unknown identifier`.

## Snapshots are YAML accessibility trees

- A full `browser_snapshot()` is large. Prefer `browser_find({ regex })` for a
  cheap targeted look (e.g. `/"Your search found|No trade marks matching/`).
- When you must parse a whole results grid, snapshot once and parse in JS
  rather than re-snapshotting. See "grid parsing" below.

## Refs are per-snapshot and go stale

- Refs like `f22e44` are regenerated after every navigation / re-render / form
  submit. Re-snapshot and re-extract refs after any page change.
- Exception: on a **stable form page that does not reload**, refs stay valid
  across repeated submits. Detect which situation you're in before reusing a
  ref; when unsure, re-extract.

### Extracting refs by regex (avoids giant snapshots)

```js
const s = await tools.playwright.browser_snapshot({});
const box = s.match(/textbox "Search word\(s\)" \[ref=(\w+)\]/)?.[1];
const btn = s.match(/button "Search" \[ref=(\w+)\]/)?.[1];
```

## Navigation timeouts are usually harmless

Slow server-rendered pages (UKIPO especially) often throw:
`TimeoutError ... waiting for scheduled navigations to finish`.
The click/type **already succeeded** — only the "wait for load" gave up.

```js
try { await tools.playwright.browser_click({ element: "Search", target: btn }); }
catch (e) { /* nav timeout — fine, the submit landed */ }
await tools.playwright.browser_wait_for({ time: 5 });
// then snapshot / find
```

Do **not** re-click after a timeout — you'll double-submit or reset state.

## Waiting out challenges / async loads

- Bot-check or challenge pages (UKIPO "Security check"): `browser_wait_for({ time: 8 })`,
  then re-snapshot. They usually auto-resolve.
- Results grids render async: wait, then confirm the grid actually populated
  (look for a `rowgroup`) before parsing.

## Grid parsing (repeated result rows)

Result tables come back as repeated `rowgroup`/`row` blocks. Split once, then
regex-extract fields per block. Field layout differs per site (see site-mapping),
but the shape is the same:

```js
const s = await tools.playwright.browser_snapshot({});
const lines = s.split("\n");
const blocks = [];
let cur = null;
for (const L of lines) {
  if (L.includes("rowgroup [ref=")) { if (cur) blocks.push(cur); cur = []; }
  if (cur) cur.push(L);
}
if (cur) blocks.push(cur);

for (const b of blocks) {
  const txt = b.join("\n");
  // adjust these three regexes per site:
  const name   = txt.match(/link "([^"]+)" \[ref=\w+\] \[cursor=pointer\]:\s*\n\s*- \/url: "#\/tmview\/detail\//)?.[1];
  const cls    = txt.match(/paragraph \[ref=\w+\]: "([\d, ]+)"/)?.[1] || "";
  const status = txt.match(/gridcell "(Registered|Expired|Pending|Filed|Dead|Cancelled|Rejected|Lapsed)"/)?.[1] || "";
  console.log(`${name} | cls ${cls} | ${status}`);
}
```

Tune the three regexes to the site's row markup. For UKIPO, values sit on the
line *after* their `"Label:"` marker at the same indent, so read `lines[i+1]`
when you see `"Mark text:"` / `"Classes:"`.

## Cheap targeted reads

- `browser_evaluate({ function: "() => document.querySelector('main').innerText" })`
  reads a region without a full snapshot — slice to what you need.
- `browser_find({ regex })` returns only matching snippets + refs — the best
  tool for "did it find anything / how many / which status".
