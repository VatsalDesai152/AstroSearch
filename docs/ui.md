# Browser UI

AstroSearch includes a small dependency-free browser interface backed by the same `crossmatch` function used by the Python API.

## Start it locally

From the repository root:

```bash
python -m app.web
```

Then open <http://127.0.0.1:8000>. The host and port can be changed:

```bash
python -m app.web --host 127.0.0.1 --port 8080
```

The page accepts right ascension and declination in decimal degrees plus a search radius in arcseconds. Submit a query to see matches grouped by wavelength, catalog failures, and the raw JSON response.

## HTTP API

The UI calls:

```http
POST /api/crossmatch
Content-Type: application/json

{"ra": 187.277920, "dec": 2.052390, "radius_arcsec": 3}
```

Successful responses use `UnifiedRecord.as_dict()`. Invalid input returns HTTP 400, oversized request bodies return HTTP 413, and unexpected server failures return HTTP 500. `GET /health` returns `{"status":"ok"}`.

## Operational notes

- The UI runs catalog queries against the configured public endpoints. A query can take up to the configured request timeout, and individual catalog failures are shown without hiding successful results.
- Bind to `127.0.0.1` for local use. Put an authenticated reverse proxy and production-grade server in front of it before exposing it publicly.
- Configuration is read from the same environment variables described in [.env.example](../.env.example).
