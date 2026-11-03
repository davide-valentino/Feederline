# Feederline — P0 Scope & Design

> Status: draft (for sign-off)
> Date: 2026-11-03
> Depends on: `docs/product-vision.md`
> Scope: the first sellable slice. Implementation detail is included only where a decision
> changes the schema or the API.

## 1. What P0 proves

One design partner can, without touching AWS primitives:

1. register a channel/event and its feeds and outputs;
2. declare a **route** with a schedule, a quality preset, N outputs and a resilience level;
3. get an **authoritative dry-run**: feasibility checks + a customer-safe plan summary
   (the full resolved plan is operator-only);
4. schedule it, see it provision, go live, degrade/fail, complete or stop;
5. have support inspect deliveries, plans and outbox events, retry a failure, and stop a run —
   with every operator action audited.

If that demo is clean, P0 is done. Everything else is P1.

**Definition of done:** a route created through the API (not the old `Delivery` API) runs
end-to-end against the existing outbox/state-machine kernel, and an operator can intervene
through `/internal/*` with a token.

## 2. In / out of scope

| In P0 | Out of P0 (P1+) |
| --- | --- |
| Content registry, derived status | Content rights windows / geo restrictions |
| Feeds & Outputs with capability-required fields | Expert raw JSON path |
| Capability catalog served as data (code-defined) | Capability admin CRUD / per-tenant overrides |
| Route create + dry-run + schedule + cancel/stop | Route revisions / edit / reschedule / duplicate |
| Fan-out to N outputs (one Delivery per output) | Calendar/timeline view |
| Authoritative server-side checks + plan summary | Pricing / quote |
| Route event vocabulary | Custom ABR ladders |
| Operator reads + retry/stop + audit, behind a token | Full RBAC, multi-tenant roles, separate portal |
| Output overlap / conflict detection on create | Feed contention / capacity planning |
| Existing `Delivery` state machine reused unchanged | Second execution engine |

Commercial concepts (packages, entitlements, pricing, billing) stay out of the domain model.
The resolved plan (operator-only) keeps a `cost` **seam** only (no fields, no endpoint).

## 3. Domain model

```
Content 1──* Route *──* Output(Destination)
                 │
                 ├──* RouteFeed *── Feed(Source)
                 └──1──* Delivery ──* DeliveryOutboxEvent
Quality preset (catalog)      Capability (catalog)
```

### 3.1 Content

New table `content`.

| Column | Type | Notes |
| --- | --- | --- |
| `id` | UUID PK | |
| `name` | varchar(128), unique | |
| `content_type` | varchar(16) | `channel` \| `event` |
| `owner` | varchar(128) | free text for P0 |
| `description` | text, nullable | |
| `is_deleted` | bool | soft delete, as elsewhere |

**No `status` column.** Status is derived (§6.5).

### 3.2 Feed and Output (Connections)

No parallel `connection` table in P0. Extend the existing execution tables, because
`Delivery` already references them and the state machine must not change:

- `source` becomes the **Feed**: add `capability_id` (e.g. `srt-listener`), keep
  `source_type` as the execution field, keep `region`, `config`, `stream_profile_id`.
- `destination` becomes the **Output**: add `capability_id` (e.g. `hls`), keep
  `destination_type`, keep `region`, `config`.
- `config` holds the capability-required fields (port, latency, whitelist CIDR, stream ID,
  entitlement ARN, bucket/folder, URL, egress access, etc.) and is validated against the
  catalog on write.
- `source_type` / `destination_type` are **derived** from the capability's `legacy_*` field
  (§3.6), not chosen by the client — the capability is the public contract, the type is the
  execution detail.

Exposed under the customer vocabulary (`/v1/feeds`, `/v1/outputs`) while the old
`/v1/sources`, `/v1/destinations` routes stay for one release (additive, not breaking).

> **Decision D1** — this "extend in place" path is chosen over a new `connection` table to
> avoid dual writes and to keep `Delivery` untouched. If the team prefers a clean
> `connection` entity, it must also migrate `delivery.source_id` / `destination_id`, which is
> a larger change and should be scheduled before P0, not during it.

### 3.3 Route

New table `route`.

| Column | Type | Notes |
| --- | --- | --- |
| `id` | UUID PK | |
| `content_id` | UUID FK content | |
| `feed_id` | UUID FK source | primary feed |
| `redundant_feed_id` | UUID FK source, nullable | redundant source for every leg; required when resilience != single (D7) |
| `quality_preset` | varchar(32) | catalog id: `passthrough` \| `1080p` \| `1080p-premium` \| `uhd` |
| `resilience` | varchar(16) | `single` \| `redundant` \| `geo-redundant` |
| `desired_state` | varchar(16) | `draft` \| `scheduled` \| `active` \| `stopped` \| `cancelled` |
| `observed_state` | varchar(16) | `pending` \| `provisioning` \| `live` \| `degraded` \| `stopping` \| `completed` \| `failed` |
| `health` | varchar(16) | `healthy` \| `degraded` \| `unknown` |
| `planned_start` | UTCDateTime | tz required |
| `planned_end` | UTCDateTime, nullable | null iff open-ended |
| `is_open_ended` | bool | |
| `encrypted` | bool | applies to feeds |
| `resolved_plan` | JSON, nullable | last resolved plan (§6.1) |
| `plan_version` | int | default 1 |
| `stop_reason` | varchar(32), nullable | |
| `is_deleted` | bool | |

Constraints mirror `delivery`: end-after-start, open-ended-implies-null-end,
redundant-feed-required-when-not-single.

Join table `route_output` gives fan-out:

| Column | Type | Notes |
| --- | --- | --- |
| `route_id` | UUID FK route | PK part |
| `output_id` | UUID FK destination | PK part; primary destination |
| `redundant_output_id` | UUID FK destination, nullable | redundant destination; required when resilience != single (D7) |

A route must have ≥ 1 output (enforced in the API and by an application-level check; SQLite
cannot express it). When `resilience != single`, every leg requires both `redundant_feed_id`
(on the route) and `redundant_output_id` (on the leg) so it maps to `Delivery`'s redundant
source/destination pair unchanged.

**Desired vs observed.** These are separate on purpose (vision rule). The API never lets a
client set `observed_state`; it is derived from the route's deliveries and events.

### 3.4 Delivery (unchanged kernel, one addition)

Reuse `delivery` exactly as it is. Add:

- `route_id` UUID FK route, nullable (legacy rows and old API keep null).

On route schedule, P0 creates **one Delivery per output leg**, mapped straight onto the
existing redundancy pair:

- `source_id = route.feed_id`
- `redundant_source_id = route.redundant_feed_id` (null when `resilience = single`)
- `destination_id = route_output.output_id`
- `redundant_destination_id = route_output.redundant_output_id` (null when `single`)

One Delivery per output keeps the current state machine, the `ck_delivery_redundant_pair`
constraint, the overlap check and outbox logic intact, and gives per-output observed state
for free.

> **Decision D2** — one Delivery per output (not one per route). Simpler, reuses existing
> kernel, and the route aggregates delivery state. A single "route delivery" that fans out
> internally would require replacing the state machine.

> **Decision D7** — route resilience reuses `Delivery`'s redundant source/destination pair.
> The route-level `redundant_feed_id` supplies the redundant source for every leg; the
> per-leg `redundant_output_id` supplies the redundant destination. `single` leaves both
> redundant FKs null. This is the only shape that keeps `ck_delivery_redundant_pair` and the
> overlap check unchanged. If a shared redundant feed per route proves wrong, move
> `redundant_feed_id` into `route_output` per leg (see §11). The
> `geo-redundancy-requires-two-regions` check counts **primary** output legs only: the
> execution kernel requires a redundant pair to be same-region (different AZ), so
> cross-region redundancy is expressed as two primary legs, not as a cross-region pair.
> The redundant feed must be distinct from the primary feed (`duplicate-redundant-feed`)
> and in the same region (`redundant-feed-region-mismatch`); each redundant output must be
> in the same region as its primary output (`redundant-output-region-mismatch`).

### 3.5 Route events

New table `route_event` for the customer-facing vocabulary; written in the same transaction
as state changes.

| Column | Type |
| --- | --- |
| `id` | UUID PK |
| `route_id` | UUID FK route |
| `event_type` | varchar(64) |
| `level` | `info` \| `success` \| `warning` \| `error` \| `neutral` |
| `message` | text |
| `metadata` | JSON |
| `created_at` | UTCDateTime |

Vocabulary: `route.scheduled`, `route.started`, `route.degraded`, `route.failed`,
`route.completed`, `route.stopped`, `route.cancelled`.

Raw `DeliveryStart` / `PipelineLink` / `PipelineUnlink` stay operator-only; the mapping lives
in one module (`app/domains/route_events.py`) so it can be tested in isolation.

### 3.6 Capability and quality presets (catalog)

Code-defined in P0, served read-only. No `capability` table yet.

```text
app/capabilities/catalog.py
  CATALOG_VERSION = "2026-11-03"
  TRANSPORTS: list[Capability]   # 6
  FORMATS: list[Capability]      # 11
  PRESETS: list[QualityPreset]   # 4
```

Each capability carries: `id`, `kind` (`transport` | `format`), `name`, `mode`,
`regions: {eu|uk|us|ap → {available, reason}}`, `required_fields: [FieldSpec]`,
`constraints`, `fixed_config` (platform-filled values such as the protocol under
`ipsource`), `legacy_source_type` / `legacy_destination_type` (the execution field to
write), a `plan_recipe` and a `transcode_recipe`.

A `FieldSpec` carries `field_type` (`string` | `integer` | `boolean` | `url` | `cidr`),
`required`, `choices`, `required_when` (conditional requirement) and `secret`. Write-time
validation enforces required/conditional fields, choice membership and CIDR/URL shape.

Presets: `id`, `name`, `description`, `ladder: [Rendition]`, `transcode`,
`profile` (codec/GOP/rate-control/frame-rate/scan/colour-space, or `None` for
passthrough) and `audio_tracks`. `passthrough` means "reuse the source profile"; the
other presets resolve to a bundled profile. RTMP pins the audio track count to 1.

P0 ships the corrected six transports (SRT listener/caller, RTP, RIST, Zixi, MediaConnect)
and eleven outputs (linear/IP, SRT caller/listener, RTMP, HLS, DASH, S3 archive,
MediaShuttle, MediaConnect, Zixi push/pull), plus four presets (passthrough, 1080p,
1080p-premium, 4K UHD). Field sets and resource recipes are aligned with the classic
Arqade services; see `docs/arqade-counterpart-review.md`.

**Capability ↔ legacy type mapping is explicit, not inferred.** `rtp` and `rist` both map
to `ipsource` (RIST is `protocol=rist` in `fixed_config`, not a separate source type),
`zixi` maps to `zixipushsource`, and `mediaconnect` requires the customer's
`entitlement_arn` — the flow ARN is generated by the platform. The `legacy_*` field on
each capability is what the plan resolver writes into `source.source_type` /
`destination.destination_type`. A capability without a `legacy_*` value would be
catalog-visible but non-executable; none is in the P0 catalog.


> **Decision D3** — the catalog's source of truth is code in P0, served via API. A DB-backed
> catalog with per-tenant rollout is P1. This is deliberate: it avoids building an admin
> surface before we know what varies.

## 4. Customer API

All new endpoints are additive; the existing `/v1/*` delivery API keeps working. Use
snake_case JSON (matches the current API), UTC timestamps with timezone, `limit`/`offset`,
soft delete, unique names and 409 on conflict — same conventions as `app/domains/*.py`.

### 4.1 Content

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/v1/content` | create |
| GET | `/v1/content` | list |
| GET | `/v1/content/{id}` | get (includes derived `status`) |
| PATCH | `/v1/content/{id}` | update name/owner/description |
| DELETE | `/v1/content/{id}` | soft delete (409 if active routes) |

### 4.2 Connections

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/v1/feeds` | create feed (capability + region + config) |
| GET | `/v1/feeds` | list |
| GET/PATCH/DELETE | `/v1/feeds/{id}` | get/update/soft-delete |
| POST | `/v1/outputs` | create output (format capability + region + config) |
| GET | `/v1/outputs` | list |
| GET/PATCH/DELETE | `/v1/outputs/{id}` | get/update/soft-delete |

Write-time validation: `capability_id` exists and matches the endpoint's kind, region is
available for it, every required/conditional `required_field` is present, choice fields
match their allowed values, and `cidr`/`url` fields are well formed. Platform-filled values
(e.g. `protocol` for RTP/RIST) are merged into `config` and are not asked of the customer.

Reads return a **capability summary** (`id`, `kind`, `name`, `mode`), the region and a
`details` object containing only standard (non-provider, non-secret) values. Execution
types (`source_type`/`destination_type`), provider identifiers (ARNs, account/bucket
names), `config`, `availability_zone` and `stream_profile_id` are never returned on the
customer surface. Customer connections and legacy execution rows are separate name
domains: a customer name cannot be used to detect a hidden legacy row, and uniqueness is
enforced within each domain.

### 4.3 Capabilities & presets

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/v1/capabilities` | public catalog + `catalog_version` |
| GET | `/v1/capabilities/{id}` | one capability |
| GET | `/v1/quality-presets` | preset catalog |

The customer catalog exposes `id`, `kind`, `name`, `mode`, regions, required fields and
safe constraints, but **not** `legacy_*` types, `fixed_config`, `plan_recipe`,
`transcode_recipe` or a preset's codec profile. Those are served by
`/internal/capabilities` and `/internal/quality-presets`.

Filtering (`?kind=transport&region=eu`) is nice-to-have, not required for P0.

### 4.4 Routes

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/v1/routes/dry-run` | authoritative checks + customer plan summary; **no persistence** |
| POST | `/v1/routes` | create + schedule; fan-out to one Delivery per output |
| GET | `/v1/routes` | list (`?state=`, `?content_id=`, `?from=&to=`) |
| GET | `/v1/routes/{id}` | get (desired, observed, health, plan summary) |
| POST | `/v1/routes/{id}/cancel` | cancel before start |
| POST | `/v1/routes/{id}/stop` | stop an active route |
| GET | `/v1/routes/{id}/events` | customer event timeline |
| GET | `/v1/routes/{id}/plan` | customer plan summary (full plan: `/internal/plans/{id}`) |

Route create body (dry-run uses the same body):

```json
{
  "content_id": "…",
  "feed_id": "…",
  "redundant_feed_id": null,
  "quality_preset": "1080p-premium",
  "resilience": "redundant",
  "output_ids": ["…", "…"],
  "planned_start": "2026-11-04T20:00:00Z",
  "planned_end": null,
  "is_open_ended": true,
  "encrypted": true
}
```

Dry-run response (customer-safe):

```json
{
  "feasible": true,
  "checks": [
    {"level": "warn", "code": "transcode-required", "message": "…"}
  ],
  "effective_quality_preset": "1080p",
  "plan_summary": {
    "effective_quality_preset": "1080p",
    "estimated_start": "35-50s",
    "output_count": 2,
    "resilience": "redundant",
    "audio_tracks": 4
  }
}
```

The full plan (`provider`, `workflow`, `region`, `resources`, `stream_profile`) is available
only from `POST /internal/routes/dry-run` and `GET /internal/plans/{route_id}`.

`feasible` is false iff any check has `level = "error"`. Blocking checks are enforced again
server-side on `POST /v1/routes`; a client cannot schedule an infeasible route.

### 4.5 Control room read models

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/v1/control-room/summary` | live/scheduled/error counts, feeds online, outputs in use |
| GET | `/v1/control-room/activity` | recent route events |
| GET | `/v1/control-room/upcoming` | scheduled routes ordered by start |

P0 can implement these as thin queries over `route` / `route_event`; no new tables.

## 5. Operator API (internal)

Separate surface, same app, under `/internal/*`. P0 adds a **placeholder auth dependency**:
a static operator token from settings (`OPERATOR_TOKEN`), compared constant-time, required on
every `/internal/*` route. Missing/incorrect → 401. This is not full RBAC; it is the P0
boundary and the seam for P1 #10.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/internal/deliveries` | list with route, provider, state, workflow, outbox, error |
| GET | `/internal/deliveries/{id}` | detail incl. workflow runs |
| POST | `/internal/deliveries/{id}/retry` | re-queue a failed event (audited) |
| POST | `/internal/deliveries/{id}/stop` | stop (audited; reuse existing logic) |
| GET | `/internal/capabilities` | full catalog incl. legacy types, recipes, fixed config |
| GET | `/internal/capabilities/{id}` | one capability with execution detail |
| GET | `/internal/quality-presets` | presets incl. codec profile |
| POST | `/internal/routes/dry-run` | full feasibility evaluation + resolved plan |
| GET | `/internal/plans/{route_id}` | resolved plan + resource map |
| GET | `/internal/outbox` | outbox events (`?status=&route_id=`) |
| GET | `/internal/route-events` | raw route events incl. kernel metadata |
| GET | `/internal/audit` | operator audit log |

Legacy execution routes (`/v1/sources`, `/v1/destinations`, `/v1/stream-profiles`,
`/v1/deliveries`) remain for one release but now require the operator token: they expose
execution types and kernel state and are not part of the customer intent surface.

New table `operator_audit`: `id`, `actor` (token label/name), `action`, `target_type`,
`target_id`, `request_metadata` JSON, `created_at`. Every mutating internal route writes one
row in the same transaction.

The existing unauthenticated `/internal/deliveries/{id}/start|stop` gain the token dependency
in the same change. This is a behavior change and must be called out in the changelog.

> **Decision D4** — static token in P0. Real RBAC (customers vs support vs platform admin)
> is P1. The token is a boundary, not an identity system; `actor` is a fixed label in P0.

## 6. Core flows

### 6.1 Dry-run (authoritative)

1. Load content, primary/redundant feed, outputs, preset, capability entries.
2. Validate: content/feed/outputs exist and are not deleted; output IDs are unique; preset
   exists; schedule valid (start present, end rules, not in the past for new routes).
3. Run checks, at minimum:
   - feed capability available in feed region;
   - every output capability available in its region;
   - OTT output (HLS/DASH/RTMP) with `passthrough` → warn + effective preset upgrade;
   - `geo-redundant` requires **primary** outputs in ≥ 2 regions; a redundant output is a
     same-region twin and does not add a region;
   - `redundant`/`geo-redundant` requires a redundant feed, distinct from the primary
     (`duplicate-redundant-feed`) and in the same region (`redundant-feed-region-mismatch`),
     plus a redundant output on every leg in the same region as its primary
     (`redundant-output-region-mismatch`). When both availability zones are set they must
     differ (the execution kernel's redundant pair is same-region, different-AZ);
   - output overlap against active/scheduled deliveries (reuse the `Delivery` overlap query);
   - `uhd` + `geo-redundant` → info/approval note;
   - event + open-ended → error (events must have an end);
   - bounded route shorter than 10 minutes → error;
   - end ≤ start → error.
4. Resolve the plan from the catalog's `plan_recipe` (provider, workflow, region, ordered
   resources, estimated start, version). The plan is **derived**, never client-supplied.
5. Return `feasible`, `checks`, `effective_quality_preset`, `plan_summary`. Persist nothing.
   `POST /internal/routes/dry-run` returns the full plan instead.

### 6.2 Schedule / fan-out

`POST /v1/routes` re-runs the dry-run (never trust the client), then in **one transaction**:

1. insert `route` with `desired_state = scheduled`;
2. insert `route_output` rows;
3. create one `delivery` per output leg (`route_id`, `source_id = feed_id`,
   `redundant_source_id = redundant_feed_id` when redundant, `destination_id = output_id`,
   `redundant_destination_id = redundant_output_id` when redundant, `planned_start/end`,
   `is_open_ended`, `state = PENDING`);
4. create `DeliveryStart` (and `DeliveryStop` when bounded) outbox events — reuse
   `create_delivery`'s logic or call it internally so there is one implementation;
5. store `resolved_plan` + `plan_version` on the route;
6. write a `route.scheduled` event.

Idempotency: `POST /v1/routes` accepts an `Idempotency-Key` header; repeated keys return the
same route (new table `idempotency_key` or a unique `(tenant, key)` column — P0 can use a
small table with `key`, `route_id`, `request_fingerprint`, `created_at`). The stored
`request_fingerprint` is a sha256 of the canonicalised intent, so replaying a key with a
different body returns **409**, not the first route.

### 6.3 Lifecycle reconciliation

The outbox worker and `/internal` callbacks already drive `delivery.state`. Add a thin
sync (`app/domains/route_lifecycle.py`) that maps delivery transitions to route state:

| Delivery signal | Route effect |
| --- | --- |
| all deliveries `STARTING` | `observed_state = provisioning` |
| any `IN_PROGRESS` | `observed_state = live`, `health = healthy` |
| `PipelineFailed` / `ERROR` | `observed_state = failed`, `health = degraded`, `route.failed` |
| `degraded` signal (P1) | `health = degraded`, `route.degraded` |
| all `COMPLETE` | `observed_state = completed`, `desired_state = stopped` |
| all `CANCELLED` | `observed_state = completed`, `desired_state = cancelled` |

P0 does not add new outbox event types; the sync runs after existing transitions and is
idempotent (derives from delivery states, not from counters).

### 6.4 Operator intervention

- Retry a stuck outbox event: an event that failed to publish is already `PENDING` with
  `attempts` incremented and `last_error` set (there is no `FAILED` outbox status). Retry
  forces it due now (`due_at = now`, `attempts` kept) so the worker re-dispatches it.
  Audited as `outbox.retry`.
- Retry a failed delivery: for a `delivery` in `ERROR`, re-queue `PipelineLink` to re-link
  the pipeline. Audited as `delivery.retry`. Distinct from the outbox retry above.
- Stop: call the existing stop logic, then run the route sync, audit it.
- All are idempotent and return the updated read model.

### 6.5 Content status derivation

Precedence, evaluated over non-deleted routes for the content:

1. any route `observed_state in (provisioning, live, degraded)` → `live`
2. else any route `desired_state = scheduled` and `planned_start > now` → `scheduled`
3. else any route `desired_state = active` → `live`
4. else if all routes `observed_state = completed` and ≥ 1 route → `complete`
5. else `off-air`

Keep this in one function and test it. No stored status.

## 7. Data and migration

- New migration `0004_intent_layer` (do not edit 0001–0003).
- New tables: `content`, `route`, `route_output`, `route_event`, `operator_audit`,
  `idempotency_key`.
- Altered tables: `source` (+`capability_id` nullable), `destination` (+`capability_id`
  nullable), `delivery` (+`route_id` nullable). All additive; backfill is unnecessary
  (no deployed DB).
- Indexes: `route(desired_state, planned_start)`, `route(content_id)`,
  `route_event(route_id, created_at)`, `delivery(route_id)`,
  `route_output(output_id)`.
- Tests build from `Base.metadata.create_all`, so models and migration must be checked
  together (repo convention). Add a migration smoke test if practical.

Backward compatibility: old `/v1/sources`, `/v1/destinations`, `/v1/deliveries` and
`/v1/stream-profiles` remain. The new UI uses the new endpoints. Remove the old ones in P1
once no customer depends on them.

## 8. Non-functional

- **Auth:** operator token only (D4). Customer endpoints stay unauthenticated for P0, as the
  app is local/design-partner only. Do not expose publicly.
- **Audit:** every internal mutation writes `operator_audit`.
- **Concurrency:** reuse the PostgreSQL `LOCK TABLE delivery` + overlap query for route
  scheduling; the existing create path already locks. Keep the same pattern.
- **Errors:** machine-readable `code` on checks; HTTP 409 for conflicts, 422 for validation,
  404 for missing, 409 for delete-in-use (mirror existing code).
- **Time:** UTCDateTime everywhere; reject naive timestamps (existing pattern).
- **Observability:** structured logs on route state transitions; outbox already has
  `attempts`/`last_error`.

## 9. Acceptance criteria

1. A route with two outputs creates exactly two deliveries, linked by `route_id`, each with
   its own outbox events, in one transaction.
2. Dry-run rejects an infeasible transport/region pair, a missing redundant feed or
   redundant output for `redundant`, a past start, and `end <= start`; `POST /v1/routes`
   rejects the same inputs even if the client skips dry-run.
3. `passthrough` + HLS returns a warning, upgrades the effective preset to `1080p`, the
   customer summary reports `1080p`, and the operator plan includes a transcode resource.
4. Scheduling an output that overlaps an active delivery returns 409.
5. Route transitions follow delivery transitions per §6.3 and emit exactly one customer event
   per transition (idempotent re-sync emits none).
6. Content status is derived correctly for the five cases in §6.5 and never stored.
7. `/internal/*` without the token returns 401; with it, retry and stop work and write audit
   rows.
8. `pytest` passes with new tests for catalog validation, dry-run, fan-out, lifecycle,
   content status, operator auth and conflict detection.

## 10. Milestones

| Milestone | Deliverable | Exit check |
| --- | --- | --- |
| **M0 — Foundations** | `capability` catalog module + `GET /v1/capabilities`, `/v1/quality-presets`; content CRUD; feed/output CRUD with capability validation; migration 0004 | Catalog + CRUD tests green |
| **M1 — Intent** | `route` model + `route_output`; `POST /v1/routes/dry-run`; checks + plan resolver | Dry-run tests cover §6.1 |
| **M2 — Execution** | `POST /v1/routes` fan-out; lifecycle sync; route events; cancel/stop | End-to-end route → N deliveries → events |
| **M3 — Operator** | token dependency on `/internal/*`; operator read models; retry/stop; audit | 401/audit/retry tests |
| **M4 — Control room** | summary/activity/upcoming read models; demo script; docs | Design-partner demo runs clean |

M0–M1 are the design decisions; M2–M3 are integration; M4 is polish. If time is short, M4 can
ship as read-only queries with no new tables.

## 11. Decisions to confirm

| # | Decision | Recommendation | If changed |
| --- | --- | --- | --- |
| D1 | Extend `source`/`destination` as Feed/Output vs new `connection` table | **Extend in place** | New table + migrate `delivery.source_id/destination_id`; schedule before M1 |
| D2 | One Delivery per output vs one per route | **Per output** | Replace/aggregate the state machine; large |
| D3 | Capability catalog in code vs DB | **Code in P0** | Add admin CRUD + rollout; defer to P1 |
| D4 | Operator auth = static token vs full RBAC | **Static token** | Needs identity/roles; defer to P1 |
| D5 | Content status rules (§6.5) | **As written** | Adjust precedence and tests |
| D6 | Idempotency key scope | **Global per token** | Needs tenant scope in P1 |
| D7 | Route redundancy via `Delivery` redundant pair | **As written** (§3.4) | If a shared redundant feed per route is wrong, move `redundant_feed_id` to `route_output` per leg |

## 12. P1 hooks (do not build now)

- Route revisions (immutable versions) and edit/reschedule/duplicate.
- Calendar/timeline view and feed contention.
- DB-backed capability catalog with per-tenant flags.
- Full RBAC and a separate operator portal.
- Second execution engine behind the same `plan_recipe`.
- Pricing service consuming the resolved plan (no schema change).
