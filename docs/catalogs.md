
# Catalogs

## Gaia DR3
- Provider: TAP
- Endpoint: https://gea.esac.esa.int/tap-server/tap/sync
- Protocol: ADQL/TAP
- Wavelength: optical
- Units: degrees
- Table: `gaiadr3.gaia_source`
- Query method: positional ADQL cone search

## 2MASS PSC
- Provider: IRSA Gator
- Endpoint: https://irsa.ipac.caltech.edu/cgi-bin/Gator/nph-query
- Protocol: HTTP cone search
- Wavelength: infrared
- Units: arcseconds
- Catalog: `fp_psc`
- Query method: positional cone search

## AllWISE
- Provider: IRSA Gator
- Endpoint: https://irsa.ipac.caltech.edu/cgi-bin/Gator/nph-query
- Protocol: HTTP cone search
- Wavelength: infrared
- Units: arcseconds
- Catalog: `allwise_p3as_psd`
- Query method: positional cone search

## Pan-STARRS DR2
- Provider: MAST
- Endpoint: https://catalogs.mast.stsci.edu/api/v0.1/panstarrs/dr2/mean
- Protocol: JSON API
- Wavelength: optical
- Units: arcseconds
- Query method: positional search

## SDSS
- Provider: SDSS
- Endpoint: https://skyserver.sdss.org/dr18/SkyServerWS/ConeSearch/ConeSearch
- Protocol: Cone Search
- Wavelength: optical
- Units: arcseconds
- Query method: positional cone search

## FIRST / NVSS / VLASS / LoTSS / ROSAT / Chandra / XMM
- Provider: HEASARC Xamin
- Endpoint: https://heasarc.gsfc.nasa.gov/xamin/query
- Protocol: Xamin HTTP query
- Wavelength: radio/x-ray
- Units: arcseconds
- Query method: positional search
