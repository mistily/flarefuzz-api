# flarefuzz-api

A FastAPI service for conservatively interpreting and inventorying NASA PSI
combustion CSV headers, with frontend APIs for browsing investigations,
normalizing datasets, descriptive model fitting, and evidence-grounded answers.

## Run locally

Requires Python 3.10 or later.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m uvicorn flarefuzz_api.main:app --reload
```

## Frontend API

- `GET /api/investigations?term=combustion&offset=0&limit=20` searches NASA PSI
  investigations. `GET /api/investigations/{id}` returns the PSI metadata and
  NASA-hosted source-file links found in that record.
- `POST /api/datasets/ingest` accepts a CSV upload as multipart `file`; optional
  fields are `provenance`, `experiment`, `simulation`, and `header_rows`. It
  returns a dataset `id` for use with the quality and series endpoints.
- `GET /api/datasets/{id}/quality` returns validation issues and unit mappings.
  `GET /api/datasets/{id}/series` returns normalized SI values where conversion
  is supported; unconvertible values are `null`.
- `POST /api/models/fit` accepts `{ "dataset_id": "...", "model": "linear",
  "x_series": "...", "y_series": "..." }` and fits ordinary least squares
  to complete finite pairs. The run response includes predictions, residuals,
  residual standard error, and an approximate 95% uncertainty interval.
  `GET /api/models/runs/{id}` retrieves that result.
- `POST /api/questions` accepts `{ "dataset_id": "...", "question": "..." }`.
  The MVP returns descriptive statistics only when the question names a series;
  otherwise it abstains and lists available series. It does not generate
  literature-backed claims or infer causation.

Dataset and model-run records are currently held in process memory. They are
lost on restart and are not shared across multiple workers; persistent storage
is needed for production deployment. NASA PSI endpoints are accessed without
following redirects. The legacy `POST /api/v1/normalize` and
`GET /api/v1/search` routes remain available.

## Normalization behavior

Unknown units and uncertain parameter identities remain unconverted and are
marked `manual_review`. `K (mm²/sec)` is provisional D-squared-law burning or
evaporation data, not thermal diffusivity; cool-flame diameters and the two
diameter ratios have distinct identities; watts are not treated as radiance;
and gas-composition basis is not assumed to be mole fraction.

File ingestion uses a Strategy/Registry boundary (`FileParser` and
`ParserRegistry`). The MVP registers only `CSVParser`; unsupported formats
return HTTP 415.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Contributors:
@mistily https://github.com/mistily/
@Akshaya https://github.com/Akshaya-125
