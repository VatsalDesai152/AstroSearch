# Providers

Each provider implements:

```python
async def query(
    catalog: CatalogDefinition,
    target: Target,
    radius_arcsec: float,
) -> list[CatalogSource]
```

Provider classes are in `app/providers.py`:

- `TapProvider` builds ADQL and supports JSON, CSV, and VOTable responses.
- `IRSAGatorProvider` sends IRSA cone-search requests.
- `MASTProvider` queries the Pan-STARRS JSON API.
- `SDSSProvider` requests SDSS CSV cone-search results.
- `HEASARCXaminProvider` queries HEASARC Xamin JSON results.

All requests can share an injected `httpx.AsyncClient`. This is recommended for applications making more than one request because it reuses HTTP connections and gives the caller control over lifecycle and transport configuration.
