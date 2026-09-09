"""Adapters for the public astronomy catalog services."""

from __future__ import annotations

from abc import ABC, abstractmethod
import math
from typing import Any
from urllib.parse import quote

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
            error_value = normalized.get('position_uncertainty_arcsec')
            if error_value is None and positional_error_key:
                error_value = row.get(positional_error_key)
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
                        'physical': {
                            key: normalized[key]
                            for key in ('parallax', 'redshift', 'object_type', 'quality_flags')
                            if key in normalized
                        },
                        'links': {
                            'SIMBAD': f'https://simbad.cds.unistra.fr/simbad/sim-id?Ident={quote(source_id)}'
                            if catalog.name == 'simbad' else None,
                            'NED': f'https://ned.ipac.caltech.edu/byname?objname={quote(source_id)}'
                            if catalog.name == 'ned' else None,
                            'MAST images / spectra / light curves': f'https://mast.stsci.edu/portal/Mashup/Clients/Mast/Portal.html?searchQuery={ra}%20{dec}'
                            if catalog.provider == 'mast' else None,
                            'IRSA finder chart': f'https://irsa.ipac.caltech.edu/applications/finderchart/servlet/api?locstr={ra}%20{dec}'
                            if catalog.provider == 'irsa_gator' else None,
                            'Legacy Survey image cutout': f'https://www.legacysurvey.org/viewer/fits-cutout?ra={ra}&dec={dec}&pixscale=0.262&bands=griz'
                            if catalog.wavelength in {'optical', 'extragalactic'} else None,
                        },
                    },
                    provenance=build_provenance(
                        catalog.name,
                        provider=self.provider_name,
                        source_id=source_id,
                        endpoint=endpoint,
                        query_parameters=parameters,
                        search_radius_arcsec=radius_arcsec,
                    ),
                    epoch=normalized.get('epoch'),
                    proper_motion_ra_masyr=normalized.get('pmra'),
                    proper_motion_dec_masyr=normalized.get('pmdec'),
                    position_uncertainty_arcsec=positional_error,
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
        parameters = catalog.parameters
        columns = parameters.get('columns') or ['source_id', 'ra', 'dec']
        if isinstance(columns, str):
            columns = [item.strip() for item in columns.split(',') if item.strip()]
        columns = [str(column) for column in columns]
        ra_field = str(parameters.get('ra_field', 'ra'))
        dec_field = str(parameters.get('dec_field', 'dec'))
        id_field = str(parameters.get('id_field', 'source_id'))
        positional_error_field = parameters.get('positional_error_field')
        if ra_field not in columns:
            columns.append(ra_field)
        if dec_field not in columns:
            columns.append(dec_field)
        if id_field not in columns:
            columns.append(id_field)
        adql = (
            'SELECT TOP 100 ' + ', '.join(columns) + ' FROM '
            f"{catalog.table or 'gaiadr3.gaia_source'} "
            "WHERE CONTAINS(POINT('ICRS', ra, dec), CIRCLE('ICRS', "
            f'{target.ra}, {target.dec}, {radius_deg})) = 1'
        )
        # Tables with non-standard coordinate column names need those names in
        # the geometric predicate as well.
        adql = adql.replace("POINT('ICRS', ra, dec)", f"POINT('ICRS', {ra_field}, {dec_field})")
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
        mapped_rows = []
        for row in rows[:100]:
            mapped = dict(row)
            if ra_field in mapped:
                mapped['ra'] = mapped[ra_field]
            if dec_field in mapped:
                mapped['dec'] = mapped[dec_field]
            if id_field in mapped:
                mapped['source_id'] = mapped[id_field]
            mapped_rows.append(mapped)
        return self._sources(catalog, mapped_rows, radius_arcsec, endpoint, params, positional_error_field or 'ra_error')


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
