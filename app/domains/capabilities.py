"""Read-only capability and quality-preset catalog endpoints (P0 decision D3)."""

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.capabilities.catalog import (
    CATALOG_VERSION,
    PRESETS,
    capability_to_dict,
    catalog_payload,
    get_capability,
    preset_to_dict,
)

router = APIRouter(tags=["capabilities"])


@router.get("/v1/capabilities")
def list_capabilities(
    kind: str | None = Query(default=None),
) -> dict[str, Any]:
    payload = catalog_payload(public=True)
    if kind is None:
        return payload
    if kind not in {"transport", "format"}:
        raise HTTPException(status_code=422, detail="kind must be transport or format")
    return {
        "catalog_version": CATALOG_VERSION,
        "transports": payload["transports"] if kind == "transport" else [],
        "formats": payload["formats"] if kind == "format" else [],
        "quality_presets": payload["quality_presets"],
    }


@router.get("/v1/capabilities/{capability_id}")
def get_capability_endpoint(capability_id: str) -> dict[str, Any]:
    capability = get_capability(capability_id)
    if capability is None:
        raise HTTPException(status_code=404, detail="Capability not found")
    return capability_to_dict(capability, public=True)


@router.get("/v1/quality-presets")
def list_quality_presets() -> dict[str, Any]:
    return {
        "catalog_version": CATALOG_VERSION,
        "quality_presets": [preset_to_dict(preset, public=True) for preset in PRESETS],
    }
