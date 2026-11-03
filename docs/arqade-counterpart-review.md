# Arqade counterpart review — P0 assumptions vs. the existing platform

> Status: findings for input to P0 sign-off / P1 planning
> Date: 2026-11-03
> Scope: read-only inspection of `/home/davide/arqiva/arqade` and
> `repositories/{arqade-common, arqade-sources, arqade-destinations, arqade-distribution,
> arqade-media-pipeline, arqade-stream-profiles, arqade-ui}` against
> `app/capabilities/{catalog.py, planner.py}` and `app/domains/*`.
> These are **not** implemented changes; they are corrections to validate before the
> capability catalog is presented as the product contract.

## Headline

- The `legacy_*` **type strings are mostly right**.
- The **required fields, region matrix, transcode flags and plan recipes are largely
  wrong**, and several real capabilities are missing or mis-modelled.
- **D2 (one Delivery per output) and D7 (redundant pair) hold** against the real kernel.
- **Cross-region redundant pairs do not hold**: redundancy is same-region (different AZ).
  This forced a correction to the `geo-redundant` dry-run rule (see §D).

## Status (2026-11-03)

Must-fix 1–10 below are implemented in `app/capabilities/{catalog.py,planner.py}` and the
write-time/dry-run validation:

- RIST is `ipsource` with `protocol=rist` (fixed by the platform).
- MediaConnect requires `entitlement_arn`; `flow_arn` is not customer input.
- Transport/output field sets, choices, conditional fields and CIDR/URL validation match
  the classic services.
- The catalog serves 6 transports and 11 outputs (including SRT listener, MediaConnect,
  Zixi push/pull and MediaShuttle).
- `archive`/`hls`/`dash`/`rtmp` transcode; recipes and workflow keys use the real resource
  steps (`medialive_channel`, `medialive_*_group`, MediaPackage v2 channel/endpoint,
  cross-region router).
- Presets resolve to profiles/ladders; passthrough has no profile; audio-track count is
  modelled (RTMP = 1).
- Events must have an end; bounded routes are at least 10 minutes.
- Redundant pairs are same-region/different-AZ; geo-redundancy is two primary regions.

Nice-to-have 11–13 (region sets, rendition/port limits, HDR) remain open, as do the
questions at the end of this document. Secret fields (`crypto_key`, `stream_name`) are
marked `secret` in the catalog but are not yet redacted on read; storing them as a
Secrets Manager reference and returning write-only values is P1.

## A. Ingest transports

Real enumeration: `arqade-common/common/app/constants.py` (`ESourceType`) has five types:
`ipsource`, `mediaconnectsource`, `zixipushsource`, `srtlistenersource`,
`srtcallersource`. **RIST is not a source type** — it is `ipsource` with
`protocol=rist` (`EIpSourceType`: `rtp | rtp-fec | rist`).

| P0 capability | `legacy_source_type` | Field corrections |
| --- | --- | --- |
| `srt-listener` | `srtlistenersource` ✔ | needs `port`, `min_latency_ms`, `whitelist_cidr` |
| `srt-caller` | `srtcallersource` ✔ | `listener_address` + `port` + `min_latency_ms`; optional `stream_id` (not `endpoint`) |
| `rtp` | `ipsource` ✔ | `port` + `whitelist_cidr` (+ `protocol`); host is derived, not customer input |
| `zixi` | `zixipushsource` ✔ | `port` + `whitelist_cidr`; `stream_id` optional; host derived |
| `mediaconnect` | `mediaconnectsource` ✔ | requires **`entitlement_arn`**, not `flow_arn` (the flow is created *from* the entitlement) |
| `rist` | `None` ✘ | should be `ipsource` with `protocol=rist` (real code supports it) |

Evidence: `arqade-common/common/app/schemas_source.py`,
`arqade-sources/app/models.py`, `arqade-sources/app/mediaconnect.py`,
`arqade-sources/app/service.py` (one region set for all source types).

Also: `max_bitrate_bps` exists on every source and is absent from P0; the "not offered in
AP/US" region restrictions in the catalog have no counterpart.

## B. Outputs

Real enumeration: 11 types in `arqade-media-pipeline/app/schemas_dest.py`
(`srtlistener`, `srtcaller`, `ip`, `mediaconnect`, `zixipush`, `zixipull`, `rtmp`,
`s3archive`, `hls`, `dash`, `mediashuttle`). P0 can express only 6 of them.

- `linear → ipdestination` ✔ but RTP / RTP-FEC / RIST live in `ip_destination_type`, so P0
  cannot distinguish them and cannot express a RIST output.
- `srt → srtcallerdestination` ✔ but `srtlistenerdestination` (a different output) is
  missing.
- `archive → s3archivedestination` ✔ but requires `bucket_name` + `folder_path`, and
  MediaShuttle is a distinct type with server-derived bucket/folder/role.
- `hls` requires `url`, `output_destination_type`, `name_modifier`, `cdn_settings_type`.
- `rtmp` requires `url` + secret `stream_name`.
- `dash` creates a MediaPackage v2 channel + origin endpoint; it requires `egress_access`
  (+ `cdn_distribution_arn` iff `cdn`) and has **no customer URL** — P0's `target` is wrong.
- Not expressible at all: `mediaconnect`, `zixipush`, `zixipull`, `srtlistener`,
  `mediashuttle`.

## C. Field semantics worth fixing in the catalog

- **MediaConnect**: customer supplies an entitlement ARN; `flow_arn` is an output.
- **Encryption is per connection**, on feeds *and* outputs: `crypto_algo` + write-only
  `crypto_key` (stored as a Secrets Manager ARN). A route-level `encrypted` boolean cannot
  represent it.
- **Zixi / RTP / SRT-listener**: the ingest host is derived; the customer supplies
  port/whitelist/latency, not an endpoint.
- **SRT listener output**: port 5000 is reserved for MediaLive transcode egress; there are
  real port-collision rules (`arqade-media-pipeline/app/pipeline/validator.py`).

## D. Execution unit and plans

**D2 holds**: the kernel plans a `(source, destination)` pair (plus redundant twin) per
delivery; fan-out = one delivery per output, each adding a flow output to the shared flow
(`arqade-media-pipeline/app/pipeline/{plan/rules_types.py,pipeline.py}`).

**D7 holds mechanically**: redundancy is "both sources and both destinations present"
(`rules_planner_conditions.py`). **But redundant sources and destinations must be in the
same region and different AZ** (`pipeline/validator.py`). Cross-region redundancy is not a
valid pair; cross-region is a separate Router path.

**Consequence for P0 (already applied)**: `geo-redundant` is modelled as **two primary
output legs in two regions**, and redundant pairs must be same-region. The dry-run now
rejects `redundant-feed-region-mismatch`, `redundant-output-region-mismatch`, and
same-AZ pairs. The earlier "count both primary and redundant legs" reading was wrong.

**Plan recipes are materially wrong**:

| P0 recipe | Reality |
| --- | --- |
| `rtmp → rtmp_packager` | no packager; MediaLive channel + `RtmpGroupSettings` |
| `hls → … media_package_origin` | MediaLive + HLS group; MediaPackage is optional |
| `dash → transcode_job, dash_packager, media_package_origin` | MediaLive + MediaPackage v2 channel + origin endpoint + policy (channel group must pre-exist) |
| `zixi → zixi_bridge` | no bridge; a zixi-push source on the flow |
| `archive.requires_transcode = false` | archive/rtmp/hls/dash all transcode via MediaLive |
| workflow name `{format}_{region}_{resilience}` | real keys are `ARQADE_WORKFLOW_TYPE` (`hls_same_region_redundant`, `direct_cross_region_with_router_redundant`, pre-created variants, overlay variants) |

The P0 Step Functions pilot is narrower still: `resolve_direct_link_plan` supports only
MediaConnect sources, non-redundant, same-region (`app/workflows/direct_same_region.py`).

## E. Quality presets

Real `StreamProfile` is one encoded rendition: codec, bitrate, width/height, GOP,
colour space, rate control, frame rate, scan type, plus a `renditions` JSONB
(`arqade-stream-profiles/app/models.py`, `app/ladder.py`).

- P0 presets are **labels, not resolvable profiles**; the planner needs a
  `stream_profile_id`.
- `passthrough` is derived (`stream_profile_id = NULL` / `is_stream_profile_locked`), not a
  preset row.
- Real limits: max 5 renditions, no upscaling, UHD ≤ 40 Mbps, multi-rendition only for
  HLS/DASH.
- Default audio tracks = **4** (`PipelineSpec.num_audio_tracks`); RTMP forces 1.
- HDR colour space is stored but not rendered by the current MediaLive video descriptions.

## F. Scheduling

- The schedule lives on the contribution (source pair), not the delivery; fan-out is N
  deliveries per contribution.
- Delivery states are a faithful copy of the existing enum.
- **Overlap detection at schedule time is new** (Arqade only checks at runtime); the P0
  behaviour is an addition, not a port.
- Real rules P0 lacks: minimum 10-minute window; events must have an end and cannot be
  open-ended; back-to-back / cancellation delays.

## G. Naming and missing concepts

Renames are sound (Contribution→Route, Source→Feed, Destination→Output,
StreamProfile→Preset). Missing inputs the existing pipeline needs:

1. entitlement ARN vs flow ARN (MediaConnect feeds);
2. per-connection encryption (algo + key/secret);
3. availability-zone semantics for redundant pairs (same region, different AZ);
4. pre-created destinations (`is_precreated`, `pre_created_flow_arn`);
5. MediaConnect Router (`router_input_arn`, `router_input_tier`);
6. PNG overlay (`png_overlay_s3url`);
7. S3 archive `extra_tags` (`EventDuration`, `ContributionName`);
8. a real preset → profile binding (codec/ladder/audio), and `stream_profile_locked` set by
   route fan-out.

## Prioritized corrections

### Must-fix before the catalog is treated as the contract

1. RIST → `ipsource` + `protocol=rist`; remove the "no RIST" claims.
2. MediaConnect source requires `entitlement_arn`; `flow_arn` is an output.
3. Correct required fields for SRT listener/caller, RTP, Zixi (see §A).
4. Correct output fields for HLS, RTMP, DASH, archive; add `ip_destination_type`.
5. `archive.requires_transcode = true`; re-audit hls/dash/rtmp.
6. Replace invented plan recipes and workflow keys with the real resource fragments.
7. Keep `geo-redundant` = primary legs in two regions; enforce same-region redundant pairs
   (done in this change).
8. Add the missing output capabilities or explicitly sell a documented subset.
9. Bind presets to real profiles/ladders; `passthrough = no profile`; model audio tracks.
10. Enforce event/channel schedule rules.

### Nice-to-have

11. Use the real region sets; drop the invented AP/US splits.
12. Replace per-format `max_outputs` with rendition/flow-output limits; add port rules.
13. Render colour space if HDR is promised; otherwise stop claiming HDR.

## Open questions

1. Is `arqade-destinations` still authoritative, or has `arqade-media-pipeline` replaced it?
   They disagree on the 11th type (`virtualconnect` vs `dash`).
2. Which service creates deliveries (no Delivery ORM exists in the seven repos)?
3. Is the P0 target the Airflow/MWAA path or the Step Functions pilot? The catalog recipes
   match neither.
4. Should a customer-supplied `flow_arn` be a product, or only managed entitlements?
5. Is the cross-region Router path in P0? If not, global availability claims are misleading.
6. Is `stream_profile_locked` customer-facing or operator-only?
