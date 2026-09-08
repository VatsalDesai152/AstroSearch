# Usage and extension guide

## One-shot requests

```python
from app.main import crossmatch

result = await crossmatch(187.277920, 2.052390, radius_arcsec=5)
payload = result.as_dict()
```

`crossmatch` creates and closes its HTTP client for the request. Use `build_service` with a caller-owned client for batch work.

## Custom registry

Copy `app/registry.yaml`, change `enabled`, `provider`, `endpoint`, `table`, or `catalog`, and pass the file path:

```python
from app.main import build_service

service = build_service(registry_path="config/my-registry.yaml")
```

Disabled entries are not queried. Unknown provider names are reported as catalog failures rather than stopping other catalog queries.

## Adding a provider

1. Add a `CatalogProvider` implementation to `app/providers.py`.
2. Convert the remote response into `CatalogSource` objects using `_sources`.
3. Register the provider name in `provider_map`.
4. Add a matching `provider` value to the YAML registry.
5. Add parser or provider tests to `tests/test_astrosearch.py`.

## Operational notes

- Catalog endpoints are external services and can be unavailable or rate-limited.
- The service runs queries concurrently, but each query has a timeout.
- Use a small search radius and reuse an HTTP client for batch work.
- Provider failures are part of the returned record, so callers should inspect `failures`.
