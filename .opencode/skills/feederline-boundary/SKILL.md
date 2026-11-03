---
name: Feederline Customer/Operator Boundary
description: Use when reviewing or changing Feederline P0 API endpoints or schemas, to verify the customer intent surface never exposes execution types, provider identifiers, delivery ids or resolved plans. Codifies the customer-vs-operator split in docs/product-vision.md and pinned by tests/test_p0_customer_surface.py.
---

# Feederline customer/operator boundary

Feederline splits its API into two surfaces. The customer declares intent; the
operator sees execution. The boundary is enforced at the schema level, not just
in docs. Use this checklist whenever you touch an endpoint, a Pydantic schema,
or the capability catalog, and whenever you review someone else's change to the
customer surface.

The source of truth for the product rule is `docs/product-vision.md` (decision
#5) and `docs/p0-scope.md` (§4–5). The executable pin is
`tests/test_p0_customer_surface.py`; run it as the gate.

## The two surfaces

**Customer (`/v1/*`)** — intent only:
`/v1/content`, `/v1/feeds`, `/v1/outputs`, `/v1/capabilities`,
`/v1/capabilities/{id}`, `/v1/quality-presets`, `/v1/routes/dry-run`,
`/v1/routes`, `/v1/routes/{id}` (+`/cancel`, `/stop`, `/events`, `/plan`),
`/v1/control-room/{summary,activity,upcoming}`.

**Operator (`/internal/*`, plus token-gated legacy execution routes)** — full
detail: `/internal/deliveries*`, `/internal/outbox*`, `/internal/capabilities*`,
`/internal/quality-presets`, `/internal/routes/dry-run`, `/internal/plans/{id}`,
`/internal/audit`, `/internal/route-events`, and the legacy
`/v1/sources`, `/v1/destinations`, `/v1/stream-profiles`, `/v1/deliveries`
(operator token required).

## Forbidden on any customer response

Never let these appear in a customer payload:

- execution types — `source_type`, `destination_type`
- provider identifiers/credentials — ARNs, account IDs, bucket/folder names,
  `flow_arn`, `entitlement_arn`, `cdn_distribution_arn`, crypto keys, stream keys
- raw `config` blobs, `availability_zone`, `stream_profile_id`
- kernel state — `delivery_ids`, delivery state enums, outbox rows, workflow runs
- full plan — `resolved_plan`, `plan_version`, `provider`, `workflow`, `region`
  (as plan detail), `resources`, `stream_profile`/codec profile
- operator event metadata (e.g. `delivery_states`)

Allowed: `capability` summary `{id, kind, name, mode}`, `details` (standard
fields only), `plan_summary` `{effective_quality_preset, estimated_start,
output_count, resilience, audio_tracks}`, and `region` on connections (per
`docs/p0-scope.md` §4.2).

## Direct checks

1. **Response models** — `app/domains/connections.py` (`FeedRead`, `OutputRead`),
   `app/domains/routes.py` (`RouteRead`, `PlanSummary`, `DryRunResponse`,
   `RouteEventRead`), `app/domains/content.py` (`ContentRead`). Grep each for the
   forbidden field names.
2. **Catalog split** — `app/capabilities/catalog.py`: `capability_to_dict(public=True)`
   must exclude `legacy_*`, `fixed_config`, `plan_recipe`, `transcode_recipe`;
   `preset_to_dict(public=True)` must exclude `profile`.
3. **Serializers** — `serialize_route` and `public_config` must project, not leak.
4. **Event metadata** — `app/domains/route_events.py::route_event_to_dict(public=True)`
   must filter to `_PUBLIC_EVENT_METADATA`.

## Indirect leaks (the ones reviews actually catch)

- **Error messages / checks** — customer `checks[].message` and HTTP `detail`
  must not name providers, workflows, "delivery", or interpolate a hidden
  resource's `name`. Grep for `f"...{...}..."` interpolations in customer paths.
- **Legacy-row visibility** — customer `GET/PATCH/DELETE /v1/feeds` and
  `/v1/outputs` must filter `capability_id IS NOT NULL` so operator-created
  rows are invisible and immutable.
- **Name-existence oracle** — uniqueness is per domain via partial unique
  indexes (`uq_source_customer_name`, etc.); a customer name must not reveal or
  collide with a hidden legacy name.
- **Control-room aggregates** — `outputs_in_use` must only count route-linked
  deliveries (`Delivery.route_id IS NOT NULL`).
- **OpenAPI** — the public `/openapi.json` still describes operator schemas;
  acceptable for local P0, disable for a deployed environment.

## When you change the boundary

Update `tests/test_p0_customer_surface.py` to pin the new/removed fields, and
update `docs/p0-scope.md` / `docs/product-vision.md` / `README.md` if the
surface changed.

## Gate commands

```bash
poetry run pytest tests/test_p0_customer_surface.py tests/test_p0_catalog.py
poetry run pytest            # full suite
grep -rn "resolved_plan\|delivery_ids\|source_type\|destination_type" app/domains/
```

## Report format

Emit a table of CUSTOMER routes with response model and PASS/FAIL, a table of
OPERATOR routes with the detail they expose, then findings with
`file:line`, severity (high/medium/low) and a one-line fix. Say "not found"
rather than guessing.
