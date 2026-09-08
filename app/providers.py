"""Adapters for the public astronomy catalog services."""

from __future__ import annotations

from abc import ABC, abstractmethod
import math
from typing import Any

import httpx

from app.models import (
    CatalogDefinition,
    CatalogQueryError,
    CatalogSource,
    ResponseParseError,
    Target,
    build_provenance,
    normalize_source_record,
    parse_csv_records,
    parse_ipac_records,
    parse_json_records,
    parse_votable_records,
)


class CatalogProvider(ABC):
    @abstractmethod
    async def query(self, catalog: CatalogDefinition, target: Target, radius_arcsec: float) -> list[CatalogSource]:
        raise NotImplementedError


class _HTTPProvider(CatalogProvider):
    provider_name = 'unknown'

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        timeout: float = 30.0,
        max_response_bytes: int = 10_000_000,
    ) -> None:
        self.client = client or httpx.AsyncClient(timeout=timeout, follow_redirects=True)
        self.max_response_bytes = max_response_bytes

    def _sources(
        self,
        catalog: CatalogDefinition,
        rows: list[dict[str, Any]],
        radius_arcsec: float,
        endpoint: str | None,
        parameters: dict[str, Any],
        positional_error_key: str | None = None,
    ) -> list[CatalogSource]:
        sources: list[CatalogSource] = []
        for row in rows:
            try:
                normalized = normalize_source_record(row)
            except (TypeError, ValueError):
                continue
            if 'ra' not in normalized or 'dec' not in normalized:
                continue
            try:
                ra = float(str(normalized['ra'])) % 360.0
                dec = float(str(normalized['dec']))
            except (TypeError, ValueError):
                continue
            if not math.isfinite(ra) or not math.isfinite(dec) or not -90.0 <= dec <= 90.0:
                continue
            source_id = str(normalized.get('source_id', f'{catalog.name}-{len(sources)}'))
            error_value = row.get(positional_error_key) if positional_error_key else None
            try:
                positional_error = float(str(error_value)) if error_value not in (None, '') else None
            except (TypeError, ValueError):
                positional_error = None
            sources.append(
                CatalogSource(
                    catalog=catalog.name,
                    source_id=source_id,
                    ra=ra,
                    dec=dec,
                    positional_error_arcsec=positional_error,
                    data=dict(row),
                    metadata={
                        'wavelength': catalog.wavelength,
                        'table': catalog.table,
                        'catalog': catalog.catalog,
                    },
                    provenance=build_provenance(
                        catalog.name,
                        provider=self.provider_name,
                        source_id=source_id,
                        endpoint=endpoint,
                        query_parameters=parameters,
                        search_radius_arcsec=radius_arcsec,
                    ),
                )
            )
        return sources

    @staticmethod
    def _check(response: httpx.Response, provider: str) -> None:
        if response.status_code >= 400:
            raise CatalogQueryError(f'{provider} query failed: HTTP {response.status_code}')

    def _check_size(self, response: httpx.Response, provider: str) -> None:
        if len(response.content) > self.max_response_bytes:
            raise CatalogQueryError(
                f'{provider} response exceeded the {self.max_response_bytes} byte limit.'
            )


class TapProvider(_HTTPProvider):
    provider_name = 'tap'

    async def query(self, catalog: CatalogDefinition, target: Target, radius_arcsec: float) -> list[CatalogSource]:
        endpoint = catalog.endpoint or 'https://gea.esac.esa.int/tap-server/tap/sync'
        radius_deg = radius_arcsec / 3600.0
        adql = (
            'SELECT TOP 100 source_id, ra, dec, ra_error, dec_error FROM '
            f"{catalog.table or 'gaiadr3.gaia_source'} "
            "WHERE CONTAINS(POINT('ICRS', ra, dec), CIRCLE('ICRS', "
            f'{target.ra}, {target.dec}, {radius_deg})) = 1'
        )
        params = {'REQUEST': 'doQuery', 'LANG': 'ADQL', 'FORMAT': 'json', 'QUERY': adql}
        response = await self.client.get(endpoint, params=params)
        self._check(response, 'TAP')
        self._check_size(response, 'TAP')
        try:
            content_type = response.headers.get('content-type', '').lower()
            if 'votable' in content_type or 'xml' in content_type:
                rows = parse_votable_records(response.content)
            elif 'csv' in content_type:
                rows = parse_csv_records(response.text)
            else:
                rows = parse_json_records(response.text)
        except Exception as exc:
            raise ResponseParseError(f'TAP response could not be parsed: {exc}') from exc
        return self._sources(catalog, rows[:100], radius_arcsec, endpoint, params, 'ra_error')


class IRSAGatorProvider(_HTTPProvider):
    provider_name = 'irsa_gator'

    async def query(self, catalog: CatalogDefinition, target: Target, radius_arcsec: float) -> list[CatalogSource]:
        endpoint = catalog.endpoint or 'https://irsa.ipac.caltech.edu/cgi-bin/Gator/nph-query'
        params = {
            'catalog': catalog.catalog or catalog.name,
            'spatial': 'cone',
            'objstr': f'{target.ra} {target.dec}',
            'radius': str(radius_arcsec),
            'radunits': 'arcsec',
            # Gator's documented machine-readable output is an IPAC ASCII
            # table, not JSON. ``selcols`` is optional; keep the full row so
            # photometry and catalog-specific metadata remain available.
            'outfmt': '1',
        }
        response = await self.client.get(endpoint, params=params)
        self._check(response, 'IRSA')
        self._check_size(response, 'IRSA')
        try:
            rows = parse_ipac_records(response.content)
        except Exception as exc:
            raise ResponseParseError(f'IRSA response could not be parsed: {exc}') from exc
        return self._sources(catalog, rows, radius_arcsec, endpoint, params)


class MASTProvider(_HTTPProvider):
    provider_name = 'mast'

    async def query(self, catalog: CatalogDefinition, target: Target, radius_arcsec: float) -> list[CatalogSource]:
        endpoint = catalog.endpoint or 'https://catalogs.mast.stsci.edu/api/v0.1/panstarrs/dr2/mean'
        params = {'ra': str(target.ra), 'dec': str(target.dec), 'radius': str(radius_arcsec / 3600.0)}
        response = await self.client.get(endpoint, params=params)
        self._check(response, 'MAST')
        self._check_size(response, 'MAST')
        try:
            rows = parse_json_records(response.json())
        except Exception as exc:
            raise ResponseParseError(f'MAST response could not be parsed: {exc}') from exc
        return self._sources(catalog, rows, radius_arcsec, endpoint, params, 'raError')


class SDSSProvider(_HTTPProvider):
    provider_name = 'sdss'

    async def query(self, catalog: CatalogDefinition, target: Target, radius_arcsec: float) -> list[CatalogSource]:
        endpoint = catalog.endpoint or 'https://skyserver.sdss.org/dr18/SkyServerWS/ConeSearch/ConeSearchService'
        # SDSS Cone Search expects the radius in arcminutes.
        params = {'format': 'csv', 'ra': str(target.ra), 'dec': str(target.dec), 'sr': str(radius_arcsec / 60.0)}
        response = await self.client.get(endpoint, params=params)
        self._check(response, 'SDSS')
        self._check_size(response, 'SDSS')
        try:
            rows = parse_csv_records(response.text)
        except Exception as exc:
            raise ResponseParseError(f'SDSS response could not be parsed: {exc}') from exc
        return self._sources(catalog, rows, radius_arcsec, endpoint, params)


class HEASARCXaminProvider(_HTTPProvider):
    provider_name = 'heasarc_xamin'

    async def query(self, catalog: CatalogDefinition, target: Target, radius_arcsec: float) -> list[CatalogSource]:
        endpoint = catalog.endpoint or 'https://heasarc.gsfc.nasa.gov/xamin/query'
        params = {
            'table': catalog.table or catalog.name,
            'coord': f'{target.ra},{target.dec}',
            'radius': str(radius_arcsec),
            'format': 'json',
        }
        response = await self.client.get(endpoint, params=params)
        self._check(response, 'HEASARC Xamin')
        self._check_size(response, 'HEASARC Xamin')
        try:
            rows = parse_json_records(response.json())
        except Exception as exc:
            raise ResponseParseError(f'HEASARC Xamin response could not be parsed: {exc}') from exc
        return self._sources(catalog, rows, radius_arcsec, endpoint, params, 'poserr')


def provider_map(
    client: httpx.AsyncClient | None = None,
    *,
    timeout: float = 30.0,
    max_response_bytes: int = 10_000_000,
) -> dict[str, CatalogProvider]:
    """Build the provider map used by :class:`CrossmatchService`."""

    return {
        'tap': TapProvider(client, timeout=timeout, max_response_bytes=max_response_bytes),
        'irsa_gator': IRSAGatorProvider(client, timeout=timeout, max_response_bytes=max_response_bytes),
        'mast': MASTProvider(client, timeout=timeout, max_response_bytes=max_response_bytes),
        'sdss': SDSSProvider(client, timeout=timeout, max_response_bytes=max_response_bytes),
        'heasarc_xamin': HEASARCXaminProvider(client, timeout=timeout, max_response_bytes=max_response_bytes),
    }
