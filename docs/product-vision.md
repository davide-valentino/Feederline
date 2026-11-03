# Feederline — Product Vision

> Status: draft (record of intent, for review)
> Date: 2026-11-03
> Scope: product direction, not implementation

## One line

**Feederline is a schedule-first control plane for live delivery.** Operations declare
*what* to deliver (content → outputs, when, how resilient) and Feederline resolves it into
a plan and runs it — without exposing cloud or network primitives to the customer.

## Who it is for

Broadcast **operations and traffic** people — the team that knows *what* goes to air and
*when*, but not *how to wire the transport*. It is explicitly **not** aimed at network
engineers (VideoIPath), cloud DevOps (AWS console), or live production staff (switchers,
graphics).

## Competitive positioning

The incumbents sell engineering power; the open space is intent.

| Competitor | What they sell | Feederline's position |
| --- | --- | --- |
| Grass Valley AMPP | App-store grid of live-production tools | We don't do production/switching; we schedule delivery. |
| Nevion VideoIPath | SDN orchestration, topology, bandwidth, gear | We hide the network; capability-driven declarations instead. |
| AWS MediaConnect | Raw cloud transport primitives (flows, IAM, regions) | We orchestrate *on top of* an engine and hide the primitives. |

Three anchors that survive the competitive reality-check:

1. **Route is the single scheduling object.** AMPP is app/trigger based, VideoIPath is
   connection/salvo based, MediaConnect has no schedule (external orchestrator). None treat
   a route as a first-class, fan-out, time-boxed unit.
2. **Capabilities as data, not a type matrix.** Incumbents *sell* the primitives, so they
   structurally cannot hide them. This is our durable advantage.
3. **Operator split.** The customer sees intent and product reasons; the platform sees
   deliveries/plans/outbox. Incumbents give raw telemetry (CloudWatch, alarms), not a
   product-level boundary.

We orchestrate **on top of** an execution engine (AWS first). We never claim to replace it.

## Vocabulary

Two deliberate vocabularies, one boundary.

| Layer | Concepts |
| --- | --- |
| **Customer** | Content, Route, Connection (Feed/Output), Capability |
| **Platform (operator only)** | Delivery, Resolved plan, Outbox, Workflow run |

- **Content** — a channel or event. Identity and ownership only; no schedule. Its status is
  *derived* from active routes, never stored independently.
- **Route** — the only scheduling object: content → outputs, when, with how much resilience
  and which quality preset. Fans out to N outputs.
- **Connection** — a Feed (transport + region + encryption + capability-required params) or
  an Output (format + target + region).
- **Capability** — what is available where and what it requires, as data (region matrix,
  required fields, constraints such as "HLS requires transcode").
- **Delivery / resolved plan / outbox** — the control-plane kernel that actually executes a
  route. Platform-only, never customer-facing.

## Confirmed product decisions

Summary of the evaluation conclusions, locked for the MVP.

1. **Route is the primary scheduling object** — Content stays a metadata registry. Route
   owns schedule, fan-out, resilience and lifecycle.
2. **Five customer jobs** — monitor (control room), schedule, content, connections,
   capabilities. "Concept" is a design doc, not a shipped surface. "Operator" is a role, not
   a nav item.
3. **Capability registry removes the type matrix** — the "5 × 11 typed resources" model
   collapses into 6 transports × 6 formats described by data. No expert JSON path at MVP.
4. **Quality presets are an accepted abstraction** — passthrough / 1080p / 1080p premium /
   4K UHD replace stream profiles and rendition ladders. Custom ladders deferred.
5. **Operator split is a role boundary, not a UI toggle** — enforced server-side via the
   internal surface, with audit. Customers never see provider identifiers.
6. **Commercial concepts are out of scope (dropped)** — packages, entitlements, pricing and
   billing live outside the domain model. The resolved plan keeps a *seam* for a future
   pricing service, but no `price`/`package` fields leak into Route or Capability. The
   "review" step sells on *what will be provisioned and when*, not on a dollar figure.

## Initial feature set

### P0 — the sellable core (design partners run real routes)

1. **Content registry** — channels + events; identity/ownership only; status derived.
2. **Connections** — feeds and outputs with capability-driven fields.
3. **Routes** — wizard: content → ingest → quality → outputs → schedule → review.
   Fan-out to N outputs, resilience enum (single/redundant/geo-redundant), quality presets.
4. **Authoritative dry-run** — server-side feasibility checks + a read-only resolved plan.
   No price.
5. **Lifecycle actions** — schedule, cancel, stop, plus a stable event vocabulary
   (scheduled/started/degraded/failed/completed/stopped).
6. **Operator surface** — deliveries, resolved plans, outbox with actions (retry, stop) +
   audit, behind a role.

### P1 — what earns trust (first 6–12 weeks of feedback)

7. **Timeline/calendar view + conflict detection** — schedulers think in time; output
   contention must be visible and blocked.
8. **Route revisions / edit / reschedule / duplicate** — the scheduling unit is only real
   if changes are versioned.
9. **Capability registry as versioned data + field collection** — the wizard must actually
   collect entitlement/stream-id/whitelist/endpoint; add transports without a redeploy.
10. **RBAC operator boundary** — make `/internal/*` the authoritative internal surface with
    audit (it is unauthenticated today).

### P2 — discovery-driven, explicitly later

- Custom ABR ladders / expert raw-fields path (only if a partner demands it).
- Per-customer geo/DR scheduling policies.
- A second execution engine beyond AWS (keep the seam, don't build it).
- Pricing/entitlements (decision #6) as a standalone service.

### Non-goals for launch

- No production switching/graphics/audio (AMPP).
- No SDN, topology, bandwidth reservation, gear inventory (VideoIPath).
- No raw AWS-console-style flow/IAM config in the customer surface (MediaConnect).
- No custom transcoder tuning in MVP.
- No packages/billing (decision #6).

## The discovery loop

Sell a narrow MVP and learn fast. The Delivery state machine + transactional outbox are the
instrument: every route is a traceable event sequence.

1. **Instrument what actually happens** — which transports/formats/regions are used, where
   the dry-run rejects, where support has to intervene.
2. **Feature-flag capabilities and presets** — roll a new transport/region to one tenant
   first. This is the concrete payoff of "capabilities as data."
3. **Structured partner rhythm** — 2–3 design partners, an interview after every live event,
   one question: *"what did you have to do outside Feederline?"*
4. **Watch operator actions, not just customer clicks** — repeated support retries or
   hand-edited plans signal a missing capability or preset.

## Mapping to the current codebase

The repo is already the **kernel** of this MVP, not a throwaway prototype:

- `app/domains/deliveries.py` → the RouteRun/Delivery (observed state) and the outbox.
- `app/workflows/direct_same_region.py` + `infra/*.asl.json` → the resolved plan that
  actually executes.
- `/internal/deliveries/{id}/start|stop` → the seed of the operator surface.

What is missing is only the **customer-facing intent layer**: Content, Connection, Route,
Capability. Build it on top of the existing kernel; do not re-model deliveries.

### Naming note

The current code models the *old* world (Source, Destination, StreamProfile, Delivery). The
target model above renames those concepts (Feed, Output, Quality preset, Route). Migration is
a design decision, not an instruction in this document — see "Open questions."

## Open questions

Not yet decided; do not build around an assumption.

- Migration path from `Source/Destination/StreamProfile/Delivery` to
  `Feed/Output/Preset/Route`. In-place rename (as was done for `core_*` → unprefixed) vs a
  new domain layer over the existing tables.
- Whether **Route** becomes a new table or Delivery gains route semantics.
- The capability registry schema and its source of truth (code, DB, or a served catalog).
- Whether the operator surface is the same app behind RBAC or a separate portal.
