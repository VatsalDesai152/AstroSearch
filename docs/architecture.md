# Architecture

AstroSearch is a library with a small local web interface, four core production modules, and one YAML catalog registry.

```text
app/main.py
    └── app/service.py
            ├── app/models.py
            └── app/providers.py ── public astronomy catalog services
```

## Responsibilities

`app/models.py` defines the data contract (`Target`, `CatalogDefinition`, `CatalogSource`, `Match`, and `UnifiedRecord`), configuration, catalog loading, coordinate validation, source normalization, and JSON/CSV/VOTable parsers.

`app/providers.py` contains the provider contract and endpoint-specific adapters. All adapters return `CatalogSource` objects, so the rest of the application does not depend on a catalog's response format.

`app/service.py` plans enabled catalogs, executes provider calls concurrently with per-query timeouts, computes angular separations with Astropy, scores matches, and assembles the final result.

`app/main.py` is the public facade. Use `build_service` for a reusable service or `crossmatch` for a one-shot request. `app/web.py` serves the browser UI and translates `POST /api/crossmatch` requests into calls to the same public function.

The web server is intentionally small and suited to local use. It does not provide authentication, rate limiting, or production hosting features.
