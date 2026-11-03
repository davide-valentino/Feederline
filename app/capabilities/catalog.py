"""Code-defined capability and quality-preset catalog (P0 decision D3).

The catalog is the public contract. It deliberately hides the execution enums used by
the delivery kernel behind ``legacy_*`` fields, and it carries region availability,
required fields and plan fragments as data so the customer surface never needs a
hard-coded type matrix.

The field sets and recipes below were validated against the existing Arqade services
(see ``docs/arqade-counterpart-review.md``). Values that are still illustrative are
marked in the docstring of the relevant capability; the *shape* is what P0 ships.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

CATALOG_VERSION = "2026-11-03"

# Product regions are stable labels; AWS regions are execution details. A capability's
# availability matrix is keyed by product region so it survives provider changes.
REGION_GROUPS: dict[str, frozenset[str]] = {
    "eu": frozenset({"eu-west-1", "eu-central-1", "eu-north-1"}),
    "uk": frozenset({"eu-west-2"}),
    "us": frozenset({"us-east-1", "us-west-2"}),
    "ap": frozenset({"ap-southeast-2", "ap-northeast-1"}),
}
REGION_GROUP_BY_REGION: dict[str, str] = {
    region: group for group, regions in REGION_GROUPS.items() for region in regions
}


@dataclass(frozen=True)
class RegionAvailability:
    available: bool
    reason: str | None = None


@dataclass(frozen=True)
class FieldSpec:
    name: str
    label: str
    field_type: str = "string"  # string | integer | boolean | url | cidr
    required: bool = True
    choices: tuple[str, ...] | None = None
    # When set, the field is required only if every key/value pair matches config.
    required_when: dict[str, Any] | None = None
    secret: bool = False
    # "standard" fields are echoed back on customer connections; "provider" fields
    # are accepted on write but are provider/network detail and stay below the line.
    exposure: str = "standard"
    description: str | None = None


@dataclass(frozen=True)
class Capability:
    id: str
    kind: str  # transport | format
    name: str
    mode: str
    regions: dict[str, RegionAvailability]
    required_fields: tuple[FieldSpec, ...] = ()
    constraints: dict[str, Any] = field(default_factory=dict)
    # Values the platform fills in, not the customer (e.g. the protocol under an
    # ``ipsource`` capability).
    fixed_config: dict[str, Any] = field(default_factory=dict)
    legacy_source_type: str | None = None
    legacy_destination_type: str | None = None
    # Fragments for a non-transcoding plan.
    plan_recipe: tuple[str, ...] = ()
    # Extra fragments when the effective preset transcodes.
    transcode_recipe: tuple[str, ...] = ()
    version: int = 1


@dataclass(frozen=True)
class Rendition:
    name: str
    width: int
    height: int
    bitrate_bps: int


@dataclass(frozen=True)
class QualityPreset:
    id: str
    name: str
    description: str
    ladder: tuple[Rendition, ...]
    transcode: bool
    version: int = 1
    # ``None`` means passthrough: reuse the source profile (stream_profile_id NULL in
    # the execution kernel) rather than resolving a bundled profile.
    profile: dict[str, Any] | None = None
    audio_tracks: int | None = None


def _regions(available: tuple[str, ...] = ("eu", "uk", "us", "ap"), reason: str | None = None):
    return {
        group: RegionAvailability(group in available, None if group in available else reason)
        for group in ("eu", "uk", "us", "ap")
    }


# Shared field specs. Names match the existing Arqade payloads where they overlap.
_MAX_BITRATE = FieldSpec(
    "max_bitrate_bps", "Max bitrate (bps)", "integer", required=False, exposure="provider"
)
_CRYPTO_ALGO = FieldSpec(
    "crypto_algo",
    "Encryption algorithm",
    required=False,
    choices=("aes128", "aes192", "aes256"),
    exposure="provider",
)
_CRYPTO_KEY = FieldSpec(
    "crypto_key", "Encryption key", required=False, secret=True, exposure="provider"
)
_PORT = FieldSpec("port", "Port", "integer", exposure="provider")
_STREAM_ID = FieldSpec("stream_id", "Stream ID", required=False, exposure="provider")
_WHITELIST = FieldSpec(
    "whitelist_cidr", "Whitelist CIDR", "cidr", exposure="provider"
)
_MIN_LATENCY = FieldSpec("min_latency_ms", "Minimum latency (ms)", "integer")
_MAX_LATENCY = FieldSpec("max_latency_ms", "Maximum latency (ms)", "integer", required=False)

TRANSPORTS: tuple[Capability, ...] = (
    Capability(
        id="srt-listener",
        kind="transport",
        name="SRT Listener",
        mode="listener",
        regions=_regions(),
        required_fields=(_PORT, _MIN_LATENCY, _WHITELIST, _MAX_BITRATE, _CRYPTO_KEY),
        constraints={"encryption": "srt-password"},
        legacy_source_type="srtlistenersource",
        plan_recipe=("mediaconnect_flow", "srt_listener_input"),
    ),
    Capability(
        id="srt-caller",
        kind="transport",
        name="SRT Caller",
        mode="caller",
        regions=_regions(("eu", "uk", "us"), "SRT caller is not yet certified in AP"),
        required_fields=(
            FieldSpec("listener_address", "Listener address", "string"),
            _PORT,
            _MIN_LATENCY,
            _STREAM_ID,
            _MAX_BITRATE,
            _CRYPTO_KEY,
        ),
        constraints={"encryption": "srt-password"},
        legacy_source_type="srtcallersource",
        plan_recipe=("mediaconnect_flow", "srt_caller_source"),
    ),
    Capability(
        id="rtp",
        kind="transport",
        name="RTP",
        mode="push",
        regions=_regions(),
        required_fields=(_PORT, _WHITELIST, _MAX_BITRATE),
        constraints={"protocol": "rtp"},
        fixed_config={"protocol": "rtp"},
        legacy_source_type="ipsource",
        plan_recipe=("mediaconnect_flow", "rtp_input"),
    ),
    Capability(
        id="rist",
        kind="transport",
        name="RIST",
        mode="push",
        regions=_regions(),
        required_fields=(_PORT, _WHITELIST, _MAX_BITRATE),
        constraints={"protocol": "rist"},
        fixed_config={"protocol": "rist"},
        # RIST is not a source type of its own in the execution kernel: it is an
        # ``ipsource`` with ``protocol=rist``.
        legacy_source_type="ipsource",
        plan_recipe=("mediaconnect_flow", "rist_input"),
    ),
    Capability(
        id="zixi",
        kind="transport",
        name="Zixi",
        mode="push",
        regions=_regions(("eu", "uk", "us"), "Zixi is not yet offered in AP"),
        required_fields=(
            _PORT,
            _WHITELIST,
            _STREAM_ID,
            _MAX_LATENCY,
            _MAX_BITRATE,
            _CRYPTO_ALGO,
            _CRYPTO_KEY,
        ),
        constraints={"encryption": "optional"},
        legacy_source_type="zixipushsource",
        plan_recipe=("mediaconnect_flow", "zixi_push_source"),
    ),
    Capability(
        id="mediaconnect",
        kind="transport",
        name="MediaConnect",
        mode="managed",
        regions=_regions(),
        required_fields=(
            FieldSpec("entitlement_arn", "Entitlement ARN", "string", exposure="provider"),
            _CRYPTO_ALGO,
            _CRYPTO_KEY,
        ),
        constraints={"managed_by": "aws", "flow_arn": "system-generated"},
        legacy_source_type="mediaconnectsource",
        plan_recipe=("mediaconnect_flow", "mediaconnect_entitlement_source"),
    ),
)

FORMATS: tuple[Capability, ...] = (
    Capability(
        id="linear",
        kind="format",
        name="Linear / IP contribution",
        mode="push",
        regions=_regions(),
        required_fields=(
            FieldSpec("ip_address", "IP address", "string"),
            _PORT,
            FieldSpec(
                "ip_destination_type",
                "IP protocol",
                choices=("rtp", "rtp-fec", "rist"),
            ),
        ),
        constraints={"requires_transcode": False},
        legacy_destination_type="ipdestination",
        plan_recipe=("mediaconnect_flow_output", "ip_output"),
        transcode_recipe=("medialive_udp_input", "medialive_channel", "transcode_flow"),
    ),
    Capability(
        id="srt",
        kind="format",
        name="SRT output (caller)",
        mode="caller",
        regions=_regions(),
        required_fields=(
            FieldSpec("destination_address", "Destination address", "string"),
            _PORT,
            _MIN_LATENCY,
            _STREAM_ID,
            _CRYPTO_KEY,
        ),
        constraints={"requires_transcode": False, "encryption": "srt-password"},
        legacy_destination_type="srtcallerdestination",
        plan_recipe=("mediaconnect_flow_output", "srt_caller_output"),
        transcode_recipe=("medialive_channel", "transcode_flow"),
    ),
    Capability(
        id="srt-listener-output",
        kind="format",
        name="SRT output (listener)",
        mode="listener",
        regions=_regions(),
        required_fields=(_PORT, _MIN_LATENCY, _WHITELIST, _CRYPTO_KEY),
        constraints={"requires_transcode": False, "encryption": "srt-password"},
        legacy_destination_type="srtlistenerdestination",
        plan_recipe=("mediaconnect_flow_output", "srt_listener_output"),
        transcode_recipe=("medialive_channel", "transcode_flow"),
    ),
    Capability(
        id="rtmp",
        kind="format",
        name="RTMP",
        mode="push",
        regions=_regions(("eu", "uk", "us"), "RTMP is not yet offered in AP"),
        required_fields=(
            FieldSpec("url", "Stream URL", "url"),
            FieldSpec("stream_name", "Stream key", "string", secret=True, exposure="provider"),
        ),
        constraints={"requires_transcode": True, "audio_tracks": 1},
        legacy_destination_type="rtmpdestination",
        plan_recipe=("medialive_channel", "medialive_rtmp_group"),
    ),
    Capability(
        id="hls",
        kind="format",
        name="HLS",
        mode="push",
        regions=_regions(),
        required_fields=(
            FieldSpec("url", "Origin URL", "url"),
            FieldSpec(
                "output_destination_type",
                "Output destination",
                choices=("s3", "mediastore", "akamai", "basic_put", "webdav", "mediapackage"),
                exposure="provider",
            ),
            FieldSpec("name_modifier", "Name modifier", "string"),
            FieldSpec(
                "cdn_settings_type",
                "CDN settings",
                choices=("none", "akamai", "basic_put", "mediastore", "webdav", "s3"),
                exposure="provider",
            ),
        ),
        constraints={"requires_transcode": True},
        legacy_destination_type="hlsdestination",
        plan_recipe=("medialive_channel", "medialive_hls_group"),
    ),
    Capability(
        id="dash",
        kind="format",
        name="DASH",
        mode="push",
        regions=_regions(("eu", "uk", "us"), "DASH is not yet offered in AP"),
        required_fields=(
            FieldSpec("egress_access", "Egress access", choices=("public", "cdn")),
            FieldSpec(
                "cdn_distribution_arn",
                "CDN distribution ARN",
                required=False,
                required_when={"egress_access": "cdn"},
                exposure="provider",
            ),
        ),
        constraints={"requires_transcode": True},
        legacy_destination_type="dashdestination",
        # DASH is a MediaPackage v2 channel plus an origin endpoint; the playback URL
        # is derived, so there is no customer-supplied target.
        plan_recipe=(
            "medialive_channel",
            "mediapackage_v2_channel",
            "mediapackage_origin_endpoint",
            "mediapackage_endpoint_policy",
        ),
    ),
    Capability(
        id="archive",
        kind="format",
        name="S3 archive",
        mode="push",
        regions=_regions(),
        required_fields=(
            FieldSpec("bucket_name", "Bucket name", "string", exposure="provider"),
            FieldSpec("folder_path", "Folder path", "string", exposure="provider"),
            FieldSpec("role_arn", "Role ARN", "string", required=False, exposure="provider"),
        ),
        constraints={"requires_transcode": True},
        legacy_destination_type="s3archivedestination",
        plan_recipe=("medialive_channel", "medialive_archive_group", "s3_archive"),
    ),
    Capability(
        id="mediashuttle",
        kind="format",
        name="MediaShuttle archive",
        mode="push",
        regions=_regions(),
        required_fields=(),
        constraints={"requires_transcode": True, "derived_from_org": True},
        legacy_destination_type="mediashuttledestination",
        plan_recipe=(
            "medialive_channel",
            "medialive_archive_group",
            "mediashuttle_transfer",
        ),
    ),
    Capability(
        id="mediaconnect-output",
        kind="format",
        name="MediaConnect entitlement",
        mode="managed",
        regions=_regions(),
        required_fields=(
            FieldSpec("account_id", "Target account ID", "string", exposure="provider"),
            _CRYPTO_ALGO,
            _CRYPTO_KEY,
        ),
        constraints={"managed_by": "aws", "region": "output.region"},
        legacy_destination_type="mediaconnectdestination",
        plan_recipe=("mediaconnect_flow_output", "mediaconnect_entitlement"),
    ),
    Capability(
        id="zixipush",
        kind="format",
        name="Zixi push output",
        mode="caller",
        regions=_regions(),
        required_fields=(
            FieldSpec("ip_address", "IP address", "string"),
            _PORT,
            _STREAM_ID,
            _MAX_LATENCY,
            _CRYPTO_ALGO,
            _CRYPTO_KEY,
        ),
        constraints={"requires_transcode": False},
        legacy_destination_type="zixipushdestination",
        plan_recipe=("mediaconnect_flow_output", "zixi_push_output"),
    ),
    Capability(
        id="zixipull",
        kind="format",
        name="Zixi pull output",
        mode="push",
        regions=_regions(),
        required_fields=(
            FieldSpec("remote_id", "Remote ID", "string", exposure="provider"),
            _WHITELIST,
            _STREAM_ID,
            _MAX_LATENCY,
            FieldSpec("listener_address", "Listener address", "string", required=False),
            _CRYPTO_ALGO,
            _CRYPTO_KEY,
        ),
        constraints={"requires_transcode": False},
        legacy_destination_type="zixipulldestination",
        plan_recipe=("mediaconnect_flow_output", "zixi_pull_output"),
    ),
)

PRESETS: tuple[QualityPreset, ...] = (
    QualityPreset(
        id="passthrough",
        name="Passthrough",
        description="Reuse the source profile without transcoding.",
        ladder=(),
        transcode=False,
        profile=None,
        audio_tracks=None,
    ),
    QualityPreset(
        id="1080p",
        name="1080p",
        description="Single 1080p rendition with standard adaptive packaging.",
        ladder=(Rendition("1080p", 1920, 1080, 5_000_000),),
        transcode=True,
        profile={
            "id": "profile-1080p",
            "codec": "avc",
            "gop_frames": 50,
            "rate_control": "QVBR",
            "frame_rate": 25,
            "scan_type": "progressive",
            "colour_space": "rec709",
        },
        audio_tracks=4,
    ),
    QualityPreset(
        id="1080p-premium",
        name="1080p Premium",
        description="Five-rung ABR ladder from 1080p to 270p.",
        ladder=(
            Rendition("1080p", 1920, 1080, 6_000_000),
            Rendition("720p", 1280, 720, 3_000_000),
            Rendition("540p", 960, 540, 1_600_000),
            Rendition("360p", 640, 360, 900_000),
            Rendition("270p", 480, 270, 400_000),
        ),
        transcode=True,
        profile={
            "id": "profile-1080p-premium",
            "codec": "avc",
            "gop_frames": 50,
            "rate_control": "QVBR",
            "frame_rate": 25,
            "scan_type": "progressive",
            "colour_space": "rec709",
        },
        audio_tracks=4,
    ),
    QualityPreset(
        id="uhd",
        name="4K UHD",
        description="2160p/1080p ladder. SDR today; HDR is not rendered yet.",
        ladder=(
            Rendition("2160p", 3840, 2160, 20_000_000),
            Rendition("1080p", 1920, 1080, 8_000_000),
        ),
        transcode=True,
        profile={
            "id": "profile-uhd",
            "codec": "avc",
            "gop_frames": 50,
            "rate_control": "QVBR",
            "frame_rate": 25,
            "scan_type": "progressive",
            "colour_space": "rec709",
        },
        audio_tracks=4,
    ),
)

TRANSPORTS_BY_ID = {capability.id: capability for capability in TRANSPORTS}
FORMATS_BY_ID = {capability.id: capability for capability in FORMATS}
CAPABILITIES_BY_ID = {**TRANSPORTS_BY_ID, **FORMATS_BY_ID}
if len(CAPABILITIES_BY_ID) != len(TRANSPORTS) + len(FORMATS):
    raise RuntimeError("capability ids must be unique across transports and formats")
PRESETS_BY_ID = {preset.id: preset for preset in PRESETS}


def get_capability(capability_id: str | None) -> Capability | None:
    return CAPABILITIES_BY_ID.get(capability_id or "")


def get_preset(preset_id: str | None) -> QualityPreset | None:
    return PRESETS_BY_ID.get(preset_id or "")


def region_group(region: str | None) -> str | None:
    return REGION_GROUP_BY_REGION.get(region or "")


def region_availability(capability: Capability, region: str | None) -> RegionAvailability:
    if not region:
        return RegionAvailability(False, "A region is required")
    if region in capability.regions:
        return capability.regions[region]
    group = region_group(region)
    if group is None:
        return RegionAvailability(False, f"Unknown region {region}")
    return capability.regions.get(
        group, RegionAvailability(False, f"{capability.name} is not available in {group}")
    )


def field_is_required(spec: FieldSpec, config: dict[str, Any]) -> bool:
    if spec.required_when is not None:
        return all(config.get(key) == value for key, value in spec.required_when.items())
    return spec.required


# Constraints that are meaningful to a customer. Provider details (managed_by,
# protocol, region bindings) stay in the operator view.
_PUBLIC_CONSTRAINTS = {"requires_transcode", "audio_tracks"}


def field_to_dict(spec: FieldSpec, *, public: bool = False) -> dict[str, Any]:
    data = asdict(spec)
    if not public:
        return data
    # Secret values are write-only; the customer view only needs to know that a
    # password-style input is expected.
    return data


def capability_to_dict(capability: Capability, *, public: bool = False) -> dict[str, Any]:
    if not public:
        data = asdict(capability)
        data["regions"] = {
            group: asdict(availability) for group, availability in capability.regions.items()
        }
        return data
    return {
        "id": capability.id,
        "kind": capability.kind,
        "name": capability.name,
        "mode": capability.mode,
        "version": capability.version,
        "regions": {
            group: asdict(availability)
            for group, availability in capability.regions.items()
        },
        "required_fields": [
            field_to_dict(field_spec, public=True)
            for field_spec in capability.required_fields
        ],
        "constraints": {
            key: value
            for key, value in capability.constraints.items()
            if key in _PUBLIC_CONSTRAINTS
        },
    }


def preset_to_dict(preset: QualityPreset, *, public: bool = False) -> dict[str, Any]:
    data = asdict(preset)
    if public:
        # The codec/GOP/rate-control profile is execution detail; the customer sees
        # the named preset and its rendition ladder.
        data.pop("profile", None)
    return data


def catalog_payload(*, public: bool = False) -> dict[str, Any]:
    return {
        "catalog_version": CATALOG_VERSION,
        "transports": [
            capability_to_dict(capability, public=public) for capability in TRANSPORTS
        ],
        "formats": [
            capability_to_dict(capability, public=public) for capability in FORMATS
        ],
        "quality_presets": [
            preset_to_dict(preset, public=public) for preset in PRESETS
        ],
    }


def public_config(capability: Capability | None, config: dict[str, Any]) -> dict[str, Any]:
    """Return only the customer-safe values of a connection's config.

    Provider identifiers, secrets, ports, CIDRs and platform-filled values are not
    echoed back to the customer; they remain visible on the operator surface.
    """

    if capability is None:
        return {}
    fields = {spec.name: spec for spec in capability.required_fields}
    return {
        name: value
        for name, value in config.items()
        if (spec := fields.get(name)) is not None
        and spec.exposure == "standard"
        and not spec.secret
    }

