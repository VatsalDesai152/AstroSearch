"""Public construction helpers for the AstroSearch library and web app."""

from __future__ import annotations

from typing import Any

import httpx

from app.models import CatalogRegistry, Settings, UnifiedRecord
from app.providers import provider_map
from app.service import CrossmatchService


def build_service(
    *,
    settings: Settings | None = None,
    registry_path: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> CrossmatchService:
    """Create a configured service instance.

    Pass an ``httpx.AsyncClient`` in tests or long-running applications so its
    connection pool can be managed by the caller.
    """

    active_settings = settings or Settings()
    registry = CatalogRegistry(registry_path or active_settings.registry_path)
    providers = provider_map(
        client,
        timeout=active_settings.request_timeout_seconds,
        max_response_bytes=active_settings.max_response_bytes,
    )
    return CrossmatchService(
        registry,
        providers,
        radius_arcsec=active_settings.default_radius_arcsec,
        timeout=active_settings.request_timeout_seconds,
    )


async def crossmatch(
    ra: float | str,
    dec: float | str,
    *,
    radius_arcsec: float | None = None,
    settings: Settings | None = None,
) -> UnifiedRecord:
    """Run one cross-match using a freshly configured service."""

    async with httpx.AsyncClient(timeout=(settings or Settings()).request_timeout_seconds, follow_redirects=True) as client:
        service = build_service(settings=settings, client=client)
        return await service.crossmatch(ra, dec, radius_arcsec=radius_arcsec)


def catalog_definitions(*, settings: Settings | None = None) -> dict[str, Any]:
    """Return the configured catalog definitions for inspection or reporting."""

    active_settings = settings or Settings()
    return CatalogRegistry(active_settings.registry_path).catalogs


__all__ = ['build_service', 'catalog_definitions', 'crossmatch']
