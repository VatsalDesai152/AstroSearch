from __future__ import annotations

import asyncio
import json
import threading
import urllib.request

import httpx
import pytest

from app.main import build_service
from app.models import (
    CatalogDefinition,
    CatalogQueryError,
    CatalogRegistry,
    CatalogSource,
    InvalidCoordinateError,
    QueryPlan,
    QueryTimeoutError,
    Settings,
    Target,
    UnifiedRecord,
    normalize_source_record,
    parse_ipac_records,
    validate_target,
)
from app.providers import CatalogProvider, IRSAGatorProvider, MASTProvider, SDSSProvider
from app.service import CrossmatchService, QueryExecutor, angular_separation_arcsec, match_score, match_target
from app.web import create_server, parse_crossmatch_request


def test_validate_target_normalizes_ra_and_rejects_invalid_dec() -> None:
    assert validate_target(361.0, 10.0).ra == 1.0
    with pytest.raises(InvalidCoordinateError):
        validate_target(12.3, 91.0)


def test_angular_separation_is_in_arcseconds() -> None:
    assert 0 < angular_separation_arcsec(Target(0.0, 0.0), Target(0.0, 1.0)) <= 3600


def test_matching_filters_and_scores_sources() -> None:
    target = Target(10.0, 5.0)
    sources = [
        CatalogSource('gaia', '1', 10.0, 5.0, 0.1, {}, {'wavelength': 'optical'}, {}),
        CatalogSource('wise', '2', 10.2, 5.1, 0.2, {}, {'wavelength': 'infrared'}, {}),
    ]
    assert len(match_target(target, sources, radius_arcsec=3600)) == 2
    assert 0.0 <= match_score(10.0) <= 1.0


def test_normalization_handles_provider_aliases() -> None:
    normalized = normalize_source_record({'RAJ2000': 12.5, 'DEJ2000': -4.2, 'designation': 'J0001'})
    assert normalized == {'ra': 12.5, 'dec': -4.2, 'source_id': 'J0001'}


def test_parse_ipac_records_handles_irsa_gator_output() -> None:
    payload = """
\\fixlen = T
\\RowsRetrieved = 1
| ra        | dec       | designation |
| double    | double    | char        |
|           |           |             |
 10.123456   -5.654321   J001
"""
    rows = parse_ipac_records(payload)
    assert rows[0]['designation'] == 'J001'
    assert float(rows[0]['ra']) == pytest.approx(10.123456)


def test_registry_and_service_are_configurable() -> None:
    service = build_service(settings=Settings(CATALOG_REGISTRY_PATH='app/registry.yaml'))
    assert 'gaia_dr3' in service.registry.enabled_catalogs()
    assert 'sdss' in service.registry.enabled_catalogs()


def test_web_request_parser_requires_coordinates_and_accepts_radius() -> None:
    assert parse_crossmatch_request({'ra': '10', 'dec': '-5', 'radius_arcsec': '2.5'}) == ('10', '-5', 2.5)
    with pytest.raises(ValueError, match='ra and dec'):
        parse_crossmatch_request({'ra': 10})


def test_web_server_serves_ui_and_api(monkeypatch) -> None:
    async def fake_crossmatch(ra, dec, *, radius_arcsec=None, settings=None):
        return UnifiedRecord(
            target={'ra': float(ra), 'dec': float(dec), 'frame': 'icrs'},
            catalogs_queried=0,
            catalog_results={},
            counterparts={},
            failures=[],
            provenance={'query_radius_arcsec': radius_arcsec, 'matches': []},
        )

    monkeypatch.setattr('app.web.crossmatch', fake_crossmatch)
    server = create_server(port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f'http://127.0.0.1:{server.server_address[1]}'
    try:
        with urllib.request.urlopen(base_url, timeout=2) as response:
            assert response.status == 200
            assert b'AstroSearch' in response.read()
        request = urllib.request.Request(
            f'{base_url}/api/crossmatch',
            data=json.dumps({'ra': 10, 'dec': 5, 'radius_arcsec': 3}).encode(),
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            assert json.loads(response.read())['target']['ra'] == 10.0
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


class _StaticProvider(CatalogProvider):
    async def query(self, catalog: CatalogDefinition, target: Target, radius_arcsec: float) -> list[CatalogSource]:
        return [CatalogSource(catalog.name, 'source-1', target.ra, target.dec, None, {}, {'wavelength': catalog.wavelength}, {})]


class _SlowProvider(CatalogProvider):
    async def query(self, catalog: CatalogDefinition, target: Target, radius_arcsec: float) -> list[CatalogSource]:
        await asyncio.sleep(0.05)
        return []


@pytest.mark.asyncio
async def test_crossmatch_preserves_catalog_metadata_from_registry(tmp_path) -> None:
    registry_path = tmp_path / 'registry.yaml'
    registry_path.write_text(
        'catalogs:\n  demo:\n    enabled: true\n    provider: static\n    wavelength: optical\n    parameters:\n      survey: test\n',
        encoding='utf-8',
    )
    registry = CatalogRegistry(registry_path)
    service = CrossmatchService(registry, {'static': _StaticProvider()}, radius_arcsec=3.0)

    result = await service.crossmatch(10.0, 5.0)

    assert list(result.counterparts) == ['optical']


@pytest.mark.asyncio
async def test_query_timeout_is_reported_with_library_exception() -> None:
    executor = QueryExecutor({'slow': _SlowProvider()}, timeout=0.001)
    plans = [QueryPlan('demo', 'slow', None, {}, 1.0, wavelength='optical')]

    _, failures = await executor.execute(plans, Target(10.0, 5.0))

    assert failures[0].error_type == QueryTimeoutError.__name__


@pytest.mark.asyncio
async def test_provider_discards_invalid_coordinates_and_enforces_response_limit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={'content-type': 'application/json'},
            content=json.dumps([
                {'ra': 10.0, 'dec': 5.0, 'objID': 'valid'},
                {'ra': 10.0, 'dec': 91.0, 'objID': 'invalid-dec'},
                {'ra': 'not-a-number', 'dec': 5.0, 'objID': 'invalid-ra'},
            ]).encode(),
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        provider = MASTProvider(client, max_response_bytes=10_000)
        catalog = CatalogDefinition('demo', 'mast', 'optical', endpoint='https://example.test')
        sources = await provider.query(catalog, Target(10.0, 5.0), 3.0)
        assert [source.source_id for source in sources] == ['valid']

        limited_provider = MASTProvider(client, max_response_bytes=1)
        with pytest.raises(CatalogQueryError, match='response exceeded'):
            await limited_provider.query(catalog, Target(10.0, 5.0), 3.0)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_irsa_provider_parses_ipac_tables() -> None:
    payload = b"""\\fixlen = T
\\RowsRetrieved = 1
| ra        | dec       | designation |
| double    | double    | char        |
|           |           |             |
 10.0        5.0         J001
"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={'content-type': 'text/plain'}, content=payload, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        provider = IRSAGatorProvider(client)
        catalog = CatalogDefinition('twomass_psc', 'irsa_gator', 'infrared', catalog='fp_psc')
        sources = await provider.query(catalog, Target(10.0, 5.0), 3.0)
        assert [source.source_id for source in sources] == ['J001']
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_sdss_provider_uses_cone_search_service_and_arcminutes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith('/ConeSearchService')
        assert request.url.params['sr'] == '0.05'
        return httpx.Response(
            200,
            headers={'content-type': 'text/plain'},
            content=b'#Table1\nobjid,ra,dec\n123,10.0,5.0\n',
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        provider = SDSSProvider(client)
        catalog = CatalogDefinition('sdss', 'sdss', 'optical')
        sources = await provider.query(catalog, Target(10.0, 5.0), 3.0)
        assert [source.source_id for source in sources] == ['123']
    finally:
        await client.aclose()


def test_registry_string_false_is_not_enabled(tmp_path) -> None:
    registry_path = tmp_path / 'registry.yaml'
    registry_path.write_text('catalogs:\n  disabled:\n    enabled: "false"\n    provider: none\n    wavelength: optical\n', encoding='utf-8')

    assert CatalogRegistry(registry_path).enabled_catalogs() == {}


@pytest.mark.asyncio
async def test_non_finite_radius_is_rejected() -> None:
    service = CrossmatchService(CatalogRegistry('app/registry.yaml'), {}, radius_arcsec=3.0)

    with pytest.raises(ValueError, match='finite number'):
        await service.crossmatch(10.0, 5.0, radius_arcsec=float('nan'))
