"""Coordinate validation, querying, matching, and unified-record assembly."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from time import time
from typing import Any

from astropy import units as u
from astropy.coordinates import SkyCoord
from astropy.time import Time

from app.models import (
    CatalogDefinition,
    CatalogFailure,
    CatalogRegistry,
    CatalogSource,
    CatalogUnavailableError,
    Match,
    QueryPlan,
    QueryTimeoutError,
    Target,
    UnifiedRecord,
    validate_target,
)
from app.providers import CatalogProvider


def _source_coordinate(source: CatalogSource, epoch: float | None = None) -> SkyCoord:
    coordinate = SkyCoord(ra=source.ra * u.deg, dec=source.dec * u.deg, frame='icrs')
    if (
        epoch is not None
        and source.epoch is not None
        and source.proper_motion_ra_masyr is not None
        and source.proper_motion_dec_masyr is not None
    ):
        try:
            moving = SkyCoord(
                ra=source.ra * u.deg,
                dec=source.dec * u.deg,
                pm_ra_cosdec=source.proper_motion_ra_masyr * u.mas / u.yr,
                pm_dec=source.proper_motion_dec_masyr * u.mas / u.yr,
                obstime=Time(source.epoch, format='jyear'),
                frame='icrs',
            )
            coordinate = moving.apply_space_motion(new_obstime=Time(epoch, format='jyear'))
        except (TypeError, ValueError, u.UnitConversionError):
            pass
    return coordinate


def angular_separation_arcsec(target: Target, source: CatalogSource | Target) -> float:
    target_coord = SkyCoord(ra=target.ra * u.deg, dec=target.dec * u.deg, frame=target.frame)
    source_coord = _source_coordinate(source, target.epoch) if isinstance(source, CatalogSource) else SkyCoord(ra=source.ra * u.deg, dec=source.dec * u.deg, frame='icrs')
    return float(target_coord.separation(source_coord).to(u.arcsec).value)


def match_score(
    separation_arcsec: float,
    *,
    positional_error_arcsec: float | None = None,
    target_uncertainty_arcsec: float | None = None,
) -> float:
    if separation_arcsec < 0:
        return 0.0
    if positional_error_arcsec is not None or target_uncertainty_arcsec is not None:
        source_sigma = max(positional_error_arcsec or 0.0, 0.1)
        target_sigma = max(target_uncertainty_arcsec or 0.0, 0.0)
        sigma = math.sqrt(source_sigma**2 + target_sigma**2)
        likelihood = math.exp(-0.5 * (separation_arcsec / sigma) ** 2)
        return round(max(0.0, min(1.0, likelihood)), 6)
    scale = separation_arcsec / 3.0
    return round(max(0.0, 1.0 - min(scale, 10.0) / 10.0), 6)


def match_target(target: Target, sources: list[CatalogSource], radius_arcsec: float) -> list[Match]:
    matches = []
    for source in sources:
        separation = angular_separation_arcsec(target, source)
        if separation <= radius_arcsec:
            matches.append(Match(source.catalog, source, separation, match_score(separation, positional_error_arcsec=source.positional_error_arcsec)))
    return sorted(matches, key=lambda match: match.separation_arcsec)


class QueryCache:
    """Small in-memory TTL cache for callers that want to reuse query results."""

    def __init__(self) -> None:
        self._store: dict[str, tuple[float, Any]] = {}

    @staticmethod
    def make_key(catalog: str, ra: float, dec: float, radius_arcsec: float, query_version: str = '1') -> str:
        payload = json.dumps({'catalog': catalog, 'ra': ra, 'dec': dec, 'radius_arcsec': radius_arcsec, 'query_version': query_version}, sort_keys=True)
        return hashlib.sha256(payload.encode('utf-8')).hexdigest()

    def get(self, key: str) -> Any:
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at <= time():
            self._store.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any, *, ttl_seconds: int = 600) -> None:
        self._store[key] = (time() + ttl_seconds, value)


class QueryPlanner:
    def __init__(self, registry: CatalogRegistry) -> None:
        self.registry = registry

    def plan(self, radius_arcsec: float, profile: str | None = None) -> list[QueryPlan]:
        return [
            QueryPlan(
                catalog=name,
                provider=catalog.provider,
                endpoint=catalog.endpoint,
                parameters={'catalog': catalog.catalog, 'table': catalog.table, **catalog.parameters},
                radius_arcsec=radius_arcsec,
                wavelength=catalog.wavelength,
            )
            for name, catalog in self.registry.enabled_catalogs().items()
            if profile is None or not catalog.profiles or profile in catalog.profiles
        ]


class QueryExecutor:
    def __init__(self, providers: dict[str, CatalogProvider], *, timeout: float = 30.0) -> None:
        self.providers = providers
        self.timeout = timeout

    async def execute(self, plans: list[QueryPlan], target: Target) -> tuple[list[tuple[str, list[CatalogSource]]], list[CatalogFailure]]:
        async def run(plan: QueryPlan) -> tuple[str, list[CatalogSource]]:
            provider = self.providers.get(plan.provider)
            if provider is None:
                raise CatalogUnavailableError(f'No provider configured for {plan.provider}')
            catalog = CatalogDefinition(
                name=plan.catalog,
                provider=plan.provider,
                wavelength=plan.wavelength,
                endpoint=plan.endpoint,
                table=plan.parameters.get('table'),
                catalog=plan.parameters.get('catalog'),
                parameters=dict(plan.parameters),
            )
            try:
                sources = await asyncio.wait_for(provider.query(catalog, target, plan.radius_arcsec), timeout=self.timeout)
            except asyncio.TimeoutError as exc:
                raise QueryTimeoutError(f'Catalog {plan.catalog} timed out after {self.timeout:g}s') from exc
            return plan.catalog, sources

        gathered = await asyncio.gather(*(run(plan) for plan in plans), return_exceptions=True)
        successes: list[tuple[str, list[CatalogSource]]] = []
        failures: list[CatalogFailure] = []
        for plan, item in zip(plans, gathered):
            if isinstance(item, BaseException):
                failures.append(CatalogFailure(plan.catalog, error_type=item.__class__.__name__, message=str(item)))
            else:
                successes.append(item)
        return successes, failures


def _source_dict(match: Match) -> dict[str, Any]:
    source = match.source
    return {
        'catalog': source.catalog,
        'source_id': source.source_id,
        'ra': source.ra,
        'dec': source.dec,
        'separation_arcsec': match.separation_arcsec,
        'confidence': match.confidence,
        'metadata': source.metadata,
        'data': source.data,
        'provenance': source.provenance,
        'positional_error_arcsec': source.positional_error_arcsec,
        'epoch': source.epoch,
        'proper_motion_ra_masyr': source.proper_motion_ra_masyr,
        'proper_motion_dec_masyr': source.proper_motion_dec_masyr,
        'physical': source.metadata.get('physical', {}),
        'links': {name: url for name, url in source.metadata.get('links', {}).items() if url},
    }


def _group_matches(matches: list[Match], target: Target, radius_arcsec: float) -> list[dict[str, Any]]:
    """Group detections that are mutually consistent, using propagated positions."""

    if not matches:
        return []
    parent = list(range(len(matches)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    for left in range(len(matches)):
        for right in range(left + 1, len(matches)):
            first = _source_coordinate(matches[left].source, target.epoch)
            second = _source_coordinate(matches[right].source, target.epoch)
            separation = float(first.separation(second).to(u.arcsec).value)
            allowed = max(radius_arcsec, matches[left].source.positional_error_arcsec or 0.0, matches[right].source.positional_error_arcsec or 0.0)
            if separation <= allowed:
                union(left, right)

    grouped: dict[int, list[Match]] = {}
    for index, match in enumerate(matches):
        grouped.setdefault(find(index), []).append(match)
    result = []
    for group_number, group in enumerate(grouped.values(), start=1):
        result.append({
            'group_id': f'object-{group_number}',
            'catalogs': sorted({match.catalog for match in group}),
            'wavelengths': sorted({str(match.source.metadata.get('wavelength', 'unknown')) for match in group}),
            'members': [_source_dict(match) for match in group],
        })
    return result


class CrossmatchService:
    """Orchestrate catalog queries and return one normalized result object."""

    def __init__(self, registry: CatalogRegistry, providers: dict[str, CatalogProvider], *, radius_arcsec: float = 3.0, timeout: float = 30.0) -> None:
        self.registry = registry
        self.providers = providers
        self.radius_arcsec = radius_arcsec
        self.planner = QueryPlanner(registry)
        self.executor = QueryExecutor(providers, timeout=timeout)

    async def crossmatch(
        self,
        ra: float | str,
        dec: float | str,
        *,
        radius_arcsec: float | None = None,
        epoch: float | None = None,
        profile: str | None = None,
    ) -> UnifiedRecord:
        target = validate_target(ra, dec, epoch=epoch)
        try:
            search_radius = self.radius_arcsec if radius_arcsec is None else float(radius_arcsec)
        except (TypeError, ValueError) as exc:
            raise ValueError('radius_arcsec must be a finite number greater than zero.') from exc
        if not math.isfinite(search_radius) or search_radius <= 0:
            raise ValueError('radius_arcsec must be a finite number greater than zero.')

        plans = self.planner.plan(search_radius, profile=profile)
        successes, failures = await self.executor.execute(plans, target)
        catalog_results = {name: {'sources': sources, 'status': 'success'} for name, sources in successes}
        all_sources = [source for _, sources in successes for source in sources]
        matches = match_target(target, all_sources, search_radius)
        counterparts: dict[str, list[dict[str, Any]]] = {}
        for match in matches:
            wavelength = str(match.source.metadata.get('wavelength', 'unknown'))
            counterparts.setdefault(wavelength, []).append(_source_dict(match))

        failure_dicts = [
            {'catalog': failure.catalog, 'status': failure.status, 'error_type': failure.error_type, 'message': failure.message}
            for failure in failures
        ]
        groups = _group_matches(matches, target, search_radius)
        provenance = {
            'query_radius_arcsec': search_radius,
            'target_epoch': target.epoch,
            'profile': profile,
            'catalogs_planned': [plan.catalog for plan in plans],
            'matches': [
                {'catalog': match.catalog, 'source_id': match.source.source_id, 'separation_arcsec': match.separation_arcsec, 'confidence': match.confidence}
                for match in matches
            ],
        }
        return UnifiedRecord(
            target={'ra': target.ra, 'dec': target.dec, 'frame': target.frame},
            catalogs_queried=len(catalog_results) + len(failure_dicts),
            catalog_results=catalog_results,
            counterparts=counterparts,
            failures=failure_dicts,
            provenance=provenance,
            crossmatch_groups=groups,
        )

    async def crossmatch_many(self, targets: list[dict[str, Any]], *, radius_arcsec: float | None = None, epoch: float | None = None, profile: str | None = None) -> list[UnifiedRecord]:
        return [
            await self.crossmatch(
                target['ra'],
                target['dec'],
                radius_arcsec=target.get('radius_arcsec', radius_arcsec),
                epoch=target.get('epoch', epoch),
                profile=target.get('profile', profile),
            )
            for target in targets
        ]
