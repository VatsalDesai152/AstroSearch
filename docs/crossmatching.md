# Crossmatching

1. RA is converted to a finite float and normalized to `[0, 360)` degrees.
2. DEC is required to be within `[-90, 90]` degrees.
3. Every enabled catalog receives the same target and search radius.
4. Provider rows are normalized to canonical `ra`, `dec`, and `source_id` fields.
5. Astropy `SkyCoord` computes the target-to-source separation in arcseconds.
6. Sources outside the requested radius are discarded.
7. Remaining matches are sorted by separation and receive a confidence score from 0 to 1.

The result retains raw provider data and query provenance for each source. A provider failure is recorded under `failures` while successful catalog results remain available.

## Result shape

`UnifiedRecord.as_dict()` returns:

- `target`: normalized target coordinates.
- `catalogs_queried`: successful plus failed catalog count.
- `catalog_results`: raw normalized sources grouped by catalog.
- `counterparts`: matched sources grouped by wavelength.
- `failures`: catalog-specific errors.
- `provenance`: search radius and match summary.
