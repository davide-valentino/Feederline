---
name: Arqade Counterpart Reference
description: Use when aligning the Feederline capability catalog, planner or P0 domain model with the classic Arqade services. Points at the external repositories and the field/recipe/preset/schedule dimensions that must stay consistent, with the prioritised corrections already identified.
---

# Arqade counterpart reference

Feederline orchestrates on top of the existing Arqade services. The P0 catalog
(`app/capabilities/catalog.py`) hides those services behind capability
declarations, so its field sets, `legacy_*` types, plan recipes and presets must
match what Arqade actually accepts. This skill records where the counterpart
lives and what to check; the full evidence log is
`docs/arqade-counterpart-review.md` (read it first).

## Repositories (machine-specific, read-only)

```text
/home/davide/arqiva/arqade/
/home/davide/arqiva/arqade/repositories/arqade-common
/home/davide/arqiva/arqade/repositories/arqade-sources
/home/davide/arqiva/arqade/repositories/arqade-destinations
/home/davide/arqiva/arqade/repositories/arqade-distribution
/home/davide/arqiva/arqade/repositories/arqade-media-pipeline
/home/davide/arqiva/arqade/repositories/arqade-stream-profiles
/home/davide/arqiva/arqade/repositories/arqade-ui
```

Never modify these. They are a separate codebase; only read and compare.

## What to compare when a capability/preset changes

1. **Ingest transports** — `arqade-common/common/app/constants.py` (`ESourceType`,
   `EIpSourceType`) and `arqade-common/common/app/schemas_source.py`. Key facts:
   RIST is `ipsource` with `protocol=rist`; MediaConnect takes `entitlement_arn`
   (the flow ARN is platform-generated); SRT listener/caller and Zixi require
   port/whitelist/latency, not an endpoint.
2. **Output formats** — `arqade-media-pipeline/app/schemas_dest.py` (11 types).
   HLS/DASH/RTMP/archive all transcode via MediaLive; DASH is a MediaPackage v2
   channel + origin endpoint (no customer URL); archive requires bucket+key.
3. **Plan recipes & workflow keys** — `rules_planner_conditions.py`,
   `rules_planner_actions.py`, `plan_to_workflow_mappings.py` in
   `arqade-media-pipeline`. Recipes are `mediaconnect_flow*`, `medialive_*`,
   `mediapackage_v2_*`, `s3_archive`; workflow keys are
   `{family}_{same|cross_region_with_router}_{single|redundant}`.
4. **Quality presets** — `arqade-stream-profiles/app/models.py` + `app/ladder.py`.
   One row = one rendition; presets resolve to a profile (codec/GOP/rate
   control/frame rate/scan/colour), max 5 renditions, default 4 audio tracks,
   RTMP forces 1; passthrough = no profile.
5. **Scheduling** — `arqade-distribution/app/models.py` + `arqade-ui` create
   payloads. Schedule lives on the contribution; events need an end, bounded
   routes are ≥10 minutes; redundant pairs are same-region/different-AZ.

## Enforced invariants in this repo

- Capability ids are unique across transports and formats
  (`app/capabilities/catalog.py` raises on collision).
- `legacy_source_type`/`legacy_destination_type` are the only execution fields
  written to `source.source_type`/`destination.destination_type`.
- Redundant feed/output must be same region (different AZ when both set); geo
  redundancy is two primary legs in two regions.

## Open questions (do not build around an assumption)

Which repo owns destinations (`arqade-destinations` vs `arqade-media-pipeline`)?
Is the target the Airflow/MWAA path or the Step Functions pilot? Is the
cross-region Router path in P0? See the "Open questions" section of
`docs/arqade-counterpart-review.md`.
