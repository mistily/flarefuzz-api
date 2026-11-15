# flarefuzz-api

A FastAPI service for conservatively interpreting and inventorying NASA PSI
combustion CSV headers. It does not forecast or combine datasets.

## Run locally

Requires Python 3.10 or later.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m uvicorn flarefuzz_api.main:app --reload
```

`POST /api/v1/normalize` accepts one CSV as multipart `file`. Optional form
fields are `provenance` (`experimental`, `simulated`, or `unknown`),
`experiment`, `simulation`, and `header_rows`. Its response is the parameter
inventory, including source column index/header, canonical name, source and SI
units, conversion factor, series identifier, and validation status. The
response also includes original `source_values` and separate `si_values`;
unknown or unconvertible values are `null` in the SI rows.

`GET /api/v1/search` queries the NASA PSI repository search endpoint. It accepts
`term`, `source`, `type`, `from`, `size`, `sort`, and `order`, then follows
NASA-hosted JSON/resource links and inventories linked CSV files. Search hits
without accessible CSV links are reported as not found rather than represented
as a fabricated inventory. The service does not follow redirects or download
non-NASA hosts.

Unknown units and uncertain parameter identities remain unconverted and are
marked `manual_review`. `K (mm²/sec)` is provisional D-squared-law burning or
evaporation data, not thermal diffusivity; cool-flame diameters and the two
diameter ratios have distinct identities; watts are not treated as radiance;
and gas-composition basis is not assumed to be mole fraction.

File ingestion uses a Strategy/Registry boundary (`FileParser` and
`ParserRegistry`). The MVP registers only `CSVParser`; unsupported formats
return HTTP 415. A future format can add its own parser strategy and register
it without changing the CSV normalizer or gathering logic.


Contributors:
@mistily https://github.com/mistily/
@Akshaya https://github.com/Akshaya-125

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest
```
