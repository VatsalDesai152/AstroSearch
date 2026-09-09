

from __future__ import annotations

import csv
import io
import json
import math
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml
from astropy.io import ascii
from astropy.io.votable import parse as parse_votable
from astropy.coordinates import SkyCoord
from astropy import units as u

#error classes

class AstroSearchError(Exception):
    """Base exception for the cross-match engine."""


class InvalidCoordinateError(AstroSearchError):
    """Raised when RA/DEC values fail validation."""


class CatalogUnavailableError(AstroSearchError):
    """Raised when a catalog or provider is unavailable."""


class QueryTimeoutError(AstroSearchError):
    """Raised when a catalog query exceeds its timeout."""


class CatalogQueryError(AstroSearchError):
    """Raised when a provider request fails."""


class ResponseParseError(AstroSearchError):
    """Raised when a provider response cannot be parsed."""

#establishing data-types for fields
@dataclass(slots=True)
class Target:
    ra: float
    dec: float
    frame: str = 'icrs'
    epoch: float | None = None


@dataclass(slots=True)
class CatalogDefinition:
    name: str
    provider: str
    wavelength: str
    enabled: bool = True
    endpoint: str | None = None
    table: str | None = None
    catalog: str | None = None
    description: str | None = None
    query_method: str | None = None
    units: str | None = None
    parameters: dict[str, Any] = field(default_factory=dict)
    profiles: tuple[str, ...] = ()


@dataclass(slots=True)
class CatalogSource:
    catalog: str
    source_id: str
    ra: float
    dec: float
    positional_error_arcsec: float | None
    data: dict[str, Any]
    metadata: dict[str, Any]
    provenance: dict[str, Any]
    epoch: float | None = None
    proper_motion_ra_masyr: float | None = None
    proper_motion_dec_masyr: float | None = None
    position_uncertainty_arcsec: float | None = None


@dataclass(slots=True)
class QueryPlan:
    catalog: str
    provider: str
    endpoint: str | None
    parameters: dict[str, Any]
    radius_arcsec: float
    wavelength: str = 'unknown'


@dataclass(slots=True)
class Match:
    catalog: str
    source: CatalogSource
    separation_arcsec: float
    confidence: float


@dataclass(slots=True)
class CatalogFailure:
    catalog: str
    status: str = 'failed'
    error_type: str | None = None
    message: str | None = None


@dataclass(slots=True)
class UnifiedRecord:
    target: dict[str, Any]
    catalogs_queried: int
    catalog_results: dict[str, Any]
    counterparts: dict[str, list[dict[str, Any]]]
    failures: list[dict[str, Any]]
    provenance: dict[str, Any]
    crossmatch_groups: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation of this result."""

        return asdict(self)


class Settings:
    """Runtime settings sourced from environment variables or keyword overrides."""

    def __init__(self, **overrides: Any) -> None:
        def value(name: str, default: Any) -> Any:
            return overrides.get(name, os.getenv(name, default))

        self.app_name = str(value('APP_NAME', 'astro-crossmatch'))
        self.debug = str(value('DEBUG', 'false')).lower() == 'true'
        self.default_radius_arcsec = float(value('DEFAULT_RADIUS_ARCSEC', 3.0))
        self.request_timeout_seconds = float(value('REQUEST_TIMEOUT_SECONDS', 30.0))
        self.max_response_bytes = int(value('MAX_RESPONSE_BYTES', 10_000_000))
        if not math.isfinite(self.default_radius_arcsec) or self.default_radius_arcsec <= 0:
            raise ValueError('DEFAULT_RADIUS_ARCSEC must be finite and greater than zero.')
        if not math.isfinite(self.request_timeout_seconds) or self.request_timeout_seconds <= 0:
            raise ValueError('REQUEST_TIMEOUT_SECONDS must be finite and greater than zero.')
        if self.max_response_bytes <= 0:
            raise ValueError('MAX_RESPONSE_BYTES must be greater than zero.')
        self.registry_path = str(value('CATALOG_REGISTRY_PATH', Path(__file__).resolve().parent / 'registry.yaml'))
        self.log_level = str(value('LOG_LEVEL', 'INFO'))


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    normalized = str(value).strip().lower()
    if normalized in {'true', '1', 'yes', 'on'}:
        return True
    if normalized in {'false', '0', 'no', 'off'}:
        return False
    return default


class CatalogRegistry:
    """Load and expose enabled catalog definitions from a YAML registry."""

    def __init__(self, registry_path: str | os.PathLike[str] | None = None) -> None:
        default_path = Path(__file__).resolve().parent / 'registry.yaml'
        self.registry_path = Path(registry_path or default_path)
        self._catalogs: dict[str, CatalogDefinition] = {}
        self.reload()

    def reload(self) -> None:
        data = yaml.safe_load(self.registry_path.read_text(encoding='utf-8')) or {}
        self._catalogs = {}
        for name, payload in data.get('catalogs', {}).items():
            if not isinstance(payload, dict):
                continue
            self._catalogs[name] = CatalogDefinition(
                name=name,
                provider=str(payload.get('provider', 'unknown')),
                wavelength=str(payload.get('wavelength', 'unknown')),
                enabled=_as_bool(payload.get('enabled', True), default=True),
                endpoint=payload.get('endpoint'),
                table=payload.get('table'),
                catalog=payload.get('catalog'),
                description=payload.get('description'),
                query_method=payload.get('query_method'),
                units=payload.get('units'),
                parameters=dict(payload.get('parameters', {})),
                profiles=tuple(str(item) for item in payload.get('profiles', ())),
            )

    @property
    def catalogs(self) -> dict[str, CatalogDefinition]:
        return dict(self._catalogs)

    def enabled_catalogs(self) -> dict[str, CatalogDefinition]:
        return {name: catalog for name, catalog in self._catalogs.items() if catalog.enabled}

    def get(self, name: str) -> CatalogDefinition:
        return self._catalogs[name]


def _as_float(value: Any, name: str) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise InvalidCoordinateError(f'{name} must be numeric.') from exc
    if not math.isfinite(numeric):
        raise InvalidCoordinateError(f'{name} must be finite.')
    return numeric


def validate_target(ra: float | str, dec: float | str, *, frame: str = 'icrs', epoch: float | None = None) -> Target:
    """Validate coordinates and normalize right ascension to [0, 360)."""

    ra_value = _as_float(ra, 'ra') % 360.0
    dec_value = _as_float(dec, 'dec')
    if not -90.0 <= dec_value <= 90.0:
        raise InvalidCoordinateError('DEC must be within [-90, 90] degrees.')
    if epoch is not None:
        try:
            epoch = float(epoch)
        except (TypeError, ValueError) as exc:
            raise InvalidCoordinateError('epoch must be numeric.') from exc
        if not math.isfinite(epoch) or epoch < 1800 or epoch > 2200:
            raise InvalidCoordinateError('epoch must be a finite Julian year between 1800 and 2200.')
    try:
        coordinate = SkyCoord(ra=ra_value * u.deg, dec=dec_value * u.deg, frame=frame)
    except Exception as exc:
        raise InvalidCoordinateError(f'Unsupported coordinate frame: {frame}') from exc
    if not math.isfinite(coordinate.ra.deg) or not math.isfinite(coordinate.dec.deg):
        raise InvalidCoordinateError('Coordinate values are not finite.')
    return Target(ra=ra_value, dec=dec_value, frame=frame, epoch=epoch)


def normalize_field_names(record: Mapping[str, Any]) -> dict[str, Any]:
    aliases = {
        'ra': 'ra', 'ra_icrs': 'ra', 'raj2000': 'ra', 'ramean': 'ra',
        'dec': 'dec', 'dec_icrs': 'dec', 'dej2000': 'dec', 'decmean': 'dec',
        'source_id': 'source_id', 'objid': 'source_id', 'designation': 'source_id',
        'id': 'source_id', 'sourceid': 'source_id', 'main_id': 'source_id',
        'objname': 'source_id', 'prefname': 'source_id', 'pl_name': 'source_id', 'oid': 'source_id',
        'pmra': 'pmra', 'pm_ra': 'pmra', 'pmra_cosdec': 'pmra',
        'pmdec': 'pmdec', 'pm_dec': 'pmdec',
        'ref_epoch': 'epoch', 'epoch': 'epoch', 'obsepoch': 'epoch',
        'poserr': 'position_uncertainty_arcsec', 'pos_error': 'position_uncertainty_arcsec',
        'ra_error': 'position_uncertainty_arcsec', 'dec_error': 'position_uncertainty_arcsec',
        'parallax': 'parallax', 'plx_value': 'parallax',
        'z': 'redshift', 'redshift': 'redshift',
        'otype': 'object_type', 'objtype': 'object_type', 'morphology': 'object_type', 'prefphytype': 'object_type',
        'quality': 'quality_flags', 'quality_flag': 'quality_flags', 'flags': 'quality_flags',
    }
    normalized: dict[str, Any] = {}
    for key, value in record.items():
        clean_key = str(key).strip()
        normalized[aliases.get(clean_key.lower(), clean_key)] = value
    return normalized


def normalize_source_record(raw_record: Mapping[str, Any]) -> dict[str, Any]:
    """Canonicalize provider row names without inventing missing coordinates."""

    values = normalize_field_names(raw_record)
    if 'ra' in values:
        values['ra'] = float(values['ra'])
    if 'dec' in values:
        values['dec'] = float(values['dec'])
    if 'source_id' in values:
        values['source_id'] = str(values['source_id'])
    for key in ('pmra', 'pmdec', 'epoch', 'position_uncertainty_arcsec', 'parallax', 'redshift'):
        if key in values and values[key] not in (None, ''):
            try:
                values[key] = float(values[key])
            except (TypeError, ValueError):
                values.pop(key, None)
    return values


def parse_json_records(payload: str | bytes | dict[str, Any] | list[Any]) -> list[dict[str, Any]]:
    data = json.loads(payload) if isinstance(payload, (str, bytes)) else payload
    if isinstance(data, dict):
        rows = data.get('data', data.get('results'))
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
        return [data]
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    return []


def parse_csv_records(payload: str | bytes) -> list[dict[str, Any]]:
    text = payload.decode('utf-8') if isinstance(payload, bytes) else payload
    csv_text = '\n'.join(line for line in text.splitlines() if not line.lstrip().startswith('#'))
    return [dict(row) for row in csv.DictReader(io.StringIO(csv_text))]


def parse_ipac_records(payload: str | bytes) -> list[dict[str, Any]]:
    """Parse an IPAC ASCII table, including IRSA Gator responses."""

    text = payload.decode('utf-8') if isinstance(payload, bytes) else payload
    table = ascii.read(text, format='ipac')
    return [{name: row[name] for name in table.colnames} for row in table]


def parse_votable_records(payload: bytes | io.BytesIO) -> list[dict[str, Any]]:
    table = parse_votable(io.BytesIO(payload) if isinstance(payload, bytes) else payload)
    first_table = table.get_first_table()
    return [{key: value for key, value in zip(first_table.columns.names, row)} for row in first_table.array]


def build_provenance(
    catalog: str,
    *,
    provider: str,
    source_id: str,
    endpoint: str | None,
    query_parameters: Mapping[str, object],
    search_radius_arcsec: float,
) -> dict[str, object]:
    from datetime import datetime, timezone

    return {
        'catalog': catalog,
        'provider': provider,
        'source_id': source_id,
        'retrieved_at': datetime.now(timezone.utc).isoformat(),
        'endpoint': endpoint,
        'search_radius_arcsec': search_radius_arcsec,
        'query_parameters': dict(query_parameters),
    }
