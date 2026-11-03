"""Derive a read-only execution plan from catalog recipes.

A plan is always derived server-side. Clients never supply one, and no price or
commercial field belongs here (P0 decision #6 keeps a seam only).

The workflow key mirrors the existing ``ARQADE_WORKFLOW_TYPE`` shape: a family
(``hls`` / ``dash`` / ``s3`` / ``rtmp`` / ``direct``), the region scope (same region or
cross-region with router) and the resilience suffix. Plan fragments are the resource
steps the media pipeline already performs, not invented abstractions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.capabilities.catalog import Capability, QualityPreset

# Real workflow families: OTT packagers keep their name, archive collapses to s3, and
# every direct transport/output (linear, SRT, MediaConnect, Zixi, RIST) is "direct".
_WORKFLOW_FAMILIES = {
    "hls": "hls",
    "dash": "dash",
    "archive": "s3",
    "mediashuttle": "s3",
    "rtmp": "rtmp",
}


@dataclass(frozen=True)
class PlanLeg:
    output_id: str
    capability: Capability
    region: str
    redundant_capability: Capability | None = None
    redundant_region: str | None = None


def _estimated_start(resilience: str, transcode: bool) -> str:
    if resilience == "geo-redundant":
        return "60-90s"
    if resilience == "redundant":
        return "45-70s"
    if transcode:
        return "35-50s"
    return "25-40s"


def _workflow(family: str, cross_region: bool, resilience: str) -> str:
    scope = "cross_region_with_router" if cross_region else "same_region"
    suffix = "single" if resilience == "single" else "redundant"
    return f"{family}_{scope}_{suffix}"


def _audio_tracks(preset: QualityPreset, legs: list[PlanLeg]) -> int | None:
    # A format may pin the track count (RTMP currently forces one audio track).
    for leg in legs:
        override = leg.capability.constraints.get("audio_tracks")
        if isinstance(override, int):
            return override
    return preset.audio_tracks


def resolve_plan(
    *,
    feed: Capability,
    feed_region: str,
    redundant_feed: Capability | None,
    redundant_feed_region: str | None,
    legs: list[PlanLeg],
    preset: QualityPreset,
    effective_preset: QualityPreset,
    resilience: str,
    version: int = 1,
) -> dict[str, Any]:
    resources: list[str] = []

    def add(value: str) -> None:
        if value not in resources:
            resources.append(value)

    for fragment in feed.plan_recipe:
        add(f"feed:{feed.id}:{fragment}")
    if redundant_feed is not None:
        for fragment in redundant_feed.plan_recipe:
            add(f"redundant-feed:{redundant_feed.id}:{fragment}")

    primary = legs[0]
    cross_region = feed_region != primary.region
    if cross_region:
        add("router:mediaconnect_router")

    transcode = effective_preset.transcode
    profile = effective_preset.profile
    if transcode:
        add(f"transcode:{effective_preset.id}")
        if profile is not None:
            add(f"stream_profile:{profile['id']}")
        for rendition in effective_preset.ladder:
            add(f"rendition:{rendition.name}")
        tracks = _audio_tracks(effective_preset, legs)
        if tracks is not None:
            add(f"audio:{tracks}")

    for leg in legs:
        for fragment in leg.capability.plan_recipe:
            add(f"output:{leg.output_id}:{fragment}")
        if transcode:
            for fragment in leg.capability.transcode_recipe:
                add(f"output:{leg.output_id}:{fragment}")
        if leg.redundant_capability is not None:
            for fragment in leg.redundant_capability.plan_recipe:
                add(f"redundant-output:{leg.output_id}:{fragment}")
            if transcode:
                for fragment in leg.redundant_capability.transcode_recipe:
                    add(f"redundant-output:{leg.output_id}:{fragment}")

    family = _WORKFLOW_FAMILIES.get(primary.capability.id, "direct")
    return {
        "provider": "aws",
        "workflow": _workflow(family, cross_region, resilience),
        "region": primary.region,
        "resources": resources,
        "estimated_start": _estimated_start(resilience, transcode),
        "version": version,
        "resolved_at": datetime.now(timezone.utc).isoformat(),
        "resilience": resilience,
        "quality_preset": preset.id,
        "effective_quality_preset": effective_preset.id,
        "stream_profile": profile,
        "audio_tracks": _audio_tracks(effective_preset, legs),
        "cross_region": cross_region,
        "feed_region": feed_region,
        "redundant_feed_region": redundant_feed_region,
        # Seam only: a future pricing service consumes this plan; no price is emitted.
        "pricing": None,
    }
