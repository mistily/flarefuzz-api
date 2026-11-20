# flarefuzz-api

A FastAPI service for conservatively interpreting and inventorying NASA PSI
combustion CSV headers, with frontend APIs for browsing investigations,
normalizing datasets, descriptive model fitting, evidence-grounded answers, SI-based
transport-property calculations, and an illustrative controlled-flame envelope model.

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
- `POST /api/calculations/dimensionless` is the deterministic, SI-only
  dimensionless-analysis stage between normalized inputs and future simulations.
  It calculates Reynolds (`Re`), Prandtl (`Pr`), Schmidt (`Sc`), and Lewis
  (`Le`) numbers. Every input includes source/reference provenance; results
  include equations, dimensionless units, input lineage, assumptions, and a
  formula-set version. `GET /api/calculations/dimensionless/formulas` lists the
  required fields and formulas. The API requires explicit SI-valued inputs and
  does not infer transport properties from arbitrary CSV headers.
- `POST /api/simulations/spherical-droplet-flame` returns a time-indexed,
  spherical flame-envelope projection from a detected droplet size through the
  model's predicted remaining droplet-burnout time. The first version uses the
  D-squared law and an explicitly supplied constant flame/droplet diameter
  ratio. It requires timezone-aware detection time, SI measurements, fixed
  ambient temperature/pressure/oxygen/velocity, and provenance for every input.
  The environmental values are recorded as fixed context; this simple model
  does not use them to infer combustion parameters. Results include envelope
  diameter, radius, volume, surface area, formulas, assumptions, and lineage.
  Burnout is only an endpoint proxy, not a validated flame-extinction
  prediction, and detection time does not reconstruct pre-detection history.

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
