# AstroSearch

AstroSearch is a Python library and lightweight local web app for cross-matching a sky position against a configurable set of public astronomy catalogs. It validates and normalizes coordinates, queries enabled providers concurrently, parses heterogeneous responses, calculates angular separations, and returns a provenance-rich `UnifiedRecord`.

## Installation

```bash
python -m venv .venv
. .venv/bin/activate          # Windows: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

The project requires Python 3.12 or newer. The editable install includes the
catalog registry and browser UI as package data.

## Quick start

```python
import asyncio

from app.main import crossmatch


async def main() -> None:
    result = await crossmatch(187.277920, 2.052390, radius_arcsec=3.0)
    print(result.as_dict())


asyncio.run(main())
```

## Browser UI

Start the basic local interface with:

```bash
python -m app.web
```

Or, after installing the package, run:

```bash
astrosearch-web
```

Open <http://127.0.0.1:8000> and enter decimal-degree coordinates and a search radius. The HTML page is served by the Python backend and sends a `POST` request to `/api/crossmatch`; no separate frontend build is required. See [Browser UI documentation](docs/ui.md) for the HTTP endpoint and deployment notes.

You can also call the backend directly:

```bash
curl -X POST http://127.0.0.1:8000/api/crossmatch \
  -H "Content-Type: application/json" \
  -d '{"ra": 187.277920, "dec": 2.052390, "radius_arcsec": 3}'
```

For long-running applications, create one service and reuse one `httpx.AsyncClient`:

```python
import httpx

from app.main import build_service


async def run(ra: float, dec: float) -> dict:
    async with httpx.AsyncClient(follow_redirects=True) as client:
        service = build_service(client=client)
        result = await service.crossmatch(ra, dec)
        return result.as_dict()
```

## Configuration

Configuration is read from environment variables. See [.env.example](.env.example) for the complete list.

The catalog registry is [app/registry.yaml](app/registry.yaml). Set `CATALOG_REGISTRY_PATH` to use another registry. Each entry declares its provider, wavelength, endpoint, and provider-specific catalog or table name.

## Supported providers

- TAP/ADQL: Gaia DR3
- IRSA Gator: 2MASS PSC and AllWISE
- MAST: Pan-STARRS DR2
- SDSS Cone Search
- HEASARC Xamin: radio and X-ray catalogs

See [docs/catalogs.md](docs/catalogs.md) for the registry inventory.

## Production layout

Production code is organized into these modules:

- [app/models.py](app/models.py): data models, settings, registry loading, validation, normalization, and parsers.
- [app/providers.py](app/providers.py): HTTP adapters for each catalog protocol.
- [app/service.py](app/service.py): query planning/execution, matching, caching, and result assembly.
- [app/main.py](app/main.py): public construction and convenience functions.
- [app/web.py](app/web.py): local HTTP server and browser API.
- [app/static/index.html](app/static/index.html): browser UI.

The test suite is consolidated into [tests/test_astrosearch.py](tests/test_astrosearch.py).

## Testing

```bash
python -m pytest -q
python -m ruff check .
```

Tests cover validation, parsing, matching, registry loading, and service construction. Network catalog calls are not required for the unit tests.

## Documentation

- [Architecture](docs/architecture.md)
- [Catalog registry](docs/catalogs.md)
- [Crossmatching behavior](docs/crossmatching.md)
- [Provider contract](docs/providers.md)
- [Usage and extension guide](docs/usage.md)
- [Browser UI](docs/ui.md)

## GitHub workflow

Push this directory to a new GitHub repository. The included GitHub Actions
workflow runs the test suite and Ruff on every push and pull request. Generated
files, virtual environments, local `.env` files, and caches are excluded by
`.gitignore`.
"# AstroSearch" 
